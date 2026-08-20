"""FastAPI application: web UI, Google OAuth callback and manual sync triggers."""
from __future__ import annotations

import json
import logging
import secrets
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import BackgroundTasks, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from . import google_client, ics, sync
from .config import settings
from .db import Database
from .scheduler import SyncScheduler, sync_lock

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("outlook-gcal-sync")

BASE_DIR = Path(__file__).resolve().parent
db = Database(settings.db_path)
scheduler = SyncScheduler(db, settings)


@asynccontextmanager
async def lifespan(_: FastAPI):
    scheduler.start()
    if not settings.app_password:
        log.warning(
            "APP_PASSWORD is not set - the UI is unauthenticated. Only run it on "
            "a trusted network or behind your own proxy auth."
        )
    yield
    scheduler.shutdown()


def _session_secret() -> str:
    """Persist the cookie-signing key so logins survive a restart."""
    path = settings.data_dir / "session.key"
    if not path.exists():
        path.write_text(secrets.token_urlsafe(48))
        path.chmod(0o600)
    return path.read_text().strip()


app = FastAPI(title="Outlook -> Google Calendar sync", lifespan=lifespan)
app.add_middleware(
    SessionMiddleware,
    secret_key=_session_secret(),
    session_cookie="ocsync",
    same_site="lax",
    max_age=14 * 24 * 3600,
)
templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


# --- auth -------------------------------------------------------------------


def auth_required(request: Request) -> None:
    if not settings.app_password:
        return
    if not request.session.get("authenticated"):
        raise HTTPException(status_code=307, headers={"Location": "/login"})


@app.exception_handler(HTTPException)
async def redirect_handler(request: Request, exc: HTTPException):
    if exc.status_code == 307 and exc.headers and "Location" in exc.headers:
        return RedirectResponse(exc.headers["Location"], status_code=303)
    return JSONResponse({"detail": exc.detail}, status_code=exc.status_code)


@app.get("/login", response_class=HTMLResponse)
def login_form(request: Request):
    if not settings.app_password:
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(request, "login.html", {"error": None})


@app.post("/login")
def login(request: Request, password: str = Form("")):
    if secrets.compare_digest(password, settings.app_password):
        request.session["authenticated"] = True
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request, "login.html", {"error": "Incorrect password."}, status_code=401
    )


@app.post("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/login", status_code=303)


# --- helpers ----------------------------------------------------------------


def _flash(request: Request, message: str, level: str = "ok") -> None:
    request.session["flash"] = {"message": message, "level": level}


def _take_flash(request: Request) -> dict[str, str] | None:
    return request.session.pop("flash", None)


def _google_status() -> dict[str, Any]:
    if not settings.google_configured:
        return {"state": "unconfigured"}
    if not db.get_setting("google_token"):
        return {"state": "disconnected"}
    return {"state": "connected", "email": db.get_setting("google_email")}


def _calendars_or_empty() -> list[dict[str, Any]]:
    try:
        return google_client.list_calendars(google_client.get_service(db, settings))
    except Exception as exc:
        log.warning("could not list calendars: %s", exc)
        return []


def _source_view(row) -> dict[str, Any]:
    last = db.last_run_for_source(int(row["id"]))
    return {
        "row": row,
        "last_run": last,
    }


# --- dashboard --------------------------------------------------------------


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, _: None = Depends(auth_required)):
    sources = [_source_view(row) for row in db.list_sources()]
    calendar_names = {}
    if _google_status()["state"] == "connected" and sources:
        calendar_names = {
            cal["id"]: cal.get("summary", cal["id"]) for cal in _calendars_or_empty()
        }
    return templates.TemplateResponse(
        request,
        "index.html",
        {
            "google": _google_status(),
            "sources": sources,
            "calendar_names": calendar_names,
            "runs": db.recent_runs(15),
            "interval": db.get_setting("sync_interval_minutes", "60"),
            "next_run": scheduler.next_run(),
            "redirect_uri": settings.redirect_uri,
            "password_set": bool(settings.app_password),
            "flash": _take_flash(request),
            "now": datetime.now(timezone.utc),
        },
    )


@app.get("/healthz")
def healthz():
    return {"status": "ok", "sources": len(db.list_sources())}


# --- Google connection ------------------------------------------------------


@app.get("/oauth/google/start")
def google_start(request: Request, _: None = Depends(auth_required)):
    if not settings.google_configured:
        _flash(request, "Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET first.", "error")
        return RedirectResponse("/", status_code=303)
    url, state = google_client.authorization_url(settings)
    request.session["oauth_state"] = state
    return RedirectResponse(url, status_code=303)


