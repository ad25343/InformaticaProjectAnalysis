"""
job_exporter.py — Write completed job artifacts to disk after Gate 3 approval.

Folder structure written under OUTPUT_DIR/<job_id>/:

    input/
        mapping.xml                 ← source Informatica XML
        workflow.xml                ← workflow XML (if uploaded)
        params.xml                  ← parameter file (if uploaded)

    output/
        <conversion files>          ← generated code preserving folder structure
        tests/<test files>          ← generated test files (if present)

    docs/
        documentation.md            ← documentation agent output
        s2t_mapping.xlsx            ← source-to-target workbook
        manifest.xlsx               ← pre-conversion manifest
        verification_report.md      ← Stage A verification findings
        security_scan.md            ← Gate 2 security scan findings

    logs/
        <job_id>.log                ← full pipeline log

If OUTPUT_DIR is set to "disabled" or is unavailable, the export is skipped
with a warning. All failures are non-fatal — a failed export never blocks
the pipeline from reaching COMPLETE.
"""
from __future__ import annotations

import io
import logging
import os
import shutil
import zipfile
from pathlib import Path
from typing import Optional

from .config import settings

log = logging.getLogger("conversion.job_exporter")

# ── Resolve output root ───────────────────────────────────────────────────────
def _resolve_output_root() -> Optional[Path]:
    """Return the configured output root directory, or None if disabled."""
    raw = (settings.output_dir or "").strip()
    if raw.lower() == "disabled":
        return None
    if raw:
        return Path(raw)
    # Default: <repo_root>/jobs   (app/ is one level below repo root)
    here = Path(__file__).resolve().parent          # app/backend/
    return here.parent.parent / "jobs"              # repo_root/jobs/


def job_output_dir(job_id: str) -> Optional[Path]:
    """Return the output directory for a specific job, or None if disabled."""
    root = _resolve_output_root()
    if root is None:
        return None
    return root / job_id


# ── Markdown renderers ────────────────────────────────────────────────────────

def _render_verification_md(verification: dict) -> str:
    lines = ["# Verification Report\n"]
    lines.append(f"**Overall status:** {verification.get('overall_status', 'unknown')}\n")
    lines.append(f"**Mapping name:** {verification.get('mapping_name', 'unknown')}\n")
    flags = verification.get("flags", [])
    if flags:
        lines.append(f"\n## Flags ({len(flags)})\n")
        for f in flags:
            sev   = f.get("severity", "")
            ftype = f.get("flag_type", "")
            msg   = f.get("message", "")
            lines.append(f"- **[{sev}]** `{ftype}` — {msg}")
    else:
        lines.append("\n✅ No flags raised.\n")
    notes = verification.get("notes", [])
    if notes:
        lines.append("\n## Notes\n")
        for n in notes:
            lines.append(f"- {n}")
    return "\n".join(lines) + "\n"


def _render_security_scan_md(security_scan: dict) -> str:
    lines = ["# Security Scan Report\n"]
    lines.append(f"**Verdict:** {security_scan.get('verdict', 'unknown')}\n")
    findings = security_scan.get("findings", [])
    if findings:
        lines.append(f"\n## Findings ({len(findings)})\n")
        for f in findings:
            sev  = f.get("severity", "")
            rule = f.get("rule_id", "")
            msg  = f.get("message", "")
            file = f.get("file", "")
            line = f.get("line")
            loc  = f"{file}:{line}" if file and line else file or ""
            lines.append(f"### [{sev}] {rule}")
            if loc:
                lines.append(f"*{loc}*")
            lines.append(f"{msg}\n")
    else:
        lines.append("\n✅ No security findings.\n")
    auto = security_scan.get("auto_approved", False)
    if auto:
        lines.append("\n> Auto-approved: no actionable findings.\n")
    return "\n".join(lines) + "\n"


# ── Main export function ──────────────────────────────────────────────────────

async def export_job(job_id: str, job: dict, state: dict) -> Optional[Path]:
    """
    Write all job artifacts to OUTPUT_DIR/<job_id>/.

    Parameters
    ----------
    job_id : str
    job    : dict   — raw job record from db.get_job()
    state  : dict   — decoded state dict (job["state"])

    Returns the job output directory on success, None if export is disabled or
    fails.  All exceptions are caught so a failed export never blocks COMPLETE.
    """
    out_dir = job_output_dir(job_id)
    if out_dir is None:
        log.info("Job export disabled (OUTPUT_DIR=disabled): job_id=%s", job_id)
        return None

    try:
        _write_all(out_dir, job_id, job, state)
        log.info("Job exported to disk: job_id=%s path=%s", job_id, out_dir)
        return out_dir
    except Exception as exc:
        log.error("Job export failed (non-fatal): job_id=%s error=%s", job_id, exc, exc_info=True)
        return None


