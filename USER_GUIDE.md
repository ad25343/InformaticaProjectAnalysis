# InformaticaProjectAnalysis — User Guide

**Version:** 0.1.0
**Last Updated:** 2026-03-09

---

## What Is This Tool For?

If your organization uses **Informatica PowerCenter** for data integration and you are
planning to migrate that logic to **open code** — Python scripts, dbt models, PySpark
jobs, or any modern framework — this tool is the first step.

The migration landscape has shifted. Teams used to move from one SaaS platform to another
(Informatica Cloud, Talend, Matillion). Now they want to land in code they own,
version-control, and deploy without platform lock-in. That changes the conversion problem:
you need to produce well-structured, maintainable source code, not just functionally
equivalent scripts inside another tool.

Informatica PowerCenter stores its data transformation logic in units called **mappings**.
Each mapping defines a data flow: source tables → transformations (lookups, expressions,
filters, aggregations) → target tables. A typical project has dozens to hundreds of
mappings. All of this can be **exported as XML files** — and those XML exports are what
this tool reads.

**Why not just convert each mapping one at a time?** You can, but you'll end up with a
mess. If 14 mappings all follow the same pattern differing only by table name, converting
them individually produces 14 separate scripts instead of one parameterized template plus
a config file. Shared lookup tables get redefined in every script. There's no dependency
graph, no project structure, and massive duplication. You left a proprietary tool and
landed in a code mess.

InformaticaProjectAnalysis reads all the mapping XMLs from your Informatica project
(the complete collection of exported mappings, workflows, and parameter files) and
produces a **conversion strategy** before any conversion begins. The strategy answers
three questions:

1. **Which mappings share the same structural pattern?** Mappings with the same
   transformation flow (e.g., source → lookup → expression → target) differing only
   by table names and column names are grouped together as candidates for a single
   parameterized template. The tool shows the evidence and assigns a confidence level.

2. **What depends on what?** If mapping A loads `DIM_CUSTOMER` and mapping B does a
   lookup against `DIM_CUSTOMER`, then B depends on A. The tool builds a dependency
   graph across all mappings and computes a safe execution order.

3. **What needs human attention?** Not every mapping can be confidently classified.
   Custom SQL overrides, missing definitions, and unusual patterns reduce certainty.
   The tool flags these for tech lead review.

The output is a strategy document (PDF + Excel + JSON) that tech leads and leadership
review and approve before conversion work starts. The tool does not prescribe which
target language to use or how to orchestrate — those decisions belong to the humans
reviewing the strategy.

---

## Quick Start

### 1. Prerequisites

- Python 3.11+
- An Anthropic API key (for AI-assisted analysis in Phases 1 and 2)

### 2. Installation

```bash
git clone https://github.com/ad25343/InformaticaProjectAnalysis.git
cd InformaticaProjectAnalysis

python -m venv .venv
source .venv/bin/activate    # macOS / Linux
# .venv\Scripts\activate     # Windows

pip install -r requirements.txt
```

### 3. Configuration

Copy the example environment file and fill in your values:

```bash
cp .env.example .env
```

Required settings in `.env`:

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Your Anthropic API key for AI-assisted analysis |
| `SECRET_KEY` | Random string for session cookie signing (change from default) |

Optional settings are documented in `.env.example`.

### 4. Create a Project Config

Create a `*.project.yaml` file that points at your Informatica project exports:

```yaml
project:
  name: "My Migration"
  version: "1.0"
  owner: "Data Engineering"

source:
  type: folder
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
  detect_shared_assets: true
  build_dependency_dag: true
  classify_expressions: true

review:
  tech_lead:
    name: "Your Name"
    email: "you@company.com"
```

### 5. Run the Tool

```bash
python -m app.main
```

The server starts on `http://localhost:8090` (configurable via the `PORT` env var).
Open the URL in your browser to access the UI.

---

## Project Config Reference

The `*.project.yaml` file is the single input that drives the entire analysis.

### `project` Section

| Field | Required | Description |
|---|---|---|
| `name` | Yes | Human-readable project name (appears in strategy documents) |
| `version` | No | Your version label for this migration scope |
| `owner` | No | Team or individual responsible |

### `source` Section

