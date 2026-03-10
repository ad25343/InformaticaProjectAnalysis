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
from ._client import make_client
from ..security_knowledge import build_security_context_block

from ..config import settings as _cfg
MODEL = _cfg.claude_model


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
    client = make_client()
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
- Define schema explicitly using StructType / StructField — no inferred schemas
- Use native Spark functions (pyspark.sql.functions as F) — UDFs only as last resort; if used, document why
- Column references: always use F.col("name") — never string-only access in expressions
- Add structured logging (row counts) at each major step: logger.info("After <step>: %d rows", df.count())
- Externalize ALL hardcoded env-specific values to a config dict at the top of the file
- Add inline comments for every business rule

Informatica-to-PySpark pattern guide (apply where relevant):
- LOOKUP transformation → use broadcast join for small lookup tables (< 100 MB):
    df.join(F.broadcast(lookup_df), on=join_key, how="left")
- AGGREGATOR transformation → use groupBy().agg()
- SORTER transformation → use orderBy()
- ROUTER transformation → split into multiple filtered DataFrames with .filter()
- JOINER transformation → use .join() with appropriate join type
- SCD Type 2 → use Window functions:
    w = Window.partitionBy("natural_key").orderBy(F.desc("effective_date"))
    df.withColumn("rank", F.row_number().over(w)).filter(F.col("rank") == 1)
- UNION transformation → use unionByName(allowMissingColumns=True)
- Sequence generator → use F.monotonically_increasing_id() or row_number() over an ordered window

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

Informatica-to-dbt pattern guide (apply where relevant):
- Large target tables that load incrementally → use incremental materialisation:
    {{ config(materialized='incremental', unique_key='<pk_column>') }}
    {% if is_incremental() %} WHERE updated_at > (SELECT MAX(updated_at) FROM {{ this }}) {% endif %}
- Surrogate key generation (replaces Informatica sequence generators):
    {{ dbt_utils.generate_surrogate_key(['col1', 'col2']) }} AS surrogate_key
- SCD Type 2 → use dbt snapshots (dbt_project/snapshots/) with strategy: timestamp or check
- LOOKUP transformation → use a ref() join to the lookup model; never hardcode lookup values inline
- ROUTER transformation → use separate CTEs or models with explicit WHERE filters
- Reusable expression logic → extract to a dbt macro in macros/

- Output complete, runnable SQL model files

## Execution-Ready Requirements — REQUIRED for all dbt conversions

dbt only handles the transformation layer (T).  To make the output fully execution-ready
you MUST also generate the following two Python files:

### extract/extract_{mapping_name}.py  — Python EL (Extract-Load) script
A self-contained Python script that:
- Reads from every SOURCE connection documented in the mapping
    * Relational / JDBC sources → SQLAlchemy: pd.read_sql(query, engine, chunksize=50_000)
    * Flat-file sources → pd.read_csv(path, chunksize=50_000) with dtype=str fallback
- Applies any pre_session_sql documented in the session config BEFORE the main extract
  (execute via connection.execute(text(pre_sql)) before pd.read_sql)
- Writes extracted data into the warehouse STAGING schema as table stg_{source_table}
  (use pandas DataFrame.to_sql with if_exists="replace" and method="multi")
- Uses a CONFIG dict at the top for ALL connection strings, schema names, paths, passwords
  (read values from os.environ — never hardcode credentials)
- Structured JSON logging: extract start, row count after read, row count after load, duration
- Error handling: on failure log the error with traceback and call sys.exit(1)
- Uses context managers for all DB connections: with engine.connect() as conn:
- Replace {mapping_name} with the actual mapping name (lowercase, underscores)

### run_pipeline.py  — Orchestration wrapper
A Python script that sequences EL → dbt run → dbt test:
- Step 1 — EL: subprocess.run([sys.executable, "extract/extract_{mapping_name}.py"], check=False)
    On non-zero exit: log "EL step failed" and sys.exit(1) — do NOT continue to dbt
- Step 2 — dbt run: subprocess.run(["dbt", "run", "--select", "{dbt_model_name}", "--profiles-dir", "."], check=False)
    On non-zero exit: log "dbt run failed" and sys.exit(1)
