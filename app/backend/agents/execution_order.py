"""
Execution order — topologically sorts the dependency DAG into parallel stages.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from app.backend.models.schemas import DependencyEdge

logger = logging.getLogger(__name__)


def compute_execution_order(
    all_mapping_names: list[str],
    dependency_edges: list[DependencyEdge],
) -> list[list[str]]:
    """
    Topologically sort mappings into parallel execution stages.

    Stage N contains all mappings whose dependencies are fully satisfied
    by stages 0..N-1. Mappings within a stage can run concurrently.

    Args:
        all_mapping_names: All mapping names in the project.
        dependency_edges: Cross-mapping dependency edges.

    Returns:
        List of stages, each a list of mapping names.
    """
    # Build adjacency and in-degree
    in_degree: dict[str, int] = {name: 0 for name in all_mapping_names}
    successors: dict[str, list[str]] = defaultdict(list)

    for edge in dependency_edges:
        if edge.to_mapping in in_degree:
            in_degree[edge.to_mapping] += 1
            successors[edge.from_mapping].append(edge.to_mapping)

    # Kahn's algorithm — layer by layer
    stages: list[list[str]] = []
    remaining = dict(in_degree)

    while remaining:
        # Find all nodes with in-degree 0
        ready = sorted([n for n, d in remaining.items() if d == 0])

        if not ready:
            # Cycle detected — dump remaining as final stage with warning
            logger.warning(
                "Cycle detected in dependency graph. "
                "Remaining mappings placed in final stage: %s",
                sorted(remaining.keys()),
            )
            stages.append(sorted(remaining.keys()))
            break

        stages.append(ready)

        # Remove ready nodes and update in-degrees
        for node in ready:
            del remaining[node]
            for succ in successors.get(node, []):
                if succ in remaining:
                    remaining[succ] -= 1

    logger.info(
        "Execution order: %d stages for %d mappings",
        len(stages),
        len(all_mapping_names),
    )

    return stages
