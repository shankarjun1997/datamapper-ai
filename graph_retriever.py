"""
graph_retriever.py — Hybrid Retrieval for DataMapper AI

Combines three retrieval signals and re-ranks with LLM:

  1. Graph traversal  — domain matching, concept matching, FK chains, past mappings
  2. Vector search    — sentence-transformer embeddings over target column descriptors
  3. Rule-based boost — type compatibility, name fuzzy match (existing confidence floor)
  4. LLM rerank       — LLM judges top-k candidates and picks final mapping

The retriever is called from _run_pipeline() in server.py as an upgrade to the
current batch-LLM approach. Falls back gracefully if sentence-transformers or
numpy are not installed (pure graph + rule mode).
"""

from __future__ import annotations

import json
import math
import os
from typing import Any, Dict, List, Optional, Tuple

from graph_engine import (
    _col_node_id, _classify_domain, _classify_concept,
    get_domain_columns, get_concept_columns, get_past_mappings,
    get_fk_chain, get_all_target_columns, ingest_source_schema, ingest_target_schema,
    get_graph_stats,
)

# Optional imports — degrade gracefully
try:
    import numpy as np
    _NP_OK = True
except ImportError:
    _NP_OK = False

try:
    from sentence_transformers import SentenceTransformer
    _ST_OK = True
except ImportError:
    _ST_OK = False

try:
    from rapidfuzz import fuzz as _fuzz
    _RF_OK = True
except ImportError:
    _RF_OK = False


# ─────────────────────────────────────────────────────────────────────────────
# Embedding index — built once per session, cached in memory
# ─────────────────────────────────────────────────────────────────────────────

_MODEL_NAME = os.getenv("KG_EMBED_MODEL", "all-MiniLM-L6-v2")  # 22MB, fast
_model: Optional[Any] = None
_index_embeddings: Optional[Any] = None  # numpy array [N, D]
_index_nodes: List[Dict] = []


def _get_model():
    global _model
    if _model is None and _ST_OK:
        _model = SentenceTransformer(_MODEL_NAME)
    return _model


def _col_descriptor(node: Dict) -> str:
    """Human-readable descriptor for a column — used for embedding."""
    return (
        f"{node.get('table', '')} {node.get('name', '')} "
        f"type:{node.get('col_type', '')} "
        f"domain:{node.get('domain', '')} "
        f"concept:{node.get('concept', '')}"
    ).strip()


def build_vector_index(session_id: Optional[str] = None):
    """
    Build or rebuild the in-memory vector index from all target column nodes.
    Called after target schema is ingested (L2 stage).
    """
    global _index_embeddings, _index_nodes
    tgt_cols = get_all_target_columns()
    if not tgt_cols:
        _index_nodes = []
        _index_embeddings = None
        return

    _index_nodes = tgt_cols
    model = _get_model()
    if model is None or not _NP_OK:
        _index_embeddings = None
        return

    descs = [_col_descriptor(c) for c in tgt_cols]
    _index_embeddings = model.encode(descs, normalize_embeddings=True, show_progress_bar=False)


def _vector_search(query: str, top_k: int = 20) -> List[Tuple[Dict, float]]:
    """Return top-k target columns by cosine similarity."""
    model = _get_model()
    if model is None or _index_embeddings is None or not _NP_OK:
        return []
    import numpy as np
    q_emb = model.encode([query], normalize_embeddings=True)[0]
    sims = _index_embeddings @ q_emb  # cosine sim since both are L2-normalised
    top_idx = np.argsort(sims)[::-1][:top_k]
    return [(_index_nodes[i], float(sims[i])) for i in top_idx]


# ─────────────────────────────────────────────────────────────────────────────
# Name + type scoring (mirrors server.py, kept local to avoid circular import)
# ─────────────────────────────────────────────────────────────────────────────

