# InformaticaProjectAnalysis — Design Document

**Status:** Ideation
**Parent project:** InformaticaConversion (v2.15.0)
**Author:** ad25343
**Created:** 2026-03-09

---

## 1. Problem Statement

The InformaticaConversion tool (v2.15.0) converts Informatica PowerCenter mappings
one at a time, in complete isolation. Each mapping produces a fully self-contained
output with no awareness of other mappings in the estate.

This means:
- If 10 mappings all do truncate-and-load with only the table name differing, the tool
  produces 10 separate files instead of one parameterized template + config.
- If 8 mappings share the same SCD2 pattern, each gets its own copy of the SCD2 logic.
- Shared source tables are redefined independently in every output.
- There is no dependency graph — the tool cannot tell you which mappings must run before others.
- There is no project-level structure — no unified `sources.yml`, no shared macros, no
  layered model organization.

InformaticaProjectAnalysis solves this by analyzing an entire Informatica estate
**before** any conversion runs, identifying cross-mapping patterns, and producing a
conversion strategy that a human reviews before conversion starts.

---

## 2. Core Principles

1. **Analyze all N mappings together**, not in partitions. Cross-mapping references
   (target-to-lookup dependencies, shared sources) can only be detected when the full
   estate is visible.

2. **Pattern grouping is the primary goal.** The analysis determines which mappings share
   enough structural similarity that one template + config replaces N separate files.
   The estate of 50 mappings might collapse into 8 templates + configs and 12 unique files
   instead of 50 independent scripts.

3. **The strategy is a recommendation with evidence.** For every pattern group, the
   document shows: the member mappings, the structural evidence (shared fingerprint),
   the parameter differences (what varies), the recommended template approach, and a
   confidence level. Tech leads validate the groupings before conversion runs.

4. **We are converting, not rewriting.** The analysis observes what exists and recommends
   smart conversion — collapsing identical patterns into templates. It does not redesign
   the data architecture or suggest how the estate "should have been built."

5. **Variation handling is explicit.** Mappings are grouped by structural similarity, and
   variation within groups is surfaced transparently so humans can confirm or override.

---

## 3. Target Personas

**Primary: Data Engineering Tech Lead**
Reviews the strategy document in detail. Validates pattern groupings, confirms or
adjusts mapping-to-group assignments, identifies edge cases. Needs structural evidence,
dependency graphs, and per-mapping confidence levels.

**Secondary: Engineering Leadership**
Reviews the summary layer. Needs mapping count, pattern group count, complexity
distribution, risk flags, and estimated conversion scope reduction. Does not need
per-mapping technical detail.

---

## 4. Workflow

```
Input: N mapping XMLs + optional workflow XMLs + parameter files
(same file types the conversion tool already accepts)
    │
    ▼
Phase 1 — Discovery (deterministic parsing + AI-assisted interpretation)
    Parse all N mappings.
    Extract structural fingerprints.
    Build cross-mapping dependency graph.
    AI interprets: custom SQL overrides, expression logic classification,
    mapplet black boxes, implicit dependencies.
    │
    ▼
Phase 2 — Pattern Grouping (AI-assisted)
    Cluster mappings by structural fingerprint (spine + complexity profile).
    Within each cluster, diff parameters to confirm same-pattern-different-config.
    Name patterns in human terms.
    Identify which parameters externalize to config.
    Flag edge cases with confidence levels.
    │
    ▼
Phase 3 — Strategy Document Generation
    Produce PDF (leadership summary + tech lead detail) and Excel workbook
    (pattern groups, dependency graph, shared assets, per-mapping assignments).
    │
    ▼
Phase 4 — Human Gate
    Tech leads + leadership review the strategy in the UI.
    Confirm, adjust, or override groupings.
    │
    ▼
Phase 5 — Handoff to InformaticaConversion
    Approved strategy feeds into the conversion pipeline.
    Pattern groups → template + config conversion.
    Unique mappings → individual conversion.
    Conversion agent knows about shared assets and emits proper references.
```

---

## 5. Phase 1 — Discovery

### 5.1 Deterministic Parsing (no AI)

For each of the N mappings, the existing InformaticaConversion parser extracts:
- Transformation types and their order (the "spine")
- Source tables and their connection attributes (DBDNAME)
- Target tables and their connection attributes
- Lookup transformation targets (which tables are used as lookups)
- Connector edges (which transformation connects to which)
- Expression transformation bodies
- Mapplet instances (expanded if definitions present, flagged if not)
- SQL overrides on Source Qualifiers
- Parameter variables ($$VARs)

