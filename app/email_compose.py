"""HTML composition for job-listing and urgency emails (spec section 7.2).
Table-based markup for mobile email client compatibility. Sections are
included/excluded according to the user's granular email preferences."""
import html
from datetime import date, datetime, timedelta

from . import config, db, goals


def _e(value) -> str:
    return html.escape(str(value or ""))


def job_block(index, job, user) -> str:
    research = job.get("company_research") or {}
    user_benefits = set(db.jloads(user.get("preferred_benefits"), []))
    benefits = job.get("detected_benefits") or []
    matched = [b for b in benefits if b in user_benefits]
    rows = []

    score_color = "#16a34a" if job["match_score"] >= 80 else "#ca8a04" if job["match_score"] >= 60 else "#ea580c"
    rows.append(
        f"<div style='font-size:18px;font-weight:bold;color:#111827'>"
        f"🏢 {index}. {_e(job['job_title'])} "
        f"<span style='background:{score_color};color:#fff;border-radius:12px;padding:2px 10px;"
        f"font-size:13px;vertical-align:middle'>{int(job['match_score'])}% Match</span></div>"
    )
    rows.append(f"<div style='margin-top:6px'><strong>Company:</strong> {_e(job['company_name'])}</div>")
    rows.append(f"<div>📍 <strong>Location:</strong> {_e(job.get('location') or 'Not specified')}</div>")
    if job.get("commute_minutes"):
        mode = {"driving": "car", "transit": "public transit", "walking": "foot", "biking": "bike"}.get(
            job.get("commute_mode") or "", job.get("commute_mode") or "")
        rows.append(f"<div>🚗 <strong>Commute:</strong> ~{job['commute_minutes']} minutes by {mode}</div>")
    if job.get("salary"):
        source = f" (Source: {_e(job.get('salary_source') or 'Job listing')})"
        rows.append(f"<div>💰 <strong>Salary:</strong> {_e(job['salary'])}{source}</div>")
    else:
        rows.append("<div>💰 <strong>Salary:</strong> Not listed</div>")
    if job.get("applicant_count"):
        rows.append(f"<div>👥 <strong>Applicants:</strong> {_e(job['applicant_count'])}</div>")
    if matched:
        nice = ", ".join(b.replace("_", " ").title() for b in matched)
        rows.append(f"<div>✨ <strong>Benefits:</strong> {_e(nice)} &mdash; matches your preferences!</div>")
    elif benefits:
        nice = ", ".join(b.replace("_", " ").title() for b in benefits[:4])
        rows.append(f"<div>✨ <strong>Benefits:</strong> {_e(nice)}</div>")
    if job.get("is_multi_hire"):
        rows.append("<div>🟢 Hiring Multiple Positions</div>")
    if job.get("is_reopened"):
        rows.append("<div>🔄 This position was previously listed and appears to have been reopened.</div>")
    if job.get("is_urgent"):
        rows.append("<div>🔥 <strong>Urgent Hire</strong> — this position appears to need immediate filling.</div>")
    if job.get("deadline_date"):
        rows.append(f"<div>⏰ <strong>Application Deadline:</strong> {_e(job['deadline_date'])} — apply soon!</div>")
    if job.get("match_reason"):
        rows.append(f"<div style='margin-top:6px'>✅ <strong>Why this matches you:</strong> {_e(job['match_reason'])}</div>")
    gaps = db.jloads(job.get("skills_gap"), []) if isinstance(job.get("skills_gap"), str) else (job.get("skills_gap") or [])
    if gaps:
        rows.append(
            f"<div style='margin-top:6px;background:#fef9c3;border-radius:8px;padding:8px 10px'>"
            f"💡 <strong>Skills note:</strong> this role asks for "
            f"{', '.join('<strong>' + _e(g) + '</strong>' for g in gaps[:3])} — "
            f"consider highlighting these if you have them.</div>"
        )
    if any(research.get(k) for k in ("size", "glassdoor_rating", "recent_news", "mission", "notable_info")):
        bits = " &bull; ".join(_e(research[k]) for k in ("size", "glassdoor_rating", "recent_news", "mission", "notable_info") if research.get(k))
        rows.append(
            f"<div style='margin-top:8px;background:#f3f4f6;border-radius:8px;padding:10px'>"
            f"📋 <strong>About {_e(job['company_name'])}:</strong><br>{bits}</div>"
        )
    rows.append(
        f"<div style='margin-top:12px'><a href='{_e(job['apply_link'])}' "
        f"style='background:#4f46e5;color:#ffffff;text-decoration:none;padding:10px 22px;"
        f"border-radius:8px;font-weight:bold;display:inline-block'>Apply Here →</a></div>"
    )
    if job.get("aggregate_link") and job["aggregate_link"] != job["apply_link"]:
        rows.append(f"<div style='margin-top:6px;font-size:13px'>📋 Also listed on: "
                    f"<a href='{_e(job['aggregate_link'])}'>{_e(job['aggregate_link'][:60])}…</a></div>")
    return ("<div style='border:1px solid #e5e7eb;border-radius:12px;padding:16px;margin:14px 0'>"
            + "".join(rows) + "</div>")


