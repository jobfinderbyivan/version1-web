"""Enumerate REAL ATS tenants from the Common Crawl web index and cache every
board that has Utah postings.

Unlike fill_utah_ats_cache.py (which guesses slugs from company names), this
works backwards: Common Crawl knows every boards.greenhouse.io/X,
jobs.lever.co/X, *.myworkdayjobs.com/X ... URL the web crawler has ever seen.
We extract the tenant slugs, fetch each board once, check whether any posting
is located in Utah, and cache hits under the company's real name.

Every checked tenant is recorded in enumerated_tenants, so repeated runs are
incremental — run it as long as you like.

Usage: python tools/enumerate_ats_tenants.py [--cc-pages 3] [--max-per-ats 1200]
                                             [--workers 6] [--enumerate-only]
"""
import argparse
import json
import random
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app import db, link_resolver as lr  # noqa: E402

UA = {"User-Agent": "UtahEmployerResearch/1.0 (personal job-search tool; "
                    "contact: ivandoublejr@gmail.com)"}
CC_INDEX_HOST = "https://index.commoncrawl.org"

UTAH_MARKERS = [
    "utah", ", ut", " ut ", "salt lake", "provo", "lehi", "ogden", "draper",
    "sandy, ", "orem", "murray, ", "layton", "logan, ", "st. george", "saint george",
    "park city", "american fork", "west valley", "south jordan", "west jordan",
    "taylorsville", "herriman", "riverton", "eagle mountain", "spanish fork",
    "springville", "pleasant grove", "cedar city", "kaysville", "bountiful",
    "clearfield", "midvale", "tooele", "vineyard, ", "saratoga springs",
]

BAD_SLUGS = {"www", "jobs", "careers", "embed", "sitemap", "api", "wday", "login",
             "js", "css", "static", "assets", "favicon", "robots", "search", "404"}

LOCALE_RE = re.compile(r"^[a-z]{2}(-[A-Z]{2})?$")


def utah_text(text: str) -> bool:
    t = " " + (text or "").lower() + " "
    return any(m in t for m in UTAH_MARKERS)


# ------------------------------------------------- Common Crawl harvesting --

def cc_newest_index() -> str:
    resp = httpx.get(f"{CC_INDEX_HOST}/collinfo.json", headers=UA, timeout=60)
    return resp.json()[0]["id"]


def cc_query(index_id: str, pattern: str, pages: int) -> list:
    """Sample up to `pages` pages of CC index results for a URL pattern."""
    urls = []
    base = f"{CC_INDEX_HOST}/{index_id}-index"
    try:
        resp = httpx.get(base, params={"url": pattern, "output": "json",
                                       "showNumPages": "true"},
                         headers=UA, timeout=120)
        total_pages = int(resp.json().get("pages", 0))
    except Exception as exc:
        print(f"  CC page-count failed for {pattern}: {exc}")
        return urls
    if total_pages == 0:
        return urls
    # sample pages spread across the index to avoid alphabetical bias
    chosen = sorted({int(i * total_pages / max(pages, 1)) for i in range(min(pages, total_pages))})
    for page in chosen:
        for attempt in range(3):
            try:
                resp = httpx.get(base, params={"url": pattern, "output": "json",
                                               "fl": "url", "page": page},
                                 headers=UA, timeout=300)
                if resp.status_code == 200:
                    for line in resp.text.splitlines():
                        try:
                            urls.append(json.loads(line)["url"])
                        except (ValueError, KeyError):
                            continue
                    break
                time.sleep(5 * (attempt + 1))
            except Exception:
                time.sleep(5 * (attempt + 1))
    print(f"  {pattern}: {len(urls)} URLs from {len(chosen)}/{total_pages} index pages")
    return urls


def extract_tenant(ats: str, url: str):
    """URL -> tenant slug (workday: 'tenant|wdN|site' composite)."""
    try:
        if ats == "workday":
            m = re.match(r"https?://([a-z0-9-]+)\.(wd\d+)\.myworkdayjobs\.com/([^?#]*)", url)
            if not m:
                return None
            tenant, wd, path = m.group(1), m.group(2), m.group(3)
            segments = [s for s in path.split("/") if s]
            if segments and LOCALE_RE.match(segments[0]):
                segments = segments[1:]
            if not segments:
                return None
            site = segments[0]
            if site.lower() in BAD_SLUGS or site.lower() in ("job", "jobs", "wday"):
                return None
            return f"{tenant}|{wd}|{site}"
        if ats == "icims":
            m = re.match(r"https?://careers-([a-z0-9-]+)\.icims\.com", url)
            slug = m.group(1) if m else None
        elif ats in ("recruitee", "bamboohr"):
            m = re.match(r"https?://([a-z0-9-]+)\.", url)
            slug = m.group(1) if m else None
        else:
            m = re.match(r"https?://[^/]+/([A-Za-z0-9._-]+)", url)
            slug = m.group(1) if m else None
        if not slug or slug.lower() in BAD_SLUGS or len(slug) < 2 or len(slug) > 50:
            return None
        if re.fullmatch(r"\d+", slug):
            return None
        return slug
    except Exception:
        return None


