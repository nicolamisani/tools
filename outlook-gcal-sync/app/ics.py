"""Fetch and parse a published Outlook (or any RFC 5545) calendar feed.

Recurrence is deliberately *not* expanded here: RRULE/RDATE/EXDATE are handed
to Google as-is so a weekly stand-up stays one recurring event instead of 250
copies. Occurrences that Outlook moved or cancelled individually arrive as
separate VEVENTs carrying RECURRENCE-ID and are applied as instance patches by
the sync engine.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from icalendar import Calendar

MAX_FEED_BYTES = 25 * 1024 * 1024
USER_AGENT = "outlook-gcal-sync/1.0 (+https://github.com/nicolamisani/tools)"

# Exchange still emits Windows zone names in TZID. Google's API only accepts
# IANA names, so translate the ones that actually show up; anything unmapped
# falls back to absolute UTC timestamps.
WINDOWS_TZ = {
    "AUS Eastern Standard Time": "Australia/Sydney",
    "Arabian Standard Time": "Asia/Dubai",
    "Argentina Standard Time": "America/Argentina/Buenos_Aires",
    "Atlantic Standard Time": "America/Halifax",
    "Canada Central Standard Time": "America/Regina",
    "Cen. Australia Standard Time": "Australia/Adelaide",
    "Central America Standard Time": "America/Guatemala",
    "Central Brazilian Standard Time": "America/Cuiaba",
    "Central Europe Standard Time": "Europe/Budapest",
    "Central European Standard Time": "Europe/Warsaw",
    "Central Standard Time": "America/Chicago",
    "Central Standard Time (Mexico)": "America/Mexico_City",
    "China Standard Time": "Asia/Shanghai",
    "E. Africa Standard Time": "Africa/Nairobi",
    "E. Australia Standard Time": "Australia/Brisbane",
    "E. South America Standard Time": "America/Sao_Paulo",
    "Eastern Standard Time": "America/New_York",
    "Egypt Standard Time": "Africa/Cairo",
    "FLE Standard Time": "Europe/Kiev",
    "GMT Standard Time": "Europe/London",
    "GTB Standard Time": "Europe/Bucharest",
    "Greenwich Standard Time": "Atlantic/Reykjavik",
    "Hawaiian Standard Time": "Pacific/Honolulu",
    "India Standard Time": "Asia/Kolkata",
    "Iran Standard Time": "Asia/Tehran",
    "Israel Standard Time": "Asia/Jerusalem",
    "Japan Standard Time": "Asia/Tokyo",
    "Korea Standard Time": "Asia/Seoul",
    "Mountain Standard Time": "America/Denver",
    "Mountain Standard Time (Mexico)": "America/Chihuahua",
    "New Zealand Standard Time": "Pacific/Auckland",
    "Pacific SA Standard Time": "America/Santiago",
    "Pacific Standard Time": "America/Los_Angeles",
    "Romance Standard Time": "Europe/Paris",
    "Russian Standard Time": "Europe/Moscow",
    "SA Eastern Standard Time": "America/Cayenne",
    "SA Pacific Standard Time": "America/Bogota",
    "SA Western Standard Time": "America/La_Paz",
    "SE Asia Standard Time": "Asia/Bangkok",
    "Singapore Standard Time": "Asia/Singapore",
    "South Africa Standard Time": "Africa/Johannesburg",
    "Taipei Standard Time": "Asia/Taipei",
    "Tokyo Standard Time": "Asia/Tokyo",
    "Turkey Standard Time": "Europe/Istanbul",
    "US Eastern Standard Time": "America/Indiana/Indianapolis",
    "US Mountain Standard Time": "America/Phoenix",
    "UTC": "UTC",
    "W. Australia Standard Time": "Australia/Perth",
    "W. Central Africa Standard Time": "Africa/Lagos",
    "W. Europe Standard Time": "Europe/Berlin",
    "West Asia Standard Time": "Asia/Tashkent",
    "West Pacific Standard Time": "Pacific/Port_Moresby",
}


class FeedError(RuntimeError):
    """The feed could not be fetched or parsed."""


def normalize_tzid(tzid: str | None) -> str | None:
    """Return an IANA zone name for `tzid`, or None if it can't be resolved."""
    if not tzid:
        return None
    tzid = str(tzid).strip().strip('"')
    if not tzid:
        return None
    try:
        ZoneInfo(tzid)
        return tzid
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        pass
    mapped = WINDOWS_TZ.get(tzid)
    if mapped:
        return mapped
    # Exchange sometimes prefixes the zone, e.g. "tzone://Microsoft/Utc".
    if tzid.lower().endswith("/utc"):
        return "UTC"
    return None


