"""Direct employer application links (spec section 6.3).

Aggregate-board links are upgraded to the employer's own posting when it can
be located, via three mechanisms:

 1. cross-source duplicate merging during filtering — when the same job
    arrives from several sources, the direct link wins and the board link
    becomes the secondary "also listed on" link (see automation._merge_duplicate)
 2. ATS board probing — most employers host jobs on one of a handful of ATS
    platforms, all of which expose public JSON job boards. We derive likely
    company slugs, probe Greenhouse / Lever / Ashby / SmartRecruiters /
    Workable / Recruitee, and fuzzy-match the job title against the company's
    actual board to obtain the exact posting URL.
 3. a JSearch lookup as a last resort (budgeted per cycle — it costs requests).

Board discoveries AND misses are cached per company for 30 days.
"""
import html as _html
import json
import logging
import re
from datetime import datetime, timedelta

import httpx

from . import config, db
from .job_search import US_STATE_NAMES

log = logging.getLogger("links")

_STATE_BY_NAME = {v.lower(): k for k, v in US_STATE_NAMES.items()}
_STATE_CODES = set(US_STATE_NAMES)


def _state_code(token: str):
    t = (token or "").strip()
    if not t:
        return None
    if t.upper() in _STATE_CODES:
        return t.upper()
    return _STATE_BY_NAME.get(t.lower())


def _loc_is_remote(s: str) -> bool:
    return bool(re.search(r"\bremote\b|\banywhere\b|work from home|\bwfh\b", s or "", re.I))

import sys as _sys
if hasattr(_sys.stdout, "reconfigure"):  # ensure tool output never crashes on non-ASCII
    try:
        _sys.stdout.reconfigure(encoding="utf-8")
        _sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

AGGREGATOR_DOMAINS = (
    "adzuna.com", "indeed.com", "linkedin.com", "ziprecruiter.com", "glassdoor.com",
    "jooble.org", "talent.com", "monster.com", "careerbuilder.com", "simplyhired.com",
    "lensa.com", "snagajob.com", "whatjobs.com", "jobrapido.com", "neuvoo.com",
    "talentify.io", "bebee.com", "jobs2careers.com", "themuse.com", "getwork.com",
    "jobgether.com", "jobted.com", "jobilize.com", "learn4good.com", "adzuna.co.uk",
    "salary.com", "jobcase.com", "joblist.com", "theladders.com", "upward.careers",
)

_LEGAL_SUFFIXES = {"llc", "inc", "incorporated", "corp", "corporation", "ltd",
                   "limited", "co", "company", "plc", "llp", "lp", "group", "holdings"}


def normalize_company(name: str) -> str:
    """Fold 'Sizzling Platter, LLC' and 'Sizzling Platter' to the same key."""
    tokens = re.sub(r"[^a-z0-9 ]", " ", (name or "").lower()).split()
    while tokens and tokens[-1] in _LEGAL_SUFFIXES:
        tokens.pop()
    return " ".join(tokens)


def host_of(url: str) -> str:
    m = re.match(r"https?://([^/:]+)", url or "")
    host = (m.group(1).lower() if m else "")
    return host[4:] if host.startswith("www.") else host


def is_direct_link(url: str) -> bool:
    """A link is 'direct' when its host is not a known aggregate board.
    Company sites and ATS hosts (Workday, Taleo, iCIMS, Greenhouse, ...)
    all count as direct — they are the employer's own application flow."""
    host = host_of(url)
    return bool(host) and not any(host == d or host.endswith("." + d) for d in AGGREGATOR_DOMAINS)


# ------------------------------------------------------------ title match --

_STOP_TOKENS = {"the", "a", "an", "of", "and", "or", "for", "to", "in", "at"}


def _title_tokens(title: str) -> set:
    return set(re.findall(r"[a-z0-9]+", (title or "").lower())) - _STOP_TOKENS


def titles_match(a: str, b: str) -> bool:
    ta, tb = _title_tokens(a), _title_tokens(b)
    if not ta or not tb:
        return False
    overlap = len(ta & tb)
    return overlap / min(len(ta), len(tb)) >= 0.75


def _best_match(board: list, title: str):
    """board: list of (title, url). Returns the URL of the best title match."""
    best, best_score = None, 0.0
    target = _title_tokens(title)
    if not target:
        return None
    for board_title, url in board:
        tokens = _title_tokens(board_title)
        if not tokens or not url:
            continue
        score = len(target & tokens) / min(len(target), len(tokens))
        if score > best_score:
            best, best_score = url, score
    return best if best_score >= 0.75 else None


# ------------------------------------------------------------- ATS boards --

def slug_variants(company: str, domain: str = None) -> list:
    out = []
    if domain:
        d = host_of(domain if "://" in domain else "https://" + domain)
        first = (d or "").split(".")[0]
        if first:
            out.append(first)
    norm = normalize_company(company)
    if norm:
        out.append(norm.replace(" ", ""))
        out.append(norm.replace(" ", "-"))
    seen, result = set(), []
    for s in out:
        s = re.sub(r"[^a-z0-9-]", "", s)
        if 2 <= len(s) <= 40 and s not in seen:
            seen.add(s)
            result.append(s)
    return result[:4]


def _get_json(url: str, timeout: float = 8, **kwargs):
    try:
        resp = httpx.get(url, timeout=timeout, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (JobSearchAssistant link check)"},
                         **kwargs)
        if resp.status_code == 200:
            return resp.json()
    except Exception:
        pass
    return None


