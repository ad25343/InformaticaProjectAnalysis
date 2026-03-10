"""
Job TTL cleanup — deletes jobs (and their associated log/S2T files) that are
older than JOB_RETENTION_DAYS.

Runs as a background asyncio loop started during app lifespan.
Can also be called directly for one-off cleanup (e.g., from a script).

Environment variables
---------------------
  JOB_RETENTION_DAYS      Days to keep completed jobs (default: 30)
  CLEANUP_INTERVAL_HOURS  How often the background loop runs (default: 24)
"""
from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone

import aiosqlite

from .db.database import DB_PATH
from .logger import job_log_path
from .agents.s2t_agent import s2t_excel_path

log = logging.getLogger("conversion.cleanup")

JOB_RETENTION_DAYS     = int(os.environ.get("JOB_RETENTION_DAYS",     "30"))
CLEANUP_INTERVAL_HOURS = int(os.environ.get("CLEANUP_INTERVAL_HOURS", "24"))


async def cleanup_old_jobs() -> dict[str, int]:
    """
    Delete jobs created more than JOB_RETENTION_DAYS ago together with
    their associated log files and S2T Excel workbooks.

    Returns a dict: {"deleted_jobs": N, "deleted_files": N}
    """
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=JOB_RETENTION_DAYS)
    ).isoformat()

    # ── Collect job IDs to delete ───────────────────────────────────────────
    async with aiosqlite.connect(DB_PATH) as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT job_id FROM jobs WHERE created_at < ?", (cutoff,)
        )
        rows = await cursor.fetchall()

    job_ids: list[str] = [row["job_id"] for row in rows]
    if not job_ids:
        log.debug("Cleanup: no jobs older than %d days", JOB_RETENTION_DAYS)
        return {"deleted_jobs": 0, "deleted_files": 0}

    # ── Delete associated files ─────────────────────────────────────────────
    deleted_files = 0
    for job_id in job_ids:
        for path_fn in (job_log_path, s2t_excel_path):
            path = path_fn(job_id)
            if path and path.exists():
                try:
                    path.unlink()
                    deleted_files += 1
                except OSError as exc:
                    log.warning("Could not delete file for job %s: %s", job_id, exc)

    # ── Delete rows from DB ─────────────────────────────────────────────────
    async with aiosqlite.connect(DB_PATH) as conn:
        placeholders = ",".join("?" * len(job_ids))
        await conn.execute(
            f"DELETE FROM jobs WHERE job_id IN ({placeholders})", job_ids
        )
        await conn.commit()

    log.info(
        "Cleanup: removed %d job(s) older than %d days; %d file(s) deleted",
        len(job_ids), JOB_RETENTION_DAYS, deleted_files,
    )
    return {"deleted_jobs": len(job_ids), "deleted_files": deleted_files}


async def run_cleanup_loop() -> None:
    """
    Background coroutine — sleeps for CLEANUP_INTERVAL_HOURS then runs cleanup,
    forever.  Start with asyncio.create_task() during app lifespan.
    """
    log.info(
        "Job cleanup loop started (retention=%d days, interval=%dh)",
        JOB_RETENTION_DAYS, CLEANUP_INTERVAL_HOURS,
    )
    while True:
        await asyncio.sleep(CLEANUP_INTERVAL_HOURS * 3_600)
        try:
            result = await cleanup_old_jobs()
            if result["deleted_jobs"] > 0:
                log.info("Scheduled cleanup complete: %s", result)
        except Exception as exc:
            log.error("Cleanup loop error: %s", exc, exc_info=True)
