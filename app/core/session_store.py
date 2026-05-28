"""
app/core/session_store.py — session load/save/_session_or_404/_now
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Dict

from fastapi import HTTPException, Request

from app.state import _sessions


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SESSION_ID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def _session_or_404(session_id: str) -> Dict:
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "Invalid session ID format")
    if session_id not in _sessions:
        raise HTTPException(404, f"Session {session_id!r} not found")
    return _sessions[session_id]