ATS_NAMES = ["greenhouse", "lever", "ashby", "smartrecruiters", "workable", "recruitee",
             "bamboohr", "icims"]

# URL shapes that reveal a company's board — used to LEARN mappings for free
# from any direct link that passes through the pipeline.
ATS_URL_PATTERNS = [
    ("greenhouse", re.compile(r"https?://(?:boards|job-boards)\.greenhouse\.io/(?:embed/job_board\?for=)?([A-Za-z0-9]+)")),
    ("lever", re.compile(r"https?://jobs\.(?:eu\.)?lever\.co/([A-Za-z0-9-]+)")),
    ("ashby", re.compile(r"https?://jobs\.ashbyhq\.com/([A-Za-z0-9.-]+)")),
    ("smartrecruiters", re.compile(r"https?://(?:jobs|careers)\.smartrecruiters\.com/([A-Za-z0-9]+)")),
    ("smartrecruiters", re.compile(r"https?://api\.smartrecruiters\.com/v1/companies/([A-Za-z0-9]+)")),
    ("workable", re.compile(r"https?://(?:apply|[a-z0-9-]+)\.workable\.com/(?:j/|view/|api/[^\"]*?/accounts/)?([a-z0-9-]+)")),
    ("recruitee", re.compile(r"https?://([a-z0-9-]+)\.recruitee\.com")),
    ("bamboohr", re.compile(r"https?://([a-z0-9-]+)\.bamboohr\.com")),
    ("workday", re.compile(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([A-Za-z0-9_-]+)")),
    # learner-only ATSes (no public board API, but knowing the tenant still
    # confirms direct links and feeds future adapters)
    ("icims", re.compile(r"https?://careers-([a-z0-9-]+)\.icims\.com")),
    ("taleo", re.compile(r"https?://[a-z0-9]+\.tbe\.taleo\.net/[^\s\"?]*\?(?:[^\"\s]*&)?org=([A-Za-z0-9]+)")),
    ("taleo", re.compile(r"https?://([a-z0-9]+)\.taleo\.net")),
    ("jobvite", re.compile(r"https?://jobs\.jobvite\.com/([A-Za-z0-9-]+)")),
    ("paradox", re.compile(r"https?://[a-z0-9.-]*paradox\.ai/co/([A-Za-z0-9-]+)")),
    ("successfactors", re.compile(r"https?://careers?\d*\.successfactors\.(?:com|eu)/[^\s\"]*?company=([A-Za-z0-9]+)")),
    ("ukgpro", re.compile(r"https?://recruiting2?\.ultipro\.com/([A-Za-z0-9]+)")),
    ("adp", re.compile(r"https?://workforcenow\.adp\.com/[^\s\"]*?cid=([A-Za-z0-9-]+)")),
]


_BAD_ATS_SLUGS = {"www", "jobs", "careers", "career", "embed", "search", "job",
                  "job_board", "en", "us", "company", "about", "apply"}


def match_ats(url: str):
    """If a URL is a recognizable ATS board, return (ats, slug); else None."""
    for ats, pattern in ATS_URL_PATTERNS:
        m = pattern.search(url or "")
        if not m:
            continue
        slug = "|".join(m.groups()) if ats == "workday" else m.group(1)
        if not slug or slug.lower() in _BAD_ATS_SLUGS:
            continue
        return ats, slug
    return None


def cache_mapping(company: str, ats: str, slug: str, domain: str = None) -> None:
    key = normalize_company(company)
    if not key:
        return
    db.execute(
        "INSERT OR REPLACE INTO company_ats_cache "
        "(company_key, ats, slug, board_found, domain, created_at) VALUES (?, ?, ?, 1, ?, ?)",
        (key, ats, slug, domain, db.now()))


def learn_from_url(company: str, url: str) -> bool:
    """If a direct link exposes the company's ATS board, cache the mapping —
    no probing needed. Called for every direct-link job in the pipeline."""
    key = normalize_company(company)
    if not key or not url:
        return False
    hit = match_ats(url)
    if hit:
        cache_mapping(company, hit[0], hit[1])
        return True
    return False


# ----------------------------------------------------- careers-page detect --

# A few high-yield entry points; homepage link-following covers the long tail.
CAREERS_PATHS = ["/careers", "/jobs", "/careers/jobs"]


def _fetch_html(url: str) -> str:
    try:
        resp = httpx.get(url, timeout=6, follow_redirects=True,
                         headers={"User-Agent": "Mozilla/5.0 (JobSearchAssistant careers check)"})
        if resp.status_code == 200 and "text/html" in resp.headers.get("content-type", ""):
            return resp.text[:400000]
    except Exception:
        pass
    return ""


def detect_ats_in_html(html: str):
    """Scan a careers page's HTML for an embedded/linked ATS. Returns
    (ats, slug) of the first recognizable board, else None."""
    if not html:
        return None
    # all absolute URLs in href/src/data attributes and inline JS config
    for url in re.findall(r'https?://[^\s"\'<>)]+', html):
        hit = match_ats(url)
        if hit:
            return hit
    # Greenhouse embed often appears as  for="company"  near grnhse markup
    m = re.search(r'grnhse[^>]*?for=["\']([A-Za-z0-9]+)["\']', html, re.I) or \
        re.search(r'greenhouse[^>]*?["\']([A-Za-z0-9]+)["\'][^>]*?job_board', html, re.I)
    if m and m.group(1).lower() not in _BAD_ATS_SLUGS:
        return "greenhouse", m.group(1)
    return None


def resolve_from_website(company: str, domain: str):
    """Fetch the company's own careers pages and detect the ATS behind a
    vanity careers domain. Caches and returns the ats name, else None.
    Lean: ~5 fetches max — direct careers paths, the careers subdomain, and
    one homepage link-follow."""
    if not domain:
        return None
    host = host_of(domain if "://" in domain else "https://" + domain)
    if not host:
        return None
    tried = set()

    def _try(url):
        if url in tried:
            return None
        tried.add(url)
        return detect_ats_in_html(_fetch_html(url))

    # 1) direct careers paths + the careers. subdomain (highest yield)
    candidates = [f"https://{host}/careers", f"https://{host}/jobs",
                  f"https://careers.{host}", f"https://jobs.{host}"]
    for url in candidates:
        hit = _try(url)
        if hit:
            cache_mapping(company, hit[0], hit[1], domain=host)
            log.info("Careers-page ATS for %s: %s/%s via %s", company, hit[0], hit[1], url)
            return hit[0]

    # 2) homepage: detect inline, else follow one careers link
    home = _fetch_html(f"https://{host}")
    hit = detect_ats_in_html(home)
    if not hit and home:
        link = re.search(r'href=["\']([^"\']*(?:career|/jobs)[^"\']*)["\']', home, re.I)
        if link:
            nxt = link.group(1)
            if nxt.startswith("/"):
                nxt = f"https://{host}" + nxt
            if nxt.startswith("http"):
                hit = _try(nxt)
    if hit:
        cache_mapping(company, hit[0], hit[1], domain=host)
        return hit[0]
    return None


def fetch_board(ats: str, slug: str):
    """Returns list of (title, posting_url) from the company's public job
    board, or None when the board does not exist on that ATS."""
    if ats == "greenhouse":
        data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        if data and isinstance(data.get("jobs"), list):
            return [(j.get("title", ""), j.get("absolute_url", "")) for j in data["jobs"]]
    elif ats == "lever":
        data = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        if isinstance(data, list):
            return [(j.get("text", ""), j.get("hostedUrl", "")) for j in data]
    elif ats == "ashby":
        data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        if data and isinstance(data.get("jobs"), list):
            return [(j.get("title", ""), j.get("jobUrl") or j.get("applyUrl", "")) for j in data["jobs"]]
    elif ats == "smartrecruiters":
        data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings")
        if data and isinstance(data.get("content"), list):
            return [(j.get("name", ""), f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}")
                    for j in data["content"]]
    elif ats == "workable":
        data = _get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=false")
        if data and isinstance(data.get("jobs"), list):
            return [(j.get("title", ""), j.get("url") or j.get("shortlink", "")) for j in data["jobs"]]
    elif ats == "recruitee":
        data = _get_json(f"https://{slug}.recruitee.com/api/offers/")
        if data and isinstance(data.get("offers"), list):
            return [(j.get("title", ""), j.get("careers_url", "")) for j in data["offers"]]
    elif ats == "bamboohr":
        data = _get_json(f"https://{slug}.bamboohr.com/careers/list")
        if data and isinstance(data.get("result"), list):
            return [(j.get("jobOpeningName", ""),
                     f"https://{slug}.bamboohr.com/careers/{j.get('id')}")
                    for j in data["result"]]
    elif ats == "workday":
        # slug is the composite "tenant|wdN|site" learned from a Workday URL
        try:
            tenant, wd, site = slug.split("|")
        except ValueError:
            return None
        return _workday_jobs(tenant, wd, site, "")
    elif ats == "icims":
        # iCIMS has no public JSON board; parse the hosted search page HTML.
        try:
            resp = httpx.get(
                f"https://careers-{slug}.icims.com/jobs/search?ss=1&in_iframe=1",
                headers={"User-Agent": "Mozilla/5.0 (JobSearchAssistant link check)"},
                timeout=10, follow_redirects=True)
        except Exception:
            return None
        if resp.status_code != 200 or "icims.com" not in str(resp.url):
            return None
        out = []
        for m in re.finditer(
                r'href="(https://careers-' + re.escape(slug) +
                r'\.icims\.com/jobs/\d+/[^/"]+/job)[^"]*"[^>]*>(.*?)</a>',
                resp.text, re.S):
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()
            title = re.sub(r"^job title\s*", "", title, flags=re.I).strip()
            if title and len(title) > 2:
                out.append((title, m.group(1)))
        return out if out else []
    return None


def _workday_jobs(tenant: str, wd: str, site: str, search_text: str):
    """Workday's public job-board JSON (the CXS API behind myworkdayjobs.com)."""
    try:
        resp = httpx.post(
            f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
            json={"limit": 20, "offset": 0, "searchText": search_text or "",
                  "appliedFacets": {}},
            headers={"User-Agent": "Mozilla/5.0 (JobSearchAssistant link check)",
                     "Content-Type": "application/json"},
            timeout=10)
        if resp.status_code != 200:
            return None
        postings = resp.json().get("jobPostings") or []
        base = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
        return [(p.get("title", ""), base + (p.get("externalPath") or ""))
                for p in postings if p.get("title")]
    except Exception:
        return None


# ----------------------------------------------- detailed board fetching ---
# fetch_board (above) returns (title, url) pairs — enough to resolve a single
# job's direct link. The daily cache (app/job_cache.py) needs the FULL posting:
# location, salary, description, posted date. These adapters pull everything the
# board's public list endpoint exposes in ONE request per board (with detail
# params where they're free), so a full crawl stays cheap. Platforms whose list
# endpoint omits the description (SmartRecruiters, BambooHR, Workday, iCIMS)
# return has_detail=False so callers know the text is title-only.

def _strip_html(s: str) -> str:
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r"\n\s*\n+", "\n\n", s)
    return s.strip()[:6000]


def _epoch_ms_to_date(ms):
    try:
        return datetime.utcfromtimestamp(int(ms) / 1000).date().isoformat()
    except (TypeError, ValueError, OSError):
        return None


def _row(title, link, *, location=None, city=None, state=None, is_remote=False,
         salary=None, description=None, department=None, employment_type=None,
         posted_date=None):
    """Build a normalized cache row, or None if it lacks a title or link."""
    title = (title or "").strip()
    if not title or not link:
        return None
    desc = (description or "").strip() or None
    return {
        "title": title[:300], "apply_link": link,
        "location": (location or "").strip() or None, "city": city, "state": state,
        "is_remote": bool(is_remote), "salary": salary, "description": desc,
        "department": department, "employment_type": employment_type,
        "posted_date": posted_date, "has_detail": bool(desc),
    }


def fetch_board_detailed(ats: str, slug: str) -> list:
    """Return full job dicts for the daily cache (see module note above).
    Always returns a list (empty when the board is missing/unreachable)."""
    try:
        fn = _DETAILED_FETCHERS.get(ats)
        if fn:
            return [r for r in fn(slug) if r]
    except Exception:
        log.debug("Detailed fetch failed for %s/%s", ats, slug, exc_info=True)
    return []


def _gh_detailed(slug):
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true", timeout=15)
    out = []
    for j in (data or {}).get("jobs") or []:
        depts = j.get("departments") or []
        out.append(_row(
            j.get("title"), j.get("absolute_url"),
            location=(j.get("location") or {}).get("name"),
            description=_strip_html(j.get("content")),
            department=(depts[0].get("name") if depts else None),
            posted_date=(j.get("updated_at") or "")[:10] or None))
    return out


def _lever_detailed(slug):
    data = _get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=15)
    out = []
    for j in data if isinstance(data, list) else []:
        cat = j.get("categories") or {}
        salary = None
        sr = j.get("salaryRange") or {}
        if sr.get("min") and sr.get("max"):
            unit = "/hour" if "hour" in (sr.get("interval") or "").lower() else "/year"
            salary = f"${sr['min']:,.0f} - ${sr['max']:,.0f}{unit}"
        out.append(_row(
            j.get("text"), j.get("hostedUrl"),
            location=cat.get("location"),
            is_remote="remote" in (j.get("workplaceType") or "").lower(),
            salary=salary,
            description=j.get("descriptionPlain") or _strip_html(j.get("description")),
            department=cat.get("department") or cat.get("team"),
            employment_type=cat.get("commitment"),
            posted_date=_epoch_ms_to_date(j.get("createdAt"))))
    return out