@app.get("/oauth/google/callback")
def google_callback(request: Request, _: None = Depends(auth_required)):
    expected = request.session.pop("oauth_state", None)
    received = request.query_params.get("state")
    if not expected or expected != received:
        _flash(request, "OAuth state mismatch - please start the connection again.", "error")
        return RedirectResponse("/", status_code=303)
    if request.query_params.get("error"):
        _flash(request, f"Google returned: {request.query_params['error']}", "error")
        return RedirectResponse("/", status_code=303)

    try:
        flow = google_client.build_flow(settings, state=expected)
        flow.fetch_token(authorization_response=str(request.url))
        creds = flow.credentials
        if not creds.refresh_token:
            _flash(
                request,
                "Google did not return a refresh token. Remove this app at "
                "myaccount.google.com/permissions and connect again.",
                "error",
            )
            return RedirectResponse("/", status_code=303)
        google_client.store_credentials(db, settings, creds)
        db.set_setting("google_email", google_client.account_email(creds))
        _flash(request, "Google account connected.")
    except Exception as exc:
        log.exception("oauth callback failed")
        _flash(request, f"Could not complete the Google connection: {exc}", "error")
    return RedirectResponse("/", status_code=303)


@app.post("/google/disconnect")
def google_disconnect(request: Request, _: None = Depends(auth_required)):
    google_client.disconnect(db)
    _flash(request, "Google account disconnected. Synced events were left in place.")
    return RedirectResponse("/", status_code=303)


# --- sources ----------------------------------------------------------------


@app.get("/sources/new", response_class=HTMLResponse)
def new_source(request: Request, _: None = Depends(auth_required)):
    if _google_status()["state"] != "connected":
        _flash(request, "Connect your Google account before adding a source.", "error")
        return RedirectResponse("/", status_code=303)
    return templates.TemplateResponse(
        request,
        "source_form.html",
        {
            "source": None,
            "calendars": _calendars_or_empty(),
            "flash": _take_flash(request),
        },
    )


@app.get("/sources/{source_id}/edit", response_class=HTMLResponse)
def edit_source(request: Request, source_id: int, _: None = Depends(auth_required)):
    source = db.get_source(source_id)
    if source is None:
        raise HTTPException(404, "No such source")
    return templates.TemplateResponse(
        request,
        "source_form.html",
        {
            "source": source,
            "calendars": _calendars_or_empty(),
            "flash": _take_flash(request),
        },
    )


def _clean_source_form(
    name: str,
    ics_url: str,
    target_calendar_id: str,
    new_calendar_name: str,
    privacy: str,
    past_days: int,
    future_days: int,
    enabled: str | None,
    reminders: str | None,
) -> dict[str, Any]:
    ics_url = ics_url.strip()
    if ics_url.startswith("webcal://"):
        ics_url = "https://" + ics_url[len("webcal://") :]
    if not ics_url.startswith(("http://", "https://")):
        raise ValueError("The calendar URL must start with https:// or webcal://")
    if privacy not in ("full", "busy"):
        privacy = "full"

    if target_calendar_id == "__new__":
        summary = new_calendar_name.strip() or name.strip() or "Outlook"
        service = google_client.get_service(db, settings)
        target_calendar_id = google_client.create_calendar(service, summary)

    if not target_calendar_id:
        raise ValueError("Pick a target Google calendar.")

    return {
        "name": name.strip() or "Outlook calendar",
        "ics_url": ics_url,
        "target_calendar_id": target_calendar_id,
        "privacy": privacy,
        "past_days": max(0, min(int(past_days), 3650)),
        "future_days": max(1, min(int(future_days), 3650)),
        "enabled": 1 if enabled else 0,
        "reminders": 1 if reminders else 0,
    }


@app.post("/sources")
def create_source(
    request: Request,
    background: BackgroundTasks,
    name: str = Form(""),
    ics_url: str = Form(""),
    target_calendar_id: str = Form(""),
    new_calendar_name: str = Form(""),
    privacy: str = Form("full"),
    past_days: int = Form(30),
    future_days: int = Form(365),
    enabled: str | None = Form(None),
    reminders: str | None = Form(None),
    _: None = Depends(auth_required),
):
    try:
        fields = _clean_source_form(
            name, ics_url, target_calendar_id, new_calendar_name, privacy,
            past_days, future_days, enabled, reminders,
        )
    except Exception as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse("/sources/new", status_code=303)

    source_id = db.create_source(**fields)
    _flash(request, f"Added “{fields['name']}”. First sync is running now.")
    background.add_task(_run_sync, source_id, True)
    return RedirectResponse("/", status_code=303)