- Step 3 — dbt test: subprocess.run(["dbt", "test", "--select", "{dbt_model_name}", "--profiles-dir", "."], check=False)
    On non-zero exit: log "dbt test failed" and sys.exit(1)
- Structured logging with timestamps at each step start and completion
- Single entry point: if __name__ == "__main__": run_pipeline()
- Replace {mapping_name} and {dbt_model_name} with the actual names

Both files use the same <<<BEGIN_FILE: ...>>> / <<<END_FILE>>> delimiter format as all other output files.
""" + _DW_AUDIT_RULES

PYTHON_SYSTEM = """You are a senior data engineer converting Informatica PowerCenter mappings to Python (Pandas).

Rules:
- Work ONLY from the documentation provided — never invent logic not documented there
- One function per logical transformation step; functions must be independently testable
- Add type hints to all functions and return values
- Structured JSON logging at each step with row counts
- Externalize ALL config — no hardcoded values; use a CONFIG dict or config file
- Use context managers for all DB/file connections (with statement — ensures cleanup)
- Use try/finally blocks around any resource acquisition not covered by context managers
- Use chunked reading for large files: pd.read_csv(..., chunksize=50_000)
- Use pyarrow-backed dtypes where available for memory efficiency (dtype_backend="pyarrow")

Informatica-to-Pandas pattern guide (apply where relevant):
- LOOKUP transformation → use pd.merge(..., how="left") on the key fields
- AGGREGATOR transformation → use groupby().agg()
- SORTER transformation → use sort_values()
- ROUTER transformation → split into filtered DataFrames with boolean masks
- JOINER transformation → use pd.merge() with appropriate how parameter
- SCD Type 2 → use sort + drop_duplicates with keep="first" after ordering by effective date
- UNION transformation → use pd.concat([df1, df2], ignore_index=True)
- Sequence generator → use df.reset_index(drop=True).index + start_value

- Output complete, runnable Python files
""" + _DW_AUDIT_RULES

CONVERSION_PROMPT = """Convert the Informatica mapping documented below to {stack}.

{security_context}
## Stack Assignment Rationale
{rationale}
{approved_fixes_section}{flag_handling_section}{manifest_override_section}
## Full Mapping Documentation (your source of truth)
{documentation_md}

## Conversion Requirements
- Follow the documented logic EXACTLY
- Every business rule from the docs → inline comment in the code
- All hardcoded env-specific values → config dict / config file
- Structured logging at: job start, after each major transformation, job end (with row counts)
- Reject/error handling as documented
- Where a Reviewer-Approved Fix is listed above, apply it precisely as described
- Where a Verification Flag Handling rule is listed above, apply it — do NOT skip the transformation

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


