"""
STEP 0 — Session & Parameter Parser  (v1.1)

Responsibilities
----------------
1. Auto-detect the type of each uploaded file (MAPPING / WORKFLOW / PARAMETER / UNKNOWN)
   from its XML structure or content.
2. Cross-reference validation: the Session task inside a Workflow XML must reference
   the same mapping name found in the Mapping XML before the pipeline is allowed to run.
3. Session config extraction: pull connections, reject-file config, pre/post SQL,
   commit intervals, error thresholds, and any other SESSTRANSFORMINSTATTR rows from
   the Workflow XML.
4. Parameter file resolution: parse $$VARIABLE=value lines and resolve any $$VARS
   referenced in session attributes or SQL overrides.

Returns a SessionParseReport which is stored on the job and threaded through to
documentation_agent (for context injection) and conversion_agent (YAML artifacts).
"""
from __future__ import annotations

import re
from typing import Optional

from lxml import etree

from ..security import safe_parse_xml
from ..models.schemas import (
    CrossRefValidation,
    FileType,
    ParameterEntry,
    SessionConfig,
    SessionConnection,
    SessionParseReport,
    UploadedFile,
)
from datetime import datetime, timezone


# ─────────────────────────────────────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────────────────────────────────────

def parse(
    mapping_xml:    Optional[str],
    workflow_xml:   Optional[str] = None,
    parameter_file: Optional[str] = None,
) -> SessionParseReport:
    """
    Run Step 0.

    Parameters
    ----------
    mapping_xml     Required — the Informatica Mapping XML export.
    workflow_xml    Optional — the Workflow XML that contains a Session referencing
                    the mapping.
    parameter_file  Optional — plain-text parameter file ($$VAR=value lines).

    Returns
    -------
    SessionParseReport
    """
    now = datetime.now(timezone.utc).isoformat()
    uploaded_files: list[UploadedFile] = []
    notes: list[str] = []

    # ── 1. Detect file types ────────────────────────────────────────────────
    mapping_type = _detect_type(mapping_xml) if mapping_xml else FileType.UNKNOWN
    uploaded_files.append(UploadedFile(
        filename="mapping.xml",
        file_type=mapping_type,
        detected_at=now,
    ))

    if workflow_xml is not None:
        wf_type = _detect_type(workflow_xml)
        uploaded_files.append(UploadedFile(
            filename="workflow.xml",
            file_type=wf_type,
            detected_at=now,
        ))
    else:
        wf_type = None

    if parameter_file is not None:
        uploaded_files.append(UploadedFile(
            filename="parameter_file.txt",
            file_type=FileType.PARAMETER,
            detected_at=now,
        ))

    # ── 2. Cross-reference validation ───────────────────────────────────────
    cross_ref = _cross_reference(
        mapping_xml=mapping_xml,
        workflow_xml=workflow_xml if wf_type == FileType.WORKFLOW else None,
    )

    # Stop early if mapping XML is completely missing / wrong type
    if mapping_xml is None or mapping_type == FileType.UNKNOWN:
        return SessionParseReport(
            uploaded_files=uploaded_files,
            cross_ref=cross_ref,
            parse_status="FAILED",
            notes=["Mapping XML is missing or could not be identified."],
        )

    if mapping_type != FileType.MAPPING:
        return SessionParseReport(
            uploaded_files=uploaded_files,
            cross_ref=cross_ref,
            parse_status="FAILED",
            notes=[f"Uploaded mapping file was detected as {mapping_type.value}, not MAPPING."],
        )

    # ── 3. Parameter file resolution ────────────────────────────────────────
    raw_params = _parse_parameter_file(parameter_file) if parameter_file else []

    # Build a lookup dict for variable substitution
    param_lookup: dict[str, str] = {p.name.upper(): p.value for p in raw_params}

    # ── 4. Session config extraction ────────────────────────────────────────
    session_config: Optional[SessionConfig] = None
    unresolved_variables: list[str] = []

    if workflow_xml and wf_type == FileType.WORKFLOW:
        session_config, unresolved = _extract_session_config(workflow_xml, param_lookup)
        unresolved_variables = unresolved
        if session_config is None:
            notes.append("Workflow XML present but no SESSION task could be extracted.")
    elif workflow_xml:
        notes.append(
            f"workflow.xml was detected as {wf_type.value if wf_type else 'UNKNOWN'} "
            "rather than WORKFLOW — session config skipped."
        )

    # ── 5. Determine parse status ────────────────────────────────────────────
    if cross_ref.status == "INVALID":
        parse_status = "FAILED"
        notes.append("Cross-reference validation failed — see cross_ref.issues for details.")
    elif cross_ref.status == "WARNINGS":
        parse_status = "PARTIAL"
    elif workflow_xml and session_config is None:
        parse_status = "PARTIAL"
    elif workflow_xml is None:
        parse_status = "MAPPING_ONLY"
    else:
        parse_status = "COMPLETE"

    return SessionParseReport(
        uploaded_files=uploaded_files,
        cross_ref=cross_ref,
        session_config=session_config,
        parameters=raw_params,
        unresolved_variables=unresolved_variables,
        parse_status=parse_status,
        notes=notes,
    )


