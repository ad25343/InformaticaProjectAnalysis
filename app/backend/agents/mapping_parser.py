"""
Mapping XML parser — extracts structural components from Informatica
PowerCenter mapping XML exports.

Phase 1, Step 1.2: Parse each mapping XML into a MappingParseResult.

Security: Uses defusedxml for XXE-hardened parsing. DTD loading and
entity resolution are disabled.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path

from defusedxml import ElementTree as ET

from app.backend.models.schemas import (
    Connector,
    MappingDef,
    MappingParseResult,
    ParameterDef,
    SourceDef,
    SourceField,
    TableAttribute,
    TargetDef,
    TargetField,
    TransformationDef,
    TransformPort,
)

logger = logging.getLogger(__name__)


class MappingParseError(Exception):
    """Raised when a mapping XML cannot be parsed."""


def parse_mapping_xml(file_path: str | Path) -> MappingParseResult:
    """
    Parse a single Informatica PowerCenter mapping XML file.

    Extracts:
    - Sources with fields
    - Targets with fields
    - Transformations with ports, expressions, and table attributes
    - Mapping connectors (the wiring topology)
    - Parameters
    - Target load order

    Args:
        file_path: Path to the mapping XML file.

    Returns:
        MappingParseResult with all extracted components.
    """
    file_path = Path(file_path)

    if not file_path.exists():
        raise MappingParseError(f"File not found: {file_path}")

    # Compute content hash for caching
    raw_bytes = file_path.read_bytes()
    file_hash = hashlib.sha256(raw_bytes).hexdigest()

    # Parse XML (XXE-hardened via defusedxml)
    try:
        tree = ET.parse(str(file_path))
    except ET.ParseError as e:
        raise MappingParseError(f"Invalid XML in {file_path}: {e}") from e

    root = tree.getroot()

    # Navigate: POWERMART > REPOSITORY > FOLDER
    folder = _find_folder(root)
    if folder is None:
        raise MappingParseError(
            f"No FOLDER element found in {file_path}. "
            "Expected POWERMART > REPOSITORY > FOLDER structure."
        )

    # Extract components
    sources = _extract_sources(folder)
    targets = _extract_targets(folder)
    transformations = _extract_transformations(folder)
    mapping, connectors, load_order = _extract_mapping(folder)
    parameters = _extract_parameters(folder)
    parse_errors: list[str] = []

    if mapping is None:
        parse_errors.append("No MAPPING element found in FOLDER")
        mapping = MappingDef(name=file_path.stem)

    mapping.connectors = connectors
    mapping.target_load_order = load_order

    result = MappingParseResult(
        file_path=str(file_path),
        file_hash=file_hash,
        mapping=mapping,
        sources=sources,
        targets=targets,
        transformations=transformations,
        parameters=parameters,
        parse_errors=parse_errors,
    )

    logger.debug(
        "Parsed %s: %d sources, %d targets, %d transformations, %d connectors",
        result.mapping_name,
        len(sources),
        len(targets),
        len(transformations),
        len(connectors),
    )

    return result


# ---------------------------------------------------------------------------
# Internal extraction helpers
# ---------------------------------------------------------------------------


def _find_folder(root: ET.Element) -> ET.Element | None:
    """Navigate POWERMART > REPOSITORY > FOLDER."""
    # Direct FOLDER child
    folder = root.find(".//FOLDER")
    return folder


def _extract_sources(folder: ET.Element) -> list[SourceDef]:
    """Extract SOURCE elements and their SOURCEFIELDs."""
    sources = []
    for src_elem in folder.findall("SOURCE"):
        fields = []
        for field_elem in src_elem.findall("SOURCEFIELD"):
            fields.append(SourceField(
                name=field_elem.get("NAME", ""),
                datatype=field_elem.get("DATATYPE", ""),
                precision=int(field_elem.get("PRECISION", "0") or "0"),
                scale=int(field_elem.get("SCALE", "0") or "0"),
                nullable=field_elem.get("NULLABLE", "YES") != "NOTNULL",
                key_type=field_elem.get("KEYTYPE", ""),
            ))

        sources.append(SourceDef(
            name=src_elem.get("NAME", ""),
            db_type=src_elem.get("DATABASETYPE", ""),
            dbdname=src_elem.get("DBDNAME", ""),
            owner=src_elem.get("OWNERNAME", ""),
            description=src_elem.get("DESCRIPTION", ""),
            fields=fields,
        ))

    return sources


def _extract_targets(folder: ET.Element) -> list[TargetDef]:
    """Extract TARGET elements and their TARGETFIELDs."""
    targets = []
    for tgt_elem in folder.findall("TARGET"):
        fields = []
        for field_elem in tgt_elem.findall("TARGETFIELD"):
            fields.append(TargetField(
                name=field_elem.get("NAME", ""),
                datatype=field_elem.get("DATATYPE", ""),
                precision=int(field_elem.get("PRECISION", "0") or "0"),
                scale=int(field_elem.get("SCALE", "0") or "0"),
                nullable=field_elem.get("NULLABLE", "YES") != "NOTNULL",
                key_type=field_elem.get("KEYTYPE", ""),
            ))

        targets.append(TargetDef(
            name=tgt_elem.get("NAME", ""),
            db_type=tgt_elem.get("DATABASETYPE", ""),
            dbdname=tgt_elem.get("DBDNAME", ""),
            owner=tgt_elem.get("OWNERNAME", ""),
            description=tgt_elem.get("DESCRIPTION", ""),
            fields=fields,
        ))

    return targets


def _extract_transformations(folder: ET.Element) -> list[TransformationDef]:
    """Extract TRANSFORMATION elements with ports and table attributes."""
    transforms = []
    for tx_elem in folder.findall("TRANSFORMATION"):
        ports = []
        for port_elem in tx_elem.findall("TRANSFORMFIELD"):
            ports.append(TransformPort(
                name=port_elem.get("NAME", ""),
                datatype=port_elem.get("DATATYPE", ""),
                port_type=port_elem.get("PORTTYPE", ""),
                expression=port_elem.get("EXPRESSION", ""),
                precision=int(port_elem.get("PRECISION", "0") or "0"),
                scale=int(port_elem.get("SCALE", "0") or "0"),
                default_value=port_elem.get("DEFAULTVALUE", ""),
            ))

        table_attrs = []
        for attr_elem in tx_elem.findall("TABLEATTRIBUTE"):
            table_attrs.append(TableAttribute(
                name=attr_elem.get("NAME", ""),
                value=attr_elem.get("VALUE", ""),
            ))

        transforms.append(TransformationDef(
            name=tx_elem.get("NAME", ""),
            type=tx_elem.get("TYPE", ""),
            description=tx_elem.get("DESCRIPTION", ""),
            reusable=tx_elem.get("REUSABLE", "NO") == "YES",
            ports=ports,
            table_attributes=table_attrs,
        ))

    return transforms


def _extract_mapping(
    folder: ET.Element,
) -> tuple[MappingDef | None, list[Connector], list[str]]:
    """Extract the MAPPING element with CONNECTORs and TARGETLOADORDER."""
    mapping_elem = folder.find("MAPPING")
    if mapping_elem is None:
        return None, [], []

    connectors = []
    for conn_elem in mapping_elem.findall("CONNECTOR"):
        connectors.append(Connector(
            from_instance=conn_elem.get("FROMINSTANCE", ""),
            from_instance_type=conn_elem.get("FROMINSTANCETYPE", ""),
            from_field=conn_elem.get("FROMFIELD", ""),
            to_instance=conn_elem.get("TOINSTANCE", ""),
            to_instance_type=conn_elem.get("TOINSTANCETYPE", ""),
            to_field=conn_elem.get("TOFIELD", ""),
        ))

    load_order = []
    for order_elem in mapping_elem.findall("TARGETLOADORDER"):
        target_name = order_elem.get("TARGETINSTANCE", "")
        if target_name:
            load_order.append(target_name)

    mapping_def = MappingDef(
        name=mapping_elem.get("NAME", ""),
        description=mapping_elem.get("DESCRIPTION", ""),
        is_valid=mapping_elem.get("ISVALID", "YES") == "YES",
        connectors=connectors,
        target_load_order=load_order,
    )

    return mapping_def, connectors, load_order


def _extract_parameters(folder: ET.Element) -> list[ParameterDef]:
    """Extract parameter definitions ($$VAR style) if present."""
    params = []
    # Parameters can appear as MAPPINGVARIABLE inside MAPPING
    mapping_elem = folder.find("MAPPING")
    if mapping_elem is not None:
        for var_elem in mapping_elem.findall("MAPPINGVARIABLE"):
            params.append(ParameterDef(
                name=var_elem.get("NAME", ""),
                datatype=var_elem.get("DATATYPE", ""),
                default_value=var_elem.get("DEFAULTVALUE", ""),
            ))
    return params