def _build_flag_handling_section(verification_flags: list[dict]) -> str:
    """
    Build a prompt section that instructs Claude to auto-handle each verification flag.
    The tool addresses as much as possible in code; the human reviewer handles what can't be.
    Returns an empty string if there are no actionable flags.
    """
    if not verification_flags:
        return ""

    # Per-flag-type handling rules mapped to concrete code instructions
    _HANDLING_RULES: dict[str, str] = {
        "INCOMPLETE_LOGIC": (
            "The transformation has missing or incomplete logic (e.g. a Filter with no condition, "
            "a Router with an undefined group, or a conditional with no ELSE branch). "
            "Generate the transformation as a PASS-THROUGH (all records proceed). "
            "Add a prominent comment: "
            "# TODO [AUTO-FLAG]: INCOMPLETE_LOGIC — {detail}. "
            "Confirm the intended rule with the mapping owner before promoting to production."
        ),
        "ENVIRONMENT_SPECIFIC_VALUE": (
            "A hardcoded environment-specific value was found (connection string, file path, "
            "schema name, server name, rate, threshold, etc.). "
            "Move it to the config dict / config file at the top of the generated code. "
            "Never embed it inline. Add a comment: "
            "# CONFIG: {detail}"
        ),
        "HIGH_RISK": (
            "A high-risk logic pattern was detected (complex conditional, multi-branch routing, "
            "hardcoded business constant, potential data loss path, etc.). "
            "Implement the logic as documented. "
            "Add an assertion or row-count check immediately after the transformation. "
            "Add a comment: # HIGH-RISK [AUTO-FLAG]: {detail} — validate output with UAT."
        ),
        "LINEAGE_GAP": (
            "A target field could not be traced to a source. "
            "Set the field to None / NULL with a comment: "
            "# TODO [AUTO-FLAG]: LINEAGE GAP — {detail}. Trace manually in the Informatica mapping."
        ),
        "DEAD_LOGIC": (
            "A transformation is isolated from the data flow (no inputs or outputs). "
            "Comment it out entirely with: "
            "# DEAD LOGIC [AUTO-FLAG]: {detail} — confirm with mapping owner whether to remove."
        ),
        "REVIEW_REQUIRED": (
            "Logic is ambiguous or unclear from the documentation. "
            "Implement a best-effort interpretation based on field names and context. "
            "Add a comment: # TODO [AUTO-FLAG]: REVIEW REQUIRED — {detail}. "
            "Confirm interpretation with the mapping owner."
        ),
        "ORPHANED_PORT": (
            "A port has no connections. Skip it in the converted code. "
            "Add a comment: # ORPHANED PORT [AUTO-FLAG]: {detail}"
        ),
        "UNRESOLVED_PARAMETER": (
            "A parameter has no resolved value. "
            "Add it to the config dict with a placeholder: PARAM_NAME = '<fill_in>' "
            "and reference it from there. Never hardcode the parameter inline. "
            "Add a comment: # UNRESOLVED PARAM [AUTO-FLAG]: {detail}"
        ),
        "UNRESOLVED_VARIABLE": (
            "A $$VARIABLE has no resolved value. "
            "Add it to the config dict with a placeholder and reference it. "
            "Add a comment: # UNRESOLVED VARIABLE [AUTO-FLAG]: {detail}"
        ),
        "UNSUPPORTED_TRANSFORMATION": (
            "This transformation type cannot be automatically converted. "
            "Generate a clearly-marked stub with: "
            "# TODO [MANUAL REQUIRED]: UNSUPPORTED TRANSFORMATION — {detail}. "
            "Leave the stub in place so the engineer knows exactly what to implement."
        ),
    }

    lines: list[str] = []
    seen: set[str] = set()

    for flag in verification_flags:
        flag_type = flag.get("flag_type", "")
        location  = flag.get("location", "")
        detail    = flag.get("description", flag.get("detail", ""))
        rule      = _HANDLING_RULES.get(flag_type)
        if not rule:
            continue  # INFO/DOCUMENTATION_TRUNCATED flags don't need code handling
        key = f"{flag_type}::{location}"
        if key in seen:
            continue
        seen.add(key)

        instruction = rule.replace("{detail}", detail or flag_type)
        lines.append(f"- [{flag_type}] at {location or 'mapping level'}:\n  {instruction}")

    if not lines:
        return ""

    return (
        "\n## ⚙️ Verification Flag Auto-Handling — Apply These Rules During Conversion\n"
        "The verification step found the following issues. The tool will handle each one "
        "in code rather than blocking the conversion. Apply every rule below exactly. "
        "Do NOT skip any flagged transformation — generate code (or a clearly-marked stub) "
        "for every item:\n\n"
        + "\n\n".join(lines)
        + "\n\n"
    )


def _build_manifest_override_section(overrides: list[dict]) -> str:
    """
    Build a prompt section listing reviewer-confirmed overrides from the manifest xlsx.
    These take precedence over anything the tool inferred from naming patterns.
    Returns empty string if no overrides were supplied.
    """
    if not overrides:
        return ""

    lines: list[str] = []
    for o in overrides:
        location  = o.get("location", "")
        itype     = o.get("item_type", "")
        override  = o.get("reviewer_override", "").strip()
        notes     = o.get("notes", "")
        if not override or override.upper() in ("", "N/A"):
            continue
        note_str = f" (Note: {notes})" if notes else ""
        lines.append(f"- [{itype}] {location} → Reviewer confirmed: {override}{note_str}")

    if not lines:
        return ""

    return (
        "\n## 📋 Reviewer-Confirmed Manifest Overrides — These Take Precedence\n"
        "A human reviewer examined the pre-conversion manifest and supplied the following "
        "corrections. Use these INSTEAD OF any tool-inferred connection or determination "
        "for the listed items. Do not second-guess these — they are authoritative:\n\n"
        + "\n".join(lines)
        + "\n\n"
    )


