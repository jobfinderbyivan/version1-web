# Job Search Automation System

A complete local build of the **Job Search Automation System** described in
`job_automation_master_prompt.md`: a multi-user web dashboard plus a scheduled
automation engine that searches for jobs matching each user's skills, ranks
them with AI, and emails the top 5 matches per cadence — with urgency alerts,
resume advice, market intelligence, interview prep, voice mock interviews,
goals, and anonymized success stories.

---

## Quick start

```powershell
# 1. (already done if Claude built this for you) install dependencies
python -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt

# 2. configure
copy .env.example .env     # then edit .env — see "Configuration" below

# 3. run
.venv\Scripts\python run.py
```

Open **http://127.0.0.1:8000** — you'll land on the login page.

### First login (important!)

There is **no sign-up page** (by design). The first admin account is created
automatically from `ADMIN_EMAIL` in `.env` (default `ivandoublejr@gmail.com`).

1. Enter the admin email on the login page and click **Send Code**.
2. **Where is my code?** If you haven't configured Gmail SMTP yet, the app
   runs in *outbox mode*: every email — including OTP codes — is written as an
   HTML file to **`data/outbox/`**. Open the newest `*_otp_*.html` file there
   to read your 6-digit code.
3. From **Admin → Resume Uploads**, drag in PDF/DOCX resumes. Each one creates
   a user account using the email found inside the resume. Those users can
   then log in the same way.

### Run the automation

The main cycle runs daily at `AUTOMATION_HOUR` (default 08:00) and processes
each user when their personal cadence (daily / every 3 days / weekly) is due.
You can also trigger it any time from **Admin → Analytics → "Run main cycle
now"**, then watch **Admin → Activity** for a step-by-step log.

### Smoke test

With the server running:

```powershell
.venv\Scripts\python tests\smoke.py
```

41 checks covering the spec's end-to-end verification flow (OTP auth, resume
→ user creation, preferences, automation, dedup, pipeline tracking, goals,
stories, mock interview, admin dashboards, brute-force lockout).

---

## Configuration (`.env`)