### 5.2 Cross-Mapping Graph (no AI)

From the per-mapping parse results, build the estate-level graph:
- **Nodes:** every source table, target table, and mapping
- **Edges:** mapping A writes to TABLE_X, mapping B has a Lookup against TABLE_X → B depends on A
- **Shared assets:** tables referenced as Lookup sources by 3+ mappings
- **Repeated expressions:** expression fragments appearing verbatim in 4+ mappings

### 5.3 AI-Assisted Interpretation

The parser extracts the facts; AI fills in the gaps:

- **Custom SQL in Source Qualifiers:** Understanding what hand-written SQL overrides do —
  what tables they join, what filters apply, whether two SQL overrides are structurally
  equivalent despite different table/column names.
- **Expression logic classification:** Determining whether two Expression transformations
  implement the same pattern with different parameters vs. fundamentally different logic.
  `IIF(ISNULL(IN_STATUS), 'UNKNOWN', IN_STATUS)` vs.
  `IIF(ISNULL(IN_CATEGORY), 'DEFAULT', IN_CATEGORY)` = same pattern.
  A 40-line DECODE cascade = different.
- **Mapplet inference:** When a mapplet definition is missing, infer its likely purpose
  from input/output ports and wiring context.
- **Implicit dependencies:** Dependencies hidden in SQL overrides or stored procedure calls
  that don't appear in the structured XML.

---

## 6. Phase 2 — Pattern Grouping

### 6.1 Structural Fingerprinting

Each mapping's transformation topology is reduced to a canonical signature — the "spine":
the ordered sequence of transformation types from source to target, ignoring branches.

Example: `SQ → EXP → LKP → LKP → TARGET`

Two mappings with the same spine are candidates for the same pattern group.

### 6.2 Variation Tiers

Within a fingerprint group, variation is classified into three tiers:

**Tier 1 — Parameter variation.** Structurally identical. Only table names, column names,
filter values, connection strings differ. One template, one config file. No question,
these group together.

**Tier 2 — Minor structural variation.** Core flow is the same, but one mapping has an
extra Expression (e.g., adds ETL_LOAD_DATE), or one has a Filter that another doesn't,
or one has 3 Lookups vs. 5. The template accommodates variation via config flags
(e.g., `add_etl_metadata: true`).

**Tier 3 — Fundamental structural variation.** Different transformation types, different
flow shapes. These don't group. Convert individually.

The boundary between Tier 2 and Tier 3 is determined by **spine + complexity profile.**
Two mappings match when they have the same spine AND their complexity at each step is
in the same ballpark.

### 6.3 Per-Group Evidence

For each pattern group, the strategy document shows:

```
Pattern Group: Truncate & Load (14 mappings)
Core spine: SQ → EXP → TARGET
Variations found:
  - 11 mappings: exact match, differ only by table/columns
  - 2 mappings: extra Filter before TARGET
    (minor — recommend config flag `has_filter: true`)
  - 1 mapping: Expression contains 25-line business logic unlike the others
    (flag for tech lead — does this belong in this group or convert individually?)
Confidence: HIGH (11), MEDIUM (2), LOW (1)
```

### 6.4 Classification by Structural Behavior (not naming conventions)

Real-world Informatica estates do not follow consistent naming conventions. Tables may
be called `ACCT_LOAD`, `PROCESS_TRANSACTIONS`, `RPT_SUMMARY`, or `TBL_047_PROC`.

Classification is based on transformation topology and graph position, not names:

| Signal | What it tells you |
|---|---|
| `DBDNAME` on SOURCE vs TARGET | Which tables are OLTP vs warehouse |
| Lookup `TABLE` pointing at a table | That table is a shared reference/dimension |
| Self-lookup (LKP points at own target) | SCD2 dimension — certain |
| Aggregator transformation present | Aggregate/summary table |
| Number of Lookup transformations | Fan-out of dimension joins → likely fact table |
| Router + Update Strategy together | SCD2 or conditional load — dimension pattern |
| Union transformation | Multi-source merge — consolidation table |
| Lookup in-degree across estate | Shared dimension vs one-off lookup |
| Mapping produces 2+ targets | Router/split output — affects project structure |

Naming conventions are one optional hint that gets folded in if present, not relied upon.

---

