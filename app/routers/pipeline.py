"""
app/routers/pipeline.py — /api/sessions/{sid}/run + approve-gate2 + events SSE
"""
from __future__ import annotations

import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from typing import AsyncIterator, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.core.audit import _now, _write_audit_event
from app.core.job_store import (
    cancel_job,
    create_job,
    find_active_job_for_session,
    get_job,
    list_jobs,
    set_task_id,
    update_job,
)
from app.core.mapping_memory import _absorb_approved_mappings
from app.core.rbac import require_mapper, require_reviewer
from app.core.session_store import _session_or_404
from app.core.pipeline import _run_pipeline, _run_sql_generation
from app.core.webhooks import fire_webhook
from app.intelligence.confidence import _strip_vendor
from app.routers._helpers import _check_rate_limit, _get_client_ip
from app.state import _TENANTS, _audit_events, _audit_log, _sessions, _sse_queues, _save_sessions
from app.config import _ADMIN_TENANT
from app.core import billing as _billing


def _enforce_run_quota(s: dict) -> None:
    """Raise 402 if the session's tenant has hit its monthly run/token limit."""
    tenant = s.get("tenant") or "default"
    tobj = _TENANTS.get(tenant) or {"slug": tenant}
    tobj.setdefault("slug", tenant)
    q = _billing.check_quota(tobj, "run_pipeline", _sessions, _audit_events, admin_tenant=_ADMIN_TENANT)
    if not q.get("allowed", True):
        raise HTTPException(402, q.get("message", "Plan run limit reached — upgrade to continue."))

router = APIRouter()


def _snapshot_mappings(session: dict, label: str = "run") -> None:
    """Append a frozen copy of current mappings to session['mapping_versions']."""
    import copy
    versions = session.setdefault("mapping_versions", [])
    versions.append({
        "version_id": str(uuid.uuid4())[:8],
        "ts": _now(),
        "label": label,  # "run", "gate2_approved", "manual_edit", "gate2_partial"
        "count": len(session.get("mappings", [])),
        "snapshot": copy.deepcopy(session.get("mappings", [])),
    })
    # Keep only last 10 versions to prevent unbounded growth
    session["mapping_versions"] = versions[-10:]
    _save_sessions()


def _reconcile_mappings(mappings: List[Dict]) -> Dict:
    """Run a consistency audit over a completed mapping set."""
    issues: List[str] = []

    tgt_to_srcs: Dict[str, List[str]] = {}
    for m in mappings:
        if m.get("status") == "unmapped" or not m.get("tgt_column"):
            continue
        key = f"{m.get('tgt_table','')}.{m.get('tgt_column','')}"
        tgt_to_srcs.setdefault(key, []).append(m.get("src_field", ""))
    duplicate_targets = {k: v for k, v in tgt_to_srcs.items() if len(v) > 1}
    for tgt, srcs in duplicate_targets.items():
        issues.append(f"DUPLICATE TARGET: {tgt} ← {', '.join(srcs)} (review M:1 vs conflict)")

    _MANDATORY_PATTERNS = re.compile(
        r'(customer_id|account_id|device_id|billing_dt|snapshot_dt|period_dt'
        r'|created_at|updated_at|etl_load_timestamp|migration_batch_id)',
        re.IGNORECASE,
    )
    unmapped_mandatory = []
    for m in mappings:
        if m.get("status") == "unmapped" and _MANDATORY_PATTERNS.search(m.get("src_field", "")):
            unmapped_mandatory.append(m.get("src_field", ""))
    for f in unmapped_mandatory:
        issues.append(f"UNMAPPED MANDATORY FIELD: {f} — verify intentionally excluded")

    _FK_FIELDS = re.compile(
        r'(customer_id|account_id|device_id|service_id|order_id|ticket_id)',
        re.IGNORECASE,
    )
    fk_targets: Dict[str, set] = {}
    for m in mappings:
        if not m.get("tgt_column") or m.get("status") == "unmapped":
            continue
        sf = m.get("src_field", "")
        if _FK_FIELDS.search(sf):
            bare = _strip_vendor(sf).lower()
            fk_targets.setdefault(bare, set()).add(
                f"{m.get('tgt_table','')}.{m.get('tgt_column','')}"
            )
    fk_inconsistencies = {k: sorted(v) for k, v in fk_targets.items() if len(v) > 1}
    for fk, targets in fk_inconsistencies.items():
        issues.append(
            f"FK INCONSISTENCY: '{fk}' maps to multiple targets: {', '.join(targets)} "
            f"— confirm fan-out is intentional"
        )

    non_unused = [m for m in mappings if m.get("mapping_type", "").lower() != "unused"]
    mapped_count = sum(1 for m in non_unused if m.get("tgt_column"))
    coverage_pct = round(mapped_count / max(len(non_unused), 1) * 100, 1)
    if coverage_pct < 80:
        issues.append(f"LOW COVERAGE: only {coverage_pct}% of source columns have a target assignment")

    shaky = [
        m for m in mappings
        if m.get("status") == "mapped" and float(m.get("confidence", 1.0)) < 0.6
    ]
    for m in shaky[:10]:
        issues.append(
            f"LOW-CONFIDENCE MAPPED: {m.get('src_field')} → "
            f"{m.get('tgt_table')}.{m.get('tgt_column')} "
            f"(conf={m.get('confidence', 0):.2f}) — recommend manual review"
        )

    return {
        "passed":               len(issues) == 0,
        "issue_count":          len(issues),
        "coverage_pct":         coverage_pct,
        "duplicate_targets":    duplicate_targets,
        "unmapped_mandatory":   unmapped_mandatory,
        "fk_inconsistencies":   fk_inconsistencies,
        "low_confidence_count": len(shaky),
        "issues":               issues,
        "timestamp":            datetime.now(timezone.utc).isoformat(),
    }


