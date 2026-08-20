"""Google OAuth handling and a thin Calendar API wrapper."""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

# The consent screen always returns the `openid` scope alongside what we ask
# for; without this oauthlib treats that as a scope-change attack and raises.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")

from google.auth.transport.requests import Request  # noqa: E402
from google.oauth2.credentials import Credentials  # noqa: E402
from google_auth_oauthlib.flow import Flow  # noqa: E402
from googleapiclient.discovery import build  # noqa: E402
from googleapiclient.errors import HttpError  # noqa: E402

from . import crypto  # noqa: E402
from .config import Settings  # noqa: E402
from .db import Database  # noqa: E402

SCOPES = [
    "https://www.googleapis.com/auth/calendar",
    "https://www.googleapis.com/auth/userinfo.email",
    "openid",
]


class NotConnected(RuntimeError):
    """No usable Google credentials are stored."""


def _client_config(settings: Settings) -> dict[str, Any]:
    return {
        "web": {
            "client_id": settings.google_client_id,
            "client_secret": settings.google_client_secret,
            "auth_uri": "https://accounts.google.com/o/oauth2/auth",
            "token_uri": "https://oauth2.googleapis.com/token",
            "redirect_uris": [settings.redirect_uri],
        }
    }


def build_flow(settings: Settings, state: str | None = None) -> Flow:
    flow = Flow.from_client_config(_client_config(settings), scopes=SCOPES, state=state)
    flow.redirect_uri = settings.redirect_uri
    return flow


def authorization_url(settings: Settings) -> tuple[str, str]:
    flow = build_flow(settings)
    url, state = flow.authorization_url(
        access_type="offline",
        include_granted_scopes="true",
        prompt="consent",  # force a refresh token even on re-authorisation
    )
    return url, state


def credentials_to_json(creds: Credentials) -> str:
    return json.dumps(
        {
            "token": creds.token,
            "refresh_token": creds.refresh_token,
            "token_uri": creds.token_uri,
            "client_id": creds.client_id,
            "client_secret": creds.client_secret,
            "scopes": list(creds.scopes or SCOPES),
            "expiry": creds.expiry.isoformat() if creds.expiry else None,
        }
    )


def store_credentials(db: Database, settings: Settings, creds: Credentials) -> None:
    blob = crypto.encrypt(credentials_to_json(creds), settings.key_path)
    db.set_setting("google_token", blob)


def load_credentials(db: Database, settings: Settings) -> Credentials:
    blob = db.get_setting("google_token")
    if not blob:
        raise NotConnected("Google account is not connected yet.")
    try:
        data = json.loads(crypto.decrypt(blob, settings.key_path))
    except Exception as exc:
        raise NotConnected(
            "Stored Google credentials could not be decrypted. Reconnect the "
            "account (the encryption key in DATA_DIR may have been replaced)."
        ) from exc

    creds = Credentials(
        token=data.get("token"),
        refresh_token=data.get("refresh_token"),
        token_uri=data.get("token_uri"),
        client_id=data.get("client_id") or settings.google_client_id,
        client_secret=data.get("client_secret") or settings.google_client_secret,
        scopes=data.get("scopes") or SCOPES,
    )
    if not creds.valid:
        if not creds.refresh_token:
            raise NotConnected("Stored Google credentials have no refresh token.")
        creds.refresh(Request())
        store_credentials(db, settings, creds)
    return creds


def disconnect(db: Database) -> None:
    db.set_setting("google_token", "")
    db.set_setting("google_email", "")


def get_service(db: Database, settings: Settings):
    return build(
        "calendar", "v3", credentials=load_credentials(db, settings), cache_discovery=False
    )


def account_email(creds: Credentials) -> str:
    try:
        service = build("oauth2", "v2", credentials=creds, cache_discovery=False)
        return service.userinfo().get().execute().get("email", "")
    except Exception:
        return ""


def list_calendars(service) -> list[dict[str, Any]]:
    calendars: list[dict[str, Any]] = []
    page_token = None
    while True:
        result = service.calendarList().list(
            pageToken=page_token, maxResults=250, showHidden=True
        ).execute()
        calendars.extend(result.get("items", []))
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    calendars.sort(key=lambda c: (not c.get("primary", False), c.get("summary", "")))
    return calendars


def create_calendar(service, summary: str, time_zone: str | None = None) -> str:
    body: dict[str, Any] = {"summary": summary}
    if time_zone:
        body["timeZone"] = time_zone
    created = service.calendars().insert(body=body).execute()
    return created["id"]


def calendar_exists(service, calendar_id: str) -> bool:
    try:
        service.calendars().get(calendarId=calendar_id).execute()
        return True
    except HttpError as exc:
        if exc.resp.status in (404, 403):
            return False
        raise


def describe_http_error(exc: HttpError) -> str:
    try:
        payload = json.loads(exc.content.decode("utf-8"))
        message = payload.get("error", {}).get("message")
        if message:
            return f"HTTP {exc.resp.status}: {message}"
    except Exception:
        pass
    return f"HTTP {getattr(exc.resp, 'status', '?')}: {exc}"
