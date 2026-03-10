"""
Unit tests for deterministic (non-Claude) business logic.

No API key required. All tests are fully offline.

Run:  python3 test_core.py [-v]
      python3 -m pytest test_core.py -v   (if pytest installed)
"""
import asyncio
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import MagicMock

# ── path setup ────────────────────────────────────────────────────────────────
sys.path.insert(0, str(Path(__file__).parent))

# ── env setup BEFORE any backend imports ──────────────────────────────────────
# auth.py hashes APP_PASSWORD at module-load time; must be set first.
os.environ.setdefault("SECRET_KEY",   "test-secret-key-32-chars-exactly!")
os.environ.setdefault("APP_PASSWORD", "s3cr3t-test-password")
os.environ.setdefault("DATABASE_URL", "sqlite+aiosqlite:///test_core_tmp.db")

# ── imports ───────────────────────────────────────────────────────────────────
from backend.limiter import _parse, RateLimiter
from backend.auth   import create_session_token, verify_session_token, check_password
from backend.agents.conversion_agent import (
    _is_sql_friendly,
    _validate_conversion_files,
    _build_dbt_runtime_artifacts,
    _build_flag_handling_section,
)
from backend.models.schemas import (
    TargetStack, StackAssignment, ComplexityTier,
    SessionParseReport, SessionConfig, SessionConnection,
    CrossRefValidation, UploadedFile, FileType,
)

SEP  = "─" * 60
PASS = 0
FAIL = 0


def _run(coro):
    """Run a coroutine synchronously (Python 3.10+)."""
    return asyncio.get_event_loop().run_until_complete(coro)


# ══════════════════════════════════════════════════════════════════════════════
# 1. Rate Limiter — _parse()
# ══════════════════════════════════════════════════════════════════════════════

class TestRateLimiterParse(unittest.TestCase):

    def test_parse_per_minute(self):
        count, period = _parse("20/minute")
        self.assertEqual(count, 20)
        self.assertEqual(period, 60)

    def test_parse_per_second(self):
        count, period = _parse("5/second")
        self.assertEqual(count, 5)
        self.assertEqual(period, 1)

    def test_parse_per_hour(self):
        count, period = _parse("100/hour")
        self.assertEqual(count, 100)
        self.assertEqual(period, 3600)

    def test_parse_per_day(self):
        count, period = _parse("1000/day")
        self.assertEqual(count, 1000)
        self.assertEqual(period, 86400)

    def test_parse_invalid_falls_back_to_default(self):
        count, period = _parse("bad/string")
        self.assertEqual(count, 20)
        self.assertEqual(period, 60)

    def test_parse_unknown_unit_falls_back(self):
        count, period = _parse("10/week")
        self.assertEqual(count, 20)
        self.assertEqual(period, 60)

    def test_parse_whitespace_tolerant(self):
        count, period = _parse(" 30 / minute ")
        self.assertEqual(count, 30)
        self.assertEqual(period, 60)


# ══════════════════════════════════════════════════════════════════════════════
# 2. Rate Limiter — sliding window behaviour
# ══════════════════════════════════════════════════════════════════════════════

class TestRateLimiterBehaviour(unittest.TestCase):

    def _make_request(self, ip="127.0.0.1"):
        req = MagicMock()
        req.client.host = ip
        return req

    def test_allows_within_limit(self):
        limiter = RateLimiter("3/minute")
        req = self._make_request()
        for _ in range(3):
            _run(limiter(req))   # Must not raise

    def test_blocks_over_limit(self):
        from fastapi import HTTPException
        limiter = RateLimiter("2/minute")
        req = self._make_request()
        _run(limiter(req))
        _run(limiter(req))
        with self.assertRaises(HTTPException) as ctx:
            _run(limiter(req))
        self.assertEqual(ctx.exception.status_code, 429)

    def test_different_ips_are_independent(self):
        limiter = RateLimiter("1/minute")
        _run(limiter(self._make_request("10.0.0.1")))
        _run(limiter(self._make_request("10.0.0.2")))  # different IP — must not raise

    def test_window_expires_allows_again(self):
        """Requests older than the period should not count."""
        limiter = RateLimiter("1/second")
        req = self._make_request()
        _run(limiter(req))
        # Manually age the window entry by >1 second
        limiter._windows["127.0.0.1"] = [time.monotonic() - 2.0]
        _run(limiter(req))  # window has expired — must not raise


