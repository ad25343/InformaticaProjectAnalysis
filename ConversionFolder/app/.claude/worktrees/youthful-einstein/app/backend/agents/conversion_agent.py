"""
STEP 6+7 — Stack Assignment + Conversion Agent
Rule-based stack assignment, then Claude-powered conversion.
Conversion source of truth = the verified documentation (Step 3), NOT raw XML.
"""
from __future__ import annotations
import json
import re
import os
import anthropic

from typing import Optional
from ..models.schemas import (
    ComplexityReport, ComplexityTier, StackAssignment,
    ConversionOutput, TargetStack, ParseReport, SessionParseReport
)
from ..security_knowledge import build_security_context_block

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")


# ─────────────────────────────────────────────
# STEP 6 — Stack Assignment
# ─────────────────────────────────────────────

async def assign_stack(
    complexity: ComplexityReport,
    graph: dict,
    parse_report: ParseReport,
) -> StackAssignment:
    mapping_name = parse_report.mapping_names[0] if parse_report.mapping_names else "unknown"
    tier = complexity.tier

    all_trans_types = []
    for m in graph.get("mappings", []):
        all_trans_types.extend(t["type"] for t in m.get("transformations", []))

    # Determine stack by rules
    special_concerns: list[str] = []

    if tier in (ComplexityTier.HIGH, ComplexityTier.VERY_HIGH):
        stack = TargetStack.PYSPARK
    elif _is_sql_friendly(all_trans_types):
        stack = TargetStack.DBT
    elif tier == ComplexityTier.LOW:
        stack = TargetStack.PYTHON
    else:
        stack = TargetStack.PYSPARK  # Medium default to PySpark for safety

    if "HTTP Transformation" in all_trans_types:
        special_concerns.append("HTTP Transformation — API integration required in converted code")
    if any("Stored Procedure" in t for t in all_trans_types):
        special_concerns.append("Stored procedure references — will require manual resolution")

    # Ask Claude for written rationale
    rationale = await _get_stack_rationale(stack, complexity, all_trans_types)

    return StackAssignment(
        mapping_name=mapping_name,
        complexity_tier=tier,
        assigned_stack=stack,
        rationale=rationale,
        data_volume_est=complexity.data_volume_est,
        special_concerns=special_concerns,
    )


def _is_sql_friendly(trans_types: list[str]) -> bool:
    sql_friendly = {"Expression", "Filter", "Aggregator", "Joiner",
                    "Lookup", "Router", "Source Qualifier", "Sorter"}
    non_sql = {"Java Transformation", "External Procedure", "HTTP Transformation",
               "Normalizer", "Transaction Control"}
    present = set(trans_types)
    return not (present & non_sql) and bool(present & sql_friendly)


async def _get_stack_rationale(stack: TargetStack, complexity: ComplexityReport,
                                trans_types: list) -> str:
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    prompt = (
        f"A mapping has been assigned to {stack.value}.\n"
        f"Complexity tier: {complexity.tier.value}\n"
        f"Criteria: {'; '.join(complexity.criteria_matched)}\n"
        f"Transformation types present: {', '.join(set(trans_types))}\n\n"
        "Write a 2-3 sentence rationale for this stack assignment, "
        "tied to the specific criteria and transformation types listed. "
        "Be concrete. No fluff."
    )
    try:
        msg = await client.messages.create(
            model=MODEL, max_tokens=300,
            messages=[{"role": "user", "content": prompt}]
        )
        return msg.content[0].text
    except Exception:
        return f"Assigned {stack.value} based on complexity tier {complexity.tier.value}."


# ─────────────────────────────────────────────
# STEP 7 — Convert
# ─────────────────────────────────────────────

_DW_AUDIT_RULES = """
Standard DW audit fields — apply to ALL target tables regardless of documentation:
- Any target field matching DW_INSERT_DT, DW_LOAD_DT, ETL_INSERT_DT, or similar patterns
  → populate with current_timestamp() / CURRENT_TIMESTAMP / datetime.utcnow()
- Any target field matching DW_UPDATE_DT, DW_LAST_UPDATE_DT, ETL_UPDATE_DT
  → populate with current_timestamp()
- Any target field matching ETL_BATCH_ID, BATCH_ID
  → populate from a job parameter or generate a UUID
- Any target field matching ETL_SOURCE, SOURCE_SYSTEM, ETL_SOURCE_SYSTEM
  → populate with the source system name extracted from the mapping name or config
These fields are standard DW convention and are intentionally "unmapped" in Informatica
because PowerCenter populates them automatically via session-level parameters.
Never leave them NULL in generated code — always populate with appropriate runtime values.
"""

