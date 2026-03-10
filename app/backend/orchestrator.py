"""
Analysis orchestrator — runs the full 5-phase pipeline for a project.
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.backend.agents.config_parser import parse_project_config
from app.backend.agents.project_graph import build_project_graph
from app.backend.agents.execution_order import compute_execution_order
from app.backend.agents.mapping_parser import parse_mapping_xml
from app.backend.agents.pattern_grouper import group_mappings
from app.backend.agents.source_resolver import resolve_source
from app.backend.agents.spine_extractor import extract_spine
from app.backend.models.schemas import (
    AnalysisJob,
    AnalysisStatus,
    ProjectConfig,
    StrategySummary,
    StrategyJSON,
)

logger = logging.getLogger(__name__)


class AnalysisOrchestrator:
    """Runs the analysis pipeline and maintains job state."""

    def __init__(self) -> None:
        self.jobs: dict[str, AnalysisJob] = {}

    def create_job(self, config: ProjectConfig) -> AnalysisJob:
        """Create a new analysis job from a validated project config."""
        job_id = uuid.uuid4().hex[:12]
        now = datetime.now(timezone.utc).isoformat()

        job = AnalysisJob(
            job_id=job_id,
            project_name=config.project.name,
            project_config=config,
            status=AnalysisStatus.PENDING,
            current_phase=0,
            created_at=now,
            updated_at=now,
        )
        self.jobs[job_id] = job
        logger.info("Created analysis job %s for %s", job_id, config.project.name)
        return job

    def create_job_from_yaml(self, config_path: str | Path) -> AnalysisJob:
        """Parse a YAML config file and create a job."""
        config = parse_project_config(config_path)
        return self.create_job(config)

    def run_analysis(self, job_id: str) -> AnalysisJob:
        """
        Run the full analysis pipeline for a job.

        Phases:
        1. Discovery (source resolution + parsing + project graph)
        2. Pattern grouping (fingerprinting + classification)
        3. Strategy generation

        Phases 4 (human gate) and 5 (delivery) are triggered separately.
        """
        job = self.jobs.get(job_id)
        if job is None:
            raise ValueError(f"Job not found: {job_id}")

        try:
            self._run_phase_1(job)
            self._run_phase_2(job)
            self._run_phase_3(job)
        except Exception as e:
            job.status = AnalysisStatus.FAILED
            job.error = str(e)
            job.updated_at = datetime.now(timezone.utc).isoformat()
            logger.error("Job %s failed: %s", job_id, e, exc_info=True)
            raise

        return job

    def _run_phase_1(self, job: AnalysisJob) -> None:
        """Phase 1: Discovery — resolve source, parse all mappings, build graph."""
        config = job.project_config

        # Step 1.1: Source resolution
        job.status = AnalysisStatus.RESOLVING_SOURCE
        job.current_phase = 1
        job.updated_at = datetime.now(timezone.utc).isoformat()

        resolved_files = resolve_source(config)
        job.resolved_files = [str(f) for f in resolved_files]

        logger.info(
            "Phase 1.1: Resolved %d files for %s",
            len(resolved_files), job.project_name,
        )

        # Step 1.2: Parse all mapping XMLs
        job.status = AnalysisStatus.PARSING

        parse_results = []
        seen_hashes: set[str] = set()
        seen_names: set[str] = set()
        for file_path in resolved_files:
            try:
                result = parse_mapping_xml(file_path)
                # Deduplicate by content hash (same file in multiple dirs)
                if result.file_hash in seen_hashes:
                    logger.debug("Skipping duplicate (hash): %s", file_path)
                    continue
                # Deduplicate by mapping name
                if result.mapping_name in seen_names:
                    logger.debug("Skipping duplicate (name): %s", file_path)
                    continue
                seen_hashes.add(result.file_hash)
                seen_names.add(result.mapping_name)
                parse_results.append(result)
            except Exception as e:
                logger.warning("Failed to parse %s: %s", file_path, e)
                # Continue with remaining files

        job.parse_results = parse_results

        logger.info(
            "Phase 1.2: Parsed %d/%d mappings",
            len(parse_results), len(resolved_files),
        )

        # Step 1.3: Build project graph
        job.status = AnalysisStatus.BUILDING_GRAPH

        project_graph = build_project_graph(
            parse_results,
            min_shared_refs=config.analysis.min_group_size,
        )
        job.project_graph = project_graph

        logger.info(
            "Phase 1.3: Project graph — %d dependencies, %d shared assets",
            len(project_graph.dependency_edges),
            len(project_graph.shared_assets),
        )

    def _run_phase_2(self, job: AnalysisJob) -> None:
        """Phase 2: Pattern grouping — extract spines and group by signature."""
        job.status = AnalysisStatus.GROUPING
        job.current_phase = 2
        job.updated_at = datetime.now(timezone.utc).isoformat()

        # Step 2.1: Extract spines
        spines = [extract_spine(pr) for pr in job.parse_results]
        job.spines = spines

        # Steps 2.2–2.3: Group and classify
        pattern_groups, unique_mappings = group_mappings(
            job.parse_results,
            spines,
            job.project_config.analysis,
        )

        job.pattern_groups = pattern_groups
        job.unique_mappings = unique_mappings

        logger.info(
            "Phase 2: %d pattern groups, %d unique mappings",
            len(pattern_groups), len(unique_mappings),
        )

    def _run_phase_3(self, job: AnalysisJob) -> None:
        """Phase 3: Generate strategy JSON."""
        job.status = AnalysisStatus.GENERATING_STRATEGY
        job.current_phase = 3
        job.updated_at = datetime.now(timezone.utc).isoformat()

        # Compute execution order
        all_mapping_names = [pr.mapping_name for pr in job.parse_results]
        edges = job.project_graph.dependency_edges if job.project_graph else []
        execution_order = compute_execution_order(all_mapping_names, edges)

        # Build summary
        template_candidates = sum(
            g.member_count for g in job.pattern_groups
        )
        total = len(job.parse_results)
        unique_count = len(job.unique_mappings)
        scope_reduction = (
            round((1 - (len(job.pattern_groups) + unique_count) / total) * 100, 1)
            if total > 0 else 0
        )

        summary = StrategySummary(
            total_mappings=total,
            pattern_groups=len(job.pattern_groups),
            template_candidates=template_candidates,
            unique_mappings=unique_count,
            scope_reduction_pct=scope_reduction,
        )

        strategy = StrategyJSON(
            project_name=job.project_name,
            analysis_job_id=job.job_id,
            analyzed_at=datetime.now(timezone.utc).isoformat(),
            summary=summary,
            pattern_groups=job.pattern_groups,
            unique_mappings=job.unique_mappings,
            shared_assets=job.project_graph.shared_assets if job.project_graph else [],
            dependency_dag=job.project_graph.dependency_edges if job.project_graph else [],
            execution_order=execution_order,
        )

        job.strategy = strategy
        job.status = AnalysisStatus.AWAITING_REVIEW
        job.updated_at = datetime.now(timezone.utc).isoformat()

        logger.info(
            "Phase 3: Strategy generated — %d groups, %d unique, %.1f%% scope reduction",
            summary.pattern_groups,
            summary.unique_mappings,
            summary.scope_reduction_pct,
        )

    def get_job(self, job_id: str) -> AnalysisJob | None:
        """Get a job by ID."""
        return self.jobs.get(job_id)

    def list_jobs(self) -> list[AnalysisJob]:
        """List all jobs."""
        return list(self.jobs.values())
