"""
app/routers/mappings.py — mapping CRUD + memory + reconcile + remap + append-source + table-mappings
"""
from __future__ import annotations

import asyncio
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Body, Depends, HTTPException
from pydantic import BaseModel

from app.core.llm_client import _add_usage, _make_llm
from app.core.mapping_memory import _absorb_single_correction, _recall_mapping_hints
from app.core.crew_learnings import record_learning, pending_extraction_count
from app.core.pipeline import MAPPING_SYSTEM, _build_mapping_system
from app.core.rbac import require_mapper
from app.core.session_store import _session_or_404
from app.intelligence.business_logic import _auto_business_logic
from app.intelligence.confidence import (
    _name_score,
    _recompute_relation_types,
    _type_score,
    compute_confidence,
    conf_tier,
)
from app.state import _mapping_memory, _L3_SEM, _save_mapping_memory, _save_sessions, _sessions

router = APIRouter()


@router.get("/api/sessions/{sid}/mappings")
async def get_mappings(sid: str):
    s = _session_or_404(sid)
    return {"mappings": s.get("mappings", []), "stats": s.get("stats", {})}


class BulkMappingsBody(BaseModel):
    mappings: list[dict]


@router.post("/api/sessions/{sid}/mappings")
async def save_bulk_mappings(sid: str, body: BulkMappingsBody):
    """Accept browser-direct mapping results and store them in the session."""
    s = _session_or_404(sid)
    rows = []
    for m in body.mappings:
        row = {
            "id":              str(uuid.uuid4()),
            "source_table":    m.get("source_table", ""),
            "source_column":   m.get("source_column", ""),
            "source_type":     m.get("source_type", "STRING"),
            "tgt_table":       m.get("target_table") or m.get("tgt_table", ""),
            "tgt_column":      m.get("target_column") or m.get("tgt_column", ""),
            "tgt_type":        m.get("target_type") or m.get("tgt_type", ""),
            "mapping_type":    m.get("mapping_type", "DIRECT"),
            "transformation":  m.get("transformation", ""),
            "confidence":      float(m.get("confidence", 0.8)),
            "business_logic":  m.get("business_logic", ""),
            "status":          "mapped" if m.get("target_column") else "unmapped",
            "tier":            "llm-browser",
            "modified":        False,
        }
        rows.append(row)
    s["mappings"] = rows
    s["status"]   = "done"
    s["stage"]    = "complete"

    mapped   = sum(1 for r in rows if r["status"] == "mapped")
    unmapped = len(rows) - mapped
    avg_conf = sum(r["confidence"] for r in rows) / len(rows) if rows else 0.0
    s["stats"] = {
        "total": len(rows), "mapped": mapped, "unmapped": unmapped,
        "avg_confidence": round(avg_conf, 3),
    }
    _save_sessions()
    return {"ok": True, "total": len(rows), "mapped": mapped, "stats": s["stats"]}


class MappingPatch(BaseModel):
    tgt_table:        Optional[str] = None
    tgt_column:       Optional[str] = None
    mapping_type:     Optional[str] = None
    mapping_relation: Optional[str] = None
    business_logic:   Optional[str] = None
    status:           Optional[str] = None
    pii_class:        Optional[str] = None


