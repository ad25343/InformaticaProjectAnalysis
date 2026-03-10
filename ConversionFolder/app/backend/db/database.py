"""
SQLite database layer — stores job state as JSON blobs.
Simple and portable for MVP. Swap to Postgres by changing DATABASE_URL.
"""
import json
import logging
import uuid
import zlib
import base64
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Optional, List

import aiosqlite

_db_log = logging.getLogger("conversion.db")

# ── State compression ────────────────────────────────────────────────────────
# state_json is stored as either:
#   - plain JSON text (legacy jobs)
#   - "z:" + base64(zlib.compress(json)) — compressed jobs (v2.4.3+)
# Compression typically reduces state size by 70-80% for text-heavy content.
_COMPRESS_PREFIX = "z:"
_MAX_LOG_ENTRIES = 300   # pipeline_log entries kept in state (older entries dropped)


def _encode_state(state: dict) -> str:
    """Serialize and compress state dict for storage."""
    # Cap pipeline_log to avoid unbounded growth
    if "pipeline_log" in state and isinstance(state["pipeline_log"], list):
        state["pipeline_log"] = state["pipeline_log"][-_MAX_LOG_ENTRIES:]
    raw = json.dumps(state, separators=(",", ":"))
    compressed = zlib.compress(raw.encode(), level=6)
    return _COMPRESS_PREFIX + base64.b64encode(compressed).decode()


def _decode_state(stored: str) -> dict:
    """Deserialize state — handles both compressed and legacy plain-JSON formats.

    Returns an empty dict rather than raising on corrupted data so a single
    bad row cannot crash a list-jobs call or pipeline query.
    """
    if not stored:
        return {}
    try:
        if stored.startswith(_COMPRESS_PREFIX):
            compressed = base64.b64decode(stored[len(_COMPRESS_PREFIX):])
            return json.loads(zlib.decompress(compressed).decode())
        return json.loads(stored)   # legacy plain JSON
    except Exception as exc:
        _db_log.error(
            "State deserialization failed (returning {}). "
            "Stored value prefix: %.80r — error: %s",
            stored, exc,
        )
        return {}

import os  # noqa: F401  kept for backward-compat imports elsewhere
from ..config import settings as _cfg

# DB_PATH: use explicit setting if provided, otherwise default to app/data/jobs.db
_default_db = Path(__file__).parent.parent.parent / "data" / "jobs.db"
DB_PATH = Path(_cfg.db_path) if _cfg.db_path else _default_db
DB_PATH.parent.mkdir(parents=True, exist_ok=True)


# ── Connection helper (v2.6.0) ───────────────────────────────────────────────
# All DB access goes through _connect() so every connection gets:
#   - busy_timeout=5000   — wait up to 5 s instead of raising "database is locked"
#   - WAL mode is persistent on the file (set once in init_db); no per-connection PRAGMA needed
@asynccontextmanager
async def _connect():
    """Open a DB connection with the standard per-connection PRAGMAs applied."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("PRAGMA busy_timeout=5000")
        yield db

CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id                  TEXT PRIMARY KEY,
    filename                TEXT NOT NULL,
    xml_content             TEXT,
    workflow_xml_content    TEXT,
    parameter_file_content  TEXT,
    status                  TEXT NOT NULL DEFAULT 'pending',
    current_step            INTEGER NOT NULL DEFAULT 0,
    state_json              TEXT NOT NULL DEFAULT '{}',
    created_at              TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    batch_id                TEXT,
    complexity_tier         TEXT
);
"""

CREATE_BATCH_TABLE = """
CREATE TABLE IF NOT EXISTS batches (
    batch_id        TEXT PRIMARY KEY,
    source_zip      TEXT NOT NULL,
    mapping_count   INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);
"""

CREATE_INDICES = """
CREATE INDEX IF NOT EXISTS idx_jobs_status     ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_created_at ON jobs(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_jobs_batch_id   ON jobs(batch_id);
CREATE INDEX IF NOT EXISTS idx_jobs_deleted_at ON jobs(deleted_at);
"""
# Columns added in v1.1 — applied via ALTER TABLE so existing DBs keep working
_V1_1_MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN workflow_xml_content   TEXT",
    "ALTER TABLE jobs ADD COLUMN parameter_file_content TEXT",
]