PYSPARK_SYSTEM = """You are a senior data engineer converting Informatica PowerCenter mappings to PySpark.

Rules:
- Work ONLY from the documentation provided — never invent logic not documented there
- Use DataFrame API only (no RDD)
- Define schema explicitly — no inferred schemas
- Use native Spark functions — UDFs only as last resort, document why
- Add structured logging (row counts) at each major step
- Externalize all hardcoded env-specific values to a config dict at the top
- Add inline comments for every business rule
- Where no direct Spark equivalent exists, comment the design decision
- Output complete, runnable Python files
""" + _DW_AUDIT_RULES

DBT_SYSTEM = """You are a senior analytics engineer converting Informatica PowerCenter mappings to dbt.

Rules:
- Work ONLY from the documentation provided — never invent logic not documented there
- Match the number of models to the actual mapping complexity:
    * Simple mapping (1 source → 1 target, basic expressions/filters): ONE model + sources.yml + dbt_project.yml
    * Medium mapping (multiple sources, lookups, or aggregations): staging model + final model + sources.yml + dbt_project.yml
    * Complex mapping (multiple joins, SCD, complex routing): staging + intermediate + mart + sources.yml + dbt_project.yml
- Do NOT create intermediate layers that add no transformation value
- Define sources in sources.yml (required)
- Add tests only for primary keys and not-null on critical fields — keep schema YMLs lean
- Combine all model schema docs into a single schema.yml per folder rather than one YML per model
- Output complete, runnable SQL model files
""" + _DW_AUDIT_RULES

PYTHON_SYSTEM = """You are a senior data engineer converting Informatica PowerCenter mappings to Python (Pandas).

Rules:
- Work ONLY from the documentation provided — never invent logic not documented there
- One function per logical transformation step
- Functions must be independently testable
- Add type hints to all functions
- Structured JSON logging
- Externalize config — no hardcoded values
- Use chunked reading for larger files
- Output complete, runnable Python files
""" + _DW_AUDIT_RULES

CONVERSION_PROMPT = """Convert the Informatica mapping documented below to {stack}.

{security_context}
## Stack Assignment Rationale
{rationale}
{approved_fixes_section}
## Full Mapping Documentation (your source of truth)
{documentation_md}

## Conversion Requirements
- Follow the documented logic EXACTLY
- Every business rule from the docs → inline comment in the code
- All hardcoded env-specific values → config dict / config file
- Structured logging at: job start, after each major transformation, job end (with row counts)
- Reject/error handling as documented
- Where a Reviewer-Approved Fix is listed above, apply it precisely as described

Output complete, production-ready code.

Use this EXACT file delimiter format — do NOT use JSON, markdown code blocks, or any other wrapper:

<<<BEGIN_FILE: path/to/filename.ext>>>
<complete file contents here, raw — no escaping needed>
<<<END_FILE>>>

<<<BEGIN_FILE: path/to/another_file.ext>>>
<complete file contents here>
<<<END_FILE>>>

<<<NOTES>>>
Any conversion decisions or warnings, one per line.
<<<END_NOTES>>>

Rules for the delimiter format:
- The <<<BEGIN_FILE: ...>>> and <<<END_FILE>>> markers must be on their own lines
- Write file contents exactly as they would appear on disk — no escaping, no indentation of the delimiters
- Every file must have both BEGIN_FILE and END_FILE markers
- Put NOTES section at the end
"""


