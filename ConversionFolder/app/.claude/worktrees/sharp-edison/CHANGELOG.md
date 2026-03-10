# Changelog

All notable changes to the Informatica Conversion Tool are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

---

## 2026-03-02 — v2.3.6 (Verification — Rank/Sorter Accuracy Improvements)

### Fixed
- **Parser captures Sorter sort keys** — `SORTKEYPOSITION` and `SORTDIRECTION` attributes
  on Sorter `TRANSFORMFIELD` elements are now stored in the port dict (`sort_key_position`,
  `sort_direction`). Previously discarded; the graph summary could not show sort order.
- **Graph summary surfaces Rank config and Sorter sort keys** — `_build_graph_summary()`
  now emits `rank_config` (Number Of Ranks, Rank TOP/BOTTOM) for every Rank transformation
  and `sort_keys` (field + direction) for every Sorter. Eliminates false REVIEW_REQUIRED
  on Rank dedup mappings where Claude could not determine whether RANKINDEX=1 = latest/earliest.
- **RANKINDEX DEAD_LOGIC suppression** — `rank_index_ports` collected from all Rank
  transformations; passed to Claude with a prompt note explaining the intrinsic-filter
  pattern. Post-filter extended to also suppress any DEAD_LOGIC Claude raises on RANKINDEX.
- **Accuracy check semantics** — Renamed "No high-risk logic patterns detected" →
  "Claude graph review completed". Check now PASSES when Claude ran (findings in FLAGS)
  and FAILS only if the API call itself errored. Previously, correctly finding HIGH_RISK
  patterns caused a FAILED check and a misleading REQUIRES_REMEDIATION overall status.

---

## 2026-03-02 — v2.3.5 (Verification — Source Connectivity False Positive Fixes)

### Fixed
- **Abbreviated SQ names no longer fail source connectivity check** — the check now tests
  bidirectionally: if the SQ name (minus the `SQ_` prefix) is a substring of the source name,
  the source is considered in-flow. Fixes false FAILED for patterns like source
  `CORELOGIC_APPRAISALS` connected through `SQ_APPRAISALS` (abbreviated naming convention).
- **Lookup reference sources no longer fail source connectivity check** — sources that feed a
  Lookup transformation (e.g. `REF_COUNTY_LIMITS` via `LKP_COUNTY_LIMITS`) have no Source
  Qualifier; the parser now checks whether the source name appears as the `"Lookup table name"`
  attribute on any Lookup transformation in the mapping. These sources are now correctly
  reported as participating in data flow.
- **`RANKINDEX` no longer flagged as ORPHANED_PORT on Rank transformations** — in Informatica's
  Sorter → Rank(N=1) deduplication pattern, RANKINDEX is an internal counter that is never
  wired downstream. The rank filter is intrinsic (the transformation only outputs the top-N
  rows per group); no explicit downstream Filter on RANKINDEX=1 is required. The false
  ORPHANED_PORT flag has been suppressed for RANKINDEX on Rank-type transformations.

### Impact
Running verification on `m_APPRAISAL_RANK_DEDUP.xml` (and similar mappings with abbreviated
SQ names or Oracle lookup reference tables) now produces an accurate report: the two FAILED
source checks and the RANKINDEX orphan flag are eliminated. The remaining flags — LINEAGE_GAP
on `LKP_COUNTY_LIMITS.IN_LIMIT_YEAR`, HIGH_RISK on conforming loan classification, and
ORPHANED_PORT for BEDROOMS/BATHROOMS/APPRAISER_ID — are genuine findings that warrant review.

---

## 2026-03-02 — v2.3.4 (Security KB — Auto-Promotion + Rule Sync)

### Security
- **Pattern auto-promotion to standing rules** — new `promote_patterns_to_rules(threshold=3)`
  function in `security_knowledge.py`. Any pattern in `security_patterns.json` that has
  appeared in 3 or more distinct Gate 2 decisions is automatically promoted to a standing
  rule in `security_rules.yaml` (as a `rule_auto_*` entry). Auto-promotion fires after every
  `record_findings()` call, so no manual intervention is needed. Promoted patterns are marked
  `promoted: true` in the JSON store so they are not processed again.