# ─────────────────────────────────────────────────────────────────────────────
# File-type auto-detection
# ─────────────────────────────────────────────────────────────────────────────

def _detect_type(content: Optional[str]) -> FileType:
    """Infer whether content is a Mapping XML, Workflow XML, or Parameter file."""
    if not content:
        return FileType.UNKNOWN

    stripped = content.strip()

    # Parameter files: plain text, no XML declaration
    if not stripped.startswith("<") and "$$" in stripped:
        return FileType.PARAMETER

    # Try to find the marker elements without full parsing (faster, tolerant)
    try:
        root = safe_parse_xml(stripped)
    except Exception:
        # Not valid XML — check for parameter file patterns
        if re.search(r"\$\$\w+\s*=", stripped):
            return FileType.PARAMETER
        return FileType.UNKNOWN

    # Look for <MAPPING> element anywhere in tree
    if root.find(".//{http://powermart.informatica.com/DTD/PowerMart}MAPPING") is not None \
            or root.find(".//MAPPING") is not None:
        return FileType.MAPPING

    # Look for <WORKFLOW> element
    if root.find(".//{http://powermart.informatica.com/DTD/PowerMart}WORKFLOW") is not None \
            or root.find(".//WORKFLOW") is not None:
        # Confirm it contains SESSION tasks (otherwise it might be a worklet)
        if root.find(".//TASKINSTANCE[@TASKTYPE='Session']") is not None \
                or root.find(".//SESSION") is not None \
                or root.find(".//TASK[@TYPE='Session']") is not None:
            return FileType.WORKFLOW

    return FileType.UNKNOWN


# ─────────────────────────────────────────────────────────────────────────────
# Cross-reference validation
# ─────────────────────────────────────────────────────────────────────────────

def _extract_mapping_name(mapping_xml: str) -> Optional[str]:
    """Pull the first MAPPING/@NAME from the mapping XML."""
    try:
        root = safe_parse_xml(mapping_xml)
    except Exception:
        return None
    el = root.find(".//MAPPING")
    if el is None:
        el = root.find(".//{http://powermart.informatica.com/DTD/PowerMart}MAPPING")
    return el.get("NAME") if el is not None else None


