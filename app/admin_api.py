"""Admin-only API (spec 5.2): resume uploads, user management, analytics,
deliverability, token budget, story moderation, manual automation triggers."""
import logging
import threading
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, File, Request, UploadFile
from fastapi.exceptions import HTTPException

from . import auth, automation, config, db, resume_parser, scheduler

log = logging.getLogger("admin")
router = APIRouter(prefix="/api/admin")


def _process_upload(content: bytes, filename: str, manual_email: str = None) -> dict:
    path = resume_parser.save_resume_file("admin", filename, content)
    try:
        raw_text = resume_parser.extract_text(path)
    except Exception as exc:
        return {"filename": filename, "ok": False, "error": f"Could not read file: {exc}"}
    if not raw_text.strip():
        return {"filename": filename, "ok": False, "error": "No text could be extracted."}
    parsed = resume_parser.parse_resume_text(raw_text)
    result = resume_parser.create_or_update_user_from_resume(
        path, raw_text, parsed, manual_email=manual_email)
    if result["needs_email"]:
        # stash for manual email completion
        pending_id = db.execute(
            "INSERT INTO app_settings (key, value) VALUES (?, ?)",
            (f"pending_resume_{path.name}", db.jdumps({"path": str(path), "filename": filename})))
        return {"filename": filename, "ok": False, "needs_email": True,
                "pending_file": path.name, "parsed": parsed,
                "error": "No email address found in the resume — enter it manually."}
    return {"filename": filename, "ok": True, "user_id": result["user_id"],
            "created": result["created"], "parsed": result["parsed"],
            "strength": result["strength"]}


@router.post("/upload-resume")
async def upload_resume(request: Request, file: UploadFile = File(...)):
    auth.require_admin(request)
    content = await file.read()
    return _process_upload(content, file.filename)


@router.post("/bulk-upload")
async def bulk_upload(request: Request, files: list[UploadFile] = File(...)):
    auth.require_admin(request)
    results = []
    for f in files:
        content = await f.read()
        try:
            results.append(_process_upload(content, f.filename))
        except Exception as exc:
            log.exception("Bulk upload failed for %s", f.filename)
            results.append({"filename": f.filename, "ok": False, "error": str(exc)})
    succeeded = sum(1 for r in results if r["ok"])
    return {"results": results,
            "summary": f"Uploaded {len(results)} resumes. {succeeded} successfully parsed. "
                       f"{len(results) - succeeded} failed."}


@router.post("/complete-upload")
def complete_upload(payload: dict, request: Request):
    """Finish an upload whose resume had no detectable email (edge case 1)."""
    auth.require_admin(request)
    pending_file = str(payload.get("pending_file") or "")
    email = str(payload.get("email") or "").strip().lower()
    row = db.query_one("SELECT value FROM app_settings WHERE key = ?", (f"pending_resume_{pending_file}",))
    if row is None:
        raise HTTPException(404, "Pending upload not found")
    info = db.jloads(row["value"], {})
    path = Path(info["path"])
    raw_text = resume_parser.extract_text(path)
    parsed = resume_parser.parse_resume_text(raw_text)
    result = resume_parser.create_or_update_user_from_resume(path, raw_text, parsed, manual_email=email)
    if result["needs_email"]:
        raise HTTPException(400, "That email address is not valid.")
    db.execute("DELETE FROM app_settings WHERE key = ?", (f"pending_resume_{pending_file}",))
    return {"ok": True, "user_id": result["user_id"], "created": result["created"]}


@router.get("/users")
def list_users(request: Request):
    auth.require_admin(request)
    rows = db.query(
        "SELECT id, full_name, email, city, state, is_admin, is_paused, created_at, "
        "last_login_at, last_email_sent_at, email_frequency, resume_strength_score, "
        "(SELECT COUNT(*) FROM job_history j WHERE j.user_id = users.id) AS jobs_sent "
        "FROM users ORDER BY id")
    return {"users": rows}


@router.patch("/users/{user_id}/pause")
def pause_user(user_id: int, payload: dict, request: Request):
    auth.require_admin(request)
    paused = 1 if payload.get("paused") else 0
    db.execute("UPDATE users SET is_paused = ? WHERE id = ?", (paused, user_id))
    db.log("admin", user_id, "Paused" if paused else "Resumed")
    return {"ok": True}


