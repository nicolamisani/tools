"""Background scheduler that runs every enabled source on a fixed interval."""
from __future__ import annotations

import logging
import threading

from apscheduler.schedulers.background import BackgroundScheduler

from . import sync
from .config import Settings
from .db import Database

log = logging.getLogger("outlook-gcal-sync")

# Serialises scheduled runs against "sync now" clicks so a source is never
# written by two threads at once.
sync_lock = threading.Lock()

JOB_ID = "sync-all"


class SyncScheduler:
    def __init__(self, db: Database, settings: Settings) -> None:
        self.db = db
        self.settings = settings
        self.scheduler = BackgroundScheduler(timezone="UTC")

    def _tick(self) -> None:
        if not sync_lock.acquire(blocking=False):
            log.info("scheduled run skipped: another sync is already running")
            return
        try:
            for result in sync.sync_all(self.db, self.settings):
                log.info("scheduled run: %s - %s", result.status, result.message)
        except Exception:  # pragma: no cover
            log.exception("scheduled run failed")
        finally:
            sync_lock.release()

    def interval_minutes(self) -> int:
        try:
            value = int(self.db.get_setting("sync_interval_minutes", "60"))
        except ValueError:
            value = 60
        return max(5, min(value, 24 * 60))

    def start(self) -> None:
        self.scheduler.start()
        self.reschedule()

    def reschedule(self) -> None:
        minutes = self.interval_minutes()
        self.scheduler.add_job(
            self._tick,
            "interval",
            minutes=minutes,
            id=JOB_ID,
            replace_existing=True,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=300,
        )
        log.info("sync scheduled every %s minutes", minutes)

    def next_run(self):
        job = self.scheduler.get_job(JOB_ID)
        return job.next_run_time if job else None

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)
