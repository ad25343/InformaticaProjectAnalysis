"""
Project graph builder — aggregates per-mapping parse results into a
project-level dependency graph with shared assets.

Phase 1, Step 1.3: Build cross-mapping dependency edges and detect shared assets.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from app.backend.models.schemas import (
    DependencyEdge,
    ProjectGraph,
    MappingParseResult,
    SharedAsset,
    SourceDef,
    TargetDef,
)

logger = logging.getLogger(__name__)


def build_project_graph(
    parse_results: list[MappingParseResult],
    min_shared_refs: int = 2,
) -> ProjectGraph:
    """
    Build project-level graph from N individual mapping parse results.

    Constructs:
    1. Deduplicated source and target lists
    2. target_to_mapping index (which mapping writes which target)
    3. Dependency edges (mapping B looks up table X which mapping A writes)
    4. Shared assets (tables referenced as lookups by N+ mappings)

    Args:
        parse_results: List of per-mapping parse outputs.
        min_shared_refs: Minimum references to qualify as a shared asset.

    Returns:
        ProjectGraph with all cross-mapping relationships.
    """
    # 1. Collect and deduplicate sources and targets
    all_sources: dict[str, SourceDef] = {}
    all_targets: dict[str, TargetDef] = {}
    target_to_mapping: dict[str, str] = {}
    mapping_lookups: dict[str, list[str]] = {}

    for pr in parse_results:
        mapping_name = pr.mapping_name

        for src in pr.sources:
            key = f"{src.name}|{src.db_type}|{src.owner}"
            if key not in all_sources:
                all_sources[key] = src

        for tgt in pr.targets:
            key = f"{tgt.name}|{tgt.db_type}|{tgt.owner}"
            if key not in all_targets:
                all_targets[key] = tgt
            # Record which mapping writes this target
            target_to_mapping[tgt.name] = mapping_name

        # Collect lookup references for this mapping
        lookup_names = pr.lookup_table_names
        if lookup_names:
            mapping_lookups[mapping_name] = lookup_names

    # Also detect lookups from source definitions named LKP_*
    for pr in parse_results:
        mapping_name = pr.mapping_name
        existing = mapping_lookups.get(mapping_name, [])
        for src in pr.sources:
            if src.name.startswith("LKP_"):
                # Extract the referenced table name from the LKP_ prefix
                # e.g., LKP_DIM_CUSTOMER -> DIM_CUSTOMER
                ref_table = src.name[4:]  # Remove LKP_ prefix
                if ref_table and ref_table not in existing:
                    existing.append(ref_table)
        if existing:
            mapping_lookups[mapping_name] = existing

    # 2. Build dependency edges
    dependency_edges: list[DependencyEdge] = []
    for mapping_name, lookup_tables in mapping_lookups.items():
        for lookup_table in lookup_tables:
            # Does another mapping write this table?
            writer = target_to_mapping.get(lookup_table)
            if writer and writer != mapping_name:
                dependency_edges.append(DependencyEdge(
                    from_mapping=writer,
                    to_mapping=mapping_name,
                    via_table=lookup_table,
                ))

    # 3. Detect shared assets
    # Count how many mappings reference each table via lookup
    table_ref_count: dict[str, list[str]] = defaultdict(list)
    for mapping_name, lookup_tables in mapping_lookups.items():
        for table in lookup_tables:
            table_ref_count[table].append(mapping_name)

    shared_assets: list[SharedAsset] = []
    for table_name, referencing_mappings in sorted(table_ref_count.items()):
        if len(referencing_mappings) >= min_shared_refs:
            shared_assets.append(SharedAsset(
                table_name=table_name,
                reference_type="lookup",
                referenced_by=sorted(set(referencing_mappings)),
            ))

    graph = ProjectGraph(
        mapping_count=len(parse_results),
        all_sources=list(all_sources.values()),
        all_targets=list(all_targets.values()),
        dependency_edges=dependency_edges,
        shared_assets=shared_assets,
        target_to_mapping=target_to_mapping,
        mapping_lookups=mapping_lookups,
    )

    logger.info(
        "Project graph: %d mappings, %d sources, %d targets, "
        "%d dependency edges, %d shared assets",
        graph.mapping_count,
        len(graph.all_sources),
        len(graph.all_targets),
        len(graph.dependency_edges),
        len(graph.shared_assets),
    )

    return graph
