"""
STEP 3 — Documentation Agent
Claude-powered. Produces the full Markdown documentation file per mapping.
Works strictly from the parsed graph — never from general Informatica knowledge.

Two-pass strategy (primary approach)
--------------------------------------
Large or complex mappings routinely exceed single-call output limits even with
the extended-output beta enabled.  We always use two sequential Claude calls:

  Pass 1 — Overview + all Transformations + Parameters & Variables
  Pass 2 — Field-Level Lineage + Session & Runtime Context + Ambiguities

Pass 2 receives Pass 1's output as context so lineage traces have the full
transformation detail to reference.  The combined output is returned as one
Markdown document.

Each call requests 64 000 tokens with the extended-output beta, giving a
theoretical ceiling of 128 000 output tokens — sufficient for any Informatica
mapping in practice.
"""
from __future__ import annotations
import json
import logging
import os
import anthropic

from typing import Optional
from ..models.schemas import ComplexityReport, ComplexityTier, ParseReport, SessionParseReport

log = logging.getLogger("conversion.documentation_agent")

MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-4-5-20250929")

_DOC_MAX_TOKENS       = 64_000
_EXTENDED_OUTPUT_BETA = "output-128k-2025-02-19"

# Sentinel appended to the markdown when Claude hit the token limit on either pass.
# The orchestrator checks for this before advancing to Step 4 and fails the job
# immediately rather than running verification on an incomplete document.
DOC_TRUNCATION_SENTINEL = "\n\n<!-- DOC_TRUNCATED -->"

# Sentinel appended to the markdown when both passes complete without truncation.
# The orchestrator requires this to be present before advancing to Step 4.
DOC_COMPLETE_SENTINEL = "\n\n<!-- DOC_COMPLETE -->"

# ── Prompts ──────────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """You are a senior data engineer deeply familiar with Informatica PowerCenter.

You are producing technical documentation for a mapping that will be converted to modern code.
Your ONLY source of truth is the structured data provided. Never invent or assume logic.

Rules:
- Document every transformation — never skip one because it seems trivial
- Preserve every expression verbatim AND explain it in plain English
- Never paraphrase in a way that changes meaning
- Flag AMBIGUITY explicitly — do not guess
- If UNSUPPORTED TRANSFORMATION is present, document all visible ports/metadata and clearly flag it
- Output ONLY the Markdown document — no preamble, no commentary outside the doc
"""

PASS_1_PROMPT = """Produce the FIRST PART of technical documentation for the Informatica mapping below.

## Parse Summary
{parse_summary}

## Complexity Classification
{complexity_summary}

## Full Mapping Graph (JSON)
```json
{graph_json}
```

## Instructions

Write ONLY the following sections — stop after completing Parameters and Variables:

# Mapping: [name]

## Overview
- Inferred purpose (plain English)
- Source systems and tables
- Target systems and tables
- Complexity tier and rationale
- High-level data flow narrative

## Transformations (in execution order)
For EACH transformation — document ALL of them, do not skip any:
### [Transformation Name] — [Type]
- **Purpose**: What business logic does this perform?
- **Input Ports**: table with name, datatype, source
- **Output Ports**: table with name, datatype, destination
- **Logic Detail**: every expression verbatim + plain English explanation
- **Table Attributes**: join conditions, lookup conditions, filter conditions, SQL overrides — all verbatim
- **Hardcoded Values**: list any constants
- If UNSUPPORTED: state UNSUPPORTED TRANSFORMATION clearly, document all visible metadata

## Parameters and Variables
Table: name | datatype | default value | purpose | resolved?

Do NOT write Field-Level Lineage, Session & Runtime Context, or Ambiguities and Flags — \
those will be produced in a second pass.
"""

PASS_2_PROMPT = """You are completing the documentation for an Informatica mapping.
Pass 1 has already documented the Overview, all Transformations, and Parameters & Variables.

## Pass 1 Documentation (already written — use as reference)
{pass1_doc}

## Full Mapping Graph (JSON)
```json
{graph_json}
```
{session_context_block}
## Instructions

Write ONLY the following remaining sections:

## Field-Level Lineage
For every target field, trace: source field → each transformation step → target field.
State what happened at each step (passthrough / renamed / retyped / derived / aggregated / lookup / generated).
Flag as LINEAGE GAP if the trace cannot be completed.
Use the transformation detail from Pass 1 above as your primary reference.

## Session & Runtime Context
(Populated from Workflow XML and parameter file when uploaded)
- Connection names and types for each source/target
- Pre-session and post-session SQL
- Reject file configuration
- Commit interval and error threshold
- Resolved $$VARIABLES (name → value)
- Any unresolved $$VARIABLES

## Ambiguities and Flags
List every point of uncertainty across the full mapping, with location and description.
Include anything flagged in Pass 1 transformations plus any lineage gaps found above.

Output ONLY these three sections — do not repeat any content from Pass 1.
"""


# ── Helpers ──────────────────────────────────────────────────────────────────

def _count_transformations(graph: dict) -> int:
    return sum(
        len(m.get("transformations", []))
        for m in graph.get("mappings", [])
    )


def _build_session_context_block(spr: Optional[SessionParseReport]) -> str:
    """
    Build a plain-text block describing the session config and resolved parameters
    so Claude can include them in the documentation.
    Returns an empty string when no session data is available.
    """
    if not spr:
        return ""

    lines: list[str] = ["## Session & Runtime Context (Step 0 data)"]

    # Cross-reference status
    cr = spr.cross_ref
    lines.append(f"Cross-reference validation: {cr.status}")
    if cr.mapping_name:
        lines.append(f"  Mapping name: {cr.mapping_name}")
    if cr.session_name:
        lines.append(f"  Session name: {cr.session_name}")
    if cr.workflow_name if hasattr(cr, 'workflow_name') else False:
        lines.append(f"  Workflow name: {cr.workflow_name}")  # type: ignore
    if cr.issues:
        lines.append("  Issues: " + "; ".join(cr.issues))

    # Session config
    sc = spr.session_config
    if sc:
        lines.append(f"\nSession: {sc.session_name}  (Workflow: {sc.workflow_name})")
        if sc.connections:
            lines.append("Connections:")
            for conn in sc.connections:
                parts = [f"  {conn.role}: {conn.transformation_name}"]
                if conn.connection_name:
                    parts.append(f"connection={conn.connection_name}")
                if conn.connection_type:
                    parts.append(f"type={conn.connection_type}")
                if conn.file_name:
                    parts.append(f"file={conn.file_name}")
                if conn.file_dir:
                    parts.append(f"dir={conn.file_dir}")
                lines.append("  " + "  ".join(parts))
        if sc.pre_session_sql:
            lines.append(f"Pre-session SQL: {sc.pre_session_sql[:500]}")
        if sc.post_session_sql:
            lines.append(f"Post-session SQL: {sc.post_session_sql[:500]}")
        if sc.commit_interval is not None:
            lines.append(f"Commit interval: {sc.commit_interval}")
        if sc.error_threshold is not None:
            lines.append(f"Error threshold: {sc.error_threshold}")
        if sc.reject_filename:
            lines.append(f"Reject file: {sc.reject_filedir or ''}/{sc.reject_filename}")

    # Resolved parameters
    if spr.parameters:
        lines.append("\nResolved parameters ($$VARIABLES):")
        for p in spr.parameters:
            lines.append(f"  {p.name} = {p.value}  [{p.scope}]")

    if spr.unresolved_variables:
        lines.append("\nUnresolved variables (no value in parameter file):")
        for v in spr.unresolved_variables:
            lines.append(f"  {v}")

    return "\n".join(lines)


async def _claude_call(client: anthropic.AsyncAnthropic, prompt: str, pass_label: str) -> tuple[str, bool]:
    """
    Make a single documentation Claude call.
    Returns (text, truncated).

    No timeout is applied here — the call is async so it does not block the event
    loop regardless of how long Claude takes.  The orchestrator runs a 30-second
    heartbeat loop alongside this call so the UI shows live elapsed time.
    """
    _override = os.environ.get("DOC_MAX_TOKENS_OVERRIDE")
    max_tokens = int(_override) if _override else _DOC_MAX_TOKENS

    log.info("documentation_agent: %s — requesting max_tokens=%d", pass_label, max_tokens)

    message = await client.messages.create(
        model=MODEL,
        max_tokens=max_tokens,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": prompt}],
        extra_headers={"anthropic-beta": _EXTENDED_OUTPUT_BETA},
    )

    text = message.content[0].text
    truncated = message.stop_reason == "max_tokens"

    if truncated:
        log.warning("documentation_agent: %s hit token limit — doc may be incomplete", pass_label)

    return text, truncated


# ── Public entry point ────────────────────────────────────────────────────────

async def document(
    parse_report: ParseReport,
    complexity: ComplexityReport,
    graph: dict,
    session_parse_report: Optional[SessionParseReport] = None,
) -> str:
    """
    Returns the full documentation as a Markdown string produced by two
    sequential Claude calls (Pass 1: transformations, Pass 2: lineage).

    The returned string ends with one of two sentinels:
      DOC_COMPLETE_SENTINEL    — both passes finished without truncation; safe to advance
      DOC_TRUNCATION_SENTINEL  — a pass hit the token limit; orchestrator should fail the job

    The orchestrator checks the sentinel immediately after this call and stops
    the pipeline at Step 3 if the document is incomplete, rather than running
    verification on a partial document.
    """
    client = anthropic.AsyncAnthropic(api_key=os.environ.get("ANTHROPIC_API_KEY"))

    parse_summary = (
        f"Parse Status: {parse_report.parse_status}\n"
        f"Objects Found: {json.dumps(parse_report.objects_found)}\n"
        f"Mappings: {', '.join(parse_report.mapping_names)}\n"
        f"Unresolved Parameters: {parse_report.unresolved_parameters}\n"
        f"Flags: {len(parse_report.flags)}"
    )

    complexity_summary = (
        f"Tier: {complexity.tier.value}\n"
        f"Criteria: {'; '.join(complexity.criteria_matched)}\n"
        f"Special Flags: {complexity.special_flags}"
    )

    # Keep the graph JSON compact but complete — truncate only if truly massive
    graph_json = json.dumps(graph, indent=2)
    if len(graph_json) > 80_000:
        graph_json = graph_json[:80_000] + "\n... [truncated for length]"

    session_context_block = _build_session_context_block(session_parse_report)
    session_section = f"\n{session_context_block}\n" if session_context_block else ""

    num_trans = _count_transformations(graph)
    log.info(
        "documentation_agent: starting two-pass doc generation — "
        "tier=%s transformations=%d",
        complexity.tier.value, num_trans,
    )

    # ── Pass 1: Overview + Transformations + Parameters ───────────────────────
    pass1_prompt = PASS_1_PROMPT.format(
        parse_summary=parse_summary,
        complexity_summary=complexity_summary,
        graph_json=graph_json,
    )

    pass1_doc, pass1_truncated = await _claude_call(client, pass1_prompt, "Pass 1 (transformations)")
    log.info("documentation_agent: Pass 1 complete — %d chars", len(pass1_doc))

    if pass1_truncated:
        # Pass 1 itself truncated — stamp sentinel and return early, no point
        # running Pass 2 without complete transformation context.
        log.error("documentation_agent: Pass 1 truncated — skipping Pass 2")
        return pass1_doc + DOC_TRUNCATION_SENTINEL

    # ── Pass 2: Lineage + Session Context + Ambiguities ───────────────────────
    pass2_prompt = PASS_2_PROMPT.format(
        pass1_doc=pass1_doc,
        graph_json=graph_json,
        session_context_block=session_section,
    )

    pass2_doc, pass2_truncated = await _claude_call(client, pass2_prompt, "Pass 2 (lineage)")
    log.info("documentation_agent: Pass 2 complete — %d chars", len(pass2_doc))

    # Combine — Pass 1 ends at Parameters, Pass 2 starts at Field-Level Lineage
    combined = pass1_doc.rstrip() + "\n\n" + pass2_doc.lstrip()

    if pass2_truncated:
        combined += DOC_TRUNCATION_SENTINEL
        log.warning(
            "documentation_agent: two-pass complete but TRUNCATED — total %d chars",
            len(combined),
        )
    else:
        combined += DOC_COMPLETE_SENTINEL
        log.info(
            "documentation_agent: two-pass complete — total %d chars",
            len(combined),
        )

    return combined