def _build_yaml_artifacts(spr: SessionParseReport) -> dict[str, str]:
    """
    Generate connections.yaml and runtime_config.yaml from Step 0 session data.
    Returns a dict of {filename: yaml_content}.
    """
    artifacts: dict[str, str] = {}

    sc = spr.session_config

    # ── connections.yaml ──────────────────────────────────────────────────
    conn_lines: list[str] = [
        "# connections.yaml — generated by Informatica Conversion Tool v1.1",
        f"# Session: {sc.session_name if sc else 'N/A'}",
        f"# Workflow: {sc.workflow_name if sc else 'N/A'}",
        "#",
        "# Fill in the <placeholders> before use.",
        "",
        "connections:",
    ]

    if sc and sc.connections:
        seen: set[str] = set()
        for conn in sc.connections:
            conn_key = conn.connection_name or conn.transformation_name
            if conn_key in seen:
                continue
            seen.add(conn_key)
            conn_lines.append(f"  {conn.transformation_name}:")
            conn_lines.append(f"    role: {conn.role}")
            if conn.connection_name:
                conn_lines.append(f"    connection_name: {conn.connection_name}")
            if conn.connection_type:
                conn_lines.append(f"    connection_type: {conn.connection_type}")
            if conn.file_name:
                conn_lines.append(f"    file_name: {conn.file_name}")
            if conn.file_dir:
                conn_lines.append(f"    file_dir: {conn.file_dir}")
            # Placeholder for actual DB connection string
            if conn.connection_type and conn.connection_type.upper() in ("RELATIONAL", "ODBC"):
                conn_lines.append("    jdbc_url: <fill_in>")
                conn_lines.append("    username: <fill_in>")
                conn_lines.append("    password: <fill_in>")
    else:
        conn_lines.append("  # No connections extracted from Workflow XML")

    if spr.parameters:
        conn_lines += [
            "",
            "# Resolved parameters (from parameter file)",
            "parameters:",
        ]
        for p in spr.parameters:
            # Escape any special chars in YAML value
            safe_val = str(p.value).replace('"', '\\"')
            conn_lines.append(f'  {p.name}: "{safe_val}"  # scope: {p.scope}')

    if spr.unresolved_variables:
        conn_lines += [
            "",
            "# Unresolved variables — values must be supplied at runtime",
            "unresolved_variables:",
        ]
        for v in spr.unresolved_variables:
            conn_lines.append(f"  {v}: <fill_in>")

    artifacts["config/connections.yaml"] = "\n".join(conn_lines) + "\n"

    # ── runtime_config.yaml ───────────────────────────────────────────────
    rt_lines: list[str] = [
        "# runtime_config.yaml — generated by Informatica Conversion Tool v1.1",
        f"# Session: {sc.session_name if sc else 'N/A'}",
        f"# Workflow: {sc.workflow_name if sc else 'N/A'}",
        "",
        "session:",
    ]

    if sc:
        rt_lines.append(f"  name: {sc.session_name}")
        rt_lines.append(f"  mapping: {sc.mapping_name}")
        rt_lines.append(f"  workflow: {sc.workflow_name}")

        if sc.commit_interval is not None:
            rt_lines.append(f"  commit_interval: {sc.commit_interval}")
        if sc.error_threshold is not None:
            rt_lines.append(f"  error_threshold: {sc.error_threshold}")
        if sc.reject_filename:
            rt_lines.append(f"  reject_filename: {sc.reject_filename}")
        if sc.reject_filedir:
            rt_lines.append(f"  reject_filedir: {sc.reject_filedir}")
        if sc.pre_session_sql:
            rt_lines += ["", "  pre_session_sql: |"]
            for ln in sc.pre_session_sql.splitlines():
                rt_lines.append(f"    {ln}")
        if sc.post_session_sql:
            rt_lines += ["", "  post_session_sql: |"]
            for ln in sc.post_session_sql.splitlines():
                rt_lines.append(f"    {ln}")
    else:
        rt_lines.append("  # No session config extracted from Workflow XML")

    artifacts["config/runtime_config.yaml"] = "\n".join(rt_lines) + "\n"

    return artifacts


