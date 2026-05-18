"""
graph_engine.py — DataMapper AI Knowledge Graph

Builds and maintains an in-process schema knowledge graph using NetworkX.
No external graph DB required — persisted as a JSON snapshot alongside audits/.

Node types
----------
  column   : individual source or target column
  table    : source or target table
  domain   : business domain (Subscriber, Billing, Network, Order, …)
  concept  : semantic role (Identity, Revenue, Temporal, Asset, Flag, Code, Metric)
  mapping  : a past approved mapping event

Edge types
----------
  has_column      : table → column
  foreign_key     : column → column  (inferred from naming conventions / explicit)
  belongs_to      : table → domain   (classified from table name)
  has_role        : column → concept (classified from column name + type)
  maps_to         : src_column → tgt_column  (approved mapping; weighted by confidence)
  semantic_sim    : column → column  (added by retriever after embedding)
"""

from __future__ import annotations

import json
import os
import re
import time
from typing import Any, Dict, List, Optional, Tuple

try:
    import networkx as nx
    _NX_OK = True
except ImportError:
    _NX_OK = False

_GRAPH_PATH = os.path.join(os.path.dirname(__file__), "audits", "knowledge_graph.json")
_G: Optional[Any] = None  # singleton NetworkX DiGraph


# ─────────────────────────────────────────────────────────────────────────────
# Domain + concept classifiers
# ─────────────────────────────────────────────────────────────────────────────

_DOMAIN_KEYWORDS: Dict[str, List[str]] = {
    "Subscriber":  ["subscriber", "customer", "member", "user", "account", "sub"],
    "Billing":     ["billing", "invoice", "payment", "charge", "revenue", "bill", "fee"],
    "Network":     ["network", "circuit", "element", "node", "port", "link", "device", "asset"],
    "Order":       ["order", "provision", "request", "ticket", "case", "task"],
    "Location":    ["address", "location", "geo", "zip", "city", "state", "region"],
    "Product":     ["product", "service", "plan", "offer", "package", "bundle"],
    "Reference":   ["lookup", "ref", "code", "type", "status", "flag", "category"],
}

_CONCEPT_RULES: List[Tuple[str, List[str], List[str]]] = [
    # (concept, name_patterns, type_patterns)
    ("Identity",  ["id", "key", "uuid", "guid", "num", "number", "ref"],  ["INT64", "STRING"]),
    ("Temporal",  ["date", "time", "ts", "dt", "at", "created", "updated", "expires"], ["DATE", "TIMESTAMP", "DATETIME"]),
    ("Metric",    ["amount", "total", "count", "qty", "quantity", "balance", "cost", "price"], ["FLOAT64", "NUMERIC", "INT64"]),
    ("Flag",      ["is_", "has_", "flag", "active", "enabled", "deleted", "bool"], ["BOOLEAN", "INT64"]),
    ("Code",      ["code", "status", "type", "category", "tier", "class"],  ["STRING", "INT64"]),
    ("Descriptor",["name", "desc", "description", "label", "title", "note", "comment"], ["STRING"]),
    ("Revenue",   ["revenue", "charge", "fee", "rate", "price", "cost", "amount"],    ["FLOAT64", "NUMERIC"]),
]


def _classify_domain(table_name: str) -> str:
    tn = table_name.lower()
    for domain, keywords in _DOMAIN_KEYWORDS.items():
        if any(kw in tn for kw in keywords):
            return domain
    return "General"


def _classify_concept(col_name: str, col_type: str) -> str:
    cn = col_name.lower()
    ct = col_type.upper()
    for concept, name_pats, type_pats in _CONCEPT_RULES:
        name_hit = any(p in cn for p in name_pats)
        type_hit = any(ct.startswith(t) for t in type_pats)
        if name_hit or type_hit:
            return concept
    return "Attribute"


def _infer_fk_pairs(columns: List[Dict]) -> List[Tuple[str, str]]:
    """
    Heuristic FK inference: a column named <entity>_id or <entity>_num likely
    references the primary key of a table named <entity>.
    Returns list of (col_node_id, referenced_pattern) — resolved later.
    """
    pairs = []
    id_pat = re.compile(r"^(.+?)_(id|key|num|ref|code)$", re.IGNORECASE)
    for col in columns:
        m = id_pat.match(col.get("name", ""))
        if m:
            entity = m.group(1).lower()
            pairs.append((col["_node_id"], entity))
    return pairs


# ─────────────────────────────────────────────────────────────────────────────
# Graph initialisation
# ─────────────────────────────────────────────────────────────────────────────

def _get_graph() -> Any:
    global _G
    if _G is None:
        if not _NX_OK:
            raise RuntimeError("networkx not installed — run: pip install networkx")
        _G = nx.DiGraph()
        _load_graph()
    return _G