| Key | What it does |
|---|---|
| `ADMIN_EMAIL` | First admin account, created on startup. |
| `ANTHROPIC_API_KEY` | Enables all AI features (resume parsing, match scoring, advice, interview prep, mock interview grading, LinkedIn/portfolio analysis, company research). **Without it** the app still works using keyword heuristics — clearly lower quality, but the full pipeline is testable. |
| `LLM_MODEL` | Default `claude-opus-4-8`. |
| `SMTP_USER` / `SMTP_PASSWORD` | Gmail address + **App Password** (Google Account → Security → 2-Step Verification → App passwords). When set, real email is sent. When empty, emails go to `data/outbox/`. |
| `ADZUNA_APP_ID` / `ADZUNA_APP_KEY` | Real job listings via the free [Adzuna API](https://developer.adzuna.com/). |
| `JSEARCH_API_KEY` | Real listings via [JSearch on RapidAPI](https://rapidapi.com/letscrape-6bRBa3QguO5/api/jsearch) (aggregates Google for Jobs, often includes direct employer links). |
| `THEIRSTACK_API_KEY` | Real listings via [TheirStack](https://theirstack.com) — frequently provides direct company ATS application links. Note: TheirStack bills API credits per job returned. |
| `USAJOBS_API_KEY` / `USAJOBS_USER_AGENT` | US federal jobs via the official [USAJOBS API](https://developer.usajobs.gov) (free). The user-agent must be your registered email. Listings always include salaries and application deadlines. |
| `SEARCH_RECENCY_DAYS` | Discard listings older than this (default 7 — job APIs index postings a few days late; dedup still prevents repeats). |
| `GOOGLE_MAPS_API_KEY` | Real commute-time filtering (Distance Matrix, 8 AM arrival). Falls back to radius automatically. |
| `AUTOMATION_HOUR` | Hour of day the main cycle runs. |

**With no keys at all** the system runs fully self-contained: outbox email,
demo job listings (realistic fabricated data, deterministic per day), and
heuristic matching. Status banners on the profile/admin pages tell you which
mode you're in.

---

## What's implemented (spec section → feature)

- **§3 Database** — all 14 tables plus caches (company research, geocoding),
  settings, automation log/lock; dedup unique index incl. reopened positions.
- **§4 Auth & privacy** — email OTP login (10-min expiry, single-use),
  generic "if registered" messaging, 5-failures→30-min lockout, HTTP-only
  signed session cookies (7 days), strict per-user data isolation, GDPR
  account deletion (files + all rows; stories persist anonymized).
- **§5 Web app** — mobile-first Tailwind UI with bottom tab bar, dark mode
  persisted to DB, profile intelligence hub (strength gauge, 3 resumes with
  labels/primary/delete, LinkedIn + portfolio analyzers, skills-gap summary,
  course recommendations), every preference field from the spec (keywords,
  negative keywords, 20 industries, 6 job types, radius/commute modes,
  9 benefits, exclusion filters, blacklist/whitelist, cadence, urgency toggle,
  9 granular email preferences), goals with progress bars, job history with
  Leaflet map, match-score badges, company research cards, all 5 status
  checkboxes (offer/rejected mutually exclusive), not-interested feedback,
  interview details, filtering/sorting/pagination, success stories feed,
  admin dashboard (single + bulk upload with missing-email completion, user
  pause/delete, analytics, deliverability monitor with thresholds, token
  budget dashboard + editable budgets, story moderation, activity log,
  manual run buttons).
- **§6 Automation engine** — per-cadence scheduling, LLM query generation
  from transferable skills (not just titles), keyword/industry nudges,
  remote-only-if-opted-in (2–3 queries country-wide), recency filter,
  blacklist/negative-keyword/drug-test/criminal-record exclusions, contract
  opt-in rule with false-positive guard, not-interested deprioritisation
  (soft, −25%), reopened-position detection (>14 days), commute filter with
  radius fallback, company research with 30-day cache, LLM hire-likelihood
  (50%) + salary (30%) + competition (20%) + multi-hire/whitelist/urgency/
  benefits bonuses → 0–100 match score, minimum-salary hard cutoff (only when
  salary data exists), top-5-or-fewer (never padded, zero → no email),
  conservative disability detection from bio (additive searches only,
  respectful language, never the word "wheelchair"), deadline detection,
  market intelligence collection, persistent-skill-gap course recommendations,
  concurrency lock, per-user error isolation.
- **§7 Email** — table-based HTML for mobile clients, all spec sections
  (health score, commute, benefits matched, multi-hire/reopened/urgent/
  deadline badges, skills notes, research cards, certs, resume advice,
  goal progress + encouragement, dashboard links), granular preference
  enforcement on every send, open-tracking pixel + click-redirect tracking,
  bounce recording.
- **§8 Urgency alerts** — 12-hourly lightweight sweep, hire ≥8 + strong
  urgency signals, solo email, 1-per-24h rate limit, dedup, paused users
  excluded.
- **§9 Resume advice** — strength score on every upload, first-email rules
  (skip when performing well), upload-triggered advice, "Give Me Advice Now"
  full coaching review email.
- **§10 Market intelligence** — salary trends chart, skills-demand heatmap,
  STRONG/MODERATE/WEAK health with trend arrow, competitor analysis panel.
- **§11–13** — company research cards, on-demand interview prep (cached,
  emailed), LinkedIn analysis (graceful auth-wall fallback, monthly re-run),
  cert/course recommendations (free + paid links), goals with milestone
  emails, anonymized stories with moderation, weekly portfolio validator
  (status/SSL/response-time/content checks + alert emails), portfolio AI
  analysis with 5-dimension score.
- **§14 Mock interview** — per-job tailored questions, browser voice via Web
  Speech API (TTS questions + STT answers, push-to-talk) with automatic
  text-input fallback, AI evaluation (per-question scores, filler-word count,
  pacing, confidence, grade, top tips), sessions persisted, report emailed.
- **§15 Scheduling** — APScheduler: daily main cycle, 12-hourly urgency,
  weekly portfolio, monthly LinkedIn, daily token-budget check (alerts to
  `ALERT_EMAIL` incl. 3× runaway-user detection).

## Adaptations from the Abacus AI spec

The master prompt targets the Abacus AI agent platform. This build runs
locally on Windows, which required these substitutions:

| Spec | Local build | Why |
|---|---|---|
| PostgreSQL (platform DB) | **SQLite** (`data/app.db`), same schema | No local Postgres server; schema kept column-compatible. |
| React/Next.js frontend | **Jinja2 + Tailwind (CDN) + vanilla JS** | Installed Node is v16 (too old for modern Next.js); UI is still fully responsive, dark-mode, mobile-first per spec. |
| Abacus `web_search` scraping | **Adzuna / JSearch APIs**, demo provider fallback | No platform search tool locally; providers are pluggable in `app/job_search.py`. |
| Abacus LLM SDK | **Anthropic Claude API** (heuristic fallback without key) | |
| Gmail connector / OAuth | **Gmail SMTP app-password** or local outbox folder | |
| Google Calendar OAuth | **Downloadable `.ics` event** with 24h + 2h reminders | Works with Google/Apple/Outlook without an OAuth app registration. "Sync to Calendar" marks the job synced and downloads the file. |
| `linkedin_scraper_tool` | Best-effort public fetch; analysis falls back to resume + market data with a notice | LinkedIn blocks anonymous scraping. |
| Glassdoor salary lookups | Skipped when listing has no salary (**never fabricated**; neutral score per spec) | No search tool to query Glassdoor locally. |
| `/home/ubuntu/app_data/resumes/` | `data/resumes/` | Windows host. |
| Aggregate→company-site link cross-check (§6.3) | **Implemented via ATS board resolution** (`app/link_resolver.py`): same-job duplicates from different sources merge keeping the direct link; remaining board links are resolved against a cache of ~1,000+ employer job boards across 14 ATS platforms (Greenhouse, Lever, Ashby, SmartRecruiters, Workable, Recruitee, BambooHR, Workday, iCIMS, Taleo, Jobvite, SuccessFactors, UKG, Paradox), with fuzzy title matching and a budgeted JSearch fallback. | "Discard if filled on company site" is logged but not enforced — false "filled" verdicts are too risky. |

### Employer link cache — how it's built and kept fresh

The cache that powers direct-link resolution is populated by several tools under `tools/` and refreshed automatically:

- `tools/fill_utah_ats_cache.py` — gather employer names (Wikipedia + Claude + Adzuna) and slug-probe each.
- `tools/enumerate_ats_tenants.py` — enumerate real ATS tenants from the **Common Crawl** web index and keep boards with Utah postings. This is the breadth source.
- `tools/scrape_utah_directories.py` — deeper city/tier name harvesting.
- `tools/google_dork_ats.py` — resolve remaining companies by name via Serper.dev search (needs free `SERPER_API_KEY`); catches enterprise Workday/iCIMS tenants and follows vanity careers domains. Re-run anytime; it skips already-resolved companies.
- `tools/harvest_hiring_companies.py` — collect every employer **actively hiring in Utah** from the free job APIs (Adzuna + USAJOBS) and resolve each; also runs a careers-page pass over cached misses that have a domain. This is the "who's hiring right now" source.

**Careers-page ATS detection** (`link_resolver.resolve_from_website`): for a company with a known domain, fetch `/careers`, `/jobs`, the `careers.` subdomain and the homepage, and detect an ATS embedded or linked in the HTML (e.g. Entrata→Lever, Podium→Greenhouse). This is wired into the live pipeline (`discover_company` falls back to it when slug-probing fails) and the offline tools. It catches server-rendered embeds; fully JS-rendered SPA careers pages (where the ATS loads via JavaScript) can't be cracked by static fetch and fall back to the Serper name-search path.

**Automatic monthly refresh:** the scheduler (`app/ats_refresh.py`) checks weekly whether Common Crawl has shipped a new monthly index and, if so, runs the enumeration scan once for that index (tracked via the `last_cc_index` setting, so it never double-runs). Admins can also trigger it on demand from **Admin → Analytics → "Refresh employer link cache."** Every direct ATS link that flows through a normal search cycle also teaches the cache for free (`link_resolver.learn_from_url`).

## Project layout

```
run.py                  entry point
app/
  config.py             .env loading, constants, pricing
  db.py                 SQLite schema + helpers (all spec tables)
  auth.py               OTP, sessions, lockout
  llm.py                Claude wrapper + token accounting + JSON schemas
  emailer.py            SMTP/outbox, tracking, preference enforcement
  email_compose.py      job/urgency email HTML
  resume_parser.py      PDF/DOCX extraction, AI parse, strength score
  job_search.py         Adzuna / JSearch / demo providers + signal detection
  geo.py                Nominatim geocoding (cached), Google commute times
  research.py           company research cards (30-day cache)
  matching.py           hire likelihood + composite ranking
  market.py             salary trends, skills demand, health, competitors
  goals.py              goal periods, progress, milestone emails
  automation.py         main cycle, urgency, portfolio, LinkedIn, budget jobs
  advice.py             resume review, interview prep, mock interview, analyses
  scheduler.py          APScheduler wiring
  api.py / admin_api.py REST endpoints
  main.py               FastAPI app, pages, tracking pixel/click endpoints
templates/              login, profile, jobs, stories, admin (Tailwind)
tests/smoke.py          41-check end-to-end test
data/                   db, resume files, outbox emails (created at runtime)
```