def _validate_conversion_files(
    files: dict[str, str],
    stack,
    _cache: dict | None = None,
) -> list[str]:
    """
    GAP #7 — Non-blocking content validation of Claude-generated files.
    Returns a list of warning strings (empty = all clean).

    v2.6.0 scale improvements:
    - Optional _cache dict keyed by hash(content) → issues list.  Re-used
      across remediation rounds so unchanged files are never re-validated.
    - TODO ratio check now skips files > 150 KB (pathological edge case).
    - dbt SELECT check uses content[:5000] instead of full content.lower().
    - SparkSession check already scoped to content[:2000] (unchanged).
    """
    import ast
    import hashlib

    if _cache is None:
        _cache = {}

    issues: list[str] = []

    for fname, content in files.items():
        # ── Cache check: skip re-validating content we've already seen ────
        _key = hashlib.md5(content.encode(), usedforsecurity=False).hexdigest()
        if _key in _cache:
            issues.extend(_cache[_key])
            continue

        file_issues: list[str] = []
        stripped = content.strip()

        # 1. Empty file
        if not stripped:
            file_issues.append(
                f"⚠️ VALIDATION: '{fname}' is empty — Claude may have failed to generate content."
            )
            _cache[_key] = file_issues
            issues.extend(file_issues)
            continue

        # 2. Placeholder-only file — skip for very large files (> 150 KB)
        if len(stripped) <= 150_000:
            lines = [l.strip() for l in stripped.splitlines() if l.strip()]
            code_lines = [
                l for l in lines
                if l and not l.startswith("#") and not l.startswith('"""') and not l.startswith("'''")
            ]
            todo_lines = [l for l in lines if "TODO" in l.upper() or "FIXME" in l.upper() or "STUB" in l.upper()]
            if code_lines and len(todo_lines) / max(len(code_lines), 1) > 0.6:
                file_issues.append(
                    f"⚠️ VALIDATION: '{fname}' is mostly TODO stubs ({len(todo_lines)} TODO lines vs "
                    f"{len(code_lines)} code lines) — Claude may not have had enough context to fully convert."
                )

        # 3. Python syntax check (size-guarded at 500 KB — unchanged)
        if fname.endswith((".py", ".pyx")) and len(stripped) < 500_000:
            try:
                ast.parse(stripped)
            except SyntaxError as e:
                file_issues.append(
                    f"⚠️ VALIDATION: '{fname}' has a Python syntax error at line {e.lineno}: {e.msg}. "
                    "The file was saved but will not run without fixing this."
                )

        # 4. PySpark jobs should reference SparkSession (scoped to first 2 KB)
        if stack in (TargetStack.PYSPARK, TargetStack.HYBRID) and fname.endswith(".py"):
            if "SparkSession" not in content and "spark" not in content.lower()[:2000]:
                file_issues.append(
                    f"⚠️ VALIDATION: '{fname}' appears to be a PySpark job but contains no "
                    "SparkSession reference — verify the conversion output is complete."
                )

        # 5. dbt models should have a SELECT or {{ ref( (scoped to first 5 KB)
        if stack == TargetStack.DBT and fname.endswith(".sql"):
            head = content[:5000].lower()
            if "select" not in head and "{{" not in content[:5000]:
                file_issues.append(
                    f"⚠️ VALIDATION: '{fname}' appears to be a dbt model but contains no SELECT or "
                    "Jinja block — the model may be empty or malformed."
                )

        # 6. run_pipeline.py must reference subprocess and dbt
        if fname == "run_pipeline.py":
            if "subprocess" not in content:
                file_issues.append(
                    "⚠️ VALIDATION: 'run_pipeline.py' does not reference subprocess — "
                    "orchestration wrapper may be incomplete."
                )
            if "dbt" not in content:
                file_issues.append(
                    "⚠️ VALIDATION: 'run_pipeline.py' does not reference dbt — "
                    "orchestration wrapper may be incomplete."
                )

        _cache[_key] = file_issues
        issues.extend(file_issues)

    return issues


