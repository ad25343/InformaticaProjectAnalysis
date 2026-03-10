"""
STEP 9 — Test Generation + Coverage Check Agent
Fully deterministic — no Claude needed.

Two responsibilities:
  1. COVERAGE CHECK  — verify every S2T target field and every source filter
     appears in the generated code. Reports covered / missing.

  2. TEST GENERATION — produce stack-appropriate test files derived from the
     ACTUAL generated code (file names, model names, column names, target tables)
     rather than from hardcoded assumptions.

Design principle: everything must be driven by `conversion_output.files`.
We parse the real code to discover models, tables, columns, and stack patterns —
then generate tests that reference those real artifacts.
"""
from __future__ import annotations
import re
from datetime import datetime, timezone
from typing import Optional

from ..models.schemas import (
    ConversionOutput, TestReport, FieldCoverageCheck,
    FilterCoverageCheck, TargetStack,
)


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def generate_tests(
    conversion_output: ConversionOutput,
    s2t: dict,
    verification: dict,
    graph: dict,
) -> TestReport:
    """
    Build the TestReport for Step 9.

    Parameters
    ----------
    conversion_output : ConversionOutput
        The code files produced by Step 7.
    s2t : dict
        The s2t_state dict stored in job state (records, unmapped_sources, etc.).
    verification : dict
        The VerificationReport dict (for extracting filter flags).
    graph : dict
        The parsed Informatica graph (for source filter attributes).
    """
    stack = conversion_output.target_stack
    files = conversion_output.files  # {filename: content}

    records = s2t.get("records", [])
    mapped  = [r for r in records if r.get("status") not in
               ("Unmapped Target", "Unmapped Source")]

    # ── Discover the real structure of what was generated ─────────────────────
    discovered = _discover_generated_artifacts(files, stack)

    # ── 1. Field coverage ─────────────────────────────────────────────────────
    field_checks = _check_field_coverage(mapped, files)

    fields_covered = sum(1 for c in field_checks if c.covered)
    fields_missing = len(field_checks) - fields_covered
    missing_fields = [c.target_field for c in field_checks if not c.covered]
    coverage_pct   = (round(100 * fields_covered / len(field_checks), 1)
                      if field_checks else 100.0)

    # ── 2. Filter coverage ────────────────────────────────────────────────────
    source_filters = _extract_source_filters(graph)
    filter_checks  = _check_filter_coverage(source_filters, files)

    filters_covered = sum(1 for c in filter_checks if c.covered)
    filters_missing = len(filter_checks) - filters_covered

    # ── 3. Test file generation ───────────────────────────────────────────────
    test_files: dict[str, str] = {}
    notes: list[str] = []

    test_files, notes = _generate_tests_from_artifacts(
        discovered=discovered,
        files=files,
        mapped=mapped,
        source_filters=source_filters,
        field_checks=field_checks,
        stack=stack,
    )

    if missing_fields:
        notes.append(
            f"⚠️ {fields_missing} target field(s) not found in generated code: "
            f"{', '.join(missing_fields)}. Review Step 7 output carefully."
        )
    if filters_missing > 0:
        notes.append(
            f"⚠️ {filters_missing} filter condition(s) may not be reflected in code: "
            + "; ".join(c.filter_description for c in filter_checks if not c.covered)
        )

    return TestReport(
        mapping_name=conversion_output.mapping_name or "",
        target_stack=stack.value,
        test_files=test_files,
        field_coverage=field_checks,
        filter_coverage=filter_checks,
        fields_covered=fields_covered,
        fields_missing=fields_missing,
        coverage_pct=coverage_pct,
        missing_fields=missing_fields,
        filters_covered=filters_covered,
        filters_missing=filters_missing,
        notes=notes,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Artifact discovery — read the REAL generated code
# ─────────────────────────────────────────────────────────────────────────────

def _discover_generated_artifacts(
    files: dict[str, str],
    stack: TargetStack,
) -> dict:
    """
    Inspect the actual generated files and return a structured description:

    {
      "stack_hint":   "dbt" | "pyspark" | "python" | "sql" | "unknown",
      "sql_models":   [(filename, model_name, target_table, [columns]), ...],
      "yaml_schemas": [(filename, model_name_list), ...],
      "python_files": [(filename, function_names, dataframe_vars), ...],
      "final_model":  model_name or None,
      "final_table":  target_table or None,
      "all_columns":  sorted deduplicated list of column names found in code,
    }
    """
    result: dict = {
        "stack_hint": _infer_stack_hint(files, stack),
        "sql_models": [],
        "yaml_schemas": [],
        "python_files": [],
        "final_model": None,
        "final_table": None,
        "all_columns": [],
    }

    columns_seen: set[str] = set()

    for fname, content in files.items():
        ext = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""

        if ext == "sql":
            model_name, target_table, cols = _parse_sql_file(fname, content)
            result["sql_models"].append((fname, model_name, target_table, cols))
            columns_seen.update(cols)

        elif ext in ("yml", "yaml"):
            model_names = _parse_yaml_schema(content)
            result["yaml_schemas"].append((fname, model_names))

        elif ext == "py":
            funcs, df_vars, cols = _parse_python_file(content)
            result["python_files"].append((fname, funcs, df_vars))
            columns_seen.update(cols)

        elif ext in ("scala", "java"):
            # Best-effort: extract column-like strings
            cols = _extract_column_names_generic(content)
            columns_seen.update(cols)

    # Identify the "final" model: prefer fact/mart/output names, else last SQL
    final = None
    for fname, model_name, target_table, _ in result["sql_models"]:
        if any(k in (model_name or "").lower() for k in
               ("fact", "mart", "dim", "final", "output", "target")):
            final = (model_name, target_table)
            break
    if not final and result["sql_models"]:
        _, model_name, target_table, _ = result["sql_models"][-1]
        final = (model_name, target_table)

    if final:
        result["final_model"] = final[0]
        result["final_table"] = final[1] or final[0]

    result["all_columns"] = sorted(columns_seen)
    return result


def _infer_stack_hint(files: dict[str, str], stack: TargetStack) -> str:
    """Determine stack from file content rather than just the enum."""
    all_content = "\n".join(files.values())
    fnames = list(files.keys())

    if any("{{" in c and "ref(" in c for c in files.values()):
        return "dbt"
    if "from pyspark" in all_content.lower() or "sparksession" in all_content.lower():
        return "pyspark"
    if any(f.endswith(".py") for f in fnames):
        return "python"
    if any(f.endswith(".sql") for f in fnames):
        return "sql"
    # Fall back to schema enum
    return stack.value.lower()


def _parse_sql_file(fname: str, content: str) -> tuple[str, Optional[str], list[str]]:
    """
    Extract from a SQL file:
      - model_name: stem of the filename (e.g. 'fact_orders')
      - target_table: name from INSERT INTO / CREATE TABLE if present, else None
      - columns: list of column aliases / named expressions found in SELECT
    """
    model_name = fname.split("/")[-1].rsplit(".", 1)[0]

    # Target table from DDL / DML
    target_table: Optional[str] = None
    m = re.search(r"(?:INSERT\s+(?:INTO|OVERWRITE)\s+(?:TABLE\s+)?)([A-Za-z_][A-Za-z0-9_.]*)",
                  content, re.IGNORECASE)
    if m:
        target_table = m.group(1).strip().split(".")[-1]
    else:
        m = re.search(r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TABLE|VIEW)\s+([A-Za-z_][A-Za-z0-9_.]*)",
                      content, re.IGNORECASE)
        if m:
            target_table = m.group(1).strip().split(".")[-1]

    # Column aliases from SELECT … AS alias  or  SELECT col  (final SELECT only)
    cols = _extract_select_columns(content)

    return model_name, target_table, cols