- **`_DEFAULT_RULES` now derived from `security_rules.yaml`** — the 132-line hardcoded
  duplicate list is replaced by `_load_default_rules_from_yaml()`, which reads the live YAML
  at module load time. The YAML is now the single source of truth; the in-memory fallback
  is always in sync regardless of how many rules are added.
- **`knowledge_base_stats()` exposes `auto_promoted_count`** — visible via
  `GET /api/security/knowledge` and the sidebar badge.

### How the full feedback loop now works
1. Code is generated — all 21 standing rules + top-15 learned patterns injected into prompt.
2. Gate 2 scan runs — findings recorded in `security_patterns.json`.
3. `record_findings()` auto-calls `promote_patterns_to_rules(threshold=3)`.
4. Any pattern seen in ≥ 3 jobs is promoted to a standing rule in `security_rules.yaml`.
5. Next conversion prompt includes the promoted rule as a non-negotiable requirement.
6. The pattern can no longer appear in generated code — the loop is closed.

---

## 2026-03-02 — v2.3.3 (Security Rules Expansion + Best Practices Guide)

### Security
- **5 new standing rules in `security_rules.yaml`** (17 → 21 rules). All injected
  into every conversion prompt via `build_security_context_block()` — patterns that
  caused Gate 2 findings will no longer appear in freshly generated code:
  - `rule_oracle_tls_001` (HIGH): Oracle connections must use TCPS protocol (port 2484,
    `ssl_server_dn_match: on`), never plain TCP (port 1521). Prevents credential and
    query data from being transmitted unencrypted.
  - `rule_log_injection_001` (HIGH): values interpolated into log/error messages must
    have newlines and control characters stripped before use. Applies to dbt macros,
    PySpark logging, and Python error strings.
  - `rule_macro_sql_injection_001` (HIGH): dbt macro parameters used as SQL identifiers
    must be wrapped with `adapter.quote()`. Never interpolate bare macro arguments
    into SQL strings or WHERE clauses.
  - `rule_hardcoded_business_constant_001` (MEDIUM): tax rates, fee percentages,
    thresholds, and status codes must live in dbt vars / config dicts / env vars —
    not hardcoded inline. Inline constants are an audit and regulatory change risk.

### Docs
- **Best Practices Guide — new Security section (Section 9, 6 subsections)**:
  - 9.1 Secrets & Credentials — `env_var()` rules for profiles.yml, no-default
    requirement for security-sensitive variables
  - 9.2 Oracle TCPS — correct vs. incorrect settings table (protocol, port,
    ssl_server_dn_match)
  - 9.3 SQL injection in dbt macros — `adapter.quote()`, Jinja in WHERE clauses,
    hooks with external input
  - 9.4 Log injection — what the finding means, correct sanitisation pattern with
    before/after examples
  - 9.5 Hardcoded business constants — correct config-driven patterns per stack
    (dbt vars, PySpark CONFIG dict, Python env var)
  - 9.6 How the Security Knowledge Base evolves (standing rules + auto-learned patterns)
  - Previous sections 9 and 10 renumbered to 10 and 11

---

## 2026-03-01 — v2.3.2 (Verification Flag Auto-Handling + Source SQ Fix)

### Fixed
- **Source connectivity false positive** — the verification check "Source X has outgoing
  connections" always failed in standard Informatica mappings because source tables connect
  through a Source Qualifier (SQ_*), not directly. The CONNECTOR elements use the SQ as
  `FROMINSTANCE`, never the source table itself. The check now correctly detects connection
  via `SQ_{source_name}` in `connected_instances`, or any connected SQ whose name contains
  the source name. Check renamed to "Source X participates in data flow". (`verification_agent.py`)

