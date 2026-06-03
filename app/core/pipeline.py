"""
app/core/pipeline.py — _emit + _run_pipeline + _run_sql_generation + _build_mapping_system
"""
from __future__ import annotations

import asyncio
import os
import re
import time
import uuid
from typing import Any, Dict, List

from app.config import _BQ_DATASET, _BQ_PROJECT, _GCP_CREDS, logger
from app.core.audit import _now, _write_audit_event
from app.core.llm_client import _add_usage, _make_llm
from app.core.mapping_memory import _recall_mapping_hints
from app.core.webhooks import fire_webhook
from app.intelligence.business_logic import _auto_business_logic
from app.intelligence.confidence import (
    _name_score,
    _recompute_relation_types,
    _type_score,
    compute_confidence,
    conf_tier,
)
from app.intelligence.sql_format import _format_sql
from app.state import _L3_SEM, _mapping_memory, _sessions, _sse_queues, _save_sessions


# ── Mapping system prompt ─────────────────────────────────────────────────────
MAPPING_SYSTEM = """You are a senior data engineer performing cross-system Source-to-Target column mapping.
The source and target may use different databases, naming conventions, and data models
(e.g. Postgres→BigQuery, Oracle→Postgres, flat file→data warehouse, OLTP→OLAP).

IMPORTANT ALIAS AWARENESS:
Source and target systems frequently use different terminology for the same concept.
Always resolve semantic aliases before concluding there is no match:
  • "customer" / "cust" / "subscriber" / "sub" / "client"  →  same concept
  • "customer_id" / "cust_id" / "sub_id" / "client_id"     →  same concept
  • "address" / "addr"  |  "postal_code" / "zip" / "zipcode"
  • "phone_number" / "phone" / "phone_no" / "mobile"
  • "email_address" / "email" / "email_addr"
  • "billing_amount" / "bill_amt" / "invoice_amt" / "revenue"
  • "churn_risk_score" / "churn_score" / "risk_of_churn" / "risk_of_churn_pct"
When source uses "customer" and target uses "client_name", they ARE the same concept
(mapping_type=Direct, confidence ≥ 0.85, document the alias in rationale).

For each source column, identify the BEST matching target column considering:
  1. Semantic meaning (account_id ↔ acct_id, cust_nbr ↔ customer_number, customer ↔ client)
  2. Data type compatibility (VARCHAR→STRING, INT→INT64, DATETIME→TIMESTAMP)
  3. Business domain alignment (billing fields → billing tables, device fields → device tables)
  4. Sample data patterns when provided (ACT123 → activation_event_id style)

Return ONLY valid JSON — no markdown, no prose.

Output format:
[
  {
    "src_field": "<source column name>",
    "tgt_table": "<target table>",
    "tgt_column": "<target column>",
    "mapping_type": "<Direct|Derived|Lookup|Constant|Expression|Unused>",
    "mapping_relation": "<1:1|1:M|M:1>",
    "business_logic": "<SQL expression for the target platform>",
    "llm_confidence": <0.0-1.0 float>,
    "rationale": "<one-line explanation including any name/semantic difference>"
  }
]

Rules:
- mapping_type=Unused only when truly no sensible target exists.
- mapping_relation: "1:1" direct, "1:M" one source fans out, "M:1" multiple sources aggregate.
- llm_confidence: 1.0=exact semantic+name match, 0.7=strong semantic match with name difference,
  0.5=reasonable guess, <0.5=weak match (mark as review).
- business_logic MUST always be a valid SQL expression for the target system:
    Direct same type      → column name as-is: customer_id
    Type cast needed      → CAST(src AS TGT_TYPE) or DATE(src) / TIMESTAMP(src)
    String cleanup        → TRIM(src) or UPPER(TRIM(src))
    Name abbreviation map → src (document the semantic match in rationale)
    Derived               → transformation e.g. CONCAT(first_name,' ',last_name)
    M:1 aggregation       → COALESCE / SUM / CONCAT with fallback
    Lookup                → describe join key: lookup via dim_table.id = src
    Constant              → literal: 'USD' or 0
    Unused                → null
- Never fabricate target columns — only use columns from the target schema provided.
- When source and target use abbreviated vs. full names (sub_id vs subscriber_id),
  set mapping_type=Direct and explain the abbreviation in rationale.
"""

