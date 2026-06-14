"""Bulk-populate company_ats_cache with Utah employers.

Gathers company names from every available source, then probes each company's
likely ATS job boards (Greenhouse/Lever/Ashby/SmartRecruiters/Workable/
Recruitee) and caches discoveries AND misses for the link resolver.

Sources:
  1. Wikipedia  - category crawl of "Companies based in Utah" (+ subcategories)
  2. Claude     - sector-by-sector lists of significant Utah employers
  3. Adzuna     - company names harvested from live Utah job postings

Usage:  python tools/fill_utah_ats_cache.py [--limit N] [--workers 6] [--dry-run]
"""
import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app import config, db, link_resolver, llm  # noqa: E402

WIKI_API = "https://en.wikipedia.org/w/api.php"
HEADERS = {"User-Agent": "UtahEmployerResearch/1.0 (https://localhost personal project; "
                         "contact: ivandoublejr@gmail.com) httpx"}

SECTORS = [
    "technology and software (Silicon Slopes startups and established firms)",
    "healthcare systems, hospitals, clinics and medical device makers",
    "finance, banking, credit unions, fintech and insurance",
    "retail, grocery, restaurant and consumer brands",
    "manufacturing, aerospace, defense and industrial",
    "construction, engineering and real estate",
    "education, universities and large school districts",
    "outdoor recreation, ski resorts, tourism and hospitality",
    "logistics, trucking, distribution and supply chain",
    "energy, mining, agriculture and utilities",
    "professional services, consulting, marketing and call centers",
    "nonprofits, religious organizations and large government contractors",
]

COMPANY_SCHEMA = llm.obj_schema({
    "companies": {"type": "array", "items": llm.obj_schema({
        "name": llm.STR, "domain": llm.STR})},
})

JUNK_PATTERN = re.compile(
    r"^(list of|category:|template:|.*\(disambiguation\))|^(confidential|unknown|n/?a)$", re.I)


def clean_name(raw: str) -> str:
    name = re.sub(r"\s*\((?:company|corporation|retailer|brand|airline|bank|restaurant"
                  r"|software|business|supermarket|utah)?\)\s*$", "", (raw or "").strip())
    return name.strip()


def wikipedia_companies() -> dict:
    """Crawl Category:Companies_based_in_Utah and subcategories (depth 2)."""
    names = {}
    seen_cats = set()

    def members(cat: str, depth: int):
        if depth > 2 or cat in seen_cats:
            return
        seen_cats.add(cat)
        cont = None
        while True:
            params = {"action": "query", "list": "categorymembers", "cmtitle": cat,
                      "cmlimit": 500, "format": "json", "cmtype": "page|subcat"}
            if cont:
                params["cmcontinue"] = cont
            try:
                resp = httpx.get(WIKI_API, params=params, headers=HEADERS, timeout=20)
                data = resp.json()
            except Exception as exc:
                print(f"  wikipedia error for {cat}: {exc}")
                return
            for m in (data.get("query") or {}).get("categorymembers", []):
                title = m.get("title", "")
                if title.startswith("Category:"):
                    members(title, depth + 1)
                elif not JUNK_PATTERN.search(title):
                    name = clean_name(title)
                    if 2 <= len(name) <= 60:
                        names.setdefault(link_resolver.normalize_company(name), (name, None))
            cont = (data.get("continue") or {}).get("cmcontinue")
            if not cont:
                break
            time.sleep(0.2)
        time.sleep(0.2)

    members("Category:Companies_based_in_Utah", 0)
    members("Category:Companies_based_in_Salt_Lake_City", 0)
    print(f"Wikipedia: {len(names)} companies")
    return names


def llm_companies() -> dict:
    names = {}
    if not llm.available():
        print("Claude: skipped (no API key)")
        return names
    for sector in SECTORS:
        result = llm.complete_json(
            f"List up to 60 companies and organizations with significant operations and "
            f"employees in Utah in this sector: {sector}. Include both headquarters and "
            f"major-presence employers. For each give the company name and its website "
            f"domain (e.g. 'qualtrics.com') when you are confident of it; use an empty "
            f"string for the domain when unsure. Only include real organizations you are "
            f"confident exist.",
            process_type="company_research", user_id=None,
            schema=COMPANY_SCHEMA, max_tokens=4000)
        got = 0
        for c in (result or {}).get("companies", []):
            name = clean_name(c.get("name", ""))
            if 2 <= len(name) <= 60 and not JUNK_PATTERN.search(name):
                key = link_resolver.normalize_company(name)
                if key:
                    domain = (c.get("domain") or "").strip() or None
                    existing = names.get(key)
                    if not existing or (domain and not existing[1]):
                        names[key] = (name, domain)
                    got += 1
        print(f"Claude [{sector[:40]}...]: {got} companies")
    print(f"Claude total: {len(names)} unique")
    return names


