"""
STEP 10 — Code Quality Review Agent (v1.3: Logic Equivalence Check added)

Two-stage review:

Stage A — Logic Equivalence (v1.3):
  Goes back to the original Informatica XML as ground truth and verifies
  rule-by-rule that the generated code correctly implements every
  transformation, expression, filter, join, and null-handling pattern.
  Produces per-rule verdicts: VERIFIED / NEEDS_REVIEW / MISMATCH.

Stage B — Code Quality (existing):
  Claude cross-checks the converted code against:
    - The mapping documentation (Step 3)
    - The verification flags (Step 4)
    - The S2T field mapping (Step 2)
    - The parse report (Step 1)
  Produces a structured pass/fail checklist + overall recommendation.

No execution needed — both stages are static reviews.
"""
from __future__ import annotations
import json
import os
import anthropic

from ..models.schemas import (
    CodeReviewReport, CodeReviewCheck,
    LogicEquivalenceCheck, LogicEquivalenceReport,
    ConversionOutput, ParseReport, VerificationReport,
)

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

# ── Stage A — Logic Equivalence System & Prompt ──────────────────────────────

EQUIVALENCE_SYSTEM = """You are a senior data engineering auditor performing a logic equivalence check.
Your job is to verify that converted code correctly implements the original Informatica mapping logic
by comparing the generated code directly against the original XML — not against any documentation.
You are checking Claude's own work for errors. Be sceptical and precise.
Flag any discrepancy, no matter how small.
"""

EQUIVALENCE_PROMPT = """Perform a rule-by-rule logic equivalence check.

Compare the GENERATED {stack} CODE against the ORIGINAL INFORMATICA XML.
Do NOT use the documentation as an intermediary — go directly from XML to code.

## Original Informatica XML (ground truth)
{xml_content}

## Source-to-Target Field Mapping (S2T — structured reference)
{s2t_summary}

## Generated {stack} Code Files
{code_files}

---

For every verifiable rule you can extract from the XML, produce one check entry.
Cover all of these rule types where present:
- FIELD       : Each source-to-target field mapping — is the field present and correctly derived?
- EXPRESSION  : Each Expression transformation port formula — is the equivalent logic in the code?
- FILTER      : Each Filter or Source Qualifier filter condition — correctly implemented?
- JOIN        : Each Joiner — correct join type (INNER/LEFT/RIGHT/FULL) and join condition?
- NULL_HANDLING: Each null-handling pattern (ISNULL, NVL, default values) — preserved?
- CHAIN       : Overall transformation sequence — does the code follow the same logical order?

Verdict rules per check:
- VERIFIED      : You are confident the generated code correctly implements this rule.
- NEEDS_REVIEW  : The logic appears equivalent but involves a non-trivial translation
                  (e.g. Informatica IIF → SQL CASE WHEN) that requires human confirmation.
- MISMATCH      : The generated code does not implement this rule correctly, or the rule
                  is absent from the generated code entirely.

Return ONLY this JSON (no markdown, no explanation outside it):
{{
  "checks": [
    {{
      "rule_type": "FIELD|EXPRESSION|FILTER|JOIN|NULL_HANDLING|CHAIN",
      "rule_id": "short identifier (e.g. field name, expression name, join name)",
      "verdict": "VERIFIED|NEEDS_REVIEW|MISMATCH",
      "xml_rule": "The original rule verbatim or summarised from the XML",
      "generated_impl": "What the generated code does for this rule, or NOT FOUND",
      "note": "Brief explanation of verdict — required for NEEDS_REVIEW and MISMATCH"
    }}
  ],
  "summary": "2-3 sentence plain-English summary of equivalence findings."
}}

Be thorough — a check per field, per expression, per filter, per join.
If the XML is very large, cover all CRITICAL rules (field mappings, joins, filters) fully,
then sample expressions (cover at least 5 or all if fewer than 5 exist).
"""

# ── Stage B — Code Quality System & Prompt ───────────────────────────────────

REVIEW_SYSTEM = """You are a senior data engineering code reviewer.
Your job is to verify that converted code correctly implements the original Informatica mapping logic.
You review code STATICALLY — no execution environment available.
Be precise, concrete, and focus on correctness over style.
"""

