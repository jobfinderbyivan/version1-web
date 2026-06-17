"""SQLite database access and schema.

The spec targets PostgreSQL on the Abacus platform; locally we use SQLite with
the same table/column names so the application logic is portable. Booleans are
INTEGER 0/1, SERIAL becomes INTEGER PRIMARY KEY AUTOINCREMENT.
"""
import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime

from . import config

_local = threading.local()


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(str(config.DB_PATH), timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        _local.conn = conn
    return conn


@contextmanager
def tx():
    conn = get_conn()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise


def query(sql: str, params: tuple = ()) -> list:
    cur = get_conn().execute(sql, params)
    return [dict(r) for r in cur.fetchall()]


def query_one(sql: str, params: tuple = ()):
    cur = get_conn().execute(sql, params)
    row = cur.fetchone()
    return dict(row) if row else None


def execute(sql: str, params: tuple = ()) -> int:
    """Execute and commit; returns lastrowid."""
    with tx() as conn:
        cur = conn.execute(sql, params)
        return cur.lastrowid


def now() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def jloads(value, default):
    if value in (None, ""):
        return default
    try:
        out = json.loads(value)
        return out if out is not None else default
    except (ValueError, TypeError):
        # tolerate legacy comma-separated strings
        if isinstance(value, str) and default == []:
            return [s.strip() for s in value.split(",") if s.strip()]
        return default


def jdumps(value) -> str:
    return json.dumps(value, ensure_ascii=False)


SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    full_name TEXT,
    email TEXT NOT NULL UNIQUE,
    city TEXT,
    state TEXT,
    home_address TEXT,
    skills TEXT,
    experience_level TEXT,
    work_history TEXT,
    resume_file_path TEXT,
    resume_raw_text TEXT,
    bio TEXT,
    linkedin_url TEXT,
    portfolio_url TEXT,
    preferred_salary TEXT,
    minimum_salary TEXT,
    preferred_positions TEXT,
    additional_skills TEXT,
    feedback_notes TEXT,
    job_type_fulltime INTEGER DEFAULT 0,
    job_type_parttime INTEGER DEFAULT 0,
    job_type_contract INTEGER DEFAULT 0,
    job_type_remote INTEGER DEFAULT 0,
    job_type_hybrid INTEGER DEFAULT 0,
    job_type_onsite INTEGER DEFAULT 0,
    search_radius_miles INTEGER DEFAULT 15,
    commute_mode TEXT DEFAULT 'radius',
    commute_max_minutes INTEGER DEFAULT 30,
    preferred_benefits TEXT,
    exclude_drug_testing INTEGER DEFAULT 0,
    exclude_criminal_record_filter INTEGER DEFAULT 0,
    search_keywords TEXT,
    negative_keywords TEXT,
    industry_preferences TEXT,
    blacklisted_companies TEXT,
    whitelisted_companies TEXT,
    email_frequency TEXT DEFAULT 'every_3_days',
    urgency_alerts_enabled INTEGER DEFAULT 1,
    resume_advice_enabled INTEGER DEFAULT 0,
    resume_strength_score INTEGER,
    resume_strength_summary TEXT,
    resume_advice_pending INTEGER DEFAULT 1,
    first_email_sent INTEGER DEFAULT 0,
    last_email_sent_at TEXT,
    last_urgency_alert_at TEXT,
    last_login_at TEXT,
    is_admin INTEGER DEFAULT 0,
    is_paused INTEGER DEFAULT 0,
    dark_mode INTEGER DEFAULT 0,
    google_calendar_connected INTEGER DEFAULT 0,
    google_calendar_token TEXT,
    email_pref_job_listings INTEGER DEFAULT 1,
    email_pref_resume_advice INTEGER DEFAULT 1,
    email_pref_progress_summaries INTEGER DEFAULT 1,
    email_pref_urgency_alerts INTEGER DEFAULT 1,
    email_pref_interview_prep INTEGER DEFAULT 1,
    email_pref_cert_recommendations INTEGER DEFAULT 1,
    email_pref_linkedin_tips INTEGER DEFAULT 1,
    email_pref_goal_milestones INTEGER DEFAULT 1,
    email_pref_system_notifications INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_resumes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    label TEXT NOT NULL,
    file_path TEXT NOT NULL,
    raw_text TEXT,
    skills TEXT,
    experience_level TEXT,
    is_primary INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS otp_codes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    code TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    used INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS otp_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL,
    attempted_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS job_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    job_title TEXT NOT NULL,
    company_name TEXT NOT NULL,
    location TEXT,
    latitude REAL,
    longitude REAL,
    salary TEXT,
    salary_source TEXT,
    applicant_count TEXT,
    apply_link TEXT NOT NULL,
    aggregate_link TEXT,
    description TEXT,
    match_reason TEXT,
    match_score INTEGER,
    skills_gap TEXT,
    detected_benefits TEXT,
    preferred_qualifications TEXT,
    company_research TEXT,
    deadline_date TEXT,
    is_urgent INTEGER DEFAULT 0,
    commute_minutes INTEGER,
    commute_mode TEXT,
    date_emailed TEXT NOT NULL,
    applied INTEGER DEFAULT 0,
    heard_back INTEGER DEFAULT 0,
    interview_offered INTEGER DEFAULT 0,
    offer_received INTEGER DEFAULT 0,
    rejected INTEGER DEFAULT 0,
    interview_date TEXT,
    interview_time TEXT,
    interview_notes TEXT,
    interview_prep_requested INTEGER DEFAULT 0,
    interview_prep_data TEXT,
    calendar_event_synced INTEGER DEFAULT 0,
    not_interested INTEGER DEFAULT 0,
    not_interested_reason TEXT,
    is_multi_hire INTEGER DEFAULT 0,
    is_reopened INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE UNIQUE INDEX IF NOT EXISTS idx_job_dedup ON job_history
    (user_id, LOWER(company_name), LOWER(job_title), LOWER(COALESCE(location,'')), date_emailed);

CREATE TABLE IF NOT EXISTS salary_trends (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    role_category TEXT NOT NULL,
    location TEXT NOT NULL,
    avg_salary REAL,
    sample_count INTEGER,
    period_start TEXT NOT NULL,
    period_end TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS skills_demand (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    skill_name TEXT NOT NULL,
    mention_count INTEGER DEFAULT 0,
    total_jobs_analyzed INTEGER DEFAULT 0,
    percentage REAL,
    snapshot_date TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS market_health (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    health_score TEXT NOT NULL,
    jobs_per_week INTEGER,
    trend_direction TEXT,
    notes TEXT,
    computed_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS competitor_analysis (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    qualification TEXT NOT NULL,
    mention_count INTEGER DEFAULT 0,
    total_jobs_analyzed INTEGER DEFAULT 0,
    percentage REAL,
    snapshot_date TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS user_goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    goal_type TEXT NOT NULL,
    goal_target INTEGER NOT NULL,
    goal_period TEXT NOT NULL,
    current_progress INTEGER DEFAULT 0,
    period_start TEXT NOT NULL,
    is_active INTEGER DEFAULT 1,
    last_milestone INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS success_stories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE SET NULL,
    previous_role TEXT,
    new_role TEXT NOT NULL,
    new_salary TEXT,
    timeframe TEXT,
    story TEXT,
    is_approved INTEGER DEFAULT 1,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS email_tracking (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    email_type TEXT NOT NULL,
    subject TEXT,
    sent_at TEXT NOT NULL,
    bounced INTEGER DEFAULT 0,
    opened INTEGER DEFAULT 0,
    clicked INTEGER DEFAULT 0,
    open_tracking_pixel_url TEXT,
    tracking_token TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS token_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER,
    process_type TEXT NOT NULL,
    tokens_used INTEGER NOT NULL,
    estimated_cost REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS portfolio_checks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    url TEXT NOT NULL,
    status_code INTEGER,
    response_time_ms INTEGER,
    ssl_valid INTEGER,
    is_accessible INTEGER NOT NULL,
    issues TEXT,
    checked_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS portfolio_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    analysis_data TEXT,
    score INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS linkedin_analyses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    analysis_data TEXT,
    score INTEGER,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS mock_interview_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    job_history_id INTEGER REFERENCES job_history(id) ON DELETE CASCADE,
    questions_asked TEXT,
    user_responses TEXT,
    feedback TEXT,
    overall_score INTEGER,
    summary_report TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS cert_recommendations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER REFERENCES users(id) ON DELETE CASCADE,
    skill_name TEXT NOT NULL,
    gap_count INTEGER DEFAULT 0,
    courses TEXT,
    dismissed INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS company_research_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_name TEXT NOT NULL UNIQUE,
    data TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS company_ats_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    company_key TEXT NOT NULL UNIQUE,
    ats TEXT,
    slug TEXT,
    board_found INTEGER DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS geocode_cache (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    place TEXT NOT NULL UNIQUE,
    latitude REAL,
    longitude REAL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

-- Daily snapshot of every open posting found on the cached employer ATS
-- boards. Rebuilt by the board crawler (job_cache.crawl_all_boards) and read
-- as a first-class job source during per-user matching. fingerprint dedups a
-- posting across crawls; last_seen drives pruning of closed postings.
CREATE TABLE IF NOT EXISTS cached_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    fingerprint TEXT NOT NULL UNIQUE,
    company_key TEXT NOT NULL,
    company_name TEXT,
    ats TEXT,
    title TEXT NOT NULL,
    apply_link TEXT,
    location TEXT,
    city TEXT,
    state TEXT,
    is_remote INTEGER DEFAULT 0,
    salary TEXT,
    description TEXT,
    department TEXT,
    employment_type TEXT,
    posted_date TEXT,
    has_detail INTEGER DEFAULT 0,
    first_seen TEXT DEFAULT CURRENT_TIMESTAMP,
    last_seen TEXT DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX IF NOT EXISTS idx_cached_jobs_state ON cached_jobs(state);
CREATE INDEX IF NOT EXISTS idx_cached_jobs_remote ON cached_jobs(is_remote);
CREATE INDEX IF NOT EXISTS idx_cached_jobs_company ON cached_jobs(company_key);

-- Per-job detail cache keyed by the stable job URL. Platforms whose list feed
-- omits the description/location (Workday, SmartRecruiters, BambooHR, iCIMS)
-- need a per-job detail fetch; this remembers the result so a job is fetched
-- only ONCE — later crawls reuse it and skip the network call. Pruned by TTL so
-- closed postings fall out. (iCIMS especially: no server-side state filter, so
-- without this we'd re-scrape thousands of pages every day.)
CREATE TABLE IF NOT EXISTS ats_detail_cache (
    job_key TEXT PRIMARY KEY,
    ats TEXT,
    apply_link TEXT,
    location TEXT,
    city TEXT,
    state TEXT,
    is_remote INTEGER DEFAULT 0,
    salary TEXT,
    department TEXT,
    employment_type TEXT,
    description TEXT,
    posted_date TEXT,
    has_detail INTEGER DEFAULT 0,
    last_seen TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS app_settings (
    key TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS automation_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_type TEXT NOT NULL,
    user_id INTEGER,
    message TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS automation_lock (
    name TEXT PRIMARY KEY,
    locked_at TEXT
);
"""

DEFAULT_SETTINGS = {
    "token_daily_budget": "2000000",
    "token_monthly_budget": "40000000",
    "deliverability_min_open_rate": "10",
    "deliverability_max_bounce_rate": "5",
}


def init_db() -> None:
    conn = get_conn()
    conn.executescript(SCHEMA)
    _migrate(conn)
    for key, value in DEFAULT_SETTINGS.items():
        conn.execute("INSERT OR IGNORE INTO app_settings (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    _load_company_ats_seed(conn)
    _bootstrap_admin()


def _load_company_ats_seed(conn) -> None:
    """On a fresh database (e.g. first boot of a new deployment), pre-load the
    employer ATS-board cache from seed/company_ats_cache.json so the painstakingly
    built direct-link knowledge carries over without re-running the discovery
    tools. The seed holds only public company->ATS mappings, no personal data."""
    have = conn.execute("SELECT COUNT(*) FROM company_ats_cache").fetchone()[0]
    if have:
        return
    seed = config.BASE_DIR / "seed" / "company_ats_cache.json"
    if not seed.exists():
        return
    try:
        rows = json.loads(seed.read_text(encoding="utf-8"))
    except (ValueError, OSError):
        return
    for r in rows:
        conn.execute(
            "INSERT OR IGNORE INTO company_ats_cache "
            "(company_key, ats, slug, board_found, domain, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (r.get("company_key"), r.get("ats"), r.get("slug"),
             r.get("board_found", 0), r.get("domain"), r.get("created_at") or now()))
    conn.commit()
    log("system", None, f"Loaded {len(rows)} employer ATS-board mappings from seed")


def _migrate(conn) -> None:
    """Lightweight additive migrations for columns added after first release."""
    cols = {r["name"] for r in conn.execute("PRAGMA table_info(company_ats_cache)").fetchall()}
    if "domain" not in cols:
        conn.execute("ALTER TABLE company_ats_cache ADD COLUMN domain TEXT")
    _backfill_missing_locations(conn)


def _backfill_missing_locations(conn) -> None:
    """Earlier builds of the self-upload path didn't apply the resume's parsed
    location to the profile. Backfill city/state from stored resume text for any
    user missing a city — a cheap regex, no LLM, idempotent (won't re-touch once
    a city is set)."""
    import re as _re
    rows = conn.execute(
        "SELECT id, resume_raw_text FROM users "
        "WHERE (city IS NULL OR city = '') AND resume_raw_text IS NOT NULL "
        "AND resume_raw_text != ''").fetchall()
    for r in rows:
        m = _re.search(r"([A-Z][A-Za-z .'-]+),\s*([A-Z]{2})\b", r["resume_raw_text"] or "")
        if m:
            city, state = m.group(1).strip()[:255], m.group(2)
            conn.execute("UPDATE users SET city = ?, state = ?, "
                         "home_address = COALESCE(NULLIF(home_address,''), ?) WHERE id = ?",
                         (city, state, f"{city}, {state}", r["id"]))


def _bootstrap_admin() -> None:
    if not config.ADMIN_EMAIL:
        return
    existing = query_one("SELECT id, is_admin FROM users WHERE email = ?", (config.ADMIN_EMAIL,))
    if existing is None:
        execute(
            "INSERT INTO users (email, full_name, is_admin, resume_advice_pending) VALUES (?, ?, 1, 0)",
            (config.ADMIN_EMAIL, "Administrator"),
        )
        log("system", None, f"Created initial admin account {config.ADMIN_EMAIL}")
    elif not existing["is_admin"]:
        execute("UPDATE users SET is_admin = 1 WHERE id = ?", (existing["id"],))


def get_setting(key: str, default: str = "") -> str:
    row = query_one("SELECT value FROM app_settings WHERE key = ?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute(
        "INSERT INTO app_settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )


def log(run_type: str, user_id, message: str) -> None:
    execute(
        "INSERT INTO automation_log (run_type, user_id, message) VALUES (?, ?, ?)",
        (run_type, user_id, message),
    )


def acquire_lock(name: str, max_age_minutes: int = 120) -> bool:
    """Prevent concurrent automation runs (spec edge case 10)."""
    with tx() as conn:
        row = conn.execute("SELECT locked_at FROM automation_lock WHERE name = ?", (name,)).fetchone()
        if row:
            locked_at = datetime.strptime(row["locked_at"], "%Y-%m-%d %H:%M:%S")
            if (datetime.now() - locked_at).total_seconds() < max_age_minutes * 60:
                return False
            conn.execute("DELETE FROM automation_lock WHERE name = ?", (name,))
        conn.execute("INSERT INTO automation_lock (name, locked_at) VALUES (?, ?)", (name, now()))
        return True


def release_lock(name: str) -> None:
    execute("DELETE FROM automation_lock WHERE name = ?", (name,))
