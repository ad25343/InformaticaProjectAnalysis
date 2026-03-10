"""
Pattern grouper — groups mappings by structural spine and classifies
variation tiers.

Phase 2, Steps 2.1–2.3: Fingerprinting, variation classification,
confidence scoring.
"""

from __future__ import annotations

import logging
import uuid
from collections import defaultdict

from app.backend.models.schemas import (
    AnalysisSettings,
    Confidence,
    GroupMember,
    MappingParseResult,
    MappingSpine,
    PatternGroup,
    UniqueMapping,
    VariationTier,
)

logger = logging.getLogger(__name__)


def group_mappings(
    parse_results: list[MappingParseResult],
    spines: list[MappingSpine],
    settings: AnalysisSettings,
) -> tuple[list[PatternGroup], list[UniqueMapping]]:
    """
    Group mappings by spine signature and classify variation.

    Algorithm:
    1. Group by exact spine signature
    2. Within each group, classify variation tier by comparing
       transformation details (ports, expressions, table attributes)
    3. Assign confidence based on variation tier and structural similarity
    4. Groups below min_group_size become unique mappings

    Args:
        parse_results: All parsed mappings.
        spines: Extracted spines for each mapping.
        settings: Analysis settings from project config.

    Returns:
        Tuple of (pattern_groups, unique_mappings).
    """
    # Index parse results by mapping name
    parse_by_name: dict[str, MappingParseResult] = {
        pr.mapping_name: pr for pr in parse_results
    }
    spine_by_name: dict[str, MappingSpine] = {
        s.mapping_name: s for s in spines
    }

    # Step 1: Group by spine signature
    sig_groups: dict[str, list[str]] = defaultdict(list)
    for spine in spines:
        sig_groups[spine.spine_signature].append(spine.mapping_name)

    pattern_groups: list[PatternGroup] = []
    unique_mappings: list[UniqueMapping] = []

    for signature, member_names in sorted(sig_groups.items()):
        if len(member_names) < settings.min_group_size:
            # Too few members — unique mappings
            for name in member_names:
                unique_mappings.append(UniqueMapping(
                    mapping_name=name,
                    reason=f"Group size {len(member_names)} below minimum {settings.min_group_size}",
                    risk_flags=_get_risk_flags(parse_by_name.get(name)),
                ))
            continue

        if signature == "EMPTY":
            for name in member_names:
                unique_mappings.append(UniqueMapping(
                    mapping_name=name,
                    reason="No connectors found — cannot determine spine",
                    risk_flags=["NO_CONNECTORS"],
                ))
            continue

        # Step 2: Classify variation within group
        members = _classify_members(
            member_names, parse_by_name, settings.confidence_threshold
        )

        # Step 3: Build pattern group
        group_name = _generate_group_name(signature, parse_by_name, member_names)
        externalized = _detect_externalized_params(member_names, parse_by_name)

        group = PatternGroup(
            group_id=f"pg_{uuid.uuid4().hex[:8]}",
            group_name=group_name,
            spine_signature=signature,
            members=members,
            externalized_params=externalized,
            template_hints=_generate_template_hint(signature, len(members)),
        )

        # Check: if all members are Tier 3, they shouldn't be grouped
        tier3_count = sum(
            1 for m in members if m.variation_tier == VariationTier.TIER_3
        )
        if tier3_count == len(members):
            for m in members:
                unique_mappings.append(UniqueMapping(
                    mapping_name=m.mapping_name,
                    reason="Tier 3 — fundamental structural variation despite matching spine",
                    risk_flags=[],
                ))
        else:
            pattern_groups.append(group)

    logger.info(
        "Pattern grouping: %d groups, %d unique mappings",
        len(pattern_groups),
        len(unique_mappings),
    )

    return pattern_groups, unique_mappings


def _classify_members(
    member_names: list[str],
    parse_by_name: dict[str, MappingParseResult],
    confidence_threshold: float,
) -> list[GroupMember]:
    """
    Classify each member's variation tier and confidence within a group.

    Compares each mapping against the first (reference) mapping in the group.
    """
    members: list[GroupMember] = []

    if not member_names:
        return members

    # Use first mapping as reference
    ref_name = member_names[0]
    ref_pr = parse_by_name.get(ref_name)

    for name in member_names:
        pr = parse_by_name.get(name)
        if pr is None:
            members.append(GroupMember(
                mapping_name=name,
                confidence=Confidence.UNCLASSIFIED,
                variation_tier=VariationTier.TIER_3,
                variation_notes="Parse result not found",
            ))
            continue

        if name == ref_name:
            # Reference mapping is always Tier 1 HIGH
            members.append(GroupMember(
                mapping_name=name,
                confidence=Confidence.HIGH,
                variation_tier=VariationTier.TIER_1,
            ))
            continue

        tier, notes = _compare_to_reference(ref_pr, pr)
        confidence = _tier_to_confidence(tier, pr)

        members.append(GroupMember(
            mapping_name=name,
            confidence=confidence,
            variation_tier=tier,
            variation_notes=notes if notes else None,
        ))

    return members


