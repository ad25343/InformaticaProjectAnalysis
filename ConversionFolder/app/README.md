# Informatica Conversion Tool

Converts Informatica PowerCenter XML exports to PySpark, dbt, or Python.

12-step agentic pipeline powered by Claude with a self-improving security knowledge base, actionable remediation guidance, two-pass documentation generation, XML-grounded logic equivalence checking, three human review gates, and batch conversion — submit an entire set of mappings in a single ZIP and run up to 3 concurrently. Every Gate 2 approval makes future conversions smarter.

[![License: CC BY-NC 4.0](https://img.shields.io/badge/License-CC%20BY--NC%204.0-lightgrey.svg)](https://creativecommons.org/licenses/by-nc/4.0/)

> Free to use and adapt. Commercial use requires written permission. See [LICENSE](../LICENSE).

---

## Install on a New Machine

```bash
# 1. Clone the repo
git clone https://github.com/ad25343/InformaticaConversion.git
cd InformaticaConversion/app

# 2. Install dependencies (Python 3.11+)
pip install -r requirements.txt

# Recommended: enable the security scanner
pip install bandit

# 3. Configure environment
cp .env.example .env
# Open .env and fill in:
#   ANTHROPIC_API_KEY  — get one at https://console.anthropic.com
#   APP_PASSWORD       — login password for the web UI
#   SECRET_KEY         — any long random string for session signing

# 4. Start the server
bash start.sh
# → Web UI:   http://localhost:8000
# → API docs: http://localhost:8000/docs (set SHOW_DOCS=true)
```

---

## Upload Modes

**Individual files** — upload up to three files separately:
- Mapping XML (required)
- Workflow XML (optional — enables session config extraction and $$VAR cross-referencing)
- Parameter file `.txt` or `.param` (optional — resolves all `$$VARIABLE` references)

**ZIP archive** — drop a ZIP containing any combination of the above; file types are auto-detected from XML structure (not filename).

**Batch ZIP** *(v2.0)* — drop a ZIP with one subfolder per mapping; all mappings are converted concurrently (up to 3 at a time). Each mapping runs through the full 12-step pipeline with independent review gates.

```
batch.zip/
  mapping_a/
    mapping.xml       ← required
    workflow.xml      ← optional
    params.txt        ← optional
  mapping_b/
    mapping.xml
```

---

## Pipeline

| Step | Name | Powered By | Notes |
|------|------|-----------|-------|
| 0 | Session & Parameter Parse | Deterministic | Auto-detect file types; cross-ref validation; $$VAR resolution; credential scan on uploaded XML |
| 1 | Parse XML | lxml (deterministic) | Fails fast on malformed XML; XXE-hardened parser |
| 2 | Classify Complexity | Rule-based | LOW / MEDIUM / HIGH / VERY_HIGH |
| S2T | Source-to-Target Map | Rule-based | Excel workbook generated |
| 3 | Generate Documentation | Claude | **Tier-based**: LOW mappings use a single pass (overview + transformations + parameters). MEDIUM/HIGH/VERY_HIGH use two passes — Pass 1: transformations; Pass 2: lineage (non-trivial fields only). Pass 2 does not re-send the graph JSON. If truncated, the pipeline continues with a Gate 1 warning — no hard fail. 30-second SSE heartbeats keep UI updated during long runs. |
| 4 | Verify | Deterministic + Claude | Graph structural checks (isolated transforms, disconnected sources/targets) + Claude graph-risk review (hardcoded values, incomplete conditionals, high-risk logic). Does **not** read or check documentation — docs are reviewed visually by the human at Gate 1. |
| **5** | **Gate 1 — Human Review** | UI sign-off | **APPROVE / REJECT** |
| 6 | Stack Assignment | Rules + Claude | PySpark / dbt / Python |
| 7 | Convert | Claude | Production-ready code files + YAML config artifacts. **Security KB injected**: 17 standing rules + auto-learned patterns from prior jobs prepended to every prompt — no wait for the scan to catch known issues |
| **8** | **Security Scan** | bandit + YAML regex + Claude | Hardcoded creds, SQL injection, insecure connections — each finding includes actionable remediation guidance |
| **9** | **Gate 2 — Security Review** | UI sign-off | **APPROVED / ACKNOWLEDGED / REQUEST_FIX / FAILED** — pauses when findings exist; "🔧 How to fix" shown per finding; REQUEST_FIX re-runs Steps 7→8→Gate 2 (max 2 rounds) |
| 10 | Logic Equivalence + Code Quality | Claude | Stage A: rule-by-rule XML→code comparison (VERIFIED/NEEDS_REVIEW/MISMATCH); Stage B: 10+ static quality checks |
| 11 | Test Generation | Claude | pytest / dbt test stubs; test files re-scanned for secrets |
| **12** | **Gate 3 — Code Review** | UI sign-off | **APPROVED / REJECTED** |

### Human Gates

**Gate 1 (Step 5 — Human Review):** Reviewer sees the full Verification Report before any code is generated. Where Claude suggests an actionable code-level fix for a flag (`auto_fix_suggestion`), a "🔧 Suggested Auto-Fix" panel is shown with a checkbox — checking it carries the suggestion forward to Step 7 for the conversion agent to apply.
- APPROVE → pipeline continues to stack assignment and code generation
- REJECT → job blocked permanently

**Gate 2 (Step 9 — Security Review):** Reviewer sees the full security scan findings and makes an informed decision. Pipeline pauses only when the scan is not clean (REVIEW_RECOMMENDED or REQUIRES_FIXES). Clean scans auto-proceed.
- APPROVED → proceed to logic equivalence + code quality review (scan was clean, or reviewer confirmed no action needed)
- ACKNOWLEDGED → proceed with a note on record (known risk accepted)
- REQUEST_FIX → re-run Step 7 (code generation) with all findings injected as mandatory fix requirements, then re-run Step 8 (security scan), then re-present Gate 2. Capped at 2 remediation rounds; if the re-scan is clean it auto-proceeds. "Request Fix" button hidden after round 2.
- FAILED → job blocked permanently

**Gate 3 (Step 12 — Code Review):** Reviewer sees converted code, test coverage, and the security report.
- APPROVED → job marked COMPLETE
- REJECTED → job blocked permanently; team re-uploads the mapping to start a fresh job

---

## Architecture

```
app/
├── main.py                        FastAPI entry point (CORS, startup security warnings)
├── start.sh                       Start script (checks .env, launches uvicorn)
├── requirements.txt
├── .env.example                   Copy to .env and fill in secrets
│
├── backend/
│   ├── orchestrator.py            Pipeline state machine (12 steps + 3 gates)
│   ├── routes.py                  REST API endpoints (single-file + ZIP + batch upload)
│   ├── security.py                Central security module (XXE, Zip Slip, Zip Bomb,
│   │                              credential scan, YAML secrets scan, bandit wrapper)
│   ├── security_knowledge.py      Security KB — standing rules loader + auto-learned
│   │                              patterns store; builds prompt injection block (v2.2)
│   ├── security_rules.yaml        17 hand-curated standing security rules (v2.2)
│   ├── zip_extractor.py           ZIP upload handler (single-mapping + batch extraction)
│   ├── auth.py                    Session auth
│   ├── logger.py                  Structured per-job logging
│   ├── agents/
│   │   ├── session_parser_agent.py Step 0  — Session & parameter parse
│   │   ├── parser_agent.py        Step 1  — XML parser (lxml, XXE-hardened)
│   │   ├── classifier_agent.py    Step 2  — Complexity classifier
│   │   ├── s2t_agent.py           Step S2T — Source-to-Target Excel
│   │   ├── documentation_agent.py Step 3  — Documentation (Claude)
│   │   ├── verification_agent.py  Step 4  — Verification
│   │   ├── conversion_agent.py    Steps 6–7 — Stack assignment + code generation
│   │   ├── security_agent.py      Step 8  — Security scan (bandit + YAML + Claude)
│   │   ├── review_agent.py        Step 10 — Logic equivalence + code quality review (v1.3)
│   │   └── test_agent.py          Step 11 — Test generation
│   ├── models/
│   │   └── schemas.py             Pydantic models for all pipeline artifacts
│   └── db/
│       └── database.py            SQLite persistence (swap URL for PostgreSQL)
│
├── frontend/
│   └── templates/
│       ├── index.html             Main pipeline UI (individual files + ZIP + Batch tabs)
│       └── login.html             Login screen
│
└── sample_xml/
    ├── sample_mapping.xml         Quick single-set test (root level)
    ├── sample_workflow.xml
    ├── sample_params.txt
    ├── simple/                    3 mappings — single/dual source, passthrough
    ├── medium/                    4 mappings — lookups, filters, expressions, SCD1
    └── complex/                   2 mappings — SCD2, 3+ sources, 9–11 $$VARs
```

---

## Security Architecture

Every file-handling path flows through `backend/security.py`. Key protections:

| Threat | Defence |
|---|---|
| XXE injection | `safe_xml_parser()` — DTD loading and entity resolution disabled on every lxml parse |
| Zip Slip | `safe_zip_extract()` — every entry path resolved relative to virtual root |
| Zip Bomb | `safe_zip_extract()` — total extracted bytes and entry count capped |
| Symlink attacks | Symlink entries in ZIP silently skipped |
| Oversized uploads | `validate_upload_size()` called on every upload stream before processing |
| Credentials in uploaded XML | `scan_xml_for_secrets()` — checks CONNECTION/SESSION attrs at Step 0 |
| Insecure generated code | Step 8 — bandit (Python), YAML regex scan, Claude review (all stacks) |
| Security gate | Step 9 — human reviewer must explicitly approve, acknowledge, or fail findings before code review begins |
| Secrets in generated test code | Step 11 test files re-scanned and merged into Step 8 report before Gate 3 |
| Recurring bad patterns re-introduced | Security KB — 17 standing rules + patterns learned from every prior Gate 2 approval injected into Step 7 prompt; each job makes the next one safer |

---

## Complexity Tiers

| Tier | Criteria | QC tokens |
|------|----------|-----------|
| LOW | < 5 transformations | 2 048 |
| MEDIUM | 5–9 transformations | 4 096 |
| HIGH | 10–14 transformations | 6 144 |
| VERY_HIGH | 15+ transformations, or 2+ independent HIGH structural criteria | 8 192 |

**Documentation (Step 3)** uses a tier-based strategy. LOW-tier mappings get a single pass (Overview + Transformations + Parameters — no lineage section needed for simple mappings). MEDIUM/HIGH/VERY_HIGH use two passes: Pass 1 covers Overview + all Transformations + Parameters; Pass 2 covers Field-Level Lineage (non-trivial fields only) + Session Context + Ambiguities. Pass 2 does not re-send the graph JSON — Pass 1 output already contains all transformation detail, cutting Pass 2 input tokens by ~50%. If a pass truncates, the pipeline continues with a Gate 1 warning rather than failing.

---

## Stack Assignment

Step 6 assigns the target stack based on mapping characteristics. The decision is deterministic — reviewers can override at Gate 1.

| Criterion | PySpark | dbt | Python (Pandas) |
|---|---|---|---|
| **Complexity tier** | HIGH / VERY_HIGH | LOW / MEDIUM | LOW / MEDIUM |
| **Data volume** | > 50M rows | Any (SQL-bound) | < 1M rows |
| **Source type** | DB, files, streams | DB / warehouse | Files, APIs |
| **Target type** | DB, data lake, files | Data warehouse | Files, APIs, lightweight DB |
| **Transformation types** | Complex joins, multi-aggregations, UDFs | SQL-expressible — filters, joins, SCDs, derived fields | Simple field mapping, API calls, file conversion |
| **SCD support** | SCD1 + SCD2 (merge/upsert) | SCD1 + SCD2 (snapshots) | SCD1 only |
| **Lookup handling** | Broadcast join, dynamic cache | CTE or `ref()` | Dict lookup / merge |
| **Output artifacts** | `.py` + `requirements.txt` + YAML configs | `.sql` models + `schema.yml` + `sources.yml` + macros | `.py` script + `requirements.txt` |
| **Test framework** | pytest + pyspark.testing | dbt tests (schema.yml) | pytest |

**Hybrid:** documented explicitly in the stack assignment record when a mapping has sub-flows that suit different stacks.

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `ANTHROPIC_API_KEY` | Yes | — | Claude API key |
| `APP_PASSWORD` | Yes | — | Web UI login password |
| `SECRET_KEY` | Yes | — | Session signing key (any long random string) |
| `CLAUDE_MODEL` | No | `claude-sonnet-4-5-20250929` | Override Claude model |
| `HOST` | No | `0.0.0.0` | Server bind address |
| `PORT` | No | `8000` | Server port |
| `SHOW_DOCS` | No | `false` | Enable Swagger UI at `/docs` |
| `CORS_ORIGINS` | No | *(same-origin)* | Comma-separated allowed origins for cross-origin deployments |
| `HTTPS` | No | `false` | Set `true` to enable secure cookie flag (HTTPS deployments) |
| `MAX_UPLOAD_MB` | No | `50` | Max size for any single uploaded file |
| `MAX_ZIP_EXTRACTED_MB` | No | `200` | Max total extracted size from a ZIP (zip bomb guard) |
| `MAX_ZIP_FILE_COUNT` | No | `200` | Max number of files inside a ZIP |
| `DOC_MAX_TOKENS_OVERRIDE` | No | — | Force a specific doc token limit — for testing truncation only |
| `DB_PATH` | No | `app/data/jobs.db` | Override SQLite database location — set to an absolute path for Docker or shared-filesystem deployments |
| `BATCH_CONCURRENCY` | No | `3` | Maximum number of mapping pipelines that run concurrently in a batch upload — lower to reduce Claude API pressure |

---

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `POST` | `/api/jobs` | Upload Mapping XML (+ optional Workflow + Parameter) and start pipeline |
| `POST` | `/api/jobs/zip` | Upload a single-mapping ZIP archive — file types auto-detected |
| `POST` | `/api/jobs/batch` | Upload a batch ZIP (one subfolder per mapping) — starts all pipelines |
| `GET` | `/api/batches/{id}` | Get batch record + per-job summaries and computed batch status |
| `GET` | `/api/jobs` | List all jobs (most recent 50) |
| `GET` | `/api/jobs/{id}` | Get full job state |
| `GET` | `/api/jobs/{id}/stream` | SSE progress stream |
| `DELETE` | `/api/jobs/{id}` | Soft-delete job — stamps `deleted_at`; data preserved in Log Archive |
| `POST` | `/api/jobs/{id}/sign-off` | Gate 1 decision (`APPROVE` / `REJECT`) |
| `POST` | `/api/jobs/{id}/security-review` | Gate 2 decision (`APPROVED` / `ACKNOWLEDGED` / `REQUEST_FIX` / `FAILED`) |
| `POST` | `/api/jobs/{id}/code-signoff` | Gate 3 decision (`APPROVED` / `REJECTED`) |
| `GET` | `/api/jobs/{id}/logs` | Job log (JSON or plain text via `?format=text`) |
| `GET` | `/api/jobs/{id}/logs/download` | Download raw JSONL log |
| `GET` | `/api/jobs/{id}/s2t/download` | Download S2T Excel workbook |
| `GET` | `/api/jobs/{id}/download/{file}` | Download a generated code file |
| `GET` | `/api/jobs/{id}/tests/download/{file}` | Download a generated test file |
| `GET` | `/api/logs/registry` | All jobs with log filenames and final status |
| `GET` | `/api/logs/history` | Log Archive feed — soft-deleted + orphaned log entries |
| `GET` | `/api/logs/history/{job_id}` | Read a historical log without a live DB record |
| `GET` | `/api/security/knowledge` | Security KB summary: rule count, pattern count, top patterns |

> Enable interactive API docs at `http://localhost:8000/docs` by setting `SHOW_DOCS=true` in `.env`.

---

## Running Tests

```bash
cd app

# Unit tests — no API key needed (deterministic security utils)
python3 test_security.py

# Integration smoke test — Steps 0–4 against sample files
python3 test_pipeline.py              # mapping-only
python3 test_pipeline.py --full       # mapping + workflow + params
python3 test_pipeline.py --step0-only # Step 0 only (no Claude API calls)
```

---

## Roadmap

| Version | Status | Scope |
|---------|--------|-------|
| **v1.0** | Shipped | Transformation logic, human review gates, PySpark / dbt / Python code generation |
| **v1.1** | Shipped | Three-file upload + ZIP archive; session config extraction; $$VAR resolution; YAML artifact generation; dedicated Security Scan step (Step 8); bandit + YAML + Claude security review |
| **v1.2** | Shipped | Human Security Review Gate (Step 9); 12-step pipeline; three human-in-the-loop decision points; security sign-off record on every job |
| **v1.3** | Shipped | Logic Equivalence Check (Step 10 Stage A); XML-grounded rule-by-rule verification of generated code; per-rule VERIFIED/NEEDS_REVIEW/MISMATCH verdicts; equivalence report in Gate 3 and downloadable reports |
| **v2.0** | Shipped | Batch conversion — one subfolder per mapping ZIP; up to 3 concurrent pipelines; batch tracking (`batches` table, `batch_id` on jobs); batch group view in UI; `POST /api/jobs/batch` + `GET /api/batches/{id}` |
| **v2.1** | Shipped | Security remediation guidance per finding (B101–B703 lookup + Claude-generated); two-pass documentation (128K combined ceiling, eliminates SCD2 truncation); Gate 2 REQUEST_FIX remediation loop (re-runs Steps 7→8, max 2 rounds, security findings injected into conversion prompt); timestamp timezone fix; CI failure-only notifications |
| **v2.2** | Shipped | Security Knowledge Base (17 standing rules + auto-learned patterns; every Gate 2 approval makes future conversions smarter); scan round history + fix-round diff UI; Log Archive sidebar; soft delete; bandit PATH fix; Gate 2 UI fixes; doc truncation changed to Gate 1 warning |
| **v2.2.2** | Shipped | Verification decoupled from docs (graph structural + risk checks only); tier-based doc depth (LOW = single pass); Pass 2 no longer re-sends graph JSON (~50% input token reduction); field-level lineage scoped to non-trivial fields only |
| **v2.3.0** | Shipped | Code review hardening: bcrypt passwords, Claude API retry (exponential backoff), XML input validation, DB indices, `/health` endpoint, pydantic Settings class |
| **v2.3.1** | Shipped | Error handling: WRONG_FILE_TYPE detection for workflow-in-mapping-slot; empty mapping guard; error message propagation to UI error card; tailored actionable hints for known failure patterns |
| **v2.3.2** | Shipped | Verification flag auto-handling: conversion agent addresses all auto-fixable flags in code (pass-through stubs, config extraction, TODO comments, manual stubs); source SQ connectivity false positive fixed |
| **v2.3.3** | Shipped | 5 new security rules (Oracle TCPS, log injection, macro SQL injection, hardcoded business constants) — 17→21 standing rules; Best Practices Guide security section added |
| **v2.3.4** | Shipped | Security KB auto-promotion: patterns seen in ≥3 Gate 2 decisions auto-promoted to standing rules; `_DEFAULT_RULES` now synced from YAML (single source of truth) |
| **v2.3.5** | Shipped | Verification false positive fixes: abbreviated SQ names (SQ_APPRAISALS for CORELOGIC_APPRAISALS), Lookup reference sources (REF_COUNTY_LIMITS via LKP), and RANKINDEX orphaned port on Rank transformations now correctly handled |
| **v2.3.6** | Current | Rank/Sorter accuracy: parser captures sort keys; graph summary shows Rank config + Sorter sort order to Claude; RANKINDEX DEAD_LOGIC suppressed; accuracy check semantics fixed so HIGH_RISK findings no longer cause misleading REQUIRES_REMEDIATION |
| **v2.3** | Planned | Git integration (open PR from UI); scheduler; team review mode with comment threads; Slack/Teams webhook notifications |
| **v3.0** | Vision | Continuous migration mode; observability dashboard; self-hosted model support; repository-level object handling |

---

## Database

SQLite by default (`app/data/jobs.db`). To switch to PostgreSQL, change `DATABASE_URL` in `backend/db/database.py`.

Job logs are written to `app/logs/jobs/` as newline-delimited JSON.
