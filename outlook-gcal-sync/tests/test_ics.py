from datetime import date, datetime, timezone

from app.ics import (
    EventTime,
    FeedError,
    normalize_tzid,
    parse_calendar,
)

SIMPLE = b"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Microsoft Corporation//Outlook 16.0 MIMEDIR//EN
X-WR-TIMEZONE:Europe/Rome
BEGIN:VEVENT
UID:single-1
SUMMARY:Dentist
LOCATION:Via Roma 1
DESCRIPTION:Bring the referral
DTSTART:20260310T090000Z
DTEND:20260310T093000Z
END:VEVENT
BEGIN:VEVENT
UID:allday-1
SUMMARY:Conference
DTSTART;VALUE=DATE:20260401
DTEND;VALUE=DATE:20260403
TRANSP:TRANSPARENT
END:VEVENT
BEGIN:VEVENT
UID:noend-1
SUMMARY:Reminder
DTSTART:20260311T140000Z
END:VEVENT
BEGIN:VEVENT
UID:duration-1
SUMMARY:Standup
DTSTART:20260312T080000Z
DURATION:PT15M
END:VEVENT
END:VCALENDAR
"""

RECURRING = b"""BEGIN:VCALENDAR
VERSION:2.0
BEGIN:VEVENT
UID:series-1
SUMMARY:Weekly sync
DTSTART;TZID=W. Europe Standard Time:20260105T100000
DTEND;TZID=W. Europe Standard Time:20260105T110000
RRULE:FREQ=WEEKLY;BYDAY=MO;UNTIL=20260601T080000Z
EXDATE;TZID=W. Europe Standard Time:20260202T100000
END:VEVENT
BEGIN:VEVENT
UID:series-1
RECURRENCE-ID;TZID=W. Europe Standard Time:20260209T100000
SUMMARY:Weekly sync (moved)
DTSTART;TZID=W. Europe Standard Time:20260209T140000
DTEND;TZID=W. Europe Standard Time:20260209T150000
END:VEVENT
END:VCALENDAR
"""


def events_by_uid(parsed):
    return {e.uid: e for e in parsed.events if not e.is_override}


def test_parses_basic_fields():
    parsed = parse_calendar(SIMPLE)
    assert parsed.default_tz == "Europe/Rome"
    event = events_by_uid(parsed)["single-1"]
    assert event.summary == "Dentist"
    assert event.location == "Via Roma 1"
    assert event.description == "Bring the referral"
    assert event.start.value == datetime(2026, 3, 10, 9, 0, tzinfo=timezone.utc)
    assert not event.is_recurring_master


def test_all_day_event_keeps_date_values():
    event = events_by_uid(parse_calendar(SIMPLE))["allday-1"]
    assert event.start.is_all_day
    assert event.start.value == date(2026, 4, 1)
    assert event.end.value == date(2026, 4, 3)
    assert event.transparent is True
    assert event.start.to_google() == {"date": "2026-04-01"}


def test_missing_dtend_falls_back_to_zero_length():
    event = events_by_uid(parse_calendar(SIMPLE))["noend-1"]
    assert event.end.value == event.start.value


def test_duration_is_applied_when_dtend_absent():
    event = events_by_uid(parse_calendar(SIMPLE))["duration-1"]
    assert event.end.value == datetime(2026, 3, 12, 8, 15, tzinfo=timezone.utc)


def test_recurrence_lines_are_passed_through():
    master = events_by_uid(parse_calendar(RECURRING))["series-1"]
    assert master.is_recurring_master
    rrules = [line for line in master.recurrence if line.startswith("RRULE")]
    assert rrules == ["RRULE:FREQ=WEEKLY;UNTIL=20260601T080000Z;BYDAY=MO"]


def test_exdate_is_rewritten_as_absolute_utc():
    master = events_by_uid(parse_calendar(RECURRING))["series-1"]
    exdates = [line for line in master.recurrence if line.startswith("EXDATE")]
    # 10:00 in Europe/Berlin during winter is 09:00 UTC.
    assert exdates == ["EXDATE:20260202T090000Z"]


def test_recurrence_override_is_separated_from_the_master():
    parsed = parse_calendar(RECURRING)
    overrides = [e for e in parsed.events if e.is_override]
    assert len(overrides) == 1
    assert overrides[0].summary == "Weekly sync (moved)"
    assert overrides[0].recurrence_id is not None


def test_windows_timezone_names_map_to_iana():
    assert normalize_tzid("W. Europe Standard Time") == "Europe/Berlin"
    assert normalize_tzid("Europe/Rome") == "Europe/Rome"
    assert normalize_tzid("Not A Zone") is None
    assert normalize_tzid(None) is None


def test_google_time_uses_iana_zone_when_resolvable():
    master = events_by_uid(parse_calendar(RECURRING))["series-1"]
    assert master.start.to_google() == {
        "dateTime": "2026-01-05T10:00:00+01:00",
        "timeZone": "Europe/Berlin",
    }


def test_google_time_falls_back_to_utc_for_unknown_zones():
    moment = datetime(2026, 5, 1, 12, 0, tzinfo=timezone.utc)
    assert EventTime(value=moment, tzid="Mars/Olympus").to_google() == {
        "dateTime": "2026-05-01T12:00:00Z",
        "timeZone": "UTC",
    }


def test_garbage_input_raises_feed_error():
    try:
        parse_calendar(b"this is not a calendar")
    except FeedError:
        return
    raise AssertionError("expected FeedError")
