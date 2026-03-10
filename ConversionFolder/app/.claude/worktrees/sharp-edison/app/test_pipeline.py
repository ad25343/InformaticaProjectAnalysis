"""
Smoke test — runs Steps 0-4 directly against the sample XML files.
No server needed. Just: python3 test_pipeline.py

Usage
-----
  python3 test_pipeline.py              # mapping-only (v1.0 baseline)
  python3 test_pipeline.py --full       # mapping + workflow + params (v1.1 full)
  python3 test_pipeline.py --step0-only # just run Step 0 quickly

Requires ANTHROPIC_API_KEY in .env
"""
import argparse
import asyncio
import json
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv()

from backend.agents import parser_agent, classifier_agent, documentation_agent, verification_agent
from backend.agents.session_parser_agent import parse as step0_parse

SAMPLE_XML      = Path(__file__).parent / "sample_xml" / "sample_mapping.xml"
SAMPLE_WORKFLOW = Path(__file__).parent / "sample_xml" / "sample_workflow.xml"
SAMPLE_PARAMS   = Path(__file__).parent / "sample_xml" / "sample_params.txt"
OUTPUT_DIR      = Path(__file__).parent / "test_output"
OUTPUT_DIR.mkdir(exist_ok=True)

SEP = "─" * 60


def heading(text): print(f"\n{SEP}\n {text}\n{SEP}")
def ok(text):       print(f"  ✅ {text}")
def warn(text):     print(f"  ⚠️  {text}")
def fail(text):     print(f"  ❌ {text}")


# ─────────────────────────────────────────────────────────────────────────────
# STEP 0 — Session & Parameter Parse (v1.1)
# ─────────────────────────────────────────────────────────────────────────────

def run_step0(mode: str) -> object:
    """Run Step 0 and return a SessionParseReport (or None for mapping-only)."""
    heading("STEP 0 — SESSION & PARAMETER PARSE")

    if mode == "mapping_only":
        print("  Mode: mapping-only — Step 0 runs in detection-only mode (no workflow/params)")
        report = step0_parse(
            mapping_xml=SAMPLE_XML.read_text(),
        )
    else:
        print("  Mode: full — Mapping + Workflow XML + Parameter file")
        report = step0_parse(
            mapping_xml=SAMPLE_XML.read_text(),
            workflow_xml=SAMPLE_WORKFLOW.read_text(),
            parameter_file=SAMPLE_PARAMS.read_text(),
        )

    print(f"  parse_status         : {report.parse_status}")
    print(f"  cross_ref.status     : {report.cross_ref.status}")
    print(f"  cross_ref.mapping    : {report.cross_ref.mapping_name}")
    print(f"  cross_ref.session    : {report.cross_ref.session_name}")
    print(f"  cross_ref.ref_mapping: {report.cross_ref.referenced_mapping}")

    if report.cross_ref.issues:
        for issue in report.cross_ref.issues:
            warn(f"Cross-ref issue: {issue}")

    if report.session_config:
        sc = report.session_config
        print(f"  session_name         : {sc.session_name}")
        print(f"  workflow_name        : {sc.workflow_name}")
        print(f"  commit_interval      : {sc.commit_interval}")
        if sc.connections:
            print(f"  connections:")
            for c in sc.connections:
                resolved = f" → {c.connection_name}" if c.connection_name else ""
                print(f"    {c.role}: {c.transformation_name}{resolved}")
        if sc.pre_session_sql:
            print(f"  pre_sql              : {sc.pre_session_sql[:80]}...")

    if report.parameters:
        print(f"  parameters           : {len(report.parameters)} resolved")
        for p in report.parameters:
            print(f"    {p.name} = {p.value}  [{p.scope}]")

    if report.unresolved_variables:
        for v in report.unresolved_variables:
            warn(f"Unresolved variable: {v}")
    else:
        if mode != "mapping_only":
            ok("All $$VARIABLES resolved")

    if report.notes:
        for note in report.notes:
            warn(f"Note: {note}")

    # Assertions
    checks = [
        (report.parse_status != "FAILED",                        "parse_status is not FAILED"),
        (report.cross_ref.status in ("VALID", "WARNINGS"),       "Cross-reference status is VALID or WARNINGS"),
        (report.cross_ref.mapping_name == "m_STG_ORDERS_to_FACT_ORDERS",
                                                                  "Mapping name extracted correctly"),
    ]
    if mode != "mapping_only":
        checks += [
            (report.parse_status == "COMPLETE",                  "parse_status is COMPLETE"),
            (report.session_config is not None,                   "Session config extracted"),
            (report.session_config and
             report.session_config.session_name == "s_STG_ORDERS_to_FACT_ORDERS",
                                                                  "Session name correct"),
            (report.session_config and
             report.session_config.workflow_name == "WF_STG_ORDERS_to_FACT_ORDERS",
                                                                  "Workflow name correct"),
            (len(report.parameters) >= 4,                         "Parameters parsed (at least 4)"),
            (len(report.unresolved_variables) == 0,               "No unresolved variables"),
        ]

    all_passed = True
    for passed, label in checks:
        if passed:
            ok(label)
        else:
            fail(label)
            all_passed = False

    if all_passed:
        ok("Step 0 complete")
    else:
        fail("Step 0 had assertion failures — see above")

    # Save Step 0 report
    out = OUTPUT_DIR / "step0_session_parse_report.json"
    out.write_text(json.dumps(report.model_dump(), indent=2, default=str))
    print(f"  Saved to: {out}")

    return report


# ─────────────────────────────────────────────────────────────────────────────
# STEPS 1-4
# ─────────────────────────────────────────────────────────────────────────────

