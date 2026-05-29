"""
app/routers/migration.py — Migration Intelligence + Lineage/Impact endpoints.

Reads a session's mappings (tenant isolation enforced by middleware) and exposes:
  - GET /api/sessions/{sid}/readiness   migration readiness report (Layer 5)
  - GET /api/sessions/{sid}/lineage     column-level lineage graph (#4)
  - GET /api/sessions/{sid}/impact      impact-of-change analysis (#5)
"""
from __future__ import annotations

from typing import Optional

import io

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, StreamingResponse

from app.core.audit import _write_audit_event
from app.core.rbac import require_readonly
from app.core.session_store import _session_or_404
from app.intelligence import lineage as _lineage
from app.intelligence import migration_readiness as _mr
from app.intelligence import report as _report
from app.routers._helpers import _get_client_ip
from app.state import _audit_events

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


# ── Mapping Report (ReportSpec → JSON / HTML / XLSX) ────────────────────────────
def _spec_for(sid: str, source_platform=None, target_platform=None):
    s = _session_or_404(sid)
    sp = source_platform or _infer_source_platform(s)
    tp = target_platform or _infer_target_platform(s)
    return s, _report.build_report_spec(s, sp, tp, audit_events=_audit_events)


@router.get("/api/sessions/{sid}/report")
async def report_json(sid: str, request: Request,
                      source_platform=None, target_platform=None):
    _s, spec = _spec_for(sid, source_platform, target_platform)
    return spec


@router.get("/api/sessions/{sid}/report.html")
async def report_html(sid: str, request: Request,
                      source_platform=None, target_platform=None):
    _s, spec = _spec_for(sid, source_platform, target_platform)
    _write_audit_event("report.exported", tenant=_s.get("tenant"), session_id=sid,
                       ip=_get_client_ip(request), metadata={"format": "html"})
    return HTMLResponse(_report.render_html(spec))


@router.get("/api/sessions/{sid}/report.xlsx")
async def report_xlsx(sid: str, request: Request,
                      source_platform=None, target_platform=None):
    _s, spec = _spec_for(sid, source_platform, target_platform)
    data = _report.render_xlsx(spec)
    _write_audit_event("report.exported", tenant=_s.get("tenant"), session_id=sid,
                       ip=_get_client_ip(request), metadata={"format": "xlsx"})
    return StreamingResponse(
        io.BytesIO(data),
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="mapping_report_{sid[:8]}.xlsx"'},
    )


# Reference data for UIs: which platforms the readiness engine understands.
@router.get("/api/migration/platforms")
async def platforms():
    return {"platforms": sorted(_mr._PLATFORM.keys())}
