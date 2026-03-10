"""
Integration test — Steps 5-8 of the Informatica Conversion pipeline.

Covers:
  Step 5  — Stack Assignment (assign_stack)
  Step 6  — Code Generation (convert)
  Step 7  — Security Scan (scan)
  Step 8  — Code Review + Logic Equivalence (review)

Requires: ANTHROPIC_API_KEY in .env

Run:
  python3 test_steps58.py                  # all steps on sample XML
  python3 test_steps58.py --step5-only     # fast check — stack assignment only
  python3 test_steps58.py --through-step6  # assignment + conversion
  python3 test_steps58.py --stack dbt      # force a specific stack (pyspark|dbt|python)
"""
from __future__ import annotations
import argparse
import asyncio
import json
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

sys.path.insert(0, str(Path(__file__).parent))

from backend.agents import parser_agent, classifier_agent
from backend.agents.conversion_agent import (
    assign_stack,
    convert,
    _validate_conversion_files,
)
from backend.agents.security_agent  import scan  as security_scan
from backend.agents.review_agent    import review as code_review
from backend.models.schemas         import TargetStack, ComplexityTier, ComplexityReport

SAMPLE_XML  = Path(__file__).parent / "sample_xml" / "sample_mapping.xml"
COMPLEX_XML = Path(__file__).parent / "sample_xml" / "complex" / "m_FNMA_LOAN_DELIVERY_SCD2.xml"
OUTPUT_DIR  = Path(__file__).parent / "test_output"
OUTPUT_DIR.mkdir(exist_ok=True)

SEP = "─" * 60

def heading(t): print(f"\n{SEP}\n {t}\n{SEP}")
def ok(t):      print(f"  ✅ {t}")
def warn(t):    print(f"  ⚠️  {t}")
def fail(t):    print(f"  ❌ {t}"); global _errors; _errors.append(t)
def skip(t):    print(f"  ⏭  {t}")

_errors: list[str] = []


# ─────────────────────────────────────────────────────────────────────────────
# Shared setup: parse + classify the sample mapping
# ─────────────────────────────────────────────────────────────────────────────

def _get_parse_artifacts(xml_path: Path):
    """Run Steps 1-2 (no Claude) and return (report, graph, complexity)."""
    xml = xml_path.read_text()
    report, graph = parser_agent.parse_xml(xml)
    if report.parse_status == "FAILED":
        print("  ❌ Parse failed — cannot continue"); sys.exit(1)
    complexity = classifier_agent.classify(report, graph)
    return xml, report, graph, complexity


# ─────────────────────────────────────────────────────────────────────────────
# STEP 5 — Stack Assignment
# ─────────────────────────────────────────────────────────────────────────────

async def run_step5(
    report, graph, complexity, force_stack: str | None = None
):
    heading("STEP 5 — STACK ASSIGNMENT")

    # ── Rule-based assignment (no Claude for the routing, Claude only for rationale)
    stack_assignment = await assign_stack(complexity, graph, report)

    print(f"  Mapping       : {stack_assignment.mapping_name}")
    print(f"  Complexity    : {stack_assignment.complexity_tier}")
    print(f"  Assigned stack: {stack_assignment.assigned_stack.value}")
    print(f"  Rationale     : {stack_assignment.rationale[:120]}...")

    # Optional stack override for testing
    if force_stack:
        from backend.models.schemas import StackAssignment
        override_map = {"pyspark": TargetStack.PYSPARK,
                        "dbt":     TargetStack.DBT,
                        "python":  TargetStack.PYTHON}
        forced = override_map.get(force_stack.lower(), stack_assignment.assigned_stack)
        stack_assignment = StackAssignment(
            mapping_name=stack_assignment.mapping_name,
            complexity_tier=stack_assignment.complexity_tier,
            assigned_stack=forced,
            rationale=f"[FORCED for testing] {stack_assignment.rationale}",
            data_volume_est=stack_assignment.data_volume_est,
            special_concerns=stack_assignment.special_concerns,
        )
        warn(f"Stack overridden to: {forced.value}")

    # Assertions
    checks = [
        (stack_assignment.mapping_name != "",
         "Mapping name populated"),
        (isinstance(stack_assignment.assigned_stack, TargetStack),
         "Assigned stack is a valid TargetStack enum"),
        (stack_assignment.complexity_tier in ComplexityTier.__members__.values(),
         "Complexity tier is valid"),
        (len(stack_assignment.rationale) > 10,
         "Rationale is non-empty"),
    ]
    for passed, label in checks:
        ok(label) if passed else fail(label)

    # Save
    out = OUTPUT_DIR / "step5_stack_assignment.json"
    out.write_text(json.dumps(stack_assignment.model_dump(), indent=2, default=str))
    print(f"  Saved to      : {out}")

    ok("Step 5 complete")
    return stack_assignment


