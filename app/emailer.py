"""Email delivery.

Three modes (auto-selected; see config.effective_email_mode):
  resend - Resend HTTPS API (port 443). Use on hosts that block SMTP (Railway,
           Render, Fly). Set RESEND_API_KEY + EMAIL_FROM.
  smtp   - real delivery via Gmail SMTP (app password). Works locally; blocked
           on most cloud PaaS.
  outbox - writes each email as an .html file under data/outbox (dev mode);
           OTP codes and job emails can be read there without any account.

Every send is recorded in email_tracking with an open-tracking pixel token.
Every send is gated on the user's granular email preference (spec 7.2 / edge
case 29) — pass the matching pref column via `pref`; system-critical mail
(OTP) uses pref=None so login always works.
"""
import logging
import re
import secrets
import smtplib
import time
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import httpx

from . import config, db

log = logging.getLogger("emailer")

PREF_BY_TYPE = {
    "job_listing": "email_pref_job_listings",
    "urgency_alert": "email_pref_urgency_alerts",
    "resume_advice": "email_pref_resume_advice",
    "interview_prep": "email_pref_interview_prep",
    "cert_recommendation": "email_pref_cert_recommendations",
    "linkedin_tips": "email_pref_linkedin_tips",
    "goal_milestone": "email_pref_goal_milestones",
    "system": "email_pref_system_notifications",
    "otp": None,  # always delivered — required for login
}


def pref_allows(user: dict, email_type: str) -> bool:
    col = PREF_BY_TYPE.get(email_type, "email_pref_system_notifications")
    if col is None:
        return True
    return bool(user.get(col, 1))


def send(user: dict, email_type: str, subject: str, html_body: str,
         *, enforce_pref: bool = True, track: bool = True) -> bool:
    """Send an HTML email to `user`. Returns True if delivered (or written to
    outbox), False if suppressed or failed."""
    if enforce_pref and not pref_allows(user, email_type):
        db.log("email", user.get("id"), f"Suppressed '{email_type}' email (preference off)")
        return False

    token = secrets.token_urlsafe(16)
    if track and email_type != "otp":
        pixel = f'<img src="{config.APP_BASE_URL}/t/o/{token}.png" width="1" height="1" alt="" style="display:none">'
        if "</body>" in html_body:
            html_body = html_body.replace("</body>", pixel + "</body>")
        else:
            html_body += pixel

    mode = config.effective_email_mode()
    bounced = False
    ok = True
    if mode == "resend":
        ok, bounced = _send_resend(user["email"], subject, html_body)
    elif mode == "smtp":
        ok, bounced = _send_smtp(user["email"], subject, html_body)
    else:
        _write_outbox(user["email"], email_type, subject, html_body)

    if track:
        db.execute(
            "INSERT INTO email_tracking (user_id, email_type, subject, sent_at, bounced, "
            "open_tracking_pixel_url, tracking_token) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (user.get("id"), email_type, subject, db.now(), 1 if bounced else 0,
             f"{config.APP_BASE_URL}/t/o/{token}.png", token),
        )
    return ok


def _send_resend(to_addr: str, subject: str, html_body: str) -> tuple:
    """Send via the Resend HTTPS API (port 443 — works on hosts that block
    SMTP). Returns (ok, bounced)."""
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}",
                     "Content-Type": "application/json"},
            json={"from": config.EMAIL_FROM, "to": [to_addr],
                  "subject": subject, "html": html_body},
            timeout=20)
        if resp.status_code in (200, 201):
            return True, False
        log.warning("Resend send failed to %s: %s %s", to_addr, resp.status_code, resp.text[:200])
        # 422/403 typically = unverified domain or 'from' not allowed → treat as bounce
        return False, resp.status_code in (403, 422)
    except Exception:
        log.exception("Resend request failed to %s", to_addr)
        return False, False


def _send_smtp(to_addr: str, subject: str, html_body: str) -> tuple:
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"{config.SMTP_FROM_NAME} <{config.SMTP_FROM}>"
    msg["To"] = to_addr
    msg.attach(MIMEText(re.sub(r"<[^>]+>", " ", html_body), "plain", "utf-8"))
    msg.attach(MIMEText(html_body, "html", "utf-8"))
    try:
        with smtplib.SMTP(config.SMTP_HOST, config.SMTP_PORT, timeout=12) as server:
            server.starttls()
            server.login(config.SMTP_USER, config.SMTP_PASSWORD)
            refused = server.sendmail(config.SMTP_FROM, [to_addr], msg.as_string())
        return True, bool(refused)
    except smtplib.SMTPRecipientsRefused:
        log.warning("Recipient refused: %s", to_addr)
        return False, True
    except Exception:
        log.exception("SMTP send failed to %s", to_addr)
        return False, True


def _write_outbox(to_addr: str, email_type: str, subject: str, html_body: str) -> None:
    safe_subject = re.sub(r"[^A-Za-z0-9 _-]", "", subject)[:60].strip() or "email"
    fname = f"{time.strftime('%Y%m%d-%H%M%S')}_{email_type}_{to_addr.replace('@', '_at_')}_{safe_subject}.html"
    path = config.OUTBOX_DIR / fname
    header = (
        f"<!-- To: {to_addr} | Subject: {subject} | Type: {email_type} -->\n"
        f"<div style='background:#eef;border:1px solid #99c;padding:8px;font-family:monospace;font-size:12px'>"
        f"OUTBOX (dev mode) &mdash; To: {to_addr} &mdash; Subject: {subject}</div>\n"
    )
    path.write_text(header + html_body, encoding="utf-8")
    log.info("Outbox email written: %s", path.name)


# ---------------------------------------------------------------- layout ---

def wrap_html(title: str, inner: str) -> str:
    """Table-based layout that renders in mobile email clients."""
    return f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:0;background:#f3f4f6;font-family:Arial,Helvetica,sans-serif;">
<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f3f4f6;">
<tr><td align="center" style="padding:16px 8px;">
<table role="presentation" width="600" cellpadding="0" cellspacing="0"
       style="max-width:600px;width:100%;background:#ffffff;border-radius:12px;overflow:hidden;">
<tr><td style="background:#4f46e5;padding:20px 24px;">
  <span style="color:#ffffff;font-size:20px;font-weight:bold;">{title}</span>
</td></tr>
<tr><td style="padding:24px;color:#1f2937;font-size:16px;line-height:1.55;">
{inner}
</td></tr>
<tr><td style="padding:16px 24px;background:#f9fafb;color:#6b7280;font-size:12px;">
  Sent by your Job Search Assistant &middot; manage your email preferences from your
  <a href="{config.APP_BASE_URL}/profile" style="color:#4f46e5;">dashboard</a>.
</td></tr>
</table></td></tr></table></body></html>"""


def track_link(url: str, token: str = None) -> str:
    """Route a link through the click tracker."""
    import urllib.parse
    token = token or secrets.token_urlsafe(8)
    return f"{config.APP_BASE_URL}/t/c?u={urllib.parse.quote(url, safe='')}"
