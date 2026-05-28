"""
app/routers/sessions.py — /api/sessions (create, list, get, all-usage)
"""
from __future__ import annotations

import uuid
from typing import Optional

from fastapi import APIRouter, Request
from pydantic import BaseModel

from app.core.audit import _now, _write_audit_event
from app.core.auth import _get_tenant_from_request, _verify_token
from app.core.session_store import _session_or_404
from app.routers._helpers import _get_client_ip
from app.state import _sessions, _save_sessions

router = APIRouter()


class SessionCreate(BaseModel):
    name: Optional[str] = None
    instructions: Optional[str] = None


@router.post("/api/sessions")
async def create_session(request: Request, body: Optional[SessionCreate] = None):
    tenant = _get_tenant_from_request(request) or "default"
    sid = str(uuid.uuid4())
    # Resolve the creating user up front so we can stamp it on the session for
    # later GDPR erasure queries.
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    email = "unknown"
    if token:
        p = _verify_token(token)
        if p:
            email = p.get("email", "unknown")
    _sessions[sid] = {
        "id":                sid,
        "tenant":            tenant,
        "user_email":        (email or "").lower() if email != "unknown" else "",
        "created_at":        _now(),
        "status":            "new",
        "stage":             "idle",
        "running":           False,
        "log":               [],
        "mappings":          [],
        "stats":             {},
        "bq_config":         {},
        "api_config":        {},
        "name":              (body.name if body else None) or f"Session {sid[:6]}",
        "instructions":      (body.instructions if body else None) or "",
        "mapping_memory":    [],
        "jira_context":      {},
        "target_mode":       "bq",
        "target_files_data": None,
    }
    _save_sessions()
    _write_audit_event("session.created", tenant=tenant, email=email,
                       session_id=sid, ip=_get_client_ip(request),
                       metadata={"name": _sessions[sid]["name"]})
    return {"session_id": sid}


@router.get("/api/sessions")
async def list_sessions(request: Request):
    tenant = _get_tenant_from_request(request)
    all_sessions = sorted(_sessions.values(), key=lambda x: x["created_at"], reverse=True)
    if tenant:
        all_sessions = [s for s in all_sessions if s.get("tenant", "default") == tenant]
    return [
        {
            "id":         s["id"],
            "status":     s["status"],
            "stage":      s["stage"],
            "created_at": s["created_at"],
            "stats":      s.get("stats", {}),
            "filename":   s.get("filename", ""),
        }
        for s in all_sessions
    ]


@router.get("/api/sessions/all-usage")
async def all_sessions_usage(request: Request):
    """Aggregate token usage across all sessions. Must be before /api/sessions/{sid}."""
    tenant = _get_tenant_from_request(request)
    result = []
    for sid, s in _sessions.items():
        if tenant and s.get("tenant", "default") != tenant:
            continue
        usage = s.get("usage", {})
        result.append({
            "session_id":   sid,
            "session_name": s.get("filename") or s.get("session_name") or sid[:8],
            "status":       s.get("status", "new"),
            "created_at":   s.get("created_at", ""),
            "calls":        usage.get("calls", 0),
            "input_tokens": usage.get("input_tokens", 0),
            "output_tokens":usage.get("output_tokens", 0),
            "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
            "cost_usd":     usage.get("cost_usd", 0.0),
            "provider":     usage.get("provider", ""),
            "model":        usage.get("model", ""),
            "breakdown":    usage.get("breakdown", []),
        })
    result.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    grand_cost   = sum(r["cost_usd"] for r in result)
    grand_input  = sum(r["input_tokens"] for r in result)
    grand_output = sum(r["output_tokens"] for r in result)
    grand_calls  = sum(r["calls"] for r in result)
    return {
        "sessions":     result,
        "grand_cost":   grand_cost,
        "grand_input":  grand_input,
        "grand_output": grand_output,
        "grand_calls":  grand_calls,
    }


@router.get("/api/sessions/{sid}")
async def get_session(sid: str):
    s = _session_or_404(sid)
    return {
        "id":          s["id"],
        "status":      s["status"],
        "stage":       s["stage"],
        "created_at":  s["created_at"],
        "stats":       s.get("stats", {}),
        "bq_config":   {k: v for k, v in s.get("bq_config", {}).items() if k != "gcp_creds"},
        "api_config":  {k: ("***" if "key" in k else v) for k, v in s.get("api_config", {}).items()},
        "filename":    s.get("filename", ""),
        "schema_data": s.get("schema_data"),
        "error":       s.get("error"),
        "log":         s.get("log", [])[-50:],
    }
