"""
app/core/audit.py — audit event writing/loading/flushing
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.state import _audit_events, _flush_audit_events


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _write_audit_event(
    event: str,
    tenant: Optional[str] = None,
    email: Optional[str] = None,
    session_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
    ip: Optional[str] = None,
) -> Dict[str, Any]:
    """Append a structured audit event to the in-memory list and flush to disk."""
    evt = {
        "id":         str(uuid.uuid4()),
        "ts":         _now(),
        "event":      event,
        "tenant":     tenant or "unknown",
        "email":      email  or "anonymous",
        "session_id": session_id or None,
        "ip":         ip or "unknown",
        "meta":       metadata or {},
    }
    _audit_events.append(evt)
    _flush_audit_events()
    return evt


def _count_by(events: List[Dict], key: str) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    for e in events:
        k = e.get(key, "unknown")
        counts[k] = counts.get(k, 0) + 1
    return dict(sorted(counts.items(), key=lambda x: -x[1]))
