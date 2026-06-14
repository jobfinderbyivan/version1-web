"""Harvest more Utah employer names from public directory pages and deeper
Claude sweeps (by city and company tier), then probe each for an ATS board.

Two name sources:
  1. Directory pages — fetch each URL, strip the HTML, and have Claude extract
     real employer names from the page text (robust to any page layout).
  2. Claude city/tier sweeps — mid-size and second-tier employers by Utah
     city, which the first statewide sweep under-covered.

All names are then probed with the full adapter set (now including BambooHR
and iCIMS) via link_resolver.discover_company.

Usage: python tools/scrape_utah_directories.py [--workers 5] [--skip-probe]
"""
import argparse
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx

from app import db, link_resolver as lr, llm  # noqa: E402

UA = {"User-Agent": "UtahEmployerResearch/1.0 (personal job-search tool; "
                    "contact: ivandoublejr@gmail.com)"}

DIRECTORY_URLS = [
    "https://jobs.utah.gov/wi/data/library/firm/majoremployers.html",
    "https://en.wikipedia.org/wiki/List_of_Utah_companies",
    "https://en.wikipedia.org/wiki/Economy_of_Utah",
    "https://www.utah.gov/business/",
    "https://edcutah.org/why-utah",
]

CITY_SWEEPS = [
    "Provo and Orem, Utah",
    "Ogden, Layton and Clearfield, Utah (including Hill Air Force Base contractors)",
    "Lehi, American Fork and Pleasant Grove, Utah (Silicon Slopes)",
    "St. George and Cedar City, Utah",
    "Logan and Cache Valley, Utah",
    "Park City and Summit County, Utah",
    "West Valley City, Taylorsville and Kearns, Utah",
    "Sandy, Draper and South Jordan, Utah",
]
TIER_SWEEPS = [
    "mid-size Utah companies with roughly 100-1000 employees that people outside Utah "
    "may not know",
    "fast-growing Utah startups founded or funded between 2020 and 2026",
    "Utah staffing firms, call centers, BPOs and large franchise operators",
    "Utah-based credit unions, regional banks, insurance brokerages and title companies",
]

COMPANY_SCHEMA = llm.obj_schema({
    "companies": {"type": "array", "items": llm.obj_schema({
        "name": llm.STR, "domain": llm.STR})},
})


def names_from_page(url: str) -> dict:
    out = {}
    try:
        resp = httpx.get(url, headers=UA, timeout=25, follow_redirects=True)
        if resp.status_code != 200:
            print(f"  {url} -> HTTP {resp.status_code}, skipped")
            return out
        text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", resp.text, flags=re.S)
        text = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", text))
        if len(text) < 1500:
            print(f"  {url} -> too little text, skipped")
            return out
    except Exception as exc:
        print(f"  {url} -> fetch failed: {exc}")
        return out
    result = llm.complete_json(
        "The following is text extracted from a public web page about Utah's economy or "
        "employers. Extract every COMPANY or EMPLOYER name mentioned (ignore people, "
        "places, government program names and navigation junk). Give the website domain "
        "when it appears in the text or you are confident of it, else an empty string.\n\n"
        + text[:14000],
        process_type="company_research", user_id=None,
        schema=COMPANY_SCHEMA, max_tokens=4000)
    for c in (result or {}).get("companies", []):
        name = (c.get("name") or "").strip()
        if 2 <= len(name) <= 60:
            key = lr.normalize_company(name)
            if key:
                out[key] = (name, (c.get("domain") or "").strip() or None)
    print(f"  {url} -> {len(out)} employer names")
    return out


def names_from_sweep(topic: str) -> dict:
    out = {}
    result = llm.complete_json(
        f"List up to 50 real companies and organizations that employ people in/around: "
        f"{topic}. Focus on actual employers with offices or facilities there. For each "
        f"give the name and its website domain when confident, else empty string. Only "
        f"include organizations you are confident exist.",
        process_type="company_research", user_id=None,
        schema=COMPANY_SCHEMA, max_tokens=3500)
    for c in (result or {}).get("companies", []):
        name = (c.get("name") or "").strip()
        if 2 <= len(name) <= 60:
            key = lr.normalize_company(name)
            if key:
                out[key] = (name, (c.get("domain") or "").strip() or None)
    print(f"  sweep [{topic[:55]}...] -> {len(out)}")
    return out


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--workers", type=int, default=5)
    parser.add_argument("--skip-probe", action="store_true")
    args = parser.parse_args()
    db.init_db()

    print("=== Gathering names ===")
    merged: dict = {}
    print("Directory pages:")
    for url in DIRECTORY_URLS:
        for key, val in names_from_page(url).items():
            if key not in merged or (val[1] and not merged[key][1]):
                merged[key] = val
    if llm.available():
        print("Claude city/tier sweeps:")
        for topic in CITY_SWEEPS + TIER_SWEEPS:
            for key, val in names_from_sweep(topic).items():
                if key not in merged or (val[1] and not merged[key][1]):
                    merged[key] = val
    print(f"\nTOTAL unique names: {len(merged)}")

    cached = {r["company_key"] for r in db.query(
        "SELECT company_key FROM company_ats_cache "
        "WHERE created_at > datetime('now', '-30 days')")}
    todo = [(name, domain) for key, (name, domain) in sorted(merged.items())
            if key not in cached]
    print(f"Already cached: {len(merged) - len(todo)} | to probe: {len(todo)}")
    if args.skip_probe or not todo:
        return

    print(f"\n=== Probing {len(todo)} companies ({args.workers} workers) ===")
    found = 0
    start = time.time()
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = [pool.submit(lr.discover_company, name, domain) for name, domain in todo]
        for i, future in enumerate(as_completed(futures), 1):
            try:
                if future.result():
                    found += 1
            except Exception:
                pass
            if i % 50 == 0 or i == len(futures):
                rate = i / max(time.time() - start, 1)
                eta = (len(futures) - i) / max(rate, 0.01) / 60
                print(f"  {i}/{len(futures)} — {found} boards found — ETA {eta:.0f} min",
                      flush=True)

    total = db.query_one("SELECT COUNT(*) AS n FROM company_ats_cache WHERE board_found = 1")["n"]
    print(f"\nDone in {(time.time() - start) / 60:.1f} min — boards found this run: {found}")
    print(f"company_ats_cache total verified boards: {total}")


if __name__ == "__main__":
    main()
