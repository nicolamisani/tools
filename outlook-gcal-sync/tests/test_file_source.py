"""Uploaded-file sources, and the guard against truncated exports."""
from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from app import google_client, sync
from app.config import Settings
from app.db import Database

from tests.test_sync_engine import CALENDAR_ID, FakeService

HEADER = "BEGIN:VCALENDAR\nVERSION:2.0\n"
FOOTER = "END:VCALENDAR\n"


def event(uid: str, day: int) -> str:
    return (
        f"BEGIN:VEVENT\nUID:{uid}\nSUMMARY:Event {uid}\n"
        f"DTSTART:202609{day:02d}T090000Z\nDTEND:202609{day:02d}T100000Z\n"
        "END:VEVENT\n"
    )


def calendar(*uids: str) -> bytes:
    body = "".join(event(uid, i + 1) for i, uid in enumerate(uids))
    return (HEADER + body + FOOTER).encode()


@pytest.fixture
def env(monkeypatch):
    tmp = Path(tempfile.mkdtemp(prefix="ocsync-file-"))
    settings = Settings(
        app_password="", base_url="http://localhost:8080", google_client_id="id",
        google_client_secret="s", data_dir=tmp,
    )
    db = Database(settings.db_path)
    service = FakeService()
    monkeypatch.setattr(google_client, "get_service", lambda *_: service)
    monkeypatch.setattr(google_client, "calendar_exists", lambda *_: True)

    source_id = db.create_source(
        name="Exported", source_type="file", ics_url="",
        target_calendar_id=CALENDAR_ID, push_token="ocs_test",
        past_days=3650, future_days=3650,
    )
    return db, settings, service, source_id


def upload(settings, source_id: int, body: bytes) -> None:
    settings.upload_dir.mkdir(parents=True, exist_ok=True)
    settings.upload_path(source_id).write_bytes(body)


def run(db, settings, source_id, force=True, confirm=False):
    return sync.sync_source(
        db, settings, db.get_source(source_id), force=force, confirm_deletions=confirm
    )


def test_a_source_with_no_upload_waits_instead_of_erroring(env):
    db, settings, service, source_id = env

    result = run(db, settings, source_id)

    assert result.status == "skipped"
    assert "No calendar file" in result.message
    assert service.calls == []


def test_uploaded_file_syncs_like_any_other_calendar(env):
    db, settings, service, source_id = env
    upload(settings, source_id, calendar("a", "b", "c"))

    result = run(db, settings, source_id)

    assert result.status == "ok", result.message
    assert result.created == 3
    assert len(service.store) == 3


def test_re_uploading_the_same_file_changes_nothing(env):
    db, settings, service, source_id = env
    upload(settings, source_id, calendar("a", "b", "c"))
    run(db, settings, source_id)
    service.calls.clear()

    result = run(db, settings, source_id, force=False)

    assert result.status == "skipped"
    assert service.calls == []


def test_small_shrink_is_applied_normally(env):
    db, settings, service, source_id = env
    upload(settings, source_id, calendar("a", "b", "c", "d", "e", "f", "g", "h"))
    run(db, settings, source_id)

    upload(settings, source_id, calendar("a", "b", "c", "d", "e", "f", "g"))
    result = run(db, settings, source_id)

    assert result.status == "ok"
    assert result.deleted == 1
    assert len(service.store) == 7


def test_truncated_export_holds_deletions_back(env):
    db, settings, service, source_id = env
    upload(settings, source_id, calendar("a", "b", "c", "d", "e", "f", "g", "h"))
    run(db, settings, source_id)

    # A partial export: only two of the eight events survived.
    upload(settings, source_id, calendar("a", "b"))
    result = run(db, settings, source_id)

    assert result.status == "blocked"
    assert result.deleted == 0
    assert len(service.store) == 8, "nothing may be removed without confirmation"
    assert "Held back 6 deletions" in result.message
    assert "75%" in result.message


def test_a_held_back_run_still_applies_additions(env):
    db, settings, service, source_id = env
    upload(settings, source_id, calendar("a", "b", "c", "d", "e", "f", "g", "h"))
    run(db, settings, source_id)

    upload(settings, source_id, calendar("a", "new-one"))
    result = run(db, settings, source_id)

    assert result.status == "blocked"
    assert result.created == 1
    assert sync.google_event_id(source_id, "new-one") in service.store


def test_confirming_applies_the_held_back_deletions(env):
    db, settings, service, source_id = env
    upload(settings, source_id, calendar("a", "b", "c", "d", "e", "f", "g", "h"))
    run(db, settings, source_id)
    upload(settings, source_id, calendar("a", "b"))
    run(db, settings, source_id)

    result = run(db, settings, source_id, confirm=True)

    assert result.status == "ok"
    assert result.deleted == 6
    assert len(service.store) == 2


def test_a_blocked_run_does_not_cache_its_hash(env):
    """Otherwise the next unforced run would skip and the block would vanish."""
    db, settings, _, source_id = env
    upload(settings, source_id, calendar("a", "b", "c", "d", "e", "f", "g", "h"))
    run(db, settings, source_id)
    upload(settings, source_id, calendar("a", "b"))
    run(db, settings, source_id)

    assert db.get_source(source_id)["content_hash"] == ""
    assert run(db, settings, source_id, force=False).status == "blocked"


def test_the_guard_does_not_apply_to_url_sources(env, monkeypatch):
    """A published URL is always a complete snapshot; trust it."""
    db, settings, service, _ = env
    from app import ics

    url_source = db.create_source(
        name="Published", source_type="url",
        ics_url="https://example.invalid/c.ics",
        target_calendar_id=CALENDAR_ID, past_days=3650, future_days=3650,
    )
    monkeypatch.setattr(
        ics, "fetch_feed",
        lambda *a, **k: ics.FetchResult(
            body=calendar("a", "b", "c", "d", "e", "f", "g", "h"), etag="", last_modified=""
        ),
    )
    run(db, settings, url_source)

    monkeypatch.setattr(
        ics, "fetch_feed",
        lambda *a, **k: ics.FetchResult(body=calendar("a"), etag="", last_modified=""),
    )
    result = run(db, settings, url_source)

    assert result.status == "ok"
    assert result.deleted == 7


def test_token_lookup_finds_only_the_matching_file_source(env):
    db, _, _, source_id = env

    assert db.source_for_token("ocs_test")["id"] == source_id
    assert db.source_for_token("ocs_wrong") is None
    assert db.source_for_token("") is None
