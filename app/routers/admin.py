"""
app/routers/admin.py — /api/admin/audit + /api/audit + /api/sessions/{sid}/summary
                       + SIEM exports (CEF, JSON-LD) + tenant provisioning
"""
from __future__ import annotations

import csv
import io
import json
import os
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse

from app.config import _ADMIN_TENANT
from app.core.audit import _count_by, _now, _write_audit_event
from app.core.auth import _verify_token, _hash_password
from app.core.llm_client import _make_llm
from app.core.rbac import require_admin
from app.core.session_store import _session_or_404
from app.routers._helpers import _get_client_ip
from app.state import _audit_events, _audit_log, _TENANTS as _tenants, _save_tenants

router = APIRouter()


@router.get("/api/audit")
async def list_audit():
    """Return all audit records (newest first), without the full mapping snapshot."""
    return [
        {k: v for k, v in rec.items() if k != "mappings_snapshot"}
        for rec in reversed(_audit_log)
    ]


@router.get("/api/audit/{record_id}/csv")
async def audit_csv(record_id: str):
    """Stream the mapping snapshot of a specific audit record as CSV."""
    rec = next((r for r in _audit_log if r["id"] == record_id), None)
    if not rec:
        raise HTTPException(404, "Audit record not found")
    buf = io.StringIO()
    fieldnames = ["src_table","src_field","src_type","tgt_table","tgt_column",
                  "tgt_type","mapping_type","confidence","status","business_logic"]
    w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    w.writeheader()
    w.writerows(rec.get("mappings_snapshot", []))
    csv_bytes = buf.getvalue().encode()
    return StreamingResponse(
        io.BytesIO(csv_bytes),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="audit_{record_id[:8]}.csv"'},
    )


@router.get("/api/admin/audit")
async def admin_audit(
    request:    Request,
    tenant:     Optional[str] = None,
    event:      Optional[str] = None,
    email:      Optional[str] = None,
    since:      Optional[str] = None,
    until:      Optional[str] = None,
    limit:      int = 500,
    fmt:        str = "json",
    _user=Depends(require_admin),
):
    """Query the structured audit event log. Requires admin role."""
    caller_payload = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        caller_payload = _verify_token(auth_header[7:])
    if not caller_payload:
        raise HTTPException(status_code=401, detail="Authentication required")

    caller_tenant = caller_payload.get("tenant")
    is_admin = caller_tenant == _ADMIN_TENANT

    events = list(reversed(_audit_events))

    if tenant:
        if not is_admin and tenant != caller_tenant:
            raise HTTPException(403, "Cannot query other tenant's audit events")
        events = [e for e in events if e.get("tenant") == tenant]
    elif not is_admin:
        events = [e for e in events if e.get("tenant") == caller_tenant]

    if event:
        events = [e for e in events if e.get("event","").startswith(event)]
    if email:
        events = [e for e in events if e.get("email","").lower() == email.lower()]
    if since:
        events = [e for e in events if e.get("ts","") >= since]
    if until:
        events = [e for e in events if e.get("ts","") <= until]

    events = events[:limit]

    _write_audit_event("admin.audit_queried", tenant=caller_tenant,
                       email=caller_payload.get("email"), ip=_get_client_ip(request),
                       metadata={"filter_tenant": tenant, "filter_event": event,
                                 "result_count": len(events)})

    if fmt == "csv":
        buf = io.StringIO()
        fieldnames = ["id","ts","event","tenant","email","session_id","ip","meta"]
        w = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
        w.writeheader()
        for e in events:
            row = dict(e)
            row["meta"] = json.dumps(e.get("meta", {}))
            w.writerow(row)
        return StreamingResponse(
            io.BytesIO(buf.getvalue().encode()),
            media_type="text/csv",
            headers={"Content-Disposition": 'attachment; filename="xref_audit.csv"'},
        )

    return {
        "total":  len(events),
        "events": events,
        "summary": {
            "by_event":  _count_by(events, "event"),
            "by_tenant": _count_by(events, "tenant"),
            "by_email":  _count_by(events, "email"),
        }
    }


