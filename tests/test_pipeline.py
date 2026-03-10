"""
Integration test — runs the full analysis pipeline against the
FirstBank 50-mapping sample project.
"""

import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from app.backend.orchestrator import AnalysisOrchestrator


def main():
    config_path = Path(__file__).parent.parent / (
        "ConversionFolder/sample_data/firstbank/"
        "firstbank_migration.project.yaml"
    )

    print(f"Config: {config_path}")
    print(f"Exists: {config_path.exists()}")
    print()

    # Create and run analysis
    orch = AnalysisOrchestrator()
    job = orch.create_job_from_yaml(config_path)

    # Override source location to local path (config has user's absolute path)
    local_data = config_path.parent
    job.project_config.source.location = str(local_data)
    print(f"Job ID: {job.job_id}")
    print(f"Project: {job.project_name}")
    print()

    orch.run_analysis(job.job_id)

    # Results
    print("=" * 70)
    print("ANALYSIS RESULTS")
    print("=" * 70)
    print()
    print(f"Status: {job.status.value}")
    print(f"Files resolved: {len(job.resolved_files)}")
    print(f"Mappings parsed: {len(job.parse_results)}")
    print()

    # Project graph
    if job.project_graph:
        eg = job.project_graph
        print(f"Project graph:")
        print(f"  Sources: {len(eg.all_sources)}")
        print(f"  Targets: {len(eg.all_targets)}")
        print(f"  Dependency edges: {len(eg.dependency_edges)}")
        print(f"  Shared assets: {len(eg.shared_assets)}")
        for sa in eg.shared_assets:
            print(f"    {sa.table_name} — referenced by {len(sa.referenced_by)} mappings")
        print()

    # Spines
    print("Spine signatures:")
    sig_counts: dict[str, int] = {}
    for spine in job.spines:
        sig_counts[spine.spine_signature] = sig_counts.get(spine.spine_signature, 0) + 1
    for sig, count in sorted(sig_counts.items(), key=lambda x: -x[1]):
        print(f"  {sig}: {count} mappings")
    print()

    # Pattern groups
    print(f"Pattern groups: {len(job.pattern_groups)}")
    for g in job.pattern_groups:
        print(f"  {g.group_name}")
        print(f"    Spine: {g.spine_signature}")
        print(f"    Members: {g.member_count}")
        for m in g.members:
            print(f"      {m.mapping_name} — {m.confidence.value} (Tier {m.variation_tier.value})")
        print(f"    Externalized: {', '.join(g.externalized_params) if g.externalized_params else 'none'}")
        print()

    # Unique mappings
    print(f"Unique mappings: {len(job.unique_mappings)}")
    for u in job.unique_mappings:
        print(f"  {u.mapping_name}: {u.reason}")
    print()

    # Strategy summary
    if job.strategy:
        s = job.strategy.summary
        print("Strategy summary:")
        print(f"  Total mappings: {s.total_mappings}")
        print(f"  Pattern groups: {s.pattern_groups}")
        print(f"  Template candidates: {s.template_candidates}")
        print(f"  Unique mappings: {s.unique_mappings}")
        print(f"  Scope reduction: {s.scope_reduction_pct}%")
        print()

        # Execution order
        print("Execution order:")
        for i, stage in enumerate(job.strategy.execution_order):
            print(f"  Stage {i}: {', '.join(stage)}")
        print()

    print("DONE")


if __name__ == "__main__":
    main()