@router.patch("/api/sessions/{sid}/mappings/{row_id}")
async def patch_mapping(sid: str, row_id: str, patch: MappingPatch,
                          _user=Depends(require_mapper)):
    s = _session_or_404(sid)
    mappings = s.get("mappings", [])
    row = next((m for m in mappings if m["id"] == row_id), None)
    if not row:
        raise HTTPException(404, "Mapping row not found")

    for field, val in patch.model_dump(exclude_none=True).items():
        row[field] = val
    row["modified"] = True

    if row.get("tgt_column") and row.get("status") == "unmapped":
        row["status"] = "review"
    if row.get("status") == "unmapped" and not row.get("tgt_column"):
        row["confidence"] = 0.0
        row["tier"] = "none"

    if row.get("tgt_column") and row.get("src_field"):
        _absorb_single_correction(row)
        # Record as a crew learning event (original captured before patch was applied)
        try:
            record_learning(
                event_type="manual_edit",
                src_field=row.get("src_field", ""),
                original={"src_table": row.get("src_table", ""), "src_type": row.get("src_type", ""),
                           "tgt_table": patch.tgt_table or row.get("tgt_table", ""),
                           "tgt_column": patch.tgt_column or row.get("tgt_column", ""),
                           "mapping_type": row.get("mapping_type", ""), "confidence": row.get("confidence", 0),
                           "business_logic": row.get("business_logic", ""), "status": row.get("status", "")},
                corrected=row,
                session_id=sid,
            )
        except Exception:
            pass  # never break the patch endpoint due to learning failures

    n_mapped   = sum(1 for m in mappings if m["status"] == "mapped")
    n_review   = sum(1 for m in mappings if m["status"] == "review")
    n_unmapped = sum(1 for m in mappings if m["status"] == "unmapped")
    avg_conf   = sum(m["confidence"] for m in mappings) / max(len(mappings), 1)
    s["stats"]  = {"total": len(mappings), "mapped": n_mapped,
                   "review": n_review, "unmapped": n_unmapped,
                   "avg_confidence": round(avg_conf, 3)}
    _save_sessions()
    return {"ok": True, "row": row, "stats": s["stats"]}


@router.get("/api/sessions/{sid}/table-mappings")
async def get_table_mappings(sid: str):
    s = _session_or_404(sid)
    return {"table_mappings": s.get("table_mappings", [])}


@router.post("/api/sessions/{sid}/table-mappings")
async def save_table_mappings(sid: str, body: dict = Body(...)):
    s = _session_or_404(sid)
    mappings = body.get("table_mappings", [])
    if not isinstance(mappings, list):
        raise HTTPException(422, "table_mappings must be a list")
    cleaned = []
    for m in mappings:
        if not isinstance(m, dict) or not m.get("src_table") or not m.get("tgt_table"):
            continue
        cleaned.append({
            "id":        m.get("id", f"tm-{len(cleaned)}"),
            "src_table": str(m["src_table"]),
            "tgt_table": str(m["tgt_table"]),
            "relation":  m.get("relation", "1:1"),
        })
    s["table_mappings"] = cleaned
    _save_sessions()
    return {"ok": True, "saved": len(cleaned)}


@router.post("/api/sessions/{sid}/suggest-table-mappings")
async def suggest_table_mappings(sid: str, body: dict = Body(...)):
    s = _session_or_404(sid)
    src_tables    = body.get("src_tables", [])
    tgt_tables    = body.get("tgt_tables", [])
    custom_prompt = (body.get("custom_prompt") or "").strip()

    if not src_tables or not tgt_tables:
        raise HTTPException(422, "src_tables and tgt_tables are required")

    try:
        llm = _make_llm(s)
    except Exception as e:
        raise HTTPException(503, f"LLM not configured: {e}")

    src_desc = "\n".join(
        f"  {t['name']}: [{', '.join(str(c) for c in t.get('columns', [])[:10])}"
        f"{'...' if len(t.get('columns', [])) > 10 else ''}]"
        for t in src_tables
    )
    tgt_desc = "\n".join(
        f"  {t['name']}: [{', '.join(str(c) for c in t.get('columns', [])[:10])}"
        f"{'...' if len(t.get('columns', [])) > 10 else ''}]"
        for t in tgt_tables
    )

    custom_section = (
        f"\n\nADDITIONAL CONTEXT FROM THE USER (treat this as ground truth):\n{custom_prompt}"
        if custom_prompt else ""
    )
    system = (
        "You are a senior data engineering expert specialising in source-to-target mapping. "
        "Given source and target table names with their sample column lists, identify which source "
        "tables should map to which target tables. "
        "Base decisions on: name/prefix/suffix similarity, shared column names, domain semantics, "
        "and any user-supplied context below."
        f"{custom_section}\n\n"
        "Return ONLY a JSON array (no prose, no markdown fence) of objects: "
        "[{\"src_table\":\"...\",\"tgt_table\":\"...\",\"relation\":\"1:1\",\"reason\":\"one sentence\"}]. "
        "Relation values: 1:1, 1:M, M:1, M:M. "
        "Only include confident pairs — omit anything uncertain. "
        "If the user context explicitly states a mapping, always include it."
    )
    prompt = (
        f"SOURCE TABLES:\n{src_desc}\n\n"
        f"TARGET TABLES:\n{tgt_desc}\n\n"
        "Respond with the JSON array only."
    )

    try:
        result = await asyncio.to_thread(llm.complete_json, system, prompt)
        _add_usage(s, llm.last_usage, llm.provider, llm.model, "table_suggest")
        _save_sessions()
        if isinstance(result, dict):
            suggestions = result.get("mappings", result.get("suggestions", []))
            if not suggestions and result.get("src_table"):
                suggestions = [result]
        else:
            suggestions = result if isinstance(result, list) else []
        suggestions = [
            sg for sg in suggestions
            if isinstance(sg, dict) and sg.get("src_table") and sg.get("tgt_table")
        ]
    except Exception as e:
        from app.config import logger
        logger.warning("Table mapping suggestion LLM call failed: %s", e)
        suggestions = []

    return {"suggestions": suggestions}


