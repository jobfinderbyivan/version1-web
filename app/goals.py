"""Goal tracking helpers (spec 12.1).

Weekly goals reset Monday; monthly goals reset on the 1st. Progress is
incremented when the user checks status boxes and milestone emails fire at
100% (respecting email preferences).
"""
from datetime import date, timedelta

from . import config, db, emailer

GOAL_STATUS_FIELD = {"applications": "applied", "interviews": "interview_offered", "offers": "offer_received"}


def current_period_start(period: str) -> str:
    today = date.today()
    if period == "weekly":
        return (today - timedelta(days=today.weekday())).isoformat()
    return today.replace(day=1).isoformat()


def ensure_periods(user_id: int) -> None:
    """Roll goals into the new period when the old one has expired."""
    for goal in db.query("SELECT * FROM user_goals WHERE user_id = ? AND is_active = 1", (user_id,)):
        start = current_period_start(goal["goal_period"])
        if goal["period_start"] != start:
            db.execute(
                "UPDATE user_goals SET period_start = ?, current_progress = 0, last_milestone = 0, "
                "updated_at = ? WHERE id = ?",
                (start, db.now(), goal["id"]),
            )


def bump(user, goal_type: str, delta: int) -> None:
    """Adjust progress for all active goals of the given type."""
    ensure_periods(user["id"])
    for goal in db.query(
        "SELECT * FROM user_goals WHERE user_id = ? AND goal_type = ? AND is_active = 1",
        (user["id"], goal_type),
    ):
        progress = max(0, goal["current_progress"] + delta)
        db.execute(
            "UPDATE user_goals SET current_progress = ?, updated_at = ? WHERE id = ?",
            (progress, db.now(), goal["id"]),
        )
        if delta > 0 and progress >= goal["goal_target"] and goal["last_milestone"] < 100:
            db.execute("UPDATE user_goals SET last_milestone = 100 WHERE id = ?", (goal["id"],))
            _celebrate(user, goal, progress)


def _celebrate(user, goal, progress) -> None:
    label = describe(goal)
    body = emailer.wrap_html(
        "🎉 Goal achieved!",
        f"<p>You hit your goal! <strong>{label}</strong> &mdash; "
        f"{progress}/{goal['goal_target']} done.</p>"
        f"<p>Keep the momentum going. Your dashboard has the full picture: "
        f"<a href='{config.APP_BASE_URL}/jobs'>open dashboard</a>.</p>",
    )
    emailer.send(user, "goal_milestone", f"🎉 You hit your goal: {label}", body)


def describe(goal: dict) -> str:
    verb = {"applications": "Apply to", "interviews": "Get", "offers": "Receive"}.get(goal["goal_type"], "Reach")
    noun = {"applications": "jobs", "interviews": "interviews", "offers": "offers"}.get(goal["goal_type"], "")
    period = "this week" if goal["goal_period"] == "weekly" else "this month"
    return f"{verb} {goal['goal_target']} {noun} {period}"


def active_goals(user_id: int) -> list:
    ensure_periods(user_id)
    out = []
    for goal in db.query(
        "SELECT * FROM user_goals WHERE user_id = ? AND is_active = 1 ORDER BY id DESC", (user_id,)
    ):
        goal["description"] = describe(goal)
        goal["percentage"] = min(100, round(100.0 * goal["current_progress"] / max(goal["goal_target"], 1)))
        if goal["goal_period"] == "weekly":
            end = date.fromisoformat(goal["period_start"]) + timedelta(days=7)
        else:
            start = date.fromisoformat(goal["period_start"])
            end = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        goal["days_remaining"] = max(0, (end - date.today()).days)
        out.append(goal)
    return out