# ─────────────────────────────────────────────────────────────────────────────
# STEP 6 — Code Generation
# ─────────────────────────────────────────────────────────────────────────────

async def run_step6(report, graph, complexity, stack_assignment, xml: str):
    heading("STEP 6 — CODE GENERATION  (calling Claude...)")

    # Step 6 needs documentation (Step 3) — generate a minimal docs string
    from backend.agents.documentation_agent import document
    docs_md = await document(report, complexity, graph, session_parse_report=None)

    conversion_output = await convert(
        stack_assignment=stack_assignment,
        documentation_md=docs_md,
        xml_content=xml,
        session_parse_report=None,
    )

    stack_name = conversion_output.target_stack.value
    files      = conversion_output.files
    notes      = conversion_output.notes

    print(f"  Stack         : {stack_name}")
    print(f"  Files produced: {list(files.keys())}")
    print(f"  Notes         : {len(notes)}")
    for n in notes[:5]:
        print(f"    • {n[:100]}")

    # Content validation (the deterministic checks)
    validation_issues = _validate_conversion_files(files, conversion_output.target_stack)
    if validation_issues:
        for issue in validation_issues:
            warn(issue)
    else:
        ok("File content validation: no issues")

    # Assertions
    checks = [
        (len(files) > 0,                "At least one file generated"),
        (conversion_output.parse_ok,    "Output parsed cleanly (no delimiter failures)"),
        (not any(
            "⚠️ VALIDATION: '{}' is empty".format(f) in i
            for f in files for i in validation_issues
         ),                             "No generated files are empty"),
    ]

    # Stack-specific file checks
    if conversion_output.target_stack == TargetStack.PYSPARK:
        checks.append((
            any(f.endswith(".py") for f in files),
            "PySpark: at least one .py file present"
        ))
    elif conversion_output.target_stack == TargetStack.DBT:
        checks.append((
            any(f.endswith(".sql") for f in files),
            "dbt: at least one .sql model present"
        ))
        checks.append((
            "profiles.yml" in files,
            "dbt: profiles.yml generated"
        ))
        checks.append((
            "requirements.txt" in files,
            "dbt: requirements.txt generated"
        ))
    elif conversion_output.target_stack == TargetStack.PYTHON:
        checks.append((
            any(f.endswith(".py") for f in files),
            "Python: at least one .py file present"
        ))

    for passed, label in checks:
        ok(label) if passed else fail(label)

    # Save
    out = OUTPUT_DIR / "step6_conversion_output.json"
    out.write_text(json.dumps(conversion_output.model_dump(), indent=2, default=str))
    print(f"  Saved to      : {out}")

    ok("Step 6 complete")
    return conversion_output, docs_md


# ─────────────────────────────────────────────────────────────────────────────
# STEP 7 — Security Scan
# ─────────────────────────────────────────────────────────────────────────────

async def run_step7(conversion_output):
    heading("STEP 7 — SECURITY SCAN  (calling Claude...)")

    scan_report = await security_scan(conversion_output)

    print(f"  Recommendation: {scan_report.recommendation}")
    print(f"  Ran bandit    : {scan_report.ran_bandit}")
    print(f"  Critical      : {scan_report.critical_count}")
    print(f"  High          : {scan_report.high_count}")
    print(f"  Medium        : {scan_report.medium_count}")
    print(f"  Low           : {scan_report.low_count}")
    if scan_report.findings:
        print(f"  Top findings  :")
        for f in scan_report.findings[:3]:
            print(f"    [{f.severity}] {f.test_name or f.text[:60]}")

    # Assertions
    checks = [
        (scan_report.mapping_name != "",
         "Mapping name populated"),
        (scan_report.recommendation in ("APPROVED", "REVIEW_RECOMMENDED", "REQUIRES_FIXES"),
         "Recommendation is a valid value"),
        (scan_report.critical_count >= 0,
         "Critical count is non-negative"),
        (scan_report.high_count    >= 0,
         "High count is non-negative"),
        # Critical finding in generated code is a real failure, not just a test failure
        (scan_report.critical_count == 0,
         "No CRITICAL findings in generated code"),
    ]
    for passed, label in checks:
        ok(label) if passed else warn(f"Security issue: {label}")

    # Save
    out = OUTPUT_DIR / "step7_security_scan.json"
    out.write_text(json.dumps(scan_report.model_dump(), indent=2, default=str))
    print(f"  Saved to      : {out}")

    ok("Step 7 complete")
    return scan_report


