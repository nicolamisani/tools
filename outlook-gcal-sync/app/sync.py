"""One-way sync: published Outlook ICS feed -> a dedicated Google calendar.

The Google calendar is treated as owned by one source: every event this app
writes is tagged with `ocsync_source`, and on each run anything carrying that
tag which is no longer in the feed gets deleted. That makes the sync
self-healing -- if the local database is lost, a fresh run still converges.
"""
from __future__ import annotations

import base64
import hashlib
import json
import random
import sqlite3
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Iterable

from googleapiclient.errors import HttpError

from . import google_client, ics
from .config import Settings
from .db import Database
from .ics import FeedError, ParsedEvent

TAG_SOURCE = "ocsync_source"
TAG_UID = "ocsync_uid"
TAG_HASH = "ocsync_hash"
BUSY_TITLE = "Busy"
RETRY_STATUSES = {403, 429, 500, 502, 503, 504}


class SyncError(RuntimeError):
    pass


@dataclass
class RunResult:
    status: str = "ok"
    created: int = 0
    updated: int = 0
    deleted: int = 0
    message: str = ""
    log: list[str] = field(default_factory=list)

    def note(self, line: str) -> None:
        self.log.append(line)


def google_event_id(source_id: int, uid: str) -> str:
    """Deterministic, API-legal event id (base32hex alphabet, lowercase)."""
    digest = hashlib.sha1(f"{source_id}\x00{uid}".encode("utf-8")).digest()
    return base64.b32hexencode(digest).decode("ascii").rstrip("=").lower()


def _execute(request, attempts: int = 5):
    """Run a Google API request, backing off on rate limits and 5xx."""
    delay = 1.0
    for attempt in range(1, attempts + 1):
        try:
            return request.execute()
        except HttpError as exc:
            status = getattr(exc.resp, "status", 0)
            if status not in RETRY_STATUSES or attempt == attempts:
                raise
            time.sleep(delay + random.uniform(0, 0.4))
            delay = min(delay * 2, 30.0)


def _rrule_until(lines: Iterable[str]) -> datetime | None:
    for line in lines:
        if not line.upper().startswith("RRULE"):
            continue
        for part in line.split(":", 1)[-1].split(";"):
            key, _, value = part.partition("=")
            if key.strip().upper() != "UNTIL":
                continue
            raw = value.strip()
            for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S", "%Y%m%d"):
                try:
                    return datetime.strptime(raw, fmt).replace(tzinfo=timezone.utc)
                except ValueError:
                    continue
    return None


def in_window(event: ParsedEvent, start: datetime, end: datetime) -> bool:
    """Whether an event (or recurring series) overlaps the sync window."""
    if event.is_recurring_master:
        if event.start.as_utc() > end:
            return False
        until = _rrule_until(event.recurrence)
        return until is None or until >= start
    return event.end.as_utc() >= start and event.start.as_utc() <= end


def build_event_body(
    event: ParsedEvent,
    source: sqlite3.Row | dict[str, Any],
    default_tz: str | None,
    include_recurrence: bool = True,
) -> dict[str, Any]:
    """Map a parsed VEVENT onto a Google Calendar event resource."""
    privacy = str(source["privacy"] or "full")
    keep_details = privacy != "busy"

    body: dict[str, Any] = {
        "summary": (event.summary or "(no title)") if keep_details else BUSY_TITLE,
        "start": event.start.to_google(default_tz),
        "end": event.end.to_google(default_tz),
        "status": "confirmed",
        "transparency": "transparent" if event.transparent else "opaque",
        "visibility": "private" if (event.private or not keep_details) else "default",
        "reminders": (
            {"useDefault": True}
            if int(source["reminders"] or 0)
            else {"useDefault": False, "overrides": []}
        ),
    }

    if keep_details:
        if event.description:
            body["description"] = event.description
        if event.location:
            body["location"] = event.location

    if include_recurrence and event.recurrence:
        body["recurrence"] = list(event.recurrence)

    return body


def content_hash(body: dict[str, Any]) -> str:
    payload = {k: v for k, v in body.items() if k != "extendedProperties"}
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    ).hexdigest()[:32]