def _ashby_detailed(slug):
    data = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}?includeCompensation=true", timeout=15)
    out = []
    for j in (data or {}).get("jobs") or []:
        summary = (j.get("compensation") or {}).get("compensationTierSummary")
        out.append(_row(
            j.get("title"), j.get("jobUrl") or j.get("applyUrl"),
            location=j.get("location"), is_remote=bool(j.get("isRemote")),
            salary=summary.strip() if isinstance(summary, str) and summary.strip() else None,
            description=j.get("descriptionPlain") or _strip_html(j.get("descriptionHtml")),
            department=j.get("departmentName") or j.get("department") or j.get("team"),
            employment_type=j.get("employmentType"),
            posted_date=(j.get("publishedAt") or j.get("publishedDate") or "")[:10] or None))
    return out


def _sr_detailed(slug):
    data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100", timeout=15)
    out = []
    for j in (data or {}).get("content") or []:
        loc = j.get("location") or {}
        city, region = loc.get("city"), loc.get("region")
        out.append(_row(
            j.get("name"), f"https://jobs.smartrecruiters.com/{slug}/{j.get('id')}",
            location=", ".join(p for p in (city, region) if p) or None,
            city=city, state=region, is_remote=bool(loc.get("remote")),
            employment_type=(j.get("typeOfEmployment") or {}).get("label"),
            department=(j.get("department") or {}).get("label"),
            posted_date=(j.get("releasedDate") or "")[:10] or None))
    return out


