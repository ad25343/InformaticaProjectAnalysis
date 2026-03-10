"""
FastAPI routes — REST API for the Informatica Conversion Tool.
"""
from __future__ import annotations
import asyncio
import json
import logging
import os
from typing import AsyncGenerator

from fastapi import APIRouter, Depends, File, UploadFile, HTTPException, BackgroundTasks, Form, Request
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

router = APIRouter(prefix="/api")
logger = logging.getLogger("conversion.routes")

# ── Active pipeline tasks (in-memory for MVP) ─────
_active_tasks: dict[str, asyncio.Task] = {}
_progress_queues: dict[str, asyncio.Queue] = {}


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

    mapping_content = await file.read()
    validate_upload_size(mapping_content, label=file.filename)
    xml_str = mapping_content.decode("utf-8", errors="replace")

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

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ─────────────────────────────────────────────
# Job State
# ─────────────────────────────────────────────

@router.get("/jobs")
async def list_jobs():
    jobs = await db.list_jobs()
    return {"jobs": jobs}


@router.delete("/jobs/{job_id}")
async def delete_job(job_id: str):
    """Soft-delete a job (sets deleted_at; preserves DB record and log file)."""
    from .agents.s2t_agent import s2t_excel_path

    flagged = await db.delete_job(job_id)
    if not flagged:
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
# Download converted code
# ─────────────────────────────────────────────

@router.get("/jobs/{job_id}/s2t/download")
async def download_s2t_excel(job_id: str):
    """Download the Source-to-Target mapping Excel workbook for a job."""
    job = await db.get_job(job_id)
    if not job:
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


@router.get("/jobs/{job_id}/download/{filename}")
async def download_file(job_id: str, filename: str):
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    conversion = job["state"].get("conversion", {})
    files = conversion.get("files", {})
    if filename not in files:
        raise HTTPException(404, f"File '{filename}' not found in conversion output")
    return JSONResponse({"filename": filename, "content": files[filename]})


@router.get("/jobs/{job_id}/tests/download/{filename:path}")
async def download_test_file(job_id: str, filename: str):
    """Download a generated test file by path (e.g. tests/test_conversion.py)."""
    job = await db.get_job(job_id)
    if not job:
        raise HTTPException(404, "Job not found")
    test_report = job["state"].get("test_report", {})
    files = test_report.get("test_files", {})
    if filename not in files:
        raise HTTPException(404, f"Test file '{filename}' not found")
    return JSONResponse({"filename": filename, "content": files[filename]})


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
_BATCH_CONCURRENCY: int = int(os.environ.get("BATCH_CONCURRENCY", "3"))
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

    # Create a batch record
    batch_id = await db.create_batch(file.filename, len(mapping_results))
    logger.info(
        "Batch created: batch_id=%s source_zip=%s mapping_count=%d",
        batch_id, file.filename, len(mapping_results),
    )

    # Create all job records up-front so callers can track them immediately
    job_entries: list[dict] = []
    for parsed in mapping_results:
        mapping_fname = parsed.mapping_filename or file.filename
        job_id = await db.create_job(
            mapping_fname,
            parsed.mapping_xml,
            workflow_xml_content=parsed.workflow_xml,
            parameter_file_content=parsed.parameter_file,
            batch_id=batch_id,
        )
        queue: asyncio.Queue = asyncio.Queue()
        _progress_queues[job_id] = queue
        job_entries.append({"job_id": job_id, "filename": mapping_fname, "parsed": parsed})

    # Launch all pipelines concurrently (semaphore caps at 3 in-flight)
    async def _run_with_semaphore(j_id: str, fname: str):
        async with _batch_semaphore:
            async for progress in orchestrator.run_pipeline(j_id, fname):
                await _progress_queues[j_id].put(progress)
            await _progress_queues[j_id].put(None)  # sentinel

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