def _write_all(out_dir: Path, job_id: str, job: dict, state: dict) -> None:
    """Core writer — called by export_job; raises on any error."""

    # ── Create directory structure ────────────────────────────────────────
    (out_dir / "input").mkdir(parents=True, exist_ok=True)
    (out_dir / "output").mkdir(parents=True, exist_ok=True)
    (out_dir / "docs").mkdir(parents=True, exist_ok=True)
    (out_dir / "logs").mkdir(parents=True, exist_ok=True)

    # ── INPUT FILES ───────────────────────────────────────────────────────
    xml = job.get("xml_content") or state.get("xml_content")
    if xml:
        _write_text(out_dir / "input" / "mapping.xml", xml)

    workflow_xml = job.get("workflow_xml_content")
    if workflow_xml:
        _write_text(out_dir / "input" / "workflow.xml", workflow_xml)

    params = job.get("parameter_file_content")
    if params:
        _write_text(out_dir / "input" / "params.xml", params)

    # ── OUTPUT FILES (generated code) ─────────────────────────────────────
    conversion = state.get("conversion", {})
    conv_files: dict = conversion.get("files", {})
    for rel_path, content in conv_files.items():
        dest = out_dir / "output" / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        _write_text(dest, content)

    # ── TEST FILES ────────────────────────────────────────────────────────
    test_report = state.get("test_report", {})
    test_files: dict = test_report.get("test_files", {})
    for rel_path, content in test_files.items():
        dest = out_dir / "output" / "tests" / rel_path
        dest.parent.mkdir(parents=True, exist_ok=True)
        _write_text(dest, content)

    # ── DOCS: documentation markdown ──────────────────────────────────────
    doc_md = state.get("documentation_md")
    if doc_md:
        _write_text(out_dir / "docs" / "documentation.md", doc_md)

    # ── DOCS: S2T Excel (already on disk — copy it) ───────────────────────
    s2t_state = state.get("s2t", {})
    s2t_rel   = s2t_state.get("excel_path")           # e.g. "logs/s2t/foo_s2t_abc123.xlsx"
    if s2t_rel:
        from pathlib import Path as _P
        here = _P(__file__).resolve().parent           # app/backend/
        s2t_src = here.parent / s2t_rel                # app/<rel>
        if s2t_src.exists():
            shutil.copy2(s2t_src, out_dir / "docs" / "s2t_mapping.xlsx")
        else:
            # Try the s2t_agent helper as fallback
            try:
                from .agents.s2t_agent import s2t_excel_path
                p = s2t_excel_path(job_id)
                if p and p.exists():
                    shutil.copy2(p, out_dir / "docs" / "s2t_mapping.xlsx")
            except Exception:
                pass

    # ── DOCS: Manifest Excel (regenerate from state) ───────────────────────
    manifest_raw = state.get("manifest_report")
    if manifest_raw:
        try:
            from .agents.manifest_agent import ManifestReport, write_xlsx_bytes
            manifest_report = ManifestReport(**manifest_raw)
            xlsx_bytes = write_xlsx_bytes(manifest_report)
            (out_dir / "docs" / "manifest.xlsx").write_bytes(xlsx_bytes)
        except Exception as exc:
            log.warning("Could not write manifest.xlsx: %s", exc)

    # ── DOCS: Verification report ─────────────────────────────────────────
    verification = state.get("verification")
    if verification:
        _write_text(out_dir / "docs" / "verification_report.md",
                    _render_verification_md(verification))

    # ── DOCS: Security scan ───────────────────────────────────────────────
    security_scan = state.get("security_scan")
    if security_scan:
        _write_text(out_dir / "docs" / "security_scan.md",
                    _render_security_scan_md(security_scan))

    # ── LOGS ──────────────────────────────────────────────────────────────
    from .logger import job_log_path
    log_src = job_log_path(job_id)
    if log_src and log_src.exists():
        shutil.copy2(log_src, out_dir / "logs" / f"{job_id}.log")


# ── ZIP builder (used by the download endpoint; does NOT require disk write) ──

def build_output_zip(state: dict) -> bytes:
    """
    Build a ZIP archive of the conversion output files from state.
    Preserves folder structure.  Does not require a prior disk export.

    Returns raw ZIP bytes.
    """
    conversion = state.get("conversion", {})
    conv_files: dict = conversion.get("files", {})

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        for rel_path, content in conv_files.items():
            zf.writestr(rel_path, content)
    buf.seek(0)
    return buf.read()


# ── Helpers ───────────────────────────────────────────────────────────────────

def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
