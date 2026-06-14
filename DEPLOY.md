# Deploying to Railway at jobfinderbyivan.com

This app is a single always-on Python process using SQLite + on-disk files, with
an in-process scheduler. Railway runs it as one container with a **persistent
volume** for the data. Follow these steps once; after that, updates are a
`git push` away.

---

## 1. Put the code on GitHub (one time)

A repo is already initialized locally with a `.gitignore` that keeps your
secrets (`.env`), database, and uploaded resumes **out** of git.

1. Create a free account at https://github.com and a **new empty repository**
   named e.g. `jobfinder` (Private is fine).
2. Back in this project, connect and push (Claude can run these for you):
   ```
   git remote add origin https://github.com/<your-username>/jobfinder.git
   git branch -M main
   git push -u origin main
   ```
   GitHub will ask you to authenticate (a browser sign-in or a Personal Access
   Token). If you hit a wall, tell Claude and it'll walk you through it.

## 2. Create the Railway service

1. Sign up at https://railway.app (sign in with GitHub — easiest).
2. **New Project → Deploy from GitHub repo →** pick your `jobfinder` repo.
3. Railway detects the `Dockerfile` and starts building. Let the first build
   finish (it may error until env vars + the volume are set — that's expected).

## 3. Attach the persistent volume (CRITICAL)

Without this, your database and uploaded resumes are wiped on every redeploy.

1. Open your service → **Variables/Settings → Volumes → New Volume**.
2. Set the **Mount path** to exactly:
   ```
   /app/data
   ```
3. Save. (1 GB is plenty.)

## 4. Set environment variables

Service → **Variables → Raw Editor**, paste the block below, then fill in the
secret values from your local `.env` file (the lines marked `← from .env`):

```
ADMIN_EMAIL=ivandoublejr@gmail.com
ALERT_EMAIL=ivandoublejr@gmail.com
ANTHROPIC_API_KEY=          ← from .env
LLM_MODEL=claude-opus-4-8
EMAIL_MODE=smtp
SMTP_HOST=smtp.gmail.com
SMTP_PORT=587
SMTP_USER=jobfinderbyivan@gmail.com
SMTP_PASSWORD=              ← from .env (Gmail app password)
SMTP_FROM_NAME=Job Search Assistant
ADZUNA_APP_ID=              ← from .env
ADZUNA_APP_KEY=             ← from .env
ADZUNA_COUNTRY=us
JSEARCH_API_KEY=            ← from .env
THEIRSTACK_API_KEY=         ← from .env
USAJOBS_API_KEY=            ← from .env
USAJOBS_USER_AGENT=ivandoublejr@gmail.com
SERPER_API_KEY=             ← from .env
HOST=0.0.0.0
APP_BASE_URL=https://jobfinderbyivan.com
SECRET_KEY=Agvs5XRSqVteYLAm0v2pHcvQ82d1NI-OvttzX-kCpSfGqSf-c5f0lnynh73o3qHV
AUTOMATION_HOUR=8
SCHEDULER_ENABLED=true
```

> `SECRET_KEY` above was generated fresh for this deployment — keep it secret.
> Setting `APP_BASE_URL` to `https://…` automatically enables Secure cookies.

Railway redeploys after you save. Once it's green, your app is live at the
temporary `https://<something>.up.railway.app` URL — open it to confirm it
boots (you should reach the login page, and the cache seed loads on first start).

## 5. Point jobfinderbyivan.com at it (Spaceship DNS)

1. In Railway: service → **Settings → Networking → Custom Domain →** add
   `jobfinderbyivan.com`. Railway shows you a **DNS target** (a value like
   `abc123.up.railway.app`). Copy it. Add `www.jobfinderbyivan.com` too if you
   want www to work.
2. In **Spaceship** → your domain → **Advanced DNS / DNS manager**, add:

   | Type | Host / Name | Value | 
   |------|-------------|-------|
   | CNAME | `www` | the Railway target (`abc123.up.railway.app`) |
   | ALIAS or ANAME (if offered) | `@` | the Railway target |

   - If Spaceship offers **ALIAS/ANAME** for the root (`@`), use it with the
     Railway target — done.
   - If it only allows **A/CNAME** at the root, instead set up **domain
     forwarding / redirect** from `jobfinderbyivan.com` → `https://www.jobfinderbyivan.com`,
     and keep the `www` CNAME above. (Then set `APP_BASE_URL=https://www.jobfinderbyivan.com`.)
3. DNS + Railway's automatic HTTPS certificate take anywhere from a few minutes
   to a couple of hours to go live. Railway's domain page shows a green check
   when ready.

---

## Pushing updates later (this is the easy part)

Once GitHub ↔ Railway is connected, every change Claude makes is deployed with:
```
git add -A
git commit -m "describe the change"
git push
```
Railway auto-rebuilds and redeploys in ~1–2 minutes. No SSH, no server steps.
Your database, resumes, and cache on the volume are untouched by deploys.

## Refreshing the bundled cache (optional)

If you grow the employer cache locally and want the repo's seed to match
(so future fresh deploys inherit it):
```
python tools/export_cache_seed.py
git add seed/company_ats_cache.json && git commit -m "refresh cache seed" && git push
```

## Notes & gotchas

- **One worker on purpose.** The scheduler runs in-process; never scale to
  multiple replicas/workers or scheduled emails would double-send.
- **Gmail sending limits.** A regular Gmail account caps around ~500 emails/day —
  fine for the 100-user target. If you outgrow it, switch to a transactional
  provider (Brevo/SendGrid) by changing the `SMTP_*` vars.
- **Backups.** Your data lives on the Railway volume. Periodically download a
  copy of `data/app.db` (Railway shell or a small admin export) if the data
  matters to you.
- **Secrets rotation.** Because the Anthropic/Serper/Gmail keys passed through
  chat during setup, rotate any you're concerned about and update the Railway
  variable — no redeploy of code needed.
