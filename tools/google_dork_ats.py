"""Resolve company ATS boards via search.

For every Utah employer currently cached as a MISS (we know they're a real
employer but couldn't guess their ATS slug), this searches  "<company> careers"
and harvests the employer's ATS board URL from the results — covering Workday,
Taleo, iCIMS, Jobvite, SuccessFactors, UKG, Paradox AND the slug-probeable
platforms whose slug simply didn't match our guesses.

Note: the Serper free tier rejects the `site:` operator, so this uses plain
company-name queries instead — which is more surgical for slug discovery.

Backends (first configured key wins):
  SERPER_API_KEY            - https://serper.dev (free 2,500 queries, no card)
  GOOGLE_CSE_KEY/_ID        - legacy; Google closed CSE to new customers in 2026

Usage: python tools/google_dork_ats.py [--max-queries 1000] [--names-file path]
"""
import argparse
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app import config, db, link_resolver as lr  # noqa: E402
from tools.enumerate_ats_tenants import inspect_board  # noqa: E402

# Hosts that mean "we found the employer's own ATS board" when seen in results.
ATS_HOST_HINTS = ("greenhouse.io", "lever.co", "ashbyhq.com", "smartrecruiters.com",
                  "workable.com", "recruitee.com", "bamboohr.com", "myworkdayjobs.com",
                  "icims.com", "taleo.net", "jobvite.com", "paradox.ai",
                  "successfactors.com", "ultipro.com")


def serper_search(query: str, page: int = 1) -> list:
    """Serper.dev: Google results. num=100 costs 10 credits of the 2,500 free."""
    try:
        resp = httpx.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": config.SERPER_API_KEY,
                     "Content-Type": "application/json"},
            json={"q": query, "num": 100, "page": page},
            timeout=40)
        if resp.status_code != 200:
            print(f"  Serper error {resp.status_code}: {resp.text[:140]}")
            return []
        return [item.get("link", "") for item in resp.json().get("organic", [])]
    except Exception as exc:
        print(f"  Serper request failed: {exc}")
        return []


def cse_search(query: str, start: int = 1) -> list:
    """Legacy Google CSE (closed to new customers since 2026)."""
    try:
        resp = httpx.get(
            "https://www.googleapis.com/customsearch/v1",
            params={"key": config.GOOGLE_CSE_KEY, "cx": config.GOOGLE_CSE_ID,
                    "q": query, "num": 10, "start": start},
            timeout=30)
        if resp.status_code != 200:
            print(f"  CSE error {resp.status_code}: {resp.text[:140]}")
            return []
        return [item.get("link", "") for item in resp.json().get("items", [])]
    except Exception as exc:
        print(f"  CSE request failed: {exc}")
        return []


def run_search(query: str, page: int = 1) -> list:
    if config.SERPER_API_KEY:
        return serper_search(query, page)
    return cse_search(query, start=1 + (page - 1) * 10)


def resolve_company(name: str) -> str | None:
    """Search '<company> careers' and resolve the employer's ATS board.
    Strategy: (1) any direct ATS URL in results -> learn it; (2) otherwise
    fetch the company's own careers page from the results and detect the ATS
    embedded behind a vanity careers domain. Returns the ats name or None."""
    key = lr.normalize_company(name)
    results = run_search(f"{name} careers jobs")

    # (1) a recognizable ATS URL directly in the results
    for url in results:
        if not any(h in url for h in ATS_HOST_HINTS):
            continue
        if not lr.learn_from_url(name, url):
            continue
        row = db.query_one("SELECT ats, slug FROM company_ats_cache WHERE company_key = ?", (key,))
        if not row:
            continue
        if row["ats"] in lr.ATS_NAMES or row["ats"] == "workday":
            if not lr.fetch_board(row["ats"], row["slug"]):  # dead/empty board — drop
                db.execute("UPDATE company_ats_cache SET board_found = 0, ats = NULL, slug = NULL "
                           "WHERE company_key = ?", (key,))
                continue
        return row["ats"]

    # (2) vanity careers domain: fetch the company's own page and detect the ATS
    for url in results:
        host = lr.host_of(url)
        if not host or not lr.is_direct_link(url):
            continue
        if any(h in host for h in ATS_HOST_HINTS):
            continue  # already handled in (1)
        if "career" in url.lower() or "/jobs" in url.lower() or host.startswith("careers."):
            ats = lr.resolve_from_website(name, host)
            if ats:
                db.execute("UPDATE company_ats_cache SET domain = ? WHERE company_key = ?",
                           (host, key))
                return ats
            break  # only probe the top company-site result
    return None