def _load_graph():
    """Reload persisted graph from JSON snapshot."""
    if not os.path.exists(_GRAPH_PATH):
        return
    try:
        with open(_GRAPH_PATH) as f:
            data = json.load(f)
        G = _G
        for node in data.get("nodes", []):
            nid = node.pop("id")
            G.add_node(nid, **node)
        for edge in data.get("edges", []):
            G.add_edge(edge["src"], edge["dst"], **{k: v for k, v in edge.items() if k not in ("src", "dst")})
    except Exception as e:
        print(f"[kg] Warning: could not load graph: {e}")


def save_graph():
    """Persist graph to JSON for cross-session reuse."""
    G = _get_graph()
    os.makedirs(os.path.dirname(_GRAPH_PATH), exist_ok=True)
    nodes = [{"id": n, **G.nodes[n]} for n in G.nodes()]
    edges = [{"src": u, "dst": v, **G[u][v]} for u, v in G.edges()]
    with open(_GRAPH_PATH, "w") as f:
        json.dump({"nodes": nodes, "edges": edges, "saved_at": time.time()}, f, indent=2)


def reset_graph():
    """Clear the in-memory graph (for testing)."""
    global _G
    if _NX_OK:
        _G = nx.DiGraph()


# ─────────────────────────────────────────────────────────────────────────────
# Schema ingestion — build graph nodes from parsed schema
# ─────────────────────────────────────────────────────────────────────────────

def _col_node_id(side: str, table: str, col: str) -> str:
    return f"{side}::{table}::{col}"


def _table_node_id(side: str, table: str) -> str:
    return f"{side}::TABLE::{table}"


def _domain_node_id(domain: str) -> str:
    return f"DOMAIN::{domain}"


def _concept_node_id(concept: str) -> str:
    return f"CONCEPT::{concept}"


def ingest_source_schema(session_id: str, tables: Dict[str, List[Dict]]):
    """
    Add source schema to the graph.

    tables: { table_name: [ { name, type, nullable, ... }, ... ] }
    """
    G = _get_graph()
    _ingest_schema(G, "src", session_id, tables)
    save_graph()


def ingest_target_schema(session_id: str, tables: Dict[str, List[Dict]]):
    """Add target schema to the graph."""
    G = _get_graph()
    _ingest_schema(G, "tgt", session_id, tables)
    save_graph()


def _ingest_schema(G, side: str, session_id: str, tables: Dict[str, List[Dict]]):
    for table_name, columns in tables.items():
        t_nid = _table_node_id(side, table_name)
        domain = _classify_domain(table_name)
        d_nid = _domain_node_id(domain)

        # Table node
        if not G.has_node(t_nid):
            G.add_node(t_nid, kind="table", side=side, name=table_name, domain=domain, sessions=[session_id])
        else:
            G.nodes[t_nid].setdefault("sessions", [])
            if session_id not in G.nodes[t_nid]["sessions"]:
                G.nodes[t_nid]["sessions"].append(session_id)

        # Domain node + edge
        if not G.has_node(d_nid):
            G.add_node(d_nid, kind="domain", name=domain)
        G.add_edge(t_nid, d_nid, rel="belongs_to")

        fk_candidates = []
        for col in columns:
            col_name = col.get("name") or col.get("column") or ""
            col_type = col.get("type") or col.get("data_type") or "STRING"
            col_nullable = col.get("nullable", True)

            c_nid = _col_node_id(side, table_name, col_name)
            concept = _classify_concept(col_name, col_type)
            concept_nid = _concept_node_id(concept)

            col["_node_id"] = c_nid  # annotate for FK inference

            # Column node
            if not G.has_node(c_nid):
                G.add_node(c_nid, kind="column", side=side, table=table_name,
                           name=col_name, col_type=col_type, nullable=col_nullable,
                           domain=domain, concept=concept, sessions=[session_id])
            else:
                G.nodes[c_nid].setdefault("sessions", [])
                if session_id not in G.nodes[c_nid]["sessions"]:
                    G.nodes[c_nid]["sessions"].append(session_id)

            # Concept node + edge
            if not G.has_node(concept_nid):
                G.add_node(concept_nid, kind="concept", name=concept)
            G.add_edge(c_nid, concept_nid, rel="has_role")

            # Table → Column edge
            G.add_edge(t_nid, c_nid, rel="has_column")
            fk_candidates.append(col)

        # FK inference (heuristic)
        for c_nid_fk, entity_hint in _infer_fk_pairs(fk_candidates):
            # find any column node whose table name matches the entity hint
            for node in G.nodes():
                nd = G.nodes[node]
                if nd.get("kind") == "column" and entity_hint in nd.get("table", "").lower():
                    if nd.get("concept") == "Identity" and c_nid_fk != node:
                        if not G.has_edge(c_nid_fk, node):
                            G.add_edge(c_nid_fk, node, rel="foreign_key", inferred=True)
                        break


# ─────────────────────────────────────────────────────────────────────────────
# Mapping persistence — approved mappings become graph edges
# ─────────────────────────────────────────────────────────────────────────────

