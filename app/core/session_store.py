"""
app/core/session_store.py — session load/save/_session_or_404/_now + tenant access.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Dict, Optional

from fastapi import HTTPException

from app.config import _ADMIN_TENANT
from app.state import _sessions


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_SESSION_ID_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')


def _tenant_can_access(caller_tenant: Optional[str], session: Optional[Dict]) -> bool:
    """Return True if a caller belonging to ``caller_tenant`` may access ``session``.

    Rules:
    - An unauthenticated caller (caller_tenant is None/"") is *not* judged here —
      auth enforcement is the route guards' job; we only block confirmed
      cross-tenant access. This keeps dev/guest mode working.
    - The super-admin tenant (``_ADMIN_TENANT``) may access any session.
    - Otherwise the caller's tenant must match the session's tenant.
    """
    if not caller_tenant:
        return True
    sess_tenant = (session or {}).get("tenant", "default")
    return caller_tenant == sess_tenant or caller_tenant == _ADMIN_TENANT


def _session_or_404(session_id: str, caller_tenant: Optional[str] = None) -> Dict:
    """Fetch a session by id, validating format and existence.

    If ``caller_tenant`` is provided, also enforce tenant isolation: a session
    owned by a different tenant is reported as 404 (not 403) so we don't leak
    which session IDs exist across tenant boundaries.
    """
    if not _SESSION_ID_RE.match(session_id):
        raise HTTPException(400, "Invalid session ID format")
    if session_id not in _sessions:
        raise HTTPException(404, f"Session {session_id!r} not found")
    s = _sessions[session_id]
    if caller_tenant is not None and not _tenant_can_access(caller_tenant, s):
        # Treat cross-tenant access as if the session does not exist.
        raise HTTPException(404, f"Session {session_id!r} not found")
    return s