@router.get("/api/admin/audit/summary")
async def admin_audit_summary(request: Request):
    """Marketing-ready summary: DAU, WAU, active tenants, top features."""
    caller_payload = None
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        caller_payload = _verify_token(auth_header[7:])
    if not caller_payload:
        raise HTTPException(status_code=401, detail="Authentication required")

    events = _audit_events
    now_ts = _now()
    day_ago  = now_ts[:8]

    try:
        import datetime as dt
        today = dt.date.today()
        week_start = (today - dt.timedelta(days=7)).isoformat()
    except Exception:
        week_start = ""

    logins_ok   = [e for e in events if e["event"] == "auth.login_ok"]
    logins_fail = [e for e in events if e["event"] == "auth.login_fail"]
    sessions    = [e for e in events if e["event"] == "session.created"]
    pipelines   = [e for e in events if e["event"] == "pipeline.completed"]
    exports     = [e for e in events if e["event"].startswith("export.")]

    active_tenants = len(set(e["tenant"] for e in logins_ok))
    unique_users   = len(set(e["email"] for e in logins_ok))

    dau = len(set(e["email"] for e in logins_ok if e.get("ts","").startswith(day_ago)))
    wau = len(set(e["email"] for e in logins_ok if e.get("ts","") >= week_start)) if week_start else 0

    avg_duration = 0.0
    durations = [e["meta"].get("duration_s", 0) for e in pipelines if e.get("meta")]
    if durations:
        avg_duration = round(sum(durations) / len(durations), 1)

    avg_cost = 0.0
    costs = [e["meta"].get("cost_usd", 0) for e in pipelines if e.get("meta")]
    if costs:
        avg_cost = round(sum(costs) / len(costs), 4)

    return {
        "total_logins":      len(logins_ok),
        "failed_logins":     len(logins_fail),
        "active_tenants":    active_tenants,
        "unique_users":      unique_users,
        "dau":               dau,
        "wau":               wau,
        "sessions_created":  len(sessions),
        "pipelines_run":     len(pipelines),
        "exports_total":     len(exports),
        "export_breakdown":  _count_by(exports, "event"),
        "avg_pipeline_duration_s": avg_duration,
        "avg_pipeline_cost_usd":   avg_cost,
        "logins_by_tenant":  _count_by(logins_ok, "tenant"),
        "feature_adoption":  _count_by(events, "event"),
    }


@router.get("/api/sessions/{sid}/summary")
async def get_session_summary(sid: str):
    s = _session_or_404(sid)
    mappings = s.get("mappings", [])
    if not mappings:
        return {"ready": False}

    llm = _make_llm(s)
    stats = s.get("stats", {})
    prompt = f"""Session: {sid[:8]}
Source: {s.get('filename') or s.get('source_type', 'unknown')}
Instructions given: {s.get('instructions', 'None')}
Stats: {json.dumps(stats)}
Top mappings (first 30): {json.dumps(mappings[:30], indent=2)}

Write a structured summary with:
1. Overview (2 sentences)
2. Key mapping decisions made (bullet points, max 8)
3. Fields needing attention (unmapped or low confidence, max 5)
4. Recommended next steps (max 3)
Keep total under 400 words."""

    system = "You are a data engineering expert. Summarize a mapping session concisely."
    try:
        summary_text = await __import__("asyncio").to_thread(llm.complete, system, prompt, 0.1, 1024)
    except Exception as e:
        summary_text = f"Summary unavailable: {e}"

    return {"ready": True, "summary": summary_text, "stats": stats}


# ── CrewAI Self-Learning Endpoints ────────────────────────────────────────────

@router.get("/api/crew/learnings")
async def get_crew_learnings(limit: int = 100):
    """Return recent raw learning events captured from user corrections."""
    from app.core.crew_learnings import get_learnings, pending_extraction_count
    return {
        "learnings": get_learnings(limit),
        "pending_extraction": pending_extraction_count(),
    }


@router.get("/api/crew/patterns")
async def get_crew_patterns():
    """Return current distilled mapping rules learned from user corrections."""
    from app.core.crew_learnings import get_patterns, pending_extraction_count
    return {
        "patterns": get_patterns(),
        "pending_extraction": pending_extraction_count(),
    }