def _extract_select_columns(sql: str) -> list[str]:
    """
    Heuristically extract column names/aliases from the outermost SELECT block.
    Returns a list of lowercase identifiers.
    """
    cols: list[str] = []
    # Find AS aliases: anything matching "AS <identifier>"
    for m in re.finditer(r"\bAS\s+([A-Za-z_][A-Za-z0-9_]*)", sql, re.IGNORECASE):
        cols.append(m.group(1).lower())
    # Also pick up bare column names between SELECT and FROM (no alias)
    select_block = re.search(r"\bSELECT\b(.*?)\bFROM\b", sql,
                             re.DOTALL | re.IGNORECASE)
    if select_block:
        block = select_block.group(1)
        # Remove sub-selects / function calls to reduce noise
        block = re.sub(r"\([^)]*\)", "", block)
        for token in re.split(r"[,\n]", block):
            token = token.strip()
            # Last identifier in a token is typically the column name
            id_match = re.findall(r"[A-Za-z_][A-Za-z0-9_]*", token)
            if id_match and id_match[-1].lower() not in ("as", "from", "select"):
                cols.append(id_match[-1].lower())
    return list(dict.fromkeys(cols))  # deduplicate, preserve order


def _parse_yaml_schema(content: str) -> list[str]:
    """Extract model names referenced in a dbt schema YAML."""
    names: list[str] = []
    for m in re.finditer(r"^\s*-\s*name:\s*([A-Za-z_][A-Za-z0-9_]*)", content, re.MULTILINE):
        names.append(m.group(1))
    return names