| Field | Required | Description |
|---|---|---|
| `type` | Yes | How to locate the files: `folder`, `repo`, `zip`, or `s3` |
| `location` | Yes | Path, URL, or bucket depending on source type |

Source types:

- **folder** — local directory path; the tool scans recursively using scope globs
- **repo** — Git repository URL with optional `branch` and `path` fields
- **zip** — uploaded ZIP archive; extracted and scanned
- **s3** — S3 bucket path; objects pulled and scanned

### `scope` Section

Controls which files within the source are included in the analysis.

| Field | Required | Description |
|---|---|---|
| `mappings.include` | Yes | Glob patterns for mapping XML files |
| `mappings.exclude` | No | Glob patterns to skip |
| `workflows.include` | No | Glob patterns for workflow XML files |
| `parameters.include` | No | Glob patterns for parameter files |
| `default_parameter_env` | No | Which parameter environment to use (default: `dev`) |

### `analysis` Section

| Field | Default | Description |
|---|---|---|
| `fingerprint_strictness` | `moderate` | How strict spine matching is (spine = the ordered sequence of transformation types in a mapping; see "How the Analysis Works" below): `strict`, `moderate`, `relaxed` |
| `min_group_size` | `2` | Minimum mappings needed to form a pattern group |
| `confidence_threshold` | `0.7` | Below this, mappings are flagged for human review |
| `detect_shared_assets` | `true` | Identify tables referenced by multiple mappings |
| `build_dependency_dag` | `true` | Build cross-mapping dependency graph (DAG = Directed Acyclic Graph) |
| `classify_expressions` | `true` | Use AI to classify expression complexity |

### `review` Section

Names and emails of the reviewers who will approve the strategy. These appear in the
strategy document and audit trail.

### `notifications` Section (optional)

| Field | Description |
|---|---|
| `webhook_url` | Slack/Teams webhook for status notifications |
| `events` | Which events trigger notifications (analysis complete, strategy ready, review approved) |

---

## How the Analysis Works

The analysis runs in five phases. You can watch progress in real time via the UI or
the SSE stream endpoint.

### Phase 1 — Discovery

The tool resolves your source location, scans for files matching your scope globs,
and parses every mapping XML it finds. Each mapping is reduced to its structural
components: transformation types, connector topology, expression bodies, lookup
references, parameter variables, and SQL overrides.

From the individual parse results, the tool builds a project-level graph:

- **Dependency edges** — if mapping A writes to TABLE_X and mapping B has a Lookup
  against TABLE_X, then B depends on A.
- **Shared assets** — tables that appear as lookup sources in multiple mappings.
- **Repeated expressions** — expression logic that appears verbatim across many mappings.

AI assists with elements the parser cannot classify deterministically: custom SQL
interpretation, expression complexity classification, mapplet behavior inference,
and implicit dependency detection.

Parse results are cached by file content hash (SHA-256). On re-analysis, only
new or changed files are re-parsed.

### Phase 2 — Pattern Grouping

Each mapping's transformation topology is reduced to a canonical "spine" — the
ordered sequence of transformation types from source to target.

Examples:
- `SQ → EXP → TARGET` (simple load)
- `SQ → LKP → EXP → RTR → UPD → TARGET` (SCD2 / slowly changing dimension)
- `SQ(×3) → JNR → LKP(×2) → EXP → RTR → TARGET(×2)` (complex multi-source)

Mappings with matching spines are candidates for the same pattern group. Within
each group, variation is classified:

- **Tier 1 — Parameter variation.** Structurally identical. Only table names,
  column names, and filter values differ. One template + one config file.

- **Tier 2 — Minor structural variation.** Core flow is the same, but minor
  differences exist (an extra Expression, a Filter present in some but not all).
  Template accommodates variation via config flags.

- **Tier 3 — Fundamental variation.** Different transformation types or flow
  shapes. Does not group. Convert individually.

Each mapping-to-group assignment carries a confidence level: HIGH, MEDIUM, LOW,
or UNCLASSIFIED.

### Phase 3 — Strategy Document Generation

Three outputs are produced from the same analysis data:

**PDF report** — two layers in one document. Page 1 is the leadership summary
(mapping count, pattern groups, scope reduction, risk flags). Remaining pages
are tech lead detail (per-group evidence, dependency DAG, shared assets,
per-mapping assignments with confidence).

