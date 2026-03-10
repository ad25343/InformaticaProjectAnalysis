"""
Spine extractor — derives the canonical transformation spine from a
mapping's connector topology.

Phase 2, Step 2.1: Structural fingerprinting.

The spine is the ordered sequence of transformation instances from source
to target, following the connector graph. This is the primary grouping key.
"""

from __future__ import annotations

import logging
from collections import defaultdict

from app.backend.models.schemas import (
    Connector,
    MappingParseResult,
    MappingSpine,
    SpineStep,
)

logger = logging.getLogger(__name__)

# Canonical short names for transformation types
TYPE_SHORT = {
    "Source Definition": "SRC",
    "Source Qualifier": "SQ",
    "Expression": "EXP",
    "Lookup Procedure": "LKP",
    "Lookup": "LKP",
    "Filter": "FIL",
    "Joiner": "JNR",
    "Router": "RTR",
    "Aggregator": "AGG",
    "Sorter": "SRT",
    "Union": "UNI",
    "Normalizer": "NRM",
    "Rank": "RNK",
    "Sequence Generator": "SEQ",
    "Stored Procedure": "SP",
    "Update Strategy": "UPD",
    "Target Definition": "TGT",
    "Custom Transformation": "CT",
    "Transaction Control": "TC",
}


def extract_spine(parse_result: MappingParseResult) -> MappingSpine:
    """
    Extract the canonical transformation spine from a mapping.

    Follows the connector graph from sources to targets, producing an
    ordered list of transformation steps.

    The spine signature collapses consecutive same-type steps and uses
    short names: "SQ → EXP → LKP → TGT"

    Args:
        parse_result: Parsed mapping data.

    Returns:
        MappingSpine with ordered steps and signature string.
    """
    connectors = parse_result.mapping.connectors

    if not connectors:
        return MappingSpine(
            mapping_name=parse_result.mapping_name,
            steps=[],
            spine_signature="EMPTY",
        )

    # Build adjacency: instance → set of downstream instances
    # Also collect instance types
    adjacency: dict[str, set[str]] = defaultdict(set)
    instance_types: dict[str, str] = {}

    for conn in connectors:
        from_inst = conn.from_instance
        to_inst = conn.to_instance

        adjacency[from_inst].add(to_inst)

        # Record instance types
        if from_inst not in instance_types:
            instance_types[from_inst] = conn.from_instance_type
        if to_inst not in instance_types:
            instance_types[to_inst] = conn.to_instance_type

    # Find root nodes (sources — no incoming edges)
    all_to = {conn.to_instance for conn in connectors}
    all_from = {conn.from_instance for conn in connectors}
    roots = all_from - all_to

    # Find leaf nodes (targets — no outgoing edges from mapping connectors)
    leaves = all_to - all_from

    # BFS from roots to build ordered spine
    visited: set[str] = set()
    ordered_steps: list[SpineStep] = []

    # Start with roots sorted for determinism
    queue = sorted(roots)

    while queue:
        next_queue = []
        for inst in queue:
            if inst in visited:
                continue
            visited.add(inst)
            inst_type = instance_types.get(inst, "Unknown")

            # Skip Source Definition nodes — spine starts at SQ
            if inst_type != "Source Definition":
                ordered_steps.append(SpineStep(
                    instance_name=inst,
                    instance_type=inst_type,
                ))

            # Add downstream instances
            for downstream in sorted(adjacency.get(inst, set())):
                if downstream not in visited:
                    next_queue.append(downstream)

        queue = next_queue

    # Build signature — collapse consecutive same-type, use short names
    signature = _build_signature(ordered_steps)

    spine = MappingSpine(
        mapping_name=parse_result.mapping_name,
        steps=ordered_steps,
        spine_signature=signature,
    )

    logger.debug("Spine for %s: %s", spine.mapping_name, signature)

    return spine


def _build_signature(steps: list[SpineStep]) -> str:
    """
    Build a compact signature string from spine steps.

    Collapses consecutive same-type steps with multipliers:
    SQ, EXP, LKP, LKP, TGT -> "SQ → EXP → LKP(×2) → TGT"
    """
    if not steps:
        return "EMPTY"

    # Group consecutive same-type steps
    groups: list[tuple[str, int]] = []
    prev_type = ""
    count = 0

    for step in steps:
        short = TYPE_SHORT.get(step.instance_type, step.instance_type)
        if short == prev_type:
            count += 1
        else:
            if prev_type:
                groups.append((prev_type, count))
            prev_type = short
            count = 1
    if prev_type:
        groups.append((prev_type, count))

    # Format with multipliers
    parts = []
    for type_name, n in groups:
        if n > 1:
            parts.append(f"{type_name}(×{n})")
        else:
            parts.append(type_name)

    return " → ".join(parts)
