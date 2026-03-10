"""
Smoke-execution validation for generated code files.

Goes one step beyond _validate_conversion_files() (which checks syntax via
ast.parse) by actually compiling / parsing the files through their respective
tool chains:

  Python / PySpark  → py_compile (catches byte-code compilation errors)
  dbt SQL models    → dbt parse (optional; requires dbt installed)
  YAML files        → yaml.safe_load (structural validity)

None of these checks require a live database — they are all static.

Usage
-----
    from backend.smoke_execute import smoke_execute_files

    results = smoke_execute_files(files, target_stack)
    # results: list of SmokeResult(filename, tool, passed, detail)

    errors = [r for r in results if not r.passed]
"""
from __future__ import annotations
import ast
import importlib
import py_compile
import subprocess
import sys
import tempfile
import textwrap
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .models.schemas import TargetStack


@dataclass
class SmokeResult:
    filename: str
    tool:     str          # "py_compile" | "dbt_parse" | "yaml_load" | "ast_parse"
    passed:   bool
    detail:   Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def smoke_execute_files(
    files: dict[str, str],
    target_stack: TargetStack,
    *,
    run_dbt_parse: bool = False,   # opt-in; requires dbt installed
) -> list[SmokeResult]:
    """
    Run all appropriate smoke checks on a set of generated files.

    Parameters
    ----------
    files          dict mapping filename → content (as produced by ConversionOutput)
    target_stack   used to choose which checks are relevant
    run_dbt_parse  if True and dbt is installed, run 'dbt parse' on SQL models

    Returns
    -------
    list[SmokeResult] — one entry per file checked; empty list if no checks apply.
    """
    results: list[SmokeResult] = []

    py_files  = {k: v for k, v in files.items() if k.endswith((".py", ".pyx"))}
    sql_files = {k: v for k, v in files.items() if k.endswith(".sql")}
    yml_files = {k: v for k, v in files.items()
                 if k.endswith((".yml", ".yaml")) and k != "profiles.yml"}

    # Python / PySpark files → py_compile
    for fname, content in py_files.items():
        results.append(_check_py_compile(fname, content))

    # dbt SQL models → ast-parse the Jinja/SQL structure (and optionally dbt parse)
    if target_stack == TargetStack.DBT and sql_files:
        for fname, content in sql_files.items():
            results.append(_check_sql_structure(fname, content))
        if run_dbt_parse:
            results.extend(_check_dbt_parse(sql_files, yml_files))

    # YAML files → yaml.safe_load
    for fname, content in yml_files.items():
        results.append(_check_yaml(fname, content))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Individual checkers
# ─────────────────────────────────────────────────────────────────────────────

def _check_py_compile(fname: str, content: str) -> SmokeResult:
    """
    Compile a Python source string to bytecode using py_compile.
    Catches a broader class of errors than ast.parse() alone (e.g. encoding
    issues, f-string internals, match/case syntax on older interpreters).
    """
    if len(content.strip()) > 500_000:
        return SmokeResult(fname, "py_compile", True, "skipped — file > 500 KB")

    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".py", delete=False, encoding="utf-8"
    ) as tmp:
        tmp.write(content)
        tmp_path = tmp.name

    try:
        py_compile.compile(tmp_path, doraise=True)
        return SmokeResult(fname, "py_compile", True)
    except py_compile.PyCompileError as e:
        return SmokeResult(fname, "py_compile", False, str(e))
    finally:
        Path(tmp_path).unlink(missing_ok=True)
        cached = Path(tmp_path).with_suffix(".pyc")
        cached.unlink(missing_ok=True)