def compose_job_email(user, jobs, *, health=None, disability_note=None,
                      cert_recommendations=None, resume_advice=None) -> tuple:
    """Returns (subject, html)."""
    first_name = (user.get("full_name") or "there").split(" ")[0]
    cadence = {"daily": 1, "every_3_days": 3, "weekly": 7}.get(user.get("email_frequency") or "every_3_days", 3)
    start = date.today().strftime("%B %d, %Y")
    end = (date.today() + timedelta(days=cadence)).strftime("%B %d, %Y")
    subject = f"Your Top Job Matches — {start} to {end}"

    parts = [f"<p>Hi {_e(first_name)},</p>",
             f"<p>Here are your top {len(jobs)} personalized job matches based on your resume and preferences:</p>"]
    if health:
        emoji = {"STRONG": "💪", "MODERATE": "📊", "WEAK": "⚠️"}.get(health["health_score"], "")
        parts.append(
            f"<div style='background:#eef2ff;border-radius:10px;padding:10px 14px'>"
            f"<strong>Your job market is {health['health_score']} {emoji}</strong> — we found about "
            f"{health['jobs_per_week']} matching jobs per week for your profile "
            f"({_e(health.get('trend_direction', 'STABLE').title())}).</div>")

    for i, job in enumerate(jobs, 1):
        parts.append(job_block(i, job, user))

    if disability_note:
        parts.append(
            f"<div style='background:#eff6ff;border-radius:10px;padding:12px 14px;margin:10px 0'>"
            f"💙 <strong>A note for you:</strong> {_e(disability_note)}</div>")

    if cert_recommendations and user.get("email_pref_cert_recommendations", 1):
        rows = []
        for rec in cert_recommendations[:2]:
            courses = db.jloads(rec.get("courses"), []) if isinstance(rec.get("courses"), str) else (rec.get("courses") or [])
            links = "".join(
                f"<li><a href='{_e(c.get('url'))}'>{_e(c.get('name'))}</a> ({_e(c.get('price', 'Free'))}) — {_e(c.get('provider', ''))}</li>"
                for c in courses[:3])
            rows.append(f"<p>We've noticed <strong>{rec['gap_count']} jobs</strong> recently asked for "
                        f"<strong>{_e(rec['skill_name'])}</strong> — consider a certification:</p><ul>{links}</ul>")
        if rows:
            parts.append("<div style='border-top:2px solid #e5e7eb;margin-top:16px;padding-top:12px'>"
                         "<strong>📚 Skill Development Opportunity</strong>" + "".join(rows) + "</div>")

    if resume_advice and user.get("email_pref_resume_advice", 1):
        parts.append(
            f"<div style='background:#fefce8;border-radius:10px;padding:12px 14px;margin-top:12px'>"
            f"📝 <strong>Resume Tip:</strong> {_e(resume_advice)}</div>")

    if user.get("email_pref_progress_summaries", 1):
        parts.append(progress_summary_block(user))

    parts.append(
        f"<div style='margin-top:18px'>📊 <strong>Track your applications:</strong> "
        f"<a href='{config.APP_BASE_URL}/jobs'>view your full job history and pipeline</a>.</div>"
        f"<div style='margin-top:8px'>💡 <strong>Refine your results:</strong> want different jobs? "
        f"<a href='{config.APP_BASE_URL}/profile'>update your preferences, keywords or feedback notes</a> "
        f"and your next search will reflect the changes.</div>"
        f"<p style='margin-top:16px'>Best of luck!<br>— Your Job Search Assistant</p>")

    from . import emailer
    return subject, emailer.wrap_html("Your Top Job Matches", "".join(parts))


def progress_summary_block(user) -> str:
    week_ago = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
    stats = db.query_one(
        "SELECT SUM(applied) AS applied, SUM(heard_back) AS heard FROM job_history "
        "WHERE user_id = ? AND date_emailed >= ?",
        (user["id"], week_ago)) or {}
    lines = [f"• Jobs applied to: {stats.get('applied') or 0}",
             f"• Heard back: {stats.get('heard') or 0}"]
    for goal in goals.active_goals(user["id"])[:2]:
        lines.append(f"• Goal progress: {goal['current_progress']}/{goal['goal_target']} toward "
                     f"\"{goal['description']}\" — {goal['percentage']}%!")
    applied = stats.get("applied") or 0
    encouragement = ("Keep it up — your application rate is great!" if applied >= 3
                     else "Try applying to a couple of these — small steps add up!")
    lines.append(f"• {encouragement}")
    return ("<div style='border-top:2px solid #e5e7eb;margin-top:16px;padding-top:12px'>"
            "<strong>📊 Your Progress This Week:</strong><br>"
            + "<br>".join(_e(l) for l in lines) + "</div>")


def compose_urgency_email(user, job) -> tuple:
    subject = f"🔥 Urgent Match: {job['job_title']} at {job['company_name']}"
    intro = ("<p>We found a near-perfect match that looks urgent — we didn't want you to wait "
             "for your next regular email.</p>")
    from . import emailer
    body = emailer.wrap_html("🔥 Urgent Job Match", intro + job_block(1, job, user))
    return subject, body