**Excel workbook** — five sheets: Pattern Groups, Dependency Graph, Shared Assets,
Per-Mapping Assignments, Risk Flags.

**Strategy JSON** — machine-readable format containing pattern groups with members,
unique mappings with reasons, shared assets, dependency DAG, and execution order.

### Phase 4 — Human Gate (Strategy Review)

The strategy is presented in the UI for review. This is the decision gate.

Available actions:

| Action | What it does |
|---|---|
| Confirm mapping assignment | Locks the mapping's group assignment |
| Move mapping to different group | Simple override — no re-analysis needed |
| Convert mapping individually | Removes mapping from its group |
| Split group | Triggers lightweight re-validation |
| Merge groups | Triggers lightweight re-validation |
| Add notes | Stored per mapping and per group |
| **APPROVE** | Finalizes the strategy; generates approved strategy JSON |
| **REJECT** | Returns to analysis with reviewer notes as constraints |

Every review action is recorded in the audit trail with reviewer name, role,
timestamp, and decision.

### Phase 5 — Strategy Delivery

The approved strategy JSON is the final output. It contains everything needed to
drive a conversion: which mappings belong to which pattern groups, what parameters
are externalized, which mappings convert individually and why, the dependency DAG,
and the execution order.

The strategy can be delivered as a file or via API POST to a downstream conversion
tool.

---

## The Three UI Views

### Dashboard (Leadership)

The dashboard shows the project at a glance: total mappings, pattern groups found,
template candidates, unique mappings, and the scope reduction metric (e.g.,
"50 mappings → 8 templates + 12 unique files"). It includes complexity distribution,
confidence breakdown, dependency depth, and risk flags.

The APPROVE / REJECT gate is on this page — leadership makes the call after tech
leads have reviewed the detail.

### Pattern Groups (Tech Leads)

The detail view for validating groupings. The left panel lists all pattern groups
with member count and confidence indicator. Selecting a group shows:

- The spine (transformation flow)
- Member mappings with variation tier, confidence, and flags
- Parameter differences across members (what varies)
- Structural evidence (why these were grouped)
- Per-mapping override controls (confirm, move, convert individually)
- Notes field per mapping and per group

### Dependency Graph (Both Audiences)