async def run_steps1_4(session_parse_report=None) -> None:
    xml = SAMPLE_XML.read_text()
    errors = []

    # ── STEP 1 — PARSE ───────────────────────────────────────
    heading("STEP 1 — PARSE")
    report, graph = parser_agent.parse_xml(xml)
    print(f"  Status        : {report.parse_status}")
    print(f"  Mappings      : {report.mapping_names}")
    print(f"  Objects       : {json.dumps(report.objects_found)}")
    print(f"  Flags         : {len(report.flags)}")

    if report.parse_status == "FAILED":
        fail("Parse failed — stopping")
        return
    ok("Parse complete")

    # ── STEP 2 — CLASSIFY ────────────────────────────────────
    heading("STEP 2 — CLASSIFY")
    complexity = classifier_agent.classify(report, graph)
    print(f"  Tier          : {complexity.tier.value}")
    print(f"  Criteria      : {'; '.join(complexity.criteria_matched)}")
    if complexity.special_flags:
        warn(f"Special flags: {complexity.special_flags}")
    ok("Classification complete")

    # ── STEP 3 — DOCUMENT ────────────────────────────────────
    heading("STEP 3 — DOCUMENT  (calling Claude...)")
    try:
        docs = await documentation_agent.document(
            report, complexity, graph,
            session_parse_report=session_parse_report,
        )
        doc_path = OUTPUT_DIR / "documentation.md"
        doc_path.write_text(docs)
        print(f"  Length        : {len(docs):,} chars")
        print(f"  Session ctx   : {'injected' if session_parse_report and session_parse_report.session_config else 'not available (mapping-only)'}")
        print(f"  Saved to      : {doc_path}")

        mapping_name = report.mapping_names[0]
        checks = [
            (mapping_name in docs,              f"Mapping name '{mapping_name}' present"),
            ("EXP_BUSINESS_RULES" in docs,      "Expression transformation documented"),
            ("FIL_VALID_ORDERS" in docs,        "Filter transformation documented"),
            ("SQ_STG_ORDERS" in docs,           "Source Qualifier documented"),
            ("Field-Level Lineage" in docs or
             "lineage" in docs.lower(),         "Lineage section present"),
            ("ORDER_AMOUNT * 0.085" in docs or
             "0.085" in docs,                   "Tax expression captured"),
        ]
        # v1.1 checks when session context is available
        if session_parse_report and session_parse_report.session_config:
            checks += [
                ("WF_STG_ORDERS_to_FACT_ORDERS" in docs or
                 "s_STG_ORDERS_to_FACT_ORDERS" in docs,  "Session/workflow name in documentation"),
                ("Oracle_SALES_DB_DEV" in docs or
                 "Oracle_DWH_DEV" in docs,                "Connection names in documentation"),
            ]
        for passed, label in checks:
            ok(label) if passed else warn(f"Missing: {label}")

    except Exception as e:
        fail(f"Documentation failed: {e}")
        errors.append(str(e))
        docs = "Documentation failed"

    # ── STEP 4 — VERIFY ──────────────────────────────────────
    heading("STEP 4 — VERIFY  (calling Claude...)")
    try:
        verification = await verification_agent.verify(
            report, complexity, docs, graph,
            session_parse_report=session_parse_report,
        )
        ver_path = OUTPUT_DIR / "verification_report.json"
        ver_path.write_text(json.dumps(verification.model_dump(), indent=2))

        print(f"  Overall       : {verification.overall_status}")
        print(f"  Checks        : {verification.total_passed}/{verification.total_checks} passed")
        print(f"  Flags         : {verification.total_flags}")
        print(f"  Blocked       : {verification.conversion_blocked}")
        print(f"  Saved to      : {ver_path}")

        if verification.flags:
            print("\n  Flags raised:")
            for f in verification.flags:
                marker = "🚫" if f.blocking else "⚠️ "
                print(f"    {marker} [{f.flag_type}] {f.location}")
                print(f"       {f.description[:100]}...")

        if not verification.conversion_blocked:
            ok("Verification passed — APPROVED FOR CONVERSION")
        else:
            warn(f"Conversion blocked: {verification.blocked_reasons}")

    except Exception as e:
        fail(f"Verification failed: {e}")
        errors.append(str(e))

    # ── SUMMARY ──────────────────────────────────────────────
    heading("SUMMARY")
    print(f"  Test outputs  : {OUTPUT_DIR}/")
    if errors:
        fail(f"{len(errors)} error(s) — check output above")
        sys.exit(1)
    else:
        ok("All steps completed — pipeline is working")
        print("\n  Next: start the server and try the full UI flow")
        print("  $ bash start.sh\n")


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

async def main():
    parser = argparse.ArgumentParser(
        description="Smoke test for the Informatica Conversion pipeline"
    )
    parser.add_argument(
        "--full",
        action="store_true",
        help="Run Step 0 with all three sample files (mapping + workflow + params)",
    )
    parser.add_argument(
        "--step0-only",
        action="store_true",
        dest="step0_only",
        help="Run only Step 0 (fast, no Claude API calls needed)",
    )
    args = parser.parse_args()

    mode = "full" if args.full else "mapping_only"

    # Always run Step 0
    session_report = run_step0(mode)

    if args.step0_only:
        heading("DONE (step0-only mode)")
        ok("Step 0 completed. Run without --step0-only to test all steps.")
        return

    # Run Steps 1-4, passing session context when available
    await run_steps1_4(
        session_parse_report=session_report if args.full else None
    )


if __name__ == "__main__":
    asyncio.run(main())