SQL_SYSTEM = """You are a BigQuery SQL expert. Given the approved source-to-target mapping,
generate a single production-quality CREATE OR REPLACE TABLE AS SELECT statement.

STRICT SYNTAX RULES — every violation causes a runtime error:
1. COMMAS: Every column expression in the SELECT list MUST end with a comma EXCEPT the very last one.
   CORRECT:
     SELECT
       customer_id,
       TRIM(first_name) AS first_name,
       CAST(zip AS STRING) AS geo_zip
     FROM ...
   WRONG (missing commas):
     SELECT
       customer_id
       TRIM(first_name) AS first_name
       CAST(zip AS STRING) AS geo_zip
     FROM ...

2. FROM CLAUSE: Always use the exact source table reference provided in the prompt under
   "Source table (FROM clause)". Never substitute a different table name or alias.

3. TABLE FORMAT: Use backtick `project.dataset.table` format for ALL table references.

4. STRUCTURE: Use this exact structure:
   CREATE OR REPLACE TABLE `project.dataset.tgt_table`
   AS
   WITH _src AS (
     SELECT
       col1_expr AS col1,
       col2_expr AS col2,
       col3_expr AS col3
     FROM `catalog.schema.source_table`
   )
   SELECT
     *,
     CURRENT_TIMESTAMP() AS _loaded_at,
     CAST(NULL AS STRING)  AS _source_run_id,
     ROW_NUMBER() OVER (PARTITION BY primary_key_col ORDER BY _loaded_at) AS _row_rank
   FROM _src;

5. AUDIT COLUMNS: Always append _loaded_at TIMESTAMP, _source_run_id STRING, _row_rank INT64.
6. COMMENTS: Add inline SQL comments for Derived/Lookup/Expression mappings.
7. OUTPUT: Return ONLY the SQL. No markdown fences, no explanations.
"""


# ── L3 chunking ───────────────────────────────────────────────────────────────
# Cap source columns per LLM call so wide tables don't blow the context window.
# Configurable via env (DM_CHUNK_SIZE), default 40.
_CHUNK_SIZE = int(os.getenv("DM_CHUNK_SIZE", "40"))


def _chunk_columns(columns: list, chunk_size: int = _CHUNK_SIZE) -> list[list]:
    """Split a column list into chunks for LLM calls."""
    return [columns[i:i + chunk_size] for i in range(0, len(columns), chunk_size)]


def _merge_chunk_results(chunks: list[list]) -> list:
    """Merge per-chunk mapping results, de-duplicating by src_field (later chunk wins)."""
    seen: Dict[str, Dict] = {}
    for chunk_result in chunks:
        for row in chunk_result:
            seen[row.get("src_field", "")] = row
    return list(seen.values())


