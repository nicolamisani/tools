from datetime import datetime, timedelta, timezone

from app.ics import parse_calendar
from app.sync import (
    BUSY_TITLE,
    build_event_body,
    content_hash,
    google_event_id,
    in_window,
)

FEED = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:meeting-1
SUMMARY:Budget review
DESCRIPTION:Numbers attached
LOCATION:Room 3
CLASS:PRIVATE
DTSTART:20260310T090000Z
DTEND:20260310T100000Z
END:VEVENT
BEGIN:VEVENT
UID:series-1
SUMMARY:Standup
DTSTART:20260105T080000Z
DTEND:20260105T081500Z
RRULE:FREQ=DAILY;COUNT=200
END:VEVENT
BEGIN:VEVENT
UID:old-series
SUMMARY:Retired ritual
DTSTART:20200105T080000Z
DTEND:20200105T081500Z
RRULE:FREQ=WEEKLY;UNTIL=20200601T080000Z
END:VEVENT
END:VCALENDAR
"""

SOURCE_FULL = {"id": 1, "privacy": "full", "reminders": 0}
SOURCE_BUSY = {"id": 1, "privacy": "busy", "reminders": 0}


def events():
    return {e.uid: e for e in parse_calendar(FEED).events}


def test_event_ids_are_stable_legal_and_source_scoped():
    first = google_event_id(1, "meeting-1")
    assert first == google_event_id(1, "meeting-1")
    assert first != google_event_id(2, "meeting-1")
    assert 5 <= len(first) <= 1024
    assert set(first) <= set("0123456789abcdefghijklmnopqrstuv")


def test_full_mode_carries_details_and_honours_class_private():
    body = build_event_body(events()["meeting-1"], SOURCE_FULL, None)
    assert body["summary"] == "Budget review"
    assert body["description"] == "Numbers attached"
    assert body["location"] == "Room 3"
    assert body["visibility"] == "private"
    assert body["reminders"] == {"useDefault": False, "overrides": []}


def test_busy_mode_strips_every_detail():
    body = build_event_body(events()["meeting-1"], SOURCE_BUSY, None)
    assert body["summary"] == BUSY_TITLE
    assert "description" not in body
    assert "location" not in body
    assert body["visibility"] == "private"
    assert body["start"] == {"dateTime": "2026-03-10T09:00:00Z", "timeZone": "UTC"}


def test_recurrence_is_included_only_when_requested():
    series = events()["series-1"]
    assert build_event_body(series, SOURCE_FULL, None)["recurrence"]
    assert "recurrence" not in build_event_body(
        series, SOURCE_FULL, None, include_recurrence=False
    )


def test_reminders_can_be_left_to_google():
    body = build_event_body(events()["meeting-1"], {**SOURCE_FULL, "reminders": 1}, None)
    assert body["reminders"] == {"useDefault": True}


def test_content_hash_tracks_content_but_ignores_our_tags():
    body = build_event_body(events()["meeting-1"], SOURCE_FULL, None)
    baseline = content_hash(body)
    assert content_hash({**body, "extendedProperties": {"private": {"x": "y"}}}) == baseline
    assert content_hash({**body, "summary": "Changed"}) != baseline


def test_window_filters_single_events_by_overlap():
    event = events()["meeting-1"]
    start = datetime(2026, 3, 1, tzinfo=timezone.utc)
    end = datetime(2026, 3, 31, tzinfo=timezone.utc)
    assert in_window(event, start, end)
    assert not in_window(event, start, datetime(2026, 3, 5, tzinfo=timezone.utc))
    assert not in_window(event, datetime(2026, 3, 20, tzinfo=timezone.utc), end)


def test_window_keeps_open_ended_series_and_drops_expired_ones():
    now = datetime(2026, 3, 1, tzinfo=timezone.utc)
    window = (now - timedelta(days=30), now + timedelta(days=365))
    assert in_window(events()["series-1"], *window)
    assert not in_window(events()["old-series"], *window)


def test_window_drops_series_starting_after_the_window():
    now = datetime(2020, 1, 1, tzinfo=timezone.utc)
    assert not in_window(
        events()["series-1"], now - timedelta(days=30), now + timedelta(days=30)
    )