## 7. Phase 3 — Strategy Document

### 7.1 Format

Two deliverables:

**PDF** — human-readable strategy document with two layers:
- Leadership summary (page 1): mapping count, pattern group count, unique mapping count,
  complexity distribution, risk flags, estimated scope reduction
- Tech lead detail (remaining pages): per-group evidence, dependency DAG visualization,
  shared asset catalogue, per-mapping assignments with confidence levels

**Excel workbook** — machine-readable / reviewable data:
- Sheet 1: Pattern Groups (group name, member mappings, spine, variation notes, confidence)
- Sheet 2: Dependency Graph (source mapping, target mapping, edge type, shared table)
- Sheet 3: Shared Assets (table/expression, referenced by which mappings, recommendation)
- Sheet 4: Per-Mapping Assignments (mapping name, assigned group, confidence, flags, notes)
- Sheet 5: Risk Flags (mapping name, flag type, severity, description)

### 7.2 Honest Uncertainty

The strategy document includes an explicit uncertainty section:
- Mappings classified with HIGH confidence
- Mappings classified with MEDIUM confidence (tech lead should confirm)
- Mappings classified with LOW confidence or unclassifiable (needs human review)
- Patterns the analysis couldn't read (custom SQL too complex, missing mapplet definitions, etc.)

The output is: "here are the 38 mappings I can classify with high confidence, here are 8
that need a human to confirm, and here are 4 I couldn't read at all."

---

## 8. Phase 4 — Human Gate

The strategy is reviewed in a standalone web UI (same architectural pattern as
InformaticaConversion). Tech leads can:
- View the full strategy document (PDF rendering in UI)
- Browse pattern groups and their member mappings
- View the dependency graph
- Confirm or override individual mapping-to-group assignments
- Add notes per mapping or per group
- Approve the strategy (triggers handoff to conversion) or reject (request re-analysis)

---

## 9. Phase 5 — Handoff to InformaticaConversion

The approved strategy integrates with the existing v2.15.0 conversion pipeline:

- Pattern groups trigger **template + config** conversion mode (new)
- Unique mappings trigger **individual** conversion mode (existing)
- Shared asset catalogue is passed as context so the conversion agent emits proper
  `ref()` / `source()` calls instead of inline duplication
- Dependency DAG drives execution order for batch conversion
- Per-mapping hints from the strategy (flags, notes, overrides) are injected into
  each mapping's conversion prompt

This is an integration point between InformaticaProjectAnalysis and
InformaticaConversion — the two tools share a defined interface but remain
separate codebases.

---

## 10. Standalone Tool Architecture

InformaticaProjectAnalysis is a standalone application with its own:
- Web UI (for uploading project configs, viewing strategy, human review gate)
- REST API (for programmatic access and integration with InformaticaConversion)
- Database (job tracking, strategy persistence, review decisions)
- PDF + Excel generation pipeline

It follows the same architectural patterns as InformaticaConversion:
- FastAPI backend (port 8090)
- SSE progress streaming
- SQLite persistence
- Human review gate with structured decisions
- Audit trail

### 10.1 Project Configuration File

The primary input is a `*.project.yaml` file that defines the full migration scope.
This is the single source of truth for the analysis — not individual XML uploads.

```yaml
project:
  name: "FirstBank DWH Migration"
  version: "1.0"
  owner: "Data Engineering"

source:
  type: folder                    # folder | repo | zip | s3
  location: "/path/to/informatica/exports/"

scope:
  mappings:
    include: ["mappings/**/*.xml"]
    exclude: ["mappings/archive/**"]
  workflows:
    include: ["workflows/**/*.xml"]
  parameters:
    include: ["parameter_files/*.xml"]
  default_parameter_env: "dev"

analysis:
  fingerprint_strictness: "moderate"
  min_group_size: 2
  confidence_threshold: 0.7

review:
  tech_lead:
    name: "Jane Smith"
    email: "jane.smith@firstbank.com"
  leadership:
    name: "Mike Johnson"
    email: "mike.johnson@firstbank.com"

conversion:
  api_endpoint: "http://localhost:8090"
  batch_concurrency: 3
  output_dir: "/output/firstbank/"

notifications:
  webhook_url: ""
  events:
    on_analysis_complete: true
    on_strategy_ready: true
    on_review_approved: true
    on_conversion_complete: true
```

