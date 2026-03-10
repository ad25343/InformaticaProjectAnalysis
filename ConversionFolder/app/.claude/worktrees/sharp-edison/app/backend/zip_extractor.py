"""
zip_extractor.py — ZIP upload handling for the Informatica Conversion Tool.

Two public functions:

extract_informatica_zip(zip_bytes) -> ZipParseResult
    Single-mapping ZIP: one or more of mapping/workflow/param files in a flat
    archive (or any folder structure — first mapping found wins).

extract_batch_zip(zip_bytes) -> list[ZipParseResult]
    Batch ZIP (v2.0): one subfolder per mapping.  Each subfolder must contain
    at least one Mapping XML; workflow XML and parameter file are optional.
    Returns one ZipParseResult per valid mapping folder, ordered by folder name.

Security
--------
All extraction is delegated to security.safe_zip_extract() which guards against:
  - Zip Slip (path traversal)
  - Zip Bombs (size and entry-count limits)
  - Symlink entries (silently skipped)

File-type detection delegates to session_parser_agent._detect_type() so
all ZIP routes share exactly the same detection logic as the three-file route.
"""
from __future__ import annotations

import logging
import posixpath
from typing import Optional

from .security import safe_zip_extract, validate_upload_size, ZipExtractionError
from .agents.session_parser_agent import _detect_type
from .models.schemas import FileType

log = logging.getLogger("conversion.zip_extractor")


class ZipParseResult:
    """
    The typed output of extract_informatica_zip().

    Attributes
    ----------
    mapping_xml     : str  | None — content of the detected Mapping XML
    workflow_xml    : str  | None — content of the detected Workflow XML
    parameter_file  : str  | None — content of the detected parameter file
    mapping_filename  : str | None — original filename of the mapping entry
    workflow_filename : str | None — original filename of the workflow entry
    param_filename    : str | None — original filename of the parameter entry
    skipped         : list[str]   — entries that could not be classified
    warnings        : list[str]   — non-fatal issues (e.g. multiple mappings found)
    """

    def __init__(self) -> None:
        self.mapping_xml:      Optional[str] = None
        self.workflow_xml:     Optional[str] = None
        self.parameter_file:   Optional[str] = None
        self.mapping_filename: Optional[str] = None
        self.workflow_filename: Optional[str] = None
        self.param_filename:   Optional[str] = None
        self.skipped:  list[str] = []
        self.warnings: list[str] = []


def extract_informatica_zip(zip_bytes: bytes) -> ZipParseResult:
    """
    Safely extract and classify files from a ZIP archive.

    The archive should contain one or more of:
      - An Informatica Mapping XML export  (.xml with <MAPPING> element)
      - An Informatica Workflow XML export (.xml with <WORKFLOW>/<SESSION> elements)
      - An Informatica parameter file      (.txt / .par with $$VAR=value lines)

    Parameters
    ----------
    zip_bytes : bytes
        Raw bytes of the uploaded ZIP file (already read from the UploadFile stream).
        Size validation against MAX_UPLOAD_BYTES should be done *before* calling this.

    Returns
    -------
    ZipParseResult

    Raises
    ------
    ZipExtractionError  — on any ZIP safety violation (re-raised from security module)
    fastapi.HTTPException (413) — if total extracted bytes exceed the ZIP bomb limit
        (raised inside safe_zip_extract via the security module)
    """
    # ── 1. Extract safely (Zip Slip + Zip Bomb + symlink protection) ─────────
    extracted: dict[str, bytes] = safe_zip_extract(zip_bytes)
    log.info("ZIP extracted: %d entries", len(extracted))

    result = ZipParseResult()

    # ── 2. Classify each entry by its content ────────────────────────────────
    for name, content_bytes in extracted.items():
        # Skip macOS / Windows metadata files
        lower = name.lower()
        if lower.startswith("__macosx/") or lower.startswith(".") or lower.endswith(".ds_store"):
            log.debug("Skipping metadata entry: %s", name)
            continue

        try:
            text = content_bytes.decode("utf-8", errors="replace")
        except Exception:
            log.warning("Could not decode entry as UTF-8, skipping: %s", name)
            result.skipped.append(name)
            continue

        detected = _detect_type(text)
        log.debug("Entry '%s' detected as %s", name, detected.value)

        if detected == FileType.MAPPING:
            if result.mapping_xml is not None:
                result.warnings.append(
                    f"Multiple Mapping XML files found — using first detected "
                    f"('{result.mapping_filename}'). Ignoring '{name}'."
                )
                log.warning("Duplicate mapping entry ignored: %s", name)
            else:
                result.mapping_xml = text
                result.mapping_filename = name

        elif detected == FileType.WORKFLOW:
            if result.workflow_xml is not None:
                result.warnings.append(
                    f"Multiple Workflow XML files found — using first detected "
                    f"('{result.workflow_filename}'). Ignoring '{name}'."
                )
                log.warning("Duplicate workflow entry ignored: %s", name)
            else:
                result.workflow_xml = text
                result.workflow_filename = name

        elif detected == FileType.PARAMETER:
            if result.parameter_file is not None:
                result.warnings.append(
                    f"Multiple parameter files found — using first detected "
                    f"('{result.param_filename}'). Ignoring '{name}'."
                )
                log.warning("Duplicate parameter entry ignored: %s", name)
            else:
                result.parameter_file = text
                result.param_filename = name

        else:
            log.debug("Unclassified entry skipped: %s", name)
            result.skipped.append(name)

    # ── 3. Sanity check ──────────────────────────────────────────────────────
    if result.mapping_xml is None:
        raise ZipExtractionError(
            "No Informatica Mapping XML (<MAPPING> element) found inside the ZIP. "
            "The archive must contain at least one mapping export."
        )

    log.info(
        "ZIP classification complete: mapping=%s workflow=%s params=%s skipped=%d",
        result.mapping_filename,
        result.workflow_filename,
        result.param_filename,
        len(result.skipped),
    )

    return result