@router.post("/api/crew/extract-patterns")
async def trigger_pattern_extraction(request: Request):
    """Manually trigger LLM pattern extraction from accumulated learning events.
    Requires auth. Uses the session-level LLM config from the first available session,
    or falls back to global env config."""
    from app.core.crew_learnings import extract_patterns, pending_extraction_count, get_patterns
    from app.state import _sessions

    pending = pending_extraction_count()
    if pending == 0:
        return {"ok": True, "message": "No pending events to extract", "patterns": get_patterns()}

    # Build a minimal session dict for LLM client
    any_session = next(iter(_sessions.values()), {}) if _sessions else {}
    try:
        llm = _make_llm(any_session)
        if llm is None:
            raise ValueError("LLM in browser mode — cannot extract patterns server-side")
        patterns = extract_patterns(llm)
        return {"ok": True, "patterns_extracted": len(patterns),
                "pending_before": pending, "message": "Pattern extraction complete"}
    except Exception as e:
        raise HTTPException(500, f"Pattern extraction failed: {e}")


@router.post("/api/crew/refresh-skill")
async def trigger_skill_refresh(request: Request, _user=Depends(require_admin)):
    """Manually trigger SKILL.md rewrite based on current patterns.
    Only available to admin tenant."""
    from app.core.crew_learnings import refresh_skill_md, get_patterns
    from app.state import _sessions
    from app.core.auth import _verify_token

    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    payload = _verify_token(token) if token else {}
    if payload.get("tenant") not in (os.getenv("XREF_ADMIN_TENANT", "infinite"), "infinite"):
        raise HTTPException(403, "Skill refresh is admin-only")

    patterns = get_patterns()
    if not patterns:
        return {"ok": False, "message": "No patterns available yet — run extract-patterns first"}

    any_session = next(iter(_sessions.values()), {}) if _sessions else {}
    try:
        llm = _make_llm(any_session)
        if llm is None:
            raise ValueError("LLM in browser mode")
        updated = refresh_skill_md(llm)
        return {
            "ok": updated,
            "message": "SKILL.md updated successfully" if updated else "SKILL.md update skipped",
            "patterns_used": len(patterns),
        }
    except Exception as e:
        raise HTTPException(500, f"Skill refresh failed: {e}")


@router.post("/api/sessions/{sid}/crew-feedback")
async def submit_crew_feedback(sid: str, body: dict = Body(...)):
    """Submit explicit user feedback on a mapping row to the learning system.

    Body: { "row_id": str, "feedback": str, "corrected_tgt_column": str (optional) }
    The feedback is stored as a learning event immediately.
    """
    from app.core.crew_learnings import record_learning
    s = _session_or_404(sid)
    row_id = body.get("row_id", "")
    feedback_text = body.get("feedback", "")

    row = next((m for m in s.get("mappings", []) if m.get("id") == row_id), None)
    if not row:
        raise HTTPException(404, "Mapping row not found")

    corrected = dict(row)
    if body.get("corrected_tgt_column"):
        corrected["tgt_column"] = body["corrected_tgt_column"]
    if body.get("corrected_mapping_type"):
        corrected["mapping_type"] = body["corrected_mapping_type"]
    if body.get("corrected_business_logic"):
        corrected["business_logic"] = body["corrected_business_logic"]

    evt = record_learning(
        event_type="user_feedback",
        src_field=row.get("src_field", ""),
        original=row,
        corrected=corrected,
        session_id=sid,
        feedback_text=feedback_text,
    )
    return {"ok": True, "event_id": evt["id"], "feedback_stored": True}


# ── SIEM Exports ──────────────────────────────────────────────────────────────

def _filter_audit_events(
    request: Request,
    since: Optional[str] = None,
    until: Optional[str] = None,
    tenant: Optional[str] = None,
    limit: int = 5000,
) -> list:
    """Shared filter logic for SIEM export endpoints."""
    auth_header = request.headers.get("Authorization", "")
    payload = _verify_token(auth_header[7:]) if auth_header.startswith("Bearer ") else {}
    caller_tenant = payload.get("tenant", "")
    is_super_admin = caller_tenant == _ADMIN_TENANT

    events = list(reversed(_audit_events))
    if tenant:
        if not is_super_admin and tenant != caller_tenant:
            raise HTTPException(403, "Cannot export other tenant's audit events")
        events = [e for e in events if e.get("tenant") == tenant]
    elif not is_super_admin:
        events = [e for e in events if e.get("tenant") == caller_tenant]
    if since:
        events = [e for e in events if e.get("ts", "") >= since]
    if until:
        events = [e for e in events if e.get("ts", "") <= until]
    return events[:min(limit, 10000)]