# ══════════════════════════════════════════════════════════════════════════════
# 3. Auth — token round-trip and password checks
# ══════════════════════════════════════════════════════════════════════════════

class TestAuth(unittest.TestCase):

    def test_token_roundtrip(self):
        token = create_session_token()
        self.assertIsInstance(token, str)
        self.assertTrue(len(token) > 10)
        self.assertTrue(verify_session_token(token))

    def test_tampered_token_rejected(self):
        token = create_session_token()
        tampered = token[:-5] + "XXXXX"
        self.assertFalse(verify_session_token(tampered))

    def test_empty_token_rejected(self):
        self.assertFalse(verify_session_token(""))

    def test_garbage_token_rejected(self):
        self.assertFalse(verify_session_token("notavalidtoken"))

    def test_correct_password_accepted(self):
        self.assertTrue(check_password("s3cr3t-test-password"))

    def test_wrong_password_rejected(self):
        self.assertFalse(check_password("wrong-password"))

    def test_empty_password_rejected(self):
        self.assertFalse(check_password(""))


# ══════════════════════════════════════════════════════════════════════════════
# 4. Stack Assignment — _is_sql_friendly()
# ══════════════════════════════════════════════════════════════════════════════

class TestIsSqlFriendly(unittest.TestCase):

    def test_sql_friendly_transformations(self):
        trans = ["Expression", "Filter", "Aggregator", "Source Qualifier"]
        self.assertTrue(_is_sql_friendly(trans))

    def test_java_transformation_blocks_sql(self):
        trans = ["Expression", "Filter", "Java Transformation"]
        self.assertFalse(_is_sql_friendly(trans))

    def test_http_transformation_blocks_sql(self):
        trans = ["Expression", "HTTP Transformation"]
        self.assertFalse(_is_sql_friendly(trans))

    def test_external_procedure_blocks_sql(self):
        trans = ["Expression", "External Procedure"]
        self.assertFalse(_is_sql_friendly(trans))

    def test_normalizer_blocks_sql(self):
        trans = ["Expression", "Filter", "Normalizer"]
        self.assertFalse(_is_sql_friendly(trans))

    def test_empty_list_not_sql_friendly(self):
        # No sql_friendly present → returns False
        self.assertFalse(_is_sql_friendly([]))

    def test_only_non_sql_transformations(self):
        trans = ["Java Transformation", "External Procedure"]
        self.assertFalse(_is_sql_friendly(trans))

    def test_joiner_is_sql_friendly(self):
        trans = ["Source Qualifier", "Joiner", "Expression"]
        self.assertTrue(_is_sql_friendly(trans))


# ══════════════════════════════════════════════════════════════════════════════
# 5. _validate_conversion_files()
# ══════════════════════════════════════════════════════════════════════════════