def _extract_session_mapping_ref(workflow_xml: str) -> tuple[Optional[str], Optional[str]]:
    """
    Pull (session_name, referenced_mapping_name) from the workflow XML.

    Informatica stores the mapping link in different places depending on
    PowerCenter version:
      - SESSTRANSFORMINSTATTR row with ATTRIBUTENAME='Mapping name'
      - SESSION/@MAPPINGNAME (older exports)
      - INSTANCE/@REUSABLE_INSTANCE_NAME pointing to a MAPPING
    """
    try:
        root = safe_parse_xml(workflow_xml)
    except Exception:
        return None, None

    session_name: Optional[str] = None
    ref_mapping: Optional[str] = None

    # Find the SESSION element
    session_el = root.find(".//SESSION")
    if session_el is None:
        session_el = root.find(".//TASK[@TYPE='Session']")
    if session_el is None:
        return None, None

    session_name = session_el.get("NAME") or session_el.get("TASKNAME")

    # Direct attribute on SESSION
    ref_mapping = session_el.get("MAPPINGNAME")

    if not ref_mapping:
        # Look in SESSTRANSFORMINSTATTR / SESSIONEXTENSION
        for attr in root.iter("SESSTRANSFORMINSTATTR"):
            if (attr.get("ATTRIBUTENAME") or "").lower() in ("mapping name", "mappingname"):
                ref_mapping = attr.get("ATTRIBUTEVALUE")
                break

    if not ref_mapping:
        # Look in CONFIG / ATTRIBUTE elements
        for attr in root.iter("ATTRIBUTE"):
            if (attr.get("NAME") or "").lower() in ("mapping name", "mappingname"):
                ref_mapping = attr.get("VALUE") or attr.get("ATTRIBUTEVALUE")
                break

    if not ref_mapping:
        # Fallback: first INSTANCE that looks like a mapping instance
        for inst in root.iter("INSTANCE"):
            if inst.get("TYPE") == "Mapping" or inst.get("REUSABLE_INSTANCE_NAME"):
                ref_mapping = inst.get("REUSABLE_INSTANCE_NAME") or inst.get("NAME")
                break

    return session_name, ref_mapping


def _cross_reference(
    mapping_xml: Optional[str],
    workflow_xml: Optional[str],
) -> CrossRefValidation:
    """Build the CrossRefValidation result."""
    issues: list[str] = []

    mapping_name = _extract_mapping_name(mapping_xml) if mapping_xml else None
    if not mapping_name:
        issues.append("Could not extract MAPPING/@NAME from the Mapping XML.")

    if workflow_xml is None:
        # No workflow — valid for mapping-only mode
        return CrossRefValidation(
            status="VALID",
            mapping_name=mapping_name,
        )

    session_name, ref_mapping = _extract_session_mapping_ref(workflow_xml)

    if not session_name:
        issues.append("Could not find a SESSION task inside the Workflow XML.")

    if not ref_mapping:
        issues.append(
            "Could not determine which mapping the Session references "
            "(MAPPINGNAME attribute or SESSTRANSFORMINSTATTR not found)."
        )

    if mapping_name and ref_mapping and mapping_name != ref_mapping:
        issues.append(
            f"Mapping name mismatch: Mapping XML contains '{mapping_name}' "
            f"but Session references '{ref_mapping}'."
        )

    if issues:
        # Distinguish hard mismatches from soft warnings
        is_hard = any("mismatch" in i.lower() or "could not" in i.lower() for i in issues)
        status = "INVALID" if is_hard and mapping_name and ref_mapping else "WARNINGS"
        # If we have both names and they match despite other issues → WARNINGS only
        if mapping_name and ref_mapping and mapping_name == ref_mapping:
            status = "WARNINGS"
    else:
        status = "VALID"

    return CrossRefValidation(
        status=status,
        mapping_name=mapping_name,
        session_name=session_name,
        referenced_mapping=ref_mapping,
        issues=issues,
    )


# ─────────────────────────────────────────────────────────────────────────────
# Parameter file parsing
# ─────────────────────────────────────────────────────────────────────────────

