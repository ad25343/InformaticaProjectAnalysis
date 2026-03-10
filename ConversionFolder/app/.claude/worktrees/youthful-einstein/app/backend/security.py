"""
security.py — Central security utilities for the Informatica Conversion Tool.

Every file-handling path in the application goes through this module.
Do NOT bypass these helpers to accept raw user input.

Protections provided
────────────────────
  XXE          — safe_xml_parser() disables DTD loading and external entity resolution
  Zip Slip     — safe_zip_extract() validates every entry path before writing
  Zip Bomb     — safe_zip_extract() enforces total extracted-size and entry-count limits
  File Size    — validate_upload_size() enforces per-file byte limits
  Input Scan   — scan_xml_for_secrets() checks uploaded XMLs for embedded credentials
  YAML Scan    — scan_yaml_for_secrets() checks config files for plaintext secrets
  Code Scan    — scan_python_with_bandit() wraps bandit for generated-code audits
"""
from __future__ import annotations

import logging
import os
import re
import subprocess
import sys
import tempfile
import zipfile
from io import BytesIO
import posixpath
from pathlib import Path
from typing import Optional

from fastapi import HTTPException
from lxml import etree

log = logging.getLogger("conversion.security")

# ─────────────────────────────────────────────────────────────────────────────
# Limits
# ─────────────────────────────────────────────────────────────────────────────

MAX_UPLOAD_BYTES: int = int(os.environ.get("MAX_UPLOAD_MB", "50")) * 1024 * 1024
"""Maximum size for any single uploaded file (default 50 MB)."""

MAX_ZIP_EXTRACTED_BYTES: int = int(os.environ.get("MAX_ZIP_EXTRACTED_MB", "200")) * 1024 * 1024
"""Maximum total extracted size from a ZIP upload (default 200 MB — prevents zip bombs)."""

MAX_ZIP_FILE_COUNT: int = int(os.environ.get("MAX_ZIP_FILE_COUNT", "200"))
"""Maximum number of files inside a ZIP upload."""

MAX_BANDIT_LINES: int = 10_000
"""Skip bandit scan on files longer than this (avoids runaway subprocess time)."""


# ─────────────────────────────────────────────────────────────────────────────
# XML / XXE protection
# ─────────────────────────────────────────────────────────────────────────────

def safe_xml_parser() -> etree.XMLParser:
    """
    Return an lxml XMLParser with all external-entity and DTD features disabled.

    Prevents XML External Entity (XXE) injection:
      - resolve_entities=False  → never substitute &entity; references
      - no_network=True         → block any network fetch during parsing
      - load_dtd=False          → ignore <!DOCTYPE ...> declarations
      - huge_tree=False         → block Billion Laughs / deeply-nested bombs

    Use this parser for EVERY call to etree.fromstring() or etree.parse().
    """
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
    )


def safe_parse_xml(content: str | bytes) -> etree._Element:
    """
    Parse XML content safely (XXE-hardened).

    Parameters
    ----------
    content : str or bytes
        The raw XML to parse.

    Returns
    -------
    lxml Element (root)

    Raises
    ------
    etree.XMLSyntaxError if the content is not valid XML.
    """
    if isinstance(content, str):
        content = content.encode("utf-8")
    return etree.fromstring(content, parser=safe_xml_parser())


# ─────────────────────────────────────────────────────────────────────────────
# File size validation
# ─────────────────────────────────────────────────────────────────────────────

def validate_upload_size(
    content: bytes,
    label: str = "file",
    limit: Optional[int] = None,
) -> None:
    """
    Raise HTTP 413 if `content` exceeds the configured upload limit.

    Parameters
    ----------
    content : bytes   Raw file bytes to check.
    label   : str     Human-readable name used in the error message.
    limit   : int     Override the global limit for this specific check.
    """
    cap = limit if limit is not None else MAX_UPLOAD_BYTES
    if len(content) > cap:
        mb = cap // 1024 // 1024
        actual_mb = len(content) / 1024 / 1024
        log.warning("Upload rejected: %s is %.1f MB (limit %d MB)", label, actual_mb, mb)
        raise HTTPException(
            status_code=413,
            detail=f"{label} is {actual_mb:.1f} MB — maximum allowed is {mb} MB.",
        )