def _workable_detailed(slug):
    # The widget API puts location fields flat on each job (city/state/country),
    # not nested under a "location" key.
    data = _get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true", timeout=15)
    out = []
    for j in (data or {}).get("jobs") or []:
        city, region, country = j.get("city"), j.get("state"), j.get("country")
        out.append(_row(
            j.get("title"), j.get("url") or j.get("application_url") or j.get("shortlink"),
            location=", ".join(p for p in (city, region) if p) or country,
            city=city, state=region, is_remote=bool(j.get("telecommuting")),
            description=_strip_html(j.get("description")),
            department=j.get("department"), employment_type=j.get("employment_type"),
            posted_date=(j.get("created_at") or j.get("published_on") or "")[:10] or None))
    return out


def _recruitee_detailed(slug):
    data = _get_json(f"https://{slug}.recruitee.com/api/offers/", timeout=15)
    out = []
    for j in (data or {}).get("offers") or []:
        city, country = j.get("city"), j.get("country")
        out.append(_row(
            j.get("title"), j.get("careers_url"),
            location=j.get("location") or ", ".join(p for p in (city, country) if p) or None,
            city=city, is_remote=bool(j.get("remote")),
            description=_strip_html(j.get("description")),
            department=j.get("department"), employment_type=j.get("employment_type_code"),
            posted_date=(j.get("created_at") or "")[:10] or None))
    return out


