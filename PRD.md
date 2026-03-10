# Product Requirements Document
## InformaticaProjectAnalysis

**Version:** 0.1.0
**Author:** ad25343
**Last Updated:** 2026-03-09
**License:** CC BY-NC 4.0 — [github.com/ad25343/InformaticaProjectAnalysis](https://github.com/ad25343/InformaticaProjectAnalysis)
**Contact:** [github.com/ad25343/InformaticaProjectAnalysis/issues](https://github.com/ad25343/InformaticaProjectAnalysis/issues)

---

## 1. Problem Statement

Informatica PowerCenter estates are converted mapping by mapping, in complete
isolation. Each mapping produces a fully self-contained output with no awareness
of other mappings in the estate. This leads to duplicated source definitions,
repeated transformation logic, no shared macros, no dependency awareness, and no
project-level structure in the converted output.

InformaticaProjectAnalysis solves this by reading an entire Informatica estate
before any conversion runs, identifying cross-mapping patterns, grouping
structurally similar mappings into template candidates, building a dependency
graph, and producing a conversion strategy document that tech leads and leadership
review and approve before conversion starts.

The tool observes and surfaces characteristics of the estate. It does not prescribe
target stacks, warehouses, or orchestration platforms — those decisions belong to
the humans reviewing the strategy and the conversion tools they choose.

---

## 2. Target Personas

**Primary: Data Engineering Tech Lead**
Reviews the strategy document in detail. Validates pattern groupings, confirms or
adjusts mapping-to-group assignments, identifies edge cases. Needs structural
evidence, dependency graphs, per-mapping confidence levels, and override controls.

**Secondary: Engineering Leadership**
Reviews the summary layer. Needs mapping count, pattern group count, complexity
distribution, risk flags, estimated conversion scope reduction, and dependency
depth. Does not need per-mapping technical detail.

**Tertiary: Data Migration Engineer**
Consumes the approved strategy as input to their conversion workflow. Needs the
strategy JSON to be correct, complete, and well-structured.

---

## 3. Core Principles

1. **Analyze all N mappings together.** Cross-mapping references (target-to-lookup
   dependencies, shared sources) can only be detected when the full estate is visible.

2. **Pattern grouping is the primary goal.** Structurally similar mappings become one
   template + config instead of N separate files.

3. **Strategy is a recommendation with evidence.** Every grouping shows the member
   mappings, structural evidence, parameter differences, and confidence level.

4. **Converting, not rewriting.** The analysis observes what exists and recommends
   smart conversion. It does not redesign the data architecture.

5. **Variation handling is explicit.** Variation within groups is surfaced
   transparently so humans can confirm or override.

6. **Classification by structural behavior, not naming conventions.** Real-world
   estates do not follow consistent naming. Classification uses transformation
   topology and graph position.

7. **Honest uncertainty.** The strategy distinguishes high-confidence classifications
   from ambiguous ones that need human confirmation.

8. **Security is paramount.** All input is validated, all XML parsing is XXE-hardened,
   no secrets in code or logs, all dependencies audited.

---

## 4. Input — Project Configuration

The primary input is a `*.project.yaml` file that defines the full migration scope.
This is the single source of truth for the analysis.

```yaml
project:
  name: "Project Name"
  version: "1.0"
  owner: "Team Name"

source:
  type: folder              # folder | repo | zip | s3
  location: "/path/to/exports/"

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
  detect_shared_assets: true
  build_dependency_dag: true
  classify_expressions: true

review:
  tech_lead:
    name: "Name"
    email: "email"
  leadership:
    name: "Name"
    email: "email"

output:
  strategy_format: "json"
  output_dir: "/output/"

notifications:
  webhook_url: ""
  events:
    on_analysis_complete: true
    on_strategy_ready: true
    on_review_approved: true
```

Source types: folder (local path), repo (Git URL + branch), zip (uploaded archive),
s3 (bucket path).

---

## 5. Pipeline Architecture

```
*.project.yaml (uploaded via UI, dropped in watcher dir, or POSTed via API)
    │
    ▼
Phase 1   Discovery
          ├── Step 1.1  Source Resolution       [deterministic]
          │             Clone repo / mount folder / extract ZIP / pull S3
          │             Scan using scope globs → list of mapping XMLs
          │
          ├── Step 1.2  Parse All Mappings      [deterministic]
          │             Parse each mapping XML → structural components
          │             Cache results by file content hash (SHA-256)
          │             Aggregate into estate-level graph
          │
          ├── Step 1.3  Build Estate Graph      [deterministic + AI-assisted]
          │             Cross-mapping dependency edges (target → lookup references)
          │             Shared asset detection (tables referenced by 3+ mappings)
          │             Repeated expression detection
          │             AI: interpret custom SQL overrides, classify expression
          │             complexity, infer missing mapplet behavior, detect
          │             implicit dependencies
          │
    │
    ▼
Phase 2   Pattern Grouping                      [AI-assisted]
          ├── Step 2.1  Structural Fingerprinting
          │             Extract spine per mapping (ordered transformation types)
          │             Group by matching spine
          │
          ├── Step 2.2  Variation Classification
          │             Within each spine group, diff parameters
          │             Classify: Tier 1 (parameter only), Tier 2 (minor structural),
          │             Tier 3 (fundamental — does not group)
          │
          ├── Step 2.3  AI Pattern Naming + Confidence
          │             Name groups in human terms
          │             Assign confidence per mapping-to-group assignment
          │             Flag edge cases and ambiguous mappings
          │
    │
    ▼
Phase 3   Strategy Document Generation
          ├── Step 3.1  PDF Generation          [leadership summary + tech lead detail]
          ├── Step 3.2  Excel Generation        [5 sheets: groups, DAG, shared assets,
          │                                      assignments, risk flags]
          ├── Step 3.3  Strategy JSON           [machine-readable output]
          │
    │
    ▼
Phase 4   ◼ Human Gate — Strategy Review
          Tech leads + leadership review in the UI.
          Actions: confirm groupings, override assignments, add notes.
          Decision: APPROVE → Phase 5 | REJECT → re-analysis with notes
    │
    ▼
Phase 5   Strategy Delivery
          Approved strategy available as:
          ├── JSON file download
          ├── PDF + Excel download
          └── API endpoint (GET /api/projects/{id}/strategy.json)
```

---

## 6. Phase 1 — Discovery (Detail)

### 6.1 Source Resolution

The `source` section of the project config determines how mappings are located:

| Type | Resolution |
|---|---|
| `folder` | Scan `location` recursively using `scope` glob patterns |
| `repo` | Clone `location` at `branch`, scan `path` using scope globs |
| `zip` | Extract uploaded archive, scan using scope globs |
| `s3` | Pull objects matching scope globs from `location` bucket path |

All resolved files are auto-detected by content: mapping XML, workflow XML,
parameter file, or unknown. Files that don't match any known type are logged
and skipped.

### 6.2 Mapping Parser

Each mapping XML is parsed to extract its structural components:

```
Per-mapping parse output:
├── transformations[]         Each transformation with type, ports, expressions
│   ├── type                  "Expression", "Lookup", "Aggregator", "Router", etc.
│   ├── ports[]               Input/output ports with datatypes
│   ├── expressions[]         Expression bodies per port
│   └── table_attribs{}       Lookup table name, conditions, etc.
├── connectors[]              Wiring: from_instance → to_instance
├── sources[]                 Source tables with db_type, owner, fields
├── targets[]                 Target tables with db_type, owner, fields
├── parameters[]              $$VAR definitions with defaults
├── mapplet_instances[]       Mapplet references (expanded if definitions available)
└── sql_overrides[]           Custom SQL on Source Qualifiers
```

Results are cached by SHA-256 hash of file content. Unchanged files are not
re-parsed on incremental runs.

### 6.3 Estate Graph Construction

From the per-mapping parse results, the tool builds an estate-level graph:

**Dependency edges** — for each mapping, inspect all Lookup transformations'
target table names. If that table name matches another mapping's target name,
create a dependency edge: the lookup mapping depends on the target mapping.

**Shared assets** — tables that appear as Lookup sources in `min_group_size` or
more mappings across the estate.

**Repeated expressions** — expression bodies that appear verbatim or structurally
equivalent in 4+ mappings.

### 6.4 AI-Assisted Interpretation

AI is called to interpret elements the parser cannot classify deterministically:

- Custom SQL overrides in Source Qualifiers — what do they do, are two overrides
  structurally equivalent?
- Expression complexity — is a 40-line DECODE the same pattern as a 3-line IIF,
  or fundamentally different?
- Missing mapplet definitions — infer purpose from input/output ports and wiring
- Implicit dependencies — references hidden in SQL or stored procedure calls

---

## 7. Phase 2 — Pattern Grouping (Detail)

### 7.1 Structural Fingerprinting

Each mapping's transformation topology is reduced to a canonical spine: the ordered
sequence of transformation types from source to target, derived from the connectors
graph.

Example spines:
- `SQ → EXP → TARGET` (simple dimension load)
- `SQ → LKP → EXP → RTR → UPD → TARGET` (SCD2)
- `SQ(×3) → JNR → LKP(×2) → EXP → RTR → TARGET(×2)` (complex multi-source)

Mappings with matching spines are candidates for the same pattern group.

### 7.2 Variation Tiers

Within a spine group, variation is classified:

**Tier 1 — Parameter variation.** Structurally identical. Only table names, column
names, filter values, connection strings differ. One template + one config file.

**Tier 2 — Minor structural variation.** Core flow is the same, but minor
differences: an extra Expression, a Filter present in some but not all, different
Lookup counts. Template accommodates variation via config flags.

**Tier 3 — Fundamental structural variation.** Different transformation types or
flow shapes within the same spine match. Does not group. Convert individually.

Boundary between Tier 2 and Tier 3: **spine + complexity profile**. Two mappings
match when they share a spine AND their complexity at each step is comparable.

### 7.3 Confidence Levels

Each mapping-to-group assignment carries a confidence level:

| Confidence | Meaning | Action |
|---|---|---|
| HIGH | Spine match + Tier 1 variation + no flags | Auto-confirmed |
| MEDIUM | Spine match + Tier 2 variation or minor flags | Tech lead should confirm |
| LOW | Weak spine match, significant variation, or AI uncertainty | Requires human review |
| UNCLASSIFIED | No spine match, custom SQL, missing definitions | Convert individually |

The `analysis.confidence_threshold` in the project config controls the cutoff
below which mappings are flagged for human review (default: 0.7).

---

## 8. Phase 3 — Strategy Document (Detail)

### 8.1 PDF Report

Two layers in one document:

**Page 1 — Leadership Summary**
- Estate name, analysis date, mapping count
- Pattern groups found (count + names)
- Unique mappings (count + names)
- Scope reduction: "50 mappings → N templates + M unique files"
- Complexity distribution: simple / medium / complex counts
- Dependency depth: number of sequential stages
- Risk flags: count by severity
- Confidence distribution: HIGH / MEDIUM / LOW / UNCLASSIFIED counts

**Remaining pages — Tech Lead Detail**
- Per pattern group: member list, spine diagram, variation table,
  parameter differences, evidence, confidence per member
- Dependency DAG (visual)
- Shared asset catalogue
- Risk flags detail (per mapping)
- Unclassified mappings with reasons

### 8.2 Excel Workbook (5 sheets)

| Sheet | Contents |
|---|---|
| Pattern Groups | Group name, spine, member count, member names, variation notes, confidence summary |
| Dependency Graph | Source mapping, target mapping, edge type, shared table name |
| Shared Assets | Table name, reference type, referenced by (mapping list), recommendation |
| Per-Mapping Assignments | Mapping name, assigned group (or "unique"), confidence, variation tier, flags, override notes |
| Risk Flags | Mapping name, flag type, severity, description, recommendation |

### 8.3 Strategy JSON

Machine-readable output describing the full analysis results.

```json
{
    "strategy_version": 1,
    "estate_name": "FirstBank_Q1_Migration",
    "analysis_job_id": "uuid",
    "analyzed_at": "ISO datetime",

    "summary": {
        "total_mappings": 50,
        "pattern_groups": 8,
        "template_candidates": 36,
        "unique_mappings": 14,
        "scope_reduction_pct": 56
    },

    "pattern_groups": [
        {
            "group_id": "trunc_load_01",
            "group_name": "Truncate & Load",
            "spine": "SQ → EXP → TARGET",
            "members": [
                {
                    "mapping_name": "m_load_customer",
                    "confidence": "HIGH",
                    "variation_tier": 1,
                    "variation_notes": null,
                    "override": null
                }
            ],
            "externalized_params": ["source_table", "target_table", "column_list"],
            "template_hints": "Single config-driven truncate-and-load with optional filter"
        }
    ],

    "unique_mappings": [
        {
            "mapping_name": "m_complex_reconciliation",
            "reason": "Tier 3 — fundamentally different structure, no pattern match",
            "risk_flags": ["CUSTOM_SQL_OVERRIDE", "5_JOINER_TRANSFORMATIONS"]
        }
    ],

    "shared_assets": [
        {
            "table_name": "DIM_CUSTOMER",
            "referenced_by": ["m_fact_daily_txn", "m_fact_loan_origination", "m_agg_monthly"],
            "reference_type": "lookup",
            "recommendation": "shared reference — referenced by 3 mappings"
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
    ],

    "review": {
        "approved_at": "ISO datetime",
        "approved_by": "reviewer_name",
        "overrides": [],
        "notes": ""
    }
}
```

Schema versioned via `strategy_version` field.

---

## 9. Phase 4 — Human Gate

The strategy review is a structured decision gate in the UI.

### 9.1 Review Actions

| Action | Effect |
|---|---|
| Confirm mapping assignment | Locks the mapping's group assignment |
| Move mapping to different group | Simple override — no re-analysis |
| Convert mapping individually | Removes mapping from its group; converts standalone |
| Split group | Triggers lightweight re-validation (Phase 2 re-run on cached Phase 1 data) |
| Merge groups | Triggers lightweight re-validation |
| Add notes | Stored per mapping and per group; carried into strategy JSON |
| APPROVE | Generates final strategy JSON with review metadata |
| REJECT | Returns to analysis with reviewer notes as constraints |

### 9.2 Audit Trail

Every review action is stamped with reviewer name, role, timestamp, and decision.
Stored in the `audit_log` table and included in the strategy JSON.

---

## 10. Phase 5 — Strategy Delivery

The approved strategy is available in three formats:

**JSON** — machine-readable, schema-versioned. Downloaded via the UI or retrieved
via `GET /api/projects/{id}/strategy.json`. Suitable as input to any downstream
conversion tool or workflow.

**PDF** — human-readable strategy document. Leadership summary + tech lead detail.

**Excel** — reviewable tabular data. Five sheets covering all analysis outputs.

All three are generated from the same underlying analysis data and are consistent.

---

## 11. UI Architecture

### 11.1 Three Views

**Dashboard (leadership)**
Estate summary on one screen. Complexity heat map. Scope reduction metric.
Dependency depth. Risk flag distribution. Confidence distribution.
APPROVE / REJECT gate.

**Pattern Groups (tech leads)**
Left panel: group list with member count and confidence indicator.
Right panel: spine visualization, member table with variation tier and confidence,
parameter differences table, evidence section, override controls, notes.

**Dependency Graph (both)**
Interactive DAG. Nodes = mappings, colored by pattern group. Edges = dependencies.
Click node for details. Execution stages highlighted. Critical path shown.
Error propagation paths visible.

### 11.2 Technology

- React frontend
- FastAPI backend (port 8090)
- SSE progress streaming during analysis
- SQLite persistence
- PDF generated server-side (reportlab or weasyprint)
- Excel generated server-side (openpyxl)
- DAG visualization: d3-dag or dagre-d3

---

## 12. API Surface

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/projects` | Upload project config YAML and start analysis |
| `POST` | `/api/projects/folder` | Start analysis from a folder path |
| `GET` | `/api/projects` | List all analysis jobs |
| `GET` | `/api/projects/{id}` | Get analysis job state |
| `GET` | `/api/projects/{id}/stream` | SSE progress stream |
| `GET` | `/api/projects/{id}/strategy.json` | Download strategy JSON |
| `GET` | `/api/projects/{id}/strategy.pdf` | Download strategy PDF |
| `GET` | `/api/projects/{id}/strategy.xlsx` | Download strategy Excel |
| `POST` | `/api/projects/{id}/review` | Submit review decision (APPROVE / REJECT) |
| `POST` | `/api/projects/{id}/override` | Submit mapping override (move, confirm, individualize) |
| `GET` | `/api/projects/{id}/graph` | Get dependency graph data (JSON for frontend rendering) |
| `GET` | `/api/projects/{id}/groups` | Get pattern groups with members |
| `GET` | `/api/projects/{id}/groups/{gid}` | Get single group detail |
| `POST` | `/api/projects/{id}/deliver` | Trigger strategy delivery |
| `GET` | `/api/audit` | Audit trail of all review decisions |
| `GET` | `/api/health` | Liveness + readiness probe |

---

## 13. Data Model

```
AnalysisJob
├── job_id              UUID
├── project_name        From project config
├── project_config      Full YAML content (stored)
├── status              AnalysisStatus enum
├── current_phase       1–5
├── created_at / updated_at
└── state               JSON blob — per-phase artifacts
    ├── source_resolution       Phase 1.1: files found, types detected
    ├── parse_results           Phase 1.2: per-mapping parse output (cached)
    ├── estate_graph            Phase 1.3: aggregated graph, dependency edges, shared assets
    ├── pattern_groups          Phase 2: groups with members, spines, confidence
    ├── strategy_pdf_path       Phase 3: path to generated PDF
    ├── strategy_xlsx_path      Phase 3: path to generated Excel
    ├── strategy_json           Phase 3: strategy JSON
    ├── review                  Phase 4: review decision, overrides, notes
    └── delivery                Phase 5: delivery status

AnalysisStatus
├── PENDING
├── RESOLVING_SOURCE
├── PARSING
├── BUILDING_GRAPH
├── GROUPING
├── GENERATING_STRATEGY
├── AWAITING_REVIEW
├── APPROVED
├── DELIVERING
├── COMPLETE
├── FAILED
└── REJECTED

ReviewRecord
├── reviewer_name
├── reviewer_role
├── review_date
├── decision            APPROVED | REJECTED
├── overrides           List of mapping overrides applied
├── notes               Free-text reviewer notes

MappingOverride
├── mapping_name
├── action              confirm | move | individualize
├── from_group          Original group (if moving)
├── to_group            Target group (if moving)
├── notes               Reviewer rationale

ParseCache
├── file_hash           SHA-256 of XML content
├── parse_output        Cached parse result JSON
├── cached_at           Timestamp
```

---

## 14. Incremental Analysis

Phase 1 caches parse results by file content hash. On re-analysis:

- Only new/changed XMLs are re-parsed
- Phase 2 runs on the full estate (fast — parsing is cached)
- Previous human overrides are preserved unless the underlying XML changed
- Strategy document includes a diff section showing what changed

---

## 15. Security Architecture

Security is infrastructure, not a feature layer. See SECURITY.md for full details.

| Threat | Defence |
|---|---|
| XXE in mapping XMLs | `safe_parse_xml()` — DTD and entity resolution disabled |
| Path traversal in folder scanning | All paths resolved relative to configured root; symlinks rejected |
| Zip Slip / Zip Bomb | Validated extraction with byte and entry count caps |
| Malformed project config | `yaml.safe_load()` + schema validation before processing |
| Secrets in XML | `scan_xml_for_secrets()` checks CONNECTION attrs at parse time |
| SSRF in repo cloning | URL allowlist; no arbitrary redirects |
| Unauthenticated access | Session-cookie middleware on all non-static routes |
| Dependency CVEs | `pip-audit` in CI; dependencies pinned |
| API injection | All HTTP security headers applied (CSP, HSTS, X-Frame-Options) |

---

## 16. Success Metrics

| Metric | Target |
|---|---|
| Estate parsing completion rate | > 99% of mappings parsed successfully |
| Pattern group accuracy (human-confirmed) | > 85% of auto-assigned groupings confirmed without override |
| Dependency DAG completeness | > 90% of actual dependencies detected |
| Shared asset detection rate | > 95% of tables referenced by 3+ mappings identified |
| Strategy generation time (50 mappings) | < 10 minutes |
| Strategy generation time (500 mappings) | < 60 minutes |
| Human review time (median) | < 30 minutes for 50-mapping estate |
| Scope reduction | Typical estate: 40-60% reduction (N mappings → fewer templates + unique files) |
| Incremental re-analysis time | < 2 minutes for 5 new mappings added to 50-mapping estate |
| False positive rate (incorrect groupings) | < 10% |
| Unclassified mapping rate | < 15% of estate |

---

## 17. Technical Constraints

- **Python 3.11+** — asyncio patterns, `X | Y` union syntax
- **SQLite** — sufficient for single-instance deployment; PostgreSQL migration path via SQLAlchemy
- **Claude API required** — Phases 1.3, 2.3, and 3 call the Anthropic API
- **Port 8090** — default; configurable via `PORT` env var
- **No Docker required** — plain Python venv deployment; Dockerfile optional
- **License** — CC BY-NC 4.0; commercial use requires written permission

---

## 18. Version Roadmap

### v0.1.0 — Foundation (current target)

- Project config parser and validation
- Source resolution (folder type only)
- Mapping XML parser (extract transformations, connectors, sources, targets)
- Estate graph construction (dependency edges, shared assets)
- Basic fingerprinting and pattern grouping
- Strategy JSON generation
- FastAPI backend with health endpoint
- SQLite persistence
- Structured logging

### v0.2.0 — Strategy Documents + UI

- PDF report generation (leadership summary + tech lead detail)
- Excel workbook generation (5 sheets)
- React UI: dashboard view, pattern groups view
- SSE progress streaming
- Human review gate (APPROVE / REJECT)
- Override controls (confirm, move, individualize)

### v0.3.0 — Dependency Graph + AI Enhancement

- Dependency DAG visualization in UI
- AI-assisted expression classification
- AI-assisted custom SQL interpretation
- Confidence scoring refinement
- Execution order generation (topological sort)

### v0.4.0 — Watcher + Incremental

- Watcher mode for `*.project.yaml` files
- Incremental analysis (parse caching, diff reporting)
- Override preservation across re-analysis
- Webhook notifications

### v0.5.0 — Extended Source Types + CI/CD

- Git repo source type
- ZIP upload source type
- S3 source type
- CI/CD API (trigger analysis, retrieve strategy as artifact)

### v1.0.0 — Production Ready

- Full test suite (unit + integration + API contract tests)
- GitHub Actions CI pipeline
- Security audit and hardening
- Performance optimization for large estates (500+ mappings)
- Audit trail and compliance reporting

---

## 19. Sample Data

The repository ships a 50-mapping FirstBank test estate for development and testing:

| Tier | Count | Characteristics |
|---|---|---|
| Simple | 15 | Single source, SQ → EXP → Target, dimension/reference loads |
| Medium | 20 | Multi-source, lookups, aggregations, SCD2, fact loads |
| Complex | 15 | 3+ sources, joiners, routers, multiple targets, regulatory/risk |

Located at `sample_data/firstbank/` with a ready-to-use project config at
`firstbank_migration.project.yaml`.

Expected pattern groups from this estate (validation target):
- Simple dimension load (7 mappings, spine: SQ → EXP → TARGET)
- Reference table load (4 mappings, spine: SQ → EXP → TARGET)
- Staging extract (3 mappings, spine: SQ → FIL → TARGET)
- SCD2 dimension (3 mappings, spine: SQ → LKP → EXP → RTR → UPD)
- Fact with single lookup (6-7 mappings, spine: SQ → LKP → EXP)
- Aggregation (3 mappings, spine: SQ → AGG or SQ → JNR → AGG)
- Complex risk/regulatory (4+ mappings, similar multi-source spine)
- Unique/individual: 5-8 mappings too specialized to template