# Columns / tables added in v2.0 — batch conversion support
_V2_0_MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN batch_id TEXT",
    CREATE_BATCH_TABLE.strip(),
]

# v2.1 — soft delete: flag jobs instead of physical DELETE
_V2_1_MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN deleted_at TEXT",
]

# v2.6.0 — complexity_tier column (first-class, indexed, no state decompress needed for listing)
_V2_6_MIGRATIONS = [
    "ALTER TABLE jobs ADD COLUMN complexity_tier TEXT",
]
CREATE_COMPLEXITY_INDEX = "CREATE INDEX IF NOT EXISTS idx_jobs_complexity_tier ON jobs(complexity_tier);"

# v2.4.6 — GAP #17: immutable audit trail for all gate decisions
CREATE_AUDIT_TABLE = """
CREATE TABLE IF NOT EXISTS job_audit_log (
    audit_id      TEXT PRIMARY KEY,
    job_id        TEXT NOT NULL,
    gate          TEXT NOT NULL,
    event_type    TEXT NOT NULL,
    reviewer_name TEXT NOT NULL,
    reviewer_role TEXT,
    decision      TEXT NOT NULL,
    notes         TEXT,
    extra_json    TEXT,
    created_at    TEXT NOT NULL
);
"""
CREATE_AUDIT_INDICES = """
CREATE INDEX IF NOT EXISTS idx_audit_job_id  ON job_audit_log(job_id);
CREATE INDEX IF NOT EXISTS idx_audit_gate    ON job_audit_log(gate);
CREATE INDEX IF NOT EXISTS idx_audit_created ON job_audit_log(created_at DESC);
"""


async def init_db():
    async with _connect() as db:
        # ── v2.6.0: WAL mode — persistent on the file, set once here ─────
        # WAL allows concurrent reads alongside a single writer, eliminating
        # the "database is locked" errors that batches hit in journal mode.
        # synchronous=NORMAL is safe with WAL (no data loss on OS crash).
        async with db.execute("PRAGMA journal_mode=WAL") as _cur:
            _row = await _cur.fetchone()
            _mode = _row[0].lower() if _row else "unknown"
            if _mode != "wal":
                _db_log.warning(
                    "SQLite WAL mode requested but journal_mode='%s'. "
                    "DB may be on NFS/tmpfs/cloud FS — WAL is unsupported there. "
                    "Concurrent write contention will use the fallback locking mode.",
                    _mode,
                )
        await db.execute("PRAGMA synchronous=NORMAL")

        await db.execute(CREATE_TABLE)
        await db.execute(CREATE_BATCH_TABLE)
        # Create indices (idempotent — IF NOT EXISTS)
        for _idx_sql in CREATE_INDICES.strip().split(";"):
            _idx_sql = _idx_sql.strip()
            if _idx_sql:
                await db.execute(_idx_sql)
        # Apply v1.1 migrations idempotently — SQLite raises OperationalError
        # "duplicate column name" if column already exists; we swallow that.
        for sql in _V1_1_MIGRATIONS:
            try:
                await db.execute(sql)
            except Exception:
                pass  # column already present
        # Apply v2.0 migrations idempotently
        for sql in _V2_0_MIGRATIONS:
            try:
                await db.execute(sql)
            except Exception:
                pass  # column/table already present
        # Apply v2.1 migrations idempotently
        for sql in _V2_1_MIGRATIONS:
            try:
                await db.execute(sql)
            except Exception:
                pass  # column already present
        # v2.4.6 — audit trail table (CREATE IF NOT EXISTS, fully idempotent)
        await db.execute(CREATE_AUDIT_TABLE)
        for _idx_sql in CREATE_AUDIT_INDICES.strip().split(";"):
            _idx_sql = _idx_sql.strip()
            if _idx_sql:
                await db.execute(_idx_sql)
        # v2.6.0 — complexity_tier column + index
        for sql in _V2_6_MIGRATIONS:
            try:
                await db.execute(sql)
            except Exception:
                pass  # column already present
        await db.execute(CREATE_COMPLEXITY_INDEX)
        await db.commit()