@router.post("/api/sessions/{sid}/run")
async def run_pipeline(sid: str, request: Request, _user=Depends(require_mapper)):
    if not _check_rate_limit(_get_client_ip(request)):
        raise HTTPException(429, "Too many requests — slow down")
    s = _session_or_404(sid)
    if s.get("running"):
        raise HTTPException(409, "Pipeline already running")
    if not s.get("schema_data"):
        raise HTTPException(422, "Upload a schema file first")
    _enforce_run_quota(s)
    s["status"]  = "running"
    s["running"] = True
    s["error"]   = None
    s["mappings"] = []
    s["stats"]   = {}
    _sse_queues[sid] = asyncio.Queue()
    asyncio.create_task(_run_pipeline(sid))
    return {"ok": True, "msg": "Pipeline started"}


# ─────────────────────────────────────────────────────────────────────────────
# Async (Celery) pipeline endpoints
# ─────────────────────────────────────────────────────────────────────────────

def _tenant_of(user, session: dict) -> str:
    """Best-effort tenant resolution from auth user or session payload."""
    if isinstance(user, dict):
        t = user.get("tenant") or user.get("tenant_slug")
        if t:
            return str(t)
    t = session.get("tenant") or session.get("tenant_slug") or ""
    return str(t or "")


@router.post("/api/sessions/{sid}/run-async")
async def run_pipeline_async(sid: str, request: Request, user=Depends(require_mapper)):
    """Dispatch the pipeline to a Celery worker. Returns immediately."""
    if not _check_rate_limit(_get_client_ip(request)):
        raise HTTPException(429, "Too many requests — slow down")
    s = _session_or_404(sid)
    if s.get("running"):
        raise HTTPException(409, "Pipeline already running")
    if not s.get("schema_data"):
        raise HTTPException(422, "Upload a schema file first")
    _enforce_run_quota(s)

    tenant = _tenant_of(user, s)

    # Create the queue *before* enqueueing so the SSE stream is ready when the
    # worker starts emitting.
    s["status"]   = "queued"
    s["running"]  = True
    s["error"]    = None
    s["mappings"] = []
    s["stats"]    = {}
    _sse_queues[sid] = asyncio.Queue()
    _save_sessions()

    job_id = create_job(session_id=sid, tenant=tenant)

    # Lazy-import the Celery task so importing this router does not require a
    # live broker connection (handy for tests that don't run Redis).
    from app.tasks.pipeline_task import run_pipeline_task

    try:
        async_result = run_pipeline_task.apply_async(
            args=[sid, tenant],
            task_id=job_id,  # use our job_id as the celery task id for 1:1 mapping
        )
    except Exception as e:
        update_job(job_id, status="failed", error=str(e), finished=True,
                   progress={"step": "dispatch_failed", "pct": 0})
        s["status"]  = "error"
        s["running"] = False
        s["error"]   = f"Could not enqueue job: {e}"
        _save_sessions()
        raise HTTPException(503, f"Could not enqueue job: {e}")

    set_task_id(job_id, async_result.id)
    s["celery_job_id"] = job_id
    s["celery_task_id"] = async_result.id
    _save_sessions()

    update_job(job_id, status="queued",
               progress={"step": "queued", "pct": 0})

    return {
        "ok":         True,
        "job_id":     job_id,
        "task_id":    async_result.id,
        "session_id": sid,
        "status":     "queued",
    }


