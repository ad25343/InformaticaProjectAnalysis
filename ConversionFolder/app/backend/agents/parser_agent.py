"""
STEP 1 — XML Parser Agent
Deterministic lxml-based parser. Extracts all Informatica PowerCenter objects
and builds a structured representation of the mapping graph.

v2.12: Mapplet inline expansion — <MAPPLET> definitions are expanded into each
mapping that references them, replacing the black-box instance with its full
set of transformations and connectors so downstream agents see resolved logic.
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
        "mapplets":    [],
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

    # ── Mapplet definitions (MUST precede mapping scan) ──────────────────────
    # Build a definition dict keyed by mapplet name so _extract_mapping can
    # look up and inline-expand each mapplet instance it encounters.
    # Informatica exports <MAPPLET> elements when the mapping is exported
    # "with dependencies" from Repository Manager.
    mapplet_defs: dict[str, dict] = {}
    mapplets_detected: list[str] = []
    for mlt in root.iter("MAPPLET"):
        mlt_name = mlt.get("NAME", "")
        if not mlt_name:
            continue
        counts["Mapplet"] = counts.get("Mapplet", 0) + 1
        defn = _extract_mapplet_def(mlt, flags)
        mapplet_defs[mlt_name] = defn
        graph["mapplets"].append({"name": mlt_name, "source": "definition"})
        if mlt_name not in mapplets_detected:
            mapplets_detected.append(mlt_name)

    # ── Mappings ─────────────────────────────
    for mapping in root.iter("MAPPING"):
        counts["Mapping"] = counts.get("Mapping", 0) + 1
        m = _extract_mapping(mapping, flags, reusable, unresolved_params, mapplet_defs)
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

    # ── Emit per-mapplet flags ────────────────────────────────────────────────
    # Collect all expanded mapplet names (across every mapping).
    mapplets_expanded: list[str] = []
    for m in graph["mappings"]:
        for exp_name in m.get("mapplet_expansions", []):
            if exp_name not in mapplets_expanded:
                mapplets_expanded.append(exp_name)

    # Collect mapplets referenced as instances but with NO definition exported.
    # _extract_mapping raises MAPPLET_DETECTED flags for these; collect their names.
    for f in flags:
        if f.flag_type == "MAPPLET_DETECTED" and f.element not in mapplets_detected:
            mapplets_detected.append(f.element)

    # Raise MAPPLET_EXPANDED flags for every successfully expanded mapplet.
    for mlt_name in mapplets_expanded:
        flags.append(ParseFlag(
            flag_type="MAPPLET_EXPANDED",
            element=mlt_name,
            detail=(
                f"Mapplet '{mlt_name}' was inline-expanded: its internal transformations "
                "and connectors have been added to the mapping graph and its external "
                "connectors rewired through the Input/Output interface nodes. "
                "Review the generated code to verify completeness of the expanded logic."
            )
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
        mapplets_detected=mapplets_detected,
        mapplets_expanded=mapplets_expanded,
    ), graph


# ─────────────────────────────────────────────
# Internal helpers
# ─────────────────────────────────────────────

def _extract_mapplet_def(mlt_el: etree._Element, flags: list) -> dict:
    """
    Extract a mapplet definition for inline expansion.
    Returns a dict containing all internal transformations, connectors,
    and the names of the Input / Output interface transformations.
    """
    transformations: list[dict] = []
    connectors: list[dict] = []
    input_trans_name: str = "Input"
    output_trans_name: str = "Output"

    for trans in mlt_el.iter("TRANSFORMATION"):
        t = _extract_transformation(trans, flags)
        transformations.append(t)
        ttype = trans.get("TYPE", "")
        if ttype == "Input Transformation":
            input_trans_name = trans.get("NAME", "Input")
        elif ttype == "Output Transformation":
            output_trans_name = trans.get("NAME", "Output")

    for conn in mlt_el.iter("CONNECTOR"):
        connectors.append({
            "from_instance": conn.get("FROMINSTANCE", ""),
            "from_field":    conn.get("FROMFIELD", ""),
            "to_instance":   conn.get("TOINSTANCE", ""),
            "to_field":      conn.get("TOFIELD", ""),
        })

    return {
        "name":               mlt_el.get("NAME", ""),
        "transformations":    transformations,
        "connectors":         connectors,
        "input_trans_name":   input_trans_name,
        "output_trans_name":  output_trans_name,
    }


def _inline_expand_mapplets(
    mapping_name: str,
    transformations: list,
    connectors: list,
    instance_map: dict[str, str],   # instance_name → transformation_name
    mapplet_defs: dict[str, dict],
    flags: list,
) -> tuple[list, list, list[str]]:
    """
    Replace each mapplet INSTANCE in the mapping with the inline transformations
    and connectors from its definition.

    Prefix convention: ``{instance_name}__{internal_node_name}``
    Using the instance name (not the definition name) ensures two instances of the
    same mapplet in one mapping get distinct node names.

    Returns:
        (expanded_transformations, expanded_connectors, expanded_mapplet_def_names)
    """
    # Find instances that have a definition available for expansion
    mapplet_inst_to_def: dict[str, str] = {}   # inst_name → def_name
    for inst_name, trans_name in instance_map.items():
        if trans_name in mapplet_defs:
            mapplet_inst_to_def[inst_name] = trans_name

    if not mapplet_inst_to_def:
        return transformations, connectors, []

    extra_transformations: list[dict] = []
    extra_connectors: list[dict] = []
    expanded_def_names: list[str] = []

    for inst_name, mlt_def_name in mapplet_inst_to_def.items():
        defn = mapplet_defs[mlt_def_name]
        prefix = inst_name   # unique per instance within this mapping

        # Inline all internal transformations with prefixed names
        for t in defn["transformations"]:
            prefixed: dict = dict(t)
            prefixed["name"] = f"{prefix}__{t['name']}"
            prefixed["_mapplet_source"] = mlt_def_name   # annotation for debugging
            extra_transformations.append(prefixed)

        # Inline all internal connectors with prefixed instance references
        for c in defn["connectors"]:
            extra_connectors.append({
                "from_instance": f"{prefix}__{c['from_instance']}",
                "from_field":    c["from_field"],
                "to_instance":   f"{prefix}__{c['to_instance']}",
                "to_field":      c["to_field"],
            })

        if mlt_def_name not in expanded_def_names:
            expanded_def_names.append(mlt_def_name)

    # Rewire external connectors that referenced a mapplet instance:
    #   TOINSTANCE=inst_name   → TOINSTANCE=inst_name__<InputTransName>
    #   FROMINSTANCE=inst_name → FROMINSTANCE=inst_name__<OutputTransName>
    rewired: list[dict] = []
    for c in connectors:
        new_c = dict(c)
        to_inst   = c["to_instance"]
        from_inst = c["from_instance"]

        if to_inst in mapplet_inst_to_def:
            mlt_def_name = mapplet_inst_to_def[to_inst]
            defn = mapplet_defs[mlt_def_name]
            new_c["to_instance"] = f"{to_inst}__{defn['input_trans_name']}"

        if from_inst in mapplet_inst_to_def:
            mlt_def_name = mapplet_inst_to_def[from_inst]
            defn = mapplet_defs[mlt_def_name]
            new_c["from_instance"] = f"{from_inst}__{defn['output_trans_name']}"

        rewired.append(new_c)

    return (
        transformations + extra_transformations,
        rewired + extra_connectors,
        expanded_def_names,
    )


def _extract_mapping(
    mapping_el: etree._Element,
    flags: list,
    reusable: list,
    unresolved_params: list,
    mapplet_defs: dict[str, dict] | None = None,
) -> dict:
    if mapplet_defs is None:
        mapplet_defs = {}

    name = mapping_el.get("NAME", "unknown")
    transformations: list[dict] = []
    connectors: list[dict] = []
    instance_map: dict[str, str] = {}  # instance name → transformation name

    # ── Transformation instances ──────────────
    for inst in mapping_el.iter("INSTANCE"):
        inst_name  = inst.get("NAME", "")
        trans_name = inst.get("TRANSFORMATION_NAME", inst_name)
        trans_type = inst.get("TYPE", "")
        instance_map[inst_name] = trans_name

        # Detect mapplet instances that have NO definition available — flag for re-export
        if trans_type == "Mapplet" and trans_name and trans_name not in mapplet_defs:
            already = any(
                f.flag_type == "MAPPLET_DETECTED" and f.element == trans_name
                for f in flags
            )
            if not already:
                flags.append(ParseFlag(
                    flag_type="MAPPLET_DETECTED",
                    element=trans_name,
                    detail=(
                        f"Mapplet '{trans_name}' is referenced in mapping '{name}' "
                        "but its definition block was not found in this export. "
                        "Re-export the mapping with 'Include Dependencies' enabled in "
                        "Informatica Repository Manager to allow full inline expansion. "
                        "Until then, verify any references to "
                        f"'{trans_name}' in the generated code manually."
                    )
                ))

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

    # ── Inline-expand mapplet instances ───────
    # Replace each mapplet INSTANCE with its constituent transformations and
    # connectors from the definition dict, and rewire external connectors.
    expanded_mlt_names: list[str] = []
    if mapplet_defs:
        transformations, connectors, expanded_mlt_names = _inline_expand_mapplets(
            name, transformations, connectors, instance_map, mapplet_defs, flags
        )

    # ── Mapping-level parameters/variables ───
    mapping_params: list[dict] = []
    for param in mapping_el.iter("MAPPINGVARIABLE"):
        pname   = param.get("NAME", "")
        dtype   = param.get("DATATYPE", "")
        default = param.get("DEFAULTVALUE", "")
        mapping_params.append({"name": pname, "datatype": dtype, "default": default})
        if not default:
            unresolved_params.append(pname)

    return {
        "name":               name,
        "description":        mapping_el.get("DESCRIPTION", ""),
        "transformations":    transformations,
        "connectors":         connectors,
        "parameters":         mapping_params,
        "instance_map":       instance_map,
        "mapplet_expansions": expanded_mlt_names,   # names of expanded mapplet defs
    }


def _extract_transformation(trans_el: etree._Element, flags: list) -> dict:
    name      = trans_el.get("NAME", "unknown")
    ttype     = trans_el.get("TYPE", "unknown")
    reusable  = trans_el.get("REUSABLE", "NO")
    ports: list[dict] = []
    expressions: list[dict] = []

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
        if ttype == "Sorter":
            sort_pos = field.get("SORTKEYPOSITION", "")
            sort_dir = field.get("SORTDIRECTION", "")
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
    table_attribs: dict[str, str] = {}
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
        "name":          name,
        "type":          ttype,
        "reusable":      reusable == "YES",
        "ports":         ports,
        "expressions":   expressions,
        "table_attribs": table_attribs,
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
        "name":    src_el.get("NAME", ""),
        "db_type": src_el.get("DATABASETYPE", ""),
        "owner":   src_el.get("OWNERNAME", ""),
        "fields":  fields,
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
