"""
app/core/rbac.py — role-based access control helpers.

Role hierarchy (higher = more privileged):
    admin    (4) — full access, can manage users
    mapper   (3) — create sessions, run pipelines, edit mappings
    reviewer (2) — view sessions, approve Gate 2 (read-mostly)
    readonly (1) — view only

Use as FastAPI dependencies:

    from app.core.rbac import require_admin, require_mapper, require_reviewer

    @router.post("/api/admin/something")
    async def do_thing(_user=Depends(require_admin)):
        ...
"""
from __future__ import annotations

from typing import Optional

from fastapi import HTTPException, Request

from app.core.auth import _verify_token
from app.config import _REQUIRE_AUTH
from app.state import _TENANTS


ROLE_HIERARCHY = {"admin": 4, "mapper": 3, "reviewer": 2, "readonly": 1}

# Guest identity used when XREF_REQUIRE_AUTH=false and no token is sent.
_GUEST_USER = {
    "email":       "guest@local",
    "tenant":      "demo",
    "tenant_name": "Demo Workspace",
    "plan":        "standard",
    "role":        "admin",   # full access in dev so nothing is blocked
    "active":      True,
    "display_name": "Guest (dev)",
}


def get_user_from_request(request: Request) -> Optional[dict]:
    """Extract full user dict {email, role, tenant, tenant_name, plan, ...} from
    the bearer JWT. Returns None if missing/invalid token.

    The dict is built by merging the JWT payload with the up-to-date user
    record from _TENANTS (so role and active status reflect the current state
    even after the token was issued — token role is used as a fallback)."""
    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if not token:
        token = request.query_params.get("token", "")
    if not token:
        return None
    payload = _verify_token(token)
    if not payload:
        return None
    tenant_slug = payload.get("tenant")
    email = (payload.get("email") or "").lower()
    user = {
        "email":       email,
        "tenant":      tenant_slug,
        "tenant_name": payload.get("tenant_name", tenant_slug),
        "plan":        payload.get("plan", "standard"),
        "role":        payload.get("role", "readonly"),
        "exp":         payload.get("exp"),
    }
    tenant = _TENANTS.get(tenant_slug or "")
    if tenant:
        for u in tenant.get("users", []) or []:
            if (u.get("email") or "").lower() == email:
                # current authoritative state from the tenant store
                user["role"]         = u.get("role", user["role"])
                user["active"]       = u.get("active", True)
                user["display_name"] = u.get("display_name", "")
                break
    return user


def require_role(min_role: str):
    """FastAPI dependency factory — raises 401 if no auth, 403 if role too low,
    403 if the user has been deactivated.

    When XREF_REQUIRE_AUTH=false (default in dev), missing tokens fall back to
    _GUEST_USER (admin role) so the app works without logging in."""
    if min_role not in ROLE_HIERARCHY:
        raise ValueError(f"Unknown role: {min_role}")

    async def _check(request: Request):
        user = get_user_from_request(request)
        if not user:
            if not _REQUIRE_AUTH:
                # Dev mode — allow unauthenticated requests as guest admin
                return _GUEST_USER
            raise HTTPException(401, "Authentication required")
        if user.get("active") is False:
            raise HTTPException(403, "User account is deactivated")
        user_role = user.get("role", "readonly")
        if ROLE_HIERARCHY.get(user_role, 0) < ROLE_HIERARCHY[min_role]:
            raise HTTPException(
                403,
                f"Role '{min_role}' or higher required. You have: {user_role}",
            )
        return user

    return _check


# Convenience shortcuts — preinstantiated dependencies
require_admin    = require_role("admin")
require_mapper   = require_role("mapper")
require_reviewer = require_role("reviewer")
require_readonly = require_role("readonly")
