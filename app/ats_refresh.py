"""Monthly ATS-board cache refresh.

Common Crawl publishes a new web index roughly once a month. This module is
called weekly by the scheduler; it runs the full enumeration scan only when a
new index has appeared (tracked via the `last_cc_index` setting), so the
expensive scan happens exactly once per Common Crawl release.
"""
import logging

from . import db

log = logging.getLogger("ats_refresh")


def newest_index() -> str:
    from tools.enumerate_ats_tenants import cc_newest_index
    return cc_newest_index()


def run_if_new_index(force: bool = False, cc_pages: int = 6, max_per_ats: int = 10000) -> dict:
    """Run the Common Crawl ATS scan when a new index is available.
    Returns a stats/skip dict; safe to call repeatedly."""
    try:
        from tools import enumerate_ats_tenants as enum
        enum.ensure_tables()
        newest = enum.cc_newest_index()
    except Exception as exc:
        log.warning("Could not reach Common Crawl: %s", exc)
        db.log("ats_refresh", None, f"Skipped — Common Crawl unreachable: {exc}")
        return {"skipped": True, "reason": str(exc)}

    last = db.get_setting("last_cc_index", "")
    if newest == last and not force:
        db.log("ats_refresh", None, f"No new Common Crawl index (still {newest})")
        return {"skipped": True, "index": newest, "reason": "no new index"}

    db.log("ats_refresh", None, f"New Common Crawl index {newest} — starting ATS scan")
    log.info("Running ATS enumeration for index %s (previous: %s)", newest, last or "none")

    def _log(msg):
        log.info(msg)
        db.log("ats_refresh", None, str(msg)[:300])

    try:
        stats = enum.scan(cc_pages=cc_pages, max_per_ats=max_per_ats, workers=6, log=_log)
    except Exception as exc:
        log.exception("ATS scan failed")
        db.log("ats_refresh", None, f"ERROR during scan: {exc}")
        return {"error": str(exc)}

    # also harvest employers actively hiring in Utah (free — uses job-API keys)
    # and run a careers-page pass over cached misses that expose a domain.
    try:
        from tools import harvest_hiring_companies as harv
        hstats = harv.harvest(cities=4, workers=6, log=_log)
        mstats = harv.resolve_misses_with_domains(workers=6, log=_log)
        stats["hiring_harvest"] = hstats
        stats["careers_page_pass"] = mstats
    except Exception as exc:
        log.warning("Hiring harvest step failed: %s", exc)
        db.log("ats_refresh", None, f"Hiring harvest step failed: {exc}")

    db.set_setting("last_cc_index", newest)
    db.log("ats_refresh", None,
           f"Done: index {newest}, {stats.get('found', 0)} Utah boards added, "
           f"cache total {stats.get('cache_total', '?')}")
    return stats