def _tagged(body: dict[str, Any], source_id: int, uid: str) -> dict[str, Any]:
    tagged = dict(body)
    tagged["extendedProperties"] = {
        "private": {
            TAG_SOURCE: str(source_id),
            TAG_UID: uid[:1000],
            TAG_HASH: content_hash(body),
        }
    }
    return tagged


def _list_managed_events(service, calendar_id: str, source_id: int) -> dict[str, dict]:
    """Every event on the calendar this source previously wrote, by event id."""
    existing: dict[str, dict] = {}
    page_token = None
    while True:
        result = _execute(
            service.events().list(
                calendarId=calendar_id,
                privateExtendedProperty=f"{TAG_SOURCE}={source_id}",
                singleEvents=False,
                showDeleted=False,
                maxResults=2500,
                pageToken=page_token,
            )
        )
        for item in result.get("items", []):
            existing[item["id"]] = item
        page_token = result.get("nextPageToken")
        if not page_token:
            break
    return existing


def _apply_override(
    service,
    calendar_id: str,
    master_id: str,
    override: ParsedEvent,
    source: sqlite3.Row,
    default_tz: str | None,
    result: RunResult,
) -> None:
    """Patch the single occurrence that Outlook moved, edited or cancelled."""
    assert override.recurrence_id is not None
    original = override.recurrence_id.to_google(default_tz)
    original_start = original.get("dateTime") or original.get("date")

    try:
        instances = _execute(
            service.events().instances(
                calendarId=calendar_id,
                eventId=master_id,
                originalStart=original_start,
                maxResults=5,
                showDeleted=True,
            )
        )
    except HttpError as exc:
        result.note(
            f"! could not look up occurrence {original_start} of "
            f"{override.summary or override.uid}: "
            f"{google_client.describe_http_error(exc)}"
        )
        return

    items = instances.get("items", [])
    if not items:
        result.note(
            f"! no occurrence at {original_start} for "
            f"{override.summary or override.uid}; skipped this exception"
        )
        return

    instance = items[0]
    if override.cancelled:
        if instance.get("status") == "cancelled":
            return
        _execute(
            service.events().patch(
                calendarId=calendar_id,
                eventId=instance["id"],
                body={"status": "cancelled"},
            )
        )
        result.deleted += 1
        result.note(f"- cancelled occurrence {original_start} of {override.summary}")
        return

    body = build_event_body(override, source, default_tz, include_recurrence=False)
    current_hash = (
        (instance.get("extendedProperties") or {}).get("private", {}).get(TAG_HASH)
    )
    new_hash = content_hash(body)
    if current_hash == new_hash and instance.get("status") != "cancelled":
        return

    body = _tagged(body, int(source["id"]), override.uid)
    _execute(
        service.events().patch(
            calendarId=calendar_id, eventId=instance["id"], body=body
        )
    )
    result.updated += 1
    result.note(f"~ occurrence {original_start} of {override.summary or override.uid}")