def _build_mapping_system(session: Dict, fk_context: str = "") -> str:
    """Build context-aware system prompt for L3 mapping stage."""
    base = MAPPING_SYSTEM
    instructions = session.get("instructions", "")
    jira_ctx     = session.get("jira_context", {})
    memory       = session.get("mapping_memory", [])
    mig_ctx      = session.get("migration_context", {})

    extras = []

    domain_lines = []
    domain_text = mig_ctx.get("domain_context", "")
    if domain_text:
        domain_lines.append(domain_text)
    else:
        domain_lines.append(
            "DOMAIN: Telecom/ISP network migration. "
            "Source system is Frontier Communications (Databricks Unity Catalog). "
            "Target system is Verizon GCP BigQuery. "
            "Source column names carry 'frontier_' or 'ftr_' prefixes; "
            "target columns use 'vz_' prefixes. Strip these prefixes mentally when assessing semantic similarity."
        )

    fk_rules = mig_ctx.get("fk_rules", [])
    if fk_rules:
        domain_lines.append(
            "FK PROPAGATION RULES (apply these exactly — one source key fans into multiple target tables):\n"
            + "\n".join(f"  • {r}" for r in fk_rules)
        )

    scale_rules = mig_ctx.get("scale_rules", [])
    if scale_rules:
        domain_lines.append(
            "VALUE-SCALE RULES (apply explicit transforms, do NOT map as Direct):\n"
            + "\n".join(f"  • {r}" for r in scale_rules)
        )

    if domain_lines:
        extras.append("MIGRATION CONTEXT:\n" + "\n".join(domain_lines))

    if fk_context:
        extras.append(
            "RESOLVED FK ANCHORS FROM EARLIER BATCHES "
            "(use these exact column names for foreign-key references):\n" + fk_context
        )

    if instructions:
        extras.append(f"USER INSTRUCTIONS (follow strictly):\n{instructions}")
    if jira_ctx.get("summary"):
        extras.append(f"BUSINESS CONTEXT (from Jira):\n{jira_ctx['summary']}")

    if memory:
        mem_text = "\n".join(
            f"  {m['src']} -> {m['tgt']} [{m['type']}]: {m['logic']}"
            for m in memory[-20:]
        )
        extras.append(f"APPROVED MAPPING PATTERNS (learn from these):\n{mem_text}")

    if _mapping_memory:
        top_remembered = sorted(
            _mapping_memory.items(), key=lambda kv: kv[1].get("uses", 0), reverse=True
        )[:20]
        if top_remembered:
            rem_text = "\n".join(
                f"  {sf} → {v['tgt_table']}.{v['tgt_column']} "
                f"[{v['mapping_type']}] logic={v['business_logic'] or 'direct'} "
                f"(conf={v['confidence']:.2f}, used {v['uses']}x)"
                for sf, v in top_remembered
            )
            extras.append(
                "LEARNED MAPPINGS FROM PRIOR SESSIONS (reuse unless context differs):\n"
                + rem_text
            )

    if extras:
        return base + "\n\n" + "\n\n".join(extras)
    return base


async def _emit(session_id: str, event: str, data: Any):
    q = _sse_queues.get(session_id)
    if q:
        await q.put({"event": event, "data": data})