### Added
- **Verification flag auto-handling in conversion** — the conversion agent now receives
  all actionable verification flags from Step 4 and addresses each one directly in generated
  code. The tool no longer blocks on issues that can be handled in code; only source-level
  problems that genuinely can't be resolved automatically require human intervention.

  Per-flag code-level handling:
  - `INCOMPLETE_LOGIC` → pass-through + `# TODO [AUTO-FLAG]` stub with explanation
  - `ENVIRONMENT_SPECIFIC_VALUE` → value extracted to config dict at top of file
  - `HIGH_RISK` → implemented as documented + assertion/row-count check + warning comment
  - `LINEAGE_GAP` → target field set to `None` + `# TODO [AUTO-FLAG]` for manual trace
  - `DEAD_LOGIC` → commented out with explanation
  - `REVIEW_REQUIRED` → best-effort implementation + `# TODO` for mapping owner to confirm
  - `ORPHANED_PORT` → skipped with comment
  - `UNRESOLVED_PARAMETER` / `UNRESOLVED_VARIABLE` → added to config as `<fill_in>` placeholder
  - `UNSUPPORTED_TRANSFORMATION` → manual stub with `# TODO [MANUAL REQUIRED]`

  Flags are injected into both the primary conversion (Step 7) and security remediation
  rounds (Gate 2 REQUEST_FIX). (`conversion_agent.py`, `orchestrator.py`)

---

## 2026-03-01 — v2.3.1 (Error Handling — Wrong File Type & Empty Pipeline)

### Fixed
- **Wrong file type detection** — uploading a Workflow XML as the primary mapping file
  now produces an immediate, actionable BLOCKED result instead of silently spinning
  through steps 2–4 with no explanation. The parser detects when the XML contains
  `WORKFLOW` elements but no `MAPPING` definitions and raises a `WRONG_FILE_TYPE`
  parse flag with a clear human-readable message. (`parser_agent.py`)
- **Empty mapping guard** — the orchestrator now explicitly checks for an empty
  `mapping_names` list after parsing completes (even when `parse_status` is not
  FAILED) and surfaces a descriptive error before advancing to Step 2. (`orchestrator.py`)
- **Error message propagation** — parse flag `detail` text is now stored in
  `state.error` and surfaced in the UI error card, so users see exactly why a job
  blocked rather than a generic "Blocked on Step 1" message. (`orchestrator.py`)
- **UI error card always renders** — the error card now appears for all FAILED/BLOCKED
  jobs regardless of whether `state.error` is set. Falls back to parse flag details,
  then to a generic step-number message. Tailored actionable hints are shown for known
  failure patterns: workflow-in-mapping-slot, no mappings found, missing API key.
  (`index.html`)

---

## 2026-03-01 — v2.3.0 (Code Review Hardening — Security, Reliability, Config)

Addresses all immediate and short-term items from the external code review.

### Security
- **bcrypt password hashing** — replaced `hashlib.sha256` (fast, brute-forceable) with
  `bcrypt` (work factor 12, deliberately slow). The app password is hashed once at
  startup using a fresh salt; subsequent logins use `bcrypt.checkpw()` which includes
  constant-time comparison. Added `bcrypt==4.2.1` to requirements. (`auth.py`)

### Reliability
- **Claude API retry with exponential backoff** — new `agents/retry.py` module wraps
  every Claude API call with up to 3 attempts and exponential backoff (10 s base +
  jitter). Retries on `RateLimitError` (429), `InternalServerError` (500),
  `APIConnectionError`, and any `APIStatusError` with status 429/500/502/503/529.
  Applied to documentation Pass 1 & Pass 2 and the verification quality-check call.
  Non-retryable errors (invalid API key, bad request) still raise immediately.
- **Input validation for empty/malformed XML** — the `/api/jobs` upload endpoint now
  rejects empty files and files that do not start with `<` before creating a job record
  or spending any API tokens. Returns HTTP 400 with a descriptive message.
- **Database query indices** — four `CREATE INDEX IF NOT EXISTS` statements added to
  `init_db()`: `idx_jobs_status`, `idx_jobs_created_at`, `idx_jobs_batch_id`,
  `idx_jobs_deleted_at`. Applied idempotently on every startup; existing databases gain
  indices automatically on the next restart.

