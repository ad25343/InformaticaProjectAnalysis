"""
Logging infrastructure for the Informatica Conversion Tool.

Two logging layers:
  1. App-level   — logs/app.log     (rotating 10MB×5, JSON lines, all traffic)
  2. Per-job     — logs/jobs/<ts>_<mapping>_<short_id>.log  (one file per conversion)

Per-job files are also indexed in logs/registry.json so you can find any job
by job_id, mapping name, date, or final status without touching the UI.

Per-job log entries are also buffered in DB state (last 200) for the UI panel.
"""
from __future__ import annotations
import json
import logging
import re
import traceback as _tb
from datetime import datetime, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional

# ── Directory layout ──────────────────────────────────────────────────────
APP_DIR  = Path(__file__).parent.parent          # .../app/
LOGS_DIR = APP_DIR / "logs"
JOBS_DIR = LOGS_DIR / "jobs"
LOGS_DIR.mkdir(parents=True, exist_ok=True)
JOBS_DIR.mkdir(parents=True, exist_ok=True)

APP_LOG      = LOGS_DIR / "app.log"
REGISTRY_PATH = LOGS_DIR / "registry.json"


# ─────────────────────────────────────────────────────────────────────────────
# Log formatters
# ─────────────────────────────────────────────────────────────────────────────

class JsonFormatter(logging.Formatter):
    """One JSON object per line for app.log — grep / log-shipper friendly."""
    def format(self, record: logging.LogRecord) -> str:
        payload: dict = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "level":   record.levelname,
            "logger":  record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        for attr in ("job_id", "step", "extra_data"):
            val = getattr(record, attr, None)
            if val is not None:
                payload[attr if attr != "extra_data" else "data"] = val
        return json.dumps(payload)


class HumanFormatter(logging.Formatter):
    _COLORS = {
        "DEBUG": "\033[36m", "INFO": "\033[32m",
        "WARNING": "\033[33m", "ERROR": "\033[31m", "CRITICAL": "\033[35m",
    }
    _RST = "\033[0m"

    def format(self, record: logging.LogRecord) -> str:
        color  = self._COLORS.get(record.levelname, "")
        job_id = getattr(record, "job_id", None)
        step   = getattr(record, "step",   None)
        tag    = f"[{job_id[:8]}]" if job_id else ""
        stag   = f"[s{step}]"      if step is not None else ""
        ts     = datetime.now().strftime("%H:%M:%S")
        msg    = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return f"{color}{ts} {record.levelname:<8}{self._RST} {tag}{stag} {msg}"


# ─────────────────────────────────────────────────────────────────────────────
# App-level logging init
# ─────────────────────────────────────────────────────────────────────────────

def configure_app_logging(level: str = "INFO") -> None:
    """Call once at startup. Idempotent — safe on uvicorn --reload."""
    root = logging.getLogger()
    root.setLevel(getattr(logging, level.upper(), logging.INFO))
    if root.handlers:
        return

    ch = logging.StreamHandler()
    ch.setFormatter(HumanFormatter())
    root.addHandler(ch)

    fh = RotatingFileHandler(
        APP_LOG, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
    )
    fh.setFormatter(JsonFormatter())
    root.addHandler(fh)

    for name in ("uvicorn", "uvicorn.access"):
        logging.getLogger(name).propagate = True

    logging.info("Logging initialised — app log: %s", APP_LOG)


# ─────────────────────────────────────────────────────────────────────────────
# Registry — maps job_id → log metadata
# ─────────────────────────────────────────────────────────────────────────────