async def create_job(
    filename: str,
    xml_content: str,
    workflow_xml_content: Optional[str] = None,
    parameter_file_content: Optional[str] = None,
    batch_id: Optional[str] = None,
) -> str:
    job_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    async with _connect() as db:
        await db.execute(
            "INSERT INTO jobs "
            "(job_id, filename, xml_content, workflow_xml_content, parameter_file_content, "
            " status, current_step, state_json, created_at, updated_at, batch_id) "
            "VALUES (?, ?, ?, ?, ?, 'pending', 0, '{}', ?, ?, ?)",
            (job_id, filename, xml_content, workflow_xml_content, parameter_file_content, now, now, batch_id),
        )
        await db.commit()
    return job_id


async def get_job(job_id: str) -> Optional[dict]:
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            d = dict(row)
            d["state"] = _decode_state(d["state_json"])
            return d


async def get_xml(job_id: str) -> Optional[str]:
    """Return only the primary mapping XML (backward-compatible)."""
    async with _connect() as db:
        async with db.execute("SELECT xml_content FROM jobs WHERE job_id = ?", (job_id,)) as cur:
            row = await cur.fetchone()
            return row[0] if row else None


async def get_session_files(job_id: str) -> Optional[dict]:
    """Return all three file contents for v1.1 Step 0.

    Returns a dict with keys:
      - xml_content             (mapping XML — always present)
      - workflow_xml_content    (workflow XML — may be None)
      - parameter_file_content  (parameter file — may be None)
    Returns None if the job does not exist.
    """
    async with _connect() as db:
        async with db.execute(
            "SELECT xml_content, workflow_xml_content, parameter_file_content "
            "FROM jobs WHERE job_id = ?",
            (job_id,),
        ) as cur:
            row = await cur.fetchone()
            if not row:
                return None
            return {
                "xml_content":            row[0],
                "workflow_xml_content":   row[1],
                "parameter_file_content": row[2],
            }


async def update_job(job_id: str, status: str, step: int, state_patch: dict):
    """GAP #1 — Uses BEGIN IMMEDIATE to prevent concurrent write races.

    SQLite has no SELECT...FOR UPDATE syntax.  BEGIN IMMEDIATE acquires a
    write-lock before the first read so two concurrent update_job() calls for
    the same job_id cannot both read the same state snapshot and then race to
    overwrite each other.

    v2.6.0: also writes complexity_tier to its own column when the patch
    contains a complexity dict, so listing jobs never needs to decompress state.
    """
    now = datetime.utcnow().isoformat()
    # Extract complexity_tier if the patch carries it
    _tier: Optional[str] = None
    if "complexity" in state_patch and isinstance(state_patch["complexity"], dict):
        _tier = state_patch["complexity"].get("tier")

    async with _connect() as db:
        try:
            await db.execute("BEGIN IMMEDIATE")
            async with db.execute(
                "SELECT state_json FROM jobs WHERE job_id = ?", (job_id,)
            ) as cur:
                row = await cur.fetchone()
                current = _decode_state(row[0]) if row else {}
            current.update(state_patch)
            if _tier:
                await db.execute(
                    "UPDATE jobs SET status=?, current_step=?, state_json=?, "
                    "updated_at=?, complexity_tier=? WHERE job_id=?",
                    (status, step, _encode_state(current), now, _tier, job_id),
                )
            else:
                await db.execute(
                    "UPDATE jobs SET status=?, current_step=?, state_json=?, updated_at=?"
                    " WHERE job_id=?",
                    (status, step, _encode_state(current), now, job_id),
                )
            await db.execute("COMMIT")
        except Exception:
            try:
                await db.execute("ROLLBACK")
            except Exception:
                pass
            raise


async def create_batch(source_zip: str, mapping_count: int) -> str:
    """Create a batch record and return its batch_id."""
    batch_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    async with _connect() as db:
        await db.execute(
            "INSERT INTO batches (batch_id, source_zip, mapping_count, created_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (batch_id, source_zip, mapping_count, now, now),
        )
        await db.commit()
    return batch_id


