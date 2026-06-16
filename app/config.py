"""Central configuration. Loads .env (simple parser, no extra dependency)."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
RESUME_DIR = DATA_DIR / "resumes"
OUTBOX_DIR = DATA_DIR / "outbox"
DB_PATH = DATA_DIR / "app.db"


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv(BASE_DIR / ".env")

for _d in (DATA_DIR, RESUME_DIR, OUTBOX_DIR):
    _d.mkdir(parents=True, exist_ok=True)


def env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def env_bool(key: str, default: bool = True) -> bool:
    raw = env(key, "true" if default else "false").lower()
    return raw in ("1", "true", "yes", "on")


ADMIN_EMAIL = env("ADMIN_EMAIL", "ivandoublejr@gmail.com").lower()
ALERT_EMAIL = env("ALERT_EMAIL", ADMIN_EMAIL)

ANTHROPIC_API_KEY = env("ANTHROPIC_API_KEY")
LLM_MODEL = env("LLM_MODEL", "claude-opus-4-8")

EMAIL_MODE = env("EMAIL_MODE", "auto").lower()
# Resend (https://resend.com) — HTTP email API on port 443. Required on hosts
# like Railway/Render/Fly that block outbound SMTP ports. Preferred when set.
RESEND_API_KEY = env("RESEND_API_KEY")
# Sender for the HTTP API. Set just the bare address, e.g.
# EMAIL_FROM=login@jobfinderbyivan.com  (no display name, no angle brackets —
# those break some env-var editors). The display name (SMTP_FROM_NAME) is added
# automatically. You CAN still pass a full "Name <email>" value if you prefer.
# Defaults to Resend's onboarding domain, which works immediately but only
# delivers to the email you registered your Resend account with (great for the
# admin's first test; verify your own domain to send to everyone).
EMAIL_FROM = env("EMAIL_FROM", "onboarding@resend.dev")
SMTP_HOST = env("SMTP_HOST", "smtp.gmail.com")
SMTP_PORT = int(env("SMTP_PORT", "587") or 587)
SMTP_USER = env("SMTP_USER")
SMTP_PASSWORD = env("SMTP_PASSWORD")
SMTP_FROM_NAME = env("SMTP_FROM_NAME", "Job Search Assistant")
# Sender address; defaults to SMTP_USER (correct for Gmail). Set separately for
# relays like Brevo/SendGrid where the SMTP login is not an email address.
SMTP_FROM = env("SMTP_FROM") or SMTP_USER

ADZUNA_APP_ID = env("ADZUNA_APP_ID")
ADZUNA_APP_KEY = env("ADZUNA_APP_KEY")
ADZUNA_COUNTRY = env("ADZUNA_COUNTRY", "us")
JSEARCH_API_KEY = env("JSEARCH_API_KEY")
THEIRSTACK_API_KEY = env("THEIRSTACK_API_KEY")
USAJOBS_API_KEY = env("USAJOBS_API_KEY")
# USAJOBS requires the registered email address in the User-Agent header
USAJOBS_USER_AGENT = env("USAJOBS_USER_AGENT") or ADMIN_EMAIL

GOOGLE_MAPS_API_KEY = env("GOOGLE_MAPS_API_KEY")
# Search-engine dorking (tools/google_dork_ats.py). Serper.dev is the current
# recommended backend (free 2,500 queries); Google CSE is closed to new
# customers as of 2026 but still works for grandfathered accounts.
SERPER_API_KEY = env("SERPER_API_KEY")
GOOGLE_CSE_KEY = env("GOOGLE_CSE_KEY")
GOOGLE_CSE_ID = env("GOOGLE_CSE_ID")

HOST = env("HOST", "127.0.0.1")
PORT = int(env("PORT", "8000") or 8000)
APP_BASE_URL = env("APP_BASE_URL", f"http://{HOST}:{PORT}").rstrip("/")
SECRET_KEY = env("SECRET_KEY", "dev-secret-change-me")
# Mark session cookies Secure when served over HTTPS (production). Auto-detects
# from APP_BASE_URL; override with SECURE_COOKIES=true/false if needed.
SECURE_COOKIES = env_bool("SECURE_COOKIES", APP_BASE_URL.startswith("https://"))

# Wall-clock hours are interpreted in this timezone (the scheduler is pinned to
# it explicitly, so behaviour does not depend on the container's TZ — Railway
# runs in UTC otherwise, which would shift "11:00" to early morning Mountain).
SCHEDULER_TIMEZONE = env("SCHEDULER_TIMEZONE", "America/Denver")
# Daily board crawl populates the local job cache (job_cache.crawl_all_boards)
# before the main email cycle reads it.
BOARD_CRAWL_ENABLED = env_bool("BOARD_CRAWL_ENABLED", True)
BOARD_CRAWL_HOUR = int(env("BOARD_CRAWL_HOUR", "10") or 10)
# Open postings disappear from a board when filled; prune cache rows not seen in
# this many days (covers a board being briefly unreachable during a crawl).
CACHED_JOB_TTL_DAYS = int(env("CACHED_JOB_TTL_DAYS", "4") or 4)
AUTOMATION_HOUR = int(env("AUTOMATION_HOUR", "11") or 11)
SCHEDULER_ENABLED = env_bool("SCHEDULER_ENABLED", True)
# Listings older than this are discarded. Job APIs often index postings days
# after publication, so the spec's 3-day window is too strict in practice;
# dedup still guarantees no job is ever sent twice.
SEARCH_RECENCY_DAYS = int(env("SEARCH_RECENCY_DAYS", "7") or 7)

SESSION_DAYS = 7
OTP_TTL_MINUTES = 10
OTP_MAX_ATTEMPTS = 5
OTP_LOCKOUT_MINUTES = 30
MAX_RESUMES_PER_USER = 3
RESUME_TEXT_LIMIT = 8000  # chars sent to the LLM

# $/1M tokens (input, output) — used for cost estimation only.
MODEL_PRICING = {
    "claude-opus-4-8": (5.00, 25.00),
    "claude-opus-4-7": (5.00, 25.00),
    "claude-opus-4-6": (5.00, 25.00),
    "claude-sonnet-4-6": (3.00, 15.00),
    "claude-haiku-4-5": (1.00, 5.00),
}


def effective_email_mode() -> str:
    # Explicit outbox (dev) always wins — lets you force file output.
    if EMAIL_MODE == "outbox":
        return "outbox"
    # A configured Resend key takes precedence over everything else: SMTP is
    # blocked on most cloud hosts (Railway/Render/Fly), so a present
    # RESEND_API_KEY is a clear signal to use the HTTPS API — even if a stale
    # EMAIL_MODE=smtp is still set.
    if RESEND_API_KEY:
        return "resend"
    if EMAIL_MODE == "smtp":
        return "smtp" if (SMTP_USER and SMTP_PASSWORD) else "outbox"
    if EMAIL_MODE == "resend":
        return "outbox"  # asked for resend but no key
    # auto, no resend key:
    if SMTP_USER and SMTP_PASSWORD:
        return "smtp"
    return "outbox"


def search_provider_names() -> list:
    names = []
    if ADZUNA_APP_ID and ADZUNA_APP_KEY:
        names.append("adzuna")
    if JSEARCH_API_KEY:
        names.append("jsearch")
    if THEIRSTACK_API_KEY:
        names.append("theirstack")
    if USAJOBS_API_KEY:
        names.append("usajobs")
    if not names:
        names.append("demo")
    return names