# ── dbt warehouse-specific profiles.yml templates ─────────────────────────
# All credentials are sourced from environment variables — no hardcoded values.
_PROFILES_TEMPLATES: dict[str, str] = {
    "postgres": """\
# profiles.yml — generated by Informatica Conversion Tool
# Set the environment variables below before running dbt.
# Run:  dbt run --profiles-dir .
{mapping_name}:
  target: dev
  outputs:
    dev:
      type: postgres
      host: "{{{{ env_var('DBT_HOST') }}}}"
      port: 5432
      user: "{{{{ env_var('DBT_USER') }}}}"
      password: "{{{{ env_var('DBT_PASSWORD') }}}}"
      dbname: "{{{{ env_var('DBT_DATABASE') }}}}"
      schema: "{{{{ env_var('DBT_SCHEMA', 'public') }}}}"
      threads: 4
    prod:
      type: postgres
      host: "{{{{ env_var('DBT_HOST_PROD') }}}}"
      port: 5432
      user: "{{{{ env_var('DBT_USER_PROD') }}}}"
      password: "{{{{ env_var('DBT_PASSWORD_PROD') }}}}"
      dbname: "{{{{ env_var('DBT_DATABASE_PROD') }}}}"
      schema: "{{{{ env_var('DBT_SCHEMA_PROD', 'public') }}}}"
      threads: 8
""",
    "snowflake": """\
# profiles.yml — generated by Informatica Conversion Tool
# Set the environment variables below before running dbt.
{mapping_name}:
  target: dev
  outputs:
    dev:
      type: snowflake
      account: "{{{{ env_var('SNOWFLAKE_ACCOUNT') }}}}"
      user: "{{{{ env_var('SNOWFLAKE_USER') }}}}"
      password: "{{{{ env_var('SNOWFLAKE_PASSWORD') }}}}"
      role: "{{{{ env_var('SNOWFLAKE_ROLE', 'TRANSFORMER') }}}}"
      database: "{{{{ env_var('SNOWFLAKE_DATABASE') }}}}"
      warehouse: "{{{{ env_var('SNOWFLAKE_WAREHOUSE') }}}}"
      schema: "{{{{ env_var('SNOWFLAKE_SCHEMA', 'DEV') }}}}"
      threads: 8
    prod:
      type: snowflake
      account: "{{{{ env_var('SNOWFLAKE_ACCOUNT') }}}}"
      user: "{{{{ env_var('SNOWFLAKE_USER_PROD') }}}}"
      password: "{{{{ env_var('SNOWFLAKE_PASSWORD_PROD') }}}}"
      role: "{{{{ env_var('SNOWFLAKE_ROLE_PROD', 'TRANSFORMER') }}}}"
      database: "{{{{ env_var('SNOWFLAKE_DATABASE_PROD') }}}}"
      warehouse: "{{{{ env_var('SNOWFLAKE_WAREHOUSE_PROD') }}}}"
      schema: "{{{{ env_var('SNOWFLAKE_SCHEMA_PROD', 'PROD') }}}}"
      threads: 16
""",
    "redshift": """\
# profiles.yml — generated by Informatica Conversion Tool
{mapping_name}:
  target: dev
  outputs:
    dev:
      type: redshift
      host: "{{{{ env_var('REDSHIFT_HOST') }}}}"
      port: 5439
      user: "{{{{ env_var('REDSHIFT_USER') }}}}"
      password: "{{{{ env_var('REDSHIFT_PASSWORD') }}}}"
      dbname: "{{{{ env_var('REDSHIFT_DATABASE') }}}}"
      schema: "{{{{ env_var('REDSHIFT_SCHEMA', 'public') }}}}"
      threads: 4
      ra3_node: true
    prod:
      type: redshift
      host: "{{{{ env_var('REDSHIFT_HOST_PROD') }}}}"
      port: 5439
      user: "{{{{ env_var('REDSHIFT_USER_PROD') }}}}"
      password: "{{{{ env_var('REDSHIFT_PASSWORD_PROD') }}}}"
      dbname: "{{{{ env_var('REDSHIFT_DATABASE_PROD') }}}}"
      schema: "{{{{ env_var('REDSHIFT_SCHEMA_PROD', 'public') }}}}"
      threads: 8
      ra3_node: true
""",
    "bigquery": """\
# profiles.yml — generated by Informatica Conversion Tool
# Authentication: set GOOGLE_APPLICATION_CREDENTIALS to your service-account JSON path.
{mapping_name}:
  target: dev
  outputs:
    dev:
      type: bigquery
      method: service-account
      project: "{{{{ env_var('BQ_PROJECT') }}}}"
      dataset: "{{{{ env_var('BQ_DATASET_DEV') }}}}"
      keyfile: "{{{{ env_var('GOOGLE_APPLICATION_CREDENTIALS') }}}}"
      threads: 4
      timeout_seconds: 300
    prod:
      type: bigquery
      method: service-account
      project: "{{{{ env_var('BQ_PROJECT_PROD') }}}}"
      dataset: "{{{{ env_var('BQ_DATASET_PROD') }}}}"
      keyfile: "{{{{ env_var('GOOGLE_APPLICATION_CREDENTIALS_PROD') }}}}"
      threads: 8
      timeout_seconds: 300
""",
    "databricks": """\
# profiles.yml — generated by Informatica Conversion Tool
{mapping_name}:
  target: dev
  outputs:
    dev:
      type: databricks
      host: "{{{{ env_var('DATABRICKS_HOST') }}}}"
      http_path: "{{{{ env_var('DATABRICKS_HTTP_PATH') }}}}"
      token: "{{{{ env_var('DATABRICKS_TOKEN') }}}}"
      schema: "{{{{ env_var('DATABRICKS_SCHEMA', 'dev') }}}}"
      catalog: "{{{{ env_var('DATABRICKS_CATALOG', 'hive_metastore') }}}}"
      threads: 4
    prod:
      type: databricks
      host: "{{{{ env_var('DATABRICKS_HOST_PROD') }}}}"
      http_path: "{{{{ env_var('DATABRICKS_HTTP_PATH_PROD') }}}}"
      token: "{{{{ env_var('DATABRICKS_TOKEN_PROD') }}}}"
      schema: "{{{{ env_var('DATABRICKS_SCHEMA_PROD', 'prod') }}}}"
      catalog: "{{{{ env_var('DATABRICKS_CATALOG_PROD', 'hive_metastore') }}}}"
      threads: 8
""",
    "sqlserver": """\
# profiles.yml — generated by Informatica Conversion Tool
{mapping_name}:
  target: dev
  outputs:
    dev:
      type: sqlserver
      driver: "ODBC Driver 18 for SQL Server"
      server: "{{{{ env_var('MSSQL_SERVER') }}}}"
      port: 1433
      database: "{{{{ env_var('MSSQL_DATABASE') }}}}"
      schema: "{{{{ env_var('MSSQL_SCHEMA', 'dbo') }}}}"
      username: "{{{{ env_var('MSSQL_USER') }}}}"
      password: "{{{{ env_var('MSSQL_PASSWORD') }}}}"
      authentication: sql
      threads: 4
    prod:
      type: sqlserver
      driver: "ODBC Driver 18 for SQL Server"
      server: "{{{{ env_var('MSSQL_SERVER_PROD') }}}}"
      port: 1433
      database: "{{{{ env_var('MSSQL_DATABASE_PROD') }}}}"
      schema: "{{{{ env_var('MSSQL_SCHEMA_PROD', 'dbo') }}}}"
      username: "{{{{ env_var('MSSQL_USER_PROD') }}}}"
      password: "{{{{ env_var('MSSQL_PASSWORD_PROD') }}}}"
      authentication: sql
      threads: 8
""",
}