Interactive dependency graph (DAG) visualization. Nodes are mappings, colored by pattern group.
Edges are dependencies. Click a node to see its upstream dependencies, downstream
dependents, and error propagation impact ("if this fails, N downstream mappings
are affected").

Execution stages are highlighted so you can see which mappings run in parallel
and where serialization is required.

---

## Three Operating Modes

### Interactive

Upload or point to a project config through the UI. Watch the analysis run in
real time. Review the strategy in the browser. Approve or override.

### Watcher

The tool polls a directory for `*.project.yaml` files. When one appears or
changes, analysis triggers automatically. Configure via:

```
WATCHER_ENABLED=true
WATCHER_DIR=./watch
WATCHER_POLL_INTERVAL_SECS=30
```

### CI/CD

A pipeline step posts the project config via API. The strategy document can be
retrieved as a build artifact. Review happens in the UI or through the API.

---

## Incremental Analysis

When you re-run analysis on a project that has changed:

- Only new or modified mapping XMLs are re-parsed (unchanged files hit the
  SHA-256 cache)
- Pattern grouping runs on the full project (fast — parsing is cached)
- Previous human decisions (overrides, confirmations, notes) are preserved
  unless the underlying XML changed
- The strategy document includes a diff section showing what changed

---

## Security

Security is infrastructure, not a feature layer. Key protections:

- All XML parsing is hardened against XXE (XML External Entity) injection —
  DTD and entity resolution disabled
- Path traversal prevented — all paths resolved relative to configured root;
  symlinks rejected
- ZIP extraction validates every entry path, caps total bytes and entry count
- Project config parsed with `yaml.safe_load()` and schema validated
- Session-cookie authentication on all non-static routes
- All HTTP security headers applied (CSP, HSTS, X-Frame-Options, etc.)
- No secrets in logs — structured logging only
- Dependencies pinned and audited with `pip-audit`

See SECURITY.md for the full threat/defence matrix.

---

## API Reference

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/projects` | Upload project config and start analysis |
| `POST` | `/api/projects/folder` | Start analysis from a folder path |
| `GET` | `/api/projects` | List all analysis jobs |
| `GET` | `/api/projects/{id}` | Get analysis job state |
| `GET` | `/api/projects/{id}/stream` | SSE progress stream |
| `GET` | `/api/projects/{id}/strategy.json` | Download strategy JSON |
| `GET` | `/api/projects/{id}/strategy.pdf` | Download strategy PDF |
| `GET` | `/api/projects/{id}/strategy.xlsx` | Download strategy Excel |
| `POST` | `/api/projects/{id}/review` | Submit review decision (APPROVE / REJECT) |
| `POST` | `/api/projects/{id}/override` | Submit mapping override |
| `GET` | `/api/projects/{id}/graph` | Get dependency graph data |
| `GET` | `/api/projects/{id}/groups` | Get pattern groups with members |
| `GET` | `/api/projects/{id}/groups/{gid}` | Get single group detail |
| `POST` | `/api/projects/{id}/deliver` | Trigger strategy delivery |
| `GET` | `/api/audit` | Audit trail of all review decisions |
| `GET` | `/api/health` | Liveness + readiness probe |

---

## Environment Variables

All configuration is via environment variables (loaded from `.env`).
See `.env.example` for the full list with descriptions.

| Variable | Required | Default | Description |
|---|---|---|---|
| `ANTHROPIC_API_KEY` | Yes | — | API key for AI-assisted analysis |
| `SECRET_KEY` | Yes | — | Session cookie signing key |
| `PORT` | No | `8090` | Server port |
| `HOST` | No | `127.0.0.1` | Server bind address |
| `DB_PATH` | No | `app/data/analysis.db` | SQLite database path |
| `CLAUDE_MODEL` | No | `claude-sonnet-4-20250514` | Model for AI analysis |
| `LOG_LEVEL` | No | `INFO` | Logging level |

---

## Troubleshooting

**Analysis fails at "Resolving Source"**
Check that the `source.location` in your project config points to a valid,
accessible directory. The tool does not follow symlinks.

**Most mappings are UNCLASSIFIED**
Try lowering `analysis.fingerprint_strictness` from `strict` to `moderate` or
`relaxed`. This widens the spine matching tolerance.

**Confidence scores are low across the board**
Check if your mappings use heavy custom SQL overrides or missing mapplet
definitions. These reduce confidence because the tool cannot fully determine
structural equivalence. The flagged mappings will need manual review.

**Parse cache not working**
The cache keys on SHA-256 of file content. If file paths change but content
is identical, the cache still hits. If the cache seems stale, check that the
`DB_PATH` points to the correct database.

**Port conflict**
Change the `PORT` environment variable in `.env`. Default is `8090`.

---

## Glossary

| Term | Definition |
|---|---|
| **Mapping** | A single unit of data transformation logic in Informatica PowerCenter. Defines how data flows from sources through transformations to targets. Exported as an XML file. |
| **Project** | The complete collection of Informatica PowerCenter mappings, workflows, and parameter files being analyzed for migration. Defined by a single `*.project.yaml` config file. |
| **Spine** | The canonical ordered sequence of transformation types in a mapping (e.g., `SQ → EXP → LKP → TARGET`). Used to identify structural similarity between mappings. |
| **Pattern group** | A set of mappings that share the same structural spine and can be converted using one parameterized template instead of N separate files. |
| **Variation tier** | How much a mapping differs from its group's canonical pattern: Tier 1 (parameter only — table/column names differ), Tier 2 (minor structural — an extra filter or expression), Tier 3 (fundamental — does not group). |
| **Confidence** | How certain the tool is about a mapping-to-group assignment: HIGH, MEDIUM, LOW, UNCLASSIFIED. |
| **Dependency edge** | A relationship where one mapping's output table is used as another mapping's lookup source — meaning the second must run after the first. |
| **Shared asset** | A table referenced as a lookup source by multiple mappings across the project. |
| **Strategy document** | The PDF + Excel + JSON output describing pattern groups, dependencies, and conversion recommendations. Reviewed and approved before conversion begins. |
| **Human gate** | The structured review step where tech leads and leadership approve or reject the strategy before any conversion work starts. |
| **Project config** | The `*.project.yaml` file that defines source location, scope, analysis settings, and reviewers. The single input that drives the entire analysis. |