def _parse_parameter_file(content: str) -> list[ParameterEntry]:
    """
    Parse an Informatica parameter file.

    Format:
        [folder.workflow]        ← scope header (optional)
        $$VARIABLE=value
        # comment lines ignored
    """
    params: list[ParameterEntry] = []
    current_scope = "GLOBAL"

    for raw_line in content.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue

        # Scope header: [FolderName.WorkflowName] or [FolderName.WorkflowName.SessionName]
        if line.startswith("[") and line.endswith("]"):
            header = line[1:-1]
            parts = header.split(".")
            if len(parts) >= 3:
                current_scope = "SESSION"
            elif len(parts) == 2:
                current_scope = "WORKFLOW"
            else:
                current_scope = "GLOBAL"
            continue

        # Variable assignment
        if "=" in line:
            name, _, value = line.partition("=")
            name = name.strip()
            value = value.strip()
            params.append(ParameterEntry(name=name, value=value, scope=current_scope))

    return params


# ─────────────────────────────────────────────────────────────────────────────
# Session config extraction
# ─────────────────────────────────────────────────────────────────────────────

_PARAM_RE = re.compile(r"\$\$[A-Z0-9_]+", re.IGNORECASE)


def _resolve(value: str, lookup: dict[str, str]) -> tuple[str, list[str]]:
    """
    Replace $$VARIABLES in a string using the lookup dict.
    Returns (resolved_value, list_of_unresolved_names).
    """
    unresolved: list[str] = []

    def replacer(match: re.Match) -> str:
        var = match.group(0).upper()
        if var in lookup:
            return lookup[var]
        unresolved.append(match.group(0))
        return match.group(0)  # leave as-is

    resolved = _PARAM_RE.sub(replacer, value)
    return resolved, unresolved


