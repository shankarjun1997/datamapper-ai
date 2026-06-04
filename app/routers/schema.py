"""
app/routers/schema.py — /api/sessions/{sid}/upload + parse-ddl + jira-context + source-connect
"""
from __future__ import annotations

import asyncio
import re
from typing import Optional

import base64
import httpx
from fastapi import APIRouter, Depends, File, HTTPException, Request, UploadFile
from pydantic import BaseModel

from app.config import _JIRA_EMAIL, _JIRA_TOKEN, _JIRA_URL
from app.core.audit import _write_audit_event
from app.core.llm_client import _add_usage, _make_llm
from app.core.rbac import require_mapper
from app.core.session_store import _session_or_404
from app.parsers.ddl import parse_ddl
from app.parsers.schema import parse_schema_file
from app.routers._helpers import _check_rate_limit, _get_client_ip, _validate_upload

# All schema endpoints mutate session state → require mapper or higher
# (no-op in dev where XREF_REQUIRE_AUTH=false).
router = APIRouter(dependencies=[Depends(require_mapper)])


@router.post("/api/sessions/{sid}/upload")
async def upload_schema(sid: str, request: Request, file: UploadFile = File(...),
                        append: bool = False):
    """Upload a CSV/DDL/Excel schema. With ?append=true the parsed tables are
    MERGED into the session's existing schema (so clients can add multiple CSVs
    repeatedly); otherwise they replace it."""
    if not _check_rate_limit(_get_client_ip(request)):
        raise HTTPException(429, "Too many requests — slow down")
    s = _session_or_404(sid)
    content = await file.read()
    safe_name = _validate_upload(file.filename or "upload.csv", content)
    try:
        parsed = parse_schema_file(content, safe_name)
    except Exception as e:
        raise HTTPException(422, str(e))

    from app.intelligence.insights import merge_schemas
    existing = s.get("schema_data") if append else None
    schema_data = merge_schemas(existing, parsed) if (existing and existing.get("tables")) else parsed
    s["schema_data"] = schema_data
    s["filename"]    = safe_name
    s["status"]      = "schema_uploaded"

    # Track each contributing source file for the UI.
    added_cols = sum(len(t["columns"]) for t in parsed["tables"])
    files = s.setdefault("source_files", [])
    files.append({"name": safe_name,
                  "tables": [t["name"] for t in parsed["tables"]],
                  "columns": added_cols})

    total = sum(len(t["columns"]) for t in schema_data["tables"])

    # Best-effort: ingest into the canonical metadata repository so Discovery,
    # lineage, and versioning populate automatically. Never blocks the upload.
    try:
        from app.core.metadata_repo import ingest_schema
        ingest_schema(s.get("tenant") or "default",
                      system_name=s.get("filename") or sid[:8],
                      platform=(s.get("source_db_type") or "source"),
                      schema_data=schema_data, updated_by="")
    except Exception:
        pass

    _write_audit_event("schema.uploaded", tenant=s.get("tenant"), session_id=sid,
                       ip=_get_client_ip(request),
                       metadata={"filename": safe_name, "append": append,
                                 "tables": len(schema_data["tables"]), "columns": total})
    return {
        "ok":          True,
        "appended":    bool(existing and existing.get("tables")),
        "added":       {"file": safe_name, "tables": [t["name"] for t in parsed["tables"]], "columns": added_cols},
        "files":       [f["name"] for f in files],
        "tables":      len(schema_data["tables"]),
        "columns":     total,
        "preview":     schema_data["tables"][0]["columns"][:8] if schema_data["tables"] else [],
        "table_names": [t["name"] for t in schema_data["tables"]],
        "schema":      schema_data["tables"],
    }


@router.get("/api/sessions/{sid}/schema-insight")
async def schema_insight(sid: str):
    """Quick few-word characterization of the session's discovered schema."""
    from app.intelligence.insights import summarize_schema
    s = _session_or_404(sid)
    return summarize_schema(s.get("schema_data") or {}, name=s.get("filename", ""))


class DatasetInsightRequest(BaseModel):
    tables: list = []
    name:   Optional[str] = ""


