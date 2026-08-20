"""Verify the real RefreshError -> NotConnected translation."""
import json, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
import pytest
from google.auth.exceptions import RefreshError
from app import crypto, google_client
from app.config import Settings
from app.db import Database


def test_refresh_error_becomes_a_readable_not_connected(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="ocsync-refresh-"))
    settings = Settings(app_password="", base_url="http://x", google_client_id="id",
                        google_client_secret="s", data_dir=tmp)
    db = Database(settings.db_path)
    blob = json.dumps({"token": "stale", "refresh_token": "r",
                       "token_uri": "https://oauth2.googleapis.com/token",
                       "client_id": "id", "client_secret": "s",
                       "scopes": google_client.SCOPES,
                       "expiry": (datetime.utcnow() - timedelta(hours=1)).isoformat()})
    db.set_setting("google_token", crypto.encrypt(blob, settings.key_path))

    def boom(self, request):
        raise RefreshError("invalid_grant: Token has been expired or revoked.")

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", boom)

    with pytest.raises(google_client.NotConnected) as excinfo:
        google_client.load_credentials(db, settings)
    assert "reconnect the account" in str(excinfo.value)
    assert "invalid_grant" not in str(excinfo.value)


def test_stored_expiry_is_restored_so_refresh_happens_proactively(monkeypatch):
    """Without this, creds.valid stays True forever and we never refresh."""
    tmp = Path(tempfile.mkdtemp(prefix="ocsync-expiry-"))
    settings = Settings(app_password="", base_url="http://x", google_client_id="id",
                        google_client_secret="s", data_dir=tmp)
    db = Database(settings.db_path)
    stale = datetime.utcnow() - timedelta(hours=1)
    blob = json.dumps({"token": "stale", "refresh_token": "r",
                       "token_uri": "https://oauth2.googleapis.com/token",
                       "client_id": "id", "client_secret": "s",
                       "scopes": google_client.SCOPES, "expiry": stale.isoformat()})
    db.set_setting("google_token", crypto.encrypt(blob, settings.key_path))

    refreshed = []

    def fake_refresh(self, request):
        refreshed.append(True)
        self.token = "fresh"
        self.expiry = datetime.utcnow() + timedelta(hours=1)

    monkeypatch.setattr("google.oauth2.credentials.Credentials.refresh", fake_refresh)

    creds = google_client.load_credentials(db, settings)
    assert refreshed, "expired credentials should have been refreshed"
    assert creds.token == "fresh"