async def create_batch_atomic(
    source_zip: str,
    mappings: list[dict],   # [{"filename", "xml", "workflow_xml", "parameter_file"}]
) -> tuple[str, list[str]]:
    """
    GAP #4 — Atomic batch creation.
    Creates the batch record AND all job records in a single transaction.
    On any failure the entire batch is rolled back — no orphaned jobs.
    Returns (batch_id, [job_id, ...]) in insertion order.
    """
    batch_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    job_ids: list[str] = [str(uuid.uuid4()) for _ in mappings]

    async with _connect() as db:
        try:
            await db.execute("BEGIN")
            await db.execute(
                "INSERT INTO batches (batch_id, source_zip, mapping_count, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (batch_id, source_zip, len(mappings), now, now),
            )
            for job_id, m in zip(job_ids, mappings):
                await db.execute(
                    "INSERT INTO jobs "
                    "(job_id, filename, xml_content, workflow_xml_content, "
                    " parameter_file_content, status, current_step, state_json, "
                    " created_at, updated_at, batch_id) "
                    "VALUES (?, ?, ?, ?, ?, 'pending', 0, '{}', ?, ?, ?)",
                    (
                        job_id,
                        m["filename"],
                        m["xml"],
                        m.get("workflow_xml"),
                        m.get("parameter_file"),
                        now, now,
                        batch_id,
                    ),
                )
            await db.execute("COMMIT")
        except Exception:
            await db.execute("ROLLBACK")
            raise
    return batch_id, job_ids


async def get_batch(batch_id: str) -> Optional[dict]:
    """Return the batch record (without jobs). Returns None if not found."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT * FROM batches WHERE batch_id = ?", (batch_id,)
        ) as cur:
            row = await cur.fetchone()
            return dict(row) if row else None


async def get_batch_jobs(batch_id: str) -> List[dict]:
    """Return all jobs belonging to a batch, minimal fields only."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT job_id, filename, status, current_step, created_at, updated_at, state_json "
            "FROM jobs WHERE batch_id = ? ORDER BY created_at ASC",
            (batch_id,),
        ) as cur:
            rows = await cur.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                state = _decode_state(d.pop("state_json", ""))
                d["complexity"] = state.get("complexity", {}).get("tier") if state.get("complexity") else None
                d["batch_id"] = batch_id
                result.append(d)
            return result


async def delete_job(job_id: str) -> bool:
    """Soft-delete a job by stamping deleted_at. Returns True if the row was found.
    The record is retained so log files and audit history remain accessible."""
    now = datetime.utcnow().isoformat()
    async with _connect() as db:
        await db.execute(
            "UPDATE jobs SET deleted_at = ?, updated_at = ? WHERE job_id = ? AND deleted_at IS NULL",
            (now, now, job_id),
        )
        await db.commit()
        return db.total_changes > 0