async def _run_pipeline(session_id: str):
    session = _sessions[session_id]
    llm = _make_llm(session)
    _pipeline_start_ts = time.time()

    _write_audit_event("pipeline.started", tenant=session.get("tenant"),
                       session_id=session_id,
                       metadata={"filename": session.get("filename",""), "model": session.get("api_config",{}).get("model","")})

    async def emit(event: str, data: Any):
        await _emit(session_id, event, data)
        session.setdefault("log", []).append({"ts": _now(), "event": event, "data": data})

    try:
        # L1: Parse uploaded schema
        session["stage"] = "L1"
        await emit("stage", {"stage": "L1", "status": "running", "msg": "Parsing source schema…"})
        await asyncio.sleep(0.1)

        schema_data = session.get("schema_data")
        if not schema_data:
            raise RuntimeError("No schema uploaded. Upload an XLSX or CSV file first.")
        src_tables = schema_data.get("tables", [])
        if not src_tables:
            raise RuntimeError("Schema file parsed 0 columns. Check the file format.")

        total_cols = sum(len(t["columns"]) for t in src_tables)
        await emit("stage", {"stage": "L1", "status": "done",
                              "msg": f"Parsed {len(src_tables)} table(s) · {total_cols} source columns"})
        session["l1_done"] = True

        # L2: Crawl target schema
        session["stage"] = "L2"

        if session.get("target_mode") == "files" and session.get("target_files_data"):
            await emit("stage", {"stage": "L2", "status": "running", "msg": "Loading custom target files…"})
            bq_tables = session["target_files_data"]
            total_tgt_cols = sum(len(t["columns"]) for t in bq_tables)
            await emit("stage", {"stage": "L2", "status": "done",
                                  "msg": f"Using custom target files — {len(bq_tables)} table(s) · {total_tgt_cols} columns"})
        else:
            await emit("stage", {"stage": "L2", "status": "running", "msg": "Crawling BigQuery INFORMATION_SCHEMA…"})

            cfg = session.get("bq_config", {})
            project        = cfg.get("project") or _BQ_PROJECT
            dataset        = cfg.get("dataset") or _BQ_DATASET
            gcp_creds      = cfg.get("gcp_creds") or _GCP_CREDS
            gcp_creds_json = cfg.get("gcp_creds_json")
            tgt_filter     = [t.strip() for t in cfg.get("target_tables", "").split(",") if t.strip()]

            if not project or not dataset:
                # Target schema is OPTIONAL — proceed source-only. Mappings can be
                # completed once a target is configured.
                await emit("stage", {"stage": "L2", "status": "done",
                                      "msg": "No target configured — running source-only (configure a target to map against)"})
                bq_tables = []
            else:
                from app.connectors.bigquery import crawl_bq
                try:
                    bq_tables = await asyncio.to_thread(
                        crawl_bq, project, dataset, gcp_creds, tgt_filter or None, gcp_creds_json
                    )
                except Exception as e:
                    raise RuntimeError(f"BigQuery crawl failed: {e}")

                if not bq_tables:
                    # Empty target is not fatal — continue source-only.
                    await emit("stage", {"stage": "L2", "status": "done",
                                          "msg": f"No tables found in {project}.{dataset} — running source-only"})
                    bq_tables = []
                else:
                    total_tgt_cols = sum(len(t["columns"]) for t in bq_tables)
                    await emit("stage", {"stage": "L2", "status": "done",
                                          "msg": f"Crawled {len(bq_tables)} BQ tables · {total_tgt_cols} target columns"})

        session["bq_tables"] = bq_tables
        session["l2_done"]   = True

        # Deterministically seed table-pair mappings (name + column-overlap) when
        # the user hasn't defined any — this scopes L3 and speeds it up. Best-effort.
        if bq_tables and not session.get("table_mappings"):
            try:
                from app.intelligence.confidence import match_tables
                pairs = match_tables(src_tables, bq_tables)
                if pairs:
                    session["table_mappings"] = [
                        {"src_table": p["src_table"], "tgt_table": p["tgt_table"]} for p in pairs
                    ]
                    await emit("stage", {"stage": "L2", "status": "info",
                                          "msg": f"Auto-suggested {len(pairs)} table pairing(s) from column overlap"})
            except Exception as _e:
                logger.warning("table auto-seed skipped: %s", _e)

        await emit("gate", {"gate": "gate1", "status": "auto_approved",
                             "msg": "Gate 1 auto-approved — proceeding to semantic mapping"})

        # L3: Semantic Mapping
        session["stage"] = "L3"
        llm_mode = session.get("api_config", {}).get("llm_mode", "backend")

        if llm_mode == "browser":
            await emit("stage", {"stage": "L3", "status": "browser_mode",
                                  "msg": "Browser-direct mode — frontend will map each table via Anthropic API"})
            session["status"]  = "browser_mapping"
            session["running"] = False
            await emit("status", {"status": "browser_mapping",
                                   "msg":    "Ready for browser-side mapping",
                                   "bq_tables": bq_tables})
            return

        await emit("stage", {"stage": "L3", "status": "running",
                              "msg": "Starting semantic mapping (Batch LLM)…"})

        tgt_type_lookup: Dict[str, Dict[str, str]] = {}
        for tbl in bq_tables:
            tgt_type_lookup[tbl["table"]] = {c["name"]: c["type"] for c in tbl["columns"]}

        tgt_summary = "\n".join(
            f"Table: {t['table']}\n  Columns: " +
            ", ".join(f"{c['name']}({c['type']})" for c in t["columns"])
            for t in bq_tables
        )

        fk_anchors: Dict[str, str] = {}
        mapping_system = _build_mapping_system(session, fk_context="")  # noqa: F841
        session_context_str = ""
        if session.get("instructions"):
            session_context_str += f"USER INSTRUCTIONS: {session['instructions']}\n"
        if session.get("jira_context", {}).get("summary"):
            session_context_str += f"BUSINESS CONTEXT: {session['jira_context']['summary']}\n"

        all_mappings: List[Dict] = []
        processed = 0
        total_src_cols = sum(len(t["columns"]) for t in src_tables)

        table_mapping_pairs = {
            m["src_table"]: [m2["tgt_table"] for m2 in session.get("table_mappings", []) if m2["src_table"] == m["src_table"]]
            for m in session.get("table_mappings", [])
        }
        has_table_mappings = bool(session.get("table_mappings"))

        if has_table_mappings:
            await emit("stage", {"stage": "L3", "status": "running",
                                  "msg": f"Using {len(session['table_mappings'])} table mapping pair(s) to scope semantic mapping…"})

        for src_table in src_tables:
            cols     = src_table["columns"]
            tbl_name = src_table["name"]

            if has_table_mappings:
                allowed_tgt_names = table_mapping_pairs.get(tbl_name, [])
                if not allowed_tgt_names:
                    logger.info("[L3] Skipping %s — no table mapping defined", tbl_name)
                    processed += len(cols)
                    continue
                scoped_bq_tables = [t for t in bq_tables if t["table"] in allowed_tgt_names]
                if not scoped_bq_tables:
                    scoped_bq_tables = bq_tables
                scoped_tgt_summary = "\n".join(
                    f"Table: {t['table']}\n  Columns: " +
                    ", ".join(f"{c['name']}({c['type']})" for c in t["columns"])
                    for t in scoped_bq_tables
                )
            else:
                scoped_tgt_summary = tgt_summary
                scoped_bq_tables   = bq_tables

            await emit("progress", {"processed": processed, "total": total_src_cols,
                                     "msg": f"Mapping table {tbl_name}…"})

            _FK_PATTERN = re.compile(
                r'(customer_id|account_id|device_id|service_id|order_id|ticket_id'
                r'|_key|_fk|_sk|_id)\b',
                re.IGNORECASE,
            )

            # Build the chunk list — single chunk for small tables (identical
            # behavior to the pre-chunking path), N chunks of _CHUNK_SIZE for wide ones.
            if len(cols) > _CHUNK_SIZE:
                col_chunks = _chunk_columns(cols, _CHUNK_SIZE)
            else:
                col_chunks = [cols]
            total_chunks = len(col_chunks)

            async def _map_one_chunk(chunk: list) -> list:
                """Run a single LLM mapping call for one column chunk."""
                fk_context_str = ""
                if fk_anchors:
                    fk_context_str = "\n".join(
                        f"  {sf} → {tgt}" for sf, tgt in fk_anchors.items()
                    )
                chunk_mapping_system = _build_mapping_system(session, fk_context=fk_context_str)

                src_desc = "\n".join(
                    f"  - {c['name']} ({c['type']})" + (f" sample={c['sample']}" if c.get("sample") else "")
                    for c in chunk
                )
                chunk_field_names = [c["name"] for c in chunk]
                mem_hints = _recall_mapping_hints(chunk_field_names)
                mem_hint_block = ""
                if mem_hints:
                    mem_hint_block = "\n\nPREVIOUSLY LEARNED MAPPINGS FOR THIS BATCH " \
                        "(apply unless evidence contradicts):\n" + "\n".join(
                            f"  {sf} → {v['tgt_table']}.{v['tgt_column']} "
                            f"[{v['mapping_type']}] logic={v['business_logic'] or 'direct'}"
                            for sf, v in mem_hints.items()
                    )
                prompt = (
                    f"SOURCE TABLE: {tbl_name}\n"
                    f"SOURCE COLUMNS TO MAP:\n{src_desc}\n\n"
                    f"TARGET SCHEMA:\n{scoped_tgt_summary}"
                    f"{mem_hint_block}\n\n"
                    "Map each source column to its best target. Return JSON array."
                )
                try:
                    sem = _L3_SEM or asyncio.Semaphore(2)
                    async with sem:
                        raw = await asyncio.to_thread(llm.complete_json, chunk_mapping_system, prompt)
                    # Accumulate token usage from THIS chunk (per spec — must run per chunk).
                    _add_usage(session, llm.last_usage, llm.provider, llm.model, "L3")
                    _save_sessions()
                    if isinstance(raw, dict):
                        raw = raw.get("mappings", [raw])
                    return raw or []
                except Exception as e:
                    logger.warning("LLM mapping chunk failed: %s", e)
                    return []

            chunk_results: List[List[Dict]] = []
            for ci, chunk in enumerate(col_chunks):
                if total_chunks > 1:
                    await _emit(
                        session_id, "log",
                        f"L3 [{tbl_name}] chunk {ci+1}/{total_chunks} ({len(chunk)} cols)",
                    )
                chunk_results.append(await _map_one_chunk(chunk))

            # Merge chunk results — de-duplicate by src_field, later chunk wins.
            raw_result = _merge_chunk_results(chunk_results) if total_chunks > 1 else (chunk_results[0] if chunk_results else [])

            # Process the (merged) raw_result identically to the pre-chunking path.
            if True:
                # Build a quick lookup for src column metadata across the whole table
                # (the merged raw_result may reference columns from any chunk).
                col_by_name = {c["name"]: c for c in cols}
                for item in raw_result:
                    src_col = col_by_name.get(item.get("src_field"))
                    tgt_table_name = item.get("tgt_table", "")
                    tgt_col_name   = item.get("tgt_column", "")
                    tgt_col_type   = tgt_type_lookup.get(tgt_table_name, {}).get(tgt_col_name, "STRING")

                    name_sim = _name_score(item.get("src_field", ""), tgt_col_name) if tgt_col_name else 0.0
                    type_sim = _type_score(src_col["type"] if src_col else "STRING", tgt_col_type) if tgt_col_name else 0.0
                    llm_conf = float(item.get("llm_confidence", 0.5))
                    is_unused = item.get("mapping_type", "").lower() == "unused" or not tgt_col_name
                    confidence = 0.0 if is_unused else compute_confidence(name_sim, type_sim, llm_conf)

                    src_field_name = item.get("src_field", "")
                    if (not is_unused and tgt_col_name
                            and _FK_PATTERN.search(src_field_name)):
                        fk_anchors[src_field_name] = f"{tgt_table_name}.{tgt_col_name}"

                    row_id = str(uuid.uuid4())
                    mapping_type     = item.get("mapping_type", "Direct") or "Direct"
                    mapping_relation = item.get("mapping_relation", "1:1") or "1:1"
                    src_type_val     = src_col["type"] if src_col else "STRING"
                    src_sample_val   = src_col.get("sample", "") if src_col else ""
                    raw_logic        = (item.get("business_logic") or "").strip()
                    if not raw_logic and not is_unused:
                        raw_logic = _auto_business_logic(
                            src_field_name, src_type_val, tgt_col_type,
                            mapping_type, mapping_relation,
                            src_sample=src_sample_val, tgt_name=tgt_col_name,
                        )
                    all_mappings.append({
                        "id":               row_id,
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

                processed += len(cols)

            await emit("progress", {"processed": processed, "total": total_src_cols,
                                     "msg": f"Mapped {processed}/{total_src_cols} columns…"})

        # Sanitize
        for m in all_mappings:
            if not m.get("tgt_column") and m["status"] != "unmapped":
                m["status"]     = "unmapped"
                m["tgt_table"]  = ""
                m["tgt_column"] = ""
                m["confidence"] = 0.0
                m["tier"]       = "none"

        # Deterministic derived-split rules (e.g. full name → first_name + last_name
        # at 100% confidence, 1:M) before relation types are recomputed.
        try:
            from app.intelligence.confidence import apply_split_rules
            apply_split_rules(all_mappings, src_tables, bq_tables)
        except Exception as _e:
            logger.warning("split rules skipped: %s", _e)

        _recompute_relation_types(all_mappings)

        session["mappings"] = all_mappings
        n_mapped   = sum(1 for m in all_mappings if m["status"] == "mapped")
        n_review   = sum(1 for m in all_mappings if m["status"] == "review")
        n_unmapped = sum(1 for m in all_mappings if m["status"] == "unmapped")
        avg_conf   = sum(m["confidence"] for m in all_mappings) / max(len(all_mappings), 1)

        session["stats"] = {
            "total": len(all_mappings),
            "mapped": n_mapped,
            "review": n_review,
            "unmapped": n_unmapped,
            "avg_confidence": round(avg_conf, 3),
        }
        _save_sessions()

        # Snapshot mappings as a "run" version so future Gate-2 edits can be diffed
        try:
            from app.routers.pipeline import _snapshot_mappings
            _snapshot_mappings(session, "run")
        except Exception as _e:
            logger.warning("mapping snapshot (run) failed: %s", _e)

        await emit("stage", {
            "stage": "L3", "status": "done",
            "msg": f"Mapping complete · {n_mapped} auto-mapped · {n_review} need review · {n_unmapped} unmapped",
            "stats": session["stats"],
        })
        session["l3_done"] = True

        session["stage"] = "gate2"
        await emit("gate", {"gate": "gate2", "status": "awaiting",
                             "msg": "Gate 2: Review and edit the mapping table, then click 'Approve & Generate SQL'"})

        session["status"] = "review"
        await emit("status", {"status": "review", "msg": "Ready for human review"})
        duration_s = round(time.time() - _pipeline_start_ts, 1)
        _write_audit_event("pipeline.completed", tenant=session.get("tenant"),
                           session_id=session_id,
                           metadata={
                               "status": "review",
                               "duration_s": duration_s,
                               "mapped": session.get("stats", {}).get("mapped", 0),
                               "total": session.get("stats", {}).get("total", 0),
                               "model": session.get("api_config", {}).get("model", ""),
                               "cost_usd": session.get("usage", {}).get("cost_usd", 0),
                           })
        stats = session.get("stats", {})
        asyncio.create_task(fire_webhook("pipeline.completed", session, data={
            "mapped":     stats.get("mapped", 0),
            "unmapped":   stats.get("unmapped", 0),
            "duration_s": duration_s,
            "cost_usd":   session.get("usage", {}).get("cost_usd", 0),
        }))

    except Exception as e:
        logger.exception("Pipeline error: %s", e)
        session["status"] = "error"
        session["error"]  = str(e)
        await emit("error", {"msg": str(e)})
        _write_audit_event("pipeline.failed", tenant=session.get("tenant"),
                           session_id=session_id,
                           metadata={"error": str(e)[:200]})
        asyncio.create_task(fire_webhook("pipeline.failed", session, data={"error": str(e)[:200]}))
    finally:
        session["running"] = False
        q = _sse_queues.get(session_id)
        if q:
            await q.put(None)


async def _run_sql_generation(session_id: str):
    session = _sessions[session_id]
    llm     = _make_llm(session)

    async def emit(event: str, data: Any):
        await _emit(session_id, event, data)

    try:
        session["stage"] = "L4"
        await emit("stage", {"stage": "L4", "status": "running",
                             "msg": "Generating STM, mapping documents & materialized SQL…"})

        mappings = session.get("mappings", [])
        cfg      = session.get("bq_config", {})
        project  = cfg.get("project") or _BQ_PROJECT
        dataset  = cfg.get("dataset") or _BQ_DATASET

        mapped_rows = [m for m in mappings if m["status"] != "unmapped" and m.get("tgt_table")]
        if not mapped_rows:
            raise RuntimeError("No mapped rows to generate SQL from.")

        tgt_groups: Dict[str, List] = {}
        for m in mapped_rows:
            tgt_groups.setdefault(m["tgt_table"], []).append(m)

        source_fqn = (
            session.get("source_table_fqn")
            or session.get("migration_context", {}).get("source_table_fqn")
            or ""
        )

        sql_blocks = []
        for tgt_table, rows in tgt_groups.items():
            src_tables_in_group = list({r["src_table"] for r in rows})
            primary_src = src_tables_in_group[0]

            if source_fqn:
                from_clause = f"`{source_fqn.replace('.', '`.`')}`"
            else:
                from_clause = f"`{primary_src}`"

            mapping_desc = "\n".join(
                f"  {r['src_field']} ({r['src_type']}) -> {r['tgt_column']} ({r['tgt_type']}) "
                f"[{r['mapping_type']}] logic: {r['business_logic'] or 'direct'}"
                for r in rows
            )

            prompt = (
                f"BQ Project: {project}\n"
                f"BQ Dataset: {dataset}\n"
                f"Source table (FROM clause): {from_clause}\n"
                f"Target table: {tgt_table}\n\n"
                f"Approved mappings:\n{mapping_desc}\n\n"
                "Generate a single CREATE OR REPLACE TABLE SQL statement. "
                "Use the exact source table reference shown in 'Source table (FROM clause)' "
                "for the FROM clause — do not substitute a different table name."
            )

            import re as _re
            sql = await asyncio.to_thread(llm.complete, SQL_SYSTEM, prompt, 0.05, 4096)
            sql = _re.sub(r"```(?:sql)?\s*", "", sql).replace("```", "").strip()
            sql = _format_sql(sql)
            sql_blocks.append(f"-- ═══ Target: {project}.{dataset}.{tgt_table} ═══\n{sql}\n")

        final_sql = (
            f"-- Auto-generated by xREF Agent · {_now()}\n"
            f"-- Session: {session_id[:8]}\n"
            f"-- Mapped: {len(mapped_rows)} columns · Avg confidence: {session.get('stats', {}).get('avg_confidence', 0):.0%}\n\n"
        ) + "\n".join(sql_blocks)

        session["generated_sql"] = final_sql

        # L4 also produces the human-facing migration deliverables: the
        # consolidated Source-to-Target Mapping document and a manifest of every
        # downloadable artifact, all derived from this same session so they stay
        # consistent with the SQL.
        try:
            from app.intelligence.stm_documents import (
                build_documents_manifest,
                build_stm_markdown,
            )
            session["mapping_document"] = build_stm_markdown(session_id, session)
            session["documents"] = build_documents_manifest(session_id, session)
            await emit("documents", {"documents": session["documents"],
                                     "count": len(session["documents"])})
        except Exception as _e:
            logger.warning("STM document generation skipped: %s", _e)

        session["status"] = "done"
        session["stage"]  = "done"
        _save_sessions()

        await emit("stage", {"stage": "L4", "status": "done",
                             "msg": "STM, mapping documents & SQL generated"})
        await emit("status", {"status": "done", "msg": "Pipeline complete"})

    except Exception as e:
        logger.exception("SQL gen error: %s", e)
        await emit("error", {"msg": str(e)})
    finally:
        session["running"] = False
        q = _sse_queues.get(session_id)
        if q:
            await q.put(None)