REVIEW_PROMPT = """Review the converted {stack} code below against the original mapping documentation and field mapping.

## Original Mapping Documentation
{documentation_md}

## Verification Flags from Step 4 (issues identified in original mapping)
{flags_summary}

## Source-to-Target Field Mapping (S2T)
{s2t_summary}

## Converted Code Files
{code_files}

---

Perform these specific checks and return a JSON object:

1. **field_coverage** — Are all mapped target fields present in the final output model/script?
2. **source_filter_implemented** — Are source-level filter conditions (e.g. STATUS != 'CANCELLED') implemented?
3. **business_rules_implemented** — Are all documented business rules/expressions present in the code?
4. **hardcoded_values_flagged** — Are environment-specific hardcoded values (thresholds, rates, connection strings) externalized or at least commented?
5. **target_filter_implemented** — Are target-level filter conditions (e.g. order_amount > 0) implemented?
6. **transformation_chain_correct** — Does the transformation layer sequence (staging → intermediate → mart or equivalent) match the documented pipeline?
7. **null_handling_present** — Are nullable fields handled appropriately (COALESCE, IS NULL checks, etc.)?
8. **naming_consistency** — Do output field names match the documented target schema (allowing for reasonable snake_case conversions)?
9. **no_extra_fields** — Does the final output avoid introducing undocumented extra fields?
10. **flags_addressed** — Are CRITICAL/HIGH severity flags from verification acknowledged or handled in the code?

Return ONLY this JSON (no markdown, no explanation outside it):
{{
  "checks": [
    {{"name": "field_coverage",              "passed": true, "severity": "CRITICAL", "note": "..."}},
    {{"name": "source_filter_implemented",   "passed": true, "severity": "HIGH",     "note": "..."}},
    {{"name": "business_rules_implemented",  "passed": true, "severity": "CRITICAL", "note": "..."}},
    {{"name": "hardcoded_values_flagged",    "passed": true, "severity": "MEDIUM",   "note": "..."}},
    {{"name": "target_filter_implemented",   "passed": true, "severity": "HIGH",     "note": "..."}},
    {{"name": "transformation_chain_correct","passed": true, "severity": "CRITICAL", "note": "..."}},
    {{"name": "null_handling_present",       "passed": true, "severity": "LOW",      "note": "..."}},
    {{"name": "naming_consistency",          "passed": true, "severity": "MEDIUM",   "note": "..."}},
    {{"name": "no_extra_fields",             "passed": true, "severity": "LOW",      "note": "..."}},
    {{"name": "flags_addressed",             "passed": true, "severity": "HIGH",     "note": "..."}}
  ],
  "recommendation": "APPROVED",
  "summary": "2-3 sentence plain-English verdict on the overall code quality."
}}

Rules for recommendation:
- APPROVED: all CRITICAL and HIGH checks pass
- REVIEW_RECOMMENDED: all CRITICAL checks pass but ≥1 HIGH fails, or code output was degraded
- REQUIRES_FIXES: any CRITICAL check fails
"""


# ── Helpers ───────────────────────────────────────────────────────────────────

def _format_flags(verification: dict) -> str:
    flags = verification.get("flags", [])
    if not flags:
        return "No flags raised."
    lines = []
    for f in flags:
        sev      = f.get("severity", "MEDIUM")
        ftype    = f.get("flag_type", f.get("type", "?"))
        loc      = f.get("location", "")
        desc     = f.get("description", "")
        blocking = "BLOCKING" if f.get("blocking") else ""
        lines.append(f"[{sev}] {ftype} @ {loc} {blocking} — {desc}")
    return "\n".join(lines)


def _format_s2t(s2t: dict) -> str:
    if not s2t:
        return "S2T mapping not available."
    summary = s2t.get("summary", {})
    records = s2t.get("records", [])
    lines = [
        f"Mapped fields: {summary.get('mapped_fields', '?')}",
        f"Unmapped target fields: {summary.get('unmapped_target_fields', '?')}",
        f"Unmapped source fields: {summary.get('unmapped_source_fields', '?')}",
        "",
        "Field mappings (source → target):",
    ]
    for r in records[:40]:   # cap at 40 rows to fit in context
        logic = f" [{r['logic']}]" if r.get("logic") else ""
        lines.append(
            f"  {r.get('source_table','?')}.{r.get('source_field','?')} "
            f"→ {r.get('target_table','?')}.{r.get('target_field','?')}"
            f" ({r.get('status','?')}){logic}"
        )
    if len(records) > 40:
        lines.append(f"  ... and {len(records)-40} more fields")
    unmapped_tgt = s2t.get("unmapped_targets", [])
    if unmapped_tgt:
        lines.append("\nUnmapped target fields (no source):")
        for u in unmapped_tgt:
            lines.append(f"  {u['target_table']}.{u['target_field']}")
    return "\n".join(lines)


def _format_code(files: dict[str, str]) -> str:
    parts = []
    for fname, content in files.items():
        # Truncate very large files
        if len(content) > 3000:
            content = content[:3000] + f"\n... [truncated — {len(content)} chars total]"
        parts.append(f"### {fname}\n```\n{content}\n```")
    return "\n\n".join(parts)


def _parse_json(raw: str) -> dict:
    """Strip markdown fences and parse JSON."""
    raw = raw.strip()
    if "```json" in raw:
        raw = raw.split("```json", 1)[1].split("```")[0].strip()
    elif "```" in raw:
        raw = raw.split("```", 1)[1].split("```")[0].strip()
    return json.loads(raw)


# ── Stage A — Logic Equivalence ───────────────────────────────────────────────