def _bamboohr_detailed(slug):
    data = _get_json(f"https://{slug}.bamboohr.com/careers/list", timeout=15)
    out = []
    for j in (data or {}).get("result") or []:
        loc = j.get("location")
        if isinstance(loc, dict):
            city, state = loc.get("city"), loc.get("state")
            location = ", ".join(p for p in (city, state) if p) or None
        else:
            city = state = None
            location = (str(loc).strip() or None) if loc else None
        out.append(_row(
            j.get("jobOpeningName"), f"https://{slug}.bamboohr.com/careers/{j.get('id')}",
            location=location, city=city, state=state, is_remote=bool(j.get("isRemote")),
            department=j.get("departmentLabel"), employment_type=j.get("employmentStatusLabel")))
    return out


def _workday_detailed(slug):
    try:
        tenant, wd, site = slug.split("|")
    except ValueError:
        return []
    base = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
    api = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs"
    out, offset = [], 0
    for _ in range(10):  # cap ~200 postings per board
        try:
            resp = httpx.post(api, json={"limit": 20, "offset": offset, "searchText": "", "appliedFacets": {}},
                              headers={"User-Agent": "Mozilla/5.0 (JobSearchAssistant link check)",
                                       "Content-Type": "application/json"}, timeout=12)
            if resp.status_code != 200:
                break
            payload = resp.json()
        except Exception:
            break
        postings = payload.get("jobPostings") or []
        if not postings:
            break
        for p in postings:
            out.append(_row(p.get("title"), base + (p.get("externalPath") or ""),
                            location=p.get("locationsText")))
        offset += 20
        if offset >= (payload.get("total") or 0) or offset >= 200:
            break
    return out


def _icims_detailed(slug):
    return [_row(title, url) for title, url in (fetch_board("icims", slug) or [])]


_DETAILED_FETCHERS = {
    "greenhouse": _gh_detailed, "lever": _lever_detailed, "ashby": _ashby_detailed,
    "smartrecruiters": _sr_detailed, "workable": _workable_detailed,
    "recruitee": _recruitee_detailed, "bamboohr": _bamboohr_detailed,
    "workday": _workday_detailed, "icims": _icims_detailed,
}


# ------------------------------------------ state-filtered + enriched fetch --
# Workday/SmartRecruiters/BambooHR expose huge boards (Workday alone ~640k
# postings across the cache, only ~4% in Utah) and omit the description from
# their list feed. For the daily cache we instead pull ONLY the target-state (+
# remote) postings from these platforms, then enrich each with a per-job detail
# request to recover the full description and precise location. The full-detail
# platforms (Greenhouse/Lever/Ashby/Workable/Recruitee) already return
# descriptions in one request, so they go through fetch_board_detailed and the
# crawl trims them to the target state afterwards.
#
# Stubs returned here carry an "_enrich" dict telling enrich_job() how to fetch
# the per-job detail; the crawl enriches them concurrently in a second phase.

_STATE_FILTERED = {"workday", "smartrecruiters", "bamboohr"}

_CXS_HEADERS = {"User-Agent": "Mozilla/5.0 (JobSearchAssistant link check)",
                "Content-Type": "application/json"}


def fetch_board_for_cache(ats: str, slug: str, state_name: str, state_code: str) -> list:
    """Return cache-ready job dicts for `slug`. For the big partial-detail
    platforms this is pre-filtered to `state_name`/remote with an enrichment
    handle attached; for the rest it's the full detailed board."""
    try:
        if ats == "workday":
            return _workday_state_stubs(slug, state_name, state_code)
        if ats == "smartrecruiters":
            return _sr_state_stubs(slug, state_code)
        if ats == "bamboohr":
            return _bamboohr_state_stubs(slug, state_code)
        if ats == "icims":
            return _icims_stubs(slug, state_code)
        return fetch_board_detailed(ats, slug)
    except Exception:
        log.debug("Cache fetch failed for %s/%s", ats, slug, exc_info=True)
        return []


def _parse_workday_location(loc: str):
    """Workday detail locations read 'US, UT, Salt Lake City' (country, state,
    city). Return (city, state_code, is_remote)."""
    parts = [p.strip() for p in re.split(r"[,/]", loc or "") if p.strip()]
    state = None
    for p in parts:
        sc = _state_code(p)
        if sc:
            state = sc
            break
    city = None
    for p in reversed(parts):
        low = p.lower()
        if _state_code(p) or low in ("us", "usa") or "united states" in low or low == "remote":
            continue
        city = p
        break
    return city, state, _loc_is_remote(loc)


