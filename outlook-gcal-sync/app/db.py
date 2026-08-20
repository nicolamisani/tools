"""SQLite persistence. Small enough that a hand-rolled layer beats an ORM."""
from __future__ import annotations

import json
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

SCHEMA = """
CREATE TABLE IF NOT EXISTS setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS source (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    name              TEXT    NOT NULL,
    source_type       TEXT    NOT NULL DEFAULT 'url',   -- url | file
    ics_url           TEXT    NOT NULL DEFAULT '',
    push_token        TEXT    NOT NULL DEFAULT '',
    uploaded_at       TEXT    NOT NULL DEFAULT '',
    target_calendar_id TEXT   NOT NULL DEFAULT '',
    privacy           TEXT    NOT NULL DEFAULT 'full',   -- full | busy
    past_days         INTEGER NOT NULL DEFAULT 30,
    future_days       INTEGER NOT NULL DEFAULT 365,
    enabled           INTEGER NOT NULL DEFAULT 1,
    reminders         INTEGER NOT NULL DEFAULT 0,        -- 0 = strip reminders
    http_etag         TEXT    NOT NULL DEFAULT '',
    http_last_modified TEXT   NOT NULL DEFAULT '',
    content_hash      TEXT    NOT NULL DEFAULT '',
    created_at        TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS run (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id   INTEGER,
    started_at  TEXT NOT NULL,
    finished_at TEXT,
    status      TEXT NOT NULL,          -- running | ok | skipped | error
    created     INTEGER NOT NULL DEFAULT 0,
    updated     INTEGER NOT NULL DEFAULT 0,
    deleted     INTEGER NOT NULL DEFAULT 0,
    message     TEXT NOT NULL DEFAULT '',
    log         TEXT NOT NULL DEFAULT '[]',
    FOREIGN KEY (source_id) REFERENCES source (id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS run_started_idx ON run (started_at DESC);
"""

DEFAULT_SETTINGS = {
    "sync_interval_minutes": "60",
    "google_token": "",
    "google_email": "",
}


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Database:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as conn:
            conn.executescript(SCHEMA)
            self._migrate(conn)
            for key, value in DEFAULT_SETTINGS.items():
                conn.execute(
                    "INSERT OR IGNORE INTO setting (key, value) VALUES (?, ?)",
                    (key, value),
                )

    @staticmethod
    def _migrate(conn: sqlite3.Connection) -> None:
        """Add columns introduced after a database was first created."""
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(source)")}
        added = (
            ("source_type", "TEXT NOT NULL DEFAULT 'url'"),
            ("push_token", "TEXT NOT NULL DEFAULT ''"),
            ("uploaded_at", "TEXT NOT NULL DEFAULT ''"),
        )
        for name, ddl in added:
            if name not in existing:
                conn.execute(f"ALTER TABLE source ADD COLUMN {name} {ddl}")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    # -- settings ---------------------------------------------------------
    def get_setting(self, key: str, default: str = "") -> str:
        with self.connect() as conn:
            row = conn.execute(
                "SELECT value FROM setting WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else default

    def set_setting(self, key: str, value: str) -> None:
        with self.connect() as conn:
            conn.execute(
                "INSERT INTO setting (key, value) VALUES (?, ?) "
                "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                (key, value),
            )

    # -- sources ----------------------------------------------------------
    def list_sources(self) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute("SELECT * FROM source ORDER BY id").fetchall()

    def get_source(self, source_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM source WHERE id = ?", (source_id,)
            ).fetchone()

    def source_for_token(self, token: str) -> sqlite3.Row | None:
        """Look up a file source by its push token (constant-time compare)."""
        if not token:
            return None
        with self.connect() as conn:
            rows = conn.execute(
                "SELECT * FROM source WHERE source_type = 'file' AND push_token != ''"
            ).fetchall()
        for row in rows:
            if secrets.compare_digest(row["push_token"], token):
                return row
        return None

    def create_source(self, **fields: Any) -> int:
        fields.setdefault("created_at", utcnow())
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self.connect() as conn:
            cur = conn.execute(
                f"INSERT INTO source ({cols}) VALUES ({marks})", tuple(fields.values())
            )
            return int(cur.lastrowid)

    def update_source(self, source_id: int, **fields: Any) -> None:
        if not fields:
            return
        assigns = ", ".join(f"{k} = ?" for k in fields)
        with self.connect() as conn:
            conn.execute(
                f"UPDATE source SET {assigns} WHERE id = ?",
                (*fields.values(), source_id),
            )

    def delete_source(self, source_id: int) -> None:
        with self.connect() as conn:
            conn.execute("DELETE FROM source WHERE id = ?", (source_id,))

    # -- runs -------------------------------------------------------------
    def start_run(self, source_id: int) -> int:
        with self.connect() as conn:
            cur = conn.execute(
                "INSERT INTO run (source_id, started_at, status) VALUES (?, ?, 'running')",
                (source_id, utcnow()),
            )
            return int(cur.lastrowid)

    def finish_run(
        self,
        run_id: int,
        status: str,
        created: int = 0,
        updated: int = 0,
        deleted: int = 0,
        message: str = "",
        log: list[str] | None = None,
    ) -> None:
        with self.connect() as conn:
            conn.execute(
                "UPDATE run SET finished_at = ?, status = ?, created = ?, updated = ?, "
                "deleted = ?, message = ?, log = ? WHERE id = ?",
                (
                    utcnow(),
                    status,
                    created,
                    updated,
                    deleted,
                    message[:2000],
                    json.dumps(log or []),
                    run_id,
                ),
            )

    def recent_runs(self, limit: int = 25) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return conn.execute(
                "SELECT run.*, source.name AS source_name FROM run "
                "LEFT JOIN source ON source.id = run.source_id "
                "ORDER BY run.id DESC LIMIT ?",
                (limit,),
            ).fetchall()

    def get_run(self, run_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT run.*, source.name AS source_name FROM run "
                "LEFT JOIN source ON source.id = run.source_id WHERE run.id = ?",
                (run_id,),
            ).fetchone()

    def last_run_for_source(self, source_id: int) -> sqlite3.Row | None:
        with self.connect() as conn:
            return conn.execute(
                "SELECT * FROM run WHERE source_id = ? AND status != 'running' "
                "ORDER BY id DESC LIMIT 1",
                (source_id,),
            ).fetchone()

    def prune_runs(self, keep: int = 200) -> None:
        with self.connect() as conn:
            conn.execute(
                "DELETE FROM run WHERE id NOT IN "
                "(SELECT id FROM run ORDER BY id DESC LIMIT ?)",
                (keep,),
            )
