"""Market intelligence collection & aggregation (spec sections 6.10 / 10)."""
import re
from datetime import date, timedelta

from . import db, matching


def record_cycle_data(user: dict, analyzed_jobs: list) -> None:
    """Called after each automation cycle with every job that was analyzed
    (whether or not it made the top 5)."""
    today = date.today().isoformat()
    uid = user["id"]
    total = len(analyzed_jobs)
    if total == 0:
        return

    # --- salary trends ------------------------------------------------
    role = (db.jloads(user.get("preferred_positions"), []) or [None])[0] if \
        isinstance(user.get("preferred_positions"), str) and user.get("preferred_positions", "").startswith("[") \
        else (user.get("preferred_positions") or "").split(",")[0].strip()
    role = role or "General"
    location = ", ".join(p for p in (user.get("city"), user.get("state")) if p) or "Unknown"
    salaries = [matching.parse_salary_number(j.get("salary")) for j in analyzed_jobs]
    salaries = [s for s in salaries if s]
    if salaries:
        db.execute(
            "INSERT INTO salary_trends (role_category, location, avg_salary, sample_count, "
            "period_start, period_end) VALUES (?, ?, ?, ?, ?, ?)",
            (role, location, round(sum(salaries) / len(salaries), 2), len(salaries),
             (date.today() - timedelta(days=3)).isoformat(), today),
        )

    # --- skills demand -------------------------------------------------
    user_skills = db.jloads(user.get("skills"), [])
    counts = {}
    for job in analyzed_jobs:
        text = f"{job.get('title', '')} {job.get('description', '')}".lower()
        seen = set()
        for skill in user_skills:
            s = str(skill).strip()
            if len(s) >= 2 and s.lower() in text and s.lower() not in seen:
                seen.add(s.lower())
                counts[s] = counts.get(s, 0) + 1
        for qual in job.get("preferred_qualifications") or []:
            q = str(qual).strip()
            if 2 <= len(q) <= 40 and q.lower() not in seen:
                seen.add(q.lower())
                counts[q] = counts.get(q, 0) + 1
    db.execute("DELETE FROM skills_demand WHERE user_id = ? AND snapshot_date = ?", (uid, today))
    for skill, n in sorted(counts.items(), key=lambda kv: -kv[1])[:20]:
        db.execute(
            "INSERT INTO skills_demand (user_id, skill_name, mention_count, total_jobs_analyzed, "
            "percentage, snapshot_date) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, skill, n, total, round(100.0 * n / total, 1), today),
        )

    # --- competitor analysis (preferred qualifications) -----------------
    qual_counts = {}
    for job in analyzed_jobs:
        for qual in set((job.get("preferred_qualifications") or [])):
            q = str(qual).strip().title()
            if 2 <= len(q) <= 40:
                qual_counts[q] = qual_counts.get(q, 0) + 1
    db.execute("DELETE FROM competitor_analysis WHERE user_id = ? AND snapshot_date = ?", (uid, today))
    for qual, n in sorted(qual_counts.items(), key=lambda kv: -kv[1])[:15]:
        db.execute(
            "INSERT INTO competitor_analysis (user_id, qualification, mention_count, "
            "total_jobs_analyzed, percentage, snapshot_date) VALUES (?, ?, ?, ?, ?, ?)",
            (uid, qual, n, total, round(100.0 * n / total, 1), today),
        )


def compute_market_health(user: dict, qualifying_count: int, cadence_days: int) -> dict:
    """STRONG >15/week, MODERATE 5-15, WEAK <5 (spec 10.3)."""
    per_week = round(qualifying_count * 7.0 / max(cadence_days, 1))
    score = "STRONG" if per_week > 15 else "MODERATE" if per_week >= 5 else "WEAK"
    prev = db.query_one(
        "SELECT jobs_per_week FROM market_health WHERE user_id = ? ORDER BY id DESC LIMIT 1",
        (user["id"],),
    )
    if prev is None or prev["jobs_per_week"] is None:
        trend = "STABLE"
    elif per_week > prev["jobs_per_week"] * 1.15:
        trend = "IMPROVING"
    elif per_week < prev["jobs_per_week"] * 0.85:
        trend = "DECLINING"
    else:
        trend = "STABLE"
    db.execute(
        "INSERT INTO market_health (user_id, health_score, jobs_per_week, trend_direction, notes) "
        "VALUES (?, ?, ?, ?, ?)",
        (user["id"], score, per_week, trend,
         f"{qualifying_count} qualifying jobs found this cycle ({cadence_days}-day cadence)."),
    )
    return {"health_score": score, "jobs_per_week": per_week, "trend_direction": trend}


def get_intelligence(user_id: int) -> dict:
    """Everything the dashboard intelligence section needs."""
    health = db.query_one(
        "SELECT * FROM market_health WHERE user_id = ? ORDER BY id DESC LIMIT 1", (user_id,))
    skills = db.query(
        "SELECT skill_name, percentage, mention_count FROM skills_demand WHERE user_id = ? "
        "AND snapshot_date = (SELECT MAX(snapshot_date) FROM skills_demand WHERE user_id = ?) "
        "ORDER BY percentage DESC LIMIT 12",
        (user_id, user_id))
    competitors = db.query(
        "SELECT qualification, percentage FROM competitor_analysis WHERE user_id = ? "
        "AND snapshot_date = (SELECT MAX(snapshot_date) FROM competitor_analysis WHERE user_id = ?) "
        "ORDER BY percentage DESC LIMIT 10",
        (user_id, user_id))
    user = db.query_one("SELECT city, state FROM users WHERE id = ?", (user_id,))
    location = ", ".join(p for p in ((user or {}).get("city"), (user or {}).get("state")) if p) or "Unknown"
    trends = db.query(
        "SELECT period_end, AVG(avg_salary) AS avg_salary, SUM(sample_count) AS samples "
        "FROM salary_trends WHERE location = ? GROUP BY period_end ORDER BY period_end DESC LIMIT 26",
        (location,))
    trends.reverse()
    trend_note = ""
    if len(trends) >= 2 and trends[0]["avg_salary"]:
        change = (trends[-1]["avg_salary"] - trends[0]["avg_salary"]) / trends[0]["avg_salary"] * 100
        direction = "increase" if change >= 0 else "decrease"
        trend_note = f"Salaries for matching roles in your area saw a {abs(change):.0f}% {direction} recently."
    return {
        "health": health,
        "skills_demand": skills,
        "competitor_analysis": competitors,
        "salary_trends": trends,
        "salary_trend_note": trend_note,
    }
