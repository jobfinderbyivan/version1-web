"""REST API (spec section 15 step 2). All user-scoped endpoints resolve the
user from the session cookie — no user IDs accepted from the client."""
import logging
import threading
import urllib.parse
from datetime import date, datetime, timedelta
from pathlib import Path

from fastapi import APIRouter, File, Form, Request, Response, UploadFile
from fastapi.exceptions import HTTPException
from fastapi.responses import FileResponse, RedirectResponse

from . import (advice, auth, automation, config, db, emailer, goals, market,
               resume_parser, scheduler)

log = logging.getLogger("api")
router = APIRouter()


# ------------------------------------------------------------------ auth ---

@router.post("/api/auth/request-otp")
def request_otp(payload: dict):
    auth.request_otp(payload.get("email", ""))
    return {"message": auth.GENERIC_MESSAGE}


@router.post("/api/auth/verify-otp")
def verify_otp(payload: dict, response: Response):
    user = auth.verify_otp(payload.get("email", ""), payload.get("code", ""))
    token = auth.make_session_token(user["id"])
    response.set_cookie("session", token, max_age=config.SESSION_DAYS * 86400,
                        httponly=True, samesite="lax", secure=config.SECURE_COOKIES)
    return {"ok": True, "is_admin": bool(user["is_admin"])}


@router.post("/api/auth/logout")
def logout(response: Response):
    response.delete_cookie("session")
    return {"ok": True}


# --------------------------------------------------------------- profile ---

USER_PUBLIC_FIELDS = [
    "id", "full_name", "email", "city", "state", "home_address", "skills",
    "experience_level", "work_history", "bio", "linkedin_url", "portfolio_url",
    "preferred_salary", "minimum_salary", "preferred_positions", "additional_skills",
    "feedback_notes", "job_type_fulltime", "job_type_parttime", "job_type_contract",
    "job_type_remote", "job_type_hybrid", "job_type_onsite", "search_radius_miles",
    "commute_mode", "commute_max_minutes", "preferred_benefits", "exclude_drug_testing",
    "exclude_criminal_record_filter", "search_keywords", "negative_keywords",
    "industry_preferences", "blacklisted_companies", "whitelisted_companies",
    "email_frequency", "urgency_alerts_enabled", "resume_advice_enabled",
    "resume_strength_score", "resume_strength_summary", "is_admin", "is_paused",
    "dark_mode", "google_calendar_connected",
    "email_pref_job_listings", "email_pref_resume_advice", "email_pref_progress_summaries",
    "email_pref_urgency_alerts", "email_pref_interview_prep", "email_pref_cert_recommendations",
    "email_pref_linkedin_tips", "email_pref_goal_milestones", "email_pref_system_notifications",
]

EDITABLE_TEXT = {
    "full_name", "city", "state", "home_address", "bio", "linkedin_url", "portfolio_url",
    "preferred_salary", "minimum_salary", "preferred_positions", "additional_skills",
    "feedback_notes", "commute_mode", "email_frequency",
}
EDITABLE_JSON = {
    "preferred_benefits", "search_keywords", "negative_keywords", "industry_preferences",
    "blacklisted_companies", "whitelisted_companies",
}
EDITABLE_BOOL = {
    "job_type_fulltime", "job_type_parttime", "job_type_contract", "job_type_remote",
    "job_type_hybrid", "job_type_onsite", "exclude_drug_testing",
    "exclude_criminal_record_filter", "urgency_alerts_enabled", "resume_advice_enabled",
    "dark_mode", "email_pref_job_listings", "email_pref_resume_advice",
    "email_pref_progress_summaries", "email_pref_urgency_alerts", "email_pref_interview_prep",
    "email_pref_cert_recommendations", "email_pref_linkedin_tips", "email_pref_goal_milestones",
    "email_pref_system_notifications",
}
EDITABLE_INT = {"search_radius_miles", "commute_max_minutes"}


