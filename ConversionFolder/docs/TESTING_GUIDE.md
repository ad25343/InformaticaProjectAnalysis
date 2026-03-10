# Testing Guide — Informatica Conversion Tool

> **Version:** 2.15.0
> **Audience:** Data engineers and QA teams working with converted Informatica mappings

---

## Overview

The conversion tool validates converted code through multiple layers.  Understanding which layers run automatically and which require your action is essential for a complete quality check.

| Layer | When it runs | Requires your action? |
|---|---|---|
| Smoke / syntax check | Automatically during conversion | No |
| Field & filter coverage check | Automatically during conversion | No — review `COVERAGE_REPORT.md` |
| Generated test files | Automatically during conversion | **Yes — you must run them** |
| Expression boundary tests | Automatically during conversion | **Yes — fill in helpers, then run** |
| Golden CSV comparison | Generated automatically | **Yes — you must run it externally** |

The tool generates test artifacts and delivers them alongside the converted code.  **It does not execute them.**  Execution is your team's responsibility in your own environment.

---

## What gets generated (Step 9)

Every conversion job produces the following files in the `tests/` folder:

| File | Purpose |
|---|---|
| `tests/COVERAGE_REPORT.md` | Field and filter coverage summary — review this first |
| `tests/test_conversion.py` | pytest suite: column presence, NULL checks, row count |
| `tests/test_expressions_{mapping}.py` | Boundary tests for high-risk expressions (IIF, dates, strings, aggregations) |
| `tests/compare_golden.py` | Standalone script to compare Informatica output vs generated code output |

For dbt jobs, singular SQL tests are also generated:

| File | Purpose |
|---|---|
| `tests/assert_{model}_not_empty.sql` | dbt singular test: row count > 0 |
| `tests/assert_{model}_{pk}_not_null.sql` | dbt singular test: primary key not null |
| `tests/{model}_schema_supplement.yml` | Schema YAML additions — merge into your main schema.yml |

---

## Layer 1 — Review the Coverage Report

**When:** Immediately after conversion.
**How:** Open `tests/COVERAGE_REPORT.md`.

This file is generated automatically and shows:
- Which S2T target fields were found in the generated code (✅ / ❌)
- Which source filter conditions were carried through
- The columns discovered in the generated SQL or Python files

If any fields are marked ❌, review the Step 7 (code generation) output carefully before proceeding.

---

## Layer 2 — Run the Generated Tests (`test_conversion.py`)

**When:** After you have a working environment to run the generated code.
**Prerequisites:** The converted pipeline must be executable (dependencies installed, credentials set up).

### For PySpark / Python jobs

```bash
pip install pytest pyspark   # or just pytest for plain Python
pytest tests/test_conversion.py -v
```

**Important:** Open `tests/test_conversion.py` and find the `output_df` fixture:

```python
@pytest.fixture
def output_df():
    # FILL IN — run your converted pipeline and return the output DataFrame
    raise NotImplementedError('Replace with actual pipeline call')
```

Replace the `raise NotImplementedError` with a call to your converted pipeline's entry function.  For example:

```python
@pytest.fixture
def output_df():
    from my_pipeline.transform import run_transformation
    return run_transformation()
```

Once filled in, the suite will check that all expected columns are present and that primary key fields contain no NULLs.

### For dbt jobs

Run the singular tests using dbt:

```bash
dbt test --select assert_*
```

Merge the generated `tests/{model}_schema_supplement.yml` into your project's `schema.yml` to activate column-level tests.

### For plain SQL jobs

Run `tests/validate_{table}.sql` against your data warehouse after loading.  The script contains row count checks and NULL assertions for primary key fields.

---

## Layer 3 — Run Expression Boundary Tests (`test_expressions_{mapping}.py`)

**When:** Before UAT, after translating expressions.
**Prerequisites:** pytest only — no database or Informatica environment needed.

### What these tests check

These tests verify the most commonly mistranslated expression patterns:

| Category | Key risk |
|---|---|
| **IIF** | NULL condition input takes the FALSE branch in Informatica — not NULL propagation |
| **DECODE** | Case-sensitive matching; NULL input falls through to the default value |
| **Date arithmetic** | Month-end rollover (e.g. Jan 31 + 1 month = Feb 28/29); NULL propagation |
| **SUBSTR** | Informatica is **1-indexed**; Python is **0-indexed** — off-by-one is the most common bug |
| **Aggregations** | Empty partition must return NULL (not 0) to match Informatica |

### Setup

Open `tests/test_expressions_{mapping}.py`.  Each category has a helper function marked with a `FILL IN` comment:

```python
def _iif_helper(value):
    """
    FILL IN: call the translated IIF expression from your generated code.
    Example: return generated_module.iif_expression(value)
    """
    raise NotImplementedError('FILL IN: call translated IIF expression')
```

Replace each stub with a call to the actual translated function in your generated code.

### Running

```bash
pip install pytest
pytest tests/test_expressions_{mapping}.py -v
```

Expected output when all helpers are filled in and expressions are correct:

```
tests/test_expressions_m_appraisal_rank.py::test_iif_null_branch_semantics[100.0-100.0] PASSED
tests/test_expressions_m_appraisal_rank.py::test_iif_null_branch_semantics[-5.0-0.0] PASSED
tests/test_expressions_m_appraisal_rank.py::test_iif_null_branch_semantics[0.0-0.0] PASSED
tests/test_expressions_m_appraisal_rank.py::test_iif_null_branch_semantics[None-0.0] PASSED
...
```

If a test fails, the error message will tell you exactly which input/expected/actual values differ, pointing you directly to the mistranslated expression.

---

## Preparing Golden Reference Data

Before you can run the Layer 4 comparison, you need a golden CSV — the reference output captured directly from Informatica running against a known input dataset.  This section covers how to create it, what data to use, and where to store it.

### What is a golden dataset?

A golden dataset is a small, stable, representative sample of source rows run through the **original Informatica mapping** and captured as a CSV.  It is the ground truth that the generated code's output is compared against.

The key constraint: **the same source rows must be rerunnable through both Informatica and the generated code**.  This means you need a fixed input dataset, not a live table that changes daily.

### Step 1 — Choose your input data

Use a fixed sample of source rows, not a live production table.  Aim for:

| Criteria | Guidance |
|---|---|
| **Row count** | 500–5,000 rows is sufficient for most mappings. More is not always better — a well-chosen 500 rows beats 50,000 random rows. |
| **NULL coverage** | Ensure the sample includes NULLs in every nullable source column — the most common translation failure is NULL handling in IIF and DECODE expressions. |
| **Boundary values** | Include at least one row for each expression boundary: zero, negative numbers, empty strings, minimum/maximum dates, and month-end dates. |
| **All code paths** | Cover every branch in filter conditions and IIF logic — if a filter has 3 conditions, make sure rows exist that hit each path. |
| **PII** | Do not use production data containing PII.  Mask or synthesize sensitive columns before using as test data. |

Save this fixed input as `sample_input.csv` and keep it alongside the golden output.  Both files together form the test fixture — you need both to reproduce results.

### Step 2 — Capture Informatica output

Run the original Informatica mapping against your fixed input dataset and export the result to a CSV.

**Via Flat File target (recommended):**
1. In Informatica Designer, temporarily add a Flat File target to the mapping.
2. Configure it as CSV with headers.
3. Run a test session against your fixed input.
4. Collect the output file from the session target directory.

**Via PowerCenter Monitor session log:**
Some mappings write directly to a database target.  Run the session, then export the target table rows (filtered to your test run) as a CSV from your database client.

**Via XML session config override:**
Add a `$PMTargetFileDir` parameter pointing to a local directory when running the test session.

Once captured, rename the file clearly:
```
<mapping_name>_golden.csv
```
Example: `m_appraisal_rank_golden.csv`

### Step 3 — Store golden data alongside the conversion output

The standard location is the `golden/` subdirectory inside the mapping's test folder:

```
OUTPUT_DIR/
  <batch_label>_<timestamp>/
    m_appraisal_rank/
      input/            ← source XMLs (written by tool)
      output/           ← generated code (written by tool)
        tests/
          compare_golden.py         ← comparison script (written by tool)
          golden/                   ← YOU CREATE THIS
            m_appraisal_rank_golden.csv   ← Informatica reference output
            sample_input.csv              ← fixed input rows used to generate it
      docs/             ← S2T, documentation, security scan (written by tool)
      logs/             ← pipeline logs (written by tool)
```

Creating the `golden/` folder and placing the reference files there is a **manual step** — the tool does not create it.  Add a note in your team's runbook to do this before UAT.

### Step 4 — Version control golden data

Commit golden CSVs to git alongside the converted code.  This ensures:
- Anyone re-running the test gets the same reference point
- Changes to the generated code can be regression-tested against the same baseline
- The migration audit trail is complete

If the golden CSV is too large to commit (> a few MB), store it in a team-accessible object store (S3, Azure Blob, etc.) and add a `golden/README.md` with the retrieval path.

**Before committing:** confirm no PII is present.  Run a quick scan:
```bash
# Check for common PII patterns before committing
grep -E '\b[0-9]{3}-[0-9]{2}-[0-9]{4}\b|\b[0-9]{16}\b' golden/*.csv
```

### Running the comparison from the standard location

Once the golden file is in place, the comparison command becomes:

```bash
cd OUTPUT_DIR/<batch_label>_<timestamp>/m_appraisal_rank/output/

python tests/compare_golden.py \
  --expected tests/golden/m_appraisal_rank_golden.csv \
  --actual   generated_output.csv \
  --key-columns ACCOUNT_ID,LOAD_DATE \
  --ignore-columns AUDIT_TIMESTAMP,ETL_RUN_ID
```

### Lights-out scheduler workflow

When using the file watcher for overnight batch conversion, the golden data preparation schedule is:

1. **Before the watcher run** — confirm `golden/` folders exist for mappings being re-converted; golden CSVs do not need to be re-captured unless the source mapping changed.
2. **After Gate 3 approval** — artifacts are written to the output folder.  Golden CSVs are already in place from the prior run.
3. **Morning review** — run `compare_golden.py` for each mapping as part of the daily UAT check.

If this is a **first-time conversion** (no prior golden CSV exists), the data engineer must capture the Informatica output before the comparison can be run.  This is expected — golden data is a one-time setup per mapping, reused for all subsequent re-conversions.

---

## Layer 4 — Golden CSV Comparison (`compare_golden.py`)

**When:** During UAT — after you have a live Informatica session and the generated code running against the same source rows.
**Prerequisites:** `pip install pandas`

This script is **entirely self-contained**.  It does not require the conversion tool, a database connection, or any project-specific imports.

### Step-by-step

**Step 1 — Capture Informatica output**

Run a test session in Informatica against a sample dataset (100–10,000 rows is sufficient for spot-checking).  Export the output to a CSV file.

```
informatica_output.csv
```

**Step 2 — Run the generated code against the same input**

Execute the converted dbt model / PySpark script / SQL against the same source rows and export the output to a CSV:

```
generated_output.csv
```

**Step 3 — Run the comparison**

```bash
python tests/compare_golden.py \
  --expected informatica_output.csv \
  --actual   generated_output.csv
```

With optional arguments:

```bash
python tests/compare_golden.py \
  --expected informatica_output.csv \
  --actual   generated_output.csv \
  --threshold 99.5 \
  --key-columns ACCOUNT_ID,LOAD_DATE \
  --ignore-columns AUDIT_TIMESTAMP,ETL_RUN_ID
```

| Argument | Default | Description |
|---|---|---|
| `--expected` | required | Informatica golden-output CSV |
| `--actual` | required | Generated code output CSV |
| `--threshold` | `100.0` | Minimum % of rows that must match to pass |
| `--key-columns` | auto-detected | Columns used for row-level join |
| `--ignore-columns` | none | Columns to skip (e.g. audit timestamps that will always differ) |

### Interpreting the output

The script prints a report and exits with code `0` (pass) or `1` (fail):

- **✓ PASS** — field matches within threshold
- **⚠ WARN** — mismatches present but within threshold (check manually)
- **✗ FAIL** — mismatches exceed threshold

For each failing column the script shows a sample of up to 20 mismatched rows and a **likely-cause heuristic**:

| Pattern | Heuristic |
|---|---|
| Numeric diff < 0.01 | Float rounding — review IIF / arithmetic expression |
| Date column mismatches | Date format mismatch — check TO_DATE format string |
| String whitespace differences | LTRIM/RTRIM — Python strips both ends; Informatica may strip one |
| More rows in actual than expected | Possible fan-out — check JOIN conditions |
| Fewer rows in actual than expected | Possible data loss — check filter conditions and NULL handling |

### Exit codes

```bash
python tests/compare_golden.py --expected a.csv --actual b.csv
echo $?   # 0 = PASS, 1 = FAIL
```

This makes it straightforward to integrate into a CI pipeline or Airflow task if you want to automate the comparison as a post-load quality gate.

---

## Recommended execution sequence

```
1.  Convert mapping (tool runs automatically — Steps 1–9)
2.  Review tests/COVERAGE_REPORT.md
3.  If coverage < 100%: investigate missing fields before proceeding
4.  Fill in helper stubs in test_expressions_{mapping}.py
5.  Run: pytest tests/test_expressions_{mapping}.py -v
6.  Fix any failing expression tests before UAT
7.  Fill in output_df fixture in test_conversion.py
8.  Run: pytest tests/test_conversion.py -v
9.  Proceed to UAT environment:
    a. Capture Informatica output → informatica_output.csv
    b. Run generated code against same input → generated_output.csv
    c. Run: python tests/compare_golden.py --expected ... --actual ...
10. Sign off only after compare_golden.py exits with code 0
```

---

## Frequently asked questions

**Q: Do I need the conversion tool installed to run these tests?**
No. `compare_golden.py` and the pytest test files are self-contained. You only need Python, pandas, and pytest installed in your environment.

**Q: Can I automate the golden comparison in CI?**
Yes. `compare_golden.py` exits with code 0 (pass) or 1 (fail), making it easy to integrate into any CI system or Airflow post-load task.

**Q: What if the expression helper functions are too hard to fill in?**
The boundary tests are optional but strongly recommended before UAT. If the expression is embedded in a large SQL model rather than a standalone function, you can test it by running a small query directly against the generated model with the boundary input values.

**Q: Does the tool ever run these tests automatically?**
No — by design. Running the tests requires a live environment, credentials, and potentially large datasets. The tool's job ends at generating the artifacts. Execution is a data engineering responsibility.

**Q: What if I don't have access to an Informatica environment for the golden comparison?**
Run Layers 1–3 as a minimum. The expression boundary tests catch the most common mistranslation patterns even without a live Informatica session.