_TYPE_COMPAT = {
    ("STRING", "STRING"): 1.0, ("INT64", "INT64"): 1.0,
    ("FLOAT64", "FLOAT64"): 1.0, ("BOOLEAN", "BOOLEAN"): 1.0,
    ("DATE", "DATE"): 1.0, ("TIMESTAMP", "TIMESTAMP"): 1.0,
    ("INT64", "FLOAT64"): 0.8, ("FLOAT64", "INT64"): 0.7,
    ("INT64", "STRING"): 0.5, ("STRING", "INT64"): 0.4,
    ("DATE", "TIMESTAMP"): 0.9, ("TIMESTAMP", "DATE"): 0.8,
    ("STRING", "DATE"): 0.5, ("STRING", "TIMESTAMP"): 0.5,
    ("NUMERIC", "FLOAT64"): 0.9, ("NUMERIC", "INT64"): 0.8,
}

def _norm_type(t: str) -> str:
    t = (t or "").upper().split("(")[0].strip()
    MAP = {"VARCHAR": "STRING", "TEXT": "STRING", "CHAR": "STRING", "NVARCHAR": "STRING",
           "INTEGER": "INT64", "BIGINT": "INT64", "INT": "INT64", "SMALLINT": "INT64",
           "DOUBLE": "FLOAT64", "REAL": "FLOAT64", "DECIMAL": "NUMERIC", "NUMBER": "NUMERIC",
           "BOOL": "BOOLEAN", "DATETIME": "TIMESTAMP"}
    return MAP.get(t, t)

def _type_score(src: str, tgt: str) -> float:
    return _TYPE_COMPAT.get((_norm_type(src), _norm_type(tgt)), 0.3)

def _name_score(src: str, tgt: str) -> float:
    if not _RF_OK:
        s1, s2 = src.lower(), tgt.lower()
        return sum(c in s2 for c in s1) / max(len(s1), len(s2), 1)
    s1 = src.lower().replace("_", "").replace("-", "")
    s2 = tgt.lower().replace("_", "").replace("-", "")
    return _fuzz.ratio(s1, s2) / 100.0


# ─────────────────────────────────────────────────────────────────────────────
# Candidate scoring
# ─────────────────────────────────────────────────────────────────────────────

def _score_candidate(
    src_name: str, src_type: str, src_domain: str, src_concept: str,
    tgt_node: Dict,
    vector_sim: float = 0.0,
    past_weight: int = 0,
    graph_boost: float = 0.0,
) -> float:
    """
    Composite score blending all signals.
    Weights tuned for hybrid mode — when vectors unavailable, falls back to rule+graph.
    """
    name_s   = _name_score(src_name, tgt_node.get("name", ""))
    type_s   = _type_score(src_type, tgt_node.get("col_type", ""))
    domain_s = 1.0 if tgt_node.get("domain") == src_domain else 0.0
    concept_s = 1.0 if tgt_node.get("concept") == src_concept else 0.5
    past_s   = min(past_weight / 5.0, 1.0)  # cap at 5 approvals = max boost

    if _ST_OK and _index_embeddings is not None:
        # Full hybrid: vector is the dominant signal
        score = (
            vector_sim * 0.40 +
            name_s     * 0.15 +
            type_s     * 0.15 +
            domain_s   * 0.10 +
            concept_s  * 0.10 +
            past_s     * 0.05 +
            graph_boost* 0.05
        )
    else:
        # Degraded mode: rule-based + graph
        score = (
            name_s     * 0.35 +
            type_s     * 0.25 +
            domain_s   * 0.15 +
            concept_s  * 0.10 +
            past_s     * 0.10 +
            graph_boost* 0.05
        )
    return round(score, 4)


# ─────────────────────────────────────────────────────────────────────────────
# Main retrieval function
# ─────────────────────────────────────────────────────────────────────────────