async def _run_equivalence_check(
    client: anthropic.AsyncAnthropic,
    stack: str,
    xml_content: str,
    s2t: dict,
    files: dict[str, str],
) -> LogicEquivalenceReport:
    """Call Claude with the original XML + generated code and get per-rule verdicts."""
    # Cap XML at 10,000 chars — enough to extract transformation logic without blowing context
    xml_excerpt = xml_content[:10_000]
    if len(xml_content) > 10_000:
        xml_excerpt += f"\n... [XML truncated — {len(xml_content)} chars total; key transformations shown]"

    prompt = EQUIVALENCE_PROMPT.format(
        stack=stack,
        xml_content=xml_excerpt,
        s2t_summary=_format_s2t(s2t),
        code_files=_format_code(files),
    )

    message = await client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=EQUIVALENCE_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    data = _parse_json(message.content[0].text)

    checks = [LogicEquivalenceCheck(**c) for c in data.get("checks", [])]
    total        = len(checks)
    verified     = sum(1 for c in checks if c.verdict == "VERIFIED")
    needs_review = sum(1 for c in checks if c.verdict == "NEEDS_REVIEW")
    mismatches   = sum(1 for c in checks if c.verdict == "MISMATCH")
    coverage_pct = round((verified + needs_review) / total * 100, 1) if total else 0.0

    return LogicEquivalenceReport(
        total_verified=verified,
        total_needs_review=needs_review,
        total_mismatches=mismatches,
        coverage_pct=coverage_pct,
        checks=checks,
        summary=data.get("summary", ""),
    )


# ── Stage B — Code Quality ────────────────────────────────────────────────────

async def _run_quality_check(
    client: anthropic.AsyncAnthropic,
    conversion_output: ConversionOutput,
    documentation_md: str,
    verification: dict,
    s2t: dict,
) -> tuple[list[CodeReviewCheck], str]:
    """Run the existing 10-check code quality review. Returns (checks, recommendation, summary)."""
    prompt = REVIEW_PROMPT.format(
        stack=conversion_output.target_stack.value,
        documentation_md=documentation_md[:12_000],
        flags_summary=_format_flags(verification),
        s2t_summary=_format_s2t(s2t),
        code_files=_format_code(conversion_output.files),
    )

    message = await client.messages.create(
        model=MODEL,
        max_tokens=4096,
        system=REVIEW_SYSTEM,
        messages=[{"role": "user", "content": prompt}],
    )

    data   = _parse_json(message.content[0].text)
    checks = [CodeReviewCheck(**c) for c in data.get("checks", [])]
    return checks, data.get("recommendation", "REVIEW_RECOMMENDED"), data.get("summary", "")


# ── Public entry point ────────────────────────────────────────────────────────

async def review(
    conversion_output: ConversionOutput,
    documentation_md: str,
    verification: dict,
    s2t: dict,
    parse_report: ParseReport,
    xml_content: str = "",   # v1.3 — original Informatica XML for equivalence check
) -> CodeReviewReport:
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    # Stage A — Logic Equivalence (runs when xml_content is available)
    equivalence_report: LogicEquivalenceReport | None = None
    if xml_content:
        try:
            equivalence_report = await _run_equivalence_check(
                client=client,
                stack=conversion_output.target_stack.value,
                xml_content=xml_content,
                s2t=s2t,
                files=conversion_output.files,
            )
        except Exception as e:
            # Non-blocking — log and continue without equivalence report
            equivalence_report = LogicEquivalenceReport(
                total_verified=0,
                total_needs_review=0,
                total_mismatches=0,
                coverage_pct=0.0,
                checks=[],
                summary=f"Logic equivalence check could not complete: {e}. Review the code manually.",
            )

    # Stage B — Code Quality
    try:
        checks, recommendation, summary = await _run_quality_check(
            client=client,
            conversion_output=conversion_output,
            documentation_md=documentation_md,
            verification=verification,
            s2t=s2t,
        )
    except Exception as e:
        checks         = []
        recommendation = "REVIEW_RECOMMENDED"
        summary        = f"Automated quality review could not complete: {e}. Please review the converted code manually."

    total_passed = sum(1 for c in checks if c.passed)
    total_failed = len(checks) - total_passed

    # If equivalence check found mismatches, cap recommendation at REVIEW_RECOMMENDED minimum
    if equivalence_report and equivalence_report.total_mismatches > 0:
        if recommendation == "APPROVED":
            recommendation = "REVIEW_RECOMMENDED"

    # Override: if conversion was degraded, cap at REVIEW_RECOMMENDED
    if not conversion_output.parse_ok and recommendation == "APPROVED":
        recommendation = "REVIEW_RECOMMENDED"

    return CodeReviewReport(
        mapping_name=conversion_output.mapping_name,
        target_stack=conversion_output.target_stack.value,
        checks=checks,
        total_passed=total_passed,
        total_failed=total_failed,
        recommendation=recommendation,
        summary=summary,
        parse_degraded=not conversion_output.parse_ok,
        equivalence_report=equivalence_report,
    )