@router.post("/api/sessions/{sid}/dataset-insight")
async def dataset_insight(sid: str, req: DatasetInsightRequest):
    """Quick insight for an arbitrary dataset (e.g. a cloud dataset being
    browsed before import). Accepts a tables[] payload."""
    from app.intelligence.insights import summarize_schema
    _session_or_404(sid)
    return summarize_schema({"tables": req.tables}, name=req.name or "")


class JiraContextRequest(BaseModel):
    jira_url:   Optional[str] = ""
    jira_email: Optional[str] = ""
    jira_token: Optional[str] = ""
    ticket_url: Optional[str] = ""


@router.post("/api/sessions/{sid}/jira-context")
async def fetch_jira_context(sid: str, req: JiraContextRequest):
    s = _session_or_404(sid)

    base_url  = req.jira_url  or _JIRA_URL
    email     = req.jira_email or _JIRA_EMAIL
    token     = req.jira_token or _JIRA_TOKEN
    ticket    = req.ticket_url or ""

    if not base_url or not token:
        raise HTTPException(422, "Jira base URL and API token are required (or set JIRA_URL/JIRA_TOKEN in .env)")

    issue_key = ticket
    if "/" in ticket:
        issue_key = ticket.rstrip("/").split("/")[-1]

    if not issue_key:
        raise HTTPException(422, "Jira ticket URL or issue key is required")

    api_url = f"{base_url.rstrip('/')}/rest/api/3/issue/{issue_key}"
    creds = base64.b64encode(f"{email}:{token}".encode()).decode()

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(api_url, headers={
                "Authorization": f"Basic {creds}",
                "Accept": "application/json",
            })
        if resp.status_code == 401:
            raise HTTPException(401, "Jira authentication failed. Check email and API token.")
        if resp.status_code == 404:
            raise HTTPException(404, f"Jira issue {issue_key!r} not found.")
        resp.raise_for_status()
        issue_data = resp.json()
    except httpx.RequestError as e:
        raise HTTPException(502, f"Could not reach Jira: {e}")

    fields = issue_data.get("fields", {})
    summary_text = fields.get("summary", "")
    desc = fields.get("description", {})
    desc_text = ""
    if isinstance(desc, dict):
        for block in desc.get("content", []):
            for item in block.get("content", []):
                if item.get("type") == "text":
                    desc_text += item.get("text", "") + " "
    elif isinstance(desc, str):
        desc_text = desc

    llm = _make_llm(s)
    prompt = f"""Jira Issue: {issue_key}
Summary: {summary_text}
Description: {desc_text[:2000]}

Extract from this Jira story:
1. Intent summary (1-2 sentences, what data engineering work is needed)
2. Source system hint (what source system/database is mentioned)
3. Target system hint (what target/destination is mentioned)
4. Key business rules (bullet list, max 5)

Return JSON: {{"summary": "...", "source_hint": "...", "target_hint": "...", "business_rules": ["..."]}}"""

    system = "You are a data engineering analyst. Extract key mapping context from a Jira story. Return only valid JSON."
    try:
        ctx = await asyncio.to_thread(llm.complete_json, system, prompt)
        _add_usage(s, llm.last_usage, llm.provider, llm.model, "L1-jira")
    except Exception:
        ctx = {"summary": summary_text, "source_hint": "", "target_hint": "", "business_rules": []}

    ctx["issue_key"] = issue_key
    ctx["jira_url"] = f"{base_url.rstrip('/')}/browse/{issue_key}"
    s["jira_context"] = ctx
    return {"ok": True, "context": ctx}


