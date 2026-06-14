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
import logging
import re
from datetime import datetime, timedelta

import httpx

from . import config, db

log = logging.getLogger("links")

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


def _get_json(url: str, **kwargs):
    try:
        resp = httpx.get(url, timeout=8, follow_redirects=True,
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