async def convert(
    stack_assignment: StackAssignment,
    documentation_md: str,
    graph: dict,
    accepted_fixes: list[str] | None = None,
    security_findings: list[dict] | None = None,
    session_parse_report: Optional[SessionParseReport] = None,
) -> ConversionOutput:
    """
    Generate converted code for the assigned target stack.

    accepted_fixes     — reviewer-approved code-level fixes from Gate 1 (Step 5)
    security_findings  — security scan findings from a previous round (REQUEST_FIX path);
                         Claude must address every listed finding in this regeneration
    """
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))
    stack = stack_assignment.assigned_stack

    system_map = {
        TargetStack.PYSPARK: PYSPARK_SYSTEM,
        TargetStack.DBT:     DBT_SYSTEM,
        TargetStack.PYTHON:  PYTHON_SYSTEM,
        TargetStack.HYBRID:  PYSPARK_SYSTEM,  # Default to PySpark for hybrid MVP
    }

    # Build the optional "Reviewer-Approved Fixes" section (from Gate 1)
    if accepted_fixes:
        numbered = "\n".join(f"{i+1}. {fix}" for i, fix in enumerate(accepted_fixes))
        approved_fixes_section = (
            f"\n## ⚠️ Reviewer-Approved Fixes — Apply These Exactly\n"
            f"The human reviewer has reviewed the verification flags and approved the following "
            f"specific fixes. You MUST apply each one precisely as described — do not skip, "
            f"paraphrase, or generalise:\n\n{numbered}\n\n"
        )
    else:
        approved_fixes_section = ""

    # Build the optional "Security Findings to Fix" section (from Gate 2 REQUEST_FIX)
    if security_findings:
        finding_lines = []
        for i, f in enumerate(security_findings, 1):
            sev      = f.get("severity", "UNKNOWN")
            ftype    = f.get("test_name") or f.get("finding_type", "security issue")
            location = f.get("filename") or f.get("location", "")
            desc     = f.get("text") or f.get("description", "")
            fix      = f.get("remediation", "")
            code     = f.get("code", "").strip()
            line     = f.get("line")
            line_ref = f" line {line}" if line else ""
            snippet  = f"\n   Offending code ({location}{line_ref}):\n   ```\n   {code}\n   ```" if code else ""
            finding_lines.append(
                f"{i}. [{sev}] {ftype} — {location}{line_ref}\n"
                f"   Issue: {desc}{snippet}\n"
                f"   Fix required: {fix if fix else 'Do not reproduce this pattern in the regenerated code.'}"
            )
        security_fix_section = (
            "\n## 🔒 Security Findings — You MUST Fix All of These\n"
            "A human security reviewer rejected the previous code generation and requested fixes. "
            "The EXACT offending code snippets are shown for each finding — you must NOT reproduce "
            "these patterns anywhere in the regenerated code. For each finding, apply the stated "
            "fix or an equivalent secure alternative:\n\n"
            + "\n\n".join(finding_lines)
            + "\n\n"
        )
    else:
        security_fix_section = ""

    # Load standing rules + learned patterns from the knowledge base
    try:
        security_context = build_security_context_block()
    except Exception:
        security_context = ""  # never block a conversion due to KB read failure

    prompt = CONVERSION_PROMPT.format(
        stack=stack.value,
        rationale=stack_assignment.rationale,
        security_context=security_context,
        approved_fixes_section=approved_fixes_section + security_fix_section,
        documentation_md=documentation_md[:30_000],
    )

    message = await client.messages.create(
        model=MODEL,
        max_tokens=32000,
        system=system_map.get(stack, PYSPARK_SYSTEM),
        messages=[{"role": "user", "content": prompt}],
    )

    raw = message.content[0].text.strip()
    stop_reason = message.stop_reason  # "end_turn" | "max_tokens"

    # ── Parse delimiter format ─────────────────────────────────────────────
    # Format: <<<BEGIN_FILE: path>>>...content...<<<END_FILE>>>
    notes: list[str] = []
    files: dict[str, str] = {}
    parsed_ok = False

    begin_pattern = re.compile(r"<<<BEGIN_FILE:\s*(.+?)>>>", re.IGNORECASE)
    end_marker = "<<<END_FILE>>>"
    notes_begin = "<<<NOTES>>>"
    notes_end = "<<<END_NOTES>>>"

    pos = 0
    while pos < len(raw):
        m = begin_pattern.search(raw, pos)
        if not m:
            break
        fname = m.group(1).strip()
        content_start = m.end()
        end_pos = raw.find(end_marker, content_start)
        if end_pos == -1:
            # Truncated — take everything remaining as the file content
            content = raw[content_start:].lstrip("\n")
            files[fname] = content
            notes.append(f"File '{fname}' appears truncated (no END_FILE marker found).")
            break
        content = raw[content_start:end_pos].lstrip("\n").rstrip("\n")
        files[fname] = content
        pos = end_pos + len(end_marker)

    # Extract NOTES section
    n_start = raw.find(notes_begin)
    if n_start != -1:
        n_end = raw.find(notes_end, n_start)
        notes_text = raw[n_start + len(notes_begin): n_end if n_end != -1 else len(raw)].strip()
        for line in notes_text.splitlines():
            line = line.strip("- •").strip()
            if line:
                notes.append(line)

    if files:
        parsed_ok = True
        if stop_reason == "max_tokens":
            notes.append(
                "⚠️ Response reached the token limit — one or more files may be truncated. "
                "Review the last file carefully. If incomplete, re-run the job."
            )
    else:
        # Fallback: model didn't use delimiters at all — save raw response
        parsed_ok = False
        files = {f"converted_{stack.value.lower()}_raw.txt": raw}
        notes.append(
            "Conversion output did not use the expected file delimiter format. "
            "Raw response saved. Re-running the job should resolve this."
        )

    # ── v1.1: inject YAML config artifacts if session data is available ─────
    if session_parse_report and session_parse_report.session_config:
        try:
            yaml_files = _build_yaml_artifacts(session_parse_report)
            files.update(yaml_files)
            notes.append(
                f"v1.1: Generated {len(yaml_files)} config artifact(s): "
                + ", ".join(yaml_files.keys())
            )
        except Exception as e:
            notes.append(f"v1.1: YAML artifact generation failed (non-blocking): {e}")

    return ConversionOutput(
        mapping_name=stack_assignment.mapping_name,
        target_stack=stack,
        files=files,
        notes=notes,
        parse_ok=parsed_ok,
    )
