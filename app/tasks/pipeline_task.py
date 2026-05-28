"""
app/tasks/pipeline_task.py — Celery task wrapping the mapping pipeline.

The synchronous FastAPI endpoint at ``POST /api/sessions/{sid}/run`` schedules
``_run_pipeline`` directly on the event loop. This task is the async-queue
equivalent: it runs the same coroutine inside a private event loop owned by a
Celery worker process, so long-running jobs do not block the web tier.

Progress is reported back to clients in two channels:

  1. ``self.update_state(state="PROGRESS", meta={...})`` — visible via the
     Celery result backend (Redis) and surfaced by ``GET /api/jobs/{job_id}``.
  2. The existing SSE queue ``_sse_queues[session_id]`` — the session's
     ``/api/sessions/{sid}/events`` stream continues to receive stage / progress
     / status events exactly as it did under the sync path.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional

from celery import states
from celery.exceptions import Ignore, SoftTimeLimitExceeded

from app.worker import celery_app

logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _ensure_event_loop() -> asyncio.AbstractEventLoop:
    """Return a usable event loop for the current Celery worker process."""
    try:
        loop = asyncio.get_event_loop()
        if loop.is_closed():
            raise RuntimeError("loop closed")
        return loop
    except RuntimeError:
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        return loop


def _progress_hook(task, session_id: str):
    """Build a coroutine that mirrors session-level events into Celery state.

    We attach a lightweight subscriber to the session's SSE queue so every
    ``stage`` / ``progress`` / ``status`` event the pipeline emits also
    updates the Celery task meta. That way clients polling
    ``GET /api/jobs/{job_id}`` see live progress without having to also wire
    up SSE.
    """
    from app.state import _sse_queues

    async def _run():
        q: Optional[asyncio.Queue] = _sse_queues.get(session_id)
        if q is None:
            return
        last_step = ""
        last_pct = 0
        # We can't actually drain the queue here without stealing events from
        # the real SSE consumer. Instead we poll the session dict — every
        # _emit() also appends to session["log"].
        from app.state import _sessions
        while True:
            await asyncio.sleep(0.5)
            sess = _sessions.get(session_id)
            if not sess:
                return
            stage = sess.get("stage", "")
            stats = sess.get("stats", {}) or {}
            total = max(int(stats.get("total", 0) or 0), 1)
            mapped = int(stats.get("mapped", 0) or 0)
            # Coarse percentage: L1=10, L2=25, L3 ramps 25->85 by mapped/total,
            # gate2=90, L4=95, done=100.
            if stage == "L1":
                pct = 10
            elif stage == "L2":
                pct = 25
            elif stage == "L3":
                pct = 25 + int(60 * min(mapped / total, 1.0))
            elif stage == "gate2":
                pct = 90
            elif stage == "L4":
                pct = 95
            elif stage == "done":
                pct = 100
            else:
                pct = last_pct
            step = stage or "starting"
            if step != last_step or pct != last_pct:
                try:
                    task.update_state(
                        state="PROGRESS",
                        meta={"step": step, "pct": pct,
                              "mapped": mapped, "total": stats.get("total", 0)},
                    )
                except Exception:
                    pass
                last_step, last_pct = step, pct
            if sess.get("status") in ("review", "done", "error") and not sess.get("running"):
                # Pipeline finished from the coordinator's perspective.
                return

    return _run()


async def _run_pipeline_async(task, session_id: str):
    """Run the same pipeline coroutine the sync endpoint uses, plus a progress mirror."""
    from app.core.pipeline import _run_pipeline
    from app.state import _sse_queues

    # The pipeline emits to _sse_queues[session_id]; make sure it exists.
    if session_id not in _sse_queues:
        _sse_queues[session_id] = asyncio.Queue()

    # Kick off the progress mirror alongside the real pipeline.
    progress_task = asyncio.create_task(_progress_hook(task, session_id))
    try:
        await _run_pipeline(session_id)
    finally:
        progress_task.cancel()
        try:
            await progress_task
        except (asyncio.CancelledError, Exception):
            pass


# ─────────────────────────────────────────────────────────────────────────────
# Celery task
# ─────────────────────────────────────────────────────────────────────────────

@celery_app.task(
    bind=True,
    name="app.tasks.pipeline_task.run_pipeline_task",
    autoretry_for=(Exception,),
    retry_kwargs={"max_retries": 2},
    retry_backoff=True,
    retry_backoff_max=30,
    retry_jitter=False,
)
def run_pipeline_task(self, session_id: str, tenant: str) -> Dict[str, Any]:
    """Execute the mapping pipeline for ``session_id`` inside a Celery worker.

    Args:
        session_id: UUID of the session whose pipeline should run.
        tenant:     Tenant slug (currently informational; used for logging).

    Returns:
        A small status dict consumed by ``GET /api/jobs/{job_id}``.
    """
    started_ts = time.time()
    logger.info("run_pipeline_task starting", extra={
        "session_id": session_id, "tenant": tenant, "task_id": self.request.id,
    })

    # Lazy-import to avoid loading the entire FastAPI app at worker import time
    # — the worker still has to be able to start even if some web-only modules
    # raise at import (e.g. missing optional creds).
    from app.state import _sessions, _save_sessions

    session = _sessions.get(session_id)
    if session is None:
        # Try loading sessions from disk/db — the worker process may have a
        # cold in-memory state.
        try:
            from app.state import _load_sessions
            _load_sessions()
            session = _sessions.get(session_id)
        except Exception as e:
            logger.warning("Could not refresh sessions in worker: %s", e)

    if session is None:
        msg = f"Session {session_id!r} not found in worker process"
        logger.error(msg)
        self.update_state(state=states.FAILURE, meta={"step": "missing_session",
                                                      "pct": 0, "error": msg})
        raise Ignore()

    # Reset session for a fresh run — mirrors the sync endpoint's preamble.
    session["status"] = "running"
    session["running"] = True
    session["error"] = None
    session["mappings"] = []
    session["stats"] = {}
    session["celery_task_id"] = self.request.id
    session["celery_tenant"] = tenant
    _save_sessions()

    self.update_state(state="PROGRESS",
                      meta={"step": "starting", "pct": 0,
                            "mapped": 0, "total": 0})

    loop = _ensure_event_loop()
    try:
        loop.run_until_complete(_run_pipeline_async(self, session_id))
    except SoftTimeLimitExceeded:
        logger.warning("Soft time limit hit for session %s — shutting down gracefully", session_id)
        session["status"] = "error"
        session["error"] = "Pipeline exceeded soft time limit (540s)"
        session["running"] = False
        try:
            _save_sessions()
        except Exception:
            pass
        self.update_state(state=states.FAILURE,
                          meta={"step": "soft_timeout",
                                "pct": 0, "error": "soft_time_limit_exceeded"})
        raise Ignore()
    except Exception as e:
        logger.exception("Pipeline task failed for session %s: %s", session_id, e)
        session["status"] = "error"
        session["error"] = str(e)
        session["running"] = False
        try:
            _save_sessions()
        except Exception:
            pass
        # Re-raise so Celery autoretry kicks in (max 2 retries, exponential backoff).
        raise

    duration_s = round(time.time() - started_ts, 1)
    result = {
        "session_id": session_id,
        "tenant": tenant,
        "status": session.get("status", "unknown"),
        "stats": session.get("stats", {}),
        "duration_s": duration_s,
    }
    try:
        _save_sessions()
    except Exception:
        pass
    logger.info("run_pipeline_task complete", extra={
        "session_id": session_id, "duration_s": duration_s,
        "status": result["status"],
    })
    return result