class TestValidateConversionFiles(unittest.TestCase):

    def _run(self, files, stack=TargetStack.PYTHON):
        return _validate_conversion_files(files, stack)

    def test_clean_python_file_no_warnings(self):
        files = {
            "transform.py": (
                "import pandas as pd\n\n"
                "def run():\n"
                "    df = pd.read_csv('input.csv')\n"
                "    return df\n"
            )
        }
        issues = self._run(files)
        self.assertEqual(issues, [])

    def test_empty_file_flagged(self):
        files = {"transform.py": ""}
        issues = self._run(files)
        self.assertTrue(any("empty" in i.lower() for i in issues))

    def test_whitespace_only_file_flagged(self):
        files = {"transform.py": "   \n\n   "}
        issues = self._run(files)
        self.assertTrue(any("empty" in i.lower() for i in issues))

    def test_python_syntax_error_flagged(self):
        files = {"broken.py": "def foo(\n    pass\n"}
        issues = self._run(files)
        self.assertTrue(any("syntax error" in i.lower() for i in issues))

    def test_good_python_syntax_no_warning(self):
        files = {
            "clean.py": (
                "def greet(name: str) -> str:\n"
                "    return f'Hello, {name}'\n"
            )
        }
        issues = self._run(files)
        self.assertFalse(any("syntax" in i.lower() for i in issues))

    def test_pyspark_file_missing_spark_reference_flagged(self):
        files = {
            "job.py": (
                "import pandas as pd\n\n"
                "def run():\n"
                "    return 'no distributed compute reference'\n"
            )
        }
        issues = self._run(files, stack=TargetStack.PYSPARK)
        self.assertTrue(any("spark" in i.lower() for i in issues))

    def test_pyspark_file_with_spark_session_ok(self):
        files = {
            "job.py": (
                "from pyspark.sql import SparkSession\n\n"
                "def run():\n"
                "    spark = SparkSession.builder.getOrCreate()\n"
                "    return spark\n"
            )
        }
        issues = self._run(files, stack=TargetStack.PYSPARK)
        self.assertFalse(any("spark" in i.lower() for i in issues))

    def test_dbt_sql_missing_select_flagged(self):
        # No SELECT keyword and no Jinja block → should be flagged
        files = {"models/stg_orders.sql": "-- placeholder, awaiting implementation\n"}
        issues = self._run(files, stack=TargetStack.DBT)
        self.assertTrue(any("select" in i.lower() for i in issues))

    def test_dbt_sql_with_select_ok(self):
        files = {
            "models/stg_orders.sql": (
                "SELECT order_id, order_amount\n"
                "FROM {{ source('raw', 'orders') }}\n"
            )
        }
        issues = self._run(files, stack=TargetStack.DBT)
        self.assertFalse(any("select" in i.lower() for i in issues))

    def test_run_pipeline_missing_subprocess_flagged(self):
        files = {"run_pipeline.py": "def run(): pass\n# calls dbt"}
        issues = self._run(files, stack=TargetStack.DBT)
        self.assertTrue(any("subprocess" in i.lower() for i in issues))

    def test_run_pipeline_missing_dbt_flagged(self):
        files = {"run_pipeline.py": "import subprocess\ndef run(): pass\n"}
        issues = self._run(files, stack=TargetStack.DBT)
        self.assertTrue(any("dbt" in i.lower() for i in issues))

    def test_run_pipeline_complete_ok(self):
        files = {
            "run_pipeline.py": (
                "import subprocess, sys\n\n"
                "def run_pipeline():\n"
                "    r = subprocess.run(['dbt', 'run'], check=False)\n"
                "    if r.returncode != 0: sys.exit(1)\n\n"
                "if __name__ == '__main__': run_pipeline()\n"
            )
        }
        issues = self._run(files, stack=TargetStack.DBT)
        pipeline_issues = [i for i in issues if "run_pipeline" in i.lower()]
        self.assertEqual(pipeline_issues, [])

    def test_stub_heavy_file_flagged(self):
        """File with >60% TODO lines should warn."""
        lines = ["x = 1"] + ["# TODO: implement this" for _ in range(10)]
        files = {"transform.py": "\n".join(lines)}
        issues = self._run(files)
        self.assertTrue(any("todo" in i.lower() or "stub" in i.lower() for i in issues))

    def test_cache_deduplicates_same_content(self):
        """Same file content validated twice with a shared cache should not duplicate issues."""
        cache = {}
        content = ""   # empty → 1 issue
        files = {"a.py": content, "b.py": content}
        issues = _validate_conversion_files(files, TargetStack.PYTHON, _cache=cache)
        # Both files have the same content — issue is reported for each file name,
        # but the cache should have been populated after the first
        self.assertEqual(len(cache), 1)


