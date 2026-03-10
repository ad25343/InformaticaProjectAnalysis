"""
STEP 4 — Verification Agent
Runs ALL checks without stopping. Produces one complete Verification Report.
Deterministic checks run in Python; qualitative flags use Claude.

Each flag now carries:
  severity       — CRITICAL | HIGH | MEDIUM | LOW | INFO
  recommendation — Actionable guidance for the human reviewer
"""
from __future__ import annotations
import json
import os
import anthropic

from typing import Optional
from ..models.schemas import (
    VerificationReport, VerificationFlag, CheckResult,
    ComplexityTier, ComplexityReport, ParseReport, SessionParseReport
)

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

# Import the truncation sentinel from the documentation agent so we detect it consistently.
from .documentation_agent import DOC_TRUNCATION_SENTINEL  # noqa: E402

# Tier-aware token budget for the Claude quality-check call.
# More transformations → more flags → more tokens needed in the response.
_QC_MAX_TOKENS: dict[ComplexityTier, int] = {
    ComplexityTier.LOW:       2_048,
    ComplexityTier.MEDIUM:    4_096,
    ComplexityTier.HIGH:      6_144,
    ComplexityTier.VERY_HIGH: 8_192,
}

UNSUPPORTED_TYPES = {
    "Java Transformation", "External Procedure",
    "Advanced External Procedure", "Stored Procedure"
}

BLOCKING_FLAG_TYPES = {
    "UNSUPPORTED_TRANSFORMATION", "UNRESOLVED_PARAMETER_BLOCKING",
    "SQL_REVIEW_REQUIRED", "PARSE_FAILED"
}

# ─────────────────────────────────────────────────────────────────────────────
# FLAG METADATA — severity + actionable recommendation per flag type
# ─────────────────────────────────────────────────────────────────────────────
FLAG_META: dict[str, dict] = {
    "UNSUPPORTED_TRANSFORMATION": {
        "severity": "CRITICAL",
        "recommendation": (
            "This transformation type cannot be automatically converted. Manual re-implementation "
            "is required before conversion can proceed. Consider splitting this mapping into a "
            "separate manual migration task and converting the rest automatically."
        ),
    },
    "PARSE_FAILED": {
        "severity": "CRITICAL",
        "recommendation": (
            "The XML could not be parsed. Verify this is a valid Informatica PowerCenter export "
            "(.xml). Re-export from Informatica Designer, ensure the file is not truncated, and "
            "check for XML encoding issues."
        ),
    },
    "SQL_REVIEW_REQUIRED": {
        "severity": "CRITICAL",
        "recommendation": (
            "This SQL override cannot be automatically converted. Manually translate the custom "
            "SQL into the target stack's equivalent (e.g., Spark SQL / dbt SQL). Review for "
            "database-specific syntax that won't port directly."
        ),
    },
    "UNRESOLVED_PARAMETER_BLOCKING": {
        "severity": "CRITICAL",
        "recommendation": (
            "This parameter is used in a critical position (filter, join condition, or SQL). "
            "Resolve the actual runtime value before conversion proceeds. Document the value "
            "in the converted code's config section."
        ),
    },
    "UNRESOLVED_PARAMETER": {
        "severity": "HIGH",
        "recommendation": (
            "Replace the parameter with its runtime value, or externalize it to a config "
            "file (.env or config.yaml). If it is a session-level parameter (e.g. $PMRootDir), "
            "document it as a required input in the converted code's README."
        ),
    },
    "ENVIRONMENT_SPECIFIC_VALUE": {
        "severity": "HIGH",
        "recommendation": (
            "Move this hardcoded value to an environment config file. Never embed connection "
            "strings, file paths, server names, or schema names directly in converted code — "
            "they will break across environments (dev/staging/prod)."
        ),
    },
    "HIGH_RISK": {
        "severity": "HIGH",
        "recommendation": (
            "Flag for additional peer review and UAT testing before promoting to production. "
            "Add reconciliation row counts and data quality assertions around this logic in "
            "the converted code. Ensure an audit trail is maintained."
        ),
    },
    "LINEAGE_GAP": {
        "severity": "HIGH",
        "recommendation": (
            "Trace this target field manually in the Informatica mapping. If truly unresolvable, "
            "document it as a known gap in the conversion notes and add a TODO comment in the "
            "generated code so downstream engineers are aware."
        ),
    },
    "ACCURACY_CONCERN": {
        "severity": "HIGH",
        "recommendation": (
            "Review the generated documentation against the original XML to verify no business "
            "logic was altered during the documentation step. If inaccurate, delete this job, "
            "correct the XML or the documentation prompt, and re-run."
        ),
    },
    "INCOMPLETE_LOGIC": {
        "severity": "HIGH",
        "recommendation": (
            "Review all branches of the conditional logic. Ensure every ELSE / default case is "
            "handled explicitly. Missing branches cause silent data loss, incorrect routing, or "
            "wrong aggregation results in production."
        ),
    },
    "REVIEW_REQUIRED": {
        "severity": "MEDIUM",
        "recommendation": (
            "Assign a subject matter expert to clarify the ambiguous logic before finalising "
            "the conversion. Document the interpretation chosen in the sign-off notes so the "
            "decision is auditable."
        ),
    },
    "CLASSIFICATION_MISMATCH": {
        "severity": "MEDIUM",
        "recommendation": (
            "Review the complexity tier manually. Misclassification may result in the wrong "
            "target stack being assigned (e.g., Python/Pandas selected for a mapping that "
            "processes millions of rows and should use PySpark)."
        ),
    },
    "DEAD_LOGIC": {
        "severity": "LOW",
        "recommendation": (
            "Confirm with the business owner whether this transformation is intentional. "
            "If confirmed unused, remove it to reduce code complexity and improve readability "
            "in the converted output. It adds no value to the data flow."
        ),
    },
    "ORPHANED_PORT": {
        "severity": "LOW",
        "recommendation": (
            "Confirm whether this port is intentionally disconnected (e.g. a placeholder or "
            "legacy field). If it serves no purpose, remove it from the mapping to reduce "
            "dead code and simplify the converted output."
        ),
    },
    "DOCUMENTATION_TRUNCATED": {
        "severity": "HIGH",
        "recommendation": (
            "The documentation was cut off by the AI token limit before all transformations "
            "were written. Any 'not found in documentation' failures below are caused by this "
            "truncation — they do NOT indicate missing logic in your Informatica mapping. "
            "Re-run Step 3 to regenerate the documentation. If truncation persists, contact "
            "your admin to increase the token budget for this complexity tier."
        ),
    },
}