@router.get("/api/profile")
def get_profile(request: Request):
    user = auth.require_user(request)
    data = {k: user.get(k) for k in USER_PUBLIC_FIELDS}
    for k in ("skills", "preferred_benefits", "search_keywords", "negative_keywords",
              "industry_preferences", "blacklisted_companies", "whitelisted_companies",
              "work_history"):
        data[k] = db.jloads(user.get(k), [])
    data["resumes"] = [
        {k: r[k] for k in ("id", "label", "is_primary", "experience_level", "created_at")} |
        {"skills": db.jloads(r["skills"], []),
         "file_type": Path(r["file_path"] or "").suffix.lower().lstrip(".") or "txt"}
        for r in db.query("SELECT * FROM user_resumes WHERE user_id = ? ORDER BY is_primary DESC, id DESC",
                          (user["id"],))
    ]
    data["goals"] = goals.active_goals(user["id"])
    data["cert_recommendations"] = [
        r | {"courses": db.jloads(r["courses"], [])}
        for r in db.query("SELECT * FROM cert_recommendations WHERE user_id = ? AND dismissed = 0 "
                          "ORDER BY gap_count DESC", (user["id"],))]
    li = db.query_one("SELECT * FROM linkedin_analyses WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                      (user["id"],))
    data["linkedin_analysis"] = (db.jloads(li["analysis_data"], {}) | {"created_at": li["created_at"]}) if li else None
    pc = db.query_one("SELECT * FROM portfolio_checks WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                      (user["id"],))
    data["portfolio_status"] = (pc | {"issues": db.jloads(pc["issues"], [])}) if pc else None
    pa = db.query_one("SELECT * FROM portfolio_analyses WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                      (user["id"],))
    data["portfolio_analysis"] = (db.jloads(pa["analysis_data"], {}) | {"created_at": pa["created_at"]}) if pa else None
    # skills gap summary across recent matches
    gaps = {}
    for r in db.query("SELECT skills_gap FROM job_history WHERE user_id = ? AND skills_gap IS NOT NULL "
                      "ORDER BY id DESC LIMIT 40", (user["id"],)):
        for g in db.jloads(r["skills_gap"], []):
            gaps[g] = gaps.get(g, 0) + 1
    data["skills_gap_summary"] = [g for g, _ in sorted(gaps.items(), key=lambda kv: -kv[1])[:5]]
    data["email_mode"] = config.effective_email_mode()
    data["search_providers"] = config.search_provider_names()
    data["llm_available"] = bool(config.ANTHROPIC_API_KEY)
    return data


@router.put("/api/profile")
def update_profile(payload: dict, request: Request):
    user = auth.require_user(request)
    sets, params = [], []
    for key, value in payload.items():
        if key in EDITABLE_TEXT:
            sets.append(f"{key} = ?")
            params.append(str(value or "").strip()[:2000])
        elif key in EDITABLE_JSON:
            if not isinstance(value, list):
                raise HTTPException(400, f"{key} must be a list")
            sets.append(f"{key} = ?")
            params.append(db.jdumps([str(v).strip()[:120] for v in value][:60]))
        elif key in EDITABLE_BOOL:
            sets.append(f"{key} = ?")
            params.append(1 if value else 0)
        elif key in EDITABLE_INT:
            sets.append(f"{key} = ?")
            params.append(max(0, min(int(value or 0), 1000)))
    if not sets:
        return {"ok": True}
    if "search_radius_miles" in payload:
        idx = sets.index("search_radius_miles = ?")
        params[idx] = max(5, min(int(payload["search_radius_miles"] or 15), 50))
    if "commute_max_minutes" in payload:
        idx = sets.index("commute_max_minutes = ?")
        params[idx] = max(10, min(int(payload["commute_max_minutes"] or 30), 90))
    if payload.get("email_frequency") not in (None, "daily", "every_3_days", "weekly"):
        raise HTTPException(400, "invalid email_frequency")
    if payload.get("commute_mode") not in (None, "radius", "driving", "transit", "walking", "biking"):
        raise HTTPException(400, "invalid commute_mode")
    # frequency change: next email counts from now (spec edge case 19)
    if "email_frequency" in payload and payload["email_frequency"] != user.get("email_frequency"):
        sets.append("last_email_sent_at = ?")
        params.append(db.now())
    sets.append("updated_at = ?")
    params.append(db.now())
    params.append(user["id"])
    db.execute(f"UPDATE users SET {', '.join(sets)} WHERE id = ?", tuple(params))
    return {"ok": True}


@router.delete("/api/profile")
def delete_account(request: Request, response: Response):
    """GDPR deletion (spec 4.5). Stories persist anonymously via ON DELETE SET NULL."""
    user = auth.require_user(request)
    for r in db.query("SELECT file_path FROM user_resumes WHERE user_id = ?", (user["id"],)):
        try:
            Path(r["file_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    if user.get("resume_file_path"):
        try:
            Path(user["resume_file_path"]).unlink(missing_ok=True)
        except OSError:
            pass
    body = emailer.wrap_html(
        "Your account has been deleted",
        "<p>All your data has been permanently removed from our system. "
        "If this was a mistake, contact your administrator.</p>")
    emailer.send(user, "system", "Your account has been deleted", body, track=False)
    db.execute("DELETE FROM otp_codes WHERE email = ?", (user["email"],))
    db.execute("DELETE FROM token_usage WHERE user_id = ?", (user["id"],))
    db.execute("DELETE FROM users WHERE id = ?", (user["id"],))  # cascades the rest
    response.delete_cookie("session")
    db.log("system", None, "Account deleted (GDPR)")
    return {"ok": True}


# ------------------------------------------------------ run search now ----

_user_runs: dict = {}  # user_id -> {running, last_result, last_run_at}
RUN_COOLDOWN_MINUTES = 10


@router.post("/api/run-search")
def run_search_now(request: Request):
    """User-triggered immediate search-and-email (bypasses the cadence; the
    cadence clock resets from now so no duplicate email follows today)."""
    user = auth.require_user(request)
    uid = user["id"]
    state = _user_runs.get(uid) or {}
    if state.get("running"):
        raise HTTPException(409, "A search is already running for your account.")
    last = state.get("last_run_at")
    if last and datetime.now() - last < timedelta(minutes=RUN_COOLDOWN_MINUTES):
        remaining = RUN_COOLDOWN_MINUTES - int((datetime.now() - last).total_seconds() // 60)
        raise HTTPException(429, f"Please wait about {remaining} more minute(s) between manual searches.")
    if not (user.get("resume_raw_text") or "").strip():
        raise HTTPException(400, "Upload a resume first so we know what to search for.")
    if user.get("is_paused"):
        raise HTTPException(400, "Your automation is paused — ask the administrator to resume it.")
    _user_runs[uid] = {"running": True, "last_result": None, "last_run_at": datetime.now()}

    def runner():
        try:
            _user_runs[uid]["last_result"] = automation.run_main_cycle(force_user_id=uid)
        except Exception as exc:
            log.exception("Manual run failed for user %s", uid)
            _user_runs[uid]["last_result"] = {"error": str(exc)}
        finally:
            _user_runs[uid]["running"] = False

    threading.Thread(target=runner, daemon=True).start()
    db.log("manual", uid, "User triggered an immediate search")
    return {"ok": True}


@router.get("/api/run-search/status")
def run_search_status(request: Request):
    user = auth.require_user(request)
    state = _user_runs.get(user["id"]) or {}
    return {"running": bool(state.get("running")), "last_result": state.get("last_result")}


# ----------------------------------------------------------------- jobs ---

JOB_FILTERS = {
    "all": "1=1",
    "applied": "applied = 1",
    "heard_back": "heard_back = 1",
    "interview_offered": "interview_offered = 1",
    "offer_received": "offer_received = 1",
    "rejected": "rejected = 1",
    "not_interested": "not_interested = 1",
    "not_yet_applied": "applied = 0 AND not_interested = 0",
}
JOB_SORTS = {
    "date": "date_emailed DESC, id DESC",
    "company": "company_name COLLATE NOCASE ASC",
    "title": "job_title COLLATE NOCASE ASC",
    "score": "match_score DESC",
}


def _job_row(r: dict) -> dict:
    for k in ("skills_gap", "detected_benefits", "preferred_qualifications"):
        r[k] = db.jloads(r.get(k), [])
    r["company_research"] = db.jloads(r.get("company_research"), {})
    r["has_prep"] = bool(r.pop("interview_prep_data", None))
    r.pop("description", None)
    return r


@router.get("/api/jobs")
def list_jobs(request: Request, filter: str = "all", sort: str = "date", page: int = 1):
    user = auth.require_user(request)
    where = JOB_FILTERS.get(filter, "1=1")
    order = JOB_SORTS.get(sort, JOB_SORTS["date"])
    page = max(1, page)
    total = db.query_one(f"SELECT COUNT(*) AS n FROM job_history WHERE user_id = ? AND {where}",
                         (user["id"],))["n"]
    rows = db.query(
        f"SELECT * FROM job_history WHERE user_id = ? AND {where} ORDER BY {order} LIMIT 20 OFFSET ?",
        (user["id"], (page - 1) * 20))
    stats = db.query_one(
        "SELECT COUNT(*) AS total, SUM(applied) AS applied, SUM(heard_back) AS heard_back, "
        "SUM(interview_offered) AS interviews, SUM(offer_received) AS offers, SUM(rejected) AS rejected "
        "FROM job_history WHERE user_id = ?", (user["id"],))
    return {"jobs": [_job_row(r) for r in rows], "total": total, "page": page,
            "pages": max(1, -(-total // 20)), "stats": stats,
            "benefit_prefs": db.jloads(user.get("preferred_benefits"), [])}


STATUS_FIELDS = {"applied", "heard_back", "interview_offered", "offer_received", "rejected",
                 "not_interested"}
GOAL_BY_FIELD = {"applied": "applications", "interview_offered": "interviews",
                 "offer_received": "offers"}


def _own_job(request: Request, job_id: int) -> tuple:
    user = auth.require_user(request)
    job = db.query_one("SELECT * FROM job_history WHERE id = ? AND user_id = ?", (job_id, user["id"]))
    if job is None:
        raise HTTPException(404, "Job not found")
    return user, job


@router.patch("/api/jobs/{job_id}")
def update_job(job_id: int, payload: dict, request: Request):
    user, job = _own_job(request, job_id)
    sets, params = [], []
    for field in STATUS_FIELDS:
        if field in payload:
            value = 1 if payload[field] else 0
            sets.append(f"{field} = ?")
            params.append(value)
            if field in GOAL_BY_FIELD and value != job[field]:
                goals.bump(user, GOAL_BY_FIELD[field], 1 if value else -1)
    # mutual exclusivity (spec 5.4.4)
    if payload.get("offer_received"):
        sets.append("rejected = 0")
    if payload.get("rejected"):
        sets.append("offer_received = 0")
    for field in ("interview_date", "interview_time", "interview_notes", "not_interested_reason"):
        if field in payload:
            sets.append(f"{field} = ?")
            params.append(str(payload[field] or "")[:1000])
    if not sets:
        return {"ok": True}
    params.append(job_id)
    db.execute(f"UPDATE job_history SET {', '.join(sets)} WHERE id = ?", tuple(params))
    return {"ok": True, "job": _job_row(db.query_one("SELECT * FROM job_history WHERE id = ?", (job_id,)))}


@router.post("/api/jobs/{job_id}/interview-prep")
def create_prep(job_id: int, request: Request):
    user, job = _own_job(request, job_id)
    html = advice.generate_interview_prep(user, job)
    return {"html": html}


@router.get("/api/jobs/{job_id}/interview-prep")
def get_prep(job_id: int, request: Request):
    _, job = _own_job(request, job_id)
    return {"html": job.get("interview_prep_data") or None}


@router.post("/api/jobs/{job_id}/mock-interview/start")
def mock_start(job_id: int, request: Request):
    user, job = _own_job(request, job_id)
    return {"questions": advice.mock_interview_questions(user, job)}


@router.post("/api/jobs/{job_id}/mock-interview/evaluate")
def mock_evaluate(job_id: int, payload: dict, request: Request):
    user, job = _own_job(request, job_id)
    questions = payload.get("questions") or []
    responses = [str(r or "")[:4000] for r in (payload.get("responses") or [])]
    return advice.evaluate_mock_interview(user, job, questions, responses)


@router.get("/api/jobs/{job_id}/mock-interview/sessions")
def mock_sessions(job_id: int, request: Request):
    user, _ = _own_job(request, job_id)
    rows = db.query(
        "SELECT id, overall_score, created_at, feedback FROM mock_interview_sessions "
        "WHERE user_id = ? AND job_history_id = ? ORDER BY id DESC", (user["id"], job_id))
    return {"sessions": [r | {"feedback": db.jloads(r["feedback"], {})} for r in rows]}


@router.post("/api/jobs/{job_id}/calendar-sync")
def calendar_sync(job_id: int, request: Request):
    user, job = _own_job(request, job_id)
    if not job.get("interview_date"):
        raise HTTPException(400, "Set an interview date first.")
    db.execute("UPDATE job_history SET calendar_event_synced = 1 WHERE id = ?", (job_id,))
    return {"ok": True, "ics_url": f"/api/jobs/{job_id}/calendar.ics"}


@router.get("/api/jobs/{job_id}/calendar.ics")
def calendar_ics(job_id: int, request: Request):
    """Local replacement for Google Calendar OAuth: a downloadable .ics event
    with the job details and 24h/2h reminders (importable into any calendar)."""
    user, job = _own_job(request, job_id)
    if not job.get("interview_date"):
        raise HTTPException(400, "Set an interview date first.")
    time_part = (job.get("interview_time") or "09:00").replace(":", "")[:4] + "00"
    dt = job["interview_date"].replace("-", "") + "T" + time_part
    research = db.jloads(job.get("company_research"), {})
    desc_bits = [f"Job listing: {job['apply_link']}"]
    if research.get("mission"):
        desc_bits.append(f"About the company: {research['mission']}")
    if job.get("interview_prep_data"):
        desc_bits.append(f"Interview prep: {config.APP_BASE_URL}/jobs")
    desc_bits.append("Good luck! 🍀")
    description = "\\n".join(b.replace("\n", " ") for b in desc_bits)
    location = job.get("location") or "Video call"
    ics = "\r\n".join([
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//JobSearchAssistant//EN",
        "BEGIN:VEVENT",
        f"UID:job-{job_id}@jobsearch.local",
        f"DTSTART:{dt}",
        f"SUMMARY:Interview: {job['job_title']} at {job['company_name']}",
        f"LOCATION:{location}",
        f"DESCRIPTION:{description}",
        "BEGIN:VALARM", "TRIGGER:-P1D", "ACTION:DISPLAY", "DESCRIPTION:Interview tomorrow", "END:VALARM",
        "BEGIN:VALARM", "TRIGGER:-PT2H", "ACTION:DISPLAY", "DESCRIPTION:Interview in 2 hours", "END:VALARM",
        "END:VEVENT", "END:VCALENDAR", ""])
    return Response(content=ics, media_type="text/calendar",
                    headers={"Content-Disposition": f"attachment; filename=interview-{job_id}.ics"})


@router.get("/api/jobs/map-data")
def map_data(request: Request):
    user = auth.require_user(request)
    from . import geo
    home = None
    if user.get("city"):
        coords = geo.geocode(", ".join(p for p in (user.get("city"), user.get("state")) if p))
        if coords:
            home = {"lat": coords[0], "lon": coords[1],
                    "label": f"{user.get('city')}, {user.get('state') or ''}".strip(", ")}
    jobs = db.query(
        "SELECT id, job_title, company_name, location, latitude, longitude, match_score, "
        "applied, heard_back, interview_offered, offer_received, rejected "
        "FROM job_history WHERE user_id = ? AND latitude IS NOT NULL", (user["id"],))
    for j in jobs:
        j["status"] = ("rejected" if j["rejected"] else "offer" if j["offer_received"] else
                       "progress" if (j["heard_back"] or j["interview_offered"]) else
                       "applied" if j["applied"] else "new")
    return {"home": home, "jobs": jobs}


# --------------------------------------------------------------- resumes ---

@router.post("/api/resumes/upload")
async def upload_resume(request: Request, file: UploadFile = File(...), label: str = Form("My Resume")):
    user = auth.require_user(request)
    count = db.query_one("SELECT COUNT(*) AS n FROM user_resumes WHERE user_id = ?", (user["id"],))["n"]
    if count >= config.MAX_RESUMES_PER_USER:
        raise HTTPException(400, "Maximum 3 resumes. Delete one to upload another.")
    content = await file.read()
    if len(content) > 10 * 1024 * 1024:
        raise HTTPException(400, "File too large (max 10 MB).")
    try:
        path = resume_parser.save_resume_file(user["id"], file.filename, content)
        raw_text = resume_parser.extract_text(path)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    if not raw_text.strip():
        raise HTTPException(400, "Could not extract any text from that file.")
    parsed = resume_parser.parse_resume_text(raw_text, user["id"])
    is_primary = 1 if count == 0 else 0
    db.execute(
        "INSERT INTO user_resumes (user_id, label, file_path, raw_text, skills, experience_level, is_primary) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user["id"], label.strip()[:120] or "My Resume", str(path), raw_text,
         db.jdumps(parsed.get("skills") or []), parsed.get("experience_level"), is_primary))
    strength = resume_parser.compute_strength_score(raw_text, user["id"])
    merged_skills = sorted(set(db.jloads(user.get("skills"), []) + (parsed.get("skills") or [])))
    updates = ["resume_strength_score = ?", "resume_strength_summary = ?", "skills = ?",
               "resume_advice_pending = 1", "updated_at = ?"]
    params = [strength["score"], strength.get("summary", ""), db.jdumps(merged_skills), db.now()]
    if is_primary:
        updates += ["resume_file_path = ?", "resume_raw_text = ?", "experience_level = ?",
                    "work_history = ?"]
        params += [str(path), raw_text, parsed.get("experience_level"),
                   db.jdumps(parsed.get("work_history") or [])]
    # Auto-apply parsed location and links to the profile when the user hasn't
    # set them — the location is required for job search to work.
    def _fill(field, value):
        if value and not (user.get(field) or "").strip():
            updates.append(f"{field} = ?")
            params.append(value)
    _fill("city", parsed.get("city"))
    _fill("state", parsed.get("state"))
    _fill("home_address", parsed.get("city") and
          ", ".join(p for p in (parsed.get("city"), parsed.get("state")) if p))
    _fill("linkedin_url", parsed.get("linkedin_url"))
    _fill("portfolio_url", parsed.get("portfolio_url"))
    params.append(user["id"])
    db.execute(f"UPDATE users SET {', '.join(updates)} WHERE id = ?", tuple(params))
    out_user = db.query_one("SELECT city, state FROM users WHERE id = ?", (user["id"],))
    return {"ok": True, "strength": strength, "parsed_skills": parsed.get("skills") or [],
            "city": out_user["city"], "state": out_user["state"]}


@router.delete("/api/resumes/{resume_id}")
def delete_resume(resume_id: int, request: Request):
    user = auth.require_user(request)
    row = db.query_one("SELECT * FROM user_resumes WHERE id = ? AND user_id = ?", (resume_id, user["id"]))
    if row is None:
        raise HTTPException(404, "Resume not found")
    try:
        Path(row["file_path"]).unlink(missing_ok=True)
    except OSError:
        pass
    db.execute("DELETE FROM user_resumes WHERE id = ?", (resume_id,))
    if row["is_primary"]:
        nxt = db.query_one("SELECT * FROM user_resumes WHERE user_id = ? ORDER BY id DESC", (user["id"],))
        if nxt:
            db.execute("UPDATE user_resumes SET is_primary = 1 WHERE id = ?", (nxt["id"],))
            db.execute("UPDATE users SET resume_file_path = ?, resume_raw_text = ? WHERE id = ?",
                       (nxt["file_path"], nxt["raw_text"], user["id"]))
        else:
            db.execute("UPDATE users SET resume_file_path = NULL, resume_raw_text = NULL WHERE id = ?",
                       (user["id"],))
    return {"ok": True}


@router.patch("/api/resumes/{resume_id}")
def update_resume(resume_id: int, payload: dict, request: Request):
    user = auth.require_user(request)
    row = db.query_one("SELECT * FROM user_resumes WHERE id = ? AND user_id = ?", (resume_id, user["id"]))
    if row is None:
        raise HTTPException(404, "Resume not found")
    if "label" in payload:
        db.execute("UPDATE user_resumes SET label = ? WHERE id = ?",
                   (str(payload["label"]).strip()[:120] or row["label"], resume_id))
    if payload.get("is_primary"):
        db.execute("UPDATE user_resumes SET is_primary = 0 WHERE user_id = ?", (user["id"],))
        db.execute("UPDATE user_resumes SET is_primary = 1 WHERE id = ?", (resume_id,))
        db.execute("UPDATE users SET resume_file_path = ?, resume_raw_text = ?, experience_level = ? "
                   "WHERE id = ?", (row["file_path"], row["raw_text"], row["experience_level"], user["id"]))
    return {"ok": True}


RESUME_MEDIA_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
}


def _own_resume(request: Request, resume_id: int) -> dict:
    user = auth.require_user(request)
    row = db.query_one("SELECT * FROM user_resumes WHERE id = ? AND user_id = ?",
                       (resume_id, user["id"]))
    if row is None:
        raise HTTPException(404, "Resume not found")
    return row


@router.get("/api/resumes/{resume_id}/file")
def resume_file(resume_id: int, request: Request):
    """Serve the original resume file inline for the preview viewer."""
    row = _own_resume(request, resume_id)
    path = Path(row["file_path"] or "")
    if not path.exists():
        raise HTTPException(404, "The original file is no longer on disk.")
    media = RESUME_MEDIA_TYPES.get(path.suffix.lower(), "application/octet-stream")
    return FileResponse(path, media_type=media, filename=path.name,
                        content_disposition_type="inline")


@router.get("/api/resumes/{resume_id}/text")
def resume_text(resume_id: int, request: Request):
    """Extracted text — thumbnail content and viewer fallback for DOCX/TXT."""
    row = _own_resume(request, resume_id)
    return {"label": row["label"], "text": (row["raw_text"] or "")[:20000]}


@router.post("/api/resume-advice")
def resume_advice_now(request: Request):
    user = auth.require_user(request)
    return advice.send_full_resume_advice(user)


# ---------------------------------------------- linkedin & portfolio -------

@router.post("/api/linkedin/analyze")
def linkedin_analyze(request: Request):
    user = auth.require_user(request)
    return advice.linkedin_analyze(user, send_email=bool(user.get("email_pref_linkedin_tips", 1)))


@router.get("/api/linkedin/analysis")
def linkedin_latest(request: Request):
    user = auth.require_user(request)
    row = db.query_one("SELECT * FROM linkedin_analyses WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                       (user["id"],))
    if row is None:
        return {"analysis": None}
    return {"analysis": db.jloads(row["analysis_data"], {}), "created_at": row["created_at"]}


@router.post("/api/portfolio/validate")
def portfolio_validate(request: Request):
    user = auth.require_user(request)
    url = (user.get("portfolio_url") or "").strip()
    if not url:
        raise HTTPException(400, "Add your portfolio URL first (and save your profile).")
    if not url.startswith("http"):
        url = "https://" + url
    result = automation.check_portfolio_url(url)
    db.execute(
        "INSERT INTO portfolio_checks (user_id, url, status_code, response_time_ms, ssl_valid, "
        "is_accessible, issues) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user["id"], url, result["status_code"], result["response_time_ms"],
         1 if result["ssl_valid"] else 0, 1 if result["is_accessible"] else 0,
         db.jdumps(result["issues"])))
    return result


@router.post("/api/portfolio/analyze")
def portfolio_analyze(request: Request):
    user = auth.require_user(request)
    return advice.portfolio_analyze(user)


@router.get("/api/portfolio/status")
def portfolio_status(request: Request):
    user = auth.require_user(request)
    check = db.query_one("SELECT * FROM portfolio_checks WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                         (user["id"],))
    analysis = db.query_one("SELECT * FROM portfolio_analyses WHERE user_id = ? ORDER BY id DESC LIMIT 1",
                            (user["id"],))
    return {"check": (check | {"issues": db.jloads(check["issues"], [])}) if check else None,
            "analysis": db.jloads(analysis["analysis_data"], {}) if analysis else None}


# ------------------------------------------------------ market & goals -----

@router.get("/api/market-intelligence")
def market_intelligence(request: Request):
    user = auth.require_user(request)
    return market.get_intelligence(user["id"])


@router.get("/api/goals")
def get_goals(request: Request):
    user = auth.require_user(request)
    return {"goals": goals.active_goals(user["id"])}


@router.post("/api/goals")
def create_goal(payload: dict, request: Request):
    user = auth.require_user(request)
    goal_type = payload.get("goal_type")
    if goal_type not in ("applications", "interviews", "offers"):
        raise HTTPException(400, "goal_type must be applications, interviews or offers")
    period = payload.get("goal_period")
    if period not in ("weekly", "monthly"):
        raise HTTPException(400, "goal_period must be weekly or monthly")
    target = max(1, min(int(payload.get("goal_target") or 1), 1000))
    db.execute(
        "INSERT INTO user_goals (user_id, goal_type, goal_target, goal_period, period_start) "
        "VALUES (?, ?, ?, ?, ?)",
        (user["id"], goal_type, target, period, goals.current_period_start(period)))
    return {"ok": True, "goals": goals.active_goals(user["id"])}


@router.put("/api/goals/{goal_id}")
def update_goal(goal_id: int, payload: dict, request: Request):
    user = auth.require_user(request)
    row = db.query_one("SELECT * FROM user_goals WHERE id = ? AND user_id = ?", (goal_id, user["id"]))
    if row is None:
        raise HTTPException(404, "Goal not found")
    target = max(1, min(int(payload.get("goal_target") or row["goal_target"]), 1000))
    db.execute("UPDATE user_goals SET goal_target = ?, updated_at = ? WHERE id = ?",
               (target, db.now(), goal_id))
    return {"ok": True, "goals": goals.active_goals(user["id"])}


@router.delete("/api/goals/{goal_id}")
def delete_goal(goal_id: int, request: Request):
    user = auth.require_user(request)
    db.execute("DELETE FROM user_goals WHERE id = ? AND user_id = ?", (goal_id, user["id"]))
    return {"ok": True}


@router.post("/api/certs/{rec_id}/dismiss")
def dismiss_cert(rec_id: int, request: Request):
    user = auth.require_user(request)
    db.execute("UPDATE cert_recommendations SET dismissed = 1 WHERE id = ? AND user_id = ?",
               (rec_id, user["id"]))
    return {"ok": True}


# ---------------------------------------------------------------- stories --

@router.get("/api/stories")
def list_stories(request: Request):
    auth.require_user(request)
    rows = db.query(
        "SELECT id, previous_role, new_role, new_salary, timeframe, story, created_at "
        "FROM success_stories WHERE is_approved = 1 ORDER BY id DESC LIMIT 100")
    return {"stories": rows}


@router.post("/api/stories")
def submit_story(payload: dict, request: Request):
    user = auth.require_user(request)
    new_role = str(payload.get("new_role") or "").strip()[:255]
    if not new_role:
        raise HTTPException(400, "new_role is required")
    db.execute(
        "INSERT INTO success_stories (user_id, previous_role, new_role, new_salary, timeframe, story) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (user["id"], str(payload.get("previous_role") or "").strip()[:255], new_role,
         str(payload.get("new_salary") or "").strip()[:100],
         str(payload.get("timeframe") or "").strip()[:100],
         str(payload.get("story") or "").strip()[:500]))
    return {"ok": True}