CC_PATTERNS = [
    ("greenhouse", "boards.greenhouse.io/*"),
    ("greenhouse", "job-boards.greenhouse.io/*"),
    ("lever", "jobs.lever.co/*"),
    ("ashby", "jobs.ashbyhq.com/*"),
    ("smartrecruiters", "jobs.smartrecruiters.com/*"),
    ("workable", "apply.workable.com/*"),
    ("recruitee", "*.recruitee.com/*"),
    ("bamboohr", "*.bamboohr.com/careers*"),
    ("workday", "*.myworkdayjobs.com/*"),
    ("icims", "careers-*.icims.com/*"),
]


# ------------------------------------------------------- board inspection --

def inspect_board(ats: str, slug: str):
    """Fetch the board once. Returns (exists, utah_relevant, company_name)."""
    if ats == "workday":
        tenant, wd, site = slug.split("|")
        board = lr._workday_jobs(tenant, wd, site, "Utah")
        if board is None:
            return False, False, None
        return True, len(board) > 0, tenant.replace("-", " ").title()

    if ats == "greenhouse":
        data = lr._get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs")
        if not data or not isinstance(data.get("jobs"), list):
            return False, False, None
        locs = " | ".join((j.get("location") or {}).get("name", "") for j in data["jobs"])
        name = None
        if utah_text(locs):
            meta = lr._get_json(f"https://boards-api.greenhouse.io/v1/boards/{slug}")
            name = (meta or {}).get("name")
        return True, utah_text(locs), name
    if ats == "lever":
        data = lr._get_json(f"https://api.lever.co/v0/postings/{slug}?mode=json")
        if not isinstance(data, list):
            return False, False, None
        locs = " | ".join((j.get("categories") or {}).get("location") or "" for j in data)
        return True, utah_text(locs), None
    if ats == "ashby":
        data = lr._get_json(f"https://api.ashbyhq.com/posting-api/job-board/{slug}")
        if not data or not isinstance(data.get("jobs"), list):
            return False, False, None
        locs = " | ".join(f"{j.get('location') or ''} {j.get('address') or ''}" for j in data["jobs"])
        return True, utah_text(locs), None
    if ats == "smartrecruiters":
        data = lr._get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}/postings")
        if not data or not isinstance(data.get("content"), list):
            return False, False, None
        locs = " | ".join(" ".join(str(v) for v in (j.get("location") or {}).values())
                          for j in data["content"])
        name = None
        if utah_text(locs):
            meta = lr._get_json(f"https://api.smartrecruiters.com/v1/companies/{slug}")
            name = (meta or {}).get("name")
        return True, utah_text(locs), name
    if ats == "workable":
        data = lr._get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=false")
        if not data or not isinstance(data.get("jobs"), list):
            return False, False, None
        locs = " | ".join(f"{j.get('city') or ''} {j.get('state') or ''}" for j in data["jobs"])
        return True, utah_text(locs), data.get("name")
    if ats == "recruitee":
        data = lr._get_json(f"https://{slug}.recruitee.com/api/offers/")
        if not data or not isinstance(data.get("offers"), list):
            return False, False, None
        locs = " | ".join(f"{j.get('city') or ''} {j.get('state_name') or ''} {j.get('location') or ''}"
                          for j in data["offers"])
        return True, utah_text(locs), None
    if ats == "bamboohr":
        data = lr._get_json(f"https://{slug}.bamboohr.com/careers/list")
        if not data or not isinstance(data.get("result"), list):
            return False, False, None
        locs = " | ".join(" ".join(str(v) for v in (j.get("location") or {}).values())
                          for j in data["result"])
        return True, utah_text(locs), None
    if ats == "icims":
        # no JSON board — check the hosted search page text for Utah locations
        try:
            resp = httpx.get(f"https://careers-{slug}.icims.com/jobs/search?ss=1&in_iframe=1",
                             headers=UA, timeout=10, follow_redirects=True)
        except Exception:
            return False, False, None
        if resp.status_code != 200 or "icims.com" not in str(resp.url):
            return False, False, None
        return True, utah_text(resp.text), None
    return False, False, None