def sync_source(
    db: Database, settings: Settings, source: sqlite3.Row, force: bool = False
) -> RunResult:
    """Run one source end to end and record the outcome."""
    source_id = int(source["id"])
    run_id = db.start_run(source_id)
    result = RunResult()

    try:
        fetched = ics.fetch_feed(
            source["ics_url"],
            etag="" if force else source["http_etag"],
            last_modified="" if force else source["http_last_modified"],
        )
        if fetched.not_modified:
            result.status = "skipped"
            result.message = "Feed unchanged since the last run (HTTP 304)."
            db.finish_run(run_id, **_run_fields(result))
            return result

        parsed = ics.parse_calendar(fetched.body or b"")
        if not force and parsed.body_hash == source["content_hash"]:
            db.update_source(
                source_id,
                http_etag=fetched.etag,
                http_last_modified=fetched.last_modified,
            )
            result.status = "skipped"
            result.message = "Feed contents identical to the last run."
            db.finish_run(run_id, **_run_fields(result))
            return result

        service = google_client.get_service(db, settings)
        calendar_id = source["target_calendar_id"]
        if not calendar_id:
            raise SyncError("This source has no target Google calendar.")
        if not google_client.calendar_exists(service, calendar_id):
            raise SyncError(
                f"Target calendar {calendar_id} no longer exists or is not "
                "accessible. Pick a different calendar for this source."
            )

        now = datetime.now(timezone.utc)
        window_start = now - timedelta(days=int(source["past_days"]))
        window_end = now + timedelta(days=int(source["future_days"]))

        masters: dict[str, ParsedEvent] = {}
        overrides: list[ParsedEvent] = []
        skipped_cancelled = 0

        for event in parsed.events:
            if event.is_override:
                overrides.append(event)
                continue
            if event.cancelled:
                skipped_cancelled += 1
                continue
            if not in_window(event, window_start, window_end):
                continue
            # A feed can repeat a UID; last one wins, as Outlook intends.
            masters[event.uid] = event

        existing = _list_managed_events(service, calendar_id, source_id)
        desired_ids: set[str] = set()

        for uid, event in masters.items():
            event_id = google_event_id(source_id, uid)
            desired_ids.add(event_id)
            body = build_event_body(event, source, parsed.default_tz)
            new_hash = content_hash(body)
            current = existing.get(event_id)

            if current is not None:
                current_hash = (
                    (current.get("extendedProperties") or {})
                    .get("private", {})
                    .get(TAG_HASH)
                )
                if current_hash == new_hash:
                    continue
                _execute(
                    service.events().update(
                        calendarId=calendar_id,
                        eventId=event_id,
                        body=_tagged(body, source_id, uid),
                    )
                )
                result.updated += 1
                result.note(f"~ {event.summary or uid}")
                continue

            payload = _tagged(body, source_id, uid)
            payload["id"] = event_id
            try:
                _execute(service.events().insert(calendarId=calendar_id, body=payload))
                result.created += 1
                result.note(f"+ {event.summary or uid}")
            except HttpError as exc:
                if getattr(exc.resp, "status", 0) != 409:
                    result.note(
                        f"! failed to create {event.summary or uid}: "
                        f"{google_client.describe_http_error(exc)}"
                    )
                    raise
                # The id already exists, possibly cancelled: revive it in place.
                payload.pop("id", None)
                _execute(
                    service.events().update(
                        calendarId=calendar_id, eventId=event_id, body=payload
                    )
                )
                result.updated += 1
                result.note(f"~ {event.summary or uid}")

        for event_id, item in existing.items():
            if event_id in desired_ids:
                continue
            try:
                _execute(
                    service.events().delete(calendarId=calendar_id, eventId=event_id)
                )
                result.deleted += 1
                result.note(f"- {item.get('summary', event_id)}")
            except HttpError as exc:
                if getattr(exc.resp, "status", 0) not in (404, 410):
                    raise

        for override in overrides:
            master = masters.get(override.uid)
            if master is None or not master.is_recurring_master:
                continue
            _apply_override(
                service,
                calendar_id,
                google_event_id(source_id, override.uid),
                override,
                source,
                parsed.default_tz,
                result,
            )

        db.update_source(
            source_id,
            http_etag=fetched.etag,
            http_last_modified=fetched.last_modified,
            content_hash=parsed.body_hash,
        )

        parts = [
            f"{result.created} created",
            f"{result.updated} updated",
            f"{result.deleted} removed",
        ]
        if skipped_cancelled:
            parts.append(f"{skipped_cancelled} cancelled in Outlook")
        result.message = ", ".join(parts)

    except (FeedError, SyncError, google_client.NotConnected) as exc:
        result.status = "error"
        result.message = str(exc)
    except HttpError as exc:
        result.status = "error"
        result.message = google_client.describe_http_error(exc)
    except Exception as exc:  # pragma: no cover - surfaced in the run log
        result.status = "error"
        result.message = f"{type(exc).__name__}: {exc}"

    db.finish_run(run_id, **_run_fields(result))
    db.prune_runs()
    return result


def _run_fields(result: RunResult) -> dict[str, Any]:
    return {
        "status": result.status,
        "created": result.created,
        "updated": result.updated,
        "deleted": result.deleted,
        "message": result.message,
        "log": result.log[:500],
    }


def sync_all(db: Database, settings: Settings, force: bool = False) -> list[RunResult]:
    results = []
    for source in db.list_sources():
        if not int(source["enabled"]):
            continue
        results.append(sync_source(db, settings, source, force=force))
    return results
