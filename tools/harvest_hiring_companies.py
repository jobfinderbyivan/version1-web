"""Harvest employers that are *actively hiring* in Utah from the job APIs we
already pay for, then resolve each to its ATS board.

This is the "who's hiring right now" source: instead of waiting for a company
to appear in one user's matches, run broad Utah queries across every
configured provider (Adzuna, JSearch, TheirStack, USAJOBS), collect every
employer name + domain returned, and resolve them — first by slug-probing the
ATS platforms, then (for misses that expose a domain) by fetching the
company's own careers page and detecting the embedded ATS.

Exposes harvest() so the monthly scheduler can call it too.

Usage: python tools/harvest_hiring_companies.py [--workers 6] [--cities N]
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import config, db, job_search, link_resolver as lr  # noqa: E402

BROAD_TERMS = [
    "", "manager", "engineer", "nurse", "technician", "sales", "driver",
    "analyst", "developer", "accountant", "administrator", "specialist",
    "customer service", "warehouse", "marketing", "operations", "director",
    "assistant", "coordinator", "supervisor", "designer",
]
UTAH_CITIES = [
    ("Salt Lake City", "UT"), ("Provo", "UT"), ("Lehi", "UT"), ("Ogden", "UT"),
    ("St. George", "UT"), ("Logan", "UT"), ("West Valley City", "UT"), ("Sandy", "UT"),
]


def gather_company_names(cities: int = 4, log=print) -> dict:
    """Run broad queries against the FREE/generous providers only (Adzuna,
    USAJOBS) — JSearch (RapidAPI quota) and TheirStack (per-job credits) are too
    costly for bulk name-gathering. Returns {company_key: (name, domain)}."""
    names: dict = {}
    use_adzuna = bool(config.ADZUNA_APP_ID and config.ADZUNA_APP_KEY)
    use_usajobs = bool(config.USAJOBS_API_KEY)
    if not (use_adzuna or use_usajobs):
        log("  no free bulk providers configured (need Adzuna or USAJOBS)")
        return names

    def absorb(jobs):
        for j in jobs:
            name = (j.get("company") or "").strip()
            key = lr.normalize_company(name)
            if not key or len(name) > 60:
                continue
            domain = j.get("company_domain") or None
            if key not in names or (domain and not names[key][1]):
                names[key] = (name, domain)

    for city, state in UTAH_CITIES[:cities]:
        for term in BROAD_TERMS:
            if use_adzuna:
                try:
                    absorb(job_search._adzuna(term or "jobs", city, state, False, 50, 30))
                except Exception as exc:
                    log(f"  adzuna {term!r}@{city} failed: {exc}")
                time.sleep(1.2)
            if use_usajobs and not term:  # USAJOBS is federal-only — one broad pass per city
                try:
                    absorb(job_search._usajobs("", city, state, False, 50, 30))
                except Exception as exc:
                    log(f"  usajobs @{city} failed: {exc}")
                time.sleep(0.5)
        log(f"  {city}: running total {len(names)} unique employers")
    return names


def _resolve_one(name: str, domain: str):
    """Slug-probe, then fall back to careers-page detection if a domain exists."""
    found = lr.discover_company(name, domain)
    if found:
        return True
    if domain and lr.resolve_from_website(name, domain):
        return True
    return False


def harvest(cities: int = 4, workers: int = 6, log=print) -> dict:
    db.init_db()
    log("Phase 1: gathering actively-hiring Utah employers from free job APIs")
    free = [p for p in ("adzuna", "usajobs") if p in config.search_provider_names()]
    log(f"  bulk providers: {free or '(none — need Adzuna or USAJOBS)'}")
    names = gather_company_names(cities, log)
    fresh = {r["company_key"] for r in db.query(
        "SELECT company_key FROM company_ats_cache "
        "WHERE board_found = 1 OR created_at > datetime('now', '-30 days')")}
    todo = [(n, d) for k, (n, d) in names.items() if k not in fresh]
    log(f"Unique employers: {len(names)} | new to resolve: {len(todo)}")

    found = 0
    start = time.time()
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = {pool.submit(_resolve_one, n, d): n for n, d in todo}
        for i, fut in enumerate(as_completed(futs), 1):
            try:
                if fut.result():
                    found += 1
            except Exception:
                pass
            if i % 50 == 0 or i == len(futs):
                log(f"  resolved {i}/{len(futs)} — {found} boards found")
    total = db.query_one("SELECT COUNT(*) AS n FROM company_ats_cache WHERE board_found = 1")["n"]
    stats = {"employers_seen": len(names), "resolved_attempted": len(todo),
             "boards_found": found, "cache_total": total,
             "minutes": round((time.time() - start) / 60, 1)}
    log(f"Done: {found} new boards, cache total {total}")
    return stats


def resolve_misses_with_domains(limit: int = 0, workers: int = 6, log=print) -> dict:
    """Careers-page pass over cached MISSES that have a stored domain — cracks
    vanity careers domains and ATSes we couldn't slug-probe."""
    db.init_db()
    rows = db.query("SELECT company_key, domain FROM company_ats_cache "
                    "WHERE board_found = 0 AND domain IS NOT NULL AND domain != ''")
    if limit:
        rows = rows[:limit]
    log(f"Careers-page pass over {len(rows)} cached misses with domains")
    found = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futs = [pool.submit(lr.resolve_from_website, r["company_key"], r["domain"]) for r in rows]
        for fut in as_completed(futs):
            try:
                if fut.result():
                    found += 1
            except Exception:
                pass
    log(f"Careers-page pass found {found} boards")
    return {"checked": len(rows), "found": found}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--cities", type=int, default=4)
    args = parser.parse_args()
    stats = harvest(cities=args.cities, workers=args.workers)
    print(stats)
    for s in db.query("SELECT ats, COUNT(*) AS n FROM company_ats_cache WHERE board_found = 1 "
                      "GROUP BY ats ORDER BY n DESC"):
        print(f"  {s['ats']}: {s['n']}")


if __name__ == "__main__":
    main()