@app.post("/sources/{source_id}")
def update_source(
    request: Request,
    source_id: int,
    name: str = Form(""),
    ics_url: str = Form(""),
    target_calendar_id: str = Form(""),
    new_calendar_name: str = Form(""),
    privacy: str = Form("full"),
    past_days: int = Form(30),
    future_days: int = Form(365),
    enabled: str | None = Form(None),
    reminders: str | None = Form(None),
    _: None = Depends(auth_required),
):
    if db.get_source(source_id) is None:
        raise HTTPException(404, "No such source")
    try:
        fields = _clean_source_form(
            name, ics_url, target_calendar_id, new_calendar_name, privacy,
            past_days, future_days, enabled, reminders,
        )
    except Exception as exc:
        _flash(request, str(exc), "error")
        return RedirectResponse(f"/sources/{source_id}/edit", status_code=303)

    # Settings changed: force a full re-evaluation on the next run.
    fields.update(http_etag="", http_last_modified="", content_hash="")
    db.update_source(source_id, **fields)
    _flash(request, "Source updated.")
    return RedirectResponse("/", status_code=303)


@app.post("/sources/{source_id}/delete")
def delete_source(request: Request, source_id: int, _: None = Depends(auth_required)):
    source = db.get_source(source_id)
    if source is None:
        raise HTTPException(404, "No such source")
    db.delete_source(source_id)
    _flash(
        request,
        f"Removed “{source['name']}”. Events already copied to Google were "
        "left untouched — delete that calendar in Google if you want them gone.",
    )
    return RedirectResponse("/", status_code=303)


# --- running the sync -------------------------------------------------------


def _run_sync(source_id: int | None, force: bool) -> None:
    with sync_lock:
        if source_id is None:
            sync.sync_all(db, settings, force=force)
            return
        source = db.get_source(source_id)
        if source is not None:
            sync.sync_source(db, settings, source, force=force)


@app.post("/sources/{source_id}/sync")
def sync_one(
    request: Request,
    source_id: int,
    background: BackgroundTasks,
    _: None = Depends(auth_required),
):
    if db.get_source(source_id) is None:
        raise HTTPException(404, "No such source")
    background.add_task(_run_sync, source_id, True)
    _flash(request, "Sync started — refresh in a moment for the result.")
    return RedirectResponse("/", status_code=303)


@app.post("/sync")
def sync_now(
    request: Request, background: BackgroundTasks, _: None = Depends(auth_required)
):
    background.add_task(_run_sync, None, True)
    _flash(request, "Sync started for all enabled sources.")
    return RedirectResponse("/", status_code=303)


@app.post("/settings")
def update_settings(
    request: Request,
    sync_interval_minutes: int = Form(60),
    _: None = Depends(auth_required),
):
    minutes = max(5, min(int(sync_interval_minutes), 24 * 60))
    db.set_setting("sync_interval_minutes", str(minutes))
    scheduler.reschedule()
    _flash(request, f"Syncing every {minutes} minutes.")
    return RedirectResponse("/", status_code=303)


@app.get("/runs/{run_id}", response_class=HTMLResponse)
def run_detail(request: Request, run_id: int, _: None = Depends(auth_required)):
    run = db.get_run(run_id)
    if run is None:
        raise HTTPException(404, "No such run")
    try:
        entries = json.loads(run["log"])
    except json.JSONDecodeError:
        entries = []
    return templates.TemplateResponse(
        request, "run.html", {"run": run, "entries": entries}
    )


@app.post("/preview")
def preview_feed(request: Request, ics_url: str = Form(""), _: None = Depends(auth_required)):
    """Fetch a feed and report what we found, without writing to Google."""
    try:
        url = ics_url.strip()
        if url.startswith("webcal://"):
            url = "https://" + url[len("webcal://") :]
        fetched = ics.fetch_feed(url)
        parsed = ics.parse_calendar(fetched.body or b"")
        masters = [e for e in parsed.events if not e.is_override]
        recurring = [e for e in masters if e.is_recurring_master]
        return {
            "ok": True,
            "events": len(masters),
            "recurring": len(recurring),
            "exceptions": len(parsed.events) - len(masters),
            "timezone": parsed.default_tz or "not declared",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
