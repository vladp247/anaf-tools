"""In-memory bulk job state manager."""
from __future__ import annotations
import asyncio
import uuid
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from backend.utils.logger import get_logger

log = get_logger(__name__)

JobStatus = Literal["queued", "running", "paused", "done", "cancelled", "error"]


@dataclass
class BulkJob:
    job_id: str
    cuis: list[int]
    years: list[int]
    onrc_enrich: bool = False
    offline_financials: bool = False
    phase:   str = "idle"
    message: str = ""
    created_at: float = field(default_factory=time.time)

    total: int = 0
    processed: int = 0
    success: int = 0
    failed: int = 0

    status: JobStatus = "queued"
    current_cui: int | None = None
    current_name: str = ""
    started_at: float | None = None
    finished_at: float | None = None

    original_count: int = 0
    duplicates_removed: int = 0
    invalid_removed: int = 0

    results: list[dict] = field(default_factory=list)
    errors: list[dict] = field(default_factory=list)
    log_entries: list[str] = field(default_factory=list)

    _pause_event: asyncio.Event = field(default_factory=asyncio.Event)
    _cancel_flag: bool = False
    _task: Any = None

    def __post_init__(self):
        self.total = len(self.cuis)
        self._pause_event.set()

    @property
    def remaining(self) -> int:
        return max(0, self.total - self.processed)

    @property
    def eta_seconds(self) -> float | None:
        if not self.started_at or not self.processed:
            return None
        elapsed = time.time() - self.started_at
        rate = self.processed / elapsed
        return self.remaining / rate if rate else None

    @property
    def elapsed_seconds(self) -> float:
        if not self.started_at: return 0.0
        return (self.finished_at or time.time()) - self.started_at

    def add_log(self, msg: str):
        ts = time.strftime("%H:%M:%S")
        self.log_entries.append(f"[{ts}] {msg}")
        if len(self.log_entries) > 200:
            self.log_entries = self.log_entries[-200:]

    def to_dict(self) -> dict:
        return {
            "job_id": self.job_id,
            "status": self.status,
            "total": self.total,
            "processed": self.processed,
            "success": self.success,
            "failed": self.failed,
            "remaining": self.remaining,
            "current_cui": self.current_cui,
            "current_name": self.current_name,
            "eta_seconds": self.eta_seconds,
            "elapsed_seconds": round(self.elapsed_seconds, 1),
            "original_count": self.original_count,
            "duplicates_removed": self.duplicates_removed,
            "invalid_removed": self.invalid_removed,
            "years": self.years,
            "onrc_enrich": self.onrc_enrich,
            "offline_financials": self.offline_financials,
            "phase":   self.phase,
            "message": self.message,
            "log_entries": self.log_entries[-60:],
            "error_count": len(self.errors),
            "result_count": len(self.results),
        }

    def pause(self):
        self._pause_event.clear()
        self.status = "paused"
        self.add_log("⏸ Paused")

    def resume(self):
        self._pause_event.set()
        self.status = "running"
        self.add_log("▶ Resumed")

    def cancel(self):
        self._cancel_flag = True
        self._pause_event.set()
        self.status = "cancelled"
        self.add_log("✖ Cancelled")


class JobManager:
    def __init__(self):
        self._jobs: dict[str, BulkJob] = {}

    def create(self, cuis: list[int], years: list[int]) -> BulkJob:
        jid = str(uuid.uuid4())[:8]
        job = BulkJob(job_id=jid, cuis=cuis, years=years)
        self._jobs[jid] = job
        return job

    def get(self, jid: str) -> BulkJob | None:
        return self._jobs.get(jid)

    def all(self) -> list[BulkJob]:
        return list(self._jobs.values())


_mgr = JobManager()


def get_job_manager() -> JobManager:
    return _mgr
