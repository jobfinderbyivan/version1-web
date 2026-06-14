"""FastAPI application: pages, tracking endpoints, API routers, scheduler."""
import base64
import logging
import urllib.parse
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import admin_api, api, auth, config, db, scheduler

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger("main")

BASE = Path(__file__).resolve().parent.parent
templates = Jinja2Templates(directory=str(BASE / "templates"))


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    scheduler.start()
    log.info("Job Search Automation System ready at %s", config.APP_BASE_URL)
    log.info("Email mode: %s | Search providers: %s | LLM: %s",
             config.effective_email_mode(), config.search_provider_names(),
             "claude (" + config.LLM_MODEL + ")" if config.ANTHROPIC_API_KEY else "heuristic fallback (no key)")
    yield
    scheduler.shutdown()


app = FastAPI(title="Job Search Automation System", lifespan=lifespan)
app.include_router(api.router)
app.include_router(admin_api.router)
app.mount("/static", StaticFiles(directory=str(BASE / "static")), name="static")


def _page(request: Request, template: str, *, require_admin=False):
    user = auth.get_current_user(request)
    if user is None:
        return RedirectResponse("/login", status_code=302)
    if require_admin and not user["is_admin"]:
        return RedirectResponse("/profile", status_code=302)
    return templates.TemplateResponse(request, template, {
        "user": user, "dark": bool(user.get("dark_mode")),
        "is_admin": bool(user.get("is_admin")),
    })


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    user = auth.get_current_user(request)
    return RedirectResponse("/profile" if user else "/login", status_code=302)


@app.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if auth.get_current_user(request):
        return RedirectResponse("/profile", status_code=302)
    return templates.TemplateResponse(request, "login.html", {"dark": False})


@app.get("/profile", response_class=HTMLResponse)
def profile_page(request: Request):
    return _page(request, "profile.html")


@app.get("/jobs", response_class=HTMLResponse)
def jobs_page(request: Request):
    return _page(request, "jobs.html")


@app.get("/stories", response_class=HTMLResponse)
def stories_page(request: Request):
    return _page(request, "stories.html")


@app.get("/admin", response_class=HTMLResponse)
def admin_page(request: Request):
    return _page(request, "admin.html", require_admin=True)


# ------------------------------------------------------- email tracking ---

_PIXEL = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==")


@app.get("/t/o/{token}.png")
def open_pixel(token: str):
    db.execute("UPDATE email_tracking SET opened = 1 WHERE tracking_token = ?", (token,))
    return Response(content=_PIXEL, media_type="image/png",
                    headers={"Cache-Control": "no-store"})


@app.get("/t/c")
def click_redirect(u: str = "", t: str = ""):
    if t:
        db.execute("UPDATE email_tracking SET clicked = 1 WHERE tracking_token = ?", (t,))
    target = urllib.parse.unquote(u or "")
    if not target.startswith(("http://", "https://")):
        target = config.APP_BASE_URL
    return RedirectResponse(target, status_code=302)


@app.get("/health")
def health():
    return {"ok": True}
