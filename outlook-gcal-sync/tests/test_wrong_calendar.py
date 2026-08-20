"""Guard against exporting the Google calendar and syncing it back to Google.

In a Mac Calendar sidebar the Google and Exchange accounts sit next to each
other, so picking the wrong one is easy -- and the result would duplicate the
whole calendar into itself.
"""
from __future__ import annotations

from pathlib import Path

from app.ics import looks_like_google_calendar, parse_calendar

FIXTURES = Path(__file__).parent / "fixtures"
GOOGLE = FIXTURES / "google-calendar-export.ics"
OUTLOOK = FIXTURES / "apple-calendar-export.ics"

ACCOUNT = "nicola.misani@gmail.com"


def test_calendar_identity_is_read_from_the_file():
    parsed = parse_calendar(GOOGLE.read_bytes())
    assert parsed.name == "Calendar"
    assert parsed.description == ACCOUNT
    assert "Apple Inc." in parsed.prodid


def test_an_export_of_the_connected_google_account_is_recognised():
    parsed = parse_calendar(GOOGLE.read_bytes())
    reason = looks_like_google_calendar(parsed, ACCOUNT)
    assert reason
    assert ACCOUNT in reason


def test_matching_is_case_insensitive():
    parsed = parse_calendar(GOOGLE.read_bytes())
    assert looks_like_google_calendar(parsed, "Nicola.Misani@Gmail.COM")


def test_a_genuine_outlook_export_is_accepted():
    parsed = parse_calendar(OUTLOOK.read_bytes())
    assert looks_like_google_calendar(parsed, ACCOUNT) == ""


def test_a_file_google_itself_produced_is_recognised_without_an_email():
    body = (
        b"BEGIN:VCALENDAR\nVERSION:2.0\n"
        b"PRODID:-//Google Inc//Google Calendar 70.9054//EN\n"
        b"END:VCALENDAR\n"
    )
    assert looks_like_google_calendar(parse_calendar(body), "") == (
        "it was produced by Google"
    )


def test_no_connected_account_means_no_false_positives():
    parsed = parse_calendar(OUTLOOK.read_bytes())
    assert looks_like_google_calendar(parsed, "") == ""
