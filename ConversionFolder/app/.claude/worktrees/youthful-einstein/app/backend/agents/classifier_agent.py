"""
STEP 2 — Complexity Classifier Agent
Rule-based scoring against objective criteria from the spec.
"""
from __future__ import annotations
from ..models.schemas import ComplexityReport, ComplexityTier, ParseReport

UNSUPPORTED_TYPES = {
    "Java Transformation", "External Procedure",
    "Advanced External Procedure", "Stored Procedure"
}


def classify(parse_report: ParseReport, graph: dict) -> ComplexityReport:
    criteria: list[str] = []
    special_flags: list[str] = []
    tier = ComplexityTier.LOW

    for mapping in graph.get("mappings", []):
        trans      = mapping.get("transformations", [])
        trans_types = [t["type"] for t in trans]
        sources    = [t for t in trans if "Source Qualifier" in t["type"]]
        targets    = [t for t in trans if "Target" in t.get("name", "").upper() or
                      t["type"] in ("Target Definition",)]
        num_sources = max(len(graph.get("sources", [])), 1)
        num_targets = max(len(graph.get("targets", [])), 1)
        num_trans  = len(trans)

        # Counts of criteria that independently push to HIGH — used for accumulation escalation
        high_structural_criteria = 0

        # ── Check unsupported (auto Very High) ───
        for t in trans:
            if t["type"] in UNSUPPORTED_TYPES:
                special_flags.append(f"UNSUPPORTED_TRANSFORMATION: {t['name']} ({t['type']})")
                tier = ComplexityTier.VERY_HIGH

        # ── Count-based criteria ──────────────────
        if num_trans >= 30:
            criteria.append(f"30+ transformations ({num_trans})")
            tier = _elevate(tier, ComplexityTier.VERY_HIGH)
        elif num_trans >= 15:
            criteria.append(f"15+ transformations ({num_trans})")
            tier = _elevate(tier, ComplexityTier.VERY_HIGH)  # was HIGH — 15+ reliably exceeds HIGH doc budget
        elif num_trans >= 10:
            criteria.append(f"10-14 transformations ({num_trans})")
            tier = _elevate(tier, ComplexityTier.HIGH)       # new intermediate band
        elif num_trans >= 5:
            criteria.append(f"5-9 transformations ({num_trans})")
            tier = _elevate(tier, ComplexityTier.MEDIUM)

        if num_sources >= 5 or num_targets >= 5:
            criteria.append(f"5+ sources or targets ({num_sources} sources, {num_targets} targets)")
            tier = _elevate(tier, ComplexityTier.VERY_HIGH)
        elif num_sources >= 4 or num_targets >= 4:
            criteria.append(f"4+ sources or targets")
            tier = _elevate(tier, ComplexityTier.HIGH)
            high_structural_criteria += 1
        elif num_sources >= 2 or num_targets >= 2:
            criteria.append(f"Multiple sources/targets ({num_sources}s, {num_targets}t)")
            tier = _elevate(tier, ComplexityTier.MEDIUM)

        # ── Transformation-type criteria ──────────
        joiner_count = sum(1 for t in trans_types if "Joiner" in t)
        lookup_count = sum(1 for t in trans_types if "Lookup" in t)
        router_count = sum(1 for t in trans_types if "Router" in t)

        if joiner_count > 1:
            criteria.append(f"Multiple Joiners ({joiner_count})")
            tier = _elevate(tier, ComplexityTier.HIGH)
            high_structural_criteria += 1
        elif joiner_count == 1:
            criteria.append("Joiner transformation present")
            tier = _elevate(tier, ComplexityTier.MEDIUM)

        if lookup_count > 1:
            criteria.append(f"Multiple Lookups ({lookup_count})")
            tier = _elevate(tier, ComplexityTier.HIGH)
            high_structural_criteria += 1
        elif lookup_count == 1:
            criteria.append("Lookup transformation present")
            tier = _elevate(tier, ComplexityTier.MEDIUM)

        if any("Normalizer" in t for t in trans_types):
            criteria.append("Normalizer transformation present")
            tier = _elevate(tier, ComplexityTier.HIGH)
            high_structural_criteria += 1

        if any("Rank" in t for t in trans_types):
            criteria.append("Rank transformation present")
            tier = _elevate(tier, ComplexityTier.HIGH)
            high_structural_criteria += 1

        if any("Transaction Control" in t for t in trans_types):
            criteria.append("Transaction Control transformation present")
            tier = _elevate(tier, ComplexityTier.VERY_HIGH)

        if any("HTTP" in t for t in trans_types):
            criteria.append("HTTP transformation present")
            tier = _elevate(tier, ComplexityTier.VERY_HIGH)

        # ── SQL override check ────────────────────
        for t in trans:
            if "Source Qualifier" in t["type"]:
                sql = t.get("table_attribs", {}).get("Sql Query", "")
                if sql:
                    criteria.append("Custom SQL override in Source Qualifier")
                    tier = _elevate(tier, ComplexityTier.MEDIUM)
                    if len(sql) > 500:
                        criteria.append("Complex/long custom SQL override")
                        tier = _elevate(tier, ComplexityTier.HIGH)

        # ── Expression complexity ─────────────────
        all_expressions = []
        for t in trans:
            all_expressions.extend(t.get("expressions", []))
        complex_expr = [e for e in all_expressions if _is_complex_expression(e["expression"])]
        if complex_expr:
            criteria.append(f"Complex expressions detected ({len(complex_expr)} fields)")
            tier = _elevate(tier, ComplexityTier.MEDIUM)

        # ── Nested mapplets ───────────────────────
        mapplet_count = sum(1 for t in trans_types if "Mapplet" in t)
        if mapplet_count > 1:
            criteria.append(f"Nested/multiple mapplets ({mapplet_count})")
            tier = _elevate(tier, ComplexityTier.VERY_HIGH)
        elif mapplet_count == 1:
            criteria.append("Mapplet used")
            tier = _elevate(tier, ComplexityTier.HIGH)

        # ── Accumulation escalation ───────────────────────────────────────────
        # A mapping that hits HIGH from 2+ independent structural criteria
        # (e.g. multiple joiners + 4+ sources) will generate substantially more
        # documentation than a simple HIGH mapping — escalate to VERY_HIGH so
        # the dynamic token budget allocates sufficient room.
        if tier == ComplexityTier.HIGH and high_structural_criteria >= 2:
            criteria.append(
                f"Accumulation escalation: {high_structural_criteria} independent "
                f"HIGH structural criteria — escalated to VERY_HIGH for documentation budget"
            )
            tier = ComplexityTier.VERY_HIGH

    if not criteria and not special_flags:
        criteria.append("Single source/target, <5 transformations, no complex logic")

    return ComplexityReport(
        tier=tier,
        criteria_matched=criteria,
        data_volume_est=None,   # Would need session XML or parameter files
        special_flags=special_flags,
        rationale=_build_rationale(tier, criteria, special_flags),
    )


def _elevate(current: ComplexityTier, candidate: ComplexityTier) -> ComplexityTier:
    order = [ComplexityTier.LOW, ComplexityTier.MEDIUM,
             ComplexityTier.HIGH, ComplexityTier.VERY_HIGH]
    return candidate if order.index(candidate) > order.index(current) else current


def _is_complex_expression(expr: str) -> bool:
    if not expr:
        return False
    indicators = ["IIF(", "DECODE(", "TO_DATE(", "IN(", "INSTR(", "SUBSTR(",
                  "TRUNC(", "ROUND(", ":LKP.", "$$", "$$$"]
    return any(ind in expr.upper() for ind in indicators)


def _build_rationale(tier: ComplexityTier, criteria: list, flags: list) -> str:
    parts = [f"Classified as **{tier.value}** complexity based on the following:"]
    for c in criteria:
        parts.append(f"- {c}")
    if flags:
        parts.append("\nSpecial flags that elevated classification:")
        for f in flags:
            parts.append(f"- {f}")
    return "\n".join(parts)