def _workday_state_stubs(slug, state_name, state_code):
    try:
        tenant, wd, site = slug.split("|")
    except ValueError:
        return []
    cxs = f"https://{tenant}.{wd}.myworkdayjobs.com/wday/cxs/{tenant}/{site}"
    site_url = f"https://{tenant}.{wd}.myworkdayjobs.com/{site}"
    out, offset = [], 0
    for _ in range(75):  # cap ~1500 state stubs per board
        try:
            resp = httpx.post(f"{cxs}/jobs",
                              json={"limit": 20, "offset": offset, "searchText": state_name, "appliedFacets": {}},
                              headers=_CXS_HEADERS, timeout=12)
            if resp.status_code != 200:
                break
            payload = resp.json()
        except Exception:
            break
        posts = payload.get("jobPostings") or []
        if not posts:
            break
        for p in posts:
            ext = p.get("externalPath") or ""
            r = _row(p.get("title"), site_url + ext, location=p.get("locationsText"))
            if r:
                r["_enrich"] = {"ats": "workday", "url": f"{cxs}{ext}", "want": state_code}
                out.append(r)
        offset += 20
        if offset >= (payload.get("total") or 0):
            break
    return out


def _sr_state_stubs(slug, state_code):
    out, offset = [], 0
    for _ in range(30):  # scan up to 3000 postings/board
        data = _get_json(
            f"https://api.smartrecruiters.com/v1/companies/{slug}/postings?limit=100&offset={offset}", timeout=15)
        content = (data or {}).get("content") or []
        if not content:
            break
        for j in content:
            loc = j.get("location") or {}
            region, city, remote = loc.get("region"), loc.get("city"), bool(loc.get("remote"))
            if _state_code(region or "") != state_code and not remote:
                continue
            jid = j.get("id")
            r = _row(j.get("name"), f"https://jobs.smartrecruiters.com/{slug}/{jid}",
                     location=", ".join(p for p in (city, region) if p) or None,
                     city=city, state=region, is_remote=remote,
                     employment_type=(j.get("typeOfEmployment") or {}).get("label"),
                     department=(j.get("department") or {}).get("label"),
                     posted_date=(j.get("releasedDate") or "")[:10] or None)
            if r:
                r["_enrich"] = {"ats": "smartrecruiters",
                                "url": f"https://api.smartrecruiters.com/v1/companies/{slug}/postings/{jid}"}
                out.append(r)
        if len(content) < 100:
            break
        offset += 100
    return out


def _bamboohr_state_stubs(slug, state_code):
    data = _get_json(f"https://{slug}.bamboohr.com/careers/list", timeout=15)
    out = []
    for j in (data or {}).get("result") or []:
        loc = j.get("location") if isinstance(j.get("location"), dict) else {}
        city, state, remote = loc.get("city"), loc.get("state"), bool(j.get("isRemote"))
        if _state_code(state or "") != state_code and not remote:
            continue
        jid = j.get("id")
        r = _row(j.get("jobOpeningName"), f"https://{slug}.bamboohr.com/careers/{jid}",
                 location=", ".join(p for p in (city, state) if p) or None,
                 city=city, state=state, is_remote=remote,
                 department=j.get("departmentLabel"), employment_type=j.get("employmentStatusLabel"))
        if r:
            r["_enrich"] = {"ats": "bamboohr", "url": f"https://{slug}.bamboohr.com/careers/{jid}/detail"}
            out.append(r)
    return out


def _icims_stubs(slug, state_code):
    """iCIMS exposes no list-level location and no server-side state filter, so
    we enumerate every posting (paginated) and let enrichment discover each
    job's location. The per-job detail cache (job_cache) ensures each posting is
    fetched only once across crawls."""
    pat = re.compile(r'href="(https://careers-' + re.escape(slug) +
                     r'\.icims\.com/jobs/\d+/[^/"]+/job)[^"]*"[^>]*>(.*?)</a>', re.S)
    out, seen = [], set()
    for pr in range(0, 15):  # up to ~300 postings/board
        try:
            resp = httpx.get(f"https://careers-{slug}.icims.com/jobs/search?ss=1&in_iframe=1&pr={pr}",
                             headers={"User-Agent": "Mozilla/5.0 (JobSearchAssistant link check)"},
                             timeout=12, follow_redirects=True)
        except Exception:
            break
        if resp.status_code != 200:
            break
        found = 0
        for m in pat.finditer(resp.text):
            url = m.group(1)
            if url in seen:
                continue
            seen.add(url)
            found += 1
            title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", m.group(2))).strip()
            title = re.sub(r"^job title\s*", "", title, flags=re.I).strip()
            r = _row(title, url)
            if r:
                r["_enrich"] = {"ats": "icims", "url": url, "want": state_code}
                out.append(r)
        if found < 20:
            break
    return out


def enrich_job(j: dict) -> None:
    """Fill in a stub's description (and, for Workday/iCIMS, its precise
    location) via a per-job detail request. Mutates `j` in place; network only."""
    e = j.get("_enrich") or {}
    try:
        if e.get("ats") == "workday":
            _enrich_workday(j, e)
        elif e.get("ats") == "smartrecruiters":
            _enrich_sr(j, e)
        elif e.get("ats") == "bamboohr":
            _enrich_bamboohr(j, e)
        elif e.get("ats") == "icims":
            _enrich_icims(j, e)
    except Exception:
        log.debug("Enrich failed for %s", e.get("url"), exc_info=True)
    j.pop("_enrich", None)


def _enrich_workday(j, e):
    d = (_get_json(e["url"], timeout=12) or {}).get("jobPostingInfo") or {}
    want = e.get("want")
    chosen = None
    for loc in [d.get("location") or ""] + (d.get("additionalLocations") or []):
        c, st, rem = _parse_workday_location(loc)
        if st == want:
            chosen = (loc, c, st, rem)
            break
    if not chosen:
        loc = d.get("location") or j.get("location") or ""
        chosen = (loc,) + _parse_workday_location(loc)
    loc, c, st, rem = chosen
    j["location"], j["city"], j["state"], j["is_remote"] = loc or j.get("location"), c, st, rem
    j["apply_link"] = d.get("externalUrl") or j.get("apply_link")
    j["employment_type"] = d.get("timeType") or j.get("employment_type")
    j["posted_date"] = (d.get("startDate") or j.get("posted_date") or "")[:10] or None
    desc = _strip_html(d.get("jobDescription"))
    if desc:
        j["description"], j["has_detail"] = desc, True