def _make_flag(
    flag_type: str,
    location: str,
    description: str,
    blocking: bool,
    severity: str = None,
    recommendation: str = None,
    auto_fix_suggestion: str = None,
) -> VerificationFlag:
    """Create a VerificationFlag, auto-populating severity/recommendation from FLAG_META."""
    meta = FLAG_META.get(flag_type, {})
    return VerificationFlag(
        flag_type=flag_type,
        location=location,
        description=description,
        blocking=blocking,
        severity=severity or meta.get("severity", "MEDIUM"),
        recommendation=recommendation or meta.get("recommendation", "Review this flag with your team before proceeding."),
        auto_fix_suggestion=auto_fix_suggestion,
    )


async def verify(
    parse_report: ParseReport,
    complexity: ComplexityReport,
    documentation_md: str,
    graph: dict,
    session_parse_report: Optional[SessionParseReport] = None,
) -> VerificationReport:
    """Run all verification checks and return a complete VerificationReport."""

    completeness_checks: list[CheckResult] = []
    accuracy_checks: list[CheckResult] = []
    self_checks: list[CheckResult] = []
    flags: list[VerificationFlag] = []

    mapping_name = parse_report.mapping_names[0] if parse_report.mapping_names else "unknown"

    # ─────────────────────────────────────────
    # TRUNCATION DETECTION
    # Check whether the documentation agent hit the token limit.  If so we add
    # one prominent DOCUMENTATION_TRUNCATED flag and annotate every subsequent
    # completeness failure so the reviewer knows the real cause.
    # ─────────────────────────────────────────
    doc_was_truncated = DOC_TRUNCATION_SENTINEL in documentation_md
    if doc_was_truncated:
        # Strip the sentinel so it doesn't pollute name-search checks below
        documentation_md = documentation_md.replace(DOC_TRUNCATION_SENTINEL, "")
        flags.append(_make_flag(
            "DOCUMENTATION_TRUNCATED",
            "Step 3 — Documentation Agent",
            (
                "The documentation was cut off by the AI token limit before all transformations "
                "could be written. Any 'not found in documentation' failures in the Completeness "
                "section below are caused by this truncation — they do NOT reflect missing logic "
                "in your Informatica mapping. Re-run Step 3 to regenerate the documentation."
            ),
            blocking=False,
        ))

    def _missing_detail(entity: str, kind: str) -> str:
        """Return a failure detail that names the real cause when truncation is known."""
        base = f"{kind} '{entity}' not found in documentation"
        if doc_was_truncated:
            base += (
                " ⚠️ Documentation was truncated by the token limit — "
                "this failure is likely caused by truncation, not a missing documentation entry. "
                "Re-run Step 3 to fix."
            )
        return base

    # ─────────────────────────────────────────
    # COMPLETENESS CHECKS (deterministic)
    # ─────────────────────────────────────────

    all_transformations = []
    for m in graph.get("mappings", []):
        all_transformations.extend(m.get("transformations", []))

    # Every transformation documented?
    for t in all_transformations:
        present = t["name"] in documentation_md
        completeness_checks.append(CheckResult(
            name=f"Transformation '{t['name']}' documented",
            passed=present,
            detail=None if present else _missing_detail(t["name"], "Transformation")
        ))

    # Every source documented?
    for src in graph.get("sources", []):
        present = src["name"] in documentation_md
        completeness_checks.append(CheckResult(
            name=f"Source '{src['name']}' documented",
            passed=present,
            detail=None if present else _missing_detail(src["name"], "Source")
        ))

    # Every target documented?
    for tgt in graph.get("targets", []):
        present = tgt["name"] in documentation_md
        completeness_checks.append(CheckResult(
            name=f"Target '{tgt['name']}' documented",
            passed=present,
            detail=None if present else _missing_detail(tgt["name"], "Target")
        ))

    # All port expressions documented?
    for t in all_transformations:
        for expr in t.get("expressions", []):
            port_present = expr["port"] in documentation_md
            detail = None
            if not port_present:
                detail = f"Port '{expr['port']}' expression not found in docs"
                if doc_was_truncated:
                    detail += (
                        " ⚠️ Documentation was truncated — likely caused by token limit cutoff, "
                        "not a missing expression. Re-run Step 3."
                    )
            completeness_checks.append(CheckResult(
                name=f"Expression for port '{expr['port']}' in '{t['name']}' documented",
                passed=port_present,
                detail=detail,
            ))

    # Parameters documented?
    for param in parse_report.unresolved_parameters:
        present = param in documentation_md
        completeness_checks.append(CheckResult(
            name=f"Unresolved parameter '{param}' documented",
            passed=present,
            detail=None if present else _missing_detail(param, "Parameter")
        ))

    # Field-Level Lineage section present?
    lineage_present = "Field-Level Lineage" in documentation_md or "lineage" in documentation_md.lower()
    completeness_checks.append(CheckResult(
        name="Field-Level Lineage section present",
        passed=lineage_present,
        detail=None if lineage_present else "No lineage section found in documentation"
    ))

    # ─────────────────────────────────────────
    # SELF-CHECKS (deterministic)
    # ─────────────────────────────────────────

    # Classification consistent with parse?
    tier = complexity.tier
    expected_tier = _infer_expected_tier(all_transformations, graph)
    tier_consistent = tier == expected_tier or abs(
        [ComplexityTier.LOW, ComplexityTier.MEDIUM, ComplexityTier.HIGH, ComplexityTier.VERY_HIGH].index(tier) -
        [ComplexityTier.LOW, ComplexityTier.MEDIUM, ComplexityTier.HIGH, ComplexityTier.VERY_HIGH].index(expected_tier)
    ) <= 1

    self_checks.append(CheckResult(
        name="Complexity classification consistent with XML content",
        passed=tier_consistent,
        detail=None if tier_consistent else f"Assigned: {tier.value}, Expected based on re-check: {expected_tier.value}"
    ))
    if not tier_consistent:
        flags.append(_make_flag(
            "CLASSIFICATION_MISMATCH",
            "Step 2 output",
            f"Assigned tier {tier.value} may not match XML content (re-check suggests {expected_tier.value})",
            blocking=False,
        ))

    # Unsupported transformations?
    for t in all_transformations:
        if t["type"] in UNSUPPORTED_TYPES:
            flags.append(_make_flag(
                "UNSUPPORTED_TRANSFORMATION",
                f"Mapping: {mapping_name} / Transformation: {t['name']}",
                (
                    f"Type '{t['type']}' cannot be automatically converted. "
                    f"Input ports: {[p['name'] for p in t['ports'] if 'INPUT' in p.get('porttype','')]}. "
                    f"Output ports: {[p['name'] for p in t['ports'] if 'OUTPUT' in p.get('porttype','')]}."
                ),
                blocking=True,
            ))
            self_checks.append(CheckResult(
                name=f"Transformation '{t['name']}' is supported",
                passed=False,
                detail=f"Type '{t['type']}' is UNSUPPORTED — conversion of entire mapping is BLOCKED"
            ))

    # Unresolved parameters?
    for param in parse_report.unresolved_parameters:
        flags.append(_make_flag(
            "UNRESOLVED_PARAMETER",
            f"Parameter: {param}",
            f"Parameter '{param}' has no resolved value. May affect conversion output.",
            blocking=False,
        ))

    # v1.1: Unresolved $$VARIABLES from session/parameter parse (Step 0)
    if session_parse_report and session_parse_report.unresolved_variables:
        for var in session_parse_report.unresolved_variables:
            flags.append(_make_flag(
                "UNRESOLVED_VARIABLE",
                f"Session parameter: {var}",
                (
                    f"$$VARIABLE '{var}' is referenced in the session config but has no value "
                    "in the uploaded parameter file. The generated runtime_config.yaml contains "
                    "a <fill_in> placeholder. Supply the value before deploying the converted code."
                ),
                blocking=False,
            ))
        self_checks.append(CheckResult(
            name="All session $$VARIABLES resolved",
            passed=False,
            detail=(
                f"{len(session_parse_report.unresolved_variables)} unresolved variable(s): "
                + ", ".join(session_parse_report.unresolved_variables)
            ),
        ))

    # Orphaned output ports — field computed but never sent downstream
    # An output port is orphaned when it never appears as the FROM side of any connector.
    connected_sources = set()
    for m in graph.get("mappings", []):
        for conn in m.get("connectors", []):
            connected_sources.add((conn["from_instance"], conn["from_field"]))

    # Track expression-input-only ports so we can pass them to Claude and suppress
    # false DEAD_LOGIC flags for ports that feed derivations but aren't wired downstream.
    expr_input_ports: set[str] = set()   # "TRANSFORM_NAME.PORT_NAME" strings

    for t in all_transformations:
        # Skip target definitions — they have no outgoing connectors by design
        if t.get("type", "").lower() in ("target definition", "target"):
            continue

        # Build a set of all field names that appear inside any expression in this transformation.
        # These are "consumed" fields — even if their passthrough isn't wired, their values feed
        # other derived ports within the same transformation.
        expr_vars: set = set()
        for expr in t.get("expressions", []):
            expr_text = expr.get("expression", "")
            import re as _re
            tokens = set(_re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", expr_text))
            expr_vars.update(tokens)

        for port in t.get("ports", []):
            porttype = port.get("porttype", "")
            if "OUTPUT" not in porttype:
                continue
            port_name = port["name"]
            if (t["name"], port_name) in connected_sources:
                continue  # properly wired downstream — not orphaned

            # Determine whether this port is an expression-input passthrough:
            # INPUT/OUTPUT port whose value is referenced inside another expression
            # in the same transformation (e.g. ORDER_DATE feeds TO_CHAR(ORDER_DATE,...))
            is_expr_input = (
                "INPUT" in porttype               # must be INPUT/OUTPUT
                and port_name in expr_vars         # name referenced in some expression
                and port.get("expression", "") in ("", port_name)  # not itself a derived expression
            )

            if is_expr_input:
                expr_input_ports.add(f"{t['name']}.{port_name}")
                # The raw pass-through isn't wired, but the value IS consumed by derived fields.
                # No action needed — this is by design. Skip flagging entirely to avoid noise.
                # (Claude is told about these ports explicitly so it won't raise DEAD_LOGIC either.)
            else:
                flags.append(_make_flag(
                    "ORPHANED_PORT",
                    f"{t['name']}.{port_name}",
                    (
                        f"Output port '{port_name}' on '{t['name']}' has no downstream connection "
                        f"and is not referenced in any expression within the same transformation. "
                        f"This port produces no output and can likely be safely removed."
                    ),
                    blocking=False,
                ))

    # Parse status check
    if parse_report.parse_status == "FAILED":
        flags.append(_make_flag(
            "PARSE_FAILED",
            "Step 1 output",
            "XML parsing failed — conversion cannot proceed",
            blocking=True,
        ))
        self_checks.append(CheckResult(
            name="Parse completed successfully",
            passed=False,
            detail="Parse status is FAILED"
        ))
    else:
        self_checks.append(CheckResult(name="Parse completed successfully", passed=True))

    # ─────────────────────────────────────────
    # QUALITATIVE FLAGS — Claude
    # ─────────────────────────────────────────

    claude_flags = await _run_claude_quality_checks(
        documentation_md, graph, mapping_name, expr_input_ports,
        tier=complexity.tier,
    )
    # Post-filter: suppress any DEAD_LOGIC flags Claude raised for ports we know are
    # expression-input-only — they are NOT dead, they feed derivations within the same transform.
    def _is_expr_input_flag(f: VerificationFlag) -> bool:
        if f.flag_type != "DEAD_LOGIC":
            return False
        return any(eip.split(".")[-1] in f.location for eip in expr_input_ports)

    flags.extend(f for f in claude_flags if not _is_expr_input_flag(f))

    # Build accuracy checks from Claude output (summarised)
    accuracy_checks.append(CheckResult(
        name="No meaning-changing paraphrasing detected (Claude review)",
        passed=all(f.flag_type != "ACCURACY_CONCERN" for f in claude_flags),
        detail="See flags section for details" if any(f.flag_type == "ACCURACY_CONCERN" for f in claude_flags) else None
    ))
    accuracy_checks.append(CheckResult(
        name="Conditional logic fully represented (Claude review)",
        passed=all(f.flag_type != "INCOMPLETE_LOGIC" for f in claude_flags),
        detail=None
    ))

    # ─────────────────────────────────────────
    # Build final report
    # ─────────────────────────────────────────

    all_checks = completeness_checks + accuracy_checks + self_checks
    total_passed = sum(1 for c in all_checks if c.passed)
    total_failed = sum(1 for c in all_checks if not c.passed)
    blocking_flags = [f for f in flags if f.blocking]
    conversion_blocked = len(blocking_flags) > 0

    if conversion_blocked or total_failed > 0:
        overall_status = "REQUIRES_REMEDIATION"
        recommendation = "REQUIRES REMEDIATION — resolve all blocking issues and failed checks before conversion"
    else:
        overall_status = "APPROVED_FOR_CONVERSION"
        recommendation = "APPROVED FOR CONVERSION — all checks passed, proceed to Step 5 human review"

    return VerificationReport(
        mapping_name=mapping_name,
        complexity_tier=complexity.tier,
        overall_status=overall_status,
        completeness_checks=completeness_checks,
        accuracy_checks=accuracy_checks,
        self_checks=self_checks,
        flags=flags,
        total_checks=len(all_checks),
        total_passed=total_passed,
        total_failed=total_failed,
        total_flags=len(flags),
        conversion_blocked=conversion_blocked,
        blocked_reasons=[f.description for f in blocking_flags],
        recommendation=recommendation,
    )


async def _run_claude_quality_checks(
    documentation_md: str,
    graph: dict,
    mapping_name: str,
    expr_input_ports: set[str] | None = None,
    tier: ComplexityTier = ComplexityTier.MEDIUM,
) -> list[VerificationFlag]:
    """Ask Claude to identify qualitative issues in the documentation."""
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    expr_input_note = ""
    if expr_input_ports:
        port_list = ", ".join(sorted(expr_input_ports))
        expr_input_note = f"""
IMPORTANT — the following ports are INPUT/OUTPUT passthroughs that feed expressions within the
same transformation but are NOT wired to a downstream connector. They are NOT dead logic —
they are expression inputs whose derived counterparts carry the value forward. Do NOT flag
these as DEAD_LOGIC:
{port_list}
"""

    prompt = f"""You are reviewing technical documentation for an Informatica mapping called '{mapping_name}'.

Review the documentation below and identify ONLY real issues — do not invent problems.
{expr_input_note}
Look for:
1. REVIEW_REQUIRED — logic unclear or open to multiple interpretations
2. DEAD_LOGIC — transformation or port that has no effect on data (exclude expression-input ports listed above)
3. ENVIRONMENT_SPECIFIC_VALUE — hardcoded values like connection strings, file paths, server names
4. HIGH_RISK — logic that is financially sensitive or business-critical
5. LINEAGE_GAP — target field whose lineage cannot be fully traced
6. ACCURACY_CONCERN — documentation appears to paraphrase in a meaning-changing way
7. INCOMPLETE_LOGIC — conditional logic appears incomplete or oversimplified

For each issue found, respond with a JSON array. Each item:
{{
  "flag_type": "one of the types above",
  "location": "transformation name and port/field if applicable",
  "description": "specific description of the issue",
  "blocking": false,
  "severity": "HIGH or MEDIUM or LOW",
  "recommendation": "one sentence describing the specific action the reviewer should take",
  "auto_fix_suggestion": "A concrete, specific instruction that can be injected verbatim into the code generation prompt to automatically apply the fix (e.g. 'Move the hardcoded value \\\"PROD_DB\\\" for connection string in SQ_ORDERS into a config variable named DB_CONNECTION_STRING at the top of the file.' or 'Add a null check for CUSTOMER_ID before the join — filter out rows WHERE CUSTOMER_ID IS NULL in the staging CTE.'). Set to null if the issue requires human judgement rather than a mechanical code fix."
}}

If no issues found, return: []

Documentation to review:
---
{documentation_md[:15000]}
---

Respond with ONLY the JSON array. No other text."""

    try:
        import asyncio as _asyncio
        qc_max_tokens = _QC_MAX_TOKENS.get(tier, 4_096)
        # Hard timeout: verification must complete within 5 minutes.
        # Without this, a stalled Claude API call leaves the job permanently
        # stuck in 'verifying' state across server restarts.
        _VERIFY_TIMEOUT_SECS = int(os.environ.get("VERIFY_TIMEOUT_SECS", "300"))
        message = await _asyncio.wait_for(
            client.messages.create(
                model=MODEL,
                max_tokens=qc_max_tokens,
                messages=[{"role": "user", "content": prompt}],
            ),
            timeout=_VERIFY_TIMEOUT_SECS,
        )
        text = message.content[0].text.strip()
        if text.startswith("```"):
            text = text.split("```")[1]
            if text.startswith("json"):
                text = text[4:]

        # Try clean parse first; fall back to partial-recovery if Claude was truncated
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = _recover_truncated_json_array(text)

        flags = []
        for item in data:
            # Fill in meta defaults if Claude didn't supply them
            if "severity" not in item or not item["severity"]:
                item["severity"] = FLAG_META.get(item.get("flag_type",""), {}).get("severity", "MEDIUM")
            if "recommendation" not in item or not item["recommendation"]:
                item["recommendation"] = FLAG_META.get(item.get("flag_type",""), {}).get(
                    "recommendation", "Review this flag with your team before proceeding."
                )
            # Normalise auto_fix_suggestion — strip empty strings to None
            fix = item.get("auto_fix_suggestion") or None
            if fix and len(fix.strip()) < 10:  # too short to be a real suggestion
                fix = None
            item["auto_fix_suggestion"] = fix
            flags.append(VerificationFlag(**item))
        return flags
    except Exception as e:
        return [VerificationFlag(
            flag_type="REVIEW_REQUIRED",
            location="Verification Agent",
            description=f"Claude quality check could not complete: {str(e)}",
            blocking=False,
            severity="MEDIUM",
            recommendation="Re-run the verification step or check your ANTHROPIC_API_KEY and model settings.",
        )]


def _recover_truncated_json_array(text: str) -> list:
    """
    Extract all *complete* JSON objects from a potentially truncated JSON array.

    When Claude hits the token limit mid-response the output may look like:
        [{"flag_type": "HIGH_RISK", ...}, {"flag_type": "REVIEW_REQUIRED", "description": "Some long str
    — i.e. the last object's string is unterminated.  This function walks the text
    character-by-character and collects every successfully parsed ``{...}`` block,
    discarding only the incomplete tail.  This recovers all flags that were fully
    written before the cutoff.
    """
    objects: list = []
    depth = 0
    in_string = False
    escape_next = False
    start: int | None = None

    for i, ch in enumerate(text):
        if escape_next:
            escape_next = False
            continue
        if ch == "\\" and in_string:
            escape_next = True
            continue
        if ch == '"':
            in_string = not in_string
            continue
        if in_string:
            continue

        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    obj = json.loads(text[start : i + 1])
                    objects.append(obj)
                except json.JSONDecodeError:
                    pass
                start = None

    return objects


def _infer_expected_tier(transformations: list, graph: dict) -> ComplexityTier:
    """Quick re-check of tier for consistency validation."""
    num_trans = len(transformations)
    num_sources = len(graph.get("sources", []))
    types = [t["type"] for t in transformations]

    unsupported = any(t in UNSUPPORTED_TYPES for t in types)
    if unsupported or num_trans >= 30 or num_sources >= 5:
        return ComplexityTier.VERY_HIGH
    if num_trans >= 15 or num_sources >= 4:
        return ComplexityTier.HIGH
    if num_trans >= 5 or num_sources >= 2:
        return ComplexityTier.MEDIUM
    return ComplexityTier.LOW
