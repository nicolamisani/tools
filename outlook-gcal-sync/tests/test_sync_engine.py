"""End-to-end sync tests against an in-memory stand-in for the Calendar API."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app import google_client, ics, sync
from app.config import Settings
from app.db import Database
from app.ics import FetchResult

CALENDAR_ID = "cal-under-test"

FEED_TEMPLATE = """BEGIN:VCALENDAR
VERSION:2.0
X-WR-TIMEZONE:Europe/Rome
{events}END:VCALENDAR
"""

SINGLE = """BEGIN:VEVENT
UID:one-off
SUMMARY:{summary}
DTSTART:20260910T090000Z
DTEND:20260910T100000Z
END:VEVENT
"""

SERIES = """BEGIN:VEVENT
UID:series
SUMMARY:Weekly sync
DTSTART;TZID=W. Europe Standard Time:20260907T100000
DTEND;TZID=W. Europe Standard Time:20260907T110000
RRULE:FREQ=WEEKLY;BYDAY=MO
END:VEVENT
"""

OVERRIDE = """BEGIN:VEVENT
UID:series
RECURRENCE-ID;TZID=W. Europe Standard Time:20260914T100000
SUMMARY:Weekly sync (moved)
DTSTART;TZID=W. Europe Standard Time:20260914T140000
DTEND;TZID=W. Europe Standard Time:20260914T150000
END:VEVENT
"""


class Request:
    def __init__(self, result):
        self._result = result

    def execute(self):
        return self._result() if callable(self._result) else self._result


class FakeEvents:
    def __init__(self, store: dict, calls: list):
        self.store = store
        self.calls = calls

    def list(self, calendarId, privateExtendedProperty=None, **_):
        key, _, value = (privateExtendedProperty or "").partition("=")
        items = [
            dict(event)
            for event in self.store.values()
            if (event.get("extendedProperties") or {}).get("private", {}).get(key)
            == value
        ]
        return Request({"items": items})

    def insert(self, calendarId, body):
        def run():
            self.calls.append(("insert", body.get("id")))
            self.store[body["id"]] = dict(body)
            return dict(body)

        return Request(run)

    def update(self, calendarId, eventId, body):
        def run():
            self.calls.append(("update", eventId))
            stored = dict(body)
            stored["id"] = eventId
            self.store[eventId] = stored
            return stored

        return Request(run)

    def patch(self, calendarId, eventId, body):
        def run():
            self.calls.append(("patch", eventId))
            self.store.setdefault(eventId, {"id": eventId}).update(body)
            return self.store[eventId]

        return Request(run)

    def delete(self, calendarId, eventId):
        def run():
            self.calls.append(("delete", eventId))
            self.store.pop(eventId, None)
            return ""

        return Request(run)

    def instances(self, calendarId, eventId, originalStart=None, **_):
        # Stand in for Google expanding the series: hand back one occurrence
        # whose id is derived from the master and the requested start.
        instance_id = f"{eventId}_{originalStart}"
        master = self.store.get(eventId, {})
        return Request(
            {
                "items": [
                    {
                        "id": instance_id,
                        "status": "confirmed",
                        "summary": master.get("summary", ""),
                    }
                ]
            }
        )


class FakeService:
    def __init__(self):
        self.store: dict[str, dict] = {}
        self.calls: list[tuple[str, str]] = []

    def events(self):
        return FakeEvents(self.store, self.calls)


@pytest.fixture
def env(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="ocsync-engine-"))
    settings = Settings(
        app_password="",
        base_url="http://localhost:8080",
        google_client_id="id",
        google_client_secret="secret",
        data_dir=tmp,
    )
    db = Database(settings.db_path)
    service = FakeService()

    monkeypatch.setattr(google_client, "get_service", lambda *_: service)
    monkeypatch.setattr(google_client, "calendar_exists", lambda *_: True)

    source_id = db.create_source(
        name="Work",
        ics_url="https://example.invalid/calendar.ics",
        target_calendar_id=CALENDAR_ID,
        privacy="full",
        past_days=3650,
        future_days=3650,
    )
    return db, settings, service, source_id


def serve(monkeypatch, *event_blocks: str) -> None:
    body = FEED_TEMPLATE.format(events="".join(event_blocks)).encode()
    monkeypatch.setattr(
        ics, "fetch_feed", lambda *a, **k: FetchResult(body=body, etag="", last_modified="")
    )


def run(db, settings, source_id, force=True):
    return sync.sync_source(db, settings, db.get_source(source_id), force=force)


def test_first_run_creates_events_and_applies_the_exception(env, monkeypatch):
    db, settings, service, source_id = env
    serve(monkeypatch, SINGLE.format(summary="Kickoff"), SERIES, OVERRIDE)

    result = run(db, settings, source_id)

    assert result.status == "ok", result.message
    assert result.created == 2
    assert result.updated == 1  # the moved occurrence
    assert result.deleted == 0

    master_id = sync.google_event_id(source_id, "series")
    assert service.store[master_id]["recurrence"] == ["RRULE:FREQ=WEEKLY;BYDAY=MO"]
    assert any(kind == "patch" for kind, _ in service.calls)


def test_unchanged_feed_writes_nothing_on_a_forced_rerun(env, monkeypatch):
    db, settings, service, source_id = env
    serve(monkeypatch, SINGLE.format(summary="Kickoff"), SERIES)
    run(db, settings, source_id)
    service.calls.clear()

    result = run(db, settings, source_id)

    assert (result.created, result.updated, result.deleted) == (0, 0, 0)
    assert service.calls == []


def test_edited_event_is_updated_in_place(env, monkeypatch):
    db, settings, service, source_id = env
    serve(monkeypatch, SINGLE.format(summary="Kickoff"))
    run(db, settings, source_id)

    serve(monkeypatch, SINGLE.format(summary="Kickoff (rescheduled)"))
    result = run(db, settings, source_id)

    assert (result.created, result.updated, result.deleted) == (0, 1, 0)
    event_id = sync.google_event_id(source_id, "one-off")
    assert service.store[event_id]["summary"] == "Kickoff (rescheduled)"


def test_event_removed_from_the_feed_is_deleted_from_google(env, monkeypatch):
    db, settings, service, source_id = env
    serve(monkeypatch, SINGLE.format(summary="Kickoff"), SERIES)
    run(db, settings, source_id)

    serve(monkeypatch, SERIES)
    result = run(db, settings, source_id)

    assert (result.created, result.updated, result.deleted) == (0, 0, 1)
    assert sync.google_event_id(source_id, "one-off") not in service.store
    assert sync.google_event_id(source_id, "series") in service.store


def test_events_from_other_sources_are_never_touched(env, monkeypatch):
    db, settings, service, source_id = env
    service.store["foreign"] = {
        "id": "foreign",
        "summary": "A Google event that predates us",
        "extendedProperties": {"private": {"ocsync_source": "99"}},
    }
    service.store["untagged"] = {"id": "untagged", "summary": "Hand-made event"}

    serve(monkeypatch, SINGLE.format(summary="Kickoff"))
    run(db, settings, source_id)

    assert "foreign" in service.store
    assert "untagged" in service.store


def test_unreachable_feed_is_reported_as_a_failed_run(env, monkeypatch):
    db, settings, _, source_id = env

    def boom(*a, **k):
        raise ics.FeedError("feed returned HTTP 404. Check that the published URL is still valid.")

    monkeypatch.setattr(ics, "fetch_feed", boom)
    result = run(db, settings, source_id)

    assert result.status == "error"
    assert "404" in result.message
    assert db.last_run_for_source(source_id)["status"] == "error"


def test_identical_feed_is_skipped_without_calling_google(env, monkeypatch):
    db, settings, service, source_id = env
    serve(monkeypatch, SINGLE.format(summary="Kickoff"))
    run(db, settings, source_id)
    service.calls.clear()

    result = run(db, settings, source_id, force=False)

    assert result.status == "skipped"
    assert service.calls == []


def test_expired_google_signin_is_reported_in_plain_language(env, monkeypatch):
    """A revoked refresh token is the 7-day Testing-mode failure; say so."""
    db, settings, _, source_id = env

    def expired(*a, **k):
        raise google_client.NotConnected(
            "Google sign-in has expired or been revoked - reconnect the "
            "account from the dashboard. If your OAuth consent screen is "
            "still in Testing, Google does this every 7 days; publishing "
            "the app stops it."
        )

    serve(monkeypatch, SINGLE.format(summary="Kickoff"))
    monkeypatch.setattr(google_client, "get_service", expired)

    result = run(db, settings, source_id)

    assert result.status == "error"
    assert "reconnect the account" in result.message
    assert "RefreshError" not in result.message