# ══════════════════════════════════════════════════════════════════════════════
# 6. _build_dbt_runtime_artifacts()
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildDbtRuntimeArtifacts(unittest.TestCase):

    def _make_assignment(self, name="m_Orders_to_Fact") -> StackAssignment:
        return StackAssignment(
            mapping_name=name,
            complexity_tier=ComplexityTier.MEDIUM,
            assigned_stack=TargetStack.DBT,
            rationale="SQL-friendly transformations",
            data_volume_est=None,
            special_concerns=[],
        )

    def _make_session_report(self, connection_type: str) -> SessionParseReport:
        return SessionParseReport(
            uploaded_files=[],
            cross_ref=CrossRefValidation(status="VALID"),
            session_config=SessionConfig(
                session_name="s_test",
                mapping_name="m_test",
                workflow_name="wf_test",
                connections=[
                    SessionConnection(
                        transformation_name="SQ_SOURCE",
                        role="SOURCE",
                        connection_type=connection_type,
                    )
                ],
            ),
            parse_status="COMPLETE",
        )

    def test_default_warehouse_is_postgres(self):
        artifacts = _build_dbt_runtime_artifacts(self._make_assignment(), None)
        self.assertIn("profiles.yml", artifacts)
        self.assertIn("type: postgres", artifacts["profiles.yml"])

    def test_snowflake_detected(self):
        spr = self._make_session_report("SNOWFLAKE_ODBC")
        artifacts = _build_dbt_runtime_artifacts(self._make_assignment(), spr)
        self.assertIn("type: snowflake", artifacts["profiles.yml"])
        self.assertIn("dbt-snowflake", artifacts["requirements.txt"])

    def test_redshift_detected(self):
        spr = self._make_session_report("REDSHIFT_ODBC")
        artifacts = _build_dbt_runtime_artifacts(self._make_assignment(), spr)
        self.assertIn("type: redshift", artifacts["profiles.yml"])
        self.assertIn("dbt-redshift", artifacts["requirements.txt"])

    def test_bigquery_detected(self):
        spr = self._make_session_report("BIGQUERY_JDBC")
        artifacts = _build_dbt_runtime_artifacts(self._make_assignment(), spr)
        self.assertIn("type: bigquery", artifacts["profiles.yml"])
        self.assertIn("dbt-bigquery", artifacts["requirements.txt"])

    def test_databricks_detected(self):
        spr = self._make_session_report("DATABRICKS_ODBC")
        artifacts = _build_dbt_runtime_artifacts(self._make_assignment(), spr)
        self.assertIn("type: databricks", artifacts["profiles.yml"])
        self.assertIn("dbt-databricks", artifacts["requirements.txt"])

    def test_sqlserver_detected(self):
        spr = self._make_session_report("MSSQL_ODBC")
        artifacts = _build_dbt_runtime_artifacts(self._make_assignment(), spr)
        self.assertIn("type: sqlserver", artifacts["profiles.yml"])
        self.assertIn("dbt-sqlserver", artifacts["requirements.txt"])

    def test_mapping_slug_used_in_profiles(self):
        assignment = self._make_assignment("m_HR Employees to DIM")
        artifacts = _build_dbt_runtime_artifacts(assignment, None)
        self.assertIn("m_hr_employees_to_dim", artifacts["profiles.yml"])

    def test_no_hardcoded_credentials(self):
        """profiles.yml must not contain literal passwords."""
        artifacts = _build_dbt_runtime_artifacts(self._make_assignment(), None)
        profile = artifacts["profiles.yml"]
        for bad in ("password:", "passwd:", "secret:"):
            # Allowed only as env_var() references, never as plain values
            lines_with_bad = [
                l for l in profile.splitlines()
                if bad in l.lower() and "env_var" not in l.lower()
            ]
            self.assertEqual(
                lines_with_bad, [],
                msg=f"Found hardcoded credential hint '{bad}' in profiles.yml: {lines_with_bad}"
            )

    def test_requirements_contains_dbt_core(self):
        artifacts = _build_dbt_runtime_artifacts(self._make_assignment(), None)
        self.assertIn("dbt-core", artifacts["requirements.txt"])

    def test_requirements_contains_pandas(self):
        artifacts = _build_dbt_runtime_artifacts(self._make_assignment(), None)
        self.assertIn("pandas", artifacts["requirements.txt"])

    def test_both_artifacts_returned(self):
        artifacts = _build_dbt_runtime_artifacts(self._make_assignment(), None)
        self.assertIn("profiles.yml", artifacts)
        self.assertIn("requirements.txt", artifacts)