@router.get("/api/jobs/{job_id}")
async def get_job_status(job_id: str, _user=Depends(require_mapper)):
    """Return the current job state, merged with live Celery progress."""
    rec = get_job(job_id)
    if not rec:
        raise HTTPException(404, "Job not found")
    return rec


@router.delete("/api/jobs/{job_id}")
async def cancel_job_endpoint(job_id: str, _user=Depends(require_mapper)):
    """Revoke the underlying Celery task and mark the job cancelled."""
    rec = get_job(job_id)
    if not rec:
        raise HTTPException(404, "Job not found")
    ok = cancel_job(job_id)
    # Reflect cancellation on the session too.
    sid = rec.get("session_id")
    if sid and sid in _sessions:
        sess = _sessions[sid]
        sess["status"]  = "cancelled"
        sess["running"] = False
        sess["error"]   = "Job cancelled by user"
        _save_sessions()
    return {"ok": True, "revoked": ok, "job_id": job_id}


@router.get("/api/jobs")
async def list_jobs_endpoint(user=Depends(require_mapper), limit: int = 50):
    """List recent jobs for the caller's tenant (or all if no tenant set)."""
    tenant = ""
    if isinstance(user, dict):
        tenant = str(user.get("tenant") or user.get("tenant_slug") or "")
    jobs = list_jobs(tenant=tenant or None, limit=max(1, min(int(limit), 200)))
    return {"jobs": jobs, "count": len(jobs), "tenant": tenant or None}


@router.post("/api/sessions/{sid}/approve-gate2")
async def approve_gate2(sid: str, _user=Depends(require_reviewer)):
    s = _session_or_404(sid)
    if s.get("status") != "review":
        raise HTTPException(409, "Session is not at Gate 2 review stage")

    mappings = s.get("mappings", [])
    stats    = s.get("stats", {})
    user_edits = [m for m in mappings if m.get("modified")]
    audit_rec = {
        "id":           str(uuid.uuid4()),
        "session_id":   sid,
        "session_name": s.get("session_name", ""),
        "timestamp":    _now(),
        "total":        stats.get("total",   len(mappings)),
        "mapped":       stats.get("mapped",  0),
        "review":       sum(1 for m in mappings if m.get("status") == "review"),
        "unmapped":     stats.get("unmapped", 0),
        "avg_confidence": round(stats.get("avg_confidence", 0), 3),
        "user_edits":   len(user_edits),
        "bq_project":   s.get("bq_config", {}).get("project", ""),
        "bq_dataset":   s.get("bq_config", {}).get("dataset", ""),
        "source_file":  s.get("filename", ""),
        "mappings_snapshot": [
            {k: m[k] for k in
             ("src_table","src_field","src_type","tgt_table","tgt_column",
              "tgt_type","mapping_type","confidence","status","business_logic")
             if k in m}
            for m in mappings
        ],
    }
    _audit_log.append(audit_rec)

    absorbed = _absorb_approved_mappings(mappings)

    recon = _reconcile_mappings(mappings)
    s["reconciliation"] = recon
    _save_sessions()

    # Snapshot the approved mapping set so future edits can be diffed against it
    _snapshot_mappings(s, "gate2_approved")

    s["status"]  = "running"
    s["running"] = True
    _sse_queues[sid] = asyncio.Queue()

    _write_audit_event("gate2.approved", tenant=s.get("tenant"),
                       session_id=sid,
                       metadata={
                           "mappings_approved": len(mappings),
                           "user_edits": len(user_edits),
                           "avg_confidence": round(stats.get("avg_confidence", 0), 3),
                       })
    asyncio.create_task(fire_webhook("gate2.approved", s, data={
        "mappings_approved": len(s.get("mappings", [])),
    }))

    asyncio.create_task(_run_sql_generation(sid))
    return {
        "ok": True,
        "msg": "SQL generation started",
        "memory_absorbed": absorbed,
        "reconciliation": recon,
    }