# ─────────────────────────────────────────────────────────────────────────────
# ZIP extraction (Zip Slip + Zip Bomb protection)
# ─────────────────────────────────────────────────────────────────────────────

class ZipExtractionError(ValueError):
    """Raised when a ZIP archive fails safety checks."""


def safe_zip_extract(zip_bytes: bytes) -> dict[str, bytes]:
    """
    Safely extract a ZIP archive into an in-memory dict.

    Protections
    ───────────
    Zip Slip    Every entry path is resolved relative to a virtual root.
                Any path that would escape (e.g. ``../../etc/passwd``) is
                rejected immediately and the whole archive is discarded.
    Zip Bomb    Total extracted bytes are tracked.  If the sum exceeds
                MAX_ZIP_EXTRACTED_BYTES the extraction stops and raises.
    Entry Count If the archive contains more than MAX_ZIP_FILE_COUNT entries
                it is rejected before any extraction begins.
    Symlinks    Symbolic-link entries are silently skipped.

    Parameters
    ----------
    zip_bytes : bytes  Raw ZIP file content.

    Returns
    -------
    dict mapping ``filename → file_bytes`` for every valid entry.

    Raises
    ------
    ZipExtractionError  on any safety violation.
    zipfile.BadZipFile  if the bytes are not a valid ZIP.
    """
    try:
        zf = zipfile.ZipFile(BytesIO(zip_bytes))
    except zipfile.BadZipFile as exc:
        raise ZipExtractionError(f"Not a valid ZIP file: {exc}") from exc

    entries = zf.infolist()

    if len(entries) > MAX_ZIP_FILE_COUNT:
        raise ZipExtractionError(
            f"ZIP contains {len(entries)} entries — maximum is {MAX_ZIP_FILE_COUNT}."
        )

    extracted: dict[str, bytes] = {}
    total_bytes = 0
    virtual_root = "/safe_root"

    for entry in entries:
        # Skip directories and symlinks
        if entry.filename.endswith("/"):
            continue
        if entry.external_attr >> 28 == 0xA:  # symlink flag
            log.warning("Skipping symlink entry in ZIP: %s", entry.filename)
            continue

        # ── Zip Slip check ────────────────────────────────────────────────
        # Normalise the entry path (strip leading slash, convert backslash)
        # then resolve it relative to our virtual root using posixpath.normpath
        # which handles ".." components without requiring OS filesystem access.
        # If the normalised path escapes the virtual root the archive is rejected.
        try:
            clean = entry.filename.replace("\\", "/").lstrip("/")
            normalised = posixpath.normpath(posixpath.join(virtual_root, clean))
        except Exception:
            raise ZipExtractionError(
                f"Malformed path in ZIP entry: {entry.filename!r}"
            )

        if not (normalised == virtual_root or
                normalised.startswith(virtual_root + "/")):
            raise ZipExtractionError(
                f"Zip Slip detected: entry '{entry.filename}' would escape the "
                "extraction directory. Archive rejected."
            )

        # ── Zip Bomb check ────────────────────────────────────────────────
        total_bytes += entry.file_size
        if total_bytes > MAX_ZIP_EXTRACTED_BYTES:
            mb = MAX_ZIP_EXTRACTED_BYTES // 1024 // 1024
            raise ZipExtractionError(
                f"ZIP extraction stopped: total expanded size exceeds {mb} MB limit "
                "(possible zip bomb)."
            )

        content = zf.read(entry.filename)
        # Normalise path separator and strip leading slashes
        safe_name = entry.filename.replace("\\", "/").lstrip("/")
        extracted[safe_name] = content

    return extracted