# ══════════════════════════════════════════════════════════════════════════════
# 7. _build_flag_handling_section()
# ══════════════════════════════════════════════════════════════════════════════

class TestBuildFlagHandlingSection(unittest.TestCase):

    def test_empty_flags_returns_empty_string(self):
        self.assertEqual(_build_flag_handling_section([]), "")

    def test_info_flag_ignored(self):
        flags = [{"flag_type": "INFO", "location": "EXP_X", "description": "note"}]
        result = _build_flag_handling_section(flags)
        self.assertEqual(result, "")

    def test_incomplete_logic_generates_section(self):
        flags = [{
            "flag_type": "INCOMPLETE_LOGIC",
            "location": "FIL_VALID_ORDERS",
            "description": "Filter has no condition",
        }]
        result = _build_flag_handling_section(flags)
        self.assertIn("INCOMPLETE_LOGIC", result)
        self.assertIn("PASS-THROUGH", result)
        self.assertIn("FIL_VALID_ORDERS", result)

    def test_lineage_gap_generates_null_instruction(self):
        flags = [{
            "flag_type": "LINEAGE_GAP",
            "location": "TARGET_FIELD_X",
            "description": "No source found",
        }]
        result = _build_flag_handling_section(flags)
        self.assertIn("LINEAGE_GAP", result)
        self.assertIn("None", result)

    def test_duplicate_flags_deduplicated(self):
        flags = [
            {"flag_type": "HIGH_RISK", "location": "EXP_X", "description": "risk"},
            {"flag_type": "HIGH_RISK", "location": "EXP_X", "description": "same loc"},
        ]
        result = _build_flag_handling_section(flags)
        # Same flag_type + location → appears only once
        self.assertEqual(result.count("HIGH_RISK"), 1)

    def test_multiple_different_flags_all_present(self):
        flags = [
            {"flag_type": "INCOMPLETE_LOGIC", "location": "A", "description": "d1"},
            {"flag_type": "LINEAGE_GAP",      "location": "B", "description": "d2"},
            {"flag_type": "DEAD_LOGIC",       "location": "C", "description": "d3"},
        ]
        result = _build_flag_handling_section(flags)
        self.assertIn("INCOMPLETE_LOGIC", result)
        self.assertIn("LINEAGE_GAP", result)
        self.assertIn("DEAD_LOGIC", result)

    def test_environment_specific_value_moves_to_config(self):
        flags = [{
            "flag_type": "ENVIRONMENT_SPECIFIC_VALUE",
            "location": "SQ_ORDERS",
            "description": "Hardcoded schema name",
        }]
        result = _build_flag_handling_section(flags)
        self.assertIn("config", result.lower())

    def test_unsupported_transformation_generates_stub(self):
        flags = [{
            "flag_type": "UNSUPPORTED_TRANSFORMATION",
            "location": "JAVA_TRANS",
            "description": "Java transformation",
        }]
        result = _build_flag_handling_section(flags)
        self.assertIn("stub", result.lower())
        self.assertIn("MANUAL REQUIRED", result)


# ══════════════════════════════════════════════════════════════════════════════
# 8. Smoke Execute
# ══════════════════════════════════════════════════════════════════════════════

from backend.smoke_execute import smoke_execute_files, SmokeResult