def _enrich_sr(j, e):
    secs = ((_get_json(e["url"], timeout=15) or {}).get("jobAd") or {}).get("sections") or {}
    parts = [_strip_html((secs.get(k) or {}).get("text"))
             for k in ("jobDescription", "qualifications", "additionalInformation")]
    desc = "\n\n".join(p for p in parts if p)
    if desc:
        j["description"], j["has_detail"] = desc[:6000], True


def _enrich_bamboohr(j, e):
    res = (_get_json(e["url"], timeout=15) or {}).get("result") or {}
    raw = res.get("description") or (res.get("jobOpening") or {}).get("description") or ""
    desc = _strip_html(raw)
    if desc:
        j["description"], j["has_detail"] = desc, True


def _iter_jsonld(data):
    if isinstance(data, list):
        for x in data:
            yield from _iter_jsonld(x)
    elif isinstance(data, dict):
        if "@graph" in data:
            yield from _iter_jsonld(data["@graph"])
        else:
            yield data


_ICIMS_LOC_RE = re.compile(r"US-([A-Z]{2})-([A-Za-z.'\-]+(?:\s+[A-Za-z.'\-]+){0,3})")


def _enrich_icims(j, e):
    # The standalone job URL often redirects to the company's own marketing
    # page; the ?in_iframe=1 view renders the real iCIMS job content (location
    # as "US-XX-City", plus the Overview/Responsibilities/Qualifications body).
    url = e["url"]
    fetch_url = url + ("&" if "?" in url else "?") + "in_iframe=1"
    try:
        resp = httpx.get(fetch_url, headers={"User-Agent": "Mozilla/5.0 (JobSearchAssistant link check)"},
                         timeout=12, follow_redirects=True)
        if resp.status_code != 200:
            return
        html = resp.text
    except Exception:
        return
    want = e.get("want")

    # 1) JSON-LD JobPosting, when a portal exposes it
    for m in re.finditer(r'<script[^>]*application/ld\+json[^>]*>(.*?)</script>', html, re.S | re.I):
        try:
            data = json.loads(m.group(1).strip())
        except (ValueError, TypeError):
            continue
        for o in _iter_jsonld(data):
            t = o.get("@type")
            if t == "JobPosting" or (isinstance(t, list) and "JobPosting" in t):
                _apply_jobposting(j, o, want)
                if j.get("state") or j.get("is_remote"):
                    return

    # 2) HTML fallback: canonical "US-XX-City" location tokens
    city, state, remote = _icims_location(html, want)
    if state or city or remote:
        j["location"] = ", ".join(p for p in (city, state) if p) or ("Remote" if remote else j.get("location"))
        j["city"], j["state"], j["is_remote"] = city, state, remote
    desc = _icims_description(html)
    if desc:
        j["description"], j["has_detail"] = desc, True


def _icims_location(html, want):
    tokens, seen = [], set()
    for st, city in _ICIMS_LOC_RE.findall(html):
        key = (st, city.strip())
        if key not in seen:
            seen.add(key)
            tokens.append((city.strip(), st))
    if not tokens:
        return None, None, _loc_is_remote(html[:4000])
    chosen = next((t for t in tokens if t[1] == want), tokens[0])
    city, state = chosen
    remote = "remote" in city.lower()
    return (None if remote else city), state, remote


def _icims_description(html):
    start = re.search(r'(Overview|Responsibilities|Job\s+Summary|Position\s+Summary)', html)
    if not start:
        return ""
    seg = html[start.start():]
    for marker in ("Options</", "Apply for this job", "Share on your", "Need help", "Sorry the Share"):
        i = seg.find(marker)
        if i > 200:
            seg = seg[:i]
            break
    return _strip_html(seg)


def _apply_jobposting(j, o, want):
    """Populate a stub from a schema.org JobPosting object (JSON-LD)."""
    remote = (o.get("jobLocationType") or "").upper() == "TELECOMMUTE"
    locs = o.get("jobLocation")
    locs = locs if isinstance(locs, list) else [locs]
    best = None  # prefer a location in the wanted state
    for loc in locs:
        addr = (loc or {}).get("address") if isinstance(loc, dict) else None
        if not isinstance(addr, dict):
            continue
        city = addr.get("addressLocality")
        st = _state_code(addr.get("addressRegion") or "")
        if st == want:
            best = (city, st)
            break
        if best is None:
            best = (city, st)
    city, state = best or (None, None)
    loc_str = ", ".join(p for p in (city, state) if p) or ("Remote" if remote else None)
    if loc_str:
        j["location"] = loc_str
    j["city"], j["state"], j["is_remote"] = city, state, (remote or _loc_is_remote(loc_str or ""))
    dp = o.get("datePosted")
    if dp:
        j["posted_date"] = str(dp)[:10]
    et = o.get("employmentType")
    if et:
        j["employment_type"] = et if isinstance(et, str) else ", ".join(et)
    desc = _strip_html(o.get("description"))
    if desc:
        j["description"], j["has_detail"] = desc, True