def retrieve_candidates(
    src_table: str,
    src_col: str,
    src_type: str,
    top_k: int = 8,
) -> List[Dict]:
    """
    Run hybrid retrieval for a single source column.
    Returns top_k ranked candidate target columns with scores and provenance.
    """
    src_domain  = _classify_domain(src_table)
    src_concept = _classify_concept(src_col, src_type)
    src_nid     = _col_node_id("src", src_table, src_col)

    # ── 1. Graph signals ─────────────────────────────────────────────────────

    # Past approved mappings for this exact source column
    past_maps = {
        pm["tgt_node_id"]: pm.get("weight", 1)
        for pm in get_past_mappings(src_nid)
    }

    # FK-reachable target columns (structural proximity)
    fk_chain = set(get_fk_chain(src_nid, depth=2))

    # Domain + concept candidates from graph
    domain_cols  = {c["node_id"]: c for c in get_domain_columns(src_domain,  side="tgt")}
    concept_cols = {c["node_id"]: c for c in get_concept_columns(src_concept, side="tgt")}

    # ── 2. Vector candidates ─────────────────────────────────────────────────
    query = _col_descriptor({
        "table": src_table, "name": src_col, "col_type": src_type,
        "domain": src_domain, "concept": src_concept
    })
    vec_results = {node["node_id"]: sim for node, sim in _vector_search(query, top_k=30)}

    # ── 3. Merge candidate pool ───────────────────────────────────────────────
    all_candidates: Dict[str, Dict] = {}

    # Seed with vector results
    for nid, sim in vec_results.items():
        node = next((c for c in _index_nodes if c["node_id"] == nid), None)
        if node:
            all_candidates[nid] = {**node, "_vec_sim": sim}

    # Add graph candidates not already in pool
    for nid, node in {**domain_cols, **concept_cols}.items():
        if nid not in all_candidates:
            all_candidates[nid] = {**node, "_vec_sim": 0.0}

    # ── 4. Score + rank ───────────────────────────────────────────────────────
    scored = []
    for nid, node in all_candidates.items():
        graph_boost = 0.0
        if nid in fk_chain:
            graph_boost += 0.3
        if nid in domain_cols:
            graph_boost += 0.2
        if nid in concept_cols:
            graph_boost += 0.1

        score = _score_candidate(
            src_name=src_col, src_type=src_type,
            src_domain=src_domain, src_concept=src_concept,
            tgt_node=node,
            vector_sim=node.get("_vec_sim", 0.0),
            past_weight=past_maps.get(nid, 0),
            graph_boost=min(graph_boost, 1.0),
        )

        provenance = []
        if node.get("_vec_sim", 0) > 0.4: provenance.append("vector")
        if nid in past_maps:               provenance.append(f"past_mapping(x{past_maps[nid]})")
        if nid in fk_chain:               provenance.append("fk_chain")
        if nid in domain_cols:            provenance.append(f"domain:{src_domain}")
        if nid in concept_cols:           provenance.append(f"concept:{src_concept}")

        scored.append({
            "node_id":      nid,
            "tgt_table":    node.get("table", ""),
            "tgt_col":      node.get("name", ""),
            "tgt_type":     node.get("col_type", ""),
            "tgt_domain":   node.get("domain", ""),
            "tgt_concept":  node.get("concept", ""),
            "retrieval_score": score,
            "provenance":   provenance,
            "past_weight":  past_maps.get(nid, 0),
        })

    scored.sort(key=lambda x: x["retrieval_score"], reverse=True)
    return scored[:top_k]


# ─────────────────────────────────────────────────────────────────────────────
# LLM reranker — called after retrieve_candidates
# ─────────────────────────────────────────────────────────────────────────────

_RERANK_SYSTEM = """You are a data migration expert producing Source-to-Target Mappings.
You will receive a source column description and a ranked list of candidate target columns.
Choose the BEST match (or NONE if no candidate is suitable).

Respond ONLY with valid JSON:
{
  "tgt_table": "<table or null>",
  "tgt_col": "<column or null>",
  "mapping_type": "<Direct|Derived|Lookup|Constant|Expression|Unused>",
  "mapping_relation": "<1:1|1:M|M:1>",
  "business_logic": "<BigQuery SQL expression or passthrough field name>",
  "llm_score": <0.0-1.0>,
  "reason": "<one sentence>"
}"""