async def _fetch_jira_text(base_url: str, email: str, token: str, ticket: str) -> tuple[str, str]:
    """Fetch a Jira issue and return (issue_key, 'summary + description' plain text)."""
    base_url = base_url or _JIRA_URL
    email = email or _JIRA_EMAIL
    token = token or _JIRA_TOKEN
    if not base_url or not token:
        raise HTTPException(422, "Jira base URL and API token are required (or set JIRA_URL/JIRA_TOKEN in .env)")
    issue_key = ticket.rstrip("/").split("/")[-1] if "/" in ticket else ticket
    if not issue_key:
        raise HTTPException(422, "Jira ticket URL or issue key is required")
    api_url = f"{base_url.rstrip('/')}/rest/api/3/issue/{issue_key}"
    creds = base64.b64encode(f"{email}:{token}".encode()).decode()
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(api_url, headers={"Authorization": f"Basic {creds}", "Accept": "application/json"})
        if resp.status_code == 401:
            raise HTTPException(401, "Jira authentication failed. Check email and API token.")
        if resp.status_code == 404:
            raise HTTPException(404, f"Jira issue {issue_key!r} not found.")
        resp.raise_for_status()
        issue_data = resp.json()
    except httpx.RequestError as e:
        raise HTTPException(502, f"Could not reach Jira: {e}")
    fields = issue_data.get("fields", {})
    text = fields.get("summary", "")
    desc = fields.get("description", {})
    if isinstance(desc, dict):
        for block in desc.get("content", []):
            for item in block.get("content", []):
                if item.get("type") == "text":
                    text += " " + item.get("text", "")
    elif isinstance(desc, str):
        text += " " + desc
    return issue_key, text.strip()


class ContextSourceRequest(BaseModel):
    text:        Optional[str] = ""
    source_name: Optional[str] = ""
    append:      Optional[bool] = False
    # Optional live-Jira fetch (used when text isn't pasted directly):
    jira_url:    Optional[str] = ""
    jira_email:  Optional[str] = ""
    jira_token:  Optional[str] = ""
    ticket_url:  Optional[str] = ""


@router.post("/api/sessions/{sid}/source-from-context")
async def source_from_context(sid: str, req: ContextSourceRequest, request: Request):
    """Infer a SOURCE schema from free-form context — no database connection.

    Accepts pasted text (a Jira story, a description, a flat extract) and/or a
    live Jira ticket to fetch. The LLM infers the source tables/columns, which
    are stored exactly like an uploaded schema so the rest of the pipeline runs
    unchanged."""
    s = _session_or_404(sid)
    text = (req.text or "").strip()
    src_name = (req.source_name or "").strip()
    if req.ticket_url:
        issue_key, jira_text = await _fetch_jira_text(req.jira_url, req.jira_email, req.jira_token, req.ticket_url)
        text = f"{text}\n\n{jira_text}".strip()
        src_name = src_name or issue_key
    if not text:
        raise HTTPException(422, "Provide context text or a Jira ticket to infer the source from.")

    llm = _make_llm(s)
    if llm is None:
        raise HTTPException(409, "Inferring a source from context needs a server-side LLM provider; this session "
                                 "is in Browser-LLM mode. Switch providers in Settings, or upload a schema file.")

    from app.intelligence.source_infer import _SOURCE_SYS, build_source_prompt, normalize_schema
    try:
        raw = await asyncio.to_thread(llm.complete_json, _SOURCE_SYS, build_source_prompt(text, src_name))
        _add_usage(s, llm.last_usage, llm.provider, llm.model, "source_from_context")
    except Exception as e:
        raise HTTPException(502, f"Source inference failed: {e}")

    schema_data = normalize_schema(raw, default_name=src_name or "source_from_context")
    if not schema_data["tables"]:
        raise HTTPException(422, "Could not infer any source fields from that context. Add more detail and retry.")

    from app.intelligence.insights import merge_schemas
    existing = s.get("schema_data") if req.append else None
    if existing and existing.get("tables"):
        schema_data = merge_schemas(existing, schema_data)
    s["schema_data"] = schema_data
    s["filename"]    = src_name or "context_source"
    s["status"]      = "schema_uploaded"
    s.setdefault("source_files", []).append({
        "name": s["filename"], "tables": [t["name"] for t in schema_data["tables"]],
        "columns": sum(len(t["columns"]) for t in schema_data["tables"]), "origin": "context",
    })
    try:
        from app.core.metadata_repo import ingest_schema
        ingest_schema(s.get("tenant") or "default", system_name=s["filename"],
                      platform="context", schema_data=schema_data, updated_by="")
    except Exception:
        pass
    _write_audit_event("schema.from_context", tenant=s.get("tenant"), session_id=sid,
                       ip=_get_client_ip(request),
                       metadata={"source_name": s["filename"], "via_jira": bool(req.ticket_url),
                                 "tables": len(schema_data["tables"])})
    return {
        "ok": True, "source_name": s["filename"],
        "tables": len(schema_data["tables"]),
        "columns": sum(len(t["columns"]) for t in schema_data["tables"]),
        "schema": schema_data["tables"],
    }


