# Changelog

All notable changes to the Informatica Conversion Tool are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

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