class FieldApprovalBody(BaseModel):
    approved_ids: List[str] = []    # row IDs to approve
    rejected_ids: List[str] = []    # row IDs to reject (status -> "rejected")
    feedback: Optional[str] = None  # optional feedback text stored on session


@router.post("/api/sessions/{sid}/approve-gate2/fields")
async def approve_gate2_fields(sid: str, body: FieldApprovalBody):
    """Field-level Gate 2: approve some, reject others, leave the rest pending."""
    session = _session_or_404(sid)
    mappings = session.get("mappings", [])

    approved_count = 0
    rejected_count = 0
    for m in mappings:
        rid_a = m.get("id") in body.approved_ids or m.get("row_id") in body.approved_ids
        rid_r = m.get("id") in body.rejected_ids or m.get("row_id") in body.rejected_ids
        if rid_a:
            m["gate2_approved"] = True
            m["status"] = m.get("status", "mapped")
            approved_count += 1
        elif rid_r:
            m["gate2_approved"] = False
            m["status"] = "rejected"
            rejected_count += 1

    if body.feedback:
        session["gate2_feedback"] = body.feedback

    session["gate2_partial"]        = True
    session["gate2_approved_count"] = approved_count
    session["gate2_rejected_count"] = rejected_count

    _save_sessions()
    _snapshot_mappings(session, "gate2_partial")

    pending = len([
        m for m in mappings
        if not m.get("gate2_approved") and m.get("status") != "rejected"
    ])
    return {
        "ok": True,
        "approved": approved_count,
        "rejected": rejected_count,
        "pending": pending,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Mapping version history endpoints
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/sessions/{sid}/mapping-versions")
async def list_mapping_versions(sid: str):
    """List all saved versions (metadata only — no full snapshot payload)."""
    session = _session_or_404(sid)
    versions = session.get("mapping_versions", [])
    return [
        {
            "version_id": v["version_id"],
            "ts":         v["ts"],
            "label":      v["label"],
            "count":      v["count"],
        }
        for v in versions
    ]


@router.get("/api/sessions/{sid}/mapping-versions/{version_id}")
async def get_mapping_version(sid: str, version_id: str):
    """Return the full snapshot for a specific version."""
    session = _session_or_404(sid)
    for v in session.get("mapping_versions", []):
        if v["version_id"] == version_id:
            return v
    raise HTTPException(404, "Version not found")


@router.get("/api/sessions/{sid}/mapping-versions/{version_id}/diff")
async def diff_mapping_versions(sid: str, version_id: str, compare_to: str = "current"):
    """Diff a version against current mappings (or another version).
    Returns list of changed rows with old/new values."""
    session = _session_or_404(sid)
    target_version = None
    for v in session.get("mapping_versions", []):
        if v["version_id"] == version_id:
            target_version = v
            break
    if not target_version:
        raise HTTPException(404, "Version not found")

    if compare_to == "current":
        other_mappings = session.get("mappings", [])
    else:
        other_v = next(
            (v for v in session.get("mapping_versions", []) if v["version_id"] == compare_to),
            None,
        )
        if not other_v:
            raise HTTPException(404, "compare_to version not found")
        other_mappings = other_v["snapshot"]

    # Build diff: compare by src_field
    old_by_src = {m.get("src_field", ""): m for m in target_version["snapshot"]}
    new_by_src = {m.get("src_field", ""): m for m in other_mappings}

    diffs = []
    all_fields = set(old_by_src) | set(new_by_src)
    for field in all_fields:
        old = old_by_src.get(field)
        new = new_by_src.get(field)
        if old is None:
            diffs.append({"field": field, "change": "added", "old": None, "new": new})
        elif new is None:
            diffs.append({"field": field, "change": "removed", "old": old, "new": None})
        elif (old.get("tgt_column") != new.get("tgt_column") or
              old.get("tgt_table")  != new.get("tgt_table")  or
              old.get("status")     != new.get("status")):
            diffs.append({
                "field":  field,
                "change": "modified",
                "old": {
                    "tgt_table":  old.get("tgt_table"),
                    "tgt_column": old.get("tgt_column"),
                    "status":     old.get("status"),
                },
                "new": {
                    "tgt_table":  new.get("tgt_table"),
                    "tgt_column": new.get("tgt_column"),
                    "status":     new.get("status"),
                },
            })
    return {
        "version_id":  version_id,
        "compared_to": compare_to,
        "diff_count":  len(diffs),
        "diffs":       diffs,
    }


@router.post("/api/sessions/{sid}/reconcile")
async def reconcile_session(sid: str):
    s = _session_or_404(sid)
    mappings = s.get("mappings", [])
    if not mappings:
        raise HTTPException(400, "No mappings found — run the pipeline first")
    report = _reconcile_mappings(mappings)
    s["reconciliation"] = report
    _save_sessions()
    return report


@router.get("/api/sessions/{sid}/reconcile")
async def get_reconciliation(sid: str):
    s = _session_or_404(sid)
    recon = s.get("reconciliation")
    if not recon:
        raise HTTPException(404, "No reconciliation report yet — call POST /reconcile first")
    return recon


async def _sse_generator(sid: str, session: dict, request: Request) -> AsyncIterator[str]:
    """Shared SSE generator used by /events and /stream.

    In addition to the normal stage/progress/status events emitted by the
    pipeline coroutine, we also poll the in-memory job registry every
    heartbeat. When a Celery job is attached to this session we surface a
    ``job_progress`` event so the UI can show worker-side progress alongside
    the existing pipeline events.
    """
    state_payload = {
        "event": "state",
        "data":  {"status": session.get("status"), "stage": session.get("stage")},
    }
    yield f"data: {json.dumps(state_payload)}\n\n"

    q = _sse_queues.get(sid)
    if not q:
        return

    last_job_pct = -1
    last_job_step = ""

    async def _maybe_emit_job_progress() -> Optional[str]:
        nonlocal last_job_pct, last_job_step
        job = find_active_job_for_session(sid)
        if not job:
            return None
        prog = job.get("progress") or {}
        step = str(prog.get("step", ""))
        pct  = int(prog.get("pct", 0) or 0)
        if step == last_job_step and pct == last_job_pct:
            return None
        last_job_step, last_job_pct = step, pct
        payload = {
            "type":     "job_progress",
            "job_id":   job.get("job_id"),
            "status":   job.get("status"),
            "step":     step,
            "pct":      pct,
        }
        return f"data: {json.dumps({'event':'job_progress','data':payload})}\n\n"

    while True:
        if await request.is_disconnected():
            break
        try:
            msg = await asyncio.wait_for(q.get(), timeout=20.0)
        except asyncio.TimeoutError:
            # Heartbeat tick — emit any pending job_progress, then comment.
            job_msg = await _maybe_emit_job_progress()
            if job_msg:
                yield job_msg
            yield ": heartbeat\n\n"
            continue
        if msg is None:
            # Final job_progress snapshot before closing.
            job_msg = await _maybe_emit_job_progress()
            if job_msg:
                yield job_msg
            yield f"data: {json.dumps({'event':'done','data':{}})}\n\n"
            break
        # Piggy-back a job_progress event whenever something meaningful happens.
        job_msg = await _maybe_emit_job_progress()
        if job_msg:
            yield job_msg
        yield f"data: {json.dumps({'event': msg['event'], 'data': msg['data']})}\n\n"


@router.get("/api/sessions/{sid}/events")
async def sse_stream(sid: str, request: Request):
    s = _session_or_404(sid)
    return StreamingResponse(
        _sse_generator(sid, s, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/api/sessions/{sid}/stream")
async def sse_stream_alias(sid: str, request: Request):
    """Alias of /events kept for the Celery integration spec."""
    s = _session_or_404(sid)
    return StreamingResponse(
        _sse_generator(sid, s, request),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