@router.post("/api/sessions/{sid}/mappings/{row_id}/no-mapping")
async def set_no_mapping(sid: str, row_id: str):
    s = _session_or_404(sid)
    row = next((m for m in s.get("mappings", []) if m["id"] == row_id), None)
    if not row:
        raise HTTPException(404, "Row not found")
    if row.get("tgt_table") or row.get("tgt_column"):
        row["_prev_tgt_table"]      = row.get("tgt_table", "")
        row["_prev_tgt_column"]     = row.get("tgt_column", "")
        row["_prev_confidence"]     = row.get("confidence", 0.0)
        row["_prev_tier"]           = row.get("tier", "none")
        row["_prev_mapping_type"]   = row.get("mapping_type", "Direct")
        row["_prev_business_logic"] = row.get("business_logic", "")
        row["_prev_status"]         = row.get("status", "review")
    row.update({"tgt_table": "", "tgt_column": "", "status": "unmapped",
                "confidence": 0.0, "tier": "none", "modified": True})
    _save_sessions()
    return {"ok": True}


@router.post("/api/sessions/{sid}/mappings/{row_id}/approve")
async def approve_mapping(sid: str, row_id: str):
    s = _session_or_404(sid)
    row = next((m for m in s.get("mappings", []) if m["id"] == row_id), None)
    if not row:
        raise HTTPException(404, "Row not found")
    row["status"]   = "mapped"
    row["modified"] = True
    s.setdefault("mapping_memory", []).append({
        "src":      row.get("src_field", ""),
        "tgt":      row.get("tgt_column", ""),
        "type":     row.get("mapping_type", ""),
        "logic":    row.get("business_logic", ""),
        "relation": row.get("mapping_relation", "1:1"),
    })
    _save_sessions()
    return {"ok": True}


@router.post("/api/sessions/{sid}/mappings/{row_id}/restore")
async def restore_mapping(sid: str, row_id: str):
    s = _session_or_404(sid)
    row = next((m for m in s.get("mappings", []) if m["id"] == row_id), None)
    if not row:
        raise HTTPException(404, "Row not found")

    effective_tgt_col   = row.get("tgt_column") or row.get("_prev_tgt_column", "")
    effective_tgt_table = row.get("tgt_table")  or row.get("_prev_tgt_table",  "")

    if not effective_tgt_col:
        return {"ok": False, "msg": "No target assigned — assign tgt_table and tgt_column first"}

    row["tgt_table"]      = effective_tgt_table
    row["tgt_column"]     = effective_tgt_col
    row["mapping_type"]   = row.get("_prev_mapping_type",   row.get("mapping_type",   "Direct"))
    row["business_logic"] = row.get("_prev_business_logic", row.get("business_logic", ""))
    row["status"]         = "review"
    row["confidence"]     = row.get("_prev_confidence", row.get("llm_confidence") or 0.5)
    row["tier"]           = row.get("_prev_tier", conf_tier(row["confidence"]))
    row["modified"]       = True

    if not row.get("business_logic"):
        row["business_logic"] = _auto_business_logic(
            row.get("src_field", ""), row.get("src_type", "STRING"),
            row.get("tgt_type", "STRING"), row.get("mapping_type", "Direct"),
            row.get("mapping_relation", "1:1")
        )

    for k in ("_prev_tgt_table","_prev_tgt_column","_prev_confidence",
              "_prev_tier","_prev_mapping_type","_prev_business_logic","_prev_status"):
        row.pop(k, None)

    _save_sessions()
    return {"ok": True, "status": "review", "row": row}


