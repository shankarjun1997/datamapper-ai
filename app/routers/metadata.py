"""
app/routers/metadata.py — canonical metadata repository API (Layer 3).

Tenant-scoped. Ingest crawled schemas (directly or from a session), then browse
the versioned catalog with pagination.
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core import metadata_repo as repo
from app.core.audit import _write_audit_event
from app.core.rbac import require_mapper, require_readonly
from app.core.session_store import _session_or_404
from app.routers._helpers import _get_client_ip

router = APIRouter()


class IngestBody(BaseModel):
    session_id: Optional[str] = None
    system_name: Optional[str] = None
    platform: Optional[str] = "generic"
    schema_data: Optional[dict] = None


@router.post("/api/metadata/ingest")
async def ingest(body: IngestBody, request: Request, _user=Depends(require_mapper)):
    """Ingest a crawled schema into the canonical repo (from a session or inline)."""
    tenant = _user["tenant"]
    schema_data = body.schema_data
    system_name = body.system_name
    platform = body.platform or "generic"

    if body.session_id:
        s = _session_or_404(body.session_id)
        schema_data = schema_data or s.get("schema_data")
        system_name = system_name or s.get("filename") or s.get("name") or body.session_id[:8]
        if s.get("bq_config"):
            platform = "bigquery"

    if not schema_data or not schema_data.get("tables"):
        raise HTTPException(422, "No schema_data with tables to ingest")
    if not system_name:
        raise HTTPException(422, "system_name is required")

    counts = repo.ingest_schema(tenant, system_name, platform, schema_data,
                                updated_by=_user.get("email", ""))
    _write_audit_event("metadata.ingested", tenant=tenant, email=_user.get("email"),
                       ip=_get_client_ip(request),
                       metadata={"system": system_name, **counts})
    return {"ok": True, "system": system_name, "platform": platform, "counts": counts}


@router.get("/api/metadata/objects")
async def list_objects(_user=Depends(require_readonly), type: Optional[str] = None,
                       parent: Optional[str] = None, q: Optional[str] = None,
                       limit: int = 100, offset: int = 0):
    return repo.list_objects(_user["tenant"], otype=type, parent=parent, q=q,
                             limit=limit, offset=offset)


@router.get("/api/metadata/stats")
async def metadata_stats(_user=Depends(require_readonly)):
    return repo.stats(_user["tenant"])


@router.get("/api/metadata/objects/{oid:path}/history")
async def object_history(oid: str, _user=Depends(require_readonly)):
    hist = repo.get_history(_user["tenant"], oid)
    if not hist:
        raise HTTPException(404, "Object not found")
    return {"id": oid, "versions": hist}


@router.get("/api/metadata/objects/{oid:path}")
async def get_object(oid: str, _user=Depends(require_readonly)):
    obj = repo.get_object(_user["tenant"], oid)
    if not obj:
        raise HTTPException(404, "Object not found")
    return obj