def record_approved_mapping(
    session_id: str,
    src_table: str, src_col: str, src_type: str,
    tgt_table: str, tgt_col: str, tgt_type: str,
    mapping_type: str, business_logic: str,
    confidence: float, relation: str = "1:1",
):
    """
    Add a maps_to edge from src column to tgt column.
    Called from approve_mapping() in server.py.
    """
    G = _get_graph()
    src_nid = _col_node_id("src", src_table, src_col)
    tgt_nid = _col_node_id("tgt", tgt_table, tgt_col)

    # Ensure nodes exist even if schema wasn't ingested via graph path
    for nid, side, tbl, col, ctype in [
        (src_nid, "src", src_table, src_col, src_type),
        (tgt_nid, "tgt", tgt_table, tgt_col, tgt_type),
    ]:
        if not G.has_node(nid):
            G.add_node(nid, kind="column", side=side, table=tbl, name=col,
                       col_type=ctype, domain=_classify_domain(tbl),
                       concept=_classify_concept(col, ctype), sessions=[session_id])

    # Accumulate weight: repeated approvals across sessions strengthen the edge
    if G.has_edge(src_nid, tgt_nid) and G[src_nid][tgt_nid].get("rel") == "maps_to":
        G[src_nid][tgt_nid]["weight"] = G[src_nid][tgt_nid].get("weight", 1) + 1
        G[src_nid][tgt_nid]["sessions"].append(session_id)
        G[src_nid][tgt_nid]["last_confidence"] = confidence
    else:
        G.add_edge(src_nid, tgt_nid,
                   rel="maps_to",
                   mapping_type=mapping_type,
                   business_logic=business_logic,
                   relation=relation,
                   confidence=confidence,
                   weight=1,
                   sessions=[session_id],
                   last_confidence=confidence)

    save_graph()


# ─────────────────────────────────────────────────────────────────────────────
# Graph query helpers (used by graph_retriever)
# ─────────────────────────────────────────────────────────────────────────────

def get_graph_stats() -> Dict:
    G = _get_graph()
    kinds = {}
    for n in G.nodes():
        k = G.nodes[n].get("kind", "?")
        kinds[k] = kinds.get(k, 0) + 1
    return {
        "nodes": G.number_of_nodes(),
        "edges": G.number_of_edges(),
        "by_kind": kinds,
    }


def get_column_node(side: str, table: str, col: str) -> Optional[Dict]:
    G = _get_graph()
    nid = _col_node_id(side, table, col)
    return dict(G.nodes[nid]) if G.has_node(nid) else None


def get_domain_columns(domain: str, side: str = "tgt") -> List[Dict]:
    """Return all target columns in a given domain."""
    G = _get_graph()
    d_nid = _domain_node_id(domain)
    results = []
    for table_nid in G.predecessors(d_nid):
        tnd = G.nodes[table_nid]
        if tnd.get("kind") != "table" or tnd.get("side") != side:
            continue
        for col_nid in G.successors(table_nid):
            cnd = G.nodes[col_nid]
            if cnd.get("kind") == "column":
                results.append({"node_id": col_nid, **cnd})
    return results


def get_concept_columns(concept: str, side: str = "tgt") -> List[Dict]:
    """Return all target columns with a given semantic concept."""
    G = _get_graph()
    c_nid = _concept_node_id(concept)
    results = []
    for col_nid in G.predecessors(c_nid):
        cnd = G.nodes[col_nid]
        if cnd.get("kind") == "column" and cnd.get("side") == side:
            results.append({"node_id": col_nid, **cnd})
    return results


def get_past_mappings(src_col_node_id: str) -> List[Dict]:
    """Return all maps_to edges from this source column node."""
    G = _get_graph()
    results = []
    if not G.has_node(src_col_node_id):
        return results
    for _, tgt_nid, data in G.out_edges(src_col_node_id, data=True):
        if data.get("rel") == "maps_to":
            results.append({
                "tgt_node_id": tgt_nid,
                "tgt": G.nodes[tgt_nid] if G.has_node(tgt_nid) else {},
                **data,
            })
    return sorted(results, key=lambda x: x.get("weight", 1), reverse=True)


def get_fk_chain(col_node_id: str, depth: int = 2) -> List[str]:
    """Follow FK edges and return reachable column node IDs."""
    G = _get_graph()
    if not G.has_node(col_node_id):
        return []
    visited, frontier = set(), [col_node_id]
    for _ in range(depth):
        next_frontier = []
        for nid in frontier:
            for _, nbr, data in G.out_edges(nid, data=True):
                if data.get("rel") == "foreign_key" and nbr not in visited:
                    visited.add(nbr)
                    next_frontier.append(nbr)
        frontier = next_frontier
    return list(visited)


def get_all_target_columns() -> List[Dict]:
    """Return every target column node for vector index construction."""
    G = _get_graph()
    return [
        {"node_id": n, **G.nodes[n]}
        for n in G.nodes()
        if G.nodes[n].get("kind") == "column" and G.nodes[n].get("side") == "tgt"
    ]
