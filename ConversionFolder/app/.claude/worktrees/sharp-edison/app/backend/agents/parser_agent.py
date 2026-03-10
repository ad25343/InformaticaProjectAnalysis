"""
STEP 1 — XML Parser Agent
Deterministic lxml-based parser. Extracts all Informatica PowerCenter objects
and builds a structured representation of the mapping graph.
"""
from __future__ import annotations
import re
from typing import Any
from lxml import etree

from ..models.schemas import ParseReport, ParseFlag
from ..security import safe_parse_xml


# ─────────────────────────────────────────────
# Public entry point
# ─────────────────────────────────────────────

def parse_xml(xml_content: str) -> tuple[ParseReport, dict]:
    """
    Parse Informatica PowerCenter XML.
    Returns (ParseReport, graph_dict) where graph_dict is the full
    internal representation passed to downstream agents.
    """
    flags: list[ParseFlag] = []
    graph: dict[str, Any] = {
        "mappings":    [],
        "workflows":   [],
        "sources":     [],
        "targets":     [],
        "parameters":  [],
        "connections": [],
    }

    try:
        root = safe_parse_xml(xml_content)
    except etree.XMLSyntaxError as e:
        return ParseReport(
            objects_found={},
            reusable_components=[],
            unresolved_parameters=[],
            malformed_xml=[str(e)],
            unrecognized_elements=[],
            flags=[ParseFlag(flag_type="PARSE_ERROR", element="root", detail=str(e))],
            parse_status="FAILED",
            mapping_names=[],
        ), graph

    counts: dict[str, int] = {}
    reusable: list[str] = []
    unresolved_params: list[str] = []
    malformed: list[str] = []
    unknown_elements: list[str] = []

    # ── Sources ──────────────────────────────
    for src in root.iter("SOURCE"):
        counts["Source"] = counts.get("Source", 0) + 1
        graph["sources"].append(_extract_source(src))

    # ── Targets ──────────────────────────────
    for tgt in root.iter("TARGET"):
        counts["Target"] = counts.get("Target", 0) + 1
        graph["targets"].append(_extract_target(tgt))

    # ── Mappings ─────────────────────────────
    for mapping in root.iter("MAPPING"):
        counts["Mapping"] = counts.get("Mapping", 0) + 1
        m = _extract_mapping(mapping, flags, reusable, unresolved_params)
        graph["mappings"].append(m)

    # ── Workflows ────────────────────────────
    for wf in root.iter("WORKFLOW"):
        counts["Workflow"] = counts.get("Workflow", 0) + 1
        graph["workflows"].append(_extract_workflow(wf))

    # ── Reusable Transformations ──────────────
    for rt in root.iter("TRANSFORMATIONS"):
        for child in rt:
            if child.get("REUSABLE", "NO") == "YES":
                name = child.get("NAME", "unknown")
                reusable.append(f"{child.tag}:{name}")
                counts["ReusableTransformation"] = counts.get("ReusableTransformation", 0) + 1

    # ── Parameters (repository-level) ────────
    for param in root.iter("PARAMETER"):
        name = param.get("NAME", "")
        value = param.get("VALUE", "")
        graph["parameters"].append({"name": name, "value": value})
        counts["Parameter"] = counts.get("Parameter", 0) + 1
        if not value and name:
            unresolved_params.append(name)
            flags.append(ParseFlag(
                flag_type="UNRESOLVED_PARAMETER",
                element=name,
                detail="Parameter has no default value in the XML"
            ))

    # ── Determine parse status ────────────────
    if not graph["mappings"] and graph["workflows"]:
        # Workflow XML uploaded in the primary mapping slot — clear, actionable error.
        parse_status = "FAILED"
        wf_names = ", ".join(w.get("name", "?") for w in graph["workflows"][:5])
        flags.insert(0, ParseFlag(
            flag_type="WRONG_FILE_TYPE",
            element="root",
            detail=(
                f"This file contains {len(graph['workflows'])} Workflow definition(s) "
                f"({wf_names}) but no Mapping definitions. "
                "It looks like you uploaded a Workflow XML as the primary mapping file. "
                "Please re-upload: put the Mapping XML (.xml from Informatica Designer) "
                "in the required 'Mapping XML' field, and optionally put this file in "
                "the 'Workflow XML' field."
            )
        ))
    elif not graph["mappings"] and not graph["workflows"]:
        parse_status = "PARTIAL"
        flags.append(ParseFlag(
            flag_type="UNKNOWN_ELEMENT",
            element="root",
            detail="No MAPPING or WORKFLOW elements found — file may be a partial export"
        ))
    else:
        blocking = [f for f in flags if f.flag_type == "PARSE_ERROR"]
        parse_status = "FAILED" if blocking else ("PARTIAL" if flags else "COMPLETE")

    mapping_names = [m["name"] for m in graph["mappings"]]

    return ParseReport(
        objects_found=counts,
        reusable_components=reusable,
        unresolved_parameters=unresolved_params,
        malformed_xml=malformed,
        unrecognized_elements=unknown_elements,
        flags=flags,
        parse_status=parse_status,
        mapping_names=mapping_names,
    ), graph


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