Source types supported:
- **folder** — local path; tool scans recursively using scope globs
- **repo** — Git URL + branch + subfolder path; tool clones and scans
- **zip** — uploaded ZIP archive; tool extracts and scans
- **s3** — S3 bucket path; tool pulls and scans

### 10.2 Three Operating Modes

**Interactive** — user uploads the project config through the UI or provides a
folder/repo path. Watches the analysis run in real time. Reviews the strategy
in the browser. Approves or overrides.

**Watcher** — tool polls a directory for `*.project.yaml` files. When one appears
or changes, it triggers analysis automatically. Same pattern as the v2.14/v2.15
manifest watcher and scheduler in InformaticaConversion.

**CI/CD** — a pipeline step drops the project config and triggers analysis via
API. Strategy document posted as a PR artifact or comment. Review happens in
the PR workflow or the UI.

### 10.3 Sample Project Config

A working sample config is provided at:
`sample_data/firstbank/firstbank_migration.project.yaml`

This config points at the 50-mapping FirstBank test estate and is ready to use
for development and testing of the analysis pipeline.

---

## 11. Integration Contract (from codebase analysis)

This section describes the concrete integration points based on the actual
InformaticaConversion v2.15.0 codebase.

### 11.1 Parser Reuse

The existing `parser_agent.parse_xml()` is directly reusable. It returns:

```python
(ParseReport, graph_dict)

# graph_dict structure:
{
    "mappings": [
        {
            "name": str,
            "description": str,
            "transformations": [
                {
                    "name": str,
                    "type": str,           # "Expression", "Lookup", "Aggregator", etc.
                    "reusable": bool,
                    "ports": [{"name", "datatype", "porttype", "expression", "default"}],
                    "expressions": [{"port", "expression"}],
                    "table_attribs": {"Lookup Table Name": str, "Lookup condition": str, ...}
                }
            ],
            "connectors": [
                {"from_instance", "from_field", "to_instance", "to_field"}
            ],
            "parameters": [{"name", "datatype", "default"}],
            "instance_map": {"instance_name": "transformation_name"},
            "mapplet_expansions": [str]
        }
    ],
    "sources": [{"name", "db_type", "owner", "fields": [{"name", "datatype", "length"}]}],
    "targets": [{"name", "db_type", "owner", "fields": [{"name", "datatype", "length"}]}],
    "workflows": [{"name", "tasks": [{"name", "type"}]}],
    "parameters": [{"name", "value"}],
    "connections": [],
    "mapplets": [{"name", "source"}]
}
```

InformaticaProjectAnalysis calls `parse_xml()` once per mapping XML and aggregates
all N results into the estate-level graph. No changes to the parser are needed.

### 11.2 Key Fields for Pattern Grouping

From each mapping's graph entry, the fingerprinting engine uses:

- `transformations[].type` — the spine (ordered sequence of transformation types)
- `transformations[].table_attribs` — especially `"Lookup Table Name"` for dependency detection
- `transformations[].expressions` — expression bodies for pattern matching
- `connectors` — the wiring topology (from_instance → to_instance edges)
- `instance_map` — maps instance names to transformation names (needed for mapplet detection)

From the estate-level `sources[]` and `targets[]`:
- `name` + `db_type` + `owner` — identify shared tables across mappings
- Cross-reference target names with Lookup `table_attribs["Lookup Table Name"]` for dependency edges

### 11.3 Conversion Agent Integration

The conversion agent (`conversion_agent.py`) uses a prompt template with injection points:

```python
CONVERSION_PROMPT = """Convert the Informatica mapping documented below to {stack}.

{security_context}
## Stack Assignment Rationale
{rationale}
{approved_fixes_section}{flag_handling_section}{manifest_override_section}
## Full Mapping Documentation (your source of truth)
{documentation_md}
...
"""
```

The strategy handoff adds a new injection section: `{project_strategy_section}` containing:

- Which pattern group this mapping belongs to (or "unique/individual")
- For pattern groups: the template specification, externalized parameters, config structure
- Shared asset references: "use `ref('dim_customer')` not inline lookup definition"
- Dependency context: "this mapping depends on dim_customer and dim_account being loaded first"
- Any tech lead overrides from the human review gate

### 11.4 Strategy Handoff Format

The approved strategy is a JSON file that InformaticaConversion consumes:

```json
{
    "strategy_version": 1,
    "estate_name": "FirstBank_Q1_Migration",
    "analysis_job_id": "uuid",
    "approved_at": "ISO datetime",
    "approved_by": "reviewer_name",

    "pattern_groups": [
        {
            "group_id": "trunc_load_01",
            "group_name": "Truncate & Load",
            "spine": "SQ → EXP → TARGET",
            "conversion_mode": "template",
            "members": [
                {
                    "mapping_name": "m_load_customer",
                    "confidence": "HIGH",
                    "variation_tier": 1,
                    "variation_notes": null,
                    "override": null
                },
                {
                    "mapping_name": "m_load_account",
                    "confidence": "MEDIUM",
                    "variation_tier": 2,
                    "variation_notes": "Extra Filter before target",
                    "override": "confirmed by tech lead"
                }
            ],
            "externalized_params": ["source_table", "target_table", "column_list", "filter_condition"],
            "template_hints": "Single config-driven truncate-and-load with optional filter"
        }
    ],

    "unique_mappings": [
        {
            "mapping_name": "m_complex_reconciliation",
            "conversion_mode": "individual",
            "reason": "Tier 3 — fundamentally different structure, no pattern match",
            "risk_flags": ["CUSTOM_SQL_OVERRIDE", "5_JOINER_TRANSFORMATIONS"]
        }
    ],

    "shared_assets": [
        {
            "table_name": "DIM_CUSTOMER",
            "referenced_by": ["m_fact_daily_txn", "m_fact_loan_origination", "m_agg_monthly"],
            "reference_type": "lookup",
            "recommendation": "shared model — use ref('dim_customer')"
        }
    ],

    "dependency_dag": [
        {"from": "m_stg_customer", "to": "m_dim_customer", "via": "STG_CUSTOMER"},
        {"from": "m_dim_customer", "to": "m_fact_daily_txn", "via": "DIM_CUSTOMER"}
    ],

    "execution_order": [
        ["m_stg_customer", "m_stg_account", "m_stg_transactions"],
        ["m_dim_customer", "m_dim_account"],
        ["m_fact_daily_txn", "m_fact_loan_origination"],
        ["m_agg_monthly_summary"]
    ]
}
```

### 11.5 Two Conversion Modes

**Template mode** (new — for pattern groups): The conversion agent receives the group's
template specification and all member mappings' parameter values. It produces one
parameterized template file + one config file (YAML/JSON) with N entries. The template
uses variables or config lookups where the members differ.

**Individual mode** (existing): The conversion agent operates exactly as it does today
in v2.15.0 — one mapping in, one standalone output.

The `conversion_mode` field in the strategy JSON determines which mode is used per mapping.

---

## 12. Separation of Concerns — Analysis vs. Conversion

The analysis tool **observes and surfaces**. The conversion tool **decides and produces**.

The analysis tool does NOT prescribe target stacks, warehouses, or orchestration
platforms. Those decisions belong to the conversion tool's stack assignment logic
(Step 6) and the humans reviewing the strategy.

What the analysis tool DOES surface — characteristics that inform downstream decisions:

### 12.1 Structural Characteristics (per mapping and per group)

- Dependency depth: "4-layer chain — staging → dimensions → facts → aggregations"
- Cross-mapping dependencies: "12 mappings have Lookup references to other mappings' targets"
- Parallelism potential: "Stage 2 has 3 independent tracks that can run concurrently"
- Complexity distribution: "15 simple, 20 medium, 15 complex"

### 12.2 Transformation Characteristics (relevant to stack decisions)

- "Pattern Group 4 (SCD2) requires merge/upsert or snapshot semantics"
- "Pattern Group 7 (risk/regulatory) involves 3-source joins with complex expressions"
- "7 simple dimension loads are pure SQL — no transformation framework overhead needed"
- "1 mapping uses Union transformation across 3 heterogeneous sources"
- "2 mappings contain custom SQL overrides that bypass the transformation layer"

### 12.3 Orchestration Characteristics (relevant to execution planning)

- The dependency DAG itself — which mappings must run before others
- Stage boundaries — where parallelism is safe vs. where serialization is required
- Error propagation paths — if mapping A fails, which downstream mappings are affected
- Volume indicators — source table sizes where available from metadata

### 12.4 Risk Characteristics (relevant to review and planning)

- Unmapped expressions, missing mapplet definitions, custom SQL
- Confidence distribution across groupings
- Mappings that resist classification
- Patterns the tool couldn't interpret

The strategy document presents all of this as evidence for humans and the conversion
tool to act on — never as prescriptive decisions about technology choices.

