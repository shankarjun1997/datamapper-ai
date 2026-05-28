"""
app/core/job_store.py — in-memory + JSON-file job registry for Celery tasks.

The Celery result backend (Redis) holds task state, but we also keep a thin
local registry so we can:

  * list recent jobs per tenant without round-tripping Redis,
  * survive a worker restart (jobs are persisted to ``runtime/xref_jobs.json``),
  * map a friendly ``job_id`` (= the Celery task id) back to a session.

This module is intentionally dependency-light: just stdlib + threading.Lock so
it can be imported by both the FastAPI app and the Celery worker.
"""
from __future__ import annotations

import json
import logging
import os
import threading
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

_jobs: Dict[str, Dict[str, Any]] = {}
_lock = threading.Lock()
_JOBS_PATH = "runtime/xref_jobs.json"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ensure_runtime_dir() -> None:
    """Create the ``runtime/`` directory if it doesn't already exist."""
    dir_path = os.path.dirname(_JOBS_PATH) or "."
    if dir_path and not os.path.isdir(dir_path):
        try:
            os.makedirs(dir_path, exist_ok=True)
        except Exception as e:
            logger.warning("Could not create runtime dir %s: %s", dir_path, e)


def _save_jobs() -> None:
    """Persist jobs to disk. Best-effort — never raises."""
    _ensure_runtime_dir()
    try:
        with _lock:
            snapshot = list(_jobs.values())
        with open(_JOBS_PATH, "w") as f:
            json.dump(snapshot, f, separators=(",", ":"))
    except Exception as e:
        logger.warning("Could not persist jobs to %s: %s", _JOBS_PATH, e)


def _load_jobs() -> None:
    """Load jobs from disk on startup. Best-effort — never raises."""
    global _jobs
    if not os.path.exists(_JOBS_PATH):
        return
    try:
        with open(_JOBS_PATH) as f:
            data = json.load(f) or []
        with _lock:
            _jobs.clear()
            for rec in data:
                jid = rec.get("job_id")
                if jid:
                    _jobs[jid] = rec
        logger.info("Loaded %d jobs from %s", len(_jobs), _JOBS_PATH)
    except Exception as e:
        logger.warning("Could not load jobs from %s: %s", _JOBS_PATH, e)


def create_job(session_id: str, tenant: str) -> str:
    """Create a new job record and return the generated ``job_id``.

    The ``job_id`` is later overwritten with the Celery task id once the task
    is dispatched (see ``set_task_id``) so callers can use either value
    interchangeably.
    """
    job_id = str(uuid.uuid4())
    now = _now_iso()
    record = {
        "job_id":      job_id,
        "task_id":     None,
        "session_id":  session_id,
        "tenant":      tenant,
        "status":      "queued",
        "progress":    {"step": "queued", "pct": 0},
        "created_at":  now,
        "updated_at":  now,
        "started_at":  None,
        "finished_at": None,
        "error":       None,
        "result":      None,
    }
    with _lock:
        _jobs[job_id] = record
    _save_jobs()
    return job_id


def set_task_id(job_id: str, task_id: str) -> None:
    """Link a job record to the underlying Celery task id."""
    with _lock:
        rec = _jobs.get(job_id)
        if rec is not None:
            rec["task_id"] = task_id
            rec["updated_at"] = _now_iso()
    _save_jobs()


def _celery_state_for(task_id: Optional[str]) -> Dict[str, Any]:
    """Look up live state from Celery's result backend.

    Returns ``{}`` if the task isn't found or Celery isn't importable (e.g.
    during unit tests).
    """
    if not task_id:
        return {}
    try:
        from app.worker import celery_app
        res = celery_app.AsyncResult(task_id)
        info = res.info if isinstance(res.info, dict) else None
        return {
            "celery_state":  res.state,
            "celery_ready":  res.ready(),
            "celery_meta":   info or {},
        }
    except Exception as e:
        logger.debug("celery state lookup failed for %s: %s", task_id, e)
        return {}