def adzuna_companies() -> dict:
    names = {}
    if not (config.ADZUNA_APP_ID and config.ADZUNA_APP_KEY):
        print("Adzuna: skipped (no keys)")
        return names
    queries = ["", "engineer", "nurse", "manager", "technician", "sales",
               "driver", "analyst", "developer", "accountant"]
    for q in queries:
        for page in (1, 2):
            try:
                resp = httpx.get(
                    f"https://api.adzuna.com/v1/api/jobs/us/search/{page}",
                    params={"app_id": config.ADZUNA_APP_ID, "app_key": config.ADZUNA_APP_KEY,
                            "what": q, "where": "Utah", "distance": 400,
                            "results_per_page": 50, "max_days_old": 60,
                            "content-type": "application/json"},
                    timeout=30)
                for item in resp.json().get("results", []):
                    name = clean_name((item.get("company") or {}).get("display_name", ""))
                    if 2 <= len(name) <= 60 and not JUNK_PATTERN.search(name):
                        key = link_resolver.normalize_company(name)
                        if key:
                            names.setdefault(key, (name, None))
            except Exception as exc:
                print(f"  adzuna error ({q!r} p{page}): {exc}")
            time.sleep(1.2)
    print(f"Adzuna: {len(names)} companies from live Utah postings")
    return names


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--limit", type=int, default=0, help="probe at most N companies")
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--dry-run", action="store_true", help="gather names only, no probing")
    args = parser.parse_args()

    db.init_db()
    print("=== Gathering Utah employer names ===")
    merged: dict = {}
    for source in (wikipedia_companies, llm_companies, adzuna_companies):
        for key, (name, domain) in source().items():
            existing = merged.get(key)
            if not existing or (domain and not existing[1]):
                merged[key] = (name, domain)
    print(f"\nTOTAL unique companies: {len(merged)}")

    fresh = {r["company_key"] for r in db.query(
        "SELECT company_key FROM company_ats_cache WHERE created_at > datetime('now', '-30 days')")}
    todo = [(name, domain) for key, (name, domain) in sorted(merged.items())
            if key not in fresh]
    print(f"Already cached (fresh): {len(merged) - len(todo)} | to probe: {len(todo)}")
    if args.limit:
        todo = todo[:args.limit]
        print(f"Limited to {len(todo)}")
    if args.dry_run:
        for name, domain in todo[:40]:
            print("  ", name, f"({domain})" if domain else "")
        return

    print(f"\n=== Probing {len(todo)} companies across {len(link_resolver.ATS_NAMES)} ATS platforms "
          f"({args.workers} workers) ===")
    found = misses = errors = 0
    start = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(link_resolver.discover_company, name, domain): name
                   for name, domain in todo}
        for i, future in enumerate(as_completed(futures), 1):
            try:
                result = future.result()
                if result:
                    found += 1
                else:
                    misses += 1
            except Exception:
                errors += 1
            if i % 25 == 0 or i == len(futures):
                rate = i / max(time.time() - start, 1)
                eta = (len(futures) - i) / max(rate, 0.01) / 60
                print(f"  {i}/{len(futures)} done — {found} boards found, {misses} misses, "
                      f"{errors} errors — ETA {eta:.0f} min", flush=True)

    print(f"\n=== Finished in {(time.time() - start) / 60:.1f} min ===")
    stats = db.query(
        "SELECT ats, COUNT(*) AS n FROM company_ats_cache WHERE board_found = 1 "
        "GROUP BY ats ORDER BY n DESC")
    total = db.query_one("SELECT COUNT(*) AS n FROM company_ats_cache")["n"]
    found_total = db.query_one("SELECT COUNT(*) AS n FROM company_ats_cache WHERE board_found = 1")["n"]
    print(f"Cache now holds {total} companies, {found_total} with discovered boards:")
    for s in stats:
        print(f"  {s['ats']}: {s['n']}")


if __name__ == "__main__":
    main()
