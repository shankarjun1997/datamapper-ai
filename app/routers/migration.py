"""
app/routers/migration.py — Migration Intelligence + Lineage/Impact endpoints.

Reads a session's mappings (tenant isolation enforced by middleware) and exposes:
  - GET /api/sessions/{sid}/readiness   migration readiness report (Layer 5)
  - GET /api/sessions/{sid}/lineage     column-level lineage graph (#4)
  - GET /api/sessions/{sid}/impact      impact-of-change analysis (#5)
"""
from __future__ import annotations

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request

from app.core.rbac import require_readonly
from app.core.session_store import _session_or_404
from app.intelligence import lineage as _lineage
from app.intelligence import migration_readiness as _mr

# Read endpoints → at least an authenticated readonly user (no-op in dev).
router = APIRouter(dependencies=[Depends(require_readonly)])


def _infer_target_platform(session: dict) -> str:
    if session.get("target_mode") == "bq" or session.get("bq_config"):
        return "bigquery"
    return (session.get("target_platform") or "generic")


def _infer_source_platform(session: dict) -> str:
    return (session.get("source_db_type")
            or (session.get("source_config", {}) or {}).get("db_type")
            or session.get("source_platform")
            or "generic")


@router.get("/api/sessions/{sid}/readiness")
async def readiness(sid: str, request: Request,
                    source_platform: Optional[str] = None,
                    target_platform: Optional[str] = None):
    s = _session_or_404(sid)
    mappings = s.get("mappings", []) or []
    sp = source_platform or _infer_source_platform(s)
    tp = target_platform or _infer_target_platform(s)
    return _mr.assess_session(mappings, sp, tp)


@router.get("/api/sessions/{sid}/lineage")
async def lineage(sid: str, request: Request):
    s = _session_or_404(sid)
    return _lineage.build_lineage(s.get("mappings", []) or [])


@router.get("/api/sessions/{sid}/impact")
async def impact(sid: str, request: Request, ref: str, direction: str = "forward"):
    if direction not in ("forward", "reverse"):
        raise HTTPException(422, "direction must be 'forward' or 'reverse'")
    if not (ref or "").strip():
        raise HTTPException(422, "ref is required (e.g. 'customers' or 'customers.id')")
    s = _session_or_404(sid)
    return _lineage.impact(s.get("mappings", []) or [], ref, direction)


# Reference data for UIs: which platforms the readiness engine understands.
@router.get("/api/migration/platforms")
async def platforms():
    return {"platforms": sorted(_mr._PLATFORM.keys())}