_SAMPLE_BOARD_PATTERN = re.compile(r"\(sample\)|\(voorbeeld\)|test job|dummy", re.I)


def _looks_like_sample_board(board: list) -> bool:
    """Reject abandoned demo boards (e.g. Recruitee's 'Senior Marketer (Sample)'
    placeholders) that squat on big-company slugs."""
    if not board:
        return True
    sample_hits = sum(1 for title, _ in board if _SAMPLE_BOARD_PATTERN.search(title or ""))
    return sample_hits >= max(1, len(board) // 2) and len(board) <= 5


def discover_company(company: str, domain: str = None):
    """Locate (and cache) the company's ATS job board. Returns
    {"ats", "slug"} when a board exists, else None. Fresh cache entries are
    honoured; stale ones are re-probed."""
    key = normalize_company(company)
    if not key:
        return None
    row = db.query_one("SELECT * FROM company_ats_cache WHERE company_key = ?", (key,))
    if row:
        age = datetime.now() - datetime.strptime(row["created_at"], "%Y-%m-%d %H:%M:%S")
        if age < timedelta(days=30):
            return {"ats": row["ats"], "slug": row["slug"]} if row["board_found"] else None
        db.execute("DELETE FROM company_ats_cache WHERE company_key = ?", (key,))

    for slug in slug_variants(company, domain):
        for ats in ATS_NAMES:
            board = fetch_board(ats, slug)
            if board and not _looks_like_sample_board(board):
                db.execute(
                    "INSERT OR REPLACE INTO company_ats_cache "
                    "(company_key, ats, slug, board_found, domain, created_at) VALUES (?, ?, ?, 1, ?, ?)",
                    (key, ats, slug, domain, db.now()))
                log.info("ATS board found: %s -> %s/%s", company, ats, slug)
                return {"ats": ats, "slug": slug}

    # slug-probing failed — try detecting an ATS embedded on the company's own
    # careers page (catches server-rendered embeds and odd ATSes).
    if domain and resolve_from_website(company, domain):
        row = db.query_one("SELECT ats, slug FROM company_ats_cache WHERE company_key = ?", (key,))
        if row and row["board_found"]:
            return {"ats": row["ats"], "slug": row["slug"]}

    db.execute(
        "INSERT OR REPLACE INTO company_ats_cache "
        "(company_key, ats, slug, board_found, domain, created_at) VALUES (?, NULL, NULL, 0, ?, ?)",
        (key, domain, db.now()))
    return None


def ats_direct_link(company: str, title: str, domain: str = None):
    """Find the specific job posting on the company's own ATS board."""
    found = discover_company(company, domain)
    if not found:
        return None
    if found["ats"] == "workday":
        # large boards: use Workday's own search instead of paging everything
        try:
            tenant, wd, site = found["slug"].split("|")
        except ValueError:
            return None
        board = _workday_jobs(tenant, wd, site, title) or []
    else:
        board = fetch_board(found["ats"], found["slug"]) or []
    return _best_match(board, title)


# --------------------------------------------------------- JSearch resort --

def jsearch_direct_link(company: str, title: str):
    """Last resort: look the job up on JSearch and harvest a direct apply
    option. Costs one API request — callers budget this."""
    if not config.JSEARCH_API_KEY:
        return None
    try:
        resp = httpx.get(
            "https://jsearch.p.rapidapi.com/search",
            params={"query": f"{title} {company}", "num_pages": 1},
            headers={"X-RapidAPI-Key": config.JSEARCH_API_KEY,
                     "X-RapidAPI-Host": "jsearch.p.rapidapi.com"},
            timeout=30)
        if resp.status_code != 200:
            return None
        items = resp.json().get("data") or []
    except Exception:
        return None
    want_company = normalize_company(company)
    for item in items[:6]:
        if normalize_company(item.get("employer_name", "")) != want_company:
            continue
        if not titles_match(item.get("job_title", ""), title):
            continue
        site = item.get("employer_website") or ""
        domain = host_of(site if "://" in site else "https://" + site)
        for option in item.get("apply_options") or []:
            link = option.get("apply_link", "")
            if link and is_direct_link(link) and (option.get("is_direct") or (domain and domain in link)):
                return link
        link = item.get("job_apply_link", "")
        if is_direct_link(link):
            return link
    return None


# ---------------------------------------------------------------- driver --

def upgrade_links(jobs: list, user_id=None, jsearch_budget: int = 3) -> int:
    """Upgrade aggregate-board apply links to direct employer links in place.
    Returns the number of jobs upgraded."""
    upgraded = 0
    jsearch_used = 0
    for job in jobs:
        try:
            if is_direct_link(job.get("apply_link", "")):
                continue
            direct = ats_direct_link(job.get("company", ""), job.get("title", ""),
                                     domain=job.get("company_domain"))
            via = "ATS board"
            if not direct and jsearch_used < jsearch_budget:
                jsearch_used += 1
                direct = jsearch_direct_link(job.get("company", ""), job.get("title", ""))
                via = "JSearch lookup"
            if direct and direct != job.get("apply_link"):
                if not job.get("aggregate_link"):
                    job["aggregate_link"] = job.get("apply_link")
                job["apply_link"] = direct
                upgraded += 1
                db.log("links", user_id,
                       f"Direct link found via {via}: {job.get('title')} at {job.get('company')} -> {direct}")
        except Exception as exc:
            log.warning("Link upgrade failed for %s at %s: %s",
                        job.get("title"), job.get("company"), exc)
    return upgraded