_DBT_ADAPTER_PACKAGES: dict[str, str] = {
    "snowflake":  "dbt-snowflake>=1.7,<2.0",
    "redshift":   "dbt-redshift>=1.7,<2.0",
    "bigquery":   "dbt-bigquery>=1.7,<2.0",
    "databricks": "dbt-databricks>=1.7,<2.0",
    "sqlserver":  "dbt-sqlserver>=1.7,<2.0",
    "postgres":   "dbt-postgres>=1.7,<2.0",
}


def _build_dbt_runtime_artifacts(
    stack_assignment: StackAssignment,
    session_parse_report: Optional[SessionParseReport],
) -> dict[str, str]:
    """
    Programmatically generate profiles.yml and requirements.txt for dbt jobs.

    These are deterministic templates (no Claude call needed).
    The warehouse type is auto-detected from the connection_type in the session
    config; falls back to postgres when no session data is available.
    """
    artifacts: dict[str, str] = {}
    mapping_slug = stack_assignment.mapping_name.lower().replace(" ", "_").replace("-", "_")

    # ── Detect warehouse from connection metadata ──────────────────────────
    warehouse = "postgres"
    sc = session_parse_report.session_config if session_parse_report else None
    if sc:
        for conn in sc.connections:
            ct = (conn.connection_type or "").upper()
            if "SNOWFLAKE"  in ct: warehouse = "snowflake";   break
            if "REDSHIFT"   in ct: warehouse = "redshift";    break
            if "BIGQUERY"   in ct: warehouse = "bigquery";    break
            if "DATABRICKS" in ct: warehouse = "databricks";  break
            if "MSSQL"      in ct or "SQLSERVER" in ct: warehouse = "sqlserver"; break

    # ── profiles.yml ──────────────────────────────────────────────────────
    template = _PROFILES_TEMPLATES.get(warehouse, _PROFILES_TEMPLATES["postgres"])
    artifacts["profiles.yml"] = template.replace("{mapping_name}", mapping_slug)

    # ── requirements.txt ──────────────────────────────────────────────────
    adapter_pkg = _DBT_ADAPTER_PACKAGES.get(warehouse, "dbt-postgres>=1.7,<2.0")
    artifacts["requirements.txt"] = "\n".join([
        "# Generated by Informatica Conversion Tool — install before running the pipeline",
        "# pip install -r requirements.txt",
        "",
        "# dbt + warehouse adapter",
        "dbt-core>=1.7,<2.0",
        adapter_pkg,
        "",
        "# EL script dependencies",
        "pandas>=2.0,<3.0",
        "pyarrow>=14.0",
        "sqlalchemy>=2.0,<3.0",
        "python-dotenv>=1.0",
        "",
    ])

    return artifacts


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
    verification_flags: list[dict] | None = None,
    manifest_overrides: list[dict] | None = None,
) -> ConversionOutput:
    """
    Generate converted code for the assigned target stack.

    accepted_fixes      — reviewer-approved code-level fixes from Gate 1 (Step 5)
    security_findings   — security scan findings from a previous round (REQUEST_FIX path);
                          Claude must address every listed finding in this regeneration
    verification_flags  — flags from Step 4 verification; Claude auto-handles each
                          (stubs, TODOs, config extraction) so the conversion is never blocked
                          by issues that can be addressed in code
    manifest_overrides  — reviewer-supplied overrides from the pre-conversion manifest xlsx;
                          these take precedence over tool-inferred connections for any
                          ambiguous or unmapped items
    """
    client = make_client()
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

    # Build verification flag auto-handling section
    flag_handling_section = _build_flag_handling_section(
        [f.model_dump() if hasattr(f, "model_dump") else f for f in (verification_flags or [])]
    )

    # Build manifest overrides section — reviewer-confirmed connections and gap resolutions
    manifest_override_section = _build_manifest_override_section(manifest_overrides or [])

    prompt = CONVERSION_PROMPT.format(
        stack=stack.value,
        rationale=stack_assignment.rationale,
        security_context=security_context,
        approved_fixes_section=approved_fixes_section + security_fix_section,
        flag_handling_section=flag_handling_section,
        manifest_override_section=manifest_override_section,
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

    # ── GAP #7 — Claude output content validation ─────────────────────────
    # Run non-blocking checks on every generated file.  Issues are added to
    # notes so the reviewer sees them, but they never block the pipeline.
    _validation_issues = _validate_conversion_files(files, stack)
    notes.extend(_validation_issues)

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

    # ── v2.7: inject dbt runtime artifacts (profiles.yml, requirements.txt) ─
    # Makes the dbt output fully execution-ready — adds the EL layer is
    # generated by the prompt above; these two files are deterministic templates.
    if stack == TargetStack.DBT:
        try:
            dbt_rt = _build_dbt_runtime_artifacts(stack_assignment, session_parse_report)
            files.update(dbt_rt)
            notes.append(
                f"v2.7: Generated dbt runtime artifacts: {', '.join(dbt_rt.keys())}. "
                "Set the environment variables in profiles.yml before running."
            )
        except Exception as e:
            notes.append(f"v2.7: dbt runtime artifact generation failed (non-blocking): {e}")

    return ConversionOutput(
        mapping_name=stack_assignment.mapping_name,
        target_stack=stack,
        files=files,
        notes=notes,
        parse_ok=parsed_ok,
    )