def _parse_python_file(content: str) -> tuple[list[str], list[str], list[str]]:
    """
    Extract:
      - function names defined in the file
      - DataFrame variable names (df, spark_df, result, etc.)
      - Column strings referenced in withColumn / selectExpr / .alias()
    """
    funcs  = re.findall(r"def\s+([A-Za-z_][A-Za-z0-9_]*)\s*\(", content)
    df_vars = re.findall(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(?:spark\.|df\.|pd\.)", content)
    # Strings passed to withColumn / selectExpr / alias / rename
    col_strings: list[str] = []
    for m in re.finditer(r"""(?:withColumn|selectExpr|alias|rename)\s*\(\s*["']([^"']+)["']""",
                         content):
        col_strings.append(m.group(1).lower())
    return funcs, df_vars, col_strings


def _extract_column_names_generic(content: str) -> list[str]:
    """Generic fallback: extract quoted strings that look like column names."""
    cols: list[str] = []
    for m in re.finditer(r"""["']([A-Za-z_][A-Za-z0-9_]*)["']""", content):
        name = m.group(1)
        if len(name) >= 2 and name.upper() not in ("AS", "BY", "OR", "AND", "NOT",
                                                     "NULL", "TRUE", "FALSE"):
            cols.append(name.lower())
    return cols


# ─────────────────────────────────────────────────────────────────────────────
# Coverage checks
# ─────────────────────────────────────────────────────────────────────────────

def _check_field_coverage(
    mapped: list[dict],
    files: dict[str, str],
) -> list[FieldCoverageCheck]:
    """
    For each mapped target field, search all generated files.
    Tries: exact case, lowercase, snake_case, and partial token match.
    """
    checks: list[FieldCoverageCheck] = []
    for rec in mapped:
        tgt_field = rec.get("target_field", "")
        tgt_table = rec.get("target_table", "")
        if not tgt_field:
            continue

        found_in: list[str] = []
        match_note = ""

        # Build search variants
        exact   = tgt_field          # e.g. ORDER_YEAR_MONTH or OrderYearMonth
        lower   = tgt_field.lower()  # e.g. order_year_month
        # CamelCase → snake_case only (skip if already ALL_CAPS/all_lower — lower handles those)
        snake   = re.sub(r"([a-z])([A-Z])", r"\1_\2", tgt_field).lower()

        for fname, content in files.items():
            c_lower = content.lower()
            if (exact in content or lower in c_lower or
                    (snake != lower and snake in c_lower)):
                found_in.append(fname)

        covered = bool(found_in)
        if covered:
            # Record which variant matched
            for fname, content in files.items():
                c_lower = content.lower()
                if exact in content:
                    match_note = f"Matched as '{exact}'"
                    break
                if lower in c_lower:
                    match_note = f"Matched as lowercase '{lower}'"
                    break
                if snake != lower and snake in c_lower:
                    match_note = f"Matched as snake_case '{snake}'"
                    break
        else:
            match_note = (
                f"Not found in any generated file — tried: "
                f"'{exact}', '{lower}', '{snake}'"
            )

        checks.append(FieldCoverageCheck(
            target_field=tgt_field,
            target_table=tgt_table,
            covered=covered,
            found_in_files=list(dict.fromkeys(found_in)),  # dedup
            note=match_note,
        ))
    return checks


def _check_filter_coverage(
    source_filters: list[dict],
    files: dict[str, str],
) -> list[FilterCoverageCheck]:
    """
    For each source filter, search for its meaningful tokens in generated code.
    Ignores SQL keywords and short words to reduce noise.
    """
    checks: list[FilterCoverageCheck] = []
    _SQL_KEYWORDS = frozenset({
        "AND", "OR", "NOT", "NULL", "IS", "IN", "LIKE", "BETWEEN",
        "CASE", "WHEN", "THEN", "ELSE", "END", "SELECT", "FROM",
        "WHERE", "JOIN", "ON", "AS", "WITH",
    })

    for flt in source_filters:
        condition = flt["condition"]
        # Extract meaningful tokens: identifiers and quoted values
        tokens = re.findall(r"[A-Za-z_][A-Za-z0-9_]*|'[^']*'", condition)
        meaningful = [
            t.strip("'") for t in tokens
            if len(t.strip("'")) > 2 and t.upper().strip("'") not in _SQL_KEYWORDS
        ]

        found_files: list[str] = []
        if meaningful:
            for fname, content in files.items():
                c_lower = content.lower()
                # Require at least half the tokens to match (more robust than requiring all)
                matched = sum(1 for t in meaningful if t.lower() in c_lower)
                if matched >= max(1, len(meaningful) // 2):
                    found_files.append(fname)

        covered = bool(found_files)
        checks.append(FilterCoverageCheck(
            filter_description=condition,
            source=flt["source"],
            covered=covered,
            found_in_files=found_files,
            note="" if covered else
                  f"Tokens {meaningful} not found — filter may be missing from generated code",
        ))
    return checks


# ─────────────────────────────────────────────────────────────────────────────
# Source filter extraction
# ─────────────────────────────────────────────────────────────────────────────

def _extract_source_filters(graph: dict) -> list[dict]:
    """Pull filter conditions from Source Qualifier and Filter transformation attributes."""
    filters: list[dict] = []
    for m in graph.get("mappings", []):
        for t in m.get("transformations", []):
            ttype = t.get("type", "")
            attrs = t.get("attributes", {})
            name  = t.get("name", "")

            if ttype == "Source Qualifier":
                sq_filter = attrs.get("Source Filter", "").strip()
                if sq_filter:
                    filters.append({
                        "condition": sq_filter,
                        "source": f"{name} (Source Qualifier filter)",
                    })
            elif ttype == "Filter":
                fil_cond = attrs.get("Filter Condition", "").strip()
                if fil_cond:
                    filters.append({
                        "condition": fil_cond,
                        "source": f"{name} (Filter transformation)",
                    })
    return filters


# ─────────────────────────────────────────────────────────────────────────────
# Test file generation — driven by discovered artifacts
# ─────────────────────────────────────────────────────────────────────────────

def _generate_tests_from_artifacts(
    discovered: dict,
    files: dict[str, str],
    mapped: list[dict],
    source_filters: list[dict],
    field_checks: list[FieldCoverageCheck],
    stack: TargetStack,
) -> tuple[dict[str, str], list[str]]:
    """
    Route to the correct generator based on what was actually detected in the code,
    not just the declared stack enum.
    """
    hint = discovered["stack_hint"]

    if hint == "dbt":
        return _generate_dbt_tests(discovered, files, mapped, source_filters, field_checks)
    elif hint in ("pyspark",):
        return _generate_pytest_tests(discovered, files, mapped, source_filters,
                                      field_checks, stack, is_spark=True)
    elif hint in ("python",):
        return _generate_pytest_tests(discovered, files, mapped, source_filters,
                                      field_checks, stack, is_spark=False)
    elif hint == "sql":
        return _generate_sql_tests(discovered, files, mapped, source_filters, field_checks)
    else:
        notes = [f"Test generation not yet implemented for detected stack: {hint}"]
        # Still produce coverage report
        tf, _ = _write_coverage_report(files, discovered, field_checks, source_filters)
        notes.append("Coverage report generated.")
        return tf, notes


def _generate_dbt_tests(
    discovered: dict,
    files: dict[str, str],
    mapped: list[dict],
    source_filters: list[dict],
    field_checks: list[FieldCoverageCheck],
) -> tuple[dict[str, str], list[str]]:
    """
    Generate dbt singular SQL tests and a schema test supplement,
    using the actual model names discovered from the generated code.
    """
    test_files: dict[str, str] = {}
    notes: list[str] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Use discovered model names (from actual SQL files), not assumed names
    sql_models = discovered["sql_models"]  # [(fname, model_name, target_table, cols)]
    final_model = discovered["final_model"]

    if not sql_models:
        notes.append("No SQL model files found — skipping dbt singular test generation.")
    else:
        for fname, model_name, target_table, model_cols in sql_models:
            if not model_name:
                continue

            # Singular test: row count > 0
            not_empty = f"""-- Auto-generated by Informatica Conversion Tool — Step 9
-- Generated: {ts}
-- Source model file: {fname}
-- Test: assert model '{model_name}' is not empty after conversion
SELECT COUNT(*) AS row_count
FROM {{{{ ref('{model_name}') }}}}
HAVING COUNT(*) = 0
"""
            test_files[f"tests/assert_{model_name}_not_empty.sql"] = not_empty

            # PK not-null tests — use ACTUAL columns found in this model
            # Prefer columns that end in _key, _id, or _sk (surrogate key)
            pk_candidates = [
                c for c in model_cols
                if re.search(r"(_key|_id|_sk)$", c, re.IGNORECASE)
            ]
            # Fall back to S2T records if no PK found in parsed columns
            if not pk_candidates:
                pk_candidates = [
                    r["target_field"].lower() for r in mapped
                    if re.search(r"(_key|_id|_sk)$",
                                 r.get("target_field", ""), re.IGNORECASE)
                ]
            for pk in pk_candidates[:3]:
                null_test = f"""-- Auto-generated by Informatica Conversion Tool — Step 9
-- Generated: {ts}
-- Source model file: {fname}
-- Test: assert no NULLs on surrogate/primary key field '{pk}'
SELECT COUNT(*) AS null_count
FROM {{{{ ref('{model_name}') }}}}
WHERE {pk} IS NULL
HAVING COUNT(*) > 0
"""
                test_files[f"tests/assert_{model_name}_{pk}_not_null.sql"] = null_test

            # Schema test additions — write a supplementary YAML only for this model
            # using the columns we actually discovered in the SQL
            if model_cols:
                col_tests_yaml = "\n".join(
                    f"          - not_null\n          - dbt_expectations.expect_column_to_exist"
                    if re.search(r"(_key|_id|_sk)$", c, re.IGNORECASE)
                    else "          - not_null"
                    for c in pk_candidates[:3]
                )
                schema_supplement = f"""# Auto-generated schema test supplement — Step 9
# Generated: {ts}
# Append these column tests to your existing schema.yml for model '{model_name}'
# Detected columns: {', '.join(model_cols)}

models:
  - name: {model_name}
    description: "Auto-generated coverage tests — review and merge into main schema.yml"
    columns:
{chr(10).join(f'      - name: {c}' for c in model_cols[:20])}
"""
                test_files[f"tests/{model_name}_schema_supplement.yml"] = schema_supplement

    # Coverage report (always generated)
    coverage_files, _ = _write_coverage_report(files, discovered, field_checks, source_filters)
    test_files.update(coverage_files)

    notes.append(
        f"Generated {len(test_files)} test file(s) — "
        f"{len([k for k in test_files if k.endswith('.sql')])} SQL tests, "
        f"{len([k for k in test_files if k.endswith('.yml')])} YAML supplements, "
        f"1 coverage report"
    )
    return test_files, notes


def _generate_sql_tests(
    discovered: dict,
    files: dict[str, str],
    mapped: list[dict],
    source_filters: list[dict],
    field_checks: list[FieldCoverageCheck],
) -> tuple[dict[str, str], list[str]]:
    """
    Generate plain SQL validation scripts for raw SQL / non-dbt output.
    Uses actual target table names found in the generated code.
    """
    test_files: dict[str, str] = {}
    notes: list[str] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    sql_models = discovered["sql_models"]
    final_table = discovered["final_table"] or "TARGET_TABLE"

    pk_fields = [
        r.get("target_field", "").lower() for r in mapped
        if re.search(r"(_key|_id|_sk)$", r.get("target_field", ""), re.IGNORECASE)
    ]

    all_target_fields = [r.get("target_field", "").lower()
                         for r in mapped if r.get("target_field")]

    null_assertions = "\n".join(
        f"-- PK field: {f}\nSELECT COUNT(*) AS null_{f} FROM {final_table} WHERE {f} IS NULL;"
        for f in pk_fields[:5]
    )
    field_comments = "\n".join(
        f"  -- {'✅' if c.covered else '❌'} {c.target_field}"
        for c in field_checks
    )

    row_count_script = f"""-- Auto-generated validation script — Informatica Conversion Tool Step 9
-- Generated: {ts}
-- Target table: {final_table}
-- Run these queries against your data warehouse after loading

-- ── 1. Row count ──────────────────────────────────────────────────────────────
SELECT COUNT(*) AS total_rows FROM {final_table};
-- Expected: > 0

-- ── 2. PK / Key field NULL checks ────────────────────────────────────────────
{null_assertions or "-- No primary key fields detected from S2T mapping"}

-- ── 3. Column presence (manual verification guide) ───────────────────────────
-- Verify the following columns exist in {final_table}:
{field_comments}

-- ── 4. Filter verification ────────────────────────────────────────────────────
{chr(10).join(f"-- Filter applied: {f['condition']} ({f['source']})" for f in source_filters)
  or "-- No source filters detected"}
"""
    test_files[f"tests/validate_{final_table.lower()}.sql"] = row_count_script

    # Coverage report
    coverage_files, _ = _write_coverage_report(files, discovered, field_checks, source_filters)
    test_files.update(coverage_files)

    notes.append(
        f"Generated SQL validation script for target table '{final_table}'. "
        "Run against your warehouse after loading to verify conversion correctness."
    )
    return test_files, notes


def _generate_pytest_tests(
    discovered: dict,
    files: dict[str, str],
    mapped: list[dict],
    source_filters: list[dict],
    field_checks: list[FieldCoverageCheck],
    stack: TargetStack,
    is_spark: bool,
) -> tuple[dict[str, str], list[str]]:
    """
    Generate a pytest suite for Python / PySpark output.
    Uses actual function names and DataFrame variables found in the generated code.
    """
    notes: list[str] = []
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    # Discover actual function names and file structure
    py_files   = discovered["python_files"]  # [(fname, funcs, df_vars)]
    entry_file = None
    entry_func = None

    for fname, funcs, _ in py_files:
        for fn in funcs:
            if any(k in fn.lower() for k in
                   ("main", "run", "execute", "transform", "load", "convert")):
                entry_file = fname
                entry_func = fn
                break
        if entry_func:
            break

    if not entry_file and py_files:
        entry_file, funcs, _ = py_files[-1]
        entry_func = funcs[0] if funcs else "transform"

    # Use columns found across all Python files, fall back to S2T fields
    all_code_cols: list[str] = []
    for _, _, df_vars in py_files:
        pass  # df_vars already extracted
    for _, _, _, cols in discovered["sql_models"]:
        all_code_cols.extend(cols)

    # For field tests: use actual columns found in code where possible,
    # then supplement with S2T target fields
    s2t_fields = [r.get("target_field", "").lower()
                  for r in mapped if r.get("target_field")]
    # Merge: code-discovered columns first, then S2T (dedup)
    field_list = list(dict.fromkeys(all_code_cols + s2t_fields))

    if is_spark:
        imports = (
            "from pyspark.sql import SparkSession\n"
            "import pytest\n\n\n"
            "@pytest.fixture(scope='session')\n"
            "def spark():\n"
            "    return SparkSession.builder.master('local').appName('test').getOrCreate()\n"
        )
        fixture_hint = (
            f"    # Run the converted pipeline and return the output DataFrame\n"
            f"    # Example:\n"
            f"    #   from {(entry_file or 'your_module').replace('/', '.').rstrip('.py')} import {entry_func or 'transform'}\n"
            f"    #   return {entry_func or 'transform'}(spark)\n"
            f"    raise NotImplementedError('Replace with actual pipeline call')"
        )
        null_check = (
            "    null_count = output_df.filter(output_df[col].isNull()).count()"
        )
        len_check = "    count = output_df.count()"
    else:
        imports = "import pytest\nimport pandas as pd\n"
        fixture_hint = (
            f"    # Run the converted pipeline and return the output DataFrame\n"
            f"    # Example:\n"
            f"    #   from {(entry_file or 'your_module').replace('/', '.').rstrip('.py')} import {entry_func or 'transform'}\n"
            f"    #   return {entry_func or 'transform'}()\n"
            f"    raise NotImplementedError('Replace with actual pipeline call')"
        )
        null_check = "    null_count = output_df[col].isna().sum()"
        len_check  = "    count = len(output_df)"

    field_tests = ""
    for fld in field_list[:30]:  # cap to avoid huge files
        safe = re.sub(r"[^A-Za-z0-9_]", "_", fld)
        is_key = bool(re.search(r"(_key|_id|_sk)$", fld, re.IGNORECASE))
        hard_fail = "  # PK: hard failure" if is_key else "  # informational"
        pk_assert = (f"assert null_count == 0, "
                     f"f\"PK field '{fld}' has {{null_count}} NULLs — should be 0\""
                     if is_key
                     else "assert null_count >= 0  # informational — tighten for PK fields")
        field_tests += f"""

def test_{safe}_present_in_output(output_df):
    \"\"\"Target field '{fld}' must exist in the output dataset.\"\"\"
    cols = [c.lower() for c in output_df.columns]
    assert '{fld}' in cols, f"Missing column '{fld}' — got: {{cols}}"


def test_{safe}_nulls(output_df):{hard_fail}
    \"\"\"Check NULL count for field '{fld}'.\"\"\"
    col = '{fld}'
    if col not in [c.lower() for c in output_df.columns]:
        pytest.skip(f"Column '{{col}}' not present — skipping null check")
{null_check}
    {pk_assert}
"""

    filter_tests = ""
    for i, flt in enumerate(source_filters):
        cond = flt["condition"]
        safe_i = i + 1
        filter_tests += f"""

def test_filter_{safe_i}_applied(output_df):
    \"\"\"Filter from {flt['source']} should be reflected in output.

    Original filter: {cond}
    TODO: implement the actual assertion for this condition.
    \"\"\"
    # Verify the filter reduced rows (compare against unfiltered count if available)
    {"count = output_df.count()" if is_spark else "count = len(output_df)"}
    assert count >= 0  # placeholder — implement real filter assertion
"""

    coverage_comment = "\n".join(
        f"#   {'✅' if c.covered else '❌'}  {c.target_field}"
        f"{' ← NOT FOUND IN CODE' if not c.covered else ''}"
        for c in field_checks
    )

    content = f'''"""
Auto-generated test suite — Informatica Conversion Tool Step 9
Generated:  {ts}
Stack:      {stack.value}
Entry file: {entry_file or "unknown"}
Entry func: {entry_func or "unknown"}

Field coverage summary ({sum(1 for c in field_checks if c.covered)}/{len(field_checks)} covered):
{coverage_comment}
"""
{imports}

# ─── Fixture: load the converted pipeline output ───────────────────────────────
@pytest.fixture
def output_df():
    """
    Load the output of the converted pipeline.
    Replace this with actual code that runs your converted script/function.
    """
{fixture_hint}


# ─── Field presence & null checks (auto-generated from S2T + discovered columns) ──
{field_tests}

# ─── Filter condition checks (from Informatica source filters) ─────────────────
{filter_tests}

# ─── Row count sanity check ─────────────────────────────────────────────────────
def test_output_not_empty(output_df):
    """Output dataset must contain at least one row after conversion."""
{len_check}
    assert count > 0, "Output dataset is empty — conversion may have produced no rows"
'''

    test_files: dict[str, str] = {"tests/test_conversion.py": content}

    # Coverage report
    coverage_files, _ = _write_coverage_report(files, discovered, field_checks, source_filters)
    test_files.update(coverage_files)

    notes.append(
        f"Generated tests/test_conversion.py with {len(field_list[:30])} field tests "
        f"using {'actual column names discovered in code' if all_code_cols else 'S2T target fields'}. "
        "Fill in the output_df fixture to run."
    )
    return test_files, notes


# ─────────────────────────────────────────────────────────────────────────────
# Coverage report (always produced, stack-agnostic)
# ─────────────────────────────────────────────────────────────────────────────

def _write_coverage_report(
    files: dict[str, str],
    discovered: dict,
    field_checks: list[FieldCoverageCheck],
    source_filters: list[dict],
) -> tuple[dict[str, str], list[str]]:
    """
    Produce a markdown coverage report that summarises what was found
    in the *actual* generated files.
    """
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    covered   = sum(1 for c in field_checks if c.covered)
    total     = len(field_checks)
    pct       = round(100 * covered / total, 1) if total else 100.0

    stack_hint  = discovered["stack_hint"]
    sql_models  = discovered["sql_models"]
    final_model = discovered["final_model"] or "N/A"
    all_cols    = discovered["all_columns"]

    lines = [
        "# Conversion Coverage Report",
        f"Generated: {ts}",
        f"Detected stack: **{stack_hint}**",
        f"Final model/table: **{final_model}**",
        "",
        f"## Field Coverage — {pct}% ({covered}/{total} fields)",
        "| Status | Target Field | Target Table | Found In |",
        "|--------|-------------|-------------|----------|",
    ]
    for c in field_checks:
        status = "✅" if c.covered else "❌"
        found  = ", ".join(c.found_in_files) if c.found_in_files else "—"
        note   = f" _{c.note}_" if c.note and not c.covered else ""
        lines.append(f"| {status} | `{c.target_field}` | {c.target_table or '—'} | {found}{note} |")

    lines += [
        "",
        "## Columns Discovered in Generated Code",
        f"The following {len(all_cols)} column name(s) were parsed from the generated files:",
        "",
        "```",
        ", ".join(all_cols) if all_cols else "(none detected)",
        "```",
        "",
        "## Generated Files",
        "| File | Type | Model/Table |",
        "|------|------|------------|",
    ]
    for fname, model_name, target_table, cols in sql_models:
        lines.append(f"| `{fname}` | SQL model | {model_name} → {target_table or '?'} |")
    for fname, model_names in discovered["yaml_schemas"]:
        lines.append(f"| `{fname}` | YAML schema | {', '.join(model_names) or '?'} |")
    for fname, funcs, _ in discovered["python_files"]:
        lines.append(f"| `{fname}` | Python | funcs: {', '.join(funcs[:5]) or '?'} |")

    lines += [
        "",
        "## Source Filter Coverage",
        "| Status | Filter Condition | Source |",
        "|--------|-----------------|--------|",
    ]
    if source_filters:
        for flt in source_filters:
            lines.append(f"| ⬜ | `{flt['condition']}` | {flt['source']} |")
    else:
        lines.append("| — | No source filters detected | — |")

    return {"tests/COVERAGE_REPORT.md": "\n".join(lines)}, []
