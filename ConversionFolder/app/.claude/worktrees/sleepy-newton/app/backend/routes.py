"""
FastAPI routes — REST API for the Informatica Conversion Tool.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
import re
import time
import uuid as _uuid_mod
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, File, UploadFile, HTTPException, BackgroundTasks, Form, Request
from starlette.background import BackgroundTask
from fastapi.responses import StreamingResponse, JSONResponse, PlainTextResponse
from typing import Optional as _Opt

from .db import database as db
from .limiter import jobs_limiter
from .security_knowledge import record_findings, knowledge_base_stats
from .models.schemas import (
    SignOffRecord, SignOffRequest, ReviewDecision, JobStatus,
    CodeSignOffRequest, CodeSignOffRecord, CodeReviewDecision,
    SecuritySignOffRecord, SecuritySignOffRequest, SecurityReviewDecision,
    BatchStatus,
)
from . import orchestrator
from .logger import read_job_log, read_job_log_raw, job_log_path, list_log_registry
from .agents.s2t_agent import s2t_excel_path
from .security import validate_upload_size, ZipExtractionError
from .zip_extractor import extract_informatica_zip, extract_batch_zip
from .job_exporter import build_output_zip

router = APIRouter(prefix="/api")
logger = logging.getLogger("conversion.routes")

_ROUTE_START_TIME = time.monotonic()
_VERSION = "2.6.1"

# ── Security helpers ────────────────────────────────────────────────────────

_UUID_RE = re.compile(
    r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$",
    re.IGNORECASE,
)

# Allowed MIME types for file uploads (enforced in addition to extension checks)
_ALLOWED_XML_CONTENT_TYPES = {
    "text/xml", "application/xml", "text/plain",
    "application/octet-stream",   # some clients send this for .xml
}
_ALLOWED_ZIP_CONTENT_TYPES = {
    "application/zip", "application/x-zip-compressed",
    "application/octet-stream",
}


def _validate_job_id(job_id: str) -> str:
    """FastAPI dependency — raise 400 if job_id is not a valid UUID.

    Prevents path-traversal/injection attacks using crafted job_id strings.
    Usage: job_id: str
    """
    if not _UUID_RE.match(job_id):
        raise HTTPException(status_code=400, detail="Invalid job_id format.")
    return job_id


def _validate_xml_content_type(upload: UploadFile) -> None:
    """Reject uploads whose declared content-type is clearly not XML/text."""
    ct = (upload.content_type or "").split(";")[0].strip().lower()
    if ct and ct not in _ALLOWED_XML_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported content type '{ct}' — expected XML or plain text.",
        )


def _validate_zip_content_type(upload: UploadFile) -> None:
    """Reject uploads whose declared content-type is clearly not a ZIP archive."""
    ct = (upload.content_type or "").split(";")[0].strip().lower()
    if ct and ct not in _ALLOWED_ZIP_CONTENT_TYPES:
        raise HTTPException(
            status_code=415,
            detail=f"Unsupported content type '{ct}' — expected a ZIP archive.",
        )

# ── Active pipeline tasks (in-memory for MVP) ─────
_active_tasks: dict[str, asyncio.Task] = {}
_progress_queues: dict[str, asyncio.Queue] = {}


# ─────────────────────────────────────────────
# Health Check
# ─────────────────────────────────────────────

@router.get("/health")
async def health_check():
    """
    Liveness + readiness probe.

    Returns 200 when the application and database are healthy.
    Returns 503 when the database is unreachable.
    Used by load balancers, Docker HEALTHCHECK, and uptime monitors.
    """
    import aiosqlite
    import time
    db_status = "ok"
    try:
        async with aiosqlite.connect(db.DB_PATH) as conn:
            await conn.execute("SELECT 1")
    except Exception as exc:
        db_status = f"error: {exc}"

    uptime = round(time.monotonic() - _ROUTE_START_TIME, 1)
    payload = {
        "status": "ok" if db_status == "ok" else "degraded",
        "version": _VERSION,
        "db": db_status,
        "uptime_seconds": uptime,
    }
    status_code = 200 if db_status == "ok" else 503
    return JSONResponse(content=payload, status_code=status_code)


# ─────────────────────────────────────────────
# Upload + Start
# ─────────────────────────────────────────────

@router.post("/jobs")
async def create_job(
    file:           UploadFile = File(...),
    workflow_file:  _Opt[UploadFile] = File(default=None),
    parameter_file: _Opt[UploadFile] = File(default=None),
    _rl:            None = Depends(jobs_limiter),
):
    """Upload files and start the conversion pipeline.

    Required
    --------
    file            Informatica Mapping XML (.xml)

    Optional (v1.1)
    ---------------
    workflow_file   Informatica Workflow XML (.xml) — enables Step 0 session extraction
    parameter_file  Informatica parameter file (.txt / .par) — enables $$VAR resolution
    """
    if not file.filename.lower().endswith(".xml"):
        raise HTTPException(400, "Mapping file must be a .xml Informatica export")
    _validate_xml_content_type(file)

    mapping_content = await file.read()
    validate_upload_size(mapping_content, label=file.filename)

    # Validate the file is non-empty and looks like XML before doing anything else
    if not mapping_content:
        raise HTTPException(400, "Uploaded mapping file is empty.")
    xml_str = mapping_content.decode("utf-8", errors="replace").strip()
    if not xml_str:
        raise HTTPException(400, "Uploaded mapping file is empty after decoding.")
    if not xml_str.lstrip().startswith("<"):
        raise HTTPException(400, "Uploaded file does not appear to be valid XML — "
                               "it must start with an XML element or declaration.")

    workflow_str: _Opt[str] = None
    if workflow_file and workflow_file.filename:
        wf_content = await workflow_file.read()
        validate_upload_size(wf_content, label=workflow_file.filename)
        workflow_str = wf_content.decode("utf-8", errors="replace")
        logger.info("Workflow file uploaded: filename=%s size=%d bytes",
                    workflow_file.filename, len(wf_content))

    param_str: _Opt[str] = None
    if parameter_file and parameter_file.filename:
        pf_content = await parameter_file.read()
        validate_upload_size(pf_content, label=parameter_file.filename)
        param_str = pf_content.decode("utf-8", errors="replace")
        logger.info("Parameter file uploaded: filename=%s size=%d bytes",
                    parameter_file.filename, len(pf_content))

    job_id = await db.create_job(
        file.filename,
        xml_str,
        workflow_xml_content=workflow_str,
        parameter_file_content=param_str,
    )

    logger.info("Job created: job_id=%s filename=%s size=%d bytes has_workflow=%s has_params=%s",
                job_id, file.filename, len(mapping_content),
                workflow_str is not None, param_str is not None)

    queue: asyncio.Queue = asyncio.Queue()
    _progress_queues[job_id] = queue

    async def _run():
        async for progress in orchestrator.run_pipeline(job_id, file.filename):
            await queue.put(progress)
        await queue.put(None)  # sentinel

    task = asyncio.create_task(_run())
    _active_tasks[job_id] = task

    return {
        "job_id":        job_id,
        "filename":      file.filename,
        "has_workflow":  workflow_str is not None,
        "has_params":    param_str is not None,
        "status":        "started",
    }


# ─────────────────────────────────────────────
# SSE Progress Stream
# ─────────────────────────────────────────────

@router.get("/jobs/{job_id}/stream")
async def stream_progress(job_id: str):
    """Server-Sent Events stream for real-time pipeline progress."""
    job = await db.get_job(job_id)
    if not job:
        _validate_job_id(job_id)
        raise HTTPException(404, "Job not found")

    async def event_generator() -> AsyncGenerator[str, None]:
        queue = _progress_queues.get(job_id)

        if queue:
            while True:
                try:
                    item = await asyncio.wait_for(queue.get(), timeout=60.0)
                    if item is None:
                        yield f"data: {json.dumps({'type': 'done'})}\n\n"
                        break
                    yield f"data: {json.dumps({'type': 'progress', **item})}\n\n"
                except asyncio.TimeoutError:
                    yield f"data: {json.dumps({'type': 'heartbeat'})}\n\n"
        else:
            current = await db.get_job(job_id)
            yield f"data: {json.dumps({'type': 'state', 'status': current['status'], 'step': current['current_step']})}\n\n"
            yield f"data: {json.dumps({'type': 'done'})}\n\n"

    async def _cleanup():
        # GAP #11 — release the queue once the stream is done to prevent memory leak
        _progress_queues.pop(job_id, None)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        background=BackgroundTask(_cleanup),
    )


# ─────────────────────────────────────────────
# Job State
# ─────────────────────────────────────────────

@router.get("/jobs")
async def list_jobs(page: int = 1, page_size: int = 20):
    """List jobs newest-first with pagination.

    Query params:
      page      — 1-based page number (default 1)
      page_size — jobs per page (default 20, max 100)
    """
    page_size = min(max(page_size, 1), 100)
    page      = max(page, 1)
    offset    = (page - 1) * page_size
    jobs      = await db.list_jobs(limit=page_size, offset=offset)
    total     = await db.count_jobs()
    return {
        "jobs":      jobs,
        "total":     total,
        "page":      page,
        "page_size": page_size,
        "pages":     max(1, -(-total // page_size)),   # ceiling division
    }


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    """Soft-delete a job (sets deleted_at; preserves DB record and log file)."""
    from .agents.s2t_agent import s2t_excel_path

    flagged = await db.delete_job(job_id)
    if not flagged:
        _validate_job_id(job_id)
        raise HTTPException(404, "Job not found or already deleted")

    # S2T Excel is an intermediate artefact — still clean it up.
    # Log file and registry entry are kept so the job appears in Log Archive.
    cleaned = []
    s2t_path = s2t_excel_path(job_id)
    if s2t_path and s2t_path.exists():
        try:
            s2t_path.unlink()
            cleaned.append("s2t")
        except OSError:
            pass

    return {"flagged_deleted": True, "job_id": job_id, "cleaned": cleaned}


@router.get("/jobs/{job_id}")
async def get_job(job_id: str):
    job = await db.get_job(job_id)
    if not job:
        _validate_job_id(job_id)
        raise HTTPException(404, "Job not found")
    return {
        "job_id":       job["job_id"],
        "filename":     job["filename"],
        "status":       job["status"],
        "current_step": job["current_step"],
        "created_at":   job["created_at"],
        "updated_at":   job["updated_at"],
        "state":        job["state"],
    }


# ─────────────────────────────────────────────
# Job Logs
# ─────────────────────────────────────────────

@router.get("/jobs/{job_id}/logs")
async def get_job_logs(job_id: str, format: str = "json"):
    """
    Return the full log for a job.
    ?format=json  → JSON array of log entries (default)
    ?format=text  → Human-readable plain text (one line per entry)
    """
    job = await db.get_job(job_id)
    if not job:
        _validate_job_id(job_id)
        raise HTTPException(404, "Job not found")

    entries = read_job_log(job_id)

    if format == "text":
        lines = []
        for e in entries:
            ts   = e.get("ts", "")[:19].replace("T", " ")
            lvl  = e.get("level", "INFO").ljust(8)
            step = f"[step {e['step']}]" if e.get("step") is not None else "         "
            msg  = e.get("message", "")
            data = e.get("data")
            line = f"{ts} {lvl} {step} {msg}"
            if data:
                line += f"  |  {json.dumps(data)}"
            if e.get("exc"):
                line += f"\n{e['exc']}"
            lines.append(line)
        return PlainTextResponse("\n".join(lines))

    return JSONResponse({"job_id": job_id, "entries": entries, "count": len(entries)})


@router.get("/jobs/{job_id}/logs/download")
async def download_job_log(job_id: str):
    """Download the raw JSONL log file for a job (meaningful filename)."""
    job = await db.get_job(job_id)
    if not job:
        _validate_job_id(job_id)
        raise HTTPException(404, "Job not found")

    path = job_log_path(job_id)
    if not path or not path.exists():
        raise HTTPException(404, "Log file not found — job may not have started yet")

    content = path.read_text(encoding="utf-8")
    return PlainTextResponse(
        content,
        headers={"Content-Disposition": f'attachment; filename="{path.name}"'},
    )


# ─────────────────────────────────────────────
# Log Registry
# ─────────────────────────────────────────────

@router.get("/logs/registry")
async def get_log_registry():
    """Return the log registry — all jobs with their log filenames and final status."""
    return {"registry": list_log_registry()}


@router.get("/logs/history")
async def get_log_history():
    """Return archived jobs: soft-deleted DB records + orphaned registry entries."""
    from .logger import list_orphaned_registry_entries
    # Live (non-deleted) jobs — excluded from archive
    live_jobs    = await db.list_jobs()
    live_ids     = {j["job_id"] for j in live_jobs}
    # Soft-deleted jobs (still in DB, flagged deleted_at)
    deleted_jobs = await db.list_deleted_jobs()
    deleted_ids  = {j["job_id"] for j in deleted_jobs}
    # Normalise deleted DB rows to the same shape as registry entries
    deleted_entries = []
    for j in deleted_jobs:
        mn = j.get("mapping_name")
        if isinstance(mn, dict):
            mn = None
        deleted_entries.append({
            "job_id":       j["job_id"],
            "xml_filename": j["filename"],
            "mapping_name": mn or j["filename"],
            "status":       j["status"],
            "started_at":   j["created_at"],
            "deleted_at":   j.get("deleted_at"),
            "log_readable": True,
        })
    # Orphaned registry entries (not in DB at all)
    all_known_ids = live_ids | deleted_ids
    orphans = list_orphaned_registry_entries(all_known_ids)
    # Merge: deleted DB jobs first (most recent), then orphans
    history = deleted_entries + orphans
    history.sort(key=lambda e: e.get("deleted_at") or e.get("started_at", ""), reverse=True)
    return {"history": history}


@router.get("/logs/history/{job_id}")
async def get_history_log(job_id: str):
    """Read the log file for a historical (DB-orphaned) job."""
    from .logger import read_job_log, job_log_path
    path = job_log_path(job_id)
    if not path:
        _validate_job_id(job_id)
        raise HTTPException(404, "Log file not found")
    entries = read_job_log(job_id)
    return JSONResponse({"job_id": job_id, "entries": entries, "count": len(entries)})


# ─────────────────────────────────────────────
# Human Sign-off (Step 5 gate)
# ─────────────────────────────────────────────

@router.post("/jobs/{job_id}/sign-off")
async def submit_signoff(job_id: str, payload: SignOffRequest):
    """Submit human review decision. If APPROVED, resumes pipeline."""
    job = await db.get_job(job_id)
    if not job:
        _validate_job_id(job_id)
        raise HTTPException(404, "Job not found")
    if job["status"] != JobStatus.AWAITING_REVIEW.value:
        raise HTTPException(400, f"Job is not awaiting review (status: {job['status']})")

    sign_off = SignOffRecord(
        reviewer_name=payload.reviewer_name,
        reviewer_role=payload.reviewer_role,
        review_date=__import__("datetime").datetime.utcnow().isoformat(),
        blocking_resolved=[],
        flags_accepted=[r for r in payload.flag_resolutions if r.action == "accepted"],
        flags_resolved=[r for r in payload.flag_resolutions if r.action == "resolved"],
        decision=payload.decision,
        notes=payload.notes,
    )

    logger.info("Sign-off received: job_id=%s decision=%s reviewer=%s",
                job_id, payload.decision, payload.reviewer_name)

    await db.update_job(job_id, JobStatus.AWAITING_REVIEW.value, 5,
                        {"sign_off": sign_off.model_dump()})

    # GAP #17 — immutable audit record for Gate 1 decision
    await db.add_audit_entry(
        job_id=job_id,
        gate="gate1",
        event_type=payload.decision.lower(),
        reviewer_name=payload.reviewer_name,
        reviewer_role=payload.reviewer_role,
        decision=payload.decision,
        notes=payload.notes,
    )

    if payload.decision == ReviewDecision.REJECTED:
        await db.update_job(job_id, JobStatus.BLOCKED.value, 5, {})
        logger.info("Job rejected: job_id=%s", job_id)
        return {"message": "Job rejected. Pipeline will not proceed."}

    # APPROVED — resume pipeline in background
    queue: asyncio.Queue = asyncio.Queue()
    _progress_queues[job_id] = queue

    state    = job["state"]
    filename = job["filename"]

    async def _resume():
        async for progress in orchestrator.resume_after_signoff(job_id, state, filename):
            await queue.put(progress)
        await queue.put(None)

    task = asyncio.create_task(_resume())
    _active_tasks[job_id] = task
    logger.info("Pipeline resuming after approval: job_id=%s", job_id)

    return {"message": "Sign-off accepted. Pipeline resuming from Step 6.", "job_id": job_id}


# ─────────────────────────────────────────────
# Security Review Sign-off (Step 9 gate)
# ─────────────────────────────────────────────

@router.post("/jobs/{job_id}/security-review")
async def submit_security_review(job_id: str, payload: SecuritySignOffRequest):
    """
    Submit human security review decision (Gate 2 — Step 9).
    APPROVED / ACKNOWLEDGED  → resume pipeline from Step 10.
    REQUEST_FIX              → re-run Steps 7-8 with findings as fix context, re-present Gate 2.
    FAILED                   → block the job permanently.
    """
    job = await db.get_job(job_id)
    if not job:
        _validate_job_id(job_id)
        raise HTTPException(404, "Job not found")
    if job["status"] != JobStatus.AWAITING_SEC_REVIEW.value:
        raise HTTPException(400, f"Job is not awaiting security review (status: {job['status']})")

    state    = job["state"]
    filename = job["filename"]

    # Determine remediation round — increments each time REQUEST_FIX is chosen
    prev_round = state.get("remediation_round", 0)
    this_round = prev_round + 1 if payload.decision == SecurityReviewDecision.REQUEST_FIX else prev_round

    sec_signoff = SecuritySignOffRecord(
        reviewer_name=payload.reviewer_name,
        reviewer_role=payload.reviewer_role,
        review_date=__import__("datetime").datetime.utcnow().isoformat() + "Z",
        decision=payload.decision,
        notes=payload.notes,
        remediation_round=prev_round,
    )

    logger.info("Security review received: job_id=%s decision=%s reviewer=%s round=%d",
                job_id, payload.decision, payload.reviewer_name, prev_round)

    await db.update_job(job_id, JobStatus.AWAITING_SEC_REVIEW.value, 9,
                        {"security_sign_off": sec_signoff.model_dump()})

    # GAP #17 — immutable audit record for Gate 2 decision
    await db.add_audit_entry(
        job_id=job_id,
        gate="gate2",
        event_type=payload.decision.lower(),
        reviewer_name=payload.reviewer_name,
        reviewer_role=payload.reviewer_role,
        decision=payload.decision,
        notes=payload.notes,
        extra={"remediation_round": prev_round},
    )

    if payload.decision == SecurityReviewDecision.FAILED:
        await db.update_job(job_id, JobStatus.BLOCKED.value, 9, {})
        logger.info("Security review failed — job blocked: job_id=%s", job_id)
        return {
            "message": "Security review failed. Job is blocked — pipeline will not proceed.",
            "job_id": job_id,
            "decision": payload.decision,
        }

    if payload.decision == SecurityReviewDecision.REQUEST_FIX:
        # Re-run Steps 7-8 with security findings injected, then re-pause at Gate 2
        queue: asyncio.Queue = asyncio.Queue()
        _progress_queues[job_id] = queue

        state["security_sign_off"] = sec_signoff.model_dump()
        state["remediation_round"] = this_round

        async def _fix_and_rescan():
            async for progress in orchestrator.resume_after_security_fix_request(
                job_id, state, filename, remediation_round=this_round
            ):
                await queue.put(progress)
            await queue.put(None)

        task = asyncio.create_task(_fix_and_rescan())
        _active_tasks[job_id] = task
        logger.info("Pipeline re-running Steps 7-8 for security fix: job_id=%s round=%d",
                    job_id, this_round)
        return {
            "message": f"Security fix requested (round {this_round}). Regenerating code and re-scanning.",
            "job_id": job_id,
            "decision": payload.decision,
            "remediation_round": this_round,
        }

    # APPROVED or ACKNOWLEDGED — capture findings into the security knowledge base,
    # then resume pipeline from Step 10
    queue = asyncio.Queue()
    _progress_queues[job_id] = queue

    state["security_sign_off"] = sec_signoff.model_dump()

    # ── Record findings in the knowledge base so future jobs learn from them ──
    scan = state.get("security_scan") or {}
    findings = scan.get("findings") if isinstance(scan, dict) else []
    if findings:
        try:
            n = record_findings(job_id, findings)
            logger.info("Security KB: recorded %d pattern(s) from job %s", n, job_id)
        except Exception as kb_err:
            logger.warning("Security KB: failed to record findings: %s", kb_err)

    async def _resume():
        async for progress in orchestrator.resume_after_security_review(job_id, state, filename):
            await queue.put(progress)
        await queue.put(None)

    task = asyncio.create_task(_resume())
    _active_tasks[job_id] = task
    logger.info("Pipeline resuming after security review: job_id=%s decision=%s",
                job_id, payload.decision)

    return {
        "message": f"Security review recorded ({payload.decision}). Pipeline resuming from Step 10.",
        "job_id": job_id,
        "decision": payload.decision,
    }


# ─────────────────────────────────────────────
# Code Review Sign-off (Step 12 gate)
# ─────────────────────────────────────────────

@router.post("/jobs/{job_id}/code-signoff")
async def submit_code_signoff(job_id: str, payload: CodeSignOffRequest):
    """
    Submit code review decision (Gate 3 — Step 12).
      APPROVED → mark job COMPLETE.
      REJECTED → block the job permanently.
    """
    job = await db.get_job(job_id)
    if not job:
        _validate_job_id(job_id)
        raise HTTPException(404, "Job not found")
    if job["status"] != JobStatus.AWAITING_CODE_REVIEW.value:
        raise HTTPException(400, f"Job is not awaiting code review (status: {job['status']})")

    code_signoff = CodeSignOffRecord(
        reviewer_name=payload.reviewer_name,
        reviewer_role=payload.reviewer_role,
        review_date=__import__("datetime").datetime.utcnow().isoformat(),
        decision=payload.decision,
        notes=payload.notes,
    )

    logger.info("Code sign-off received: job_id=%s decision=%s reviewer=%s",
                job_id, payload.decision, payload.reviewer_name)

    await db.update_job(job_id, JobStatus.AWAITING_CODE_REVIEW.value, 12,
                        {"code_sign_off": code_signoff.model_dump()})

    # GAP #17 — immutable audit record for Gate 3 decision
    await db.add_audit_entry(
        job_id=job_id,
        gate="gate3",
        event_type=payload.decision.lower(),
        reviewer_name=payload.reviewer_name,
        reviewer_role=payload.reviewer_role,
        decision=payload.decision,
        notes=payload.notes,
    )

    # REJECTED — block the job immediately
    if payload.decision == CodeReviewDecision.REJECTED:
        await db.update_job(job_id, JobStatus.BLOCKED.value, 12, {})
        logger.info("Code review rejected: job_id=%s reviewer=%s",
                    job_id, payload.reviewer_name)
        return {
            "message": (
                "Code review rejected. Job is blocked — upload the mapping again "
                "to start a fresh conversion."
            ),
            "job_id":   job_id,
            "decision": payload.decision,
        }

    # APPROVED — resume to write COMPLETE status
    queue: asyncio.Queue = asyncio.Queue()
    _progress_queues[job_id] = queue

    state    = job["state"]
    state["code_sign_off"] = code_signoff.model_dump()
    filename = job["filename"]

    async def _resume():
        async for progress in orchestrator.resume_after_code_signoff(job_id, state, filename):
            await queue.put(progress)
        await queue.put(None)

    task = asyncio.create_task(_resume())
    _active_tasks[job_id] = task
    logger.info("Pipeline resuming after code sign-off: job_id=%s decision=%s",
                job_id, payload.decision)

    return {
        "message": f"Code sign-off recorded ({payload.decision}). Pipeline resuming.",
        "job_id": job_id,
        "decision": payload.decision,
    }


# ─────────────────────────────────────────────
# Audit trail  (GAP #17)
# ─────────────────────────────────────────────

@router.get("/jobs/{job_id}/audit")
async def get_job_audit(job_id: str):
    """Return all gate-decision audit entries for a job, oldest first.

    Each entry contains: audit_id, gate (gate1/gate2/gate3), event_type,
    _validate_job_id(job_id)
    reviewer_name, reviewer_role, decision, notes, extra, created_at.
    """
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    entries = await db.get_audit_log(job_id)
    return {"job_id": job_id, "entries": entries}


# ─────────────────────────────────────────────
# Download converted code
# ─────────────────────────────────────────────

@router.get("/jobs/{job_id}/s2t/download")
async def download_s2t_excel(job_id: str):
    """Download the Source-to-Target mapping Excel workbook for a job."""
    job = await db.get_job(job_id)
    if not job:
        _validate_job_id(job_id)
        raise HTTPException(404, "Job not found")

    path = s2t_excel_path(job_id)
    if not path or not path.exists():
        raise HTTPException(404, "S2T Excel file not found — the job may not have completed Step 2 yet")

    from fastapi.responses import FileResponse
    return FileResponse(
        str(path),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        filename=path.name,
    )


@router.get("/jobs/{job_id}/manifest.xlsx")
async def download_manifest_xlsx(job_id: str):
    """
    Generate and return the pre-conversion mapping manifest xlsx on demand.
    The manifest is NOT stored in state (too large); it is regenerated from the
    graph dict each time this endpoint is called.
    """
    job = await db.get_job(job_id)
    if not job:
        _validate_job_id(job_id)
        raise HTTPException(404, "Job not found")
    graph = job.get("state", {}).get("graph")
    if not graph:
        raise HTTPException(404, "Manifest not available — job has not completed parsing yet")

    from .agents import manifest_agent
    import io as _io
    report = manifest_agent.build_manifest(graph)
    xlsx_bytes = manifest_agent.write_xlsx_bytes(report)
    safe = job.get("filename", "mapping").replace(".xml", "").replace(" ", "_")
    return StreamingResponse(
        _io.BytesIO(xlsx_bytes),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="manifest_{safe}.xlsx"'},
    )


@router.post("/jobs/{job_id}/manifest-upload")
async def upload_manifest_overrides(job_id: str, file: UploadFile = File(...)):
    """
    Accept a reviewer-annotated manifest xlsx and store the overrides in job state.

    The reviewer downloads the manifest via GET /jobs/{job_id}/manifest.xlsx,
    fills in the 'Reviewer Override' column on the 'Review Required' sheet for any
    LOW or UNMAPPED rows, then re-uploads the annotated file here.

    Must be called while the job is at Gate 1 (awaiting_review).
    The conversion agent picks up the stored overrides when the pipeline resumes
    after sign-off, resolving lineage gaps before generating code.
    """
    job = await db.get_job(job_id)
    if not job:
        _validate_job_id(job_id)
        raise HTTPException(404, "Job not found")

    if job["status"] != JobStatus.AWAITING_REVIEW.value:
        raise HTTPException(
            400,
            f"Manifest overrides can only be uploaded while the job is awaiting review "
            f"(current status: {job['status']}). Download the manifest, annotate it, "
            f"then upload it before submitting your sign-off.",
        )

    fname = (file.filename or "").lower()
    if not fname.endswith(".xlsx"):
        raise HTTPException(400, "Manifest file must be a .xlsx file")

    xlsx_bytes = await file.read()
    validate_upload_size(xlsx_bytes, label=file.filename)

    if not xlsx_bytes:
        raise HTTPException(400, "Uploaded manifest file is empty")

    # Parse overrides from the annotated xlsx.
    # load_overrides() takes a file path, so write to a temp file first.
    from .agents import manifest_agent
    import tempfile as _tempfile
    import os as _os

    with _tempfile.NamedTemporaryFile(suffix=".xlsx", delete=False) as tmp:
        tmp.write(xlsx_bytes)
        tmp_path = tmp.name

    try:
        overrides = manifest_agent.load_overrides(tmp_path)
    finally:
        _os.unlink(tmp_path)

    overrides_dicts = [o.model_dump() for o in overrides]

    # Store overrides in job state — conversion agent reads state["manifest_overrides"]
    # at Step 6 (resume_after_signoff) and injects them into the conversion prompt.
    await db.update_job(
        job_id,
        JobStatus.AWAITING_REVIEW.value,
        5,
        {"manifest_overrides": overrides_dicts},
    )

    logger.info(
        "Manifest overrides uploaded: job_id=%s override_count=%d",
        job_id, len(overrides_dicts),
    )

    return {
        "message": f"Manifest uploaded successfully. {len(overrides_dicts)} override(s) stored.",
        "job_id": job_id,
        "override_count": len(overrides_dicts),
        "overrides": overrides_dicts,
    }


@router.get("/jobs/{job_id}/download/{filename}")
async def download_file(job_id: str, filename: str):
    job = await db.get_job(job_id)
    if not job:
        _validate_job_id(job_id)
        raise HTTPException(404, "Job not found")
    conversion = job["state"].get("conversion", {})
    files = conversion.get("files", {})
    if filename not in files:
        raise HTTPException(404, f"File '{filename}' not found in conversion output")

    # GAP #14 — Validate the filename is safe before serving
    # Reject path traversal attempts and non-whitelisted extensions
    import pathlib
    _safe_name = pathlib.PurePosixPath(filename).name  # strip any directory components
    _ALLOWED_EXTS = {".py", ".sql", ".yaml", ".yml", ".txt", ".md", ".json", ".sh", ".cfg", ".ini", ".toml"}
    _ext = pathlib.PurePosixPath(_safe_name).suffix.lower()
    if _ext not in _ALLOWED_EXTS:
        logger.warning("Blocked download of disallowed extension: job=%s filename=%s", job_id, filename)
        raise HTTPException(400, f"File extension '{_ext}' is not permitted for download.")

    return JSONResponse({"filename": filename, "content": files[filename]})


@router.get("/jobs/{job_id}/tests/download/{filename:path}")
async def download_test_file(job_id: str, filename: str):
    """Download a generated test file by path (e.g. tests/test_conversion.py)."""
    job = await db.get_job(job_id)
    if not job:
        _validate_job_id(job_id)
        raise HTTPException(404, "Job not found")
    test_report = job["state"].get("test_report", {})
    files = test_report.get("test_files", {})
    if filename not in files:
        raise HTTPException(404, f"Test file '{filename}' not found")
    return JSONResponse({"filename": filename, "content": files[filename]})


# ─────────────────────────────────────────────
# Output ZIP Download (v2.5.0)
# ─────────────────────────────────────────────

@router.get("/jobs/{job_id}/output.zip")
async def download_output_zip(job_id: str):
    """
    Download all generated conversion output files as a ZIP archive.

    Bundles every file from state["conversion"]["files"] into a single
    ZIP preserving folder structure.  Built directly from DB state so it
    works regardless of whether the job folder has been written to disk.

    Only available for jobs that have reached AWAITING_CODE_REVIEW or
    COMPLETE status (i.e. conversion has run).
    """
    job = await db.get_job(job_id)
    if not job:
        _validate_job_id(job_id)
        raise HTTPException(404, "Job not found")

    state = job.get("state", {})
    conversion = state.get("conversion", {})
    files = conversion.get("files", {})
    if not files:
        raise HTTPException(404, "No output files found — conversion has not completed for this job.")

    zip_bytes = build_output_zip(state)
    mapping_name = conversion.get("mapping_name", job_id)
    safe_name = "".join(c if c.isalnum() or c in "-_" else "_" for c in mapping_name)
    filename = f"{safe_name}_output.zip"

    return StreamingResponse(
        iter([zip_bytes]),
        media_type="application/zip",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


# ─────────────────────────────────────────────
# ZIP Upload (v1.1+)
# ─────────────────────────────────────────────

@router.post("/jobs/zip")
async def create_job_from_zip(
    file: UploadFile = File(...),
    _rl:  None = Depends(jobs_limiter),
):
    """
    Upload a single ZIP archive containing Informatica export files and start
    the conversion pipeline.

    The ZIP may contain any combination of:
      - Mapping XML  (.xml with a <MAPPING> element)       — REQUIRED
      - Workflow XML (.xml with <WORKFLOW>/<SESSION>)       — optional
      - Parameter file (.txt / .par with $$VAR= lines)     — optional

    File types are auto-detected from content — filenames don't matter.
    The archive is protected against Zip Slip, Zip Bombs, and symlink attacks.

    Size limits (configurable via environment variables):
      MAX_UPLOAD_MB          — per-file limit for the ZIP itself (default 50 MB)
      MAX_ZIP_EXTRACTED_MB   — total extracted size limit (default 200 MB)
      MAX_ZIP_FILE_COUNT     — maximum entries in the archive (default 200)
    """
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "File must be a .zip archive")

    _validate_zip_content_type(file)
    zip_bytes = await file.read()
    validate_upload_size(zip_bytes, label=file.filename)

    try:
        extracted = extract_informatica_zip(zip_bytes)
    except ZipExtractionError as exc:
        raise HTTPException(400, str(exc))

    warnings = extracted.warnings
    if extracted.skipped:
        warnings = warnings + [
            f"Skipped {len(extracted.skipped)} unclassified entries: "
            + ", ".join(extracted.skipped[:5])
            + ("…" if len(extracted.skipped) > 5 else "")
        ]

    job_id = await db.create_job(
        extracted.mapping_filename or file.filename,
        extracted.mapping_xml,
        workflow_xml_content=extracted.workflow_xml,
        parameter_file_content=extracted.parameter_file,
    )

    logger.info(
        "ZIP job created: job_id=%s zip=%s mapping=%s workflow=%s params=%s",
        job_id, file.filename,
        extracted.mapping_filename, extracted.workflow_filename, extracted.param_filename,
    )

    queue: asyncio.Queue = asyncio.Queue()
    _progress_queues[job_id] = queue

    async def _run():
        async for progress in orchestrator.run_pipeline(
            job_id, extracted.mapping_filename or file.filename
        ):
            await queue.put(progress)
        await queue.put(None)

    task = asyncio.create_task(_run())
    _active_tasks[job_id] = task

    return {
        "job_id":            job_id,
        "source_zip":        file.filename,
        "mapping_filename":  extracted.mapping_filename,
        "workflow_filename": extracted.workflow_filename,
        "param_filename":    extracted.param_filename,
        "has_workflow":      extracted.workflow_xml is not None,
        "has_params":        extracted.parameter_file is not None,
        "warnings":          warnings,
        "status":            "started",
    }


# ─────────────────────────────────────────────
# Batch Upload (v2.0)
# ─────────────────────────────────────────────

# Semaphore: cap concurrent mapping pipelines to respect Claude API limits.
# Override with BATCH_CONCURRENCY env var (default: 3).
from .config import settings as _cfg
_BATCH_CONCURRENCY: int = _cfg.batch_concurrency
_batch_semaphore = asyncio.Semaphore(_BATCH_CONCURRENCY)


def _compute_batch_status(job_statuses: list[str]) -> str:
    """Derive a BatchStatus string from a list of individual job status strings."""
    if not job_statuses:
        return BatchStatus.FAILED.value
    terminal = {JobStatus.COMPLETE.value, JobStatus.FAILED.value, JobStatus.BLOCKED.value}
    complete_set = {JobStatus.COMPLETE.value}
    in_flight = [s for s in job_statuses if s not in terminal]
    if in_flight:
        return BatchStatus.RUNNING.value
    completed = [s for s in job_statuses if s in complete_set]
    if len(completed) == len(job_statuses):
        return BatchStatus.COMPLETE.value
    if completed:
        return BatchStatus.PARTIAL.value
    return BatchStatus.FAILED.value


@router.post("/jobs/batch")
async def create_batch_jobs(
    file: UploadFile = File(...),
    _rl:  None = Depends(jobs_limiter),
):
    """
    Upload a batch ZIP archive and start a parallel conversion pipeline for
    each mapping folder.

    Expected ZIP structure::

        batch.zip/
          mapping_a/
            mapping.xml         ← required
            workflow.xml        ← optional
            params.txt          ← optional
          mapping_b/
            mapping.xml
          ...

    Each mapping folder is processed as an independent job with the full 12-step
    pipeline and its own human review gates.  Up to 3 mappings run concurrently.
    """
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(400, "Batch upload must be a .zip archive")

    zip_bytes = await file.read()
    validate_upload_size(zip_bytes, label=file.filename)

    try:
        mapping_results = extract_batch_zip(zip_bytes)
    except ZipExtractionError as exc:
        raise HTTPException(400, str(exc))

    if not mapping_results:
        raise HTTPException(400, "No valid mapping folders found in the batch ZIP.")

    # GAP #4 — Create batch record + all jobs atomically in one transaction.
    # If any insertion fails, the whole batch is rolled back — no orphaned jobs.
    mappings_payload = [
        {
            "filename":       parsed.mapping_filename or file.filename,
            "xml":            parsed.mapping_xml,
            "workflow_xml":   parsed.workflow_xml,
            "parameter_file": parsed.parameter_file,
        }
        for parsed in mapping_results
    ]
    try:
        batch_id, job_ids = await db.create_batch_atomic(file.filename, mappings_payload)
    except Exception as exc:
        logger.error("Atomic batch creation failed: %s", exc, exc_info=True)
        raise HTTPException(500, f"Failed to create batch jobs: {exc}")

    logger.info(
        "Batch created (atomic): batch_id=%s source_zip=%s mapping_count=%d",
        batch_id, file.filename, len(mapping_results),
    )

    job_entries: list[dict] = []
    for job_id, parsed in zip(job_ids, mapping_results):
        mapping_fname = parsed.mapping_filename or file.filename
        queue: asyncio.Queue = asyncio.Queue()
        _progress_queues[job_id] = queue
        job_entries.append({"job_id": job_id, "filename": mapping_fname, "parsed": parsed})

    # Launch all pipelines concurrently (semaphore caps at 3 in-flight)
    # GAP #8 — Wrap in try/except/finally so the sentinel is always placed on the
    # queue even when the async generator or the semaphore acquisition itself raises.
    # Without this, an unexpected exception would leave the SSE stream hanging
    # indefinitely and the task exception would be silently discarded by asyncio.
    async def _run_with_semaphore(j_id: str, fname: str):
        try:
            async with _batch_semaphore:
                async for progress in orchestrator.run_pipeline(j_id, fname):
                    await _progress_queues[j_id].put(progress)
        except Exception as exc:  # noqa: BLE001
            logger.error(
                "Batch pipeline crashed unexpectedly: job_id=%s error=%s",
                j_id, exc, exc_info=True,
            )
            # Mark the job FAILED in the DB so the batch status rolls up correctly.
            try:
                await db.update_job(j_id, JobStatus.FAILED.value, -1,
                                    {"error": f"Batch runner crashed: {exc}"})
            except Exception:  # pragma: no cover
                logger.exception("Failed to mark crashed batch job as FAILED: job_id=%s", j_id)
            # Push a synthetic FAILED progress event so any open SSE stream closes cleanly.
            await _progress_queues[j_id].put(
                {"step": -1, "status": JobStatus.FAILED.value,
                 "message": f"Pipeline crashed: {exc}"}
            )
        finally:
            # Sentinel always placed — closes the SSE generator regardless of outcome.
            await _progress_queues[j_id].put(None)

    for entry in job_entries:
        task = asyncio.create_task(
            _run_with_semaphore(entry["job_id"], entry["filename"])
        )
        _active_tasks[entry["job_id"]] = task
        logger.info("Batch job started: batch_id=%s job_id=%s filename=%s",
                    batch_id, entry["job_id"], entry["filename"])

    return {
        "batch_id":      batch_id,
        "mapping_count": len(job_entries),
        "jobs": [{"job_id": e["job_id"], "filename": e["filename"]} for e in job_entries],
        "status":        "running",
    }


# ─────────────────────────────────────────────
# Security Knowledge Base (read-only inspection)
# ─────────────────────────────────────────────

@router.get("/security/knowledge")
async def get_security_knowledge():
    """
    Return a summary of the security knowledge base:
      - rules_count    — number of active standing rules
      - patterns_count — number of auto-learned patterns
      - top_patterns   — top 10 most-recurring patterns across all jobs
    """
    return knowledge_base_stats()


# ─────────────────────────────────────────────
# Batch routes
# ─────────────────────────────────────────────

@router.get("/batches/{batch_id}")
async def get_batch(batch_id: str):
    """
    Return the batch record and a summary of all its constituent jobs.

    Response includes:
      - batch_id, source_zip, mapping_count
      - status  — computed from job statuses: running / complete / partial / failed
      - jobs    — list of job summaries (job_id, filename, status, current_step, etc.)
    """
    batch = await db.get_batch(batch_id)
    if not batch:
        raise HTTPException(404, f"Batch '{batch_id}' not found")

    jobs = await db.get_batch_jobs(batch_id)
    batch["status"] = _compute_batch_status([j["status"] for j in jobs])
    batch["jobs"] = jobs
    return batch
