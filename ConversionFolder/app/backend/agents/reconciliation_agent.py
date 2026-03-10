"""
STEP 8b — Reconciliation Report (static / structural analysis)

Since the tool generates code rather than executing it, a runtime row-count
reconciliation is only possible after the generated code has been run against
real data.  This agent performs the next-best alternative: a STRUCTURAL
reconciliation that verifies the generated code correctly covers every target
field and every source reference declared in the mapping documentation.

What it checks
--------------
1. Field coverage — every target field from the S2T mapping appears somewhere
   in the generated code (by name).
2. Source table coverage — every source table/qualifier referenced in the
   parse report appears in the generated code.
3. Expression coverage — business-rule expressions documented in the mapping
   (e.g. ORDER_AMOUNT * 0.085) are present in at least one generated file.
4. Transformation completeness — no file is pure TODO stubs.

Output
------
ReconciliationReport with:
  - match_rate: float — % of target fields found in generated code
  - mismatched_fields: list of fields not found
  - final_status: RECONCILED | PARTIAL | PENDING_EXECUTION
  - informatica_rows / converted_rows: None (not executable without a database)
"""
from __future__ import annotations
import re
from typing import Optional

from ..models.schemas import (
    ReconciliationReport, ParseReport, ConversionOutput
)


def generate_reconciliation_report(
    parse_report: ParseReport,
    conversion_output: ConversionOutput,
    s2t_field_list: Optional[list[str]] = None,
    source_tables: Optional[list[str]] = None,
    documented_expressions: Optional[list[str]] = None,
) -> ReconciliationReport:
    """
    Perform a structural reconciliation between the Informatica mapping
    specification and the generated code.

    Parameters
    ----------
    parse_report            Step 1 output — source of mapping/object names
    conversion_output       Step 6 output — the generated code files
    s2t_field_list          Optional list of target field names from the S2T
                            agent (Step 2).  If omitted, only source-table
                            coverage is checked.
    source_tables           Optional list of source table/qualifier names.
                            Defaults to objects_found from parse_report.
    documented_expressions  Optional list of expression fragments (substrings)
                            that should appear verbatim in the generated code
                            (e.g. ["ORDER_AMOUNT * 0.085", "0.15"]).

    Returns
    -------
    ReconciliationReport
    """
    mapping_name = conversion_output.mapping_name
    all_code     = _combined_code(conversion_output.files)
    all_code_low = all_code.lower()

    mismatched:   list[dict] = []
    verified:     int        = 0
    total_checks: int        = 0

    # ── 1. Target field coverage ──────────────────────────────────────────
    field_issues: list[str] = []

    if s2t_field_list:
        for field in s2t_field_list:
            total_checks += 1
            # Case-insensitive search — field names may appear as col("FIELD"),
            # df["FIELD"], or just FIELD in SQL SELECT
            if field.lower() in all_code_low:
                verified += 1
            else:
                field_issues.append(field)
                mismatched.append({
                    "type":    "TARGET_FIELD",
                    "field":   field,
                    "detail":  f"Target field '{field}' not found in any generated file.",
                })

    # ── 2. Source table / qualifier coverage ──────────────────────────────
    if source_tables is None:
        # Extract from objects_found: keys that sound like source objects
        source_tables = [
            name for name in parse_report.mapping_names
        ] + _extract_source_qualifiers(parse_report)

    for table in source_tables:
        if not table:
            continue
        total_checks += 1
        if table.lower() in all_code_low:
            verified += 1
        else:
            mismatched.append({
                "type":   "SOURCE_TABLE",
                "field":  table,
                "detail": f"Source object '{table}' not referenced in any generated file.",
            })

    # ── 3. Expression / business rule coverage ────────────────────────────
    if documented_expressions:
        for expr in documented_expressions:
            if not expr.strip():
                continue
            total_checks += 1
            if expr.lower() in all_code_low:
                verified += 1
            else:
                mismatched.append({
                    "type":   "EXPRESSION",
                    "field":  expr,
                    "detail": f"Expression '{expr}' not found in generated code — "
                              "business rule may not be implemented.",
                })

    # ── 4. Stub completeness check ────────────────────────────────────────
    stub_files = _detect_stub_files(conversion_output.files)
    if stub_files:
        mismatched.append({
            "type":   "STUB_COMPLETENESS",
            "field":  ", ".join(stub_files),
            "detail": f"Files are predominantly TODO stubs: {stub_files}. "
                      "Manual completion required.",
        })

    # ── Compute match_rate ────────────────────────────────────────────────
    if total_checks > 0:
        match_rate = round((verified / total_checks) * 100, 1)
    else:
        match_rate = 100.0  # nothing to check → structurally clean

    # ── Determine final status ────────────────────────────────────────────
    if match_rate == 100.0 and not stub_files:
        final_status = "RECONCILED"
        root_cause   = None
        resolution   = None
    elif match_rate >= 80.0:
        final_status = "PARTIAL"
        root_cause   = _describe_root_cause(mismatched)
        resolution   = (
            "Review mismatched fields above. "
            "Some target fields or source references may require manual mapping or renaming."
        )
    else:
        final_status = "PENDING_EXECUTION"
        root_cause   = _describe_root_cause(mismatched)
        resolution   = (
            "Significant structural gaps detected. "
            "Re-review the conversion output against the original mapping documentation "
            "before executing the generated code."
        )

    return ReconciliationReport(
        mapping_name=mapping_name,
        input_description=(
            f"Structural reconciliation of {len(conversion_output.files)} generated file(s) "
            f"against mapping '{mapping_name}'"
        ),
        informatica_rows=None,    # requires live execution
        converted_rows=None,      # requires live execution
        match_rate=match_rate,
        mismatched_fields=mismatched,
        root_cause=root_cause,
        resolution=resolution,
        final_status=final_status,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _combined_code(files: dict[str, str]) -> str:
    """Concatenate all generated file contents for substring searching."""
    return "\n".join(files.values())


def _extract_source_qualifiers(parse_report: ParseReport) -> list[str]:
    """
    Pull likely source qualifier / table names from the parse report.

    parse_report.objects_found is a dict like {"Source Qualifier": 2, "Mapping": 1}.
    The actual transformation names come from mapping_names and reusable_components.
    We also scan for SQ_* or similar patterns commonly found in mapping names.
    """
    candidates: list[str] = []

    # Mapping names often embed source table hints (e.g. m_STG_ORDERS_to_FACT)
    for name in parse_report.mapping_names:
        # Split on "_to_" and take left side as a hint
        parts = re.split(r"_to_", name, flags=re.IGNORECASE)
        if parts:
            # Strip leading "m_" prefix
            raw = re.sub(r"^m_", "", parts[0], flags=re.IGNORECASE)
            if raw:
                candidates.append(raw)

    # Reusable components often include source object names
    candidates.extend(parse_report.reusable_components)

    return candidates


def _detect_stub_files(files: dict[str, str]) -> list[str]:
    """
    Return filenames where >60% of code lines are TODO/FIXME/STUB markers.
    Mirrors the logic in _validate_conversion_files for consistency.
    """
    stubs = []
    for fname, content in files.items():
        stripped = content.strip()
        if not stripped or len(stripped) > 150_000:
            continue
        lines = [l.strip() for l in stripped.splitlines() if l.strip()]
        code_lines = [
            l for l in lines
            if l and not l.startswith("#") and not l.startswith('"""') and not l.startswith("'''")
        ]
        todo_lines = [l for l in lines if "TODO" in l.upper() or "FIXME" in l.upper() or "STUB" in l.upper()]
        if code_lines and len(todo_lines) / max(len(code_lines), 1) > 0.6:
            stubs.append(fname)
    return stubs


def _describe_root_cause(mismatched: list[dict]) -> str:
    type_counts: dict[str, int] = {}
    for m in mismatched:
        t = m.get("type", "UNKNOWN")
        type_counts[t] = type_counts.get(t, 0) + 1

    parts = []
    if type_counts.get("TARGET_FIELD"):
        parts.append(f"{type_counts['TARGET_FIELD']} target field(s) not found in generated code")
    if type_counts.get("SOURCE_TABLE"):
        parts.append(f"{type_counts['SOURCE_TABLE']} source object(s) not referenced")
    if type_counts.get("EXPRESSION"):
        parts.append(f"{type_counts['EXPRESSION']} business expression(s) missing")
    if type_counts.get("STUB_COMPLETENESS"):
        parts.append("One or more files are predominantly TODO stubs")

    return "; ".join(parts) if parts else "Unknown structural mismatch"
