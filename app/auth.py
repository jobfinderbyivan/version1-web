"""OTP authentication, sessions, brute-force protection."""
import secrets
from datetime import datetime, timedelta

from fastapi import HTTPException, Request
from itsdangerous import BadSignature, URLSafeTimedSerializer

from . import config, db, emailer

_serializer = URLSafeTimedSerializer(config.SECRET_KEY, salt="session")

GENERIC_MESSAGE = "If this email is registered, a code has been sent."


def request_otp(email: str) -> None:
    """Generate and send an OTP. Never reveals whether the email exists."""
    email = (email or "").strip().lower()
    if not email:
        return
    user = db.query_one("SELECT * FROM users WHERE email = ?", (email,))
    if user is None:
        db.log("auth", None, f"OTP requested for unknown email (not revealed to client)")
        return
    code = f"{secrets.randbelow(1000000):06d}"
    expires = (datetime.now() + timedelta(minutes=config.OTP_TTL_MINUTES)).strftime("%Y-%m-%d %H:%M:%S")
    db.execute("INSERT INTO otp_codes (email, code, expires_at) VALUES (?, ?, ?)", (email, code, expires))
    body = emailer.wrap_html(
        "Your login code",
        f"<p>Your one-time login code is:</p>"
        f"<p style='font-size:34px;font-weight:bold;letter-spacing:8px;color:#4f46e5'>{code}</p>"
        f"<p>This code expires in {config.OTP_TTL_MINUTES} minutes. "
        f"If you didn't request it, you can ignore this email.</p>",
    )
    emailer.send(user, "otp", "Your login code", body, enforce_pref=False, track=True)


def _locked_out(email: str) -> bool:
    cutoff = (datetime.now() - timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    row = db.query_one(
        "SELECT COUNT(*) AS n, MAX(attempted_at) AS last FROM otp_attempts WHERE email = ? AND attempted_at > ?",
        (email, cutoff),
    )
    if row and row["n"] >= config.OTP_MAX_ATTEMPTS:
        last = datetime.strptime(row["last"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() - last < timedelta(minutes=config.OTP_LOCKOUT_MINUTES):
            return True
    return False


def verify_otp(email: str, code: str):
    """Returns the user dict on success, raises HTTPException otherwise."""
    email = (email or "").strip().lower()
    code = (code or "").strip()
    if _locked_out(email):
        raise HTTPException(429, "Too many attempts. Please try again in 30 minutes.")
    row = db.query_one(
        "SELECT * FROM otp_codes WHERE email = ? AND code = ? AND used = 0 "
        "AND expires_at > ? ORDER BY id DESC LIMIT 1",
        (email, code, db.now()),
    )
    if row is None:
        db.execute("INSERT INTO otp_attempts (email) VALUES (?)", (email,))
        raise HTTPException(401, "Invalid or expired code.")
    db.execute("UPDATE otp_codes SET used = 1 WHERE id = ?", (row["id"],))
    db.execute("DELETE FROM otp_attempts WHERE email = ?", (email,))
    # housekeeping: drop expired codes
    db.execute("DELETE FROM otp_codes WHERE expires_at < ?", (db.now(),))
    user = db.query_one("SELECT * FROM users WHERE email = ?", (email,))
    if user is None:
        raise HTTPException(401, "Invalid or expired code.")
    db.execute("UPDATE users SET last_login_at = ? WHERE id = ?", (db.now(), user["id"]))
    return user


def make_session_token(user_id: int) -> str:
    return _serializer.dumps({"uid": user_id})


def get_current_user(request: Request):
    """Resolve the logged-in user from the session cookie, or None."""
    token = request.cookies.get("session")
    if not token:
        return None
    try:
        data = _serializer.loads(token, max_age=config.SESSION_DAYS * 86400)
    except BadSignature:
        return None
    return db.query_one("SELECT * FROM users WHERE id = ?", (data.get("uid"),))


def require_user(request: Request) -> dict:
    user = get_current_user(request)
    if user is None:
        raise HTTPException(401, "Not authenticated")
    return user


def require_admin(request: Request) -> dict:
    user = require_user(request)
    if not user["is_admin"]:
        raise HTTPException(403, "Admin access required")
    return user