def _compare_to_reference(
    ref: MappingParseResult | None,
    candidate: MappingParseResult,
) -> tuple[VariationTier, str]:
    """
    Compare a candidate mapping against the reference mapping.

    Returns variation tier and notes explaining the difference.
    """
    if ref is None:
        return VariationTier.TIER_1, ""

    notes_parts: list[str] = []

    # Compare transformation counts
    ref_types = sorted(t.type for t in ref.transformations)
    cand_types = sorted(t.type for t in candidate.transformations)

    if ref_types == cand_types:
        # Same transformation types — Tier 1 (parameter only)
        # Check for expression differences
        ref_exprs = _get_expression_set(ref)
        cand_exprs = _get_expression_set(candidate)

        if ref_exprs and cand_exprs and ref_exprs != cand_exprs:
            # Different expressions but same structure
            notes_parts.append("Different expression logic")
            return VariationTier.TIER_2, "; ".join(notes_parts)

        return VariationTier.TIER_1, ""

    # Different transformation types
    diff = set(cand_types) - set(ref_types)
    missing = set(ref_types) - set(cand_types)

    if len(diff) + len(missing) <= 2:
        # Minor structural difference — Tier 2
        if diff:
            notes_parts.append(f"Extra transformations: {', '.join(diff)}")
        if missing:
            notes_parts.append(f"Missing transformations: {', '.join(missing)}")
        return VariationTier.TIER_2, "; ".join(notes_parts)

    # Major structural difference — Tier 3
    notes_parts.append(
        f"Significant structural difference: "
        f"{len(diff)} extra, {len(missing)} missing transformation types"
    )
    return VariationTier.TIER_3, "; ".join(notes_parts)


def _get_expression_set(pr: MappingParseResult) -> set[str]:
    """Get set of non-trivial expression bodies from a mapping."""
    exprs = set()
    for t in pr.transformations:
        for p in t.ports:
            if p.expression and p.expression.strip():
                exprs.add(p.expression.strip())
    return exprs


def _tier_to_confidence(tier: VariationTier, pr: MappingParseResult) -> Confidence:
    """Map variation tier to confidence level, considering risk flags."""
    flags = _get_risk_flags(pr)

    if tier == VariationTier.TIER_1:
        return Confidence.HIGH if not flags else Confidence.MEDIUM
    elif tier == VariationTier.TIER_2:
        return Confidence.MEDIUM
    else:
        return Confidence.LOW


def _get_risk_flags(pr: MappingParseResult | None) -> list[str]:
    """Detect risk flags for a mapping."""
    if pr is None:
        return ["PARSE_MISSING"]

    flags = []

    # Check for custom SQL overrides
    for t in pr.transformations:
        if t.sql_query:
            flags.append("CUSTOM_SQL_OVERRIDE")
            break

    # Check for parse errors
    if pr.parse_errors:
        flags.append("PARSE_ERRORS")

    # Check for many transformations (complexity indicator)
    if len(pr.transformations) > 10:
        flags.append("HIGH_TRANSFORMATION_COUNT")

    return flags


def _generate_group_name(
    signature: str,
    parse_by_name: dict[str, MappingParseResult],
    member_names: list[str],
) -> str:
    """Generate a human-readable group name from signature and members."""
    # Use the signature as a base, with some heuristic naming
    sig_lower = signature.lower()

    # Try to infer from member names
    sample_names = member_names[:3]
    prefixes = set()
    for name in sample_names:
        parts = name.split("_")
        if len(parts) >= 2:
            # e.g., m_dim_customer_load -> "dim"
            prefixes.add(parts[1] if parts[0] == "m" else parts[0])

    # Common pattern recognition
    if "agg" in sig_lower or "agg" in prefixes:
        base = "Aggregation"
    elif "scd" in " ".join(sample_names).lower():
        base = "SCD2 Dimension"
    elif "stg" in prefixes:
        base = "Staging Extract"
    elif "fct" in prefixes or "fact" in prefixes:
        base = "Fact Load"
    elif "dim" in prefixes:
        base = "Dimension Load"
    elif "ref" in prefixes:
        base = "Reference Table Load"
    elif "bridge" in prefixes:
        base = "Bridge Table Load"
    else:
        # Fall back to signature-based name
        base = f"Pattern ({signature})"

    return f"{base} ({len(member_names)} mappings)"


def _detect_externalized_params(
    member_names: list[str],
    parse_by_name: dict[str, MappingParseResult],
) -> list[str]:
    """Detect which parameters vary across group members (candidates for config)."""
    params = set()

    # Always externalize source/target table names
    source_names = set()
    target_names = set()
    for name in member_names:
        pr = parse_by_name.get(name)
        if pr:
            source_names.update(pr.source_table_names)
            target_names.update(pr.target_table_names)

    if len(source_names) > 1:
        params.add("source_table")
    if len(target_names) > 1:
        params.add("target_table")

    # Check for different column sets
    column_sets: list[set[str]] = []
    for name in member_names:
        pr = parse_by_name.get(name)
        if pr and pr.sources:
            cols = {f.name for src in pr.sources for f in src.fields}
            column_sets.append(cols)
    if len(column_sets) > 1 and len(set(frozenset(c) for c in column_sets)) > 1:
        params.add("column_list")

    # Check for filter conditions (SQ SQL overrides)
    has_sql = set()
    for name in member_names:
        pr = parse_by_name.get(name)
        if pr:
            for t in pr.transformations:
                if t.sql_query:
                    has_sql.add(name)
    if has_sql and len(has_sql) < len(member_names):
        params.add("filter_condition")

    return sorted(params)


def _generate_template_hint(signature: str, member_count: int) -> str:
    """Generate a template hint describing the conversion approach."""
    return (
        f"Config-driven template for {member_count} mappings "
        f"sharing spine: {signature}"
    )
