"""Daily local cache of every open posting on the cached employer ATS boards.

The link_resolver discovered ~1,300 Utah-employer ATS boards. Rather than use
them only to translate aggregator links into direct ones, this module crawls
all of them once a day and stores every open posting (with as much detail as
each board exposes) in the cached_jobs table. The main automation cycle then
treats that cache as a first-class job source alongside the live job APIs.

crawl_all_boards()  — rebuilds the cache (concurrent fetch, main-thread writes,
                      stale-posting pruning). Run daily by the scheduler.
search_cached_jobs()— returns the most relevant cached postings for one user,
                      shaped exactly like a live provider's results so they flow
                      through the existing filter/dedup/scoring pipeline.

DB writes happen only on the calling thread; worker threads do network I/O
only (SQLite connections are thread-local — see db.get_conn).
"""
import concurrent.futures
import hashlib
import logging
import re
from datetime import datetime, timedelta

from . import config, db, link_resolver
from .job_search import US_STATE_NAMES

log = logging.getLogger("job_cache")

_STATE_BY_NAME = {v.lower(): k for k, v in US_STATE_NAMES.items()}
_STATE_CODES = set(US_STATE_NAMES.keys())
_REMOTE_RE = re.compile(r"\bremote\b|\banywhere\b|work from home|\bwfh\b", re.I)
_STOPWORDS = {"the", "and", "for", "with", "job", "jobs", "role", "position",
              "senior", "junior", "lead", "staff", "manager", "specialist"}


# ----------------------------------------------------------------- helpers ---

def _norm_state(token: str):
    t = (token or "").strip()
    if not t:
        return None
    if t.upper() in _STATE_CODES:
        return t.upper()
    return _STATE_BY_NAME.get(t.lower())


def split_location(loc, *, city=None, state=None, is_remote=False):
    """Best-effort (city, state_code, is_remote) from a free-text location plus
    any structured hints the ATS already supplied."""
    remote = bool(is_remote) or bool(loc and _REMOTE_RE.search(loc))
    state_code = _norm_state(state)
    city_out = (city or "").strip() or None
    if loc:
        parts = [p.strip() for p in loc.split(",") if p.strip()]
        if not city_out and parts:
            city_out = parts[0]
        if not state_code:
            for p in parts[1:]:
                sc = _norm_state(p)
                if sc:
                    state_code = sc
                    break
    return city_out, state_code, remote


def _company_display(company_key: str) -> str:
    return " ".join(w.capitalize() for w in (company_key or "").split()) or "Unknown"


def _fingerprint(company_key: str, title: str, location: str) -> str:
    raw = f"{company_key}|{(title or '').strip().lower()}|{(location or '').strip().lower()}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


# ------------------------------------------------------------- the crawler ---

def crawl_all_boards(workers: int = 12, limit: int = None, log_fn=None) -> dict:
    """Fetch every cached board and rebuild cached_jobs. Returns a stats dict."""
    log_fn = log_fn or (lambda m: log.info("%s", m))
    if not db.acquire_lock("board_crawl", max_age_minutes=180):
        log_fn("Board crawl skipped: previous run still in progress")
        return {"skipped": True, "reason": "previous run in progress"}
    started = db.now()
    try:
        boards = db.query(
            "SELECT company_key, ats, slug, domain FROM company_ats_cache "
            "WHERE board_found = 1 AND ats IS NOT NULL AND slug IS NOT NULL")
        if limit:
            boards = boards[:limit]
        log_fn(f"Board crawl starting: {len(boards)} boards")
        stats = {"boards": len(boards), "boards_ok": 0, "boards_empty": 0,
                 "jobs_upserted": 0, "by_ats": {}}
        crawled_keys = set()

        def _fetch(board):
            return board, link_resolver.fetch_board_detailed(board["ats"], board["slug"])

        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as ex:
            done = 0
            for fut in concurrent.futures.as_completed([ex.submit(_fetch, b) for b in boards]):
                done += 1
                try:
                    board, jobs = fut.result()
                except Exception:
                    continue
                ats = board["ats"]
                a = stats["by_ats"].setdefault(ats, {"boards": 0, "jobs": 0, "detail": 0})
                a["boards"] += 1
                crawled_keys.add(board["company_key"])  # reachable -> eligible for pruning
                if not jobs:
                    stats["boards_empty"] += 1
                    continue
                stats["boards_ok"] += 1
                for j in jobs:
                    _upsert(board, j, started)
                    stats["jobs_upserted"] += 1
                    a["jobs"] += 1
                    if j.get("has_detail"):
                        a["detail"] += 1
                if done % 250 == 0:
                    log_fn(f"  ...crawled {done}/{len(boards)} boards, {stats['jobs_upserted']} jobs so far")

        stats["pruned"] = _prune(crawled_keys, started)
        total = db.query_one("SELECT COUNT(*) AS n FROM cached_jobs") or {"n": 0}
        stats["cache_total"] = total["n"]
        db.set_setting("last_board_crawl_at", started)
        db.set_setting("last_board_crawl_stats", db.jdumps(stats))
        log_fn(f"Board crawl done: {stats['jobs_upserted']} postings from "
               f"{stats['boards_ok']} boards, pruned {stats['pruned']}, "
               f"cache now holds {stats['cache_total']}")
        return stats
    finally:
        db.release_lock("board_crawl")