def process_tenant(ats: str, slug: str):
    """Inspect one tenant; cache Utah-relevant boards; record the check."""
    exists, utah, name = inspect_board(ats, slug)
    if utah:
        company = name or slug.split("|")[0].replace("-", " ").replace(".", " ").title()
        key = lr.normalize_company(company)
        if key:
            existing = db.query_one(
                "SELECT board_found FROM company_ats_cache WHERE company_key = ?", (key,))
            if not existing or not existing["board_found"]:
                db.execute(
                    "INSERT OR REPLACE INTO company_ats_cache "
                    "(company_key, ats, slug, board_found, created_at) VALUES (?, ?, ?, 1, ?)",
                    (key, ats, slug, db.now()))
    db.execute(
        "INSERT OR REPLACE INTO enumerated_tenants (ats, slug, board_exists, utah, checked_at) "
        "VALUES (?, ?, ?, ?, ?)",
        (ats, slug, 1 if exists else 0, 1 if utah else 0, db.now()))
    return exists, utah


def ensure_tables():
    db.init_db()
    db.get_conn().executescript("""
        CREATE TABLE IF NOT EXISTS enumerated_tenants (
            ats TEXT NOT NULL, slug TEXT NOT NULL, board_exists INTEGER,
            utah INTEGER, checked_at TEXT, PRIMARY KEY (ats, slug));
    """)


def scan(cc_pages: int = 4, max_per_ats: int = 4000, workers: int = 6,
         enumerate_only: bool = False, log=print) -> dict:
    """Harvest ATS tenants from the newest Common Crawl index and inspect every
    not-yet-checked board for Utah postings. Returns a stats dict. Idempotent:
    tenants already in enumerated_tenants are skipped, so re-running on the same
    index inspects nothing new."""
    ensure_tables()
    log("Phase 1: harvesting tenant slugs from Common Crawl")
    index_id = cc_newest_index()
    log(f"Using index: {index_id}")
    tenants: dict = {}
    for ats, pattern in CC_PATTERNS:
        for url in cc_query(index_id, pattern, cc_pages):
            slug = extract_tenant(ats, url)
            if slug:
                tenants.setdefault((ats, slug), True)
    log(f"Unique tenants harvested: {len(tenants)}")

    checked = {(r["ats"], r["slug"]) for r in db.query("SELECT ats, slug FROM enumerated_tenants")}
    by_ats: dict = {}
    for (ats, slug) in tenants:
        if (ats, slug) not in checked:
            by_ats.setdefault(ats, []).append(slug)
    todo = []
    for ats, slugs in by_ats.items():
        random.shuffle(slugs)
        todo += [(ats, s) for s in slugs[:max_per_ats]]
    random.shuffle(todo)
    log(f"To inspect this run: {len(todo)} (already checked previously: "
        f"{len(tenants) - sum(len(s) for s in by_ats.values())})")
    stats = {"index": index_id, "harvested": len(tenants), "inspected": len(todo),
             "found": 0, "live_boards": 0, "errors": 0}
    if enumerate_only or not todo:
        return stats

    found = exists_n = errors = 0
    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {pool.submit(process_tenant, ats, slug): (ats, slug) for ats, slug in todo}
        for i, future in enumerate(as_completed(futures), 1):
            try:
                exists, utah = future.result()
                exists_n += 1 if exists else 0
                found += 1 if utah else 0
            except Exception:
                errors += 1
            if i % 100 == 0 or i == len(futures):
                rate = i / max(time.time() - start, 1)
                eta = (len(futures) - i) / max(rate, 0.01) / 60
                log(f"  {i}/{len(futures)} — {exists_n} live boards, {found} with Utah "
                    f"postings, {errors} errors — ETA {eta:.0f} min")
    stats.update(found=found, live_boards=exists_n, errors=errors,
                 minutes=round((time.time() - start) / 60, 1))
    total = db.query_one("SELECT COUNT(*) AS n FROM company_ats_cache WHERE board_found = 1")["n"]
    stats["cache_total"] = total
    log(f"Finished in {stats['minutes']} min — {found} Utah boards added, "
        f"cache now holds {total} verified boards")
    return stats


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--cc-pages", type=int, default=3, help="CC index pages to sample per pattern")
    parser.add_argument("--max-per-ats", type=int, default=1200)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--enumerate-only", action="store_true")
    args = parser.parse_args()
    scan(cc_pages=args.cc_pages, max_per_ats=args.max_per_ats, workers=args.workers,
         enumerate_only=args.enumerate_only)
    for s in db.query("SELECT ats, COUNT(*) AS n FROM company_ats_cache WHERE board_found = 1 "
                      "GROUP BY ats ORDER BY n DESC"):
        print(f"  {s['ats']}: {s['n']}")


if __name__ == "__main__":
    main()