class TestSmokeExecute(unittest.TestCase):

    def test_valid_python_passes(self):
        files = {"job.py": "def run():\n    return 42\n"}
        results = smoke_execute_files(files, TargetStack.PYTHON)
        self.assertTrue(all(r.passed for r in results))
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].tool, "py_compile")

    def test_syntax_error_python_fails(self):
        files = {"bad.py": "def foo(\n    pass\n"}
        results = smoke_execute_files(files, TargetStack.PYTHON)
        self.assertFalse(all(r.passed for r in results))
        failed = [r for r in results if not r.passed]
        self.assertEqual(failed[0].tool, "py_compile")
        self.assertIsNotNone(failed[0].detail)

    def test_valid_dbt_sql_passes(self):
        files = {
            "models/orders.sql": (
                "SELECT order_id, order_amount\n"
                "FROM {{ source('raw', 'orders') }}\n"
            )
        }
        results = smoke_execute_files(files, TargetStack.DBT)
        sql_results = [r for r in results if "models/orders.sql" in r.filename]
        self.assertTrue(all(r.passed for r in sql_results))

    def test_empty_dbt_sql_fails(self):
        files = {"models/orders.sql": "-- nothing here\n"}
        results = smoke_execute_files(files, TargetStack.DBT)
        self.assertTrue(any(not r.passed for r in results))

    def test_unbalanced_jinja_fails(self):
        files = {"models/bad.sql": "SELECT {{ ref('orders') FROM orders\n"}
        results = smoke_execute_files(files, TargetStack.DBT)
        sql_r = [r for r in results if "bad.sql" in r.filename]
        self.assertTrue(any(not r.passed for r in sql_r))

    def test_valid_yaml_passes(self):
        files = {
            "schema.yml": (
                "version: 2\nmodels:\n  - name: orders\n    description: test\n"
            )
        }
        results = smoke_execute_files(files, TargetStack.DBT)
        yml_r = [r for r in results if "schema.yml" in r.filename]
        self.assertTrue(all(r.passed for r in yml_r))

    def test_malformed_yaml_fails(self):
        files = {"schema.yml": "version: 2\n  bad_indent:\n- wrong\n"}
        results = smoke_execute_files(files, TargetStack.DBT)
        yml_r = [r for r in results if "schema.yml" in r.filename]
        self.assertTrue(any(not r.passed for r in yml_r))

    def test_profiles_yml_not_checked(self):
        """profiles.yml is deliberately excluded from yaml checks (it's a dbt artifact)."""
        files = {"profiles.yml": "# generated profiles\nmy_project:\n  target: dev\n"}
        results = smoke_execute_files(files, TargetStack.DBT)
        profiles_results = [r for r in results if "profiles.yml" in r.filename]
        self.assertEqual(profiles_results, [])

    def test_multiple_files_all_checked(self):
        files = {
            "extract.py":       "import pandas as pd\ndef extract(): pass\n",
            "run_pipeline.py":  "import subprocess\ndef run(): pass\n",
        }
        results = smoke_execute_files(files, TargetStack.DBT)
        self.assertEqual(len(results), 2)

    def test_result_dataclass_fields(self):
        files = {"clean.py": "x = 1\n"}
        results = smoke_execute_files(files, TargetStack.PYTHON)
        r = results[0]
        self.assertIsInstance(r, SmokeResult)
        self.assertIsInstance(r.filename, str)
        self.assertIsInstance(r.tool, str)
        self.assertIsInstance(r.passed, bool)


# ══════════════════════════════════════════════════════════════════════════════
# 9. Reconciliation Agent (structural)
# ══════════════════════════════════════════════════════════════════════════════

from backend.agents.reconciliation_agent import generate_reconciliation_report
from backend.models.schemas import ParseReport as _ParseReport, ConversionOutput as _ConvOutput