def _extract_session_config(
    workflow_xml: str,
    param_lookup: dict[str, str],
) -> tuple[Optional[SessionConfig], list[str]]:
    """
    Extract a SessionConfig from the Workflow XML.

    Returns (SessionConfig or None, list_of_unresolved_variable_names).
    """
    try:
        root = safe_parse_xml(workflow_xml)
    except Exception:
        return None, []

    # Locate the SESSION element
    session_el = root.find(".//SESSION")
    if session_el is None:
        session_el = root.find(".//TASK[@TYPE='Session']")
    if session_el is None:
        return None, []

    session_name = session_el.get("NAME") or session_el.get("TASKNAME") or "UNKNOWN_SESSION"
    mapping_name = session_el.get("MAPPINGNAME") or "UNKNOWN_MAPPING"

    # Workflow name
    workflow_el = root.find(".//WORKFLOW")
    if workflow_el is None:
        workflow_el = root.find(".//WORKFLOW[@NAME]")
    workflow_name = (workflow_el.get("NAME") if workflow_el is not None else None) or "UNKNOWN_WORKFLOW"

    all_unresolved: list[str] = []
    raw_attributes: dict[str, str] = {}

    # ── Collect all session attribute rows ──────────────────────────────────
    for attr in session_el.iter("SESSTRANSFORMINSTATTR", "SESSIONEXTENSION", "ATTRIBUTE"):
        attr_name  = attr.get("ATTRIBUTENAME") or attr.get("NAME") or ""
        attr_value = attr.get("ATTRIBUTEVALUE") or attr.get("VALUE") or ""
        if attr_name:
            resolved, unresolved = _resolve(attr_value, param_lookup)
            raw_attributes[attr_name] = resolved
            all_unresolved.extend(unresolved)

    # Also pick up direct attributes on the SESSION element itself
    for k, v in session_el.attrib.items():
        if k not in raw_attributes:
            resolved, unresolved = _resolve(v, param_lookup)
            raw_attributes[k] = resolved
            all_unresolved.extend(unresolved)

    # ── Well-known attributes ────────────────────────────────────────────────
    def _get(*keys: str) -> Optional[str]:
        for k in keys:
            v = raw_attributes.get(k)
            if v:
                return v
        return None

    pre_sql      = _get("Pre SQL",  "Pre-session SQL", "PRE_SQL",  "PreSQL")
    post_sql     = _get("Post SQL", "Post-session SQL", "POST_SQL", "PostSQL")
    reject_fname = _get("Reject Filename", "RejectFilename", "REJECTFILE")
    reject_fdir  = _get("Reject File Directory", "RejectFiledir", "REJECTFILEDIR")

    commit_raw   = _get("Commit Interval", "CommitInterval", "COMMIT_INTERVAL")
    error_raw    = _get("Stop On Errors",  "ErrorThreshold", "STOPONERRORS")

    commit_interval: Optional[int] = None
    error_threshold: Optional[int] = None
    try:
        commit_interval = int(commit_raw) if commit_raw else None
    except ValueError:
        pass
    try:
        error_threshold = int(error_raw) if error_raw else None
    except ValueError:
        pass

    # ── Connections ──────────────────────────────────────────────────────────
    connections: list[SessionConnection] = []
    for inst in root.iter("SESSIONINSTCONFIG", "SESSTRANSFORMINSTATTR"):
        pass  # handled below

    for conn_el in root.iter("CONNECTIONREFERENCE"):
        conn_name  = conn_el.get("CONNECTIONNAME") or conn_el.get("DBDNAME")
        conn_type  = conn_el.get("CONNECTIONSUBTYPE") or conn_el.get("CONNECTIONTYPE")
        trans_name = conn_el.get("TRANSFORMATIONINSTANCENAME") or conn_el.get("TRANSFORMATIONNAME")
        role       = "SOURCE" if (conn_el.get("ROLE") or "").upper() == "SOURCE" else "TARGET"

        if not trans_name:
            trans_name = conn_el.get("INSTANCENAME") or "UNKNOWN"

        connections.append(SessionConnection(
            transformation_name=trans_name,
            role=role,
            connection_name=conn_name,
            connection_type=conn_type,
        ))

    # Fallback: scan SESSTRANSFORMINSTATTR for connection info
    if not connections:
        for attr in session_el.iter("SESSTRANSFORMINSTATTR"):
            attr_name  = (attr.get("ATTRIBUTENAME") or "").lower()
            trans_name = attr.get("TRANSFORMATIONINSTANCENAME") or attr.get("INSTANCENAME") or ""
            attr_value = attr.get("ATTRIBUTEVALUE") or ""

            if "connection" in attr_name and attr_value and trans_name:
                # Infer role from transformation name heuristic
                role = "SOURCE" if any(
                    k in trans_name.upper() for k in ("SQ_", "SRC", "SOURCE")
                ) else "TARGET"
                connections.append(SessionConnection(
                    transformation_name=trans_name,
                    role=role,
                    connection_name=attr_value,
                ))

    # ── File-based connections ───────────────────────────────────────────────
    for attr in session_el.iter("SESSTRANSFORMINSTATTR"):
        attr_name  = (attr.get("ATTRIBUTENAME") or "").lower()
        trans_name = attr.get("TRANSFORMATIONINSTANCENAME") or attr.get("INSTANCENAME") or ""
        attr_value = attr.get("ATTRIBUTEVALUE") or ""

        if "file name" in attr_name or "filename" in attr_name:
            # Check if we already have an entry for this transformation
            existing = next(
                (c for c in connections if c.transformation_name == trans_name), None
            )
            if existing:
                existing.file_name = attr_value
            else:
                role = "SOURCE" if any(
                    k in trans_name.upper() for k in ("SQ_", "SRC", "SOURCE")
                ) else "TARGET"
                connections.append(SessionConnection(
                    transformation_name=trans_name,
                    role=role,
                    file_name=attr_value,
                    connection_type="FILE",
                ))

        elif "file dir" in attr_name or "filedir" in attr_name:
            existing = next(
                (c for c in connections if c.transformation_name == trans_name), None
            )
            if existing:
                existing.file_dir = attr_value

    return SessionConfig(
        session_name=session_name,
        mapping_name=mapping_name,
        workflow_name=workflow_name,
        connections=connections,
        pre_session_sql=pre_sql,
        post_session_sql=post_sql,
        commit_interval=commit_interval,
        error_threshold=error_threshold,
        reject_filename=reject_fname,
        reject_filedir=reject_fdir,
        raw_attributes=raw_attributes,
    ), list(set(all_unresolved))
