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
from app.state import _save_sessions

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
