"""
scheduler.py — Time-based manifest scheduler (v2.15.0)
=======================================================

Watches a directory for *.schedule.json files and materialises
*.manifest.json files into WATCHER_DIR when a scheduled cron
expression fires.  The existing manifest file watcher then picks up
the materialised manifest and triggers conversion as normal.

SCHEDULE FILE FORMAT
--------------------
Drop a file named <anything>.schedule.json in SCHEDULER_DIR:

    {
        "version":  "1.0",
        "cron":     "0 2 * * 1-5",
        "timezone": "America/New_York",
        "label":    "Customer Pipeline Nightly",
        "enabled":  true,
        "manifest": {
            "version":  "1.0",
            "mappings": [
                "m_customer_load.xml",
                {
                    "mapping":    "m_appraisal_rank.xml",
                    "workflow":   "wf_appraisal.xml"
                }
            ],
            "workflow":      "wf_default.xml",
            "parameters":    "params_prod.xml",
            "reviewer":      "Jane Smith",
            "reviewer_role": "Data Engineer"
        }
    }

Fields
------
cron        Required.  Standard 5-field cron expression
            (minute hour dom month dow).
            DOW: 0=Sunday, 1=Monday, ..., 6=Saturday (7 also = Sunday).
            Evaluated every SCHEDULER_POLL_INTERVAL_SECS seconds.

timezone    Optional.  IANA timezone name (e.g. "America/New_York",
            "Europe/London", "Asia/Singapore").
            Defaults to UTC.  Requires Python 3.9+ (zoneinfo stdlib).

label       Optional.  Written as the manifest "label" field and used
            in the output directory name.  Defaults to schedule filename
            stem.

enabled     Optional.  Set to false to pause a schedule without deleting
            the file.  Defaults to true.

manifest    Required.  Manifest payload — same schema as a hand-dropped
            manifest (see watcher.py).  The scheduler injects "label"
            if not already present in the manifest object.

LIFECYCLE
---------
1. Scheduler polls SCHEDULER_DIR every SCHEDULER_POLL_INTERVAL_SECS.
2. For each *.schedule.json, parses and evaluates the cron expression.
3. If the current minute matches and has not already fired this minute:
   a. Injects "label" into the manifest payload (if absent).
   b. Writes <label>_<YYYYMMDD_HHMMSS_ffffff>.manifest.json → WATCHER_DIR.
   c. Records the fired minute to prevent duplicate materialisation.
4. The manifest file watcher picks up the file and triggers conversion.
5. Schedule files are re-read on every poll — edits take effect
   immediately without a server restart.

ENABLING
--------
Set SCHEDULER_ENABLED=true, SCHEDULER_DIR (directory containing
*.schedule.json files), plus WATCHER_ENABLED=true and WATCHER_DIR
(where materialised manifests are dropped for the watcher to process).

CRON EXPRESSION REFERENCE
--------------------------
  Field:  minute   hour   day-of-month   month   day-of-week
  Range:  0–59     0–23   1–31           1–12    0–7 (0,7=Sun; 1=Mon; …; 6=Sat)
  Special: *  */n  a-b  a-b/n  a,b,c

  "0 2 * * 1-5"    — weekdays at 02:00
  "30 6 * * *"     — every day at 06:30
  "0 */4 * * *"    — every 4 hours on the hour
  "15 8 1 * *"     — 1st of every month at 08:15
  "0 18 * * 5"     — Fridays at 18:00
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

log = logging.getLogger("conversion.scheduler")

# ── Cron evaluation ──────────────────────────────────────────────────────────

def _expand_field(field: str, min_val: int, max_val: int) -> set[int]:
    """
    Expand a single cron field to a set of matching integer values.
    Handles: *  */n  a-b  a-b/n  a,b,c  and any comma-joined mix.
    Raises ValueError on malformed syntax.
    """
    result: set[int] = set()
    for part in field.split(","):
        part = part.strip()
        if part == "*":
            result.update(range(min_val, max_val + 1))
        elif "/" in part:
            range_part, step_str = part.split("/", 1)
            try:
                step = int(step_str)
            except ValueError:
                raise ValueError(f"Invalid step value in cron field: {part!r}")
            if step < 1:
                raise ValueError(f"Cron step must be >= 1: {part!r}")
            if range_part == "*":
                start, end = min_val, max_val
            elif "-" in range_part:
                a, b = range_part.split("-", 1)
                start, end = int(a), int(b)
            else:
                start, end = int(range_part), max_val
            result.update(range(start, end + 1, step))
        elif "-" in part:
            a, b = part.split("-", 1)
            result.update(range(int(a), int(b) + 1))
        else:
            result.add(int(part))
    return result


def _cron_matches(cron_expr: str, dt: datetime) -> bool:
    """
    Return True if the given datetime matches the 5-field cron expression.

    Day-of-week uses standard cron convention:
        0 = Sunday, 1 = Monday, ..., 6 = Saturday, 7 = Sunday (alias).
    Python's isoweekday() is mapped: Mon=1 ... Sat=6, Sun=0 via % 7.

    Raises ValueError if the expression is malformed.
    """
    fields = cron_expr.strip().split()
    if len(fields) != 5:
        raise ValueError(
            f"Cron expression must have exactly 5 fields; "
            f"got {len(fields)}: {cron_expr!r}"
        )
    minute_f, hour_f, dom_f, month_f, dow_f = fields

    # Normalise 7 → 0 in DOW field (both mean Sunday in standard cron).
    # Use word-boundary replacement to avoid mangling values like 17 or 70.
    dow_f = re.sub(r"\b7\b", "0", dow_f)

    try:
        valid_minutes = _expand_field(minute_f,  0, 59)
        valid_hours   = _expand_field(hour_f,    0, 23)
        valid_doms    = _expand_field(dom_f,      1, 31)
        valid_months  = _expand_field(month_f,    1, 12)
        valid_dows    = _expand_field(dow_f,      0,  6)
    except ValueError as exc:
        raise ValueError(f"Invalid cron expression {cron_expr!r}: {exc}") from exc

    # Python isoweekday(): 1=Mon … 6=Sat, 7=Sun → cron: 0=Sun, 1=Mon … 6=Sat
    python_dow = dt.isoweekday() % 7

    return (
        dt.minute in valid_minutes
        and dt.hour   in valid_hours
        and dt.day    in valid_doms
        and dt.month  in valid_months
        and python_dow in valid_dows
    )


# ── Timezone helpers ─────────────────────────────────────────────────────────

def _now_in_tz(tz_name: Optional[str]) -> datetime:
    """Return the current local datetime in the given IANA timezone (or UTC)."""
    import zoneinfo  # stdlib since Python 3.9
    if not tz_name:
        return datetime.now(tz=zoneinfo.ZoneInfo("UTC"))
    try:
        return datetime.now(tz=zoneinfo.ZoneInfo(tz_name))
    except (zoneinfo.ZoneInfoNotFoundError, KeyError):
        log.warning(
            "Scheduler: unknown timezone %r — falling back to UTC", tz_name
        )
        return datetime.now(tz=zoneinfo.ZoneInfo("UTC"))


# ── Schedule file reader ─────────────────────────────────────────────────────

def _read_schedule(path: Path) -> Optional[dict]:
    """
    Parse and validate a *.schedule.json file.
    Returns the validated dict on success, None on any error (errors logged).
    """
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        log.error("Scheduler: could not read %s — %s", path.name, exc)
        return None

    if not isinstance(raw, dict):
        log.error("Scheduler: %s top-level value is not a JSON object", path.name)
        return None

    # enabled field (defaults to true — missing = enabled)
    if not raw.get("enabled", True):
        log.debug("Scheduler: %s is disabled (enabled=false) — skipping", path.name)
        return None

    # cron is required
    cron_expr = raw.get("cron")
    if not isinstance(cron_expr, str) or not cron_expr.strip():
        log.error("Scheduler: %s missing required 'cron' string field", path.name)
        return None

    # manifest is required
    manifest = raw.get("manifest")
    if not isinstance(manifest, dict):
        log.error(
            "Scheduler: %s missing required 'manifest' object", path.name
        )
        return None

    # Validate cron expression syntax at read time to surface errors early
    try:
        _cron_matches(cron_expr, datetime.now())
    except ValueError as exc:
        log.error(
            "Scheduler: %s has invalid cron expression — %s", path.name, exc
        )
        return None

    # Optional fields — type-check but do not require
    tz = raw.get("timezone")
    if tz is not None and not isinstance(tz, str):
        log.error("Scheduler: %s 'timezone' must be a string", path.name)
        return None

    label = raw.get("label")
    if label is not None and not isinstance(label, str):
        log.error("Scheduler: %s 'label' must be a string", path.name)
        return None

    return raw


# ── Manifest materialiser ────────────────────────────────────────────────────

_SAFE_LABEL_RE = re.compile(r"[^\w\-]", re.ASCII)


def _safe_label(label: str) -> str:
    """Sanitise a label string for use in a filename (ASCII word chars + hyphens)."""
    sanitised = _SAFE_LABEL_RE.sub("_", label).strip("_")
    return sanitised or "schedule"


def _materialise(
    schedule_name: str,
    schedule: dict,
    watcher_dir: Path,
) -> bool:
    """
    Write a *.manifest.json file into watcher_dir from the schedule's
    manifest template.  Returns True on success, False on failure.

    Label precedence: schedule['label'] → manifest['label'] → schedule_name.
    The resolved label is always injected into the materialised manifest.
    """
    # Shallow copy so we don't mutate the caller's dict
    manifest = dict(schedule["manifest"])

    label = (
        schedule.get("label")
        or manifest.get("label")
        or schedule_name
    )
    manifest["label"] = label   # ensure watcher output dir picks up the label

    ts       = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
    safe     = _safe_label(label)
    filename = f"{safe}_{ts}.manifest.json"
    dest     = watcher_dir / filename

    try:
        dest.write_text(json.dumps(manifest, indent=4), encoding="utf-8")
        log.info(
            "Scheduler: materialised manifest for %r → %s",
            schedule_name, dest.name,
        )
        return True
    except OSError as exc:
        log.error(
            "Scheduler: could not write manifest %s — %s", dest, exc
        )
        return False


# ── Main scheduler loop ──────────────────────────────────────────────────────

async def run_scheduler_loop(
    schedule_dir: str,
    watcher_dir: str,
    poll_interval: int = 60,
) -> None:
    """
    Async polling loop.

    Every poll_interval seconds, scans schedule_dir for *.schedule.json
    files and materialises a *.manifest.json into watcher_dir for any
    whose cron expression matches the current wall-clock minute.

    A schedule fires at most once per minute per schedule file — duplicate
    materialisation within the same minute is suppressed by tracking the
    last (hour, minute) pair at which each schedule fired.

    The loop is intended to run as a long-lived asyncio.Task.  Cancellation
    is handled cleanly (CancelledError exits the loop).  All other exceptions
    are caught so a bug in one schedule file or tick never brings down the
    server.
    """
    sched_path   = Path(schedule_dir)
    watcher_path = Path(watcher_dir)

    log.info(
        "Scheduler: started — schedule_dir=%s watcher_dir=%s poll_interval=%ds",
        sched_path, watcher_path, poll_interval,
    )

    # last_fired maps schedule_stem → (hour, minute) of the last fire
    last_fired: dict[str, tuple[int, int]] = {}

    while True:
        try:
            await _tick(sched_path, watcher_path, last_fired)
        except asyncio.CancelledError:
            log.info("Scheduler: loop cancelled — shutting down.")
            return
        except Exception as exc:  # broad catch intentional — scheduler must not crash the server
            log.error(
                "Scheduler: unexpected error in tick — %s", exc, exc_info=True
            )

        try:
            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            log.info("Scheduler: loop cancelled during sleep — shutting down.")
            return


async def _tick(
    sched_path: Path,
    watcher_path: Path,
    last_fired: dict[str, tuple[int, int]],
) -> None:
    """
    Single scheduler tick.

    Reads all *.schedule.json files in sched_path and fires any whose
    cron expression matches the current minute and has not already fired
    this minute.
    """
    if not sched_path.exists():
        log.debug(
            "Scheduler: schedule_dir %s does not exist yet — waiting",
            sched_path,
        )
        return

    schedule_files = sorted(sched_path.glob("*.schedule.json"))
    if not schedule_files:
        log.debug(
            "Scheduler: no *.schedule.json files found in %s", sched_path
        )
        return

    for path in schedule_files:
        # Derive stem by stripping the full ".schedule.json" suffix
        name = path.name
        if name.endswith(".schedule.json"):
            stem = name[: -len(".schedule.json")]
        else:
            stem = path.stem

        schedule = _read_schedule(path)
        if schedule is None:
            continue   # error already logged

        tz_name = schedule.get("timezone")
        now     = _now_in_tz(tz_name)

        # Duplicate-fire guard: if we already fired this schedule this exact
        # minute, skip — prevents double-materialisation when poll_interval
        # is shorter than a minute or the server is catching up after a pause.
        hm = (now.hour, now.minute)
        if last_fired.get(stem) == hm:
            log.debug(
                "Scheduler: %s already fired at %02d:%02d — skipping",
                stem, now.hour, now.minute,
            )
            continue

        cron_expr = schedule["cron"]
        try:
            matches = _cron_matches(cron_expr, now)
        except ValueError as exc:
            log.error("Scheduler: %s cron error — %s", stem, exc)
            continue

        if not matches:
            log.debug(
                "Scheduler: %s — cron %r does not match %02d:%02d",
                stem, cron_expr, now.hour, now.minute,
            )
            continue

        log.info(
            "Scheduler: %s — cron %r fired at %s (tz=%s)",
            stem, cron_expr, now.strftime("%Y-%m-%d %H:%M"), tz_name or "UTC",
        )

        if _materialise(stem, schedule, watcher_path):
            last_fired[stem] = hm