def llm_rerank(
    llm_client: Any,
    src_table: str, src_col: str, src_type: str,
    candidates: List[Dict],
    session_context: str = "",
) -> Dict:
    """
    Ask the LLM to pick the best candidate from the pre-filtered list.
    This is far cheaper than passing all 400+ target columns.
    """
    if not candidates:
        return _no_mapping()

    cands_txt = "\n".join(
        f"  [{i+1}] {c['tgt_table']}.{c['tgt_col']} ({c['tgt_type']}) "
        f"domain:{c['tgt_domain']} concept:{c['tgt_concept']} "
        f"retrieval_score:{c['retrieval_score']:.3f} provenance:{','.join(c['provenance'])}"
        for i, c in enumerate(candidates)
    )

    prompt = f"""{session_context}

SOURCE COLUMN:
  table: {src_table}
  column: {src_col}
  type: {src_type}

CANDIDATE TARGET COLUMNS (pre-filtered by graph + vector retrieval):
{cands_txt}

Select the best target column. If none fit, return null for tgt_table and tgt_col."""

    try:
        raw = llm_client.complete_json(_RERANK_SYSTEM, prompt)
        result = raw if isinstance(raw, dict) else json.loads(raw)
        result.setdefault("mapping_type", "Direct")
        result.setdefault("mapping_relation", "1:1")
        result.setdefault("business_logic", src_col)
        result.setdefault("llm_score", 0.5)
        return result
    except Exception as e:
        return _no_mapping(reason=str(e))


def _no_mapping(reason: str = "") -> Dict:
    return {
        "tgt_table": None, "tgt_col": None,
        "mapping_type": "Unused", "mapping_relation": "1:1",
        "business_logic": "", "llm_score": 0.0,
        "reason": reason or "no suitable candidate found",
    }


# ─────────────────────────────────────────────────────────────────────────────
# Batch mapping — drop-in replacement for the L3 LLM batch in server.py
# ─────────────────────────────────────────────────────────────────────────────

def map_table_hybrid(
    llm_client: Any,
    src_table: str,
    src_columns: List[Dict],
    session_context: str = "",
    top_k: int = 8,
) -> List[Dict]:
    """
    Map all columns in a source table using hybrid retrieval + LLM rerank.
    Returns a list of mapping dicts in the same format as the existing L3 output.
    """
    results = []
    for col in src_columns:
        col_name = col.get("name") or col.get("column") or ""
        col_type = col.get("type") or col.get("data_type") or "STRING"

        candidates = retrieve_candidates(src_table, col_name, col_type, top_k=top_k)
        decision   = llm_rerank(llm_client, src_table, col_name, col_type,
                                candidates, session_context)

        # Composite confidence
        name_s = _name_score(col_name, decision.get("tgt_col") or "")
        type_s = _type_score(col_type, decision.get("tgt_type") or "")
        llm_s  = decision.get("llm_score", 0.0)
        conf   = round(name_s * 0.30 + type_s * 0.20 + llm_s * 0.50, 3)

        results.append({
            "src_table":       src_table,
            "src_column":      col_name,
            "src_type":        col_type,
            "tgt_table":       decision.get("tgt_table") or "",
            "tgt_column":      decision.get("tgt_col") or "",
            "tgt_type":        "",  # populated by caller from schema
            "mapping_type":    decision.get("mapping_type", "Direct"),
            "mapping_relation":decision.get("mapping_relation", "1:1"),
            "business_logic":  decision.get("business_logic", col_name),
            "confidence":      conf,
            "llm_score":       llm_s,
            "name_score":      round(name_s, 3),
            "type_score":      round(type_s, 3),
            "retrieval_candidates": [
                {"tgt_table": c["tgt_table"], "tgt_col": c["tgt_col"],
                 "score": c["retrieval_score"], "provenance": c["provenance"]}
                for c in candidates[:3]
            ],
            "kg_mode": True,
        })

    return results