def _extract_mapping(mapping_el: etree._Element, flags, reusable, unresolved_params) -> dict:
    name = mapping_el.get("NAME", "unknown")
    transformations = []
    connectors = []
    instance_map: dict[str, str] = {}  # instance name → transformation name

    # ── Transformation instances ──────────────
    for inst in mapping_el.iter("INSTANCE"):
        inst_name  = inst.get("NAME", "")
        trans_name = inst.get("TRANSFORMATION_NAME", inst_name)
        trans_type = inst.get("TYPE", "")
        instance_map[inst_name] = trans_name

    # ── Transformations ───────────────────────
    for trans in mapping_el.iter("TRANSFORMATION"):
        t = _extract_transformation(trans, flags)
        transformations.append(t)

    # ── Source instances in mapping ───────────
    for src_inst in mapping_el.iter("INSTANCE"):
        if src_inst.get("TYPE") in ("SOURCE", "TARGET"):
            pass  # handled via source/target lists

    # ── Connectors (links between ports) ─────
    for conn in mapping_el.iter("CONNECTOR"):
        connectors.append({
            "from_instance":     conn.get("FROMINSTANCE", ""),
            "from_field":        conn.get("FROMFIELD", ""),
            "to_instance":       conn.get("TOINSTANCE", ""),
            "to_field":          conn.get("TOFIELD", ""),
        })

    # ── Mapping-level parameters/variables ───
    mapping_params = []
    for param in mapping_el.iter("MAPPINGVARIABLE"):
        pname = param.get("NAME", "")
        dtype = param.get("DATATYPE", "")
        default = param.get("DEFAULTVALUE", "")
        mapping_params.append({"name": pname, "datatype": dtype, "default": default})
        if not default:
            unresolved_params.append(pname)

    return {
        "name":            name,
        "description":     mapping_el.get("DESCRIPTION", ""),
        "transformations": transformations,
        "connectors":      connectors,
        "parameters":      mapping_params,
        "instance_map":    instance_map,
    }


def _extract_transformation(trans_el: etree._Element, flags) -> dict:
    name      = trans_el.get("NAME", "unknown")
    ttype     = trans_el.get("TYPE", "unknown")
    reusable  = trans_el.get("REUSABLE", "NO")
    ports     = []
    expressions = []

    for field in trans_el.iter("TRANSFORMFIELD"):
        port = {
            "name":       field.get("NAME", ""),
            "datatype":   field.get("DATATYPE", ""),
            "porttype":   field.get("PORTTYPE", ""),   # INPUT | OUTPUT | INPUT/OUTPUT
            "expression": field.get("EXPRESSION", ""),
            "default":    field.get("DEFAULTVALUE", ""),
        }
        # Sorter-specific: capture sort key position and direction so the graph
        # summary can tell Claude the exact sort order (e.g. APPRAISAL_DATE DESC).
        # These attributes only appear on Sorter TRANSFORMFIELD elements.
        if ttype == "Sorter":
            sort_pos  = field.get("SORTKEYPOSITION", "")
            sort_dir  = field.get("SORTDIRECTION", "")
            if sort_pos:
                port["sort_key_position"] = sort_pos
            if sort_dir:
                port["sort_direction"] = sort_dir
        ports.append(port)
        if port["expression"]:
            expressions.append({
                "port":       port["name"],
                "expression": port["expression"],
            })

    # Collect table attributes (join condition, lookup condition, filter, etc.)
    table_attribs = {}
    for ta in trans_el.iter("TABLEATTRIBUTE"):
        table_attribs[ta.get("NAME", "")] = ta.get("VALUE", "")

    # Flag unsupported transformation types
    unsupported_types = {
        "Java Transformation", "External Procedure", "Advanced External Procedure",
        "Stored Procedure"
    }
    if ttype in unsupported_types:
        flags.append(ParseFlag(
            flag_type="UNSUPPORTED_TRANSFORMATION",
            element=name,
            detail=f"Transformation type '{ttype}' is not supported for automated conversion"
        ))

    return {
        "name":            name,
        "type":            ttype,
        "reusable":        reusable == "YES",
        "ports":           ports,
        "expressions":     expressions,
        "table_attribs":   table_attribs,
    }


def _extract_source(src_el: etree._Element) -> dict:
    fields = []
    for f in src_el.iter("SOURCEFIELD"):
        fields.append({
            "name":     f.get("NAME", ""),
            "datatype": f.get("DATATYPE", ""),
            "length":   f.get("LENGTH", ""),
        })
    return {
        "name":       src_el.get("NAME", ""),
        "db_type":    src_el.get("DATABASETYPE", ""),
        "owner":      src_el.get("OWNERNAME", ""),
        "fields":     fields,
    }


def _extract_target(tgt_el: etree._Element) -> dict:
    fields = []
    for f in tgt_el.iter("TARGETFIELD"):
        fields.append({
            "name":     f.get("NAME", ""),
            "datatype": f.get("DATATYPE", ""),
            "length":   f.get("LENGTH", ""),
        })
    return {
        "name":    tgt_el.get("NAME", ""),
        "db_type": tgt_el.get("DATABASETYPE", ""),
        "owner":   tgt_el.get("OWNERNAME", ""),
        "fields":  fields,
    }


def _extract_workflow(wf_el: etree._Element) -> dict:
    tasks = []
    for task in wf_el.iter("TASK"):
        tasks.append({
            "name": task.get("NAME", ""),
            "type": task.get("TYPE", ""),
        })
    return {
        "name":  wf_el.get("NAME", ""),
        "tasks": tasks,
    }