# ─────────────────────────────────────────────────────────────────────────────
# Generated-code security scanner (bandit)
# ─────────────────────────────────────────────────────────────────────────────

def scan_python_with_bandit(code: str, filename: str = "converted.py") -> dict:
    """
    Run bandit static analysis on generated Python / PySpark code.

    bandit checks for:
      - Hardcoded passwords / credentials (B105, B106, B107)
      - SQL injection (B608)
      - Insecure use of subprocess / shell (B602, B603, B605)
      - Use of assert for security checks (B101)
      - Binding to all interfaces 0.0.0.0 (B104)
      - Use of exec / eval (B102, B307)
      - Insecure deserialization (B301, B302, B303)
      - MD5 / SHA1 for security (B303, B324)

    Returns
    -------
    dict with keys:
      ran         bool — False if bandit is not installed or skipped
      findings    list[dict] — one entry per issue found
      high_count  int
      medium_count int
      low_count   int
      error       str | None — if scan failed for a non-security reason
    """
    result: dict = {
        "ran": False,
        "findings": [],
        "high_count": 0,
        "medium_count": 0,
        "low_count": 0,
        "error": None,
    }

    if len(code.splitlines()) > MAX_BANDIT_LINES:
        result["error"] = (
            f"File too large for bandit scan ({len(code.splitlines())} lines > "
            f"{MAX_BANDIT_LINES} limit) — manual review recommended."
        )
        return result

    # Write to a temp file so bandit can process it
    try:
        with tempfile.NamedTemporaryFile(
            suffix=".py", mode="w", delete=False, encoding="utf-8"
        ) as tmp:
            tmp.write(code)
            tmp_path = tmp.name

        proc = subprocess.run(
            [sys.executable, "-m", "bandit", "-f", "json", "-q", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        Path(tmp_path).unlink(missing_ok=True)

        import json as _json
        try:
            bandit_out = _json.loads(proc.stdout)
        except Exception:
            result["error"] = f"bandit output parse failed: {proc.stderr[:200]}"
            return result

        result["ran"] = True
        for issue in bandit_out.get("results", []):
            sev = issue.get("issue_severity", "LOW").upper()
            result["findings"].append({
                "test_id":    issue.get("test_id"),
                "test_name":  issue.get("test_name"),
                "severity":   sev,
                "confidence": issue.get("issue_confidence", ""),
                "line":       issue.get("line_number"),
                "text":       issue.get("issue_text", ""),
                "code":       issue.get("code", "").strip()[:200],
            })
            if sev == "HIGH":
                result["high_count"] += 1
            elif sev == "MEDIUM":
                result["medium_count"] += 1
            else:
                result["low_count"] += 1

    except FileNotFoundError:
        result["error"] = "bandit is not installed — pip install bandit to enable scanning."
    except subprocess.TimeoutExpired:
        result["error"] = "bandit scan timed out after 30 seconds."
    except Exception as exc:
        result["error"] = f"bandit scan error: {exc}"
    finally:
        Path(tmp_path).unlink(missing_ok=True) if "tmp_path" in dir() else None

    return result


# ─────────────────────────────────────────────────────────────────────────────
# Input XML credential scanner
# ─────────────────────────────────────────────────────────────────────────────

# Attribute names that should never carry plaintext passwords
_CRED_ATTR_NAMES = re.compile(
    r"(password|passwd|pwd|secret|apikey|api_key|token|credential|auth)",
    re.IGNORECASE,
)

# Values that look like a real secret (non-empty, not a $VARIABLE or placeholder)
_PLACEHOLDER_RE = re.compile(
    r"^\$\$?\w+$"          # Informatica $$VAR or $VAR
    r"|^<.+>$"             # XML placeholder like <your_password>
    r"|^[*]+$"             # masked-out value
    r"|^changeme$"
    r"|^your[_-]",
    re.IGNORECASE,
)


def scan_xml_for_secrets(xml_text: str) -> list[dict]:
    """
    Scan uploaded Informatica XML for hardcoded credentials in attribute values.

    Checks every element attribute whose name matches credential-like keywords
    (PASSWORD, PASSWD, SECRET, TOKEN, etc.) and flags non-empty, non-placeholder
    values as potential leaks.

    Returns a list of finding dicts:
        {severity, attribute, element, value_preview, message}
    """
    findings: list[dict] = []

    try:
        root = safe_parse_xml(xml_text)
    except Exception:
        return findings  # if it won't parse, XXE check already handled it

    for element in root.iter():
        # lxml Comment and ProcessingInstruction nodes have a callable .tag
        # (e.g. lxml.etree.Comment), not a string.  Their .attrib is also
        # non-standard — iterating it causes the Cython "not iterable" error.
        # Skip all non-Element nodes before touching .tag or .attrib.
        if not isinstance(element.tag, str):
            continue
        tag = element.tag.split("}")[-1] if "}" in element.tag else element.tag
        for attr_name, attr_value in element.attrib.items():
            if not _CRED_ATTR_NAMES.search(attr_name):
                continue
            if not attr_value or _PLACEHOLDER_RE.match(attr_value.strip()):
                continue
            # Flag it — show only first 6 chars of the value
            preview = attr_value[:6] + "…" if len(attr_value) > 6 else attr_value
            findings.append({
                "severity":      "HIGH",
                "attribute":     attr_name,
                "element":       tag,
                "value_preview": preview,
                "message": (
                    f"Element <{tag}> has attribute '{attr_name}' with a non-placeholder "
                    f"value ('{preview}'). This may be a hardcoded credential embedded in "
                    "the Informatica export — review before committing or sharing."
                ),
            })
            log.warning(
                "Possible hardcoded credential in uploaded XML: element=%s attr=%s",
                tag, attr_name,
            )

    return findings


# ─────────────────────────────────────────────────────────────────────────────
# YAML secrets scanner
# ─────────────────────────────────────────────────────────────────────────────

# YAML keys that suggest a value may be a secret
_YAML_SECRET_KEY_RE = re.compile(
    r"^\s*(password|passwd|pwd|secret|token|api[_-]?key|credential|auth[_-]?key"
    r"|private[_-]?key|access[_-]?key|client[_-]?secret)\s*:",
    re.IGNORECASE,
)

# YAML values that look like a real secret (not a placeholder or $VAR)
_YAML_PLACEHOLDER_RE = re.compile(
    r"^\s*(\"\"|''|null|~|None|changeme|<.+>|\$\$?\w+|your[_-])",
    re.IGNORECASE,
)


def scan_yaml_for_secrets(yaml_text: str, filename: str = "config.yaml") -> list[dict]:
    """
    Scan a YAML config file line-by-line for plaintext secrets.

    Uses regex rather than a full YAML parser so it works even on Jinja-templated
    dbt files and partial YAML snippets.

    Returns a list of finding dicts:
        {severity, line, key, value_preview, filename, message}
    """
    findings: list[dict] = []

    for lineno, line in enumerate(yaml_text.splitlines(), start=1):
        if not _YAML_SECRET_KEY_RE.match(line):
            continue

        # Extract the value part after the colon
        _, _, value_part = line.partition(":")
        value = value_part.strip().strip("\"'")

        if not value or _YAML_PLACEHOLDER_RE.match(value):
            continue

        preview = value[:8] + "…" if len(value) > 8 else value
        key_match = _YAML_SECRET_KEY_RE.match(line)
        key_name = key_match.group(1) if key_match else "unknown"

        findings.append({
            "severity":      "HIGH",
            "line":          lineno,
            "key":           key_name,
            "value_preview": preview,
            "filename":      filename,
            "message": (
                f"Line {lineno} of '{filename}': key '{key_name}' appears to contain "
                f"a plaintext secret ('{preview}'). Use environment variables or a "
                "secrets manager instead."
            ),
        })
        log.warning(
            "Possible plaintext secret in YAML: file=%s line=%d key=%s",
            filename, lineno, key_name,
        )

    return findings