@dataclass
class EventTime:
    """A DTSTART/DTEND/RECURRENCE-ID value plus the zone it was written in."""

    value: date | datetime
    tzid: str | None = None

    @property
    def is_all_day(self) -> bool:
        return not isinstance(self.value, datetime)

    def as_utc(self) -> datetime:
        """Absolute instant, assuming UTC for floating times."""
        if not isinstance(self.value, datetime):
            return datetime(
                self.value.year, self.value.month, self.value.day, tzinfo=timezone.utc
            )
        if self.value.tzinfo is None:
            return self.value.replace(tzinfo=timezone.utc)
        return self.value.astimezone(timezone.utc)

    def to_google(self, default_tz: str | None = None) -> dict[str, str]:
        if self.is_all_day:
            return {"date": self.value.isoformat()}

        zone = normalize_tzid(self.tzid)
        if zone is None and self.value.tzinfo is not None:
            zone = normalize_tzid(getattr(self.value.tzinfo, "key", None))
        if zone is None and self.value.tzinfo is None:
            zone = normalize_tzid(default_tz)

        if zone is not None and self.value.tzinfo is not None:
            return {
                "dateTime": self.value.isoformat(timespec="seconds").replace(
                    "+00:00", "Z"
                ),
                "timeZone": zone,
            }
        if zone is not None:  # floating time, pinned to the feed's zone
            return {
                "dateTime": self.value.replace(tzinfo=None).isoformat(
                    timespec="seconds"
                ),
                "timeZone": zone,
            }
        stamp = self.as_utc().isoformat(timespec="seconds").replace("+00:00", "Z")
        return {"dateTime": stamp, "timeZone": "UTC"}


@dataclass
class ParsedEvent:
    uid: str
    start: EventTime
    end: EventTime
    summary: str = ""
    description: str = ""
    location: str = ""
    status: str = ""
    transparent: bool = False
    private: bool = False
    recurrence: list[str] = field(default_factory=list)
    recurrence_id: EventTime | None = None
    organizer: str = ""

    @property
    def is_override(self) -> bool:
        return self.recurrence_id is not None

    @property
    def is_recurring_master(self) -> bool:
        return bool(self.recurrence) and self.recurrence_id is None

    @property
    def cancelled(self) -> bool:
        return self.status.upper() == "CANCELLED"


@dataclass
class ParsedCalendar:
    events: list[ParsedEvent]
    default_tz: str | None
    body_hash: str
    prodid: str = ""
    name: str = ""
    description: str = ""


def looks_like_google_calendar(
    parsed: "ParsedCalendar", account_email: str = ""
) -> str:
    """Say why a file appears to be an export of a Google calendar, or "".

    Round-tripping a Google calendar back into Google duplicates everything,
    and it is an easy mistake: in a Mac Calendar sidebar the Google and
    Exchange accounts sit side by side.
    """
    if "google" in parsed.prodid.lower():
        return "it was produced by Google"
    needle = account_email.strip().lower()
    if needle:
        if needle in (parsed.description or "").lower():
            return f"it is described as “{account_email}”"
        if needle in (parsed.name or "").lower():
            return f"it is named after “{account_email}”"
    return ""


def _text(comp, name: str) -> str:
    raw = comp.get(name)
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw).strip()


def _event_time(comp, name: str) -> EventTime | None:
    prop = comp.get(name)
    if prop is None:
        return None
    value = getattr(prop, "dt", None)
    if value is None:
        return None
    params = getattr(prop, "params", {}) or {}
    return EventTime(value=value, tzid=params.get("TZID"))