def _load_registry() -> dict:
    if REGISTRY_PATH.exists():
        try:
            return json.loads(REGISTRY_PATH.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {}


def _save_registry(reg: dict) -> None:
    REGISTRY_PATH.write_text(
        json.dumps(reg, indent=2, ensure_ascii=False), encoding="utf-8"
    )


def _update_registry(job_id: str, patch: dict) -> None:
    reg = _load_registry()
    entry = reg.setdefault(job_id, {})
    entry.update(patch)
    _save_registry(reg)


def list_log_registry() -> list[dict]:
    """Return all registry entries, newest first."""
    reg = _load_registry()
    entries = list(reg.values())
    entries.sort(key=lambda e: e.get("started_at", ""), reverse=True)
    return entries


def registry_entry(job_id: str) -> Optional[dict]:
    return _load_registry().get(job_id)


def remove_registry_entry(job_id: str) -> None:
    """Remove a job's entry from registry.json (call when a job is deleted)."""
    reg = _load_registry()
    if job_id in reg:
        reg.pop(job_id)
        _save_registry(reg)


def list_orphaned_registry_entries(db_job_ids: set) -> list[dict]:
    """Return registry entries whose job_id is not present in the live DB.
    These are historical jobs whose DB records were removed but whose
    log files still exist on disk."""
    reg = _load_registry()
    entries = []
    for job_id, entry in reg.items():
        if job_id not in db_job_ids:
            # Only include if the log file is still readable
            path = job_log_path(job_id)
            entries.append({**entry, "log_readable": path is not None})
    entries.sort(key=lambda e: e.get("started_at", ""), reverse=True)
    return entries


# ─────────────────────────────────────────────────────────────────────────────
# Filename helpers
# ─────────────────────────────────────────────────────────────────────────────

def _safe_name(s: str, max_len: int = 40) -> str:
    """Strip unsafe filesystem chars, collapse spaces/dots, truncate."""
    s = re.sub(r"[^\w\-.]", "_", s)          # keep word chars, dash, dot
    s = re.sub(r"_+", "_", s).strip("_.")    # collapse runs
    return s[:max_len]


def _job_log_filename(xml_filename: str, job_id: str, started_at: datetime) -> str:
    ts   = started_at.strftime("%Y%m%d_%H%M%S")
    stem = _safe_name(Path(xml_filename).stem)
    uid  = job_id[:8]
    return f"{ts}_{stem}_{uid}.log"


# ─────────────────────────────────────────────────────────────────────────────
# Per-job logger
# ─────────────────────────────────────────────────────────────────────────────

class JobLogger:
    """
    One instance per conversion job.  Writes to:
      • logs/jobs/<ts>_<mapping>_<shortid>.log   — JSONL, persistent
      • root app logger (console + app.log)
      • internal buffer  → stored in DB state for UI log panel
      • logs/registry.json  — single index of all jobs

    Log file anatomy
    ----------------
    Line 1:  {"type":"HEADER", ...}         — job metadata header
    Line N:  {"type":"LOG", ...}            — step log entry
    Line N:  {"type":"STATE_CHANGE", ...}   — pipeline state transitions
    Last:    {"type":"FOOTER", ...}         — final outcome summary
    """

    MAX_BUFFER = 200

    def __init__(self, job_id: str, xml_filename: str):
        self.job_id       = job_id
        self.xml_filename = xml_filename
        self.mapping_name: Optional[str] = None
        self._started_at  = datetime.now(timezone.utc)
        self._current_state: Optional[str] = None
        self._error_count  = 0
        self._warn_count   = 0
        self._buffer: list[dict] = []
        self._app_logger = logging.getLogger("conversion.job")

        # Build the human-readable filename now (mapping name not yet known)
        self._log_filename = _job_log_filename(xml_filename, job_id, self._started_at)
        self._log_path     = JOBS_DIR / self._log_filename
        self._fh = open(self._log_path, "a", encoding="utf-8", buffering=1)

        # Write HEADER as first line
        header = {
            "type":         "HEADER",
            "job_id":       job_id,
            "xml_filename": xml_filename,
            "log_filename": self._log_filename,
            "started_at":   self._started_at.isoformat(),
        }
        self._fh.write(json.dumps(header) + "\n")

        # Register in registry.json
        _update_registry(job_id, {
            "job_id":       job_id,
            "xml_filename": xml_filename,
            "log_filename": self._log_filename,
            "log_path":     str(self._log_path),
            "started_at":   self._started_at.isoformat(),
            "status":       "running",
            "mapping_name": None,
            "completed_at": None,
            "final_status": None,
            "steps_completed": 0,
            "error_count":  0,
            "warn_count":   0,
            "flags_count":  0,
        })

        self.info("Job started", step=0, data={"xml_filename": xml_filename})

    # ── Lifecycle ─────────────────────────────────────────────────────────

    def set_mapping_name(self, name: str) -> None:
        """Call after Step 1 parse when the mapping name is known.
        Renames the log file to include the actual mapping name."""
        if not name or name == self.mapping_name:
            return
        self.mapping_name = name

        # Build a better filename now that we know the mapping name
        new_filename = _job_log_filename(name, self.job_id, self._started_at)
        new_path     = JOBS_DIR / new_filename

        # Rename on disk (flush first)
        try:
            self._fh.flush()
            self._fh.close()
            self._log_path.rename(new_path)
            self._log_path     = new_path
            self._log_filename = new_filename
            self._fh = open(self._log_path, "a", encoding="utf-8", buffering=1)
        except Exception:
            # Rename failed (e.g. cross-device) — keep original filename
            self._fh = open(self._log_path, "a", encoding="utf-8", buffering=1)

        _update_registry(self.job_id, {
            "mapping_name": name,
            "log_filename": self._log_filename,
            "log_path":     str(self._log_path),
        })
        self.info(f"Mapping name identified: {name}", step=1)

    def state_change(self, from_status: str, to_status: str, step: int) -> None:
        """Record an explicit pipeline state transition."""
        ts = datetime.now(timezone.utc).isoformat()
        entry = {
            "type":        "STATE_CHANGE",
            "ts":          ts,
            "step":        step,
            "from_status": from_status,
            "to_status":   to_status,
            "message":     f"State: {from_status} → {to_status}",
        }
        try:
            self._fh.write(json.dumps(entry) + "\n")
        except Exception:
            pass
        self._current_state = to_status
        _update_registry(self.job_id, {"status": to_status, "steps_completed": step})

    def finalize(self, final_status: str, steps_completed: int,
                 flags_count: int = 0) -> None:
        """Write FOOTER entry and update registry with final outcome."""
        ts = datetime.now(timezone.utc).isoformat()
        elapsed = (datetime.now(timezone.utc) - self._started_at).total_seconds()
        footer = {
            "type":            "FOOTER",
            "ts":              ts,
            "job_id":          self.job_id,
            "mapping_name":    self.mapping_name,
            "final_status":    final_status,
            "steps_completed": steps_completed,
            "elapsed_seconds": round(elapsed, 1),
            "error_count":     self._error_count,
            "warn_count":      self._warn_count,
            "flags_count":     flags_count,
        }
        try:
            self._fh.write(json.dumps(footer) + "\n")
        except Exception:
            pass

        _update_registry(self.job_id, {
            "status":          final_status,
            "final_status":    final_status,
            "completed_at":    ts,
            "elapsed_seconds": round(elapsed, 1),
            "steps_completed": steps_completed,
            "error_count":     self._error_count,
            "warn_count":      self._warn_count,
            "flags_count":     flags_count,
        })

        outcome = "✅ COMPLETE" if final_status == "complete" else f"❌ {final_status.upper()}"
        self._app_logger.info(
            "%s job_id=%s mapping=%s elapsed=%.1fs errors=%d",
            outcome, self.job_id[:8], self.mapping_name, elapsed, self._error_count,
        )

    def close(self) -> None:
        try:
            self._fh.close()
        except Exception:
            pass

    # ── Step helpers ──────────────────────────────────────────────────────

    def step_start(self, step: int, name: str) -> None:
        self.info(f"▶ Step {step} started — {name}", step=step)

    def step_complete(self, step: int, name: str, summary: str = "") -> None:
        self.info(
            f"✓ Step {step} complete — {name}{': ' + summary if summary else ''}",
            step=step,
        )
        _update_registry(self.job_id, {"steps_completed": step})

    def step_failed(self, step: int, name: str, error: str, exc_info: bool = False) -> None:
        self.error(f"✗ Step {step} FAILED — {name}: {error}", step=step,
                   exc_info=exc_info)

    def claude_call(self, step: int, purpose: str,
                    tokens_in: int = None, tokens_out: int = None) -> None:
        data = {}
        if tokens_in  is not None: data["tokens_in"]  = tokens_in
        if tokens_out is not None: data["tokens_out"] = tokens_out
        self.info(f"🤖 Claude — {purpose}", step=step, data=data or None)

    # ── Core write methods ────────────────────────────────────────────────

    def info(self, message: str, step: int = None, data: dict = None) -> None:
        self._write("INFO", message, step, data)

    def warning(self, message: str, step: int = None, data: dict = None) -> None:
        self._warn_count += 1
        self._write("WARNING", message, step, data)

    def error(self, message: str, step: int = None,
              data: dict = None, exc_info: bool = False) -> None:
        self._error_count += 1
        self._write("ERROR", message, step, data, exc_info=exc_info)

    def _write(self, level: str, message: str, step, data,
               exc_info: bool = False) -> None:
        ts = datetime.now(timezone.utc).isoformat()
        entry: dict = {
            "type":    "LOG",
            "ts":      ts,
            "level":   level,
            "step":    step,
            "message": message,
        }
        if data:
            entry["data"] = data
        if exc_info:
            entry["exc"] = _tb.format_exc()

        # Write to per-job file
        try:
            self._fh.write(json.dumps(entry) + "\n")
        except Exception:
            pass

        # Route through root logger (console + app.log)
        extra: dict = {"job_id": self.job_id}
        if step   is not None: extra["step"]       = step
        if data   is not None: extra["extra_data"] = data
        log_level = getattr(logging, level, logging.INFO)
        record = self._app_logger.makeRecord(
            self._app_logger.name, log_level,
            "(pipeline)", 0, message, (), None, extra=extra
        )
        self._app_logger.handle(record)

        # Buffer for UI (strips "type" field — UI doesn't need it)
        buf_entry = {k: v for k, v in entry.items() if k != "type"}
        self._buffer.append(buf_entry)
        if len(self._buffer) > self.MAX_BUFFER:
            self._buffer = self._buffer[-self.MAX_BUFFER:]

    def get_buffer(self) -> list[dict]:
        return list(self._buffer)


# ─────────────────────────────────────────────────────────────────────────────
# Lookup helpers (used by routes)
# ─────────────────────────────────────────────────────────────────────────────

def job_log_path(job_id: str) -> Optional[Path]:
    """Return the Path to a job's log file, or None if not found."""
    entry = registry_entry(job_id)
    if entry and entry.get("log_path"):
        p = Path(entry["log_path"])
        if p.exists():
            return p
    # Fallback: scan jobs dir for matching job_id prefix
    for f in JOBS_DIR.glob(f"*_{job_id[:8]}.log"):
        return f
    return None


def read_job_log(job_id: str) -> list[dict]:
    """Read all LOG/STATE_CHANGE entries for a job. Skips HEADER/FOOTER."""
    path = job_log_path(job_id)
    if not path:
        return []
    entries = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
                # Include LOG and STATE_CHANGE; skip HEADER/FOOTER in UI buffer
                if obj.get("type") in ("LOG", "STATE_CHANGE", None):
                    # Normalise: ensure 'level' present
                    if "level" not in obj:
                        obj["level"] = "INFO"
                    entries.append(obj)
            except Exception:
                entries.append({"ts": "", "level": "RAW", "message": line})
    return entries


def read_job_log_raw(job_id: str) -> str:
    """Return the entire raw log file content as a string."""
    path = job_log_path(job_id)
    if not path:
        return ""
    return path.read_text(encoding="utf-8")