# ─────────────────────────────────────────────────────────────────────────────
# STEP 8 — Code Review + Logic Equivalence
# ─────────────────────────────────────────────────────────────────────────────

async def run_step8(report, conversion_output, docs_md, xml: str):
    heading("STEP 8 — CODE REVIEW + LOGIC EQUIVALENCE  (calling Claude...)")

    review_report = await code_review(
        conversion_output=conversion_output,
        parse_report=report,
        xml_content=xml,
        documentation_md=docs_md,
    )

    print(f"  Recommendation: {review_report.recommendation}")
    print(f"  Checks passed : {review_report.total_passed}/{review_report.total_checks}")
    print(f"  Checks failed : {review_report.total_failed}")

    if hasattr(review_report, "equivalence") and review_report.equivalence:
        eq = review_report.equivalence
        print(f"  Logic verified: {eq.total_verified}/{eq.total_verified + eq.total_needs_review + eq.total_mismatches}")
        print(f"  Coverage %    : {eq.coverage_pct:.0f}%")
        print(f"  Mismatches    : {eq.total_mismatches}")

    if review_report.checks:
        print("  Failed checks :")
        for c in review_report.checks:
            if not c.passed:
                print(f"    ✗ {c.name}: {(c.detail or '')[:80]}")

    # Assertions
    checks = [
        (review_report.recommendation in ("APPROVED", "APPROVED_WITH_NOTES", "REQUIRES_CHANGES"),
         "Recommendation is a valid value"),
        (review_report.total_checks > 0,
         "At least one check performed"),
        (review_report.total_passed >= 0,
         "Pass count is non-negative"),
    ]
    for passed, label in checks:
        ok(label) if passed else fail(label)

    # Save
    out = OUTPUT_DIR / "step8_code_review.json"
    out.write_text(json.dumps(review_report.model_dump(), indent=2, default=str))
    print(f"  Saved to      : {out}")

    ok("Step 8 complete")
    return review_report


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    global _errors

    parser = argparse.ArgumentParser(
        description="Integration test for pipeline Steps 5-8"
    )
    parser.add_argument("--step5-only",    action="store_true",
                        help="Run only Step 5 (fast, minimal Claude calls)")
    parser.add_argument("--through-step6", action="store_true",
                        help="Run Steps 5-6 only (skip security scan + review)")
    parser.add_argument("--stack",         default=None,
                        choices=["pyspark", "dbt", "python"],
                        help="Force a specific output stack (overrides rule-based assignment)")
    parser.add_argument("--complex",       action="store_true",
                        help="Use the complex SCD2 sample XML instead of the basic one")
    args = parser.parse_args()

    xml_path = COMPLEX_XML if args.complex else SAMPLE_XML
    if not xml_path.exists():
        print(f"❌ Sample XML not found: {xml_path}"); sys.exit(1)

    print(f"  Sample XML    : {xml_path.name}")

    xml, report, graph, complexity = _get_parse_artifacts(xml_path)

    # Step 5 — always runs
    stack_assignment = await run_step5(report, graph, complexity, force_stack=args.stack)

    if args.step5_only:
        heading("DONE (step5-only mode)")
        ok("Step 5 completed. Run without --step5-only to test all steps.")
        return

    # Step 6
    conversion_output, docs_md = await run_step6(report, graph, complexity, stack_assignment, xml)

    if args.through_step6:
        heading("DONE (through-step6 mode)")
        ok("Steps 5-6 completed.")
        return

    # Step 7
    await run_step7(conversion_output)

    # Step 8
    await run_step8(report, conversion_output, docs_md, xml)

    # Summary
    heading("SUMMARY — STEPS 5-8")
    print(f"  Test outputs  : {OUTPUT_DIR}/")
    if _errors:
        fail(f"{len(_errors)} assertion failure(s) — see above")
        sys.exit(1)
    else:
        ok("All Steps 5-8 assertions passed")


if __name__ == "__main__":
    asyncio.run(main())