@router.get("/api/mapping-memory")
async def get_mapping_memory():
    return {
        "total_entries": len(_mapping_memory),
        "entries": dict(sorted(
            _mapping_memory.items(),
            key=lambda kv: kv[1].get("uses", 0),
            reverse=True,
        )),
    }


@router.delete("/api/mapping-memory/{field_name}")
async def forget_mapping(field_name: str):
    if field_name not in _mapping_memory:
        raise HTTPException(404, f"'{field_name}' not in mapping memory")
    del _mapping_memory[field_name]
    _save_mapping_memory()
    return {"ok": True, "deleted": field_name, "remaining": len(_mapping_memory)}


@router.post("/api/sessions/{sid}/remap-unmapped")
async def remap_unmapped(sid: str):
    """Re-run LLM mapping on all unmapped rows."""
    s = _session_or_404(sid)
    mappings = s.get("mappings", [])
    unmapped = [m for m in mappings if m.get("status") == "unmapped"]
    if not unmapped:
        return {"ok": True, "remapped": 0, "msg": "No unmapped rows"}

    llm = _make_llm(s)
    if not llm:
        raise HTTPException(422, "LLM not configured — set API key in Settings")

    bq_tables: List[Dict] = s.get("bq_tables") or s.get("target_files_data") or []
    if not bq_tables:
        raise HTTPException(422, "Target schema not cached — re-run pipeline first")

    tgt_type_lookup: Dict[str, Dict[str, str]] = {
        tbl["table"]: {c["name"]: c["type"] for c in tbl["columns"]}
        for tbl in bq_tables
    }
    tgt_summary = "\n".join(
        f"Table: {t['table']}\n  Columns: " +
        ", ".join(f"{c['name']}({c['type']})" for c in t["columns"])
        for t in bq_tables
    )

    system = (
        "You are a data mapping specialist. The following columns could not be automatically mapped. "
        "Use semantic reasoning, abbreviation expansion, and domain knowledge to find the best match. "
        "Be creative but accurate — look for partial name matches, synonyms, and type-compatible columns. "
        "Return a JSON array of mapping objects.\n" + MAPPING_SYSTEM
    )

    remapped = 0
    by_table: Dict[str, List] = {}
    for m in unmapped:
        by_table.setdefault(m.get("src_table", ""), []).append(m)

    for tbl_name, rows in by_table.items():
        src_desc = "\n".join(
            f"  - {m['src_field']} ({m.get('src_type','STRING')})"
            + (f" — previously unmatched: {m.get('rationale','')}" if m.get('rationale') else "")
            for m in rows
        )
        prompt = (
            f"SOURCE TABLE: {tbl_name}\n"
            f"UNMAPPED COLUMNS (re-attempt with looser matching):\n{src_desc}\n\n"
            f"TARGET SCHEMA:\n{tgt_summary}\n\n"
            "Try harder — expand abbreviations, consider domain synonyms, allow type casts. "
            "Return JSON array."
        )
        try:
            raw = await asyncio.to_thread(llm.complete_json, system, prompt)
            _add_usage(s, llm.last_usage, llm.provider, llm.model, "L3-rerun")
            if isinstance(raw, dict):
                raw = raw.get("mappings", [raw])
        except Exception:
            continue

        for item in raw:
            row = next((m for m in rows if m["src_field"] == item.get("src_field")), None)
            if not row:
                continue
            tgt_tbl  = item.get("tgt_table", "")
            tgt_col  = item.get("tgt_column", "")
            if not tgt_col or item.get("mapping_type", "").lower() in ("unused", "unmapped"):
                continue
            tgt_type = tgt_type_lookup.get(tgt_tbl, {}).get(tgt_col, "STRING")
            name_sim = _name_score(item.get("src_field", ""), tgt_col)
            type_sim = _type_score(row.get("src_type", "STRING"), tgt_type)
            llm_conf = float(item.get("llm_confidence", 0.6))
            conf     = compute_confidence(name_sim, type_sim, llm_conf)
            row.update({
                "tgt_table":       tgt_tbl,
                "tgt_column":      tgt_col,
                "tgt_type":        tgt_type,
                "mapping_type":    item.get("mapping_type", "Direct"),
                "mapping_relation":item.get("mapping_relation", "1:1"),
                "business_logic":  item.get("business_logic") or _auto_business_logic(
                    row["src_field"], row.get("src_type","STRING"), tgt_type,
                    item.get("mapping_type","Direct"), "1:1"
                ),
                "confidence":      conf,
                "tier":            conf_tier(conf),
                "status":          "review" if conf < 0.8 else "mapped",
                "rationale":       item.get("rationale",""),
                "modified":        True,
            })
            remapped += 1

    _recompute_relation_types(mappings)

    total   = len(mappings)
    mapped  = sum(1 for m in mappings if m["status"] == "mapped")
    rev     = sum(1 for m in mappings if m["status"] == "review")
    unmap   = sum(1 for m in mappings if m["status"] == "unmapped")
    avg_c   = sum(m["confidence"] for m in mappings) / total if total else 0
    s["stats"] = {"total": total, "mapped": mapped, "review": rev,
                  "unmapped": unmap, "avg_confidence": round(avg_c, 3)}
    _save_sessions()

    return {"ok": True, "remapped": remapped, "stats": s["stats"], "mappings": mappings}