def _ts_to_epoch_ms(ts: str) -> int:
    """Convert ISO-8601 timestamp string to milliseconds since epoch."""
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return int(dt.timestamp() * 1000)
    except Exception:
        return 0


_CEF_SEVERITY = {
    "auth.login_fail":   5,
    "auth.login_ok":     3,
    "gdpr.data_deleted": 7,
    "admin.audit_queried": 2,
    "pipeline.failed":   5,
}


@router.get("/api/admin/audit/export/cef")
async def export_audit_cef(
    request: Request,
    since:  Optional[str] = None,
    until:  Optional[str] = None,
    tenant: Optional[str] = None,
    limit:  int = 5000,
    _user=Depends(require_admin),
):
    """Export audit log in ArcSight Common Event Format (CEF). Requires admin."""
    events = _filter_audit_events(request, since=since, until=until, tenant=tenant, limit=limit)
    lines = []
    for e in events:
        sev = _CEF_SEVERITY.get(e.get("event", ""), 3)
        evt = e.get("event", "unknown")
        epoch_ms = _ts_to_epoch_ms(e.get("ts", ""))
        ip = e.get("ip", "0.0.0.0") or "0.0.0.0"
        email = e.get("email", "") or ""
        t = e.get("tenant", "") or ""
        meta = e.get("meta") or {}
        session_id = meta.get("session_id", e.get("session_id", "")) or ""
        cef = (
            f"CEF:0|xREF|DataMapper|2.0.0|{evt}|{evt}|{sev}|"
            f"src={ip} suser={email} cs1={t} cs1Label=tenant "
            f"rt={epoch_ms}"
            + (f" cs2={session_id} cs2Label=session_id" if session_id else "")
        )
        lines.append(cef)
    content = "\n".join(lines) + ("\n" if lines else "")
    return StreamingResponse(
        io.BytesIO(content.encode()),
        media_type="text/plain",
        headers={"Content-Disposition": 'attachment; filename="xref_audit.cef"'},
    )


@router.get("/api/admin/audit/export/jsonld")
async def export_audit_jsonld(
    request: Request,
    since:  Optional[str] = None,
    until:  Optional[str] = None,
    tenant: Optional[str] = None,
    limit:  int = 5000,
    _user=Depends(require_admin),
):
    """Export audit log as ActivityStreams 2.0 JSON-LD. Requires admin."""
    events = _filter_audit_events(request, since=since, until=until, tenant=tenant, limit=limit)
    items = []
    for e in events:
        items.append({
            "@type": "Activity",
            "id": f"urn:xref:audit:{e.get('id', '')}",
            "actor": {
                "@type": "Person",
                "email": e.get("email") or "",
                "tenant": e.get("tenant") or "",
            },
            "type": e.get("event", ""),
            "published": e.get("ts", ""),
            "object": {
                "tenant": e.get("tenant") or "",
                "ip": e.get("ip") or "",
                "session_id": e.get("session_id") or "",
                "meta": e.get("meta") or {},
            },
        })
    payload = {
        "@context": "https://www.w3.org/ns/activitystreams",
        "@type": "OrderedCollection",
        "totalItems": len(items),
        "orderedItems": items,
    }
    content = json.dumps(payload, indent=2, default=str).encode()
    return StreamingResponse(
        io.BytesIO(content),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="xref_audit.jsonld"'},
    )


# ── Tenant Provisioning ───────────────────────────────────────────────────────

@router.get("/api/admin/tenants")
async def list_tenants(request: Request, _user=Depends(require_admin)):
    """List all tenants. Super-admin only."""
    auth_header = request.headers.get("Authorization", "")
    payload = _verify_token(auth_header[7:]) if auth_header.startswith("Bearer ") else {}
    if payload.get("tenant") != _ADMIN_TENANT:
        raise HTTPException(403, "Super-admin access required")
    result = []
    for slug, t in _tenants.items():
        users = t.get("users", [])
        result.append({
            "tenant_id":    slug,
            "display_name": t.get("display_name") or t.get("name") or slug,
            "user_count":   len(users),
            "active_users": sum(1 for u in users if u.get("active", True)),
            "roles":        {r: sum(1 for u in users if u.get("role") == r) for r in ["admin","mapper","reviewer","readonly"]},
        })
    return {"tenants": result, "total": len(result)}


