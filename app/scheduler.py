"""APScheduler wiring — replaces the Abacus scheduled-task system locally.

Schedules (spec section 15, step 11):
  main automation  - daily at AUTOMATION_HOUR (each user processed per cadence)
  urgency check    - every 12 hours
  portfolio checks - weekly (Sunday 03:00)
  LinkedIn re-run  - monthly (1st, 04:00)
  token budget     - daily 07:00
"""
import logging

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from . import ats_refresh, automation, config

log = logging.getLogger("scheduler")
_scheduler = None


def start():
    global _scheduler
    if not config.SCHEDULER_ENABLED or _scheduler is not None:
        return
    _scheduler = BackgroundScheduler(daemon=True)
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
    log.info("Scheduler started (main cycle daily at %02d:00)", config.AUTOMATION_HOUR)


def shutdown():
    global _scheduler
    if _scheduler is not None:
        _scheduler.shutdown(wait=False)
        _scheduler = None


def job_status() -> list:
    if _scheduler is None:
        return []
    return [{"id": j.id, "next_run": str(j.next_run_time)} for j in _scheduler.get_jobs()]
