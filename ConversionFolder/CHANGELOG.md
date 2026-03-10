# Changelog

All notable changes to the Informatica Conversion Tool are documented here.

Format follows [Keep a Changelog](https://keepachangelog.com/en/1.0.0/).

---

## [Unreleased]

---

## [2.15.0] — Time-Based Manifest Scheduler

### Added

#### `app/backend/scheduler.py` (new module)

**Time-based cron scheduler** that materialises `*.manifest.json` files into
`WATCHER_DIR` when scheduled cron expressions fire.  The existing manifest file
watcher then processes them as normal — no changes to the watcher or pipeline.

- `run_scheduler_loop(schedule_dir, watcher_dir, poll_interval)` — async background
  loop; scans `SCHEDULER_DIR` for `*.schedule.json` files every
  `SCHEDULER_POLL_INTERVAL_SECS` seconds
- `_cron_matches(cron_expr, dt)` — pure function; expands 5-field cron expressions
  to sets (handles `*`, `*/n`, `a-b`, `a-b/n`, `a,b,c`) and returns True/False;
  DOW 0 and 7 both treated as Sunday; Python `isoweekday() % 7` mapping applied
- `_expand_field(field, min_val, max_val)` — low-level cron field expander;
  raises `ValueError` on malformed syntax
- `_now_in_tz(tz_name)` — returns current datetime in the given IANA timezone
  via `zoneinfo` (Python 3.9+ stdlib); falls back to UTC with a warning log on
  unknown timezone names
- `_read_schedule(path)` — validates a schedule file: type-checks all fields,
  validates cron syntax at read time, skips `enabled=false` schedules silently
- `_materialise(schedule_name, schedule, watcher_dir)` — writes the manifest
  payload as `<safe_label>_<YYYYMMDD_HHMMSS_ffffff>.manifest.json` into
  `watcher_dir`; injects `label` from schedule → manifest → schedule filename
  stem precedence chain; `_SAFE_LABEL_RE` uses `re.ASCII` flag
- `_tick(sched_path, watcher_path, last_fired)` — single poll iteration;
  duplicate-fire guard tracks `(hour, minute)` per schedule stem to prevent
  double-materialisation when poll interval < 60s or server catches up after pause

**Schedule file format** (`*.schedule.json` in `SCHEDULER_DIR`):
```json
{
    "version":  "1.0",
    "cron":     "0 2 * * 1-5",
    "timezone": "America/New_York",
    "label":    "Customer Pipeline Nightly",
    "enabled":  true,
    "manifest": { ... manifest payload ... }
}
```

#### `app/backend/config.py`

- `scheduler_enabled: bool = False` — enables the scheduler background task
- `scheduler_dir: str = ""` — required when `scheduler_enabled=True`
- `scheduler_poll_interval_secs: int = 60` — evaluation frequency
- Version bumped to `2.15.0`

#### `app/main.py`

- Scheduler background task started in `lifespan` when `SCHEDULER_ENABLED=true`
- Guards: logs an error and does not start if `SCHEDULER_DIR` is unset, or if
  `WATCHER_ENABLED` is false / `WATCHER_DIR` is unset (scheduler requires the
  watcher to process the materialised manifests)
- Background task registered in `_bg_tasks` for clean cancellation on shutdown

#### `app/.env.example`

- New `# ── Time-based scheduler (v2.15.0)` section with full format example,
  cron expression reference, and three commented config variables

#### `docs/USER_GUIDE.md`

- New **Time-based scheduled conversions (v2.15.0)** section with how-it-works
  walkthrough, full schedule file format, cron expression reference table with
  examples, output directory structure, enabling instructions, and optional tuning
- New **Time-based scheduler** row group in the Configuration table
- Version bumped to 2.15.0

#### `docs/TESTING_GUIDE.md`

- Version bumped to 2.15.0
- **Preparing Golden Reference Data** section added (2c2140c — see that commit)

---

## [2.14.1] — Project-Group Manifest, Named Output Directories, Security Hardening

### Security (post-release review — commits ef1fc8f, 27ba012, 83177d6)

- **Path traversal prevention** — `_assert_plain_filename()` added to `watcher.py`;
  called on every user-supplied filename in the manifest (per-mapping entries,
  top-level `workflow`, top-level `parameters`, per-entry overrides).  Rejects
  filenames containing path separators or absolute paths (e.g. `"../../etc/shadow.xml"`
  passes extension check but is now caught before `root / fname` is constructed)
- **Defense-in-depth in `job_exporter.py`** — `job_output_dir()` and
  `_update_batch_index()` both validate `watcher_output_dir` / `watcher_mapping_stem`
  from DB state for path separators before constructing output paths; fall back
  to `job_id` path with a warning log if check fails
- **`config.py`** — `host = "0.0.0.0"` annotated `# nosec B104` with rationale
- **Bare `except: pass` replaced** — DB update failure in `_run_with_semaphore`
  now logged at DEBUG; S2T fallback failure in `job_exporter` now logged at DEBUG
- **`re.ASCII` flag** added to `_make_output_dir_name` regex so Unicode labels
  cannot produce non-ASCII characters in directory names
- **Type validation** — top-level `workflow` and `parameters` fields in
  `_read_manifest` now explicitly checked as `str` before extension validation
- **Bandit result: 0 issues** across all modified files

### Fixed (post-release design review — commit 27ba012)

- **Duplicate mapping filename guard** — `_read_manifest` raises `ValueError`
  immediately if any two entries in `"mappings"` resolve to the same filename;
  prevents silent `file_cache` collision and output directory overwrite
- **Manifest stem stripping** — fallback output dir name now strips `.manifest.json`
  as a whole suffix (`q1_pipeline` not `q1_pipeline.manifest`); applies to both
  `batch_dir_name` and the UI batch label
- **`batch_index.json`** — `job_exporter._update_batch_index()` writes
  `OUTPUT_DIR/<batch_dir>/batch_index.json` mapping `mapping_stem → job_id`;
  read-modify-write so entries accumulate as each mapping clears Gate 3;
  non-fatal on failure; UI-submitted jobs unaffected
- **`_move_to()` returns destination path** — all four call sites in `_poll_once`
  now derive the `.error` sidecar filename from the actual moved path (including
  UTC timestamp prefix) so manifest and sidecar are paired by name in `failed/`

---

## [2.14.1-base] — Project-Group Manifest with Named Output Directories

### Changed

#### `app/backend/watcher.py` — Option A manifest schema + label field

**Manifest schema updated to support per-mapping file overrides.**

Each entry in the `"mappings"` array can now be either a plain filename string
(inherits top-level defaults) or an object with its own `"workflow"` and
`"parameters"` fields that override the top-level values for that mapping only:

```json
{
    "version":    "1.0",
    "label":      "Customer Data Pipeline — Q1 2026",
    "mappings": [
        "m_customer_load.xml",
        { "mapping": "m_appraisal_rank.xml", "workflow": "wf_appraisal.xml" }
    ],
    "workflow":   "wf_default.xml",
    "parameters": "params_prod.xml"
}
```

**New `"label"` field** — optional human-readable name for the batch.  Drives
the output folder name and the batch label shown in the UI.  If omitted, the
manifest filename stem is used.

**Output directory naming** — watcher batches are now written to:
```
OUTPUT_DIR/<label>_<YYYYMMDD_HHMMSS_ffffff>/<mapping_stem>/
```
Microsecond timestamp is always appended so re-runs never overwrite prior
output and folders sort chronologically.  UI-submitted jobs still use the
existing `OUTPUT_DIR/<job_id>/` path — no change to non-watcher behaviour.

**`_read_manifest()`** — validates and normalises each entry via new
`_resolve_entry()` helper; per-entry overrides are merged with top-level
defaults at parse time so the rest of the pipeline never has to re-apply them.

**`_make_output_dir_name(label, manifest_stem)`** — new helper that sanitizes
the label for filesystem use and appends `strftime("%Y%m%d_%H%M%S_%f")`
(microseconds) to the base name.

Backward compatibility retained: v2.14.0 singular `"mapping"` field and
v2.14.1 `"mappings"` array of plain strings both continue to work.

#### `app/backend/job_exporter.py` — named output paths for watcher batches

`job_output_dir()` now accepts an optional `state` dict.  When the state
carries `watcher_output_dir` and `watcher_mapping_stem` (set by the watcher at
job creation time via `db.update_job()`), the export path becomes:
```
OUTPUT_DIR/<watcher_output_dir>/<watcher_mapping_stem>/
```
Watcher batches submitted overnight are immediately navigable by project name
and mapping name without querying the database.

#### `.env.example` — updated manifest documentation

Manifest example updated to show `"label"` field, per-mapping override syntax,
and output directory structure with example path.

#### `docs/USER_GUIDE.md` — updated scheduled ingestion section

Manifest section rewritten to show both the simple form (shared workflow/params)
and the per-mapping override form, with a field reference table and an output
directory tree diagram.

---

## [2.14.0] — Manifest-Based File Watcher

### Added

#### `app/backend/watcher.py` — manifest-driven scheduled ingestion

A background asyncio task that polls a configured directory for
`*.manifest.json` files and automatically submits conversion jobs without
requiring a UI upload.

**Manifest file format**

Drop a JSON file (any name ending in `.manifest.json`) into `WATCHER_DIR`:

```json
{
    "version":       "1.0",
    "mapping":       "m_appraisal_rank.xml",
    "workflow":      "wf_appraisal.xml",
    "parameters":    "params.xml",
    "reviewer":      "Jane Smith",
    "reviewer_role": "Data Engineer"
}
```

`mapping` is required; all other fields are optional. All referenced XML files
must live in the same directory as the manifest.

**Lifecycle**

1. Watcher polls `WATCHER_DIR` every `WATCHER_POLL_INTERVAL_SECS` seconds (default: 30).
2. On finding a `.manifest.json`, validates the JSON schema and checks all
   referenced files are present on disk.
3. If all files present: reads content → calls `db.create_job()` →
   launches pipeline via `orchestrator.run_pipeline()` (same path as API route) →
   moves manifest to `WATCHER_DIR/processed/<timestamp>_<name>.manifest.json`.
4. If files missing: logs a warning, leaves manifest in place, retries next poll.
   After `WATCHER_INCOMPLETE_TTL_SECS` seconds (default: 300) the manifest is
   moved to `WATCHER_DIR/failed/` with a `.error` sidecar explaining what was missing.
5. If invalid JSON or bad schema: moves immediately to `WATCHER_DIR/failed/` with
   a `.error` sidecar. No retry.

**UI behaviour**

The existing 5-second job list poll (`loadJobs`) picks up watcher-submitted jobs
automatically — no UI changes required. SSE streaming works identically to
manually submitted jobs. Gate reviews (Gate 1, 2, 3) still require human action.

**Error isolation**

Poll errors never kill the watcher loop. Each manifest is processed independently.
Server restart recovers cleanly — the existing stuck-job recovery logic in
`main.py` handles any in-flight jobs.

#### Config additions (`app/backend/config.py`)

| Variable | Default | Description |
|---|---|---|
| `WATCHER_ENABLED` | `false` | Set to `true` to activate the watcher |
| `WATCHER_DIR` | `""` | Absolute path to the directory to watch |
| `WATCHER_POLL_INTERVAL_SECS` | `30` | Seconds between directory polls |
| `WATCHER_INCOMPLETE_TTL_SECS` | `300` | Seconds before an incomplete manifest is failed |

#### `.env.example` updated

Full manifest format documentation and all four watcher config variables added
with commented-out defaults and inline usage notes.

### Changed

- `app/main.py` — watcher task launched in `lifespan()` alongside existing
  cleanup and watchdog loops; logs a clear message if watcher is disabled or
  misconfigured (enabled but no `WATCHER_DIR`).

---

## [2.13.0] — Data-Level Equivalence Testing

### Added

#### Component A — Expression boundary tests (Step 9 — Test Agent)

`test_agent.py` now scans transformation expressions from the parsed graph for
five high-risk categories and generates parametrized pytest tests for each detected
category, delivered as `tests/test_expressions_{mapping}.py` alongside every job.

**Detection patterns**

| Category | Pattern matched | Key risk |
|---|---|---|
| IIF | `IIF\s*\(` | NULL condition evaluates FALSE branch — not NULL propagation |
| DECODE | `DECODE\s*\(` | Case-sensitive; NULL falls through to default |
| Date functions | `ADD_TO_DATE\|DATE_DIFF\|TO_DATE\|TRUNC` | Month-end rollover; NULL propagation |
| String functions | `SUBSTR\|INSTR\|LTRIM\|RTRIM\|LPAD\|RPAD` | SUBSTR is 1-indexed in Informatica, 0-indexed in Python |
| Aggregations | `SUM\|AVG\|COUNT\|MAX\|MIN` | Empty partition must return NULL, not 0 |

**Generated test structure**

Each detected category produces:
- A `_helper()` stub with a `FILL IN` comment — data engineers replace this with
  a call to the actual translated function in the generated code.
- A `@pytest.mark.parametrize` test covering normal values, boundary values, and
  NULL inputs.
- A docstring explaining the specific Informatica semantic being tested.

Tests require no database connection or Informatica environment — only `pytest`
(and `pyspark` for Spark jobs).

#### Component B — Golden CSV comparison script (Step 9 — Test Agent)

A self-contained `tests/compare_golden.py` script is generated with every job.
Data engineers run it **outside the tool** after capturing Informatica output and
running the generated code against the same source rows.

**Usage**

```bash
python tests/compare_golden.py \
  --expected informatica_output.csv \
  --actual   generated_code_output.csv \
  [--threshold 99.5] \
  [--key-columns ACCOUNT_ID,LOAD_DATE] \
  [--ignore-columns AUDIT_TIMESTAMP,ETL_RUN_ID]
```

**Script capabilities**

- Row count comparison with direction hint (fan-out vs data loss)
- Schema comparison (missing / extra columns)
- Key-column-based row join (or positional alignment if no keys)
- Field-by-field value comparison with float tolerance (`rtol=1e-5`)
- Null rate per column
- Mismatch sample (up to 20 rows per column)
- Pass / fail against configurable threshold (default: 100%)
- Likely-cause heuristics for common mismatch patterns:
  - Float rounding → IIF/arithmetic expression review
  - Date mismatch → TO_DATE format string check
  - Whitespace diff → LTRIM/RTRIM behaviour difference
  - Extra rows → cartesian join risk
  - Missing rows → filter condition or NULL handling

Script requires only `pandas` and stdlib. Exits with code 0 (pass) or 1 (fail)
for CI integration.

#### `docs/TESTING_GUIDE.md` — new user-facing documentation

Comprehensive guide covering all four test layers, execution sequence, how to
fill in helper stubs, how to run the golden comparison, and an FAQ. Explicitly
documents that the tool generates test artifacts but does **not** execute them —
execution is the data engineering team's responsibility.

### Changed

- `test_agent.py` — `generate_tests()` extended with two new steps (Component A
  and Component B); updated module docstring documents the execution boundary.
- `test_agent.py` — imports `generate_comparison_script` from new
  `golden_compare.py` module.

### New files

- `app/backend/agents/golden_compare.py` — `generate_comparison_script()` function
- `docs/TESTING_GUIDE.md` — testing guide for data engineers

---

## [2.12.0] — Mapplet Inline Expansion

### Added

#### Full inline expansion of `<MAPPLET>` definitions (Step 1 — Parser)

The parser now replaces each mapplet instance in a mapping with the full set of
transformations and connectors from its definition, giving the conversion agent a
completely resolved graph with no black-box references.

**Algorithm**

1. `<MAPPLET>` definitions are scanned **before** `<MAPPING>` elements so the
   definition dict is available when mappings are processed.
2. `_extract_mapplet_def()` — new helper that extracts all internal
   transformations, connectors, and detects the Input/Output interface node names
   (`TYPE="Input Transformation"` / `TYPE="Output Transformation"`).
3. `_inline_expand_mapplets()` — new helper called from `_extract_mapping()`:
   - Adds internal transformations to the mapping, prefixed
     `{instance_name}__{internal_node_name}` (instance name used as prefix, not
     definition name, so two instances of the same mapplet in one mapping stay
     distinct).
   - Adds internal connectors with the same prefix scheme.
   - Rewires external connectors:
     - `TOINSTANCE=mlt_inst → TOINSTANCE=mlt_inst__<InputTransName>`
     - `FROMINSTANCE=mlt_inst → FROMINSTANCE=mlt_inst__<OutputTransName>`
4. `mapping["mapplet_expansions"]` — new dict key; list of definition names that
   were expanded in that mapping.
5. `ParseReport.mapplets_expanded` — new list field; aggregate of all expanded
   mapplet names across every mapping in the file (backward-compatible default `[]`).

**Two distinct flag types (replaces single MAPPLET_DETECTED)**

| Situation | ParseFlag | VerificationFlag |
|---|---|---|
| Definition present, expanded inline | `MAPPLET_EXPANDED` | MEDIUM, non-blocking |
| Instance found, definition missing | `MAPPLET_DETECTED` | HIGH, non-blocking — re-export guidance |

`FLAG_META` updated with entries for both `MAPPLET_EXPANDED` and `MAPPLET_DETECTED`.
`VerificationFlag` promotion in `verification_agent.py` now raises two separate
flags (one per type) when both situations occur in the same file.

**`_mapplet_source` annotation** — each inlined transformation carries a
`_mapplet_source` key recording the definition name; aids debugging and future
tooling.

**Unchanged behaviour for mappings with no mapplets** — zero performance overhead,
no flags raised.

---

## [2.11.0] — Mapplet Detection

### Added

#### `MAPPLET_DETECTED` parse and verification flags

Mapplets (reusable sub-mappings) are common in long-running Informatica estates.
Previously, mapplet instances were silently absorbed into the graph with no
indication that they required manual review. v2.11.0 makes them visible and
actionable at every stage of the pipeline.

**Detection (Step 1 — Parser)**

- Scans `<MAPPLET>` definition blocks (present when the mapping is exported
  with *Include Dependencies* from Repository Manager)
- Scans `<INSTANCE TYPE="Mapplet">` references within each `<MAPPING>` (catches
  the case where the definition block was not exported)
- Deduplicates across both sources — exactly **one** `ParseFlag` per unique mapplet
- Flag messages are context-aware:
  - *Definition present* → "Port-level metadata captured; full expansion planned for v2.12"
  - *Instance only / definition missing* → "Re-export with Include Dependencies enabled"
- `ParseReport.mapplets_detected` — new field; list of all detected mapplet names
- `ParseReport.objects_found["Mapplet"]` — count included in the objects summary
- `graph["mapplets"]` — new graph key; list of mapplet records passed to all downstream agents

**Verification promotion (Step 4 — Verification)**

- `MAPPLET_DETECTED` entry added to `FLAG_META` with `severity=HIGH` and an
  actionable recommendation (re-export guidance + v2.12 expansion note)
- When `parse_report.mapplets_detected` is non-empty, a single consolidated
  `VerificationFlag` (non-blocking, severity HIGH) is raised at Gate 1
- Flag lists all detected mapplet names and tells the reviewer exactly what to check

#### Mapplet detection cases

| Scenario | Flag raised | Message |
|---|---|---|
| `<MAPPLET>` definition + `<INSTANCE>` present | ✅ `MAPPLET_DETECTED` | Definition present; verify generated code |
| `<INSTANCE TYPE="Mapplet">` with no definition block | ✅ `MAPPLET_DETECTED` | Re-export with Include Dependencies |
| No mapplets in mapping | ✅ None | Silent — no false positives |
| Multiple distinct mapplets | ✅ One flag per mapplet, one consolidated VerificationFlag | Lists all names |

### Not yet implemented (planned v2.12)

Full **inline expansion** — replacing each mapplet call in the graph with its
constituent transformations and expressions — is scoped for v2.12.  Until then,
the generated code may contain placeholder references; the `MAPPLET_DETECTED`
flag at Gate 1 tells reviewers exactly what to validate manually.

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

## v2.4.6 — 2026-03-02

### fix: SQLite write locking + audit trail

- **GAP #1** — All write operations now use `BEGIN IMMEDIATE` transactions. Eliminates
  `SQLITE_BUSY` errors under concurrent batch jobs where multiple pipelines attempt
  simultaneous writes. (`database.py`)
- **GAP #17** — `audit_log` table added to the DB schema. Every Gate 1, Gate 2, and
  Gate 3 decision is stamped with `job_id`, `gate`, `decision`, `reviewer_name`,
  `reviewer_role`, `notes`, and `timestamp`. Survives job soft-delete. Accessible via
  `GET /api/audit`. (`database.py`, `routes.py`)

## v2.4.7 — 2026-03-02

### feat: Manifest re-upload endpoint

- **`POST /api/jobs/{id}/manifest-upload`** — accepts an annotated manifest XLSX with
  reviewer overrides filled in. Parses the Review Required sheet, extracts the Override
  column, and stores the overrides in `state["manifest_overrides"]`. The conversion agent
  reads these on Step 7 and injects reviewer answers as hard requirements into the
  conversion prompt. (`routes.py`, `manifest_agent.py`)

## v2.4.8 — 2026-03-02

### feat: Manifest re-upload UI in Gate 1 sign-off card

- The Gate 1 sign-off card now shows a "Re-upload Manifest" section when the manifest
  exists and the job is awaiting Gate 1 review. Reviewer can download the manifest, fill
  in the Override column, and re-upload directly from the UI without leaving the job panel.
  The upload sends a `POST /api/jobs/{id}/manifest-upload` request and refreshes the job
  state on success. (`index.html`)

## v2.4.9 — 2026-03-02

### fix: SSE sentinel + FAILED state on batch job crash

- **GAP #8** — `stream_progress()` now guarantees an SSE `[DONE]` sentinel is sent on
  all exit paths (normal completion, pipeline exception, stream consumer disconnect).
  Previously a pipeline crash left the SSE connection open indefinitely on the client.
  The `BackgroundTask` cleanup is now also registered on exception paths. (`routes.py`)
- Batch job worker catches unhandled exceptions and transitions the job to `FAILED` with
  an `error` key in state before the task exits, so the UI never shows a permanently
  spinning job after a crash. (`orchestrator.py`)

---

## v2.5.0 — 2026-03-02

### feat: Job artifact export to disk + ZIP download

- **Gate 3 artifact export** — after a Gate 3 APPROVED decision the pipeline writes a
  structured output directory: generated code files, test files, S2T Excel workbook,
  security scan report (JSON), Markdown documentation, and a `manifest.json` summary.
  Output root configurable via `OUTPUT_DIR` env var; defaults to `<repo>/jobs/`; set to
  `disabled` to suppress all disk writes. (`job_exporter.py`)
- **`GET /api/jobs/{id}/export`** — builds the output directory on demand and streams
  the contents as a ZIP archive. Returns 404 if the job is not yet COMPLETE. Suitable
  for CI pipelines that pull generated code without accessing the file system directly.
  (`routes.py`)

---

## v2.6.0 — 2026-03-02

### feat: Performance guidance in prompts + performance review stage + WAL + pagination

#### Prompt enhancements

- **PySpark — `## Performance Rules` block**: partition strategy (`.repartition()` /
  `.coalesce()`), broadcast joins for SMALL/MEDIUM lookups, UDF ban with required comment
  when unavoidable, no `.collect()` inside loops, partition pruning before joins, shuffle
  minimisation via `sortWithinPartitions`, `.cache()` / `.persist()` checkpoints,
  `spark.sql.shuffle.partitions` header in every generated script.
- **dbt — `## Performance Rules` block**: materialisation selection (view → staging,
  incremental → final mart, table → small lookup), incremental strategy per warehouse
  (BigQuery insert_overwrite, Snowflake/Redshift merge, Spark insert_overwrite), partition
  / cluster keys, SELECT ∗ ban, filter-early guidance.
- **Python/Pandas — chunked I/O rules**: `pd.read_csv(..., chunksize=100_000)` mandatory
  on all file sources, no `iterrows()` on large DataFrames, memory-efficient joins,
  chunk-pipeline pattern through the full read → transform → write chain.

#### Stage C — Performance Review

- New `run_perf_review()` function in `review_agent.py` runs after Stage B (code quality).
  Checks generated code for scale anti-patterns: `collect()` on large DataFrames, Python
  UDFs where native Spark functions exist, missing partition hints on reads, cartesian
  joins, `iterrows()`, `pd.read_csv()` without `chunksize`, final dbt mart as `view`.
  Advisory only — no pipeline gate; results stored in `state["perf_review"]`.
- `PerfReviewCheck` and `PerfReviewReport` Pydantic models added to `schemas.py`.

#### Infrastructure

- **SQLite WAL mode** — `PRAGMA journal_mode=WAL` + `PRAGMA synchronous=NORMAL` set
  immediately after `init_db()` opens the connection. Concurrent readers no longer block
  on writer. Combined with BEGIN IMMEDIATE write locking (v2.4.6), this eliminates
  reader starvation under heavy batch loads.
- **`complexity_tier` column** — new column on the `jobs` table (DEFAULT NULL). DB
  auto-migrates on startup. Written after Step 6 with the tier value. `list_jobs()` and
  `get_batch_jobs()` include it in the SELECT to avoid unnecessary state decompression.
- **Pagination for `GET /api/jobs`** — query params `?limit=N&offset=M&status=S`; default
  `limit=50`, `offset=0`. Response envelope: `{"total": N, "jobs": [...]}`. COUNT(*)
  sub-query returns total without loading all rows.

---

## v2.6.1 — 2026-03-02

### fix: Security hardening + schema_version on all persisted models

- **Path traversal in export endpoint** — `GET /api/jobs/{id}/export` now resolves all
  paths relative to the configured output directory and rejects any path that escapes
  the directory root.
- **Open redirect in auth middleware** — login redirect now validates the `next=` query
  parameter against an allowlist of internal paths; external URLs are silently discarded.
- **Timing-safe token comparison** — session token comparison in `auth.py` now uses
  `hmac.compare_digest()` instead of string equality to prevent timing-based attacks.
- **`yaml.safe_load` everywhere** — all `yaml.load()` calls in the codebase replaced with
  `yaml.safe_load()`. Eliminates arbitrary code execution via crafted YAML in uploaded
  parameter files or security rules.
- **`schema_version` field** — all persisted Pydantic models (`ParseReport`,
  `ConversionOutput`, `CodeReviewReport`, `ReconciliationReport`, etc.) include a
  `schema_version: str` field (default `"1"`). Enables future migration logic without
  a full DB schema change.
- **BPG updated** — Best Practices Guide bumped to v2.6.1; Security section (§9)
  expanded with `yaml.safe_load`, timing-safe comparison, and open-redirect notes.

---

## v2.7.0 — 2026-03-04

### feat: dbt conversions are execution-ready out of the box

Every dbt conversion now produces a complete, runnable dbt project — no manual edits
required before `dbt run`.

- **`dbt_project.yml`** — generated at the project root with correct `name`, `version`,
  `model-paths`, `test-paths`, and `profile` reference.
- **`profiles.yml` template** — written alongside the project with `env_var()` stubs for
  all connection credentials (host, user, password, database, schema). No hardcoded
  secrets. Includes a commented example for each supported warehouse adapter.
- **`packages.yml`** — included when `dbt_utils` macros are used in generated models;
  pinned to a compatible version range.
- **Macros** — written to `macros/` with correct Jinja function signatures and
  `{% macro %}` / `{% endmacro %}` delimiters.
- **`schema.yml` / `sources.yml`** — include `freshness` blocks, `not_null` and
  `unique` tests on primary key columns, and correct three-part `database.schema.table`
  references in `sources.yml`.
- **`ref()` and `source()` validation** — generated SQL models verified not to contain
  bare table name references; all cross-model references use `{{ ref('model') }}` or
  `{{ source('schema', 'table') }}`.
- **BPG updated** — Best Practices Guide §12 (dbt) expanded with the new file layout,
  profiles.yml credential management, and `ref()` / `source()` usage rules.

---

## v2.8.0 — 2026-03-04

### test: Comprehensive validation layer — 100 tests across 5 new files

#### New test modules

- **`tests/test_core.py`** — 76 unit tests covering XML parser edge cases, complexity
  scoring, S2T extraction, documentation sentinel logic, verification flag rules,
  security scan bandit/YAML/Claude paths, code review checks, test generation stubs,
  and orchestrator state machine transitions.
- **`tests/test_steps58.py`** — Steps 5–8 integration tests: Gate 1 sign-off logic,
  stack assignment, conversion output validation (`_validate_conversion_files()`),
  security scan happy-path and error paths.
- **`tests/test_routes.py`** — REST API contract tests: all 20+ endpoints exercised
  against a fixture SQLite DB. Asserts status codes, content-type headers, and payload
  shape for jobs, batch, logs, health, audit, and export endpoints.

#### New pipeline modules (wired into orchestrator)

- **`app/backend/smoke_execute.py`** — static file validation without a live database:
  `py_compile` for Python/PySpark, SELECT + Jinja delimiter balance check for dbt SQL,
  `yaml.safe_load` for YAML. `SmokeResult` model per file (passed / failed / tool /
  detail). Added as **Step 7b** in both `resume_after_signoff` and
  `resume_after_security_review`; failures stored as HIGH `smoke_flags` on
  `ConversionOutput`; pipeline continues regardless.
- **`app/backend/agents/reconciliation_agent.py`** — structural reconciliation between
  mapping specification and generated code: (1) target field coverage — every S2T target
  field found in generated code, (2) source table coverage — every source qualifier /
  table referenced in generated code, (3) expression coverage — documented business-rule
  expressions present in generated code, (4) stub completeness — no file is >60% TODO
  stubs. Returns `ReconciliationReport` with `match_rate`, `mismatched_fields`,
  `final_status` (RECONCILED ≥100%, PARTIAL ≥80%, PENDING_EXECUTION <80%). Added as
  **Step 10b** in both `resume_after_signoff` and `resume_after_security_review`;
  stored in `state["reconciliation"]`; advisory, non-blocking.

#### Infrastructure

- **GitHub Actions `test.yml`** — new workflow runs `pytest -x` on every push to `main`
  and on all PRs targeting `main`; job fails on first test failure.
- **Version string centralised** — `APP_VERSION = "2.8.0"` in `config.py`;
  `main.py` (FastAPI metadata + `/health` response) and `routes.py` both read
  `_cfg.app_version` — no more scattered hardcoded version strings.
- **HTTP security headers middleware** — new `security_headers_middleware` in `main.py`
  adds `X-Content-Type-Options: nosniff`, `X-Frame-Options: DENY`,
  `X-XSS-Protection: 1; mode=block`, `Referrer-Policy: strict-origin-when-cross-origin`,
  `Permissions-Policy`, `Content-Security-Policy` (self + unsafe-inline), and (when
  `HTTPS=true`) `Strict-Transport-Security: max-age=31536000; includeSubDomains` to
  every HTTP response.

---

## v2.9.0 — 2026-03-04

### feat: Outbound webhook notifications at gate pauses, completion, and failure

No more polling the UI to know when a job is waiting for review or when conversion
is done. Set `WEBHOOK_URL` in `.env` and receive a JSON POST at every key event.

#### New module

- **`app/backend/webhook.py`** — `fire_webhook(event, job_id, filename, step, status,
  message, gate=None)`. Non-blocking (uses `httpx.AsyncClient`), non-fatal (all
  exceptions caught and logged as warnings). Zero impact on pipeline throughput.

#### Events fired

| Event | When |
|---|---|
| `gate_waiting` | Gate 1 paused for human sign-off (Step 5) |
| `gate_waiting` | Gate 2 paused for security review (Step 9) — only when scan is not APPROVED |
| `gate_waiting` | Gate 3 paused for code sign-off (Step 12) — both pipeline paths |
| `job_complete` | Gate 3 approved; code ready for export (Step 12) |
| `job_failed` | Parse BLOCKED — wrong file type or parse error (Step 1) |
| `job_failed` | Conversion FAILED — code generation exception (Step 7) |

#### Payload (all events)

```json
{
  "event":     "gate_waiting",
  "job_id":    "3f2a1b...",
  "filename":  "m_LOAN_SCD2.xml",
  "step":      5,
  "status":    "awaiting_review",
  "message":   "Gate 1 is waiting for sign-off on 'm_LOAN_SCD2.xml' — 3 verification flag(s).",
  "gate":      "Gate 1 — Human Sign-off",
  "timestamp": "2026-03-04T14:22:07.341Z",
  "tool":      "Informatica Conversion Tool",
  "version":   "2.9.0"
}
```

#### Configuration

| Variable | Default | Description |
|---|---|---|
| `WEBHOOK_URL` | `""` | POST destination; empty = disabled |
| `WEBHOOK_SECRET` | `""` | HMAC-SHA256 signing key; empty = unsigned |
| `WEBHOOK_TIMEOUT_SECS` | `10` | Per-request timeout (seconds) |

#### Request signing (optional)

When `WEBHOOK_SECRET` is set, every outbound request carries:
```
X-Webhook-Signature: sha256=<hex>
```
Receivers compute `HMAC-SHA256(secret, raw_body)` and compare with constant-time
equality (`hmac.compare_digest`) to verify the payload originated from this tool.

#### Works with

Slack incoming webhooks, Teams incoming webhooks, PagerDuty Events API v2, and any
HTTP endpoint that accepts a JSON POST. The payload is intentionally generic — no
platform-specific formatting is applied; the receiver formats as needed.

#### Config additions (`config.py`)

- `webhook_url: str = ""`
- `webhook_secret: str = ""`
- `webhook_timeout_secs: int = 10`

---

## v2.10.0 — 2026-03-04

### feat: Automatic GitHub PR after Gate 3 approval

Set `GITHUB_TOKEN` + `GITHUB_REPO` in `.env` and every approved conversion
automatically opens a draft pull request — no ZIP download, no manual commit.

#### New module

- **`app/backend/git_pr.py`** — `create_pull_request(job_id, state, filename)`
  async function. Uses the GitHub REST API via `httpx`. Non-blocking, non-fatal.

#### What the PR contains

- All generated code files (`conversion_output.files`)
- All generated test files (`test_report.test_files`)
- Structured PR description with mapping details, quality summary, file list,
  and the three gate decisions (reviewer names + decisions)

#### Branch and PR naming

- Branch: `informatica/{mapping-name-slug}/{job-id-short}`
  e.g. `informatica/m-loan-scd2/3f2a1b4c`
- PR title: `[Informatica] {mapping_name}`
- PR opened as **draft** — signals it is machine-generated and needs human review
  before merging

#### PR description sections

1. Mapping details — source file, target stack, complexity tier
2. Quality summary table — test coverage %, code review recommendation,
   logic equivalence (V/NR/M), structural reconciliation (status + match rate)
3. Generated files — code files and test files listed separately
4. Review gates — Gate 1/2/3 decisions with reviewer names
5. Footer — timestamp and job ID

#### GitHub Enterprise support

Set `GITHUB_API_URL` to your GHE instance API root
(e.g. `https://github.mycompany.com/api/v3`). All API calls route through
that base URL instead of `https://api.github.com`.

#### State and webhook integration

- `pr_url` stored in job state on success — visible via `GET /api/jobs/{id}`
- `job_complete` webhook payload includes the PR URL when available
- COMPLETE SSE message appended with the PR URL

#### Configuration

| Variable | Default | Description |
|---|---|---|
| `GITHUB_TOKEN` | `""` | PAT with `repo` scope, or GitHub App token (required) |
| `GITHUB_REPO` | `""` | `owner/repo` — e.g. `myorg/data-migration` (required) |
| `GITHUB_BASE_BRANCH` | `main` | Branch the PR targets |
| `GITHUB_API_URL` | `https://api.github.com` | Override for GitHub Enterprise Server |
