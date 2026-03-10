"""
InformaticaProjectAnalysis — FastAPI application.

Entry point: python -m app.main
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import uvicorn
from fastapi import FastAPI, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

from app.backend.agents.config_parser import ConfigParseError, parse_project_config
from app.backend.orchestrator import AnalysisOrchestrator

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_APP_DIR = Path(__file__).resolve().parent
_FRONTEND_DIR = _APP_DIR / "frontend"

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

app = FastAPI(
    title="InformaticaProjectAnalysis",
    version="0.1.0",
    description="Informatica PowerCenter project analysis and pattern grouping tool",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Tighten in production
    allow_methods=["*"],
    allow_headers=["*"],
)

orchestrator = AnalysisOrchestrator()


# ---------------------------------------------------------------------------
# Request / Response models
# ---------------------------------------------------------------------------

class FolderRequest(BaseModel):
    config_path: str


class JobSummary(BaseModel):
    job_id: str
    project_name: str
    status: str
    current_phase: int
    created_at: str
    updated_at: str
    error: str | None = None


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.get("/api/health")
def health():
    return {"status": "ok", "version": "0.1.0"}


@app.post("/api/projects/folder")
def start_analysis_from_folder(request: FolderRequest):
    """Start analysis from a project config YAML file path."""
    try:
        job = orchestrator.create_job_from_yaml(request.config_path)
    except ConfigParseError as e:
        raise HTTPException(status_code=400, detail=str(e))

    # Run analysis (synchronous for v0.1.0; async in later versions)
    try:
        orchestrator.run_analysis(job.job_id)
    except Exception as e:
        logger.error("Analysis failed: %s", e)
        # Job status already set to FAILED by orchestrator

    return _job_summary(job)


@app.get("/api/projects")
def list_projects():
    """List all analysis jobs."""
    jobs = orchestrator.list_jobs()
    return [_job_summary(j) for j in jobs]


@app.get("/api/projects/{job_id}")
def get_project(job_id: str):
    """Get analysis job state."""
    job = orchestrator.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")
    return _job_summary(job)


@app.get("/api/projects/{job_id}/strategy.json")
def get_strategy_json(job_id: str):
    """Download strategy JSON."""
    job = orchestrator.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    if job.strategy is None:
        raise HTTPException(
            status_code=400,
            detail=f"Strategy not yet generated. Job status: {job.status}",
        )

    return JSONResponse(content=job.strategy.model_dump())


@app.get("/api/projects/{job_id}/groups")
def get_pattern_groups(job_id: str):
    """Get pattern groups with members."""
    job = orchestrator.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    return [g.model_dump() for g in job.pattern_groups]


@app.get("/api/projects/{job_id}/groups/{group_id}")
def get_pattern_group(job_id: str, group_id: str):
    """Get a single pattern group detail."""
    job = orchestrator.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    for g in job.pattern_groups:
        if g.group_id == group_id:
            return g.model_dump()

    raise HTTPException(status_code=404, detail=f"Group not found: {group_id}")


@app.get("/api/projects/{job_id}/graph")
def get_dependency_graph(job_id: str):
    """Get dependency graph data."""
    job = orchestrator.get_job(job_id)
    if job is None:
        raise HTTPException(status_code=404, detail=f"Job not found: {job_id}")

    if job.project_graph is None:
        raise HTTPException(
            status_code=400,
            detail="Project graph not yet built",
        )

    return {
        "dependency_edges": [e.model_dump() for e in job.project_graph.dependency_edges],
        "shared_assets": [a.model_dump() for a in job.project_graph.shared_assets],
        "mapping_count": job.project_graph.mapping_count,
    }


@app.get("/api/audit")
def get_audit_trail():
    """Audit trail — placeholder for v0.2.0."""
    return {"audit_entries": []}


# ---------------------------------------------------------------------------
# Frontend
# ---------------------------------------------------------------------------

@app.get("/")
def serve_frontend():
    """Serve the single-page React frontend."""
    return FileResponse(_FRONTEND_DIR / "index.html", media_type="text/html")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _job_summary(job) -> dict:
    return JobSummary(
        job_id=job.job_id,
        project_name=job.project_name,
        status=job.status.value,
        current_phase=job.current_phase,
        created_at=job.created_at,
        updated_at=job.updated_at,
        error=job.error,
    ).model_dump()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    port = int(os.getenv("PORT", "8090"))
    host = os.getenv("HOST", "127.0.0.1")

    logger.info("Starting InformaticaProjectAnalysis on %s:%d", host, port)

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=True,
        log_level=LOG_LEVEL.lower(),
    )