### Observability
- **`GET /api/health` endpoint** — liveness + readiness probe. Returns
  `{"status": "ok"|"degraded", "version": "2.3.0", "db": "ok"|"error: ...", "uptime_seconds": N}`.
  HTTP 200 when healthy, HTTP 503 when the database is unreachable. Suitable for
  Docker `HEALTHCHECK`, load balancer probes, and uptime monitors.

### Configuration
- **Pydantic `Settings` class** — new `backend/config.py` centralises all 20+
  environment variable reads into a single `Settings(BaseSettings)` instance. Every
  module now imports `from .config import settings` instead of calling
  `os.environ.get()` directly. The `.env` file is loaded automatically. Added
  `pydantic-settings==2.6.1` to requirements. Variables covered: `ANTHROPIC_API_KEY`,
  `CLAUDE_MODEL`, `APP_PASSWORD`, `SECRET_KEY`, `SESSION_HOURS`, `HOST`, `PORT`,
  `SHOW_DOCS`, `CORS_ORIGINS`, `HTTPS`, `LOG_LEVEL`, `MAX_UPLOAD_MB`,
  `MAX_ZIP_EXTRACTED_MB`, `MAX_ZIP_FILE_COUNT`, `DB_PATH`, `JOB_RETENTION_DAYS`,
  `CLEANUP_INTERVAL_HOURS`, `RATE_LIMIT_JOBS`, `RATE_LIMIT_LOGIN`,
  `BATCH_CONCURRENCY`, `DOC_MAX_TOKENS_OVERRIDE`, `VERIFY_TIMEOUT_SECS`.

---

## 2026-03-01 — v2.2.2 (Verification & Documentation Token Efficiency)

### Changed
- **Verification agent decoupled from documentation** — the verification step no longer
  reads or string-matches against the documentation text. Documentation is human-facing
  and reviewed visually by the reviewer at Gate 1; the verification agent's job is to
  check the mapping graph. (`1211aaf`)
  - Removed all doc string-matching completeness checks (transformation names, source
    names, target names, port expressions, parameters — all previously checked as
    substring presence in `documentation_md`). These checks caused cascading false
    failures whenever docs were truncated and were checking the wrong artefact.
  - Removed the doc-based Claude quality review call (`documentation_md[:15000]`).
  - Replaced with **graph structural checks**: every transformation participates in
    the data flow, every source has outgoing connectors, every target receives incoming
    connectors.
  - Claude quality check now reads a compact **graph summary** (expressions, SQL
    overrides, filter/join/lookup conditions, connector topology — capped at 20k chars)
    and flags real conversion risks: hardcoded environment values, high-risk logic,
    incomplete conditionals, dead logic, lineage gaps.
  - `accuracy_checks` in the report now reflect code-quality findings from graph
    analysis rather than documentation text review.

- **Documentation agent: tier-based depth** — documentation scope now scales with
  mapping complexity instead of always running the full two-pass treatment. (`1211aaf`)
  - **LOW tier** (< 5 transformations): single pass — Overview + Transformations +
    Parameters only. No field-level lineage section (irrelevant for simple mappings).
  - **MEDIUM / HIGH / VERY_HIGH**: two passes as before, but with the two improvements
    below.

- **Documentation Pass 2 no longer re-sends graph JSON** — Pass 1's output already
  contains all transformation detail (every port, expression, condition verbatim).
  Removing the redundant `graph_json` from the Pass 2 prompt eliminates ~80k chars of
  input tokens, roughly halving Pass 2 input cost and giving far more headroom for
  Pass 2 output before truncation. (`1211aaf`)

- **Field-level lineage scoped to non-trivial fields** — the Pass 2 lineage prompt now
  distinguishes between field types. Derived, aggregated, lookup-result, conditional,
  and type-cast fields get full individual traces. Passthrough and rename-only fields
  are grouped into a single summary table. This eliminates the dominant source of
  Pass 2 token bloat on wide mappings with many simple columns. (`1211aaf`)

---

## 2026-03-01 — v2.2.1 (Documentation Sentinel & Truncation Fixes)