class TestReconciliationAgent(unittest.TestCase):

    def _make_parse(self, mapping_name="m_TEST"):
        return _ParseReport(
            objects_found={"Mapping": 1, "Transformation": 2},
            reusable_components=[],
            unresolved_parameters=[],
            malformed_xml=[],
            unrecognized_elements=[],
            flags=[],
            parse_status="COMPLETE",
            mapping_names=[mapping_name],
        )

    def _make_conv(self, mapping_name="m_TEST", code="") -> _ConvOutput:
        return _ConvOutput(
            mapping_name=mapping_name,
            target_stack=TargetStack.PYTHON,
            files={"transform.py": code},
            notes=[],
        )

    def test_all_fields_found_is_reconciled(self):
        code = "ORDER_ID = row['ORDER_ID']\nCUSTOMER_ID = row['CUSTOMER_ID']\n"
        conv = self._make_conv(code=code)
        report = generate_reconciliation_report(
            parse_report=self._make_parse(),
            conversion_output=conv,
            s2t_field_list=["ORDER_ID", "CUSTOMER_ID"],
            source_tables=[],          # explicit empty — isolate field coverage check
        )
        self.assertEqual(report.final_status, "RECONCILED")
        self.assertEqual(report.match_rate, 100.0)
        self.assertEqual(report.mismatched_fields, [])

    def test_missing_field_lowers_match_rate(self):
        code = "ORDER_ID = row['ORDER_ID']\n"
        conv = self._make_conv(code=code)
        report = generate_reconciliation_report(
            parse_report=self._make_parse(),
            conversion_output=conv,
            s2t_field_list=["ORDER_ID", "MISSING_FIELD"],
        )
        self.assertLess(report.match_rate, 100.0)
        self.assertTrue(any(m["field"] == "MISSING_FIELD" for m in report.mismatched_fields))

    def test_expression_found_increases_match_rate(self):
        code = "tax = amount * 0.085\n"
        conv = self._make_conv(code=code)
        report = generate_reconciliation_report(
            parse_report=self._make_parse(),
            conversion_output=conv,
            s2t_field_list=[],
            source_tables=[],          # isolate — check only the expression
            documented_expressions=["amount * 0.085"],
        )
        self.assertEqual(report.match_rate, 100.0)

    def test_expression_missing_is_flagged(self):
        code = "tax = amount * 0.10\n"
        conv = self._make_conv(code=code)
        report = generate_reconciliation_report(
            parse_report=self._make_parse(),
            conversion_output=conv,
            s2t_field_list=[],
            documented_expressions=["amount * 0.085"],
        )
        self.assertFalse(report.final_status == "RECONCILED")
        self.assertTrue(any(m["type"] == "EXPRESSION" for m in report.mismatched_fields))

    def test_stub_file_flagged_in_mismatches(self):
        stub_code = "\n".join(
            ["x = 1"] + ["# TODO: implement this block" for _ in range(15)]
        )
        conv = self._make_conv(code=stub_code)
        report = generate_reconciliation_report(
            parse_report=self._make_parse(),
            conversion_output=conv,
            s2t_field_list=[],
        )
        self.assertTrue(any(m["type"] == "STUB_COMPLETENESS" for m in report.mismatched_fields))

    def test_no_s2t_list_still_returns_report(self):
        conv = self._make_conv(code="ORDER_ID = 1\n")
        report = generate_reconciliation_report(
            parse_report=self._make_parse(),
            conversion_output=conv,
        )
        self.assertIsNotNone(report)
        self.assertIsNotNone(report.final_status)
        self.assertIsNotNone(report.match_rate)

    def test_report_fields_populated(self):
        conv = self._make_conv(code="ORDER_ID = row['ORDER_ID']\n")
        report = generate_reconciliation_report(
            parse_report=self._make_parse(),
            conversion_output=conv,
        )
        self.assertIsNotNone(report.mapping_name)
        self.assertIsNotNone(report.input_description)
        self.assertIsNone(report.informatica_rows)   # not executable
        self.assertIsNone(report.converted_rows)     # not executable


# ══════════════════════════════════════════════════════════════════════════════
# Runner
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    unittest.main(verbosity=2)