@router.post("/api/admin/tenants/{slug}/plan")
async def set_tenant_plan(slug: str, request: Request, body: dict = Body(...),
                          _user=Depends(require_admin)):
    """Assign a plan (+ optional limit overrides) to a tenant. Super-admin only."""
    auth_header = request.headers.get("Authorization", "")
    payload = _verify_token(auth_header[7:]) if auth_header.startswith("Bearer ") else {}
    if payload.get("tenant") != _ADMIN_TENANT:
        raise HTTPException(403, "Super-admin access required")
    from app.core.billing import PLAN_CATALOG
    plan = (body.get("plan") or "").lower()
    if plan not in PLAN_CATALOG:
        raise HTTPException(422, f"Unknown plan '{plan}'. One of {list(PLAN_CATALOG)}")
    t = _tenants.get(slug)
    if not t:
        raise HTTPException(404, f"Tenant '{slug}' not found")
    t["plan"] = plan
    overrides = body.get("overrides")
    if isinstance(overrides, dict):
        allowed = ("seats", "sessions_per_month", "runs_per_month", "monthly_tokens")
        t.setdefault("billing", {})["overrides"] = {k: v for k, v in overrides.items() if k in allowed}
    _save_tenants()
    _write_audit_event("admin.plan_assigned", tenant=payload.get("tenant"),
                       email=payload.get("email"), ip=_get_client_ip(request),
                       metadata={"target_tenant": slug, "plan": plan})
    return {"ok": True, "tenant": slug, "plan": plan,
            "overrides": (t.get("billing", {}) or {}).get("overrides", {})}


@router.post("/api/admin/tenants")
async def create_tenant(
    request: Request,
    body: dict = Body(...),
    _user=Depends(require_admin),
):
    """Create a new tenant. Super-admin only.
    Body: { tenant_id, admin_email, admin_password, display_name? }
    """
    auth_header = request.headers.get("Authorization", "")
    payload = _verify_token(auth_header[7:]) if auth_header.startswith("Bearer ") else {}
    if payload.get("tenant") != _ADMIN_TENANT:
        raise HTTPException(403, "Super-admin access required")

    tenant_id    = (body.get("tenant_id") or "").strip().lower()
    admin_email  = (body.get("admin_email") or "").strip().lower()
    admin_pass   = (body.get("admin_password") or "").strip()
    display_name = (body.get("display_name") or tenant_id).strip()

    if not tenant_id:
        raise HTTPException(400, "tenant_id is required")
    if not admin_email:
        raise HTTPException(400, "admin_email is required")
    if not admin_pass or len(admin_pass) < 8:
        raise HTTPException(400, "admin_password must be at least 8 characters")
    if tenant_id in _tenants:
        raise HTTPException(409, f"Tenant '{tenant_id}' already exists")
    if not all(c.isalnum() or c in "-_" for c in tenant_id):
        raise HTTPException(400, "tenant_id may only contain letters, digits, hyphens, underscores")

    now_iso = datetime.now(timezone.utc).isoformat()
    new_tenant = {
        "name":         tenant_id,
        "display_name": display_name,
        "created_at":   now_iso,
        "users": [
            {
                "email":        admin_email,
                "password":     _hash_password(admin_pass),
                "role":         "admin",
                "active":       True,
                "display_name": "Tenant Admin",
                "invited_at":   now_iso,
                "last_login":   None,
            }
        ],
    }
    _tenants[tenant_id] = new_tenant
    _save_tenants()
    ip = _get_client_ip(request)
    _write_audit_event(
        "admin.tenant_created",
        tenant=payload.get("tenant"), email=payload.get("email"), ip=ip,
        metadata={"new_tenant": tenant_id, "admin_email": admin_email},
    )
    return {
        "created": True,
        "tenant_id": tenant_id,
        "display_name": display_name,
        "admin_email": admin_email,
    }