def _upsert(board, j, seen_at) -> None:
    ck = board["company_key"]
    city, state, remote = split_location(
        j.get("location"), city=j.get("city"), state=j.get("state"), is_remote=j.get("is_remote"))
    fp = _fingerprint(ck, j["title"], j.get("location"))
    db.execute(
        "INSERT INTO cached_jobs (fingerprint, company_key, company_name, ats, title, apply_link, "
        "location, city, state, is_remote, salary, description, department, employment_type, "
        "posted_date, has_detail, first_seen, last_seen) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(fingerprint) DO UPDATE SET "
        "apply_link=excluded.apply_link, location=excluded.location, city=excluded.city, "
        "state=excluded.state, is_remote=excluded.is_remote, "
        "salary=COALESCE(excluded.salary, cached_jobs.salary), "
        "description=CASE WHEN excluded.has_detail=1 THEN excluded.description ELSE cached_jobs.description END, "
        "department=COALESCE(excluded.department, cached_jobs.department), "
        "employment_type=COALESCE(excluded.employment_type, cached_jobs.employment_type), "
        "posted_date=COALESCE(excluded.posted_date, cached_jobs.posted_date), "
        "has_detail=MAX(excluded.has_detail, cached_jobs.has_detail), "
        "company_name=excluded.company_name, ats=excluded.ats, last_seen=excluded.last_seen",
        (fp, ck, _company_display(ck), board["ats"], j["title"], j.get("apply_link"),
         j.get("location"), city, state, 1 if remote else 0, j.get("salary"),
         j.get("description"), j.get("department"), j.get("employment_type"),
         j.get("posted_date"), 1 if j.get("has_detail") else 0, seen_at, seen_at))


def _prune(crawled_keys, started) -> int:
    """Remove postings that have disappeared from boards we successfully reached
    this run (last_seen older than this run), plus a TTL safety net for boards
    that dropped out of the ATS cache entirely. Boards that were briefly
    unreachable keep their rows (not in crawled_keys, so untouched here)."""
    pruned = 0
    keys = list(crawled_keys)
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        placeholders = ",".join("?" * len(chunk))
        with db.tx() as conn:
            cur = conn.execute(
                f"DELETE FROM cached_jobs WHERE last_seen < ? AND company_key IN ({placeholders})",
                (started, *chunk))
            pruned += cur.rowcount
    cutoff = (datetime.now() - timedelta(days=config.CACHED_JOB_TTL_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    with db.tx() as conn:
        cur = conn.execute("DELETE FROM cached_jobs WHERE last_seen < ?", (cutoff,))
        pruned += cur.rowcount
    return pruned


# --------------------------------------------------- per-user cache search ---

def _user_terms(user) -> set:
    bits = []
    bits += (user.get("preferred_positions") or "").replace(",", " ").split()
    bits += [str(s) for s in db.jloads(user.get("search_keywords"), [])]
    bits += [str(s) for s in db.jloads(user.get("skills"), [])]
    bits += [str(s) for s in db.jloads(user.get("industry_preferences"), [])]
    bits.append(user.get("additional_skills") or "")
    terms = set()
    for token in re.findall(r"[a-z0-9]{3,}", " ".join(bits).lower()):
        if token not in _STOPWORDS:
            terms.add(token)
    return terms


def _relevance(terms: set, title: str, description: str) -> int:
    """Cheap Tier-1 score: title hits weigh more than description hits."""
    if not terms:
        return 1  # no profile signal -> keep (recency order decides)
    title_l = (title or "").lower()
    desc_l = (description or "")[:1000].lower()
    score = 0
    for t in terms:
        if t in title_l:
            score += 3
        elif t in desc_l:
            score += 1
    return score


def _to_provider_dict(row) -> dict:
    """Shape a cached_jobs row like a live provider result so it flows through
    automation._filter_candidates unchanged."""
    is_remote = bool(row.get("is_remote"))
    location = "Remote" if is_remote else (row.get("location") or "")
    return {
        "title": row["title"],
        "company": row.get("company_name") or _company_display(row["company_key"]),
        "company_domain": None,
        "location": location,
        "latitude": None, "longitude": None,
        "salary": row.get("salary"),
        "salary_source": "Job listing" if row.get("salary") else None,
        "description": row.get("description") or "",
        "apply_link": row.get("apply_link"),
        "aggregate_link": None,
        "posted_days_ago": 0,  # currently open on the board -> always fresh
        "applicant_count": None,
        "source": "ats_cache",
    }


def search_cached_jobs(user, top_n: int = 120, *, include_local: bool = True,
                       include_remote: bool = True) -> list:
    """Return up to `top_n` of the most relevant cached postings for `user`.
    Local (in-state) postings are included when `include_local`; remote postings
    when `include_remote` — mirroring the job-type filters the caller applies to
    the live providers, so a remote-only seeker isn't shown on-site roles."""
    sc = _norm_state((user.get("state") or "").strip().upper())
    if include_local and sc and include_remote:
        where, params = "(state = ? OR is_remote = 1)", [sc]
    elif include_local and sc:
        where, params = "state = ? AND is_remote = 0", [sc]  # local-only: no remote roles
    elif include_remote:
        where, params = "is_remote = 1", []
    else:
        return []  # no location anchor -> nothing local to offer
    rows = db.query(
        f"SELECT * FROM cached_jobs WHERE {where} ORDER BY last_seen DESC LIMIT 2000",
        tuple(params))
    if not rows:
        return []
    terms = _user_terms(user)
    scored = []
    for r in rows:
        rel = _relevance(terms, r["title"], r.get("description"))
        if rel > 0:
            scored.append((rel, r))
    scored.sort(key=lambda x: -x[0])
    return [_to_provider_dict(r) for _, r in scored[:top_n]]


def last_crawl_summary() -> dict:
    """For the admin dashboard / status checks."""
    return {
        "last_crawl_at": db.get_setting("last_board_crawl_at", ""),
        "stats": db.jloads(db.get_setting("last_board_crawl_stats", ""), {}),
        "cache_total": (db.query_one("SELECT COUNT(*) AS n FROM cached_jobs") or {"n": 0})["n"],
    }