class SuggestTargetRequest(BaseModel):
    instructions: Optional[str] = ""
    context:      Optional[str] = ""


@router.post("/api/sessions/{sid}/suggest-target")
async def suggest_target(sid: str, req: SuggestTargetRequest):
    """Propose a TARGET schema from the session's source + optional context, and
    store it as the custom-files target so the pipeline maps onto it. The user
    can edit it before running."""
    s = _session_or_404(sid)
    schema_data = s.get("schema_data") or {}
    if not schema_data.get("tables"):
        raise HTTPException(422, "Add a source first (upload a file or infer from context), then generate a target.")

    llm = _make_llm(s)
    if llm is None:
        raise HTTPException(409, "Generating a target needs a server-side LLM provider; this session is in "
                                 "Browser-LLM mode. Switch providers in Settings, or upload a target schema.")

    from app.intelligence.source_infer import _TARGET_SYS, build_target_prompt, normalize_schema
    context = req.context or (s.get("jira_context", {}) or {}).get("summary", "")
    try:
        raw = await asyncio.to_thread(
            llm.complete_json, _TARGET_SYS,
            build_target_prompt(schema_data, context, req.instructions or ""))
        _add_usage(s, llm.last_usage, llm.provider, llm.model, "suggest_target")
    except Exception as e:
        raise HTTPException(502, f"Target generation failed: {e}")

    target = normalize_schema(raw, default_name="target")
    if not target["tables"]:
        raise HTTPException(422, "Could not generate a target from the source. Add instructions and retry.")
    s["target_files_data"] = target["tables"]
    s["target_mode"]       = "files"
    s["bq_tables"]         = target["tables"]
    return {
        "ok": True, "tables": len(target["tables"]),
        "columns": sum(len(t["columns"]) for t in target["tables"]),
        "target": target["tables"],
    }


class SourceConnectRequest(BaseModel):
    db_type:           str
    connection_string: str
    schema_filter:     Optional[str] = ""
    table_filter:      Optional[str] = ""


@router.post("/api/sessions/{sid}/source-connect")
async def source_connect(sid: str, req: SourceConnectRequest):
    s = _session_or_404(sid)
    from app.connectors.source_db import crawl_source_db
    try:
        schema_data = await asyncio.to_thread(
            crawl_source_db,
            req.db_type,
            req.connection_string,
            req.schema_filter or "",
            req.table_filter or "",
        )
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"Connection failed: {e}")

    s["schema_data"]  = schema_data
    s["source_type"]  = req.db_type
    host_match = re.search(r"@([^/:]+)", req.connection_string)
    host = host_match.group(1) if host_match else "unknown"
    s["source_conn_display"] = f"{req.db_type}://{host}"
    s["filename"]    = s["source_conn_display"]
    s["status"]      = "schema_uploaded"

    total = sum(len(t["columns"]) for t in schema_data["tables"])
    return {
        "ok":          True,
        "tables":      len(schema_data["tables"]),
        "columns":     total,
        "table_names": [t["name"] for t in schema_data["tables"]],
        "preview":     schema_data["tables"][0]["columns"][:8] if schema_data["tables"] else [],
        "schema":      schema_data["tables"],
    }


class DDLRequest(BaseModel):
    ddl: str


@router.post("/api/sessions/{sid}/parse-ddl")
async def parse_ddl_source(sid: str, req: DDLRequest):
    s = _session_or_404(sid)
    try:
        schema_data = parse_ddl(req.ddl)
    except Exception as e:
        raise HTTPException(422, f"DDL parse failed: {e}")
    if not schema_data["tables"]:
        raise HTTPException(422, "No CREATE TABLE statements found in the DDL")
    s["schema_data"] = schema_data
    s["filename"]    = "ddl_input.sql"
    s["status"]      = "schema_uploaded"
    s["source_type"] = "ddl"
    total = sum(len(t["columns"]) for t in schema_data["tables"])
    return {
        "ok":          True,
        "tables":      len(schema_data["tables"]),
        "columns":     total,
        "table_names": [t["name"] for t in schema_data["tables"]],
        "schema":      schema_data["tables"],
    }