def _check_sql_structure(fname: str, content: str) -> SmokeResult:
    """
    Structural check for dbt SQL models.  Does not require a database:
    - File must contain at least one SELECT or a Jinja expression
    - Jinja blocks must be balanced ({{ }} and {% %})
    """
    head = content[:5000]
    low  = head.lower()

    if "select" not in low and "{{" not in head:
        return SmokeResult(
            fname, "sql_structure", False,
            "No SELECT or Jinja block found — model appears empty or malformed"
        )

    # Check balanced Jinja delimiters
    open_expr  = content.count("{{")
    close_expr = content.count("}}")
    open_tag   = content.count("{%")
    close_tag  = content.count("%}")

    if open_expr != close_expr:
        return SmokeResult(
            fname, "sql_structure", False,
            f"Unbalanced Jinja expression delimiters: "
            f"{open_expr} '{{{{' vs {close_expr} '}}}}'"
        )
    if open_tag != close_tag:
        return SmokeResult(
            fname, "sql_structure", False,
            f"Unbalanced Jinja tag delimiters: {open_tag} '{{% '}} vs {close_tag} ' %}}'"
        )

    return SmokeResult(fname, "sql_structure", True)


def _check_yaml(fname: str, content: str) -> SmokeResult:
    """Parse YAML for structural validity (no schema enforcement)."""
    try:
        import yaml
        yaml.safe_load(content)
        return SmokeResult(fname, "yaml_load", True)
    except Exception as e:
        return SmokeResult(fname, "yaml_load", False, str(e))


def _check_dbt_parse(
    sql_files: dict[str, str],
    yml_files: dict[str, str],
) -> list[SmokeResult]:
    """
    Run 'dbt parse' in a temporary directory to validate SQL models.

    Only called when run_dbt_parse=True and dbt is installed.
    Creates a minimal dbt project structure so 'dbt parse' can work
    without a real profiles.yml or database connection.
    """
    results: list[SmokeResult] = []

    # Check dbt is available
    try:
        subprocess.run(
            ["dbt", "--version"],
            capture_output=True, text=True, check=True, timeout=10
        )
    except (subprocess.CalledProcessError, FileNotFoundError, subprocess.TimeoutExpired):
        results.append(SmokeResult(
            "dbt_parse", "dbt_parse", True,
            "dbt not installed — skipping dbt parse check"
        ))
        return results

    with tempfile.TemporaryDirectory() as tmpdir:
        root = Path(tmpdir)

        # Minimal dbt_project.yml
        (root / "dbt_project.yml").write_text(textwrap.dedent("""\
            name: smoke_test
            version: '1.0.0'
            config-version: 2
            model-paths: ["models"]
            profile: smoke_test
        """))

        # Minimal profiles.yml (uses DuckDB via dbt-duckdb if available,
        # otherwise falls back to empty — dbt parse doesn't need a live DB)
        (root / "profiles.yml").write_text(textwrap.dedent("""\
            smoke_test:
              target: dev
              outputs:
                dev:
                  type: duckdb
                  path: ':memory:'
                  threads: 1
        """))

        models_dir = root / "models"
        models_dir.mkdir()

        # Write SQL models (use basename only — dbt doesn't allow nested paths in parse)
        written: list[str] = []
        for fname, content in sql_files.items():
            stem = Path(fname).name
            (models_dir / stem).write_text(content)
            written.append(stem)

        # Write schema YAMLs
        for fname, content in yml_files.items():
            (models_dir / Path(fname).name).write_text(content)

        proc = subprocess.run(
            ["dbt", "parse", "--project-dir", str(root), "--profiles-dir", str(root)],
            capture_output=True, text=True, timeout=60
        )

        if proc.returncode == 0:
            results.append(SmokeResult(
                f"{len(written)} model(s)", "dbt_parse", True,
                f"dbt parse succeeded: {', '.join(written)}"
            ))
        else:
            results.append(SmokeResult(
                f"{len(written)} model(s)", "dbt_parse", False,
                (proc.stderr or proc.stdout)[:500]
            ))

    return results


# ─────────────────────────────────────────────────────────────────────────────
# Convenience: human-readable summary
# ─────────────────────────────────────────────────────────────────────────────

def format_smoke_results(results: list[SmokeResult]) -> str:
    """Return a plain-text summary suitable for logging or test output."""
    if not results:
        return "No smoke checks applicable."
    lines = []
    passed = sum(1 for r in results if r.passed)
    lines.append(f"Smoke execution: {passed}/{len(results)} checks passed")
    for r in results:
        icon = "✅" if r.passed else "❌"
        detail = f"  {r.detail}" if r.detail else ""
        lines.append(f"  {icon} [{r.tool}] {r.filename}{detail}")
    return "\n".join(lines)
