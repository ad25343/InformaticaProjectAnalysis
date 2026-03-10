# Product Requirements Document
## Informatica Conversion Tool

**Version:** 2.3.6
**Author:** ad25343
**Last Updated:** 2026-03-02
**License:** CC BY-NC 4.0 — [github.com/ad25343/InformaticaConversion](https://github.com/ad25343/InformaticaConversion)
**Contact:** [github.com/ad25343/InformaticaConversion/issues](https://github.com/ad25343/InformaticaConversion/issues)

---

## 1. Problem Statement

Enterprise data teams spend months manually rewriting Informatica PowerCenter ETL mappings
into modern stacks (PySpark, dbt, Python). The work is repetitive, error-prone, and requires
two skills simultaneously — deep Informatica knowledge and the target-stack fluency — rarely
found in the same engineer.

The Informatica Conversion Tool automates this migration using Claude as the conversion
engine, structured human review gates to catch errors before code ships, and a bandit +
Claude security scan with a human security review gate to ensure generated code does not
inherit bad patterns from legacy Informatica designs.

---

## 2. Target Personas

**Primary: Data Migration Engineer**
Responsible for rewriting PowerCenter mappings to PySpark or dbt. Knows Informatica well
but needs help producing idiomatic modern-stack code quickly. Cares about correctness of
field mappings, filter logic, and business rules — not about writing boilerplate.

**Secondary: Data Engineering Lead / Reviewer**
Approves the generated code before it enters a CI pipeline. Needs a structured review
checklist, a Source-to-Target field mapping document, a security scan report with a
human decision gate, and a final code sign-off — not a wall of generated code with no summary.

**Tertiary: DevOps / Platform Engineer**
Deploying the tool inside a corporate network. Cares about CVE-free dependencies, no
plaintext secrets in generated code, and configurable upload size limits.

---

## 3. Version Scope

### v1.0 — MVP (shipped)

The single-file conversion pipeline. Accepts one Informatica Mapping XML export and
produces converted code through an agentic pipeline with two human review gates.

Steps:
1. XML parse and structural analysis
2. Complexity classification (LOW / MEDIUM / HIGH / VERY_HIGH)
S2T. Source-to-Target field mapping (S2T Excel workbook)
3. Documentation generation (Markdown)
4. Verification — flags unsupported transformations, dead columns, unresolved parameters
5. Gate 1 — human sign-off (APPROVE / REJECT)
6. Target stack assignment (PySpark / dbt / Python)
7. Code generation
8. Code quality review (Claude cross-check vs. documentation and S2T)
9. Test generation
10. Gate 2 — code review sign-off (APPROVE / REJECT)

Delivered: single-file upload, SSE progress stream, per-job structured logging, SQLite
job persistence, session-cookie authentication, sample mappings across three complexity
tiers.

### v1.1 — Session & Parameter Support (shipped)

Extends the pipeline with a new Step 0 that processes Workflow XML and parameter files
alongside the Mapping XML. Adds ZIP upload as a convenience upload path. Introduces a
dedicated Security Scan step (Step 8) with bandit + YAML regex + Claude review.

New features:
- Step 0 — file-type auto-detection, cross-reference validation, session config extraction, $$VAR resolution
- Three-file upload (Mapping + Workflow + Parameter) and ZIP archive upload
- YAML artifact generation (connections.yaml, runtime_config.yaml) from session config
- UNRESOLVED_VARIABLE flags when $$VARs have no value in the parameter file
- Security-hardened infrastructure: XXE protection, Zip Slip / Zip Bomb / symlink defense, 7 CVEs patched, CORS middleware, startup secret-key warning, per-upload file size enforcement
- Step 8 — Security Scan (dedicated step): bandit (Python/PySpark), YAML regex secrets check, Claude review for all stacks
- Paired sample files for all 9 sample mappings (simple / medium / complex)
- Collapsable job history in the UI with smart default (open when no active jobs)
- Downloadable report as Markdown and PDF (print dialog)

### v1.2 — Human Security Review Gate (current)

Replaces the automatic CRITICAL-block gate after the security scan with a human decision
gate. Reviewers now make an informed choice — they can see the full scan findings in the UI
and decide to approve, acknowledge risk, or fail the job. This extends the pipeline from
11 to 12 steps and adds a third human-in-the-loop gate.

New features:
- Step 9 — Human Security Review Gate: pipeline pauses when scan recommendation is
  REVIEW_RECOMMENDED or REQUIRES_FIXES; reviewers choose APPROVED / ACKNOWLEDGED / FAILED
- Three-decision security review: APPROVED (clean or no action needed), ACKNOWLEDGED
  (issues noted and accepted as known risk), FAILED (block pipeline permanently)
- Clean scans (APPROVED recommendation) auto-proceed without pausing the pipeline
- Security review record stored on the job (reviewer name, role, decision, notes, date)
- POST `/api/jobs/{id}/security-review` endpoint
- 12-dot stepper in the UI with "Sec Rev" dot at Step 9
- All downstream step numbers updated: Quality Review → 10, Tests → 11, Code Sign-Off → 12

### v1.3 — Logic Equivalence Check (current)

Upgrades Step 10 (Code Quality Review) with a new Logic Equivalence stage that goes
back to the original Informatica XML as the ground truth and verifies rule-by-rule
that the generated code correctly implements every transformation, expression, filter,
join, and null-handling pattern. This is a cross-check of Claude's own output against
the source XML — not against Claude's documentation of it.

New features:
- Stage A — Logic Equivalence: per-rule verdicts (VERIFIED / NEEDS_REVIEW / MISMATCH)
  comparing generated code directly against the original Informatica XML
- Stage B — Code Quality: existing 10-check static review (unchanged)
- Equivalence stats shown at Gate 3: X VERIFIED / Y NEEDS REVIEW / Z MISMATCHES + coverage %
- Per-rule detail in Gate 3 card: rule type, XML rule verbatim, generated implementation, note
- Mismatches detected by equivalence check cap code review recommendation at REVIEW_RECOMMENDED
- Logic Equivalence section added to downloadable Markdown and PDF reports
- `LogicEquivalenceCheck` and `LogicEquivalenceReport` added to data model

### v2.0 — Batch Conversion (current)

Introduces multi-mapping batch conversion so an entire set of Informatica exports can be
submitted in one upload and processed concurrently.

New features:
- Batch ZIP upload: one subfolder per mapping inside the ZIP; Workflow XML and parameter
  file are optional per folder and auto-detected from content
- `POST /api/jobs/batch` endpoint: validates the ZIP, creates a batch record, spawns an
  independent 12-step pipeline job for each mapping folder
- Up to 3 mapping pipelines run concurrently (asyncio Semaphore); rate-limited against the
  Claude API by the same semaphore
- Each job retains all existing human review gates independently (Gate 1, Gate 2, Gate 3)
- Batch tracking: `batches` DB table + `batch_id` on job records; `GET /api/batches/{id}`
  returns batch record + per-job summaries with a computed status
  (running / complete / partial / failed)
- Batch UI: "Batch" upload tab alongside "Individual Files" and "ZIP Archive"; uploaded
  jobs grouped under a batch header card in the sidebar with live summary stats
  (X complete · Y awaiting review · Z running · N blocked)

### v2.1 — Security Remediation + Reliable Documentation (current)

Quality and reliability improvements across the security and documentation pipeline.

New features:
- **Security remediation guidance (v1.4):** Every security finding now includes an
  actionable "How to fix" recommendation. Bandit findings (B101–B703) are matched to a
  static remediation lookup table; YAML secrets findings include a canned credential
  externalisation guide; Claude findings include a model-generated remediation field.
  The Gate 2 UI shows a green "🔧 How to fix:" section per finding.
- **Gate 2 REQUEST_FIX remediation loop:** Reviewers can now request that Claude actually
  fix the identified security findings rather than just accepting or rejecting them. Choosing
  REQUEST_FIX re-runs Step 7 (code generation) with all security findings injected into the
  conversion prompt as mandatory fix requirements (severity, location, description, and
  required fix per finding). Step 8 (security scan) then re-runs on the regenerated code and
  Gate 2 is re-presented to the reviewer. If the re-scan is clean it auto-proceeds to Step 10.
  Capped at 2 remediation rounds — the "Request Fix" button is hidden after round 2 to prevent
  infinite loops. Round number and remaining attempts are shown as a banner in the Gate 2 UI.
  The remediation round is tracked in `SecuritySignOffRecord.remediation_round`.
- **Two-pass documentation (Step 3):** Documentation generation now runs as two
  sequential Claude calls instead of one. Pass 1 covers Overview + all Transformations
  + Parameters & Variables; Pass 2 covers Field-Level Lineage + Session & Runtime
  Context + Ambiguities, with Pass 1 output as context. Each call uses the 64K
  extended-output beta, giving a combined ceiling of ~128K output tokens — sufficient
  for any Informatica mapping in practice. Eliminates the truncation failures seen on
  HIGH/VERY_HIGH complexity SCD2 and multi-target mappings.
- **Timestamp timezone fix:** All UI timestamps are now correctly displayed in the
  user's local timezone. Previously, UTC timestamps from the database were rendered
  without conversion, showing the wrong time for non-UTC users.
- **Step 3 completeness gate:** Documentation generation stamps a `<!-- DOC_COMPLETE -->`
  sentinel at the end of the output on success, or `<!-- DOC_TRUNCATED -->` if any pass
  hits the token limit. The orchestrator checks for the sentinel immediately after Step 3
  and fails the job before Step 4 runs — preventing verification from operating on an
  incomplete document.
- **DB_PATH persistence fix:** Default database path changed from the OS temp directory
  (data loss on reboot) to `app/data/jobs.db` relative to the repository root. Override
  with the `DB_PATH` environment variable for Docker or shared-filesystem deployments.
- **Step 3 progress heartbeat:** The orchestrator now runs documentation generation
  as a background async task and emits an SSE progress event every 30 seconds showing
  elapsed time and which pass is active. Large SCD2 mappings can take 15–20+ minutes —
  the pipeline is fully async so no other jobs are blocked. No timeout is imposed on
  the Claude calls; a hard cap would incorrectly fail valid long-running passes.
- **CI noise reduction:** GitHub Actions security scan now only fires when Python
  source files change (path filter); success emails suppressed — notifications sent
  only on scan failure.

### v2.2 — Security Knowledge Base + Reliability

Closes the feedback loop between Gate 2 security findings and future code generation so
the tool evolves with every conversion run.

New features:
- **Security Knowledge Base**: two-layer system injected into every conversion prompt.
  - *Standing rules* (`security_rules.yaml`, committed to source): 17 hand-curated rules
    covering hardcoded credentials, SQL injection, eval/exec, subprocess, XXE, Zip Slip,
    weak cryptography, insecure random, PII logging, TLS bypass, temp files, and 5
    dbt/Snowflake-specific rules derived from real job findings.
  - *Auto-learned patterns* (`security_patterns.json`, runtime state): after every Gate 2
    APPROVED or ACKNOWLEDGED decision the findings are merged into a persistent store keyed
    by `(test_id/test_name, severity)` with an occurrence counter. Patterns that recur
    across jobs accumulate weight and are injected into future prompts with the highest
    emphasis. Historical backfill on first run seeds patterns from all prior job logs.
  - `GET /api/security/knowledge` — returns `rules_count`, `patterns_count`, `top_patterns`.
  - Sidebar "🛡 Security KB: N rules · M learned patterns" badge shows KB state on load.
- **Security scan round history + fix-round diff**: each scan round is archived in
  `security_scan_rounds` before being overwritten. Gate 2 shows a ✅ Fixed / ⚠️ Remains /
  🆕 New comparison table after each REQUEST_FIX round.
- **Log Archive sidebar**: collapsible section shows historical jobs whose DB records are
  gone but log files remain on disk. Clicking opens a read-only log viewer.
  `GET /api/logs/history` and `GET /api/logs/history/{job_id}` endpoints.
- **Soft delete**: the 🗑 button stamps `deleted_at` on the job instead of issuing
  `DELETE FROM jobs`. Soft-deleted jobs disappear from the active list but all data is
  preserved and surfaced in the Log Archive. DB auto-migrates on startup.
- **BATCH_CONCURRENCY env var**: batch semaphore configurable via environment (default 3).
- **E2E mortgage batch test set (Stages 2–6)**: six-stage synthetic mortgage pipeline
  covering all three target stacks and all complexity tiers.
- **Documentation truncation changed to Gate 1 warning**: previously truncated docs caused
  a hard pipeline failure. Now the pipeline continues with a `doc_truncated` flag; the
  Step 3 UI card shows an orange border and TRUNCATED badge; a HIGH (non-blocking)
  `DOCUMENTATION_TRUNCATED` flag appears in the Gate 1 verification report.
- **Doc sentinels stripped before state storage**: `<!-- DOC_COMPLETE -->` and
  `<!-- DOC_TRUNCATED -->` are stripped from `documentation_md` before being stored in
  state, so they never appear in the UI, PDF report, or downstream agent prompts.

Fixed:
- Security findings injection in REQUEST_FIX was passing blank descriptions because wrong
  field names were used (`finding_type` / `location` / `description` → corrected to
  `test_name` / `filename` / `text`); offending code snippet and line number now included.
- Gate 2 approval buttons missing after regen — `REQUEST_FIX` was counted as a signed-off
  state, hiding the decision buttons. Fixed by excluding it from the `signedOff` check.
- Gate 2 security card rendered during Steps 7/8 regen. Gated on status correctly.
- Bandit `FileNotFoundError` despite being installed — subprocess path resolution on macOS
  fixed by using `sys.executable -m bandit` instead of the bare `bandit` shell command.
- `NameError: name 'os' is not defined` at startup — missing `import os` in `routes.py`.
- `loadJobs()` silently hiding live jobs when `/api/logs/history` errored.

### v2.2.2 — Verification & Documentation Token Efficiency (current)

Decouples the verification step from documentation and reduces documentation token usage
to eliminate the truncation-cascade failure mode on large mappings.

Changed:
- **Verification agent reads the graph, not the docs**: the documentation is human-facing
  and reviewed visually at Gate 1. Verification now runs graph structural checks
  (isolated transformations, disconnected sources/targets) and a Claude graph-risk review
  (hardcoded values, incomplete conditionals, high-risk logic, lineage gaps) — all driven
  by the mapping graph. Doc string-matching and the doc-based Claude quality call are
  removed.
- **Documentation: tier-based depth**: LOW-tier mappings (< 5 transformations) use a
  single pass — Overview + Transformations + Parameters only. MEDIUM/HIGH/VERY_HIGH use
  two passes as before.
- **Documentation Pass 2 no longer receives graph JSON**: Pass 1 output already contains
  all transformation detail. Removing the redundant payload cuts Pass 2 input tokens by
  ~50%, giving substantially more headroom before truncation.
- **Field-level lineage scoped to non-trivial fields**: derived, aggregated, lookup-result,
  conditional, and type-cast fields get full individual traces; passthrough and rename-only
  fields are consolidated into a single summary table. Eliminates the dominant source of
  Pass 2 output bloat on wide mappings.

### v2.3.0 — Code Review Hardening (current)

Addresses all immediate and short-term items from the external code review.

- **bcrypt password hashing**: replaced SHA-256 (fast hash, brute-forceable) with bcrypt
  work factor 12. Password hashed once at startup; `bcrypt.checkpw()` used for logins.
- **Claude API retry logic**: new `agents/retry.py` with exponential backoff (3 attempts,
  10 s base + jitter). Retries on 429/500/502/503/529. Applied to all agent Claude calls.
- **XML input validation**: upload endpoint rejects empty files and non-XML content with
  HTTP 400 before creating a job or spending API tokens.
- **Database indices**: `idx_jobs_status`, `idx_jobs_created_at`, `idx_jobs_batch_id`,
  `idx_jobs_deleted_at` — applied idempotently at startup.
- **`GET /api/health` endpoint**: liveness + readiness probe returning status, version,
  db connectivity, and uptime. HTTP 200 / 503. Suitable for Docker HEALTHCHECK.
- **Pydantic `Settings` class**: `backend/config.py` centralises all 20+ env var reads.
  Replaces scattered `os.environ.get()` calls across 10+ files.

### v2.3.6 — Verification Rank/Sorter Accuracy Improvements (current)

Fixed:
- **Parser captures Sorter sort keys** — `SORTKEYPOSITION` and `SORTDIRECTION` on Sorter
  `TRANSFORMFIELD` elements are now stored per port. Previously discarded; Claude had no
  visibility into the sort order feeding a downstream Rank.
- **Graph summary shows Rank config + Sorter sort order** — `_build_graph_summary()` emits
  `rank_config` (Number Of Ranks, Rank=TOP/BOTTOM) and `sort_keys` (field + direction).
  Eliminates false REVIEW_REQUIRED on Rank dedup mappings.
- **RANKINDEX DEAD_LOGIC suppression** — `rank_index_ports` collected and passed to Claude
  with an explicit prompt note. Post-filter also catches any DEAD_LOGIC Claude raises on
  RANKINDEX even if the prompt note is disregarded.
- **Accuracy check semantics corrected** — "No high-risk logic patterns detected" renamed
  to "Claude graph review completed". Passes when Claude ran; fails only on API error. High-
  risk findings surface as FLAGS — they no longer trigger a misleading REQUIRES_REMEDIATION.

### v2.3.5 — Verification Source Connectivity False Positive Fixes

Fixed:
- **Abbreviated SQ names**: source connectivity check now tests bidirectionally — if the SQ
  name (minus prefix) is a substring of the source name, the source is in-flow. Resolves
  false FAILED for patterns like `CORELOGIC_APPRAISALS` → `SQ_APPRAISALS`.
- **Lookup reference sources**: sources used as Lookup tables (e.g. `REF_COUNTY_LIMITS` via
  `LKP_COUNTY_LIMITS`) have no Source Qualifier. The check now inspects each Lookup
  transformation's `"Lookup table name"` attribute. These sources are now correctly marked
  as participating in data flow.
- **RANKINDEX orphaned port**: Rank transformations with `Number Of Ranks = 1` emit only the
  top-ranked row per group — deduplication is intrinsic; RANKINDEX never needs a downstream
  Filter connection. The false ORPHANED_PORT flag on `RANKINDEX` ports is suppressed.

### v2.3.2 — Verification Flag Auto-Handling

Fixed:
- **Source connectivity false positive**: the verification check always failed for Informatica
  source tables because they connect through Source Qualifiers (SQ_*), not directly.
  Check updated to detect `SQ_{source_name}` in the connected instance set. Renamed to
  "participates in data flow".

Added:
- **Verification flag auto-handling**: the conversion agent now receives all actionable
  Step 4 flags and addresses each in generated code. The tool converts successfully in
  the presence of verification flags rather than waiting for human intervention on
  auto-fixable issues. Per-flag rules: `INCOMPLETE_LOGIC` → pass-through + TODO stub;
  `ENVIRONMENT_SPECIFIC_VALUE` → config dict extraction; `LINEAGE_GAP` → None + TODO;
  `DEAD_LOGIC` → commented out; `REVIEW_REQUIRED` → best-effort + TODO; `UNRESOLVED_PARAMETER`
  → config placeholder; `UNSUPPORTED_TRANSFORMATION` → manual stub. Flags also carried
  into security fix rounds.

### v2.3.1 — Error Handling (Wrong File Type & Empty Pipeline) (shipped)

Fixed:
- **Wrong file type detection**: uploading a Workflow XML as the primary mapping file
  now produces an immediate BLOCKED result with a human-readable explanation. The parser
  detects `WORKFLOW` elements without `MAPPING` definitions and raises a `WRONG_FILE_TYPE`
  parse flag. Previously the pipeline silently advanced to steps 2–4 before blocking.
- **Empty mapping guard**: orchestrator explicitly checks for empty `mapping_names` after
  parsing and surfaces a descriptive error before advancing to Step 2.
- **Error message propagation**: parse flag `detail` text is stored in `state.error` and
  displayed in the UI error card. Users now see the exact reason a job blocked.
- **UI error card always renders**: the error card now appears for all FAILED/BLOCKED jobs.
  Falls back to parse flag details, then a generic step-number message. Tailored hints
  for known failure patterns: workflow-in-mapping-slot, no mappings found, missing API key.

### v2.3 — Planned

- Git integration: open a pull request with generated code directly from the UI
- Scheduler: run conversion nightly when source XMLs change in a watched directory
- Team mode: multiple reviewers, comment threads on individual flags
- Webhook notifications (Slack, Teams) on gate decisions

### v3.0 — Vision

- Continuous migration mode: monitor Informatica Designer exports and auto-convert on
  change, with diff-level PR updates
- Observability: track conversion success rate, time-to-review, and flag frequency across
  the entire Informatica estate
- Self-hosted model support: route to an on-premise LLM for air-gapped environments
- Support for PowerCenter parameter sets, session configurations, and repository-level objects

---

## 4. Pipeline Architecture

```
Upload (Mapping XML + optional Workflow XML + optional Parameter File  OR  ZIP archive)
    │
    ▼
Step 0   Session & Parameter Parse
         Auto-detect file types → Cross-reference validation → $$VAR resolution
         → Scan uploaded XML for embedded credentials (passwords in CONNECTION attrs)
         → Blocked if INVALID (mapping/session mismatch); PARTIAL if warnings
    │
    ▼
Step 1   XML Parse & Graph Extraction  [deterministic, lxml + XXE-hardened parser]
Step 2   Complexity Classification     [rule-based, objective criteria from parsed XML]
Step S2T Source-to-Target Field Map    [Claude + openpyxl Excel output]
Step 3   Documentation Generation      [Claude, Markdown]
Step 4   Verification                  [deterministic + Claude flags]
    │
    ▼
Step 5   ◼ Gate 1 — Human Review Sign-off
         APPROVE → Step 6
         REJECT  → BLOCKED (terminal)
    │
    ▼
Step 6   Target Stack Assignment       [Claude classifier]
Step 7   Code Generation               [Claude, multi-file output]
    │
    ▼
Step 8   Security Scan                 [bandit (Python) + YAML regex + Claude review]
         → Produces: APPROVED / REVIEW_RECOMMENDED / REQUIRES_FIXES
    │
    ▼
Step 9   ◼ Gate 2 — Human Security Review
         APPROVED     → auto-proceed to Step 10 (scan was clean)
         ACKNOWLEDGED → proceed to Step 10 (issues noted, risk accepted)
         REQUEST_FIX  → re-run Step 7 with findings injected → re-run Step 8 → re-present Gate 2
                        (max 2 remediation rounds; auto-proceeds to Step 10 if re-scan is clean)
         FAILED       → BLOCKED (terminal)
         [Pauses only when scan is not APPROVED]
    │
    ▼
Step 10  Logic Equivalence Check       [Stage A: Claude, XML → code rule-by-rule comparison]
         Code Quality Review           [Stage B: Claude cross-check vs. docs, S2T, parse flags]
Step 11  Test Generation               [Claude, pytest / dbt test stubs]
         → Security re-scan of generated test files (merged into Step 8 report)
    │
    ▼
Step 12  ◼ Gate 3 — Code Review Sign-off
         APPROVED  → COMPLETE
         REJECTED  → BLOCKED (terminal)
```

---

## 5. Stack Assignment Decision Matrix

Step 6 assigns one of three target stacks (or a documented hybrid) based on the
criteria below. The assignment is deterministic given the mapping characteristics —
reviewers can override at Gate 1 by adding a note, but the default follows this matrix.

| Criterion | PySpark | dbt | Python (Pandas) |
|---|---|---|---|
| **Complexity tier** | HIGH / VERY_HIGH | LOW / MEDIUM | LOW / MEDIUM |
| **Data volume** | > 50M rows | Any (SQL-bound) | < 1M rows |
| **Source type** | DB, files, streams | DB / warehouse | Files (CSV/JSON/XML), APIs |
| **Target type** | DB, data lake, files | Data warehouse | Files, APIs, lightweight DB |
| **Transformation types** | Complex joins, multi-aggregations, UDFs, procedural logic | SQL-expressible — filters, joins, aggregations, SCDs, derived fields | Simple field mapping, API calls, file format conversion |
| **SCD support** | SCD1 + SCD2 (merge/upsert) | SCD1 + SCD2 (snapshots) | SCD1 only (practical limit) |
| **Join complexity** | Multiple joiners, complex conditions, cross-dataset | Single or multi JOIN in SQL | Simple merges only |
| **Lookup handling** | Broadcast join, dynamic cache | CTE or ref() | Dict lookup / merge |
| **Expressions** | Spark functions + UDFs | SQL CASE/COALESCE/macros | Python functions |
| **Parallelism** | Native (Spark cluster) | Warehouse-native | None (single process) |
| **Test framework** | pytest + pyspark.testing | dbt tests (schema.yml) | pytest |
| **Output artifacts** | `.py` job + `requirements.txt` + YAML configs | `.sql` models + `schema.yml` + `sources.yml` + macros | `.py` script + `requirements.txt` |
| **Auto-assigned when** | ≥1 Joiner + HIGH tier, or VERY_HIGH, or volume flag | SQL-friendly transformations + warehouse target | LOW tier + file/API source or target |

**Hybrid:** Where a single mapping has sub-flows that suit different stacks, the assignment
record documents which component maps to which stack and why. Hybrid is rare — most
Informatica mappings have a dominant pattern that determines the stack clearly.

---

## 6. Security Architecture

Security is infrastructure, not a feature layer. Every file-handling path in the application
flows through `backend/security.py`.

| Threat | Defence |
|---|---|
| XML External Entity (XXE) | `safe_xml_parser()` — DTD loading and entity resolution disabled on every lxml parse |
| Zip Slip | `safe_zip_extract()` — every entry path resolved relative to virtual root before write |
| Zip Bomb | `safe_zip_extract()` — total extracted bytes and entry count capped |
| Symlink attacks | Symlink entries in ZIP silently skipped |
| Oversized uploads | `validate_upload_size()` called on every upload stream before processing |
| Dependency CVEs | 7 CVEs patched in v1.1 (python-multipart ×2, jinja2 ×3, starlette ×2); reproducible via `pip-audit` |
| Hardcoded secret key | Startup warning logged if `SECRET_KEY` is the default placeholder value |
| Unauthenticated access | Session-cookie middleware enforces login on all non-static routes |
| CORS misconfiguration | No CORS headers emitted by default (same-origin only); opt-in via `CORS_ORIGINS` env var |
| Credentials in uploaded XML | `scan_xml_for_secrets()` — checks CONNECTION/SESSION attrs for non-placeholder passwords at Step 0 |
| Insecure generated code | Step 8 — bandit (Python), YAML regex secrets scan, Claude review (all stacks) |
| Security gate bypass | Step 9 — human reviewer must explicitly approve, acknowledge, or fail before pipeline continues |
| Secrets in generated test code | Step 11 test files re-scanned and merged into Step 8 security report before Gate 3 |
| Recurring bad patterns in generated code | Security Knowledge Base — 17 standing rules + auto-learned patterns from all prior Gate 2 findings injected into every conversion prompt (v2.2) |

---

## 7. API Surface

| Method | Path | Description |
|---|---|---|
| `POST` | `/api/jobs` | Upload Mapping (+ optional Workflow + Parameter) and start pipeline |
| `POST` | `/api/jobs/zip` | Upload a single-mapping ZIP archive — files auto-detected |
| `POST` | `/api/jobs/batch` | Upload a batch ZIP (one subfolder per mapping) — starts all pipelines |
| `GET` | `/api/batches/{id}` | Get batch record + per-job summaries and computed batch status |
| `GET` | `/api/jobs` | List all jobs |
| `GET` | `/api/jobs/{id}` | Get job state |
| `GET` | `/api/jobs/{id}/stream` | SSE progress stream |
| `DELETE` | `/api/jobs/{id}` | Soft-delete job (stamps `deleted_at`; data preserved in Log Archive) |
| `POST` | `/api/jobs/{id}/sign-off` | Gate 1 decision (APPROVE / REJECT) |
| `POST` | `/api/jobs/{id}/security-review` | Gate 2 decision (APPROVED / ACKNOWLEDGED / REQUEST_FIX / FAILED) |
| `POST` | `/api/jobs/{id}/code-signoff` | Gate 3 decision (APPROVED / REJECTED) |
| `GET` | `/api/jobs/{id}/logs` | Job log (JSON or plain text) |
| `GET` | `/api/jobs/{id}/logs/download` | Download raw JSONL log |
| `GET` | `/api/jobs/{id}/s2t/download` | Download S2T Excel workbook |
| `GET` | `/api/jobs/{id}/download/{file}` | Download a generated code file |
| `GET` | `/api/jobs/{id}/tests/download/{file}` | Download a generated test file |
| `GET` | `/api/logs/registry` | All jobs with log filenames and final status |
| `GET` | `/api/logs/history` | Soft-deleted DB jobs + orphaned registry entries for the Log Archive |
| `GET` | `/api/logs/history/{job_id}` | Read a historical job log without requiring a live DB record |
| `GET` | `/api/security/knowledge` | Security KB summary: rules count, patterns count, top 10 patterns |

---

## 8. Data Model (Key Fields)

```
Batch  (v2.0)
├── batch_id       UUID
├── source_zip     Original ZIP filename
├── mapping_count  Number of mapping folders detected in the ZIP
├── created_at / updated_at
└── [status]       Computed from job statuses: running / complete / partial / failed

Job
├── job_id             UUID
├── filename           Original mapping filename
├── batch_id           UUID of parent batch (v2.0, nullable — null for standalone jobs)
├── status             JobStatus enum (PARSING → COMPLETE / BLOCKED / FAILED)
├── current_step       0–12
├── xml_content        Mapping XML (stored in SQLite)
├── workflow_xml_content   Workflow XML (v1.1, nullable)
├── parameter_file_content Parameter file (v1.1, nullable)
└── state              JSON blob — pipeline artefacts per step
    ├── session_parse_report   Step 0
    ├── parse_report           Step 1
    ├── complexity             Step 2
    ├── s2t                    Step S2T (between Steps 2 and 3)
    ├── documentation_md       Step 3
    ├── verification           Step 4
    ├── sign_off               Step 5  (Gate 1)
    ├── stack_assignment       Step 6
    ├── conversion             Step 7  (files dict: filename → code)
    ├── security_scan          Step 8
    ├── security_scan_rounds   Step 8  (v2.2) list of prior scan rounds for fix-round diff
    ├── security_sign_off      Step 9  (Gate 2)
    ├── code_review            Step 10
    ├── test_report            Step 11
    └── code_sign_off          Step 12 (Gate 3)
```

Key schema types:

```
VerificationFlag
├── flag_type             Flag category (e.g. ORPHANED_PORT, UNSUPPORTED_TRANSFORMATION)
├── severity              CRITICAL | HIGH | MEDIUM | LOW | INFO
├── description           Human-readable description of the issue
├── recommendation        Actionable guidance for the reviewer
└── auto_fix_suggestion   (optional) Specific code-level fix Claude proposes; if the
                          reviewer checks "Apply this fix" at Gate 1, the suggestion is
                          forwarded to the conversion agent at Step 7

SecurityReviewDecision  (v1.2 / v2.1)
    APPROVED              Scan was clean, or reviewer confirmed no action needed
    ACKNOWLEDGED          Issues noted and accepted as known risk (proceeds to Step 10)
    REQUEST_FIX           Re-run Step 7 with findings injected → re-run Step 8 →
                          re-present Gate 2 (max 2 rounds; auto-proceeds if clean)
    FAILED                Block pipeline permanently

SecuritySignOffRecord  (Gate 2 sign-off)
├── reviewer_name         Name of the security reviewer
├── reviewer_role         Role of the reviewer
├── review_date           Timestamp of decision (UTC, displayed in local timezone)
├── decision              SecurityReviewDecision enum value
├── notes                 Reviewer notes
└── remediation_round     (v2.1) Which REQUEST_FIX round produced this record (0 = no fix
                          requested; 1 = first round; 2 = second and final round)
```

---

## 9. Sample Files

The repository ships sample Informatica exports across three complexity tiers to allow
end-to-end testing without a live PowerCenter instance.

| Tier | Mappings | Workflow + Params? | Characteristics |
|---|---|---|---|
| Simple | 3 | Yes (all) | Single or dual source, no expressions, passthrough |
| Medium | 4 | Yes (all) | Lookups, filters, expressions, SCD1 targets |
| Complex | 2 | Yes (all) | SCD2, 3+ sources, 2+ targets, pre/post SQL, 9–11 $$VARs |

Root-level `sample_mapping.xml` / `sample_workflow.xml` / `sample_params.txt` provide a
quick single-set test. All 9 mapping sets pass Step 0 validation with
`parse_status=COMPLETE` and zero unresolved variables.

---

## 10. Success Metrics

| Metric | v1.0 Target | v1.1 Target | v1.2 Target | v1.3 Target | v2.0 Target | v2.1 Target | v2.2 Target |
|---|---|---|---|---|---|---|---|
| End-to-end pipeline completion rate | > 85% | > 90% | > 90% | > 90% | > 90% per job | > 95% per job | > 95% per job |
| S2T field coverage | ≥ 95% | ≥ 95% | ≥ 95% | ≥ 95% | ≥ 95% | ≥ 95% | ≥ 95% |
| Code review pass rate (Gate 3 first attempt) | > 70% | > 75% | > 75% | > 75% | > 75% | > 75% | > 80% |
| Security scan false-positive rate | — | < 10% | < 10% | < 10% | < 10% | < 10% | < 10% |
| Security gate review time (median) | — | — | < 5 min | < 5 min | < 5 min | < 5 min | < 5 min |
| Logic equivalence MISMATCH rate | — | — | — | < 5% | < 5% | < 5% | < 5% |
| Logic equivalence VERIFIED rate | — | — | — | > 80% | > 80% | > 80% | > 80% |
| CVE count in dependencies | 0 | 0 | 0 | 0 | 0 | 0 | 0 |
| $$VAR resolution rate (when param file provided) | — | 100% | 100% | 100% | 100% | 100% | 100% |
| Batch throughput (mappings / hour) | — | — | — | — | ≥ 3 concurrent | ≥ 3 concurrent | ≥ 3 concurrent |
| Doc truncation rate (HIGH/VERY_HIGH tier) | — | — | — | — | — | 0% | 0% |
| Security KB standing rules | — | — | — | — | — | — | 17 |
| Security KB patterns (after 10 jobs) | — | — | — | — | — | — | ≥ 20 unique |

---

## 11. Technical Constraints

- **Python 3.11+** — orchestrator uses `asyncio.TaskGroup` patterns; type annotations
  use `X | Y` union syntax
- **SQLite** — sufficient for single-instance MVP; PostgreSQL migration path via SQLAlchemy
  in v2.0
- **Claude API required** — Steps 3–4, 6–7, 8, 10–11 call the Anthropic API; no offline mode
- **bandit** — optional but strongly recommended; scan step degrades gracefully if not
  installed (pip install bandit)
- **No Docker required** — plain Python venv deployment; Dockerfile optional
- **License** — CC BY-NC 4.0; commercial use requires written permission from the author