def _ical_stamp(value: date | datetime) -> str:
    if not isinstance(value, datetime):
        return value.strftime("%Y%m%d")
    if value.tzinfo is None:
        return value.strftime("%Y%m%dT%H%M%S")
    return value.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _recurrence_lines(comp) -> list[str]:
    """Rebuild RRULE/RDATE/EXDATE lines in a form the Google API accepts.

    Date-list properties are re-emitted as absolute UTC instants so an
    unresolvable TZID can never poison the rule.
    """
    lines: list[str] = []
    for name, prop in comp.property_items(recursive=False, sorted=False):
        name = name.upper()
        if name in ("RRULE", "EXRULE"):
            raw = prop.to_ical()
            lines.append(f"{name}:{raw.decode() if isinstance(raw, bytes) else raw}")
        elif name in ("RDATE", "EXDATE"):
            values = [d.dt for d in getattr(prop, "dts", [])]
            if not values:
                continue
            all_day = not isinstance(values[0], datetime)
            stamps = ",".join(_ical_stamp(v) for v in values)
            suffix = ";VALUE=DATE" if all_day else ""
            lines.append(f"{name}{suffix}:{stamps}")
    return lines


def _default_end(start: EventTime) -> EventTime:
    if start.is_all_day:
        return EventTime(value=start.value + timedelta(days=1))
    return EventTime(value=start.value, tzid=start.tzid)


def parse_calendar(body: bytes | str) -> ParsedCalendar:
    """Parse an iCalendar document into events we can map onto Google."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    body_hash = hashlib.sha256(body).hexdigest()
    try:
        cal = Calendar.from_ical(body)
    except Exception as exc:  # icalendar raises bare ValueError subclasses
        raise FeedError(f"could not parse the calendar feed: {exc}") from exc

    default_tz = normalize_tzid(_text(cal, "X-WR-TIMEZONE") or None)
    events: list[ParsedEvent] = []

    for comp in cal.walk("VEVENT"):
        uid = _text(comp, "UID")
        start = _event_time(comp, "DTSTART")
        if not uid or start is None:
            continue

        end = _event_time(comp, "DTEND")
        if end is None:
            duration = comp.get("DURATION")
            if duration is not None and getattr(duration, "dt", None) is not None:
                end = EventTime(value=start.value + duration.dt, tzid=start.tzid)
            else:
                end = _default_end(start)

        klass = _text(comp, "CLASS").upper()
        events.append(
            ParsedEvent(
                uid=uid,
                start=start,
                end=end,
                summary=_text(comp, "SUMMARY"),
                description=_text(comp, "DESCRIPTION"),
                location=_text(comp, "LOCATION"),
                status=_text(comp, "STATUS"),
                transparent=_text(comp, "TRANSP").upper() == "TRANSPARENT",
                private=klass in ("PRIVATE", "CONFIDENTIAL"),
                recurrence=_recurrence_lines(comp),
                recurrence_id=_event_time(comp, "RECURRENCE-ID"),
                organizer=_text(comp, "ORGANIZER").replace("MAILTO:", "").replace(
                    "mailto:", ""
                ),
            )
        )

    return ParsedCalendar(
        events=events,
        default_tz=default_tz,
        body_hash=body_hash,
        prodid=_text(cal, "PRODID"),
        name=_text(cal, "X-WR-CALNAME"),
        description=_text(cal, "X-WR-CALDESC"),
    )


@dataclass
class FetchResult:
    body: bytes | None            # None when the server answered 304
    etag: str
    last_modified: str
    not_modified: bool = False


def fetch_feed(url: str, etag: str = "", last_modified: str = "") -> FetchResult:
    """GET the feed, using conditional headers so unchanged feeds cost nothing."""
    headers = {"User-Agent": USER_AGENT, "Accept": "text/calendar, */*"}
    if etag:
        headers["If-None-Match"] = etag
    if last_modified:
        headers["If-Modified-Since"] = last_modified

    try:
        with httpx.Client(follow_redirects=True, timeout=60.0) as client:
            response = client.get(url, headers=headers)
    except httpx.HTTPError as exc:
        raise FeedError(f"could not reach the feed: {exc}") from exc

    if response.status_code == 304:
        return FetchResult(None, etag, last_modified, not_modified=True)
    if response.status_code >= 400:
        raise FeedError(
            f"feed returned HTTP {response.status_code}. "
            "Check that the published URL is still valid."
        )

    body = response.content
    if len(body) > MAX_FEED_BYTES:
        raise FeedError(f"feed is larger than {MAX_FEED_BYTES // 1024 // 1024} MB")
    if b"BEGIN:VCALENDAR" not in body[:4096]:
        raise FeedError(
            "the URL did not return an iCalendar document. Make sure you copied "
            "the ICS link from Outlook, not the HTML link."
        )

    return FetchResult(
        body=body,
        etag=response.headers.get("ETag", ""),
        last_modified=response.headers.get("Last-Modified", ""),
    )