### Fixed
- **`<!-- DOC_COMPLETE -->` sentinel leaking into UI and agent prompts** — both
  `DOC_COMPLETE_SENTINEL` and `DOC_TRUNCATION_SENTINEL` are now stripped from
  `documentation_md` *before* it is stored in state, so the raw HTML comment
  never appears in the rendered documentation card, the Markdown/PDF report, or
  any downstream agent prompt (verification, code review, test generation). (`cf3f68d`)
- **Hard pipeline failure on documentation truncation changed to Gate 1 warning** —
  previously the pipeline immediately set the job to `failed` when the documentation
  agent hit its token limit mid-output. Per agreed behaviour ("send a note if we ran
  out of tokens"), the pipeline now continues: `doc_truncated=True` is stored on the
  state object, a HIGH (non-blocking) `DOCUMENTATION_TRUNCATED` VerificationFlag is
  injected into the Step 4 verification report so the human reviewer sees it at Gate 1,
  and the Step 3 card in the UI shows an orange border, a TRUNCATED badge, and an inline
  warning banner explaining the situation. (`cf3f68d`)

---

## 2026-03-01 — v2.2 (Security Knowledge Base, Log Archive, Soft Delete, Gate 2 Fixes)

### Added
- **Security Knowledge Base** — two-layer system that makes every future code generation
  smarter from accumulated job history. (`d0ceb0f`, `99c9453`)
  - `security_rules.yaml` (committed to source): 17 standing rules covering credentials,
    SQL injection, eval/exec, subprocess, XXE, Zip Slip, weak hashing, insecure random,
    PII logging, TLS bypass, temp files, and 5 dbt/Snowflake-specific rules derived from
    real job findings (profiles.yml secrets, post-hook SQL injection, env_var without
    defaults, Jinja code execution, truncated SQL models).
  - `security_patterns.json` (runtime state): auto-learned patterns built from every
    Gate 2 APPROVED / ACKNOWLEDGED decision. Each approved job contributes its findings
    with occurrence counts — the most common issues get the most emphasis in future prompts.
  - `build_security_context_block()`: prepends a MANDATORY SECURITY REQUIREMENTS block to
    every conversion prompt (first generation and REQUEST_FIX reruns). KB read failures
    never block a conversion.
  - `GET /api/security/knowledge`: returns `rules_count`, `patterns_count`, `top_patterns`.
  - Sidebar badge: "🛡 Security KB: N rules · M learned patterns" shown on page load.
  - Historical backfill: all 10 findings from existing job logs seeded into patterns store.
- **Security scan round history + fix-round diff UI** — orchestrator now preserves each
  security scan as an entry in `security_scan_rounds` before overwriting. Gate 2 shows a
  ✅ Fixed / ⚠️ Remains / 🆕 New comparison table after each REQUEST_FIX round so
  reviewers can see exactly what was addressed. (`6045476`)
- **Log Archive sidebar** — collapsible "Log Archive" section in the job list shows
  historical jobs whose DB records are gone but whose log files are still on disk.
  Clicking any entry opens a read-only log panel (`GET /logs/history`,
  `GET /logs/history/{job_id}`). (`9f5cf33`)
- **Soft delete** — clicking 🗑 now stamps `deleted_at` on the job record instead of
  issuing `DELETE FROM jobs`. Deleted jobs disappear from the active list but their
  log files, registry entries, and DB records are preserved. Soft-deleted jobs appear
  in the Log Archive. DB auto-migrates via `ALTER TABLE jobs ADD COLUMN deleted_at TEXT`
  on next startup. (`4156a64`)
- **BATCH_CONCURRENCY env var** — batch semaphore is now configurable via
  `BATCH_CONCURRENCY` (default `3`). Added to `.env.example` and README. (`f50dab7`)
- **Gate 2 REQUEST_FIX remediation loop** — new fourth Gate 2 decision triggers a
  two-round remediation loop: Step 7 re-generates code with all security findings
  injected as mandatory fix context → Step 8 re-scans → Gate 2 re-presents. If the
  re-scan is clean the pipeline auto-proceeds. Max 2 rounds enforced. UI shows a
  "🔧 Request Fix & Re-scan" button and a round indicator. (`85515f2`)
- **E2E mortgage batch test set (Stages 2–6)** — six-stage synthetic mortgage pipeline
  covering all three target stacks and all complexity tiers: (`afb9b66`)
  - `02_credit_bureau_lookup` — MEDIUM / PySpark / unconnected lookup / SCD1
  - `03_underwriting_rules` — HIGH / PySpark / 3-source join / 4-group Router
  - `04_loan_pricing` — MEDIUM / dbt / rate sheet join / APR calc
  - `05_scd2_loan_status` — HIGH / PySpark / full-outer join / Sequence Generator / SCD2
  - `06_regulatory_reporting` — MEDIUM / Python / HMDA derivation / Aggregator / flat file
- **Stack assignment decision matrix** — full table added to PRD §5 and condensed
  version to README covering all assignment criteria. (`7ed7972`)

### Fixed
- **Security findings injection passing blank descriptions** — `conversion_agent.py` was
  reading `finding_type` / `location` / `description` which do not exist on the
  `SecurityFinding` model. Correct fields (`test_name` / `filename` / `text`) now used;
  offending code snippet and line number also injected so Claude has full context to fix
  findings. (`9fa54b7`)
- **Gate 2 approval buttons missing after REQUEST_FIX regen** — `signedOff` was set to
  `true` whenever `state.security_sign_off` existed (including when `decision =
  "REQUEST_FIX"`), hiding the decision buttons after regen completed. Fixed by excluding
  REQUEST_FIX from the signedOff calculation. (`1cb3f16`)
- **Gate 2 security card rendering during Steps 7/8** — the security review card was
  visible while regen was running because `|| state.security_sign_off` was truthy.
  Card now only renders when `status === 'awaiting_security_review' || signedOff`.
  (`997965d`)
- **`NameError: name 'os' is not defined`** at startup — `import os` was missing from
  `routes.py`. (`early commit`)
- **Bandit not found despite being installed** — `subprocess.run(["bandit", ...])` relied
  on the shell PATH which is not set when the server starts as a service on macOS.
  Changed to `[sys.executable, "-m", "bandit", ...]` so bandit always resolves via the
  same Python interpreter running the app. (`4cab743`)
- **`loadJobs()` silently hiding live jobs** — `Promise.all` caused the entire function
  to fail if `/api/logs/history` threw any error. History fetch moved to a separate inner
  `try/catch`. (`4156a64`)
- Log files were permanently deleted when a job was removed. Log files are now kept on
  disk; only the `registry.json` entry is cleaned up. (`a861ccc`)

### Docs
- All docs updated to v2.2: README, PRD, CHANGELOG, SECURITY.md.

---

## 2026-02-27 — v2.1 (Two-Pass Documentation, Step 3 Heartbeat)

### Added
- **Two-pass documentation strategy** — Step 3 now runs two Claude passes: Pass 1
  extracts structure (sources, targets, transformations), Pass 2 enriches with business
  logic and edge cases. Improves completeness on complex HIGH/VERY_HIGH mappings. (`8182e1d`)
- **Step 3 completeness gate** — pipeline fails at Step 3 if critical documentation
  fields (source table, target table, transformation logic) are missing. (`2b9ec60`)
- **Extended output beta** — Step 3 always uses 64K output tokens with the extended
  output beta flag. Tier-based token budgeting removed. (`7825fa6`, `7b130fe`)
- **Step 3 heartbeat** — orchestrator emits a 30-second SSE heartbeat during the
  documentation pass so the UI never appears frozen on large mappings. (`1d594c3`)

### Fixed
- Per-pass timeout removed from `documentation_agent` — the Claude call is async and
  never blocks the event loop. One observed run took 18 minutes and completed correctly;
  the timeout was killing valid jobs. (`6d7be91`)
- `recover_stuck_jobs()` was missing 4 transient statuses: `assigning_stack`,
  `security_scanning`, `reviewing`, `testing`. Jobs restarted in those states now
  correctly recover to `failed`. (`55c25d6`)
- Fixed `extra_headers` usage for extended output beta (SDK compatibility). (`b268afe`)
- Added missing `logger` injection to `documentation_agent`. (`d5b35d4`)
- Timestamps in the UI now display in local timezone instead of raw UTC. (`a910160`)

### Docs
- README and PRD updated to v2.1. (`1635112`)
- All documentation updated to reflect Step 3 heartbeat and two-pass strategy. (`623be34`, `d4771fd`)

---

## 2026-02-26 — v2.0 (Batch Conversion, Security Remediation Guidance)

### Added
- **Batch conversion** — users can upload a ZIP containing multiple XML mapping files.
  Each mapping runs as an independent job through the full pipeline, gated concurrently
  by a semaphore (default 3). Batch group UI in the sidebar. (`43885c6`)
- **Actionable remediation guidance** — security scan findings now include a structured
  remediation field per finding with severity, location, description, and a specific
  code-level fix instruction. (`cddbd84`)

### Changed
- GitHub Actions CI now only sends notifications on scan failure; success runs are
  silent. (`b0f0602`)

### Docs
- Step numbering and content gaps fixed across all docs. (`2762d83`)

---

## 2026-02-25 — v1.3 (XML-Grounded Equivalence Check)

### Added
- **XML-grounded logic equivalence check** — the code review agent (Step 10) now
  verifies that each transformation in the original XML produces the same result in the
  generated code. Differences flagged as equivalence failures. (`d77aef0`)

---

## 2026-02-24 — v1.2 (Security Review Gate, Rate Limiting, UI Polish)

### Added
- **Human security review gate (Gate 2 / Step 9)** — after the automated security scan
  (Step 8), a reviewer sees all findings and decides APPROVED / ACKNOWLEDGED / FAILED.
  Pipeline only proceeds to Step 10 after a gate decision. (`7f4f97c`)
- **MD and PDF report download** — job panel includes download buttons for the
  generated Markdown report and a browser-rendered PDF. (`b308bdc`)
- **Step 8 Security Scan UI card** — dedicated card in the job panel; all step numbers
  aligned to match backend. (`416f1c9`)
- **Rate limiting** — token-bucket rate limiter on upload endpoints via FastAPI Depends
  injection (replaced incompatible `slowapi`). (`f8e76fd`, `f9ba361`)
- **Health endpoint** (`/health`), job cleanup cron, `SECURITY.md` disclosure policy.
  (`f9ba361`)
- **GitHub Actions security CI + Dependabot**. (`cad148c`)
- **Zip Slip fix** — ZIP extractor now validates all paths stay within the target
  directory. Security test suite added. (`1f26aea`)
- **v1.1 — Session & Parameter Support** — pipeline accepts optional workflow XML and
  parameter files alongside the mapping XML. (`6db6f00`)
- **Paired workflow + parameter files** for all 9 sample mappings. (`22abd68`)

### Changed
- Gate 3 simplified to binary APPROVED / REJECTED — REGENERATE option removed. (`3b2a60a`)
- Pipeline promoted to 11 steps; security scan is a full Step 8. (`276678d`)
- PDF export fixed: opens a clean print window instead of `window.print()`. (`6967642`)
- Step progress indicator shows all 11 steps with Security and Quality as distinct dots.
  (`6ec938d`)
- Job history section always collapsible; smart default; step counter capped at 10.
  (`7198406`)

### Docs
- All docs updated for v1.2 12-step pipeline and security review gate. (`7e37f45`, `c34b62c`)
- Security hardening, scan gaps, PII detection documented. (`276678d`, `005fef9`)

---

## 2026-02-23 — v1.0 (Initial Release)

### Added
- **Initial commit** — 10-step AI pipeline converting Informatica PowerCenter XML
  mappings to PySpark, dbt, or Python/Pandas. FastAPI backend, single-file HTML
  frontend, SQLite job store, per-job JSONL logging, Claude API integration. (`fbb6311`)
- Full README with 10-step pipeline documentation and install guide. (`df6bdc4`)

---

*Commit hashes reference the short SHA for each change. Run `git show <hash>` for full diff.*

## v2.4.0 — 2026-03-02

### feat: Pre-conversion Mapping Manifest with Reviewer Override Loop

**Why it exists:** Iterative post-conversion fixes caused by naming convention surprises
(e.g. a developer naming an SQ something unrelated to its source) will now be caught
*before* conversion runs rather than discovered in the output.

**What it does:**

Step 1.5 — runs immediately after XML parsing (non-blocking):
- Analyses the parser graph dict and scores every source→SQ/Lookup connection:
  - HIGH — direct connector, exact SQ_SOURCENAME, or Lookup reference
  - MEDIUM — abbreviated SQ name match (SQ stem found in source name)
  - LOW — weak/partial token overlap, orphaned ports, lineage gaps
  - UNMAPPED — no connection found at all
- Surfaces expressions (to convert), lookups, and unresolved parameters
- Writes a three-sheet xlsx:
  - **Summary** — per-mapping counts, review_required flag
  - **Full Lineage** — all items colour-coded GREEN/AMBER/YELLOW
  - **Review Required** — only LOW/UNMAPPED rows + editable **Reviewer Override** column

**The reviewer loop:**
1. Reviewer opens the manifest xlsx, fills in the Override column for yellow rows
2. Re-uploads the annotated xlsx
3. Conversion agent reads overrides via `load_overrides()` and injects them into the
   conversion prompt — reviewer answers take precedence over all tool inferences

**Files changed:**
- `app/backend/agents/manifest_agent.py` — new (590 lines)
- `app/backend/models/schemas.py` — ManifestConfidence, ManifestItemType,
  ManifestItem, ManifestReport, ManifestOverride
- `app/backend/agents/conversion_agent.py` — manifest_overrides param +
  `_build_manifest_override_section()`
- `app/backend/orchestrator.py` — Step 1.5 wired in, overrides passed to convert()

## v2.4.3 — 2026-03-02

### fix: State compression, queue leak, DB indices, sign-off 500

- **GAP #3** — State compressed with zlib (~50x). pipeline_log capped at 300 entries. manifest_xlsx no longer stored in state — served via `/jobs/{id}/manifest.xlsx` API endpoint on demand.
- **GAP #11** — SSE stream `stream_progress()` now cleans up its queue via `BackgroundTask` when the connection closes.
- **GAP #12** — DB indices guaranteed to apply on every startup.
- **Sign-off bug** — Frontend now checks `res.ok` before `res.json()` — no more "Unexpected token I" crash masking the real server error.

## v2.4.4 — 2026-03-02

### fix: Atomic batch, deep copy, state validation, timeout watchdog

- **GAP #4** — `create_batch_atomic()` creates batch + all jobs in one SQLite transaction. On failure, everything rolls back — no orphaned jobs.
- **GAP #2** — All resume functions deep-copy state before mutating. Partial writes on crash no longer corrupt persisted state.
- **GAP #6** — Required state keys validated before Pydantic construction on every resume. Missing keys return a clear FAILED message instead of unhandled KeyError.
- **GAP #16** — `run_watchdog_loop()` polls every 60s. Jobs stuck in active pipeline statuses for 45+ minutes are automatically marked FAILED.

## v2.4.5 — 2026-03-02

### fix: Claude output validation, model deprecation, download guard, graceful shutdown

- **GAP #7** — `_validate_conversion_files()` checks every generated file: empty, placeholder-only (>60% TODOs), Python syntax errors, missing SparkSession/SELECT. Issues added to ConversionOutput.notes.
- **GAP #13** — Startup model probe: 404 from Anthropic logs an ERROR with the deprecated model string and instructions to update .env.
- **GAP #14** — Download endpoint enforces extension allowlist (.py .sql .yaml .yml .txt .md .json .sh). Path traversal stripped before check.
- **GAP #15** — Graceful shutdown: lifespan cancels all in-flight asyncio pipeline tasks on SIGTERM/SIGINT.
