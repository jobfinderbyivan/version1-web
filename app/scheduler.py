"""APScheduler wiring — replaces the Abacus scheduled-task system locally.

All wall-clock times are interpreted in config.SCHEDULER_TIMEZONE (default
America/Denver) — the scheduler is pinned to it explicitly so behaviour does not
depend on the container's clock (Railway runs in UTC, which would otherwise
shift every "hour=N" trigger).

Schedules (spec section 15, step 11):
  board crawl      - daily at BOARD_CRAWL_HOUR (rebuilds the local job cache)
  main automation  - daily at AUTOMATION_HOUR (each user processed per cadence)
  urgency check    - every 12 hours
  portfolio checks - weekly (Sunday 03:00)
  LinkedIn re-run  - monthly (1st, 04:00)
  token budget     - daily 07:00
  ATS refresh      - weekly probe; full scan only on a new Common Crawl index
"""
import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import ats_refresh, automation, config, db, job_cache

log = logging.getLogger("scheduler")
_scheduler = None


def _resolve_tz():
    try:
        from zoneinfo import ZoneInfo
        return ZoneInfo(config.SCHEDULER_TIMEZONE)
    except Exception:
        log.warning("Timezone %s unavailable; using system local time", config.SCHEDULER_TIMEZONE)
        return None


def scheduled_board_crawl():
    """Daily job-cache rebuild, with progress mirrored to the automation log."""
    def _log(msg):
        log.info("%s", msg)
        db.log("board_crawl", None, str(msg)[:300])
    return job_cache.crawl_all_boards(log_fn=_log)


def _maybe_initial_crawl():
    """On first boot with an empty cache (fresh deploy), populate it in the
    background instead of making users wait for the next daily crawl."""
    try:
        empty = (db.query_one("SELECT COUNT(*) AS n FROM cached_jobs") or {"n": 0})["n"] == 0
    except Exception:
        return
    if empty:
        log.info("Job cache is empty — running an initial board crawl in the background")
        threading.Thread(target=scheduled_board_crawl, name="initial-crawl", daemon=True).start()


def start():
    global _scheduler
    if not config.SCHEDULER_ENABLED or _scheduler is not None:
        return
    tz = _resolve_tz()
    _scheduler = BackgroundScheduler(daemon=True, timezone=tz) if tz else BackgroundScheduler(daemon=True)
    if config.BOARD_CRAWL_ENABLED:
        _scheduler.add_job(scheduled_board_crawl, CronTrigger(hour=config.BOARD_CRAWL_HOUR, minute=0),
                           id="board_crawl", max_instances=1, coalesce=True)
    _scheduler.add_job(automation.run_main_cycle, CronTrigger(hour=config.AUTOMATION_HOUR, minute=0),
                       id="main_cycle", max_instances=1, coalesce=True)
    _scheduler.add_job(automation.run_urgency_check, CronTrigger(hour="*/12", minute=30),
                       id="urgency_check", max_instances=1, coalesce=True)
    _scheduler.add_job(automation.run_portfolio_checks, CronTrigger(day_of_week="sun", hour=3),
                       id="portfolio_checks", max_instances=1, coalesce=True)
    _scheduler.add_job(automation.run_linkedin_monthly, CronTrigger(day=1, hour=4),
                       id="linkedin_monthly", max_instances=1, coalesce=True)
    _scheduler.add_job(automation.run_token_budget_check, CronTrigger(hour=7, minute=0),
                       id="token_budget", max_instances=1, coalesce=True)
    # Weekly check; runs the full ATS-board scan only when Common Crawl ships a
    # new monthly index (so effectively once a month, shortly after release).
    _scheduler.add_job(ats_refresh.run_if_new_index, CronTrigger(day_of_week="wed", hour=2, minute=0),
                       id="ats_refresh", max_instances=1, coalesce=True)
    _scheduler.start()
    log.info("Scheduler started (%s): board crawl %02d:00, main cycle %02d:00",
             config.SCHEDULER_TIMEZONE, config.BOARD_CRAWL_HOUR, config.AUTOMATION_HOUR)
    if config.BOARD_CRAWL_ENABLED:
        _maybe_initial_crawl()


def shutdown():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def job_status() -> list:
    if _scheduler is None:
        return []
    return [{"id": j.id, "next_run": str(j.next_run_time)} for j in _scheduler.get_jobs()]