@router.delete("/users/{user_id}")
def delete_user(user_id: int, request: Request):
    admin = auth.require_admin(request)
    if user_id == admin["id"]:
        raise HTTPException(400, "Use the profile page to delete your own account.")
    target = db.query_one("SELECT * FROM users WHERE id = ?", (user_id,))
    if target is None:
        raise HTTPException(404, "User not found")
    for r in db.query("SELECT file_path FROM user_resumes WHERE user_id = ?", (user_id,)):
        try:
            Path(r["file_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    db.execute("DELETE FROM token_usage WHERE user_id = ?", (user_id,))
    db.execute("DELETE FROM users WHERE id = ?", (user_id,))
    db.log("admin", None, f"Admin deleted user {target['email']}")
    return {"ok": True}


@router.get("/analytics")
def analytics(request: Request):
    auth.require_admin(request)
    today = date.today()
    d7 = (today - timedelta(days=7)).isoformat()
    d30 = (today - timedelta(days=30)).isoformat()
    users = db.query_one(
        "SELECT COUNT(*) AS total, SUM(is_paused) AS paused, "
        "SUM(CASE WHEN last_login_at >= ? THEN 1 ELSE 0 END) AS active_7d, "
        "SUM(CASE WHEN last_login_at >= ? THEN 1 ELSE 0 END) AS active_30d "
        "FROM users", (d7, d30))
    jobs = db.query_one(
        "SELECT COUNT(*) AS total, "
        "SUM(CASE WHEN date_emailed >= ? THEN 1 ELSE 0 END) AS last_30d, "
        "SUM(CASE WHEN date_emailed >= ? THEN 1 ELSE 0 END) AS last_7d "
        "FROM job_history", (d30, d7))
    pipeline = db.query_one(
        "SELECT SUM(applied) AS applied, SUM(heard_back) AS heard_back, "
        "SUM(interview_offered) AS interviews, SUM(offer_received) AS offers, "
        "SUM(rejected) AS rejected FROM job_history")
    emails = db.query_one(
        "SELECT COUNT(*) AS sent, SUM(bounced) AS bounced, SUM(opened) AS opened, "
        "SUM(clicked) AS clicked FROM email_tracking WHERE sent_at >= ?", (d30,))
    tokens30 = db.query_one(
        "SELECT COALESCE(SUM(tokens_used),0) AS t, COALESCE(SUM(estimated_cost),0) AS c "
        "FROM token_usage WHERE created_at >= ?", (d30,))
    by_process = db.query(
        "SELECT process_type, SUM(tokens_used) AS tokens, SUM(estimated_cost) AS cost "
        "FROM token_usage WHERE created_at >= ? GROUP BY process_type ORDER BY tokens DESC", (d30,))
    needs_attention = db.query(
        "SELECT email, last_login_at FROM users WHERE last_login_at IS NULL OR last_login_at < ? "
        "ORDER BY last_login_at LIMIT 10", (d30,))
    return {
        "users": users, "jobs": jobs, "pipeline": pipeline, "emails": emails,
        "tokens_30d": tokens30,
        "projected_monthly_cost": round((tokens30["c"] or 0), 2),
        "token_breakdown": by_process,
        "needs_attention": needs_attention,
        "scheduler_jobs": scheduler.job_status(),
        "email_mode": config.effective_email_mode(),
        "search_providers": config.search_provider_names(),
        "llm_available": bool(config.ANTHROPIC_API_KEY),
        "disclaimer": "Estimates based on current usage patterns. Actual costs may vary.",
    }


@router.get("/email-deliverability")
def deliverability(request: Request, days: int = 30):
    auth.require_admin(request)
    days = days if days in (7, 30, 90) else 30
    since = (date.today() - timedelta(days=days)).isoformat()
    rows = db.query(
        "SELECT email_type, COUNT(*) AS sent, SUM(bounced) AS bounced, SUM(opened) AS opened, "
        "SUM(clicked) AS clicked FROM email_tracking WHERE sent_at >= ? "
        "GROUP BY email_type ORDER BY sent DESC", (since,))
    for r in rows:
        sent = r["sent"] or 1
        r["bounce_rate"] = round(100.0 * (r["bounced"] or 0) / sent, 1)
        r["open_rate"] = round(100.0 * (r["opened"] or 0) / sent, 1)
        r["click_rate"] = round(100.0 * (r["clicked"] or 0) / sent, 1)
    min_open = float(db.get_setting("deliverability_min_open_rate", "10"))
    max_bounce = float(db.get_setting("deliverability_max_bounce_rate", "5"))
    totals = db.query_one(
        "SELECT COUNT(*) AS sent, SUM(bounced) AS bounced, SUM(opened) AS opened "
        "FROM email_tracking WHERE sent_at >= ? AND email_type != 'otp'", (since,))
    warnings = []
    sent = (totals["sent"] or 0)
    if sent >= 10:
        if 100.0 * (totals["opened"] or 0) / sent < min_open:
            warnings.append(f"Open rate below {min_open:.0f}% threshold")
        if 100.0 * (totals["bounced"] or 0) / sent > max_bounce:
            warnings.append(f"Bounce rate above {max_bounce:.0f}% threshold")
    return {"by_type": rows, "warnings": warnings, "days": days,
            "note": "Open tracking uses a pixel and is approximate; spam-folder rate is not "
                    "directly measurable and is shown as 'estimated' only via low open rates."}


@router.get("/token-usage")
def token_usage(request: Request):
    auth.require_admin(request)
    today = date.today().isoformat()
    month_start = date.today().replace(day=1).isoformat()
    def agg(since):
        return db.query_one(
            "SELECT COALESCE(SUM(tokens_used),0) AS tokens, COALESCE(SUM(estimated_cost),0) AS cost "
            "FROM token_usage WHERE created_at >= ?", (since,))
    daily, monthly = agg(today), agg(month_start)
    by_process = db.query(
        "SELECT process_type, SUM(tokens_used) AS tokens, SUM(estimated_cost) AS cost "
        "FROM token_usage WHERE created_at >= ? GROUP BY process_type ORDER BY tokens DESC",
        (month_start,))
    by_user = db.query(
        "SELECT u.email, SUM(t.tokens_used) AS tokens, SUM(t.estimated_cost) AS cost "
        "FROM token_usage t LEFT JOIN users u ON u.id = t.user_id WHERE t.created_at >= ? "
        "GROUP BY t.user_id ORDER BY tokens DESC LIMIT 15", (month_start,))
    day_of_month = max(1, date.today().day)
    projected = monthly["cost"] / day_of_month * 30
    return {
        "daily": daily, "monthly": monthly,
        "projected_monthly_cost": round(projected, 2),
        "by_process": by_process, "by_user": by_user,
        "budgets": {"daily": int(db.get_setting("token_daily_budget", "2000000")),
                    "monthly": int(db.get_setting("token_monthly_budget", "40000000"))},
        "alert_email": config.ALERT_EMAIL,
    }


@router.put("/token-budget")
def set_budget(payload: dict, request: Request):
    auth.require_admin(request)
    if "daily" in payload:
        db.set_setting("token_daily_budget", str(max(0, int(payload["daily"]))))
    if "monthly" in payload:
        db.set_setting("token_monthly_budget", str(max(0, int(payload["monthly"]))))
    return {"ok": True}


@router.get("/stories")
def stories(request: Request):
    auth.require_admin(request)
    return {"stories": db.query(
        "SELECT s.*, u.email AS submitter_email FROM success_stories s "
        "LEFT JOIN users u ON u.id = s.user_id ORDER BY s.id DESC")}


@router.patch("/stories/{story_id}")
def moderate_story(story_id: int, payload: dict, request: Request):
    auth.require_admin(request)
    db.execute("UPDATE success_stories SET is_approved = ? WHERE id = ?",
               (1 if payload.get("approved") else 0, story_id))
    return {"ok": True}


@router.delete("/stories/{story_id}")
def delete_story(story_id: int, request: Request):
    auth.require_admin(request)
    db.execute("DELETE FROM success_stories WHERE id = ?", (story_id,))
    return {"ok": True}


@router.get("/logs")
def logs(request: Request, limit: int = 100):
    auth.require_admin(request)
    return {"logs": db.query(
        "SELECT * FROM automation_log ORDER BY id DESC LIMIT ?", (min(limit, 500),))}


_run_state = {"running": False, "last_result": None}


@router.post("/run-automation")
def run_automation(payload: dict, request: Request):
    """Manual trigger from the admin dashboard. Runs in a background thread."""
    auth.require_admin(request)
    if _run_state["running"]:
        return {"ok": False, "message": "An automation run is already in progress."}
    run_type = payload.get("type", "main")
    user_id = payload.get("user_id")

    def runner():
        _run_state["running"] = True
        try:
            if run_type == "urgency":
                _run_state["last_result"] = automation.run_urgency_check()
            elif run_type == "portfolio":
                _run_state["last_result"] = automation.run_portfolio_checks()
            elif run_type == "budget":
                _run_state["last_result"] = automation.run_token_budget_check()
            elif run_type == "ats_refresh":
                from . import ats_refresh
                _run_state["last_result"] = ats_refresh.run_if_new_index(
                    force=bool(payload.get("force")))
            else:
                _run_state["last_result"] = automation.run_main_cycle(
                    force_user_id=int(user_id) if user_id else None)
        except Exception as exc:
            log.exception("Manual automation run failed")
            _run_state["last_result"] = {"error": str(exc)}
        finally:
            _run_state["running"] = False

    threading.Thread(target=runner, daemon=True).start()
    return {"ok": True, "message": f"{run_type} run started — watch the activity log below."}


@router.get("/run-status")
def run_status(request: Request):
    auth.require_admin(request)
    return {"running": _run_state["running"], "last_result": _run_state["last_result"]}