def extract_batch_zip(zip_bytes: bytes) -> list[ZipParseResult]:
    """
    Extract and classify files from a batch ZIP archive (v2.0).

    Expected structure — one subfolder per mapping::

        batch.zip/
          mapping1/
            mapping.xml        ← required (<MAPPING> element)
            workflow.xml       ← optional
            params.txt         ← optional
          mapping2/
            mapping.xml
            ...

    Rules
    -----
    - Top-level files (not inside any folder) are ignored with a warning.
    - Folders with no Mapping XML are skipped with a warning.
    - Each valid folder produces one ZipParseResult; results are returned
      sorted by folder name for deterministic ordering.
    - macOS (__MACOSX/) and hidden entries (dot-prefixed) are always skipped.

    Parameters
    ----------
    zip_bytes : bytes
        Raw bytes of the uploaded ZIP.

    Returns
    -------
    list[ZipParseResult]
        One entry per mapping folder.  Empty list if no valid folders found.

    Raises
    ------
    ZipExtractionError  — if the archive fails security checks or no valid
                          mapping folders are found at all.
    """
    # ── 1. Extract safely (Zip Slip + Zip Bomb + symlink protection) ─────────
    extracted: dict[str, bytes] = safe_zip_extract(zip_bytes)
    log.info("Batch ZIP extracted: %d entries", len(extracted))

    # ── 2. Group entries by top-level folder ─────────────────────────────────
    # key: folder name (str), value: dict[relative_path -> bytes]
    folders: dict[str, dict[str, bytes]] = {}
    top_level_files: list[str] = []

    for name, content_bytes in extracted.items():
        # Skip macOS / Windows metadata
        lower = name.lower()
        if lower.startswith("__macosx/") or lower.startswith(".") or lower.endswith(".ds_store"):
            log.debug("Skipping metadata entry: %s", name)
            continue

        # Normalise path separators to forward-slash
        name = name.replace("\\", "/")
        parts = name.split("/")

        if len(parts) < 2 or parts[0] == "":
            # Top-level file — not inside any subfolder
            top_level_files.append(name)
            log.debug("Ignoring top-level (non-folder) entry: %s", name)
            continue

        folder = parts[0]
        rel_path = "/".join(parts[1:])
        if not rel_path:
            continue  # directory entry only

        folders.setdefault(folder, {})[rel_path] = content_bytes

    if top_level_files:
        log.warning(
            "Batch ZIP: %d top-level file(s) ignored (must be inside a subfolder): %s",
            len(top_level_files), top_level_files,
        )

    if not folders:
        raise ZipExtractionError(
            "No subfolders found in the batch ZIP. "
            "Each mapping must be in its own subfolder (e.g. mapping1/mapping.xml)."
        )

    # ── 3. Classify each folder ───────────────────────────────────────────────
    results: list[ZipParseResult] = []

    for folder_name in sorted(folders.keys()):
        entries = folders[folder_name]
        folder_result = ZipParseResult()
        folder_result.warnings = []  # reset

        for rel_path, content_bytes in entries.items():
            try:
                text = content_bytes.decode("utf-8", errors="replace")
            except Exception:
                log.warning("Could not decode '%s/%s' as UTF-8, skipping", folder_name, rel_path)
                folder_result.skipped.append(f"{folder_name}/{rel_path}")
                continue

            detected = _detect_type(text)
            log.debug("Batch entry '%s/%s' detected as %s", folder_name, rel_path, detected.value)

            if detected == FileType.MAPPING:
                if folder_result.mapping_xml is not None:
                    folder_result.warnings.append(
                        f"Folder '{folder_name}': multiple Mapping XMLs found — "
                        f"using '{folder_result.mapping_filename}', ignoring '{rel_path}'."
                    )
                else:
                    folder_result.mapping_xml = text
                    folder_result.mapping_filename = f"{folder_name}/{rel_path}"

            elif detected == FileType.WORKFLOW:
                if folder_result.workflow_xml is not None:
                    folder_result.warnings.append(
                        f"Folder '{folder_name}': multiple Workflow XMLs found — "
                        f"using '{folder_result.workflow_filename}', ignoring '{rel_path}'."
                    )
                else:
                    folder_result.workflow_xml = text
                    folder_result.workflow_filename = f"{folder_name}/{rel_path}"

            elif detected == FileType.PARAMETER:
                if folder_result.parameter_file is not None:
                    folder_result.warnings.append(
                        f"Folder '{folder_name}': multiple parameter files found — "
                        f"using '{folder_result.param_filename}', ignoring '{rel_path}'."
                    )
                else:
                    folder_result.parameter_file = text
                    folder_result.param_filename = f"{folder_name}/{rel_path}"

            else:
                folder_result.skipped.append(f"{folder_name}/{rel_path}")

        if folder_result.mapping_xml is None:
            log.warning(
                "Batch ZIP: folder '%s' has no Mapping XML — skipping", folder_name
            )
            continue

        log.info(
            "Batch folder '%s': mapping=%s workflow=%s params=%s",
            folder_name,
            folder_result.mapping_filename,
            folder_result.workflow_filename,
            folder_result.param_filename,
        )
        results.append(folder_result)

    if not results:
        raise ZipExtractionError(
            "No valid mapping folders found in the batch ZIP. "
            "Each subfolder must contain an Informatica Mapping XML (<MAPPING> element)."
        )

    log.info("Batch ZIP classification complete: %d mapping(s) found", len(results))
    return results