class AppendSourceBody(BaseModel):
    tables: List[Dict[str, Any]]


@router.post("/api/sessions/{sid}/append-source")
async def append_source(sid: str, body: AppendSourceBody):
    """Merge additional source tables and run L3 on them."""
    s = _session_or_404(sid)

    new_tables = body.tables
    if not new_tables:
        return {"ok": False, "msg": "No tables provided"}

    llm = _make_llm(s)
    if not llm:
        raise HTTPException(422, "LLM not configured — set API key in Settings")

    bq_tables: List[Dict] = s.get("bq_tables") or s.get("target_files_data") or []
    if not bq_tables:
        raise HTTPException(422, "Target schema not cached — run the pipeline once first")

    existing_src_tables = {m["src_table"] for m in s.get("mappings", [])}
    tables_to_map = [t for t in new_tables if t["name"] not in existing_src_tables]
    if not tables_to_map:
        return {"ok": False, "msg": "All provided tables are already mapped in this session"}

    tgt_type_lookup: Dict[str, Dict[str, str]] = {
        tbl["table"]: {c["name"]: c["type"] for c in tbl["columns"]}
        for tbl in bq_tables
    }
    tgt_summary = "\n".join(
        f"Table: {t['table']}\n  Columns: " +
        ", ".join(f"{c['name']}({c['type']})" for c in t["columns"])
        for t in bq_tables
    )

    mapping_system = _build_mapping_system(s)
    new_mappings: List[Dict] = []

    for src_table in tables_to_map:
        cols     = src_table["columns"]
        tbl_name = src_table["name"]
        BATCH = 15
        for i in range(0, len(cols), BATCH):
            batch = cols[i: i + BATCH]
            src_desc = "\n".join(
                f"  - {c['name']} ({c['type']})" + (f" sample={c['sample']}" if c.get("sample") else "")
                for c in batch
            )
            prompt = (
                f"SOURCE TABLE: {tbl_name}\n"
                f"SOURCE COLUMNS TO MAP:\n{src_desc}\n\n"
                f"TARGET SCHEMA:\n{tgt_summary}\n\n"
                "Map each source column to its best target. Return JSON array."
            )
            try:
                sem = _L3_SEM or asyncio.Semaphore(2)
                async with sem:
                    raw_result = await asyncio.to_thread(llm.complete_json, mapping_system, prompt)
                _add_usage(s, llm.last_usage, llm.provider, llm.model, "L3-append")
                _save_sessions()
                if isinstance(raw_result, dict):
                    raw_result = raw_result.get("mappings", [raw_result])
            except Exception as e:
                from app.config import logger
                logger.warning("append-source LLM batch failed: %s", e)
                raw_result = []

            for item in raw_result:
                src_col = next((c for c in batch if c["name"] == item.get("src_field")), None)
                tgt_table_name = item.get("tgt_table", "")
                tgt_col_name   = item.get("tgt_column", "")
                tgt_col_type   = tgt_type_lookup.get(tgt_table_name, {}).get(tgt_col_name, "STRING")
                name_sim  = _name_score(item.get("src_field", ""), tgt_col_name) if tgt_col_name else 0.0
                type_sim  = _type_score(src_col["type"] if src_col else "STRING", tgt_col_type) if tgt_col_name else 0.0
                llm_conf  = float(item.get("llm_confidence", 0.5))
                is_unused = item.get("mapping_type", "").lower() == "unused" or not tgt_col_name
                confidence = 0.0 if is_unused else compute_confidence(name_sim, type_sim, llm_conf)
                mapping_type     = item.get("mapping_type", "Direct") or "Direct"
                mapping_relation = item.get("mapping_relation", "1:1") or "1:1"
                src_type_val     = src_col["type"] if src_col else "STRING"
                raw_logic        = (item.get("business_logic") or "").strip()
                if not raw_logic and not is_unused:
                    raw_logic = _auto_business_logic(
                        item.get("src_field", ""), src_type_val, tgt_col_type,
                        mapping_type, mapping_relation
                    )
                new_mappings.append({
                    "id":               str(uuid.uuid4()),
                    "src_table":        tbl_name,
                    "src_field":        item.get("src_field", ""),
                    "src_type":         src_type_val,
                    "tgt_table":        tgt_table_name if not is_unused else "",
                    "tgt_column":       tgt_col_name   if not is_unused else "",
                    "tgt_type":         tgt_col_type   if not is_unused else "",
                    "mapping_type":     mapping_type,
                    "mapping_relation": mapping_relation,
                    "business_logic":   raw_logic,
                    "confidence":       confidence,
                    "tier":             conf_tier(confidence),
                    "status":           "unmapped" if is_unused else ("review" if confidence < 0.8 else "mapped"),
                    "rationale":        item.get("rationale", ""),
                    "llm_confidence":   llm_conf,
                    "name_sim":         round(name_sim, 3),
                    "type_sim":         round(type_sim, 3),
                    "kg_mode":          False,
                    "modified":         False,
                })

    for m in new_mappings:
        if not m.get("tgt_column") and m["status"] != "unmapped":
            m["status"] = "unmapped"; m["tgt_table"] = ""; m["tgt_column"] = ""
            m["confidence"] = 0.0; m["tier"] = "none"

    all_mappings = s.get("mappings", []) + new_mappings

    existing_schema = s.get("schema_data", {})
    existing_tables = {t["name"] for t in existing_schema.get("tables", [])}
    for t in tables_to_map:
        if t["name"] not in existing_tables:
            existing_schema.setdefault("tables", []).append(t)
    s["schema_data"] = existing_schema
    s["mappings"] = all_mappings

    total   = len(all_mappings)
    mapped  = sum(1 for m in all_mappings if m["status"] == "mapped")
    rev     = sum(1 for m in all_mappings if m["status"] == "review")
    unmap   = sum(1 for m in all_mappings if m["status"] == "unmapped")
    avg_c   = sum(m["confidence"] for m in all_mappings) / total if total else 0
    s["stats"] = {"total": total, "mapped": mapped, "review": rev,
                  "unmapped": unmap, "avg_confidence": round(avg_c, 3)}
    _save_sessions()

    return {
        "ok": True,
        "new_tables": [t["name"] for t in tables_to_map],
        "new_rows": len(new_mappings),
        "stats": s["stats"],
        "mappings": all_mappings,
    }
