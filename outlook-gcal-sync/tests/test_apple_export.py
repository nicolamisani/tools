"""Apple Calendar is the practical export route on a locked-down Mac.

Its files carry VTIMEZONE blocks, VALARM subcomponents, X-APPLE-* properties,
folded lines and escaped text -- none of which may leak into Google.
"""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app import google_client, sync
from app.config import Settings
from app.db import Database
from app.ics import parse_calendar

from tests.test_sync_engine import CALENDAR_ID, FakeService

FIXTURE = Path(__file__).parent / "fixtures" / "apple-calendar-export.ics"


@pytest.fixture
def parsed():
    return parse_calendar(FIXTURE.read_bytes())


def by_summary(parsed, text):
    return next(e for e in parsed.events if e.summary == text)


def test_vtimezone_resolves_to_real_offsets(parsed):
    event = by_summary(parsed, "Kickoff with the vendor")
    assert event.start.to_google(parsed.default_tz) == {
        "dateTime": "2026-09-10T09:00:00+02:00",
        "timeZone": "Europe/Rome",
    }


def test_folded_lines_are_rejoined(parsed):
    """The Teams URL is split across two physical lines in the file."""
    event = by_summary(parsed, "Kickoff with the vendor")
    assert "https://teams.microsoft.com/l/meetup-join/abc123" in event.description


def test_escaped_text_is_unescaped(parsed):
    event = by_summary(parsed, "Kickoff with the vendor")
    assert event.location == "Sala Grande, Via Roma 1"


def test_apple_alarms_do_not_become_google_notifications(parsed):
    """VALARM is a subcomponent of VEVENT; it must not reach the event body."""
    event = by_summary(parsed, "Kickoff with the vendor")
    body = sync.build_event_body(
        event, {"id": 1, "privacy": "full", "reminders": 0}, parsed.default_tz
    )
    assert body["reminders"] == {"useDefault": False, "overrides": []}
    assert "ACTION" not in str(body)
    assert "X-APPLE" not in str(body)


def test_recurrence_and_its_exception_both_survive(parsed):
    series = by_summary(parsed, "Weekly staff meeting")
    assert series.is_recurring_master
    assert "RRULE:FREQ=WEEKLY;INTERVAL=1;BYDAY=MO" in series.recurrence
    assert "EXDATE:20261005T080000Z" in series.recurrence

    moved = by_summary(parsed, "Weekly staff meeting (moved)")
    assert moved.is_override
    assert moved.uid == series.uid


def test_all_day_span_keeps_date_values(parsed):
    event = by_summary(parsed, "Company offsite")
    assert event.start.to_google(parsed.default_tz) == {"date": "2026-10-12"}
    assert event.transparent is True


def test_the_whole_export_syncs_through_the_engine(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="ocsync-apple-"))
    settings = Settings(
        app_password="", base_url="http://localhost:8080", google_client_id="id",
        google_client_secret="s", data_dir=tmp,
    )
    db = Database(settings.db_path)
    service = FakeService()
    monkeypatch.setattr(google_client, "get_service", lambda *_: service)
    monkeypatch.setattr(google_client, "calendar_exists", lambda *_: True)

    source_id = db.create_source(
        name="Apple export", source_type="file", ics_url="",
        target_calendar_id=CALENDAR_ID, past_days=3650, future_days=3650,
    )
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.upload_path(source_id).write_bytes(FIXTURE.read_bytes())

    result = sync.sync_source(db, settings, db.get_source(source_id), force=True)

    assert result.status == "ok", result.message
    assert result.created == 3   # kickoff, the series, the offsite
    assert result.updated == 1   # the moved occurrence

    parsed = parse_calendar(FIXTURE.read_bytes())
    for event in parsed.events:
        if event.is_override:
            continue
        assert sync.google_event_id(source_id, event.uid) in service.store
