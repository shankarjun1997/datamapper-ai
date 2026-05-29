"""
app/intelligence/lineage.py — Lineage & Impact Analysis (capabilities #4 & #5).

Derives a column-level source→target lineage graph from a session's mappings,
and answers impact questions ("if I change/drop this source column, what target
objects are affected?" and the reverse). Pure, dependency-free, fully testable.
"""
from __future__ import annotations

from typing import Dict, List, Optional


def _sid(table: str, col: str) -> str:
    return f"src:{(table or '').strip()}.{(col or '').strip()}"


def _tid(table: str, col: str) -> str:
    return f"tgt:{(table or '').strip()}.{(col or '').strip()}"


def _active(m: Dict) -> bool:
    status = (m.get("status") or "").lower()
    if status in ("no_mapping", "skipped", "ignored", "rejected"):
        return False
    # must have at least a source and a target column
    return bool((m.get("tgt_column") or "").strip())


def build_lineage(mappings: List[Dict]) -> Dict:
    """Build a graph: column nodes + mapping edges, plus table-level rollups."""
    nodes: Dict[str, Dict] = {}
    edges: List[Dict] = []
    src_tables: Dict[str, set] = {}
    tgt_tables: Dict[str, set] = {}

    def add_node(nid, side, table, col):
        if nid not in nodes:
            nodes[nid] = {"id": nid, "side": side, "table": table, "column": col}

    for m in mappings or []:
        if not _active(m):
            continue
        st, sf = m.get("src_table", ""), m.get("src_field", "")
        tt, tc = m.get("tgt_table", ""), m.get("tgt_column", "")
        s_id, t_id = _sid(st, sf), _tid(tt, tc)
        add_node(s_id, "source", st, sf)
        add_node(t_id, "target", tt, tc)
        edges.append({
            "from": s_id, "to": t_id,
            "mapping_type": m.get("mapping_type", ""),
            "confidence": m.get("confidence"),
            "status": m.get("status", ""),
            "transform": m.get("business_logic", "") or "",
        })
        src_tables.setdefault(st, set()).add(tt)
        tgt_tables.setdefault(tt, set()).add(st)

    table_edges = []
    seen = set()
    for st, tts in src_tables.items():
        for tt in tts:
            key = (st, tt)
            if key not in seen:
                seen.add(key)
                table_edges.append({"from_table": st, "to_table": tt})

    return {
        "nodes": list(nodes.values()),
        "edges": edges,
        "table_lineage": table_edges,
        "stats": {
            "source_columns": sum(1 for n in nodes.values() if n["side"] == "source"),
            "target_columns": sum(1 for n in nodes.values() if n["side"] == "target"),
            "mappings": len(edges),
            "source_tables": len(src_tables),
            "target_tables": len(tgt_tables),
        },
    }


def _matches(table: str, col: str, ref_table: str, ref_col: Optional[str]) -> bool:
    if (table or "").lower() != (ref_table or "").lower():
        return False
    if ref_col is None:
        return True
    return (col or "").lower() == (ref_col or "").lower()


def impact(mappings: List[Dict], ref: str, direction: str = "forward") -> Dict:
    """Impact analysis for a 'table' or 'table.column' reference.

    direction='forward': given a SOURCE object, list TARGET objects that depend
    on it (what breaks if the source changes/drops).
    direction='reverse': given a TARGET object, list the SOURCE objects feeding
    it (what to check to populate the target).
    """
    ref = (ref or "").strip()
    if "." in ref:
        ref_table, ref_col = ref.split(".", 1)
    else:
        ref_table, ref_col = ref, None

    affected: List[Dict] = []
    tables_hit = set()
    for m in mappings or []:
        if not _active(m):
            continue
        if direction == "reverse":
            if _matches(m.get("tgt_table", ""), m.get("tgt_column", ""), ref_table, ref_col):
                affected.append({
                    "table": m.get("src_table", ""), "column": m.get("src_field", ""),
                    "via_target": f"{m.get('tgt_table','')}.{m.get('tgt_column','')}",
                    "mapping_type": m.get("mapping_type", ""), "confidence": m.get("confidence"),
                })
                tables_hit.add(m.get("src_table", ""))
        else:  # forward
            if _matches(m.get("src_table", ""), m.get("src_field", ""), ref_table, ref_col):
                affected.append({
                    "table": m.get("tgt_table", ""), "column": m.get("tgt_column", ""),
                    "from_source": f"{m.get('src_table','')}.{m.get('src_field','')}",
                    "mapping_type": m.get("mapping_type", ""), "confidence": m.get("confidence"),
                    "transform": m.get("business_logic", "") or "",
                })
                tables_hit.add(m.get("tgt_table", ""))

    return {
        "ref": ref,
        "direction": direction,
        "affected_columns": affected,
        "affected_count": len(affected),
        "affected_tables": sorted(t for t in tables_hit if t),
    }