PRIORITY_NAMES = [
    "Intermountain Health", "University of Utah", "University of Utah Health", "Zions Bancorporation",
    "Brigham Young University", "Utah State University", "Utah Valley University", "Weber State University",
    "Salt Lake City Corporation", "Salt Lake County", "State of Utah", "Granite School District",
    "Jordan School District", "Davis School District", "Alpine School District", "Canyons School District",
    "Mountain America Credit Union", "America First Credit Union", "Goldman Sachs Salt Lake City",
    "Larry H. Miller Company", "Smith's Food and Drug", "Maverik", "Deseret News",
    "Huntsman Corporation", "Merit Medical Systems", "Nu Skin Enterprises", "Ancestry",
    "Health Catalyst", "Pluralsight", "Domo", "Overstock", "Bd Becton Dickinson",
    "L3Harris", "Northrop Grumman Utah", "Boeing Salt Lake", "Autoliv", "Cytiva",
    "Intermountain Healthcare", "MountainStar Healthcare", "University of Utah Hospitals",
    "Workers Compensation Fund", "Zions Bank", "Discover Financial Salt Lake",
]


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--max-queries", type=int, default=1000,
                        help="search budget (Serper free tier = 2,500 queries)")
    parser.add_argument("--names-file", help="optional newline-delimited extra company names")
    parser.add_argument("--order", choices=["asc", "desc"], default="desc",
                        help="miss-processing order by id. Use 'asc' to RESUME a run "
                             "that was stopped: the prior (desc) run did the high ids first, "
                             "so 'asc' processes the not-yet-searched low ids and avoids "
                             "re-spending credits on already-searched companies.")
    args = parser.parse_args()

    if not (config.SERPER_API_KEY or (config.GOOGLE_CSE_KEY and config.GOOGLE_CSE_ID)):
        print("No search backend configured. Sign up free at https://serper.dev, copy the "
              "API key, and put it in .env as SERPER_API_KEY=... Then re-run this tool.")
        return
    print(f"Search backend: {'serper.dev' if config.SERPER_API_KEY else 'google cse (legacy)'}")
    db.init_db()
    db.get_conn().execute(
        "CREATE TABLE IF NOT EXISTS serper_searched "
        "(company_key TEXT PRIMARY KEY, checked_at TEXT)")

    # Build the work list: priority flagships first, then every cached miss, then
    # any extra names supplied — skipping companies already resolved OR already
    # searched (so stopped runs resume without re-spending credits).
    resolved = {r["company_key"] for r in db.query(
        "SELECT company_key FROM company_ats_cache WHERE board_found = 1")}
    searched = {r["company_key"] for r in db.query("SELECT company_key FROM serper_searched")}
    skip = resolved | searched
    names, seen = [], set()

    def add(name):
        key = lr.normalize_company(name)
        if key and key not in seen and key not in skip:
            seen.add(key)
            names.append(name)

    for n in PRIORITY_NAMES:
        add(n)
    if args.names_file and Path(args.names_file).exists():
        for line in Path(args.names_file).read_text(encoding="utf-8").splitlines():
            if line.strip():
                add(line.strip())
    # cached misses, ordered by id (desc = fresh first; asc = resume-safe)
    for r in db.query(f"SELECT company_key FROM company_ats_cache WHERE board_found = 0 "
                      f"ORDER BY id {'ASC' if args.order == 'asc' else 'DESC'}"):
        add(r["company_key"])

    todo = names[:args.max_queries]
    print(f"Companies to resolve via search: {len(todo)} (budget {args.max_queries})")
    print("=== Resolving company ATS boards ===")
    found = 0
    start = time.time()
    # serialized: stays well under Serper's rate limit and is plenty fast
    for i, name in enumerate(todo, 1):
        try:
            ats = resolve_company(name)
            if ats:
                found += 1
                print(f"  [hit] {name[:42]:44} -> {ats}", flush=True)
            # record the search so a stopped/resumed run never re-spends on it
            db.execute("INSERT OR REPLACE INTO serper_searched (company_key, checked_at) "
                       "VALUES (?, ?)", (lr.normalize_company(name), db.now()))
        except Exception as exc:
            print(f"  [err] {name[:42]}: {exc}")
        if i % 50 == 0:
            rate = i / max(time.time() - start, 1)
            eta = (len(todo) - i) / max(rate, 0.01) / 60
            print(f"  ... {i}/{len(todo)} — {found} boards found — ETA {eta:.0f} min", flush=True)
        time.sleep(0.25)

    total = db.query_one("SELECT COUNT(*) AS n FROM company_ats_cache WHERE board_found = 1")["n"]
    print(f"\nDone in {(time.time() - start) / 60:.1f} min — boards resolved this run: {found}")
    print(f"company_ats_cache total verified boards: {total}")
    for s in db.query("SELECT ats, COUNT(*) AS n FROM company_ats_cache WHERE board_found = 1 "
                      "GROUP BY ats ORDER BY n DESC"):
        print(f"  {s['ats']}: {s['n']}")


if __name__ == "__main__":
    main()