async def list_deleted_jobs() -> List[dict]:
    """Return soft-deleted jobs, newest first, for the Log Archive sidebar."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT job_id, filename, status, current_step, created_at, deleted_at, state_json, batch_id "
            "FROM jobs WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT 200"
        ) as cur:
            rows = await cur.fetchall()
            result = []
            for row in rows:
                d = dict(row)
                state = _decode_state(d.pop("state_json", ""))
                d["mapping_name"] = state.get("mapping_name") or state.get("complexity", {})
                d["log_readable"] = True   # log file preserved on disk
                result.append(d)
            return result


async def recover_stuck_jobs() -> List[str]:
    """
    Mark jobs that were left in mid-pipeline states as FAILED.

    Called once at startup.  Any job whose status is a transient processing
    state (parsing, classifying, documenting, verifying, assigning_stack,
    converting, security_scanning, reviewing, testing) will never complete
    after a server restart because its asyncio task is gone.  Marking them
    FAILED makes the UI show an actionable state (delete + retry) rather
    than a spinner that never resolves.

    Gate statuses (awaiting_review, awaiting_security_review,
    awaiting_code_review) are intentionally excluded — a human reviewer
    can still approve or reject them after the server comes back up.

    Returns the list of job_ids that were recovered.
    """
    # Every transient status where an asyncio task is doing live work.
    # These can never complete after a server restart — the task is gone.
    # Gate/awaiting statuses are intentionally excluded: a human reviewer
    # can still approve/reject them after the server comes back up.
    _STUCK_STATUSES = (
        "parsing",           # Step 1
        "classifying",       # Step 2
        "documenting",       # Step 3
        "verifying",         # Step 4
        "assigning_stack",   # Step 6
        "converting",        # Step 7
        "security_scanning", # Step 8
        "reviewing",         # Step 10
        "testing",           # Step 11
    )
    placeholders = ",".join("?" * len(_STUCK_STATUSES))
    now = datetime.utcnow().isoformat()
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            f"SELECT job_id FROM jobs WHERE status IN ({placeholders})",
            _STUCK_STATUSES,
        ) as cur:
            rows = await cur.fetchall()
        job_ids = [row["job_id"] for row in rows]
        if job_ids:
            state_patch_json = json.dumps({
                "error": (
                    "Job was interrupted by a server restart while the pipeline was running. "
                    "Delete this job and re-upload the mapping to start a fresh conversion."
                )
            })
            for job_id in job_ids:
                await db.execute(
                    "UPDATE jobs SET status='failed', state_json=?, updated_at=? "
                    "WHERE job_id=?",
                    (state_patch_json, now, job_id),
                )
            await db.commit()
    return job_ids


async def count_jobs() -> int:
    """Return the total number of non-deleted jobs (for pagination metadata)."""
    async with _connect() as db:
        async with db.execute(
            "SELECT COUNT(*) FROM jobs WHERE deleted_at IS NULL"
        ) as cur:
            row = await cur.fetchone()
            return row[0] if row else 0


async def list_jobs(limit: int = 20, offset: int = 0) -> List[dict]:
    """Return jobs newest-first with pagination.

    v2.6.0: reads complexity_tier from its dedicated column — no state
    decompression needed on the listing path.
    """
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT job_id, filename, status, current_step, created_at, updated_at, "
            "       complexity_tier, batch_id "
            "FROM jobs WHERE deleted_at IS NULL ORDER BY created_at DESC "
            "LIMIT ? OFFSET ?",
            (limit, offset),
        ) as cur:
            rows = await cur.fetchall()
            return [dict(row) for row in rows]


# ── GAP #17: Audit trail ──────────────────────────────────────────────────────

async def add_audit_entry(
    job_id: str,
    gate: str,
    event_type: str,
    reviewer_name: str,
    reviewer_role: Optional[str],
    decision: str,
    notes: Optional[str] = None,
    extra: Optional[dict] = None,
) -> str:
    """Insert one immutable audit record for a gate decision.

    gate       — 'gate1' | 'gate2' | 'gate3'
    event_type — decision value lowercased ('approved', 'rejected',
                  'acknowledged', 'request_fix', 'failed')
    extra      — gate-specific payload (e.g. {'remediation_round': 1})
    Returns the new audit_id (UUID string).
    """
    audit_id = str(uuid.uuid4())
    now = datetime.utcnow().isoformat()
    extra_json = json.dumps(extra) if extra else None
    async with _connect() as db:
        await db.execute(
            "INSERT INTO job_audit_log "
            "(audit_id, job_id, gate, event_type, reviewer_name, reviewer_role, "
            " decision, notes, extra_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                audit_id, job_id, gate, event_type,
                reviewer_name, reviewer_role,
                decision, notes, extra_json, now,
            ),
        )
        await db.commit()
    return audit_id


async def get_audit_log(job_id: str) -> List[dict]:
    """Return all audit entries for a job, oldest first."""
    async with _connect() as db:
        db.row_factory = aiosqlite.Row
        async with db.execute(
            "SELECT audit_id, job_id, gate, event_type, reviewer_name, reviewer_role, "
            "       decision, notes, extra_json, created_at "
            "FROM job_audit_log WHERE job_id = ? ORDER BY created_at ASC",
            (job_id,),
        ) as cur:
            rows = await cur.fetchall()
    result = []
    for row in rows:
        d = dict(row)
        if d.get("extra_json"):
            try:
                d["extra"] = json.loads(d["extra_json"])
            except Exception:
                d["extra"] = None
        else:
            d["extra"] = None
        del d["extra_json"]
        result.append(d)
    return result