### 12.5 Current Target Stacks (InformaticaConversion v2.15.0)

The conversion tool currently supports: dbt, PySpark, Python (Pandas).

The analysis tool is agnostic to this list. If the conversion tool adds support for
additional targets in the future (raw SQL, Spark SQL, stored procedures, Airflow DAG
generation, etc.), the analysis tool's output remains valid — it surfaces structural
characteristics, not stack-specific recommendations.

---

## 13. Design Decisions (resolved)

### 13.1 Template + Config Conversion Mode

No new conversion architecture needed. The existing conversion agent receives a richer
prompt via the `{project_strategy_section}` injection point. For pattern groups, the
prompt includes all member mappings together with the shared spine highlighted and the
parameter differences called out. The agent produces two outputs:

- **Template file** — parameterized code with `CONFIG["source_table"]`,
  `CONFIG["target_table"]`, etc.
- **Config file** — YAML with one entry per member mapping

Stack-specific implementation:
- dbt: one `.sql` model with `var()` references + `dbt_project.yml` setting variables per model
- PySpark: parameterized Python module + config dict loaded from YAML
- Python/Pandas: same pattern as PySpark

### 13.2 Strategy-to-Conversion Interface

The strategy JSON (schema in Section 11.4) is the contract. Delivered two ways:

- **File**: written to `{output_dir}/{project_name}_strategy.json` on approval.
  The conversion tool's watcher can pick it up for automated handoff.
- **API**: POST to the conversion tool's batch endpoint with the strategy attached.
  Used for interactive and CI/CD modes.

Schema versioned via `strategy_version` field so both tools can evolve independently.

### 13.3 Re-Analysis on Overrides

Two tiers of overrides:

- **Simple overrides** (move a mapping between groups, confirm/reject an assignment,
  add notes): applied directly to the strategy JSON. No re-analysis. The structural
  evidence doesn't change — only the human's decision about where a mapping belongs.

- **Structural changes** (split a group, merge groups, regroup): trigger a lightweight
  re-validation pass. Re-runs Phase 2 (pattern grouping) against the existing Phase 1
  output with the overrides as constraints. No full re-parse. Validates that the new
  grouping holds structurally and updates variation tiers and confidence levels.

### 13.4 Incremental Updates

Phase 1 caches parse results keyed by file content hash (SHA-256). When new mappings
are added or existing ones change:

- Only new/changed XMLs are re-parsed (cache hit for unchanged files)
- Phase 2 (pattern grouping) always runs on the full estate — fast because parsing
  is cached
- Phase 3 (strategy generation) runs on the full estate

The strategy document includes a diff section: "5 new mappings added since last
analysis — here's what changed in the groupings and dependency graph."

Previous human decisions (overrides, confirmations) are preserved unless the
structural change invalidates them. If a confirmed mapping's XML changed, the
confirmation is cleared and the mapping is re-evaluated.

### 13.5 UI Design

Three views, same underlying data:

**Dashboard view (leadership)**
Estate summary on one page: total mappings, pattern groups found, unique mappings,
complexity distribution (heat map), dependency depth, estimated scope reduction
("50 mappings → 8 templates + 12 unique files"). Printable. The approve/reject
gate lives here — leadership makes the call after tech leads have reviewed the detail.

**Pattern groups view (tech leads)**
Left panel: list of all pattern groups with member count and confidence indicator.
Right panel (on group select):
- Spine visualization (transformation flow diagram)
- Member mappings table with variation tier, confidence, and flags per mapping
- Parameter differences table (what varies across members)
- Evidence section (why these were grouped — structural fingerprint match details)
- Per-mapping override controls: "confirm", "move to group...", "convert individually"
- Notes field per mapping and per group

**Dependency graph view (both audiences)**
Interactive DAG visualization. Nodes are mappings, colored by pattern group.
Edges are dependencies (target → lookup references). Click a node for details.
Execution stages highlighted. Critical path shown. Error propagation paths visible
(if mapping A fails, which downstream mappings are affected).

All three views are React components. PDF and Excel exports are generated from
the same data model that powers the UI.

---

## 14. Version Target

This capability would be positioned as part of the v3.0 vision referenced in the
InformaticaConversion PRD, which mentions "Observability: track conversion success rate,
time-to-review, and flag frequency across the entire Informatica estate."

InformaticaProjectAnalysis is the foundation for that estate-level visibility.