def get_job(job_id: str) -> Optional[Dict[str, Any]]:
    """Return the job dict enriched with live Celery state, or ``None``."""
    with _lock:
        rec = _jobs.get(job_id)
        if rec is None:
            return None
        merged = dict(rec)

    live = _celery_state_for(merged.get("task_id"))
    if live:
        merged.update(live)
        # Map the Celery state into our top-level status field for convenience.
        cs = live.get("celery_state")
        if cs == "PROGRESS":
            merged["status"] = "running"
            meta = live.get("celery_meta") or {}
            if meta.get("step") or meta.get("pct") is not None:
                merged["progress"] = {
                    "step": meta.get("step", merged["progress"].get("step", "")),
                    "pct":  int(meta.get("pct", merged["progress"].get("pct", 0))),
                }
        elif cs == "SUCCESS":
            merged["status"] = merged.get("status") or "success"
            merged["progress"] = {"step": "done", "pct": 100}
        elif cs == "FAILURE":
            merged["status"] = "failed"
            err = live.get("celery_meta") or {}
            if isinstance(err, dict) and err.get("error"):
                merged["error"] = err.get("error")
        elif cs == "REVOKED":
            merged["status"] = "cancelled"
        elif cs == "STARTED":
            merged["status"] = "running"
        elif cs == "PENDING":
            # Could mean queued or unknown — keep our internal status.
            pass
    return merged


def list_jobs(tenant: Optional[str] = None, limit: int = 50) -> List[Dict[str, Any]]:
    """List jobs newest-first, optionally filtered to one tenant."""
    with _lock:
        records = list(_jobs.values())
    records.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    if tenant:
        records = [r for r in records if (r.get("tenant") or "") == tenant]
    out = []
    for r in records[:limit]:
        live = _celery_state_for(r.get("task_id"))
        merged = dict(r)
        if live:
            merged.update(live)
            cs = live.get("celery_state")
            if cs == "PROGRESS":
                merged["status"] = "running"
                meta = live.get("celery_meta") or {}
                merged["progress"] = {
                    "step": meta.get("step", merged["progress"].get("step", "")),
                    "pct":  int(meta.get("pct", merged["progress"].get("pct", 0))),
                }
            elif cs == "SUCCESS":
                merged["status"] = merged.get("status") or "success"
            elif cs == "FAILURE":
                merged["status"] = "failed"
            elif cs == "REVOKED":
                merged["status"] = "cancelled"
        out.append(merged)
    return out


def update_job(
    job_id: str,
    *,
    status: Optional[str] = None,
    progress: Optional[Dict[str, Any]] = None,
    error: Optional[str] = None,
    result: Optional[Dict[str, Any]] = None,
    started: bool = False,
    finished: bool = False,
) -> Optional[Dict[str, Any]]:
    """Mutate fields on a job record. Returns the updated dict (or ``None``)."""
    with _lock:
        rec = _jobs.get(job_id)
        if rec is None:
            return None
        if status is not None:
            rec["status"] = status
        if progress is not None:
            rec["progress"] = progress
        if error is not None:
            rec["error"] = error
        if result is not None:
            rec["result"] = result
        if started and not rec.get("started_at"):
            rec["started_at"] = _now_iso()
        if finished:
            rec["finished_at"] = _now_iso()
        rec["updated_at"] = _now_iso()
        snapshot = dict(rec)
    _save_jobs()
    return snapshot


def cancel_job(job_id: str) -> bool:
    """Revoke the underlying Celery task. Returns True if a revoke was sent."""
    with _lock:
        rec = _jobs.get(job_id)
        if rec is None:
            return False
        task_id = rec.get("task_id")
    if not task_id:
        # Mark cancelled locally; nothing to revoke.
        update_job(job_id, status="cancelled", finished=True,
                   progress={"step": "cancelled", "pct": 0})
        return False
    try:
        from app.worker import celery_app
        celery_app.control.revoke(task_id, terminate=True, signal="SIGTERM")
        update_job(job_id, status="cancelled", finished=True,
                   progress={"step": "cancelled", "pct": 0})
        return True
    except Exception as e:
        logger.error("Failed to revoke task %s: %s", task_id, e)
        return False


def find_active_job_for_session(session_id: str) -> Optional[Dict[str, Any]]:
    """Return the most-recent non-terminal job for a session, if any."""
    with _lock:
        candidates = [r for r in _jobs.values() if r.get("session_id") == session_id]
    candidates.sort(key=lambda r: r.get("created_at", ""), reverse=True)
    for rec in candidates:
        enriched = get_job(rec["job_id"]) or rec
        status = (enriched.get("status") or "").lower()
        if status not in ("success", "failed", "cancelled"):
            return enriched
    return None


# Load existing jobs once at import time so the FastAPI process starts with a
# warm registry.
_load_jobs()
