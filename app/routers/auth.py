"""
app/routers/auth.py — /api/auth/* routes (login, logout, me, user management)

Also hosts the OIDC/SSO endpoints (/api/auth/oidc/*) and the GDPR data
deletion endpoint (/api/admin/users/{email}/data).
"""
from __future__ import annotations

import asyncio
import time
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from app.config import _AUTH_TOKEN_TTL, _REQUIRE_AUTH
from app.core.audit import _write_audit_event
from app.core.auth import _get_tenant_from_request, _sign_token, _verify_token
from app.core.oidc import (
    build_authorization_url,
    claims_to_user,
    consume_state,
    exchange_code,
    generate_state,
    get_oidc_config,
    get_userinfo,
)
from app.core.rbac import ROLE_HIERARCHY, get_user_from_request, require_admin
from app.routers._helpers import _get_client_ip
from app.state import _TENANTS, _save_sessions, _save_tenants, _sessions

router = APIRouter()


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def _find_user(tenant: dict, email: str) -> Optional[dict]:
    """Case-insensitive lookup of a user in a tenant's user list."""
    needle = (email or "").strip().lower()
    for u in tenant.get("users", []) or []:
        if (u.get("email") or "").strip().lower() == needle:
            return u
    return None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_user(u: dict) -> dict:
    """Strip secrets from a user dict before sending it to the client."""
    return {
        "email":        u.get("email", ""),
        "role":         u.get("role", "readonly"),
        "active":       u.get("active", True),
        "display_name": u.get("display_name", ""),
        "last_login":   u.get("last_login"),
        "invited_at":   u.get("invited_at"),
    }


# ─────────────────────────────────────────────────────────────────────────────
# Schemas
# ─────────────────────────────────────────────────────────────────────────────

class LoginRequest(BaseModel):
    tenant: str
    email: str
    password: str
    remember: bool = False


class InviteUserBody(BaseModel):
    email: str
    role: str = "readonly"
    display_name: Optional[str] = None
    temporary_password: str


class PatchUserBody(BaseModel):
    role: Optional[str] = None
    active: Optional[bool] = None
    display_name: Optional[str] = None


class ChangePasswordBody(BaseModel):
    current_password: str
    new_password: str


# ─────────────────────────────────────────────────────────────────────────────
# Auth config (lets the frontend know if login is mandatory)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/auth/config")
async def auth_config():
    """Returns whether the server requires authentication.
    XREF_REQUIRE_AUTH=false (default) → login optional, guest access allowed.
    XREF_REQUIRE_AUTH=true  → login mandatory (production mode)."""
    return {
        "require_auth": _REQUIRE_AUTH,
        "demo_tenant":  "demo" if not _REQUIRE_AUTH else None,
        "demo_email":   "demo@xref.ai" if not _REQUIRE_AUTH else None,
    }


# ─────────────────────────────────────────────────────────────────────────────
# Tenant listing
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/auth/tenants")
async def list_tenants():
    """Return public tenant list for the workspace picker."""
    return {"tenants": [
        {"slug": t["slug"], "name": t["name"], "plan": t.get("plan", "standard")}
        for t in _TENANTS.values()
    ]}


@router.get("/api/auth/tenant/{slug}")
async def tenant_info(slug: str):
    """Validate a tenant slug and return its display info."""
    t = _TENANTS.get(slug)
    if not t:
        raise HTTPException(status_code=404, detail="Workspace not found")
    return {"slug": t["slug"], "name": t["name"], "plan": t.get("plan", "standard"), "valid": True}


# ─────────────────────────────────────────────────────────────────────────────
# Login / logout / me
# ─────────────────────────────────────────────────────────────────────────────

@router.post("/api/auth/login")
async def auth_login(request: Request, body: LoginRequest):
    """Authenticate a user against a tenant's users list and return a signed
    session token. The JWT now carries the user's role so downstream RBAC
    dependencies can authorise without a DB hit."""
    tenant_slug = body.tenant.strip().lower()
    ip          = _get_client_ip(request)
    t = _TENANTS.get(tenant_slug)
    if not t:
        _write_audit_event("auth.login_fail", tenant=tenant_slug, email=body.email,
                           ip=ip, metadata={"reason": "workspace_not_found"})
        raise HTTPException(status_code=401, detail="Workspace not found")

    email    = body.email.strip().lower()
    password = body.password

    user = _find_user(t, email)
    if not user or user.get("password") != password:
        _write_audit_event("auth.login_fail", tenant=tenant_slug, email=email,
                           ip=ip, metadata={"reason": "bad_credentials"})
        raise HTTPException(status_code=401, detail="Invalid email or password")

    if user.get("active") is False:
        _write_audit_event("auth.login_fail", tenant=tenant_slug, email=email,
                           ip=ip, metadata={"reason": "deactivated"})
        raise HTTPException(status_code=403, detail="Account deactivated — contact your admin")

    # Stamp the login and persist
    user["last_login"] = _now_iso()
    try:
        _save_tenants()
    except Exception:
        pass

    ttl = _AUTH_TOKEN_TTL * (30 if body.remember else 1)
    payload = {
        "tenant": tenant_slug,
        "tenant_name": t["name"],
        "email": email,
        "role":  user.get("role", "readonly"),
        "plan": t.get("plan", "standard"),
        "exp": time.time() + ttl,
        "iat": time.time(),
    }
    token = _sign_token(payload)

    _write_audit_event(
        "auth.login_ok", tenant=tenant_slug, email=email, ip=ip,
        metadata={"plan": t.get("plan", "standard"), "remember": body.remember,
                  "role": user.get("role", "readonly")},
    )
    return {
        "ok": True,
        "token": token,
        "tenant": tenant_slug,
        "tenant_name": t["name"],
        "email": email,
        "role":  user.get("role", "readonly"),
        "plan": t.get("plan", "standard"),
        "expires_in": ttl,
    }


@router.get("/api/auth/me")
async def auth_me(request: Request):
    """Verify the bearer token and return the current user info."""
    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if not token:
        token = request.query_params.get("token", "")
    payload = _verify_token(token)
    if not payload:
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    # Pull live role from the tenant store in case it changed since issuance
    tenant = _TENANTS.get(payload["tenant"]) or {}
    live_user = _find_user(tenant, payload["email"]) if tenant else None
    role = (live_user or {}).get("role", payload.get("role", "readonly"))
    display_name = (live_user or {}).get("display_name", "")
    return {
        "ok": True,
        "tenant": payload["tenant"],
        "tenant_name": payload.get("tenant_name", payload["tenant"]),
        "email": payload["email"],
        "role":  role,
        "display_name": display_name,
        "plan": payload.get("plan", "standard"),
        "exp": payload["exp"],
    }


@router.post("/api/auth/logout")
async def auth_logout(request: Request):
    """Client-side logout — token is stateless so just confirm."""
    tenant = _get_tenant_from_request(request)
    auth_header = request.headers.get("Authorization", "")
    token = auth_header[7:] if auth_header.startswith("Bearer ") else ""
    email = "unknown"
    if token:
        payload = _verify_token(token)
        if payload:
            email = payload.get("email", "unknown")
    _write_audit_event("auth.logout", tenant=tenant, email=email, ip=_get_client_ip(request))
    return {"ok": True, "message": "Logged out"}


# ─────────────────────────────────────────────────────────────────────────────
# User management (admin-only)
# ─────────────────────────────────────────────────────────────────────────────

@router.get("/api/auth/users")
async def list_users(_user=Depends(require_admin)):
    """List users in the caller's tenant."""
    tenant = _TENANTS.get(_user["tenant"])
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    return {"users": [_public_user(u) for u in tenant.get("users", []) or []]}


@router.post("/api/auth/users/invite")
async def invite_user(body: InviteUserBody, request: Request,
                       _user=Depends(require_admin)):
    """Create a new user in the caller's tenant."""
    tenant = _TENANTS.get(_user["tenant"])
    if not tenant:
        raise HTTPException(404, "Tenant not found")

    email = (body.email or "").strip().lower()
    if not email:
        raise HTTPException(422, "Email is required")
    if body.role not in ROLE_HIERARCHY:
        raise HTTPException(422, f"Invalid role '{body.role}'. Must be one of {list(ROLE_HIERARCHY)}")
    if not body.temporary_password:
        raise HTTPException(422, "temporary_password is required")
    if _find_user(tenant, email):
        raise HTTPException(409, f"User '{email}' already exists in this tenant")

    new_user = {
        "email":        email,
        "password":     body.temporary_password,
        "role":         body.role,
        "active":       True,
        "invited_at":   _now_iso(),
        "last_login":   None,
        "display_name": body.display_name or email.split("@")[0],
    }
    tenant.setdefault("users", []).append(new_user)
    _save_tenants()

    _write_audit_event(
        "auth.user_invited", tenant=_user["tenant"], email=_user["email"],
        ip=_get_client_ip(request),
        metadata={"invited_email": email, "role": body.role},
    )
    return {"ok": True, "user": _public_user(new_user)}


@router.patch("/api/auth/users/{email}")
async def patch_user(email: str, body: PatchUserBody, request: Request,
                      _user=Depends(require_admin)):
    """Update a user's role, active status, or display name.
    Caller cannot downgrade themselves from admin (would lock everyone out)."""
    tenant = _TENANTS.get(_user["tenant"])
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    target = _find_user(tenant, email)
    if not target:
        raise HTTPException(404, f"User '{email}' not found")

    is_self = (target.get("email") or "").lower() == (_user.get("email") or "").lower()

    if body.role is not None:
        if body.role not in ROLE_HIERARCHY:
            raise HTTPException(422, f"Invalid role '{body.role}'")
        if is_self and target.get("role") == "admin" and body.role != "admin":
            raise HTTPException(400, "You cannot downgrade your own admin role")
        target["role"] = body.role

    if body.active is not None:
        if is_self and body.active is False:
            raise HTTPException(400, "You cannot deactivate your own account")
        target["active"] = bool(body.active)

    if body.display_name is not None:
        target["display_name"] = body.display_name

    _save_tenants()
    _write_audit_event(
        "auth.user_updated", tenant=_user["tenant"], email=_user["email"],
        ip=_get_client_ip(request),
        metadata={"target_email": target["email"],
                   "role": body.role, "active": body.active,
                   "display_name": body.display_name},
    )
    return {"ok": True, "user": _public_user(target)}


@router.delete("/api/auth/users/{email}")
async def delete_user(email: str, request: Request, _user=Depends(require_admin)):
    """Remove a user from the caller's tenant. Cannot delete yourself."""
    tenant = _TENANTS.get(_user["tenant"])
    if not tenant:
        raise HTTPException(404, "Tenant not found")

    needle = (email or "").strip().lower()
    if needle == (_user.get("email") or "").lower():
        raise HTTPException(400, "You cannot delete your own account")

    users = tenant.get("users", []) or []
    before = len(users)
    tenant["users"] = [u for u in users if (u.get("email") or "").lower() != needle]
    if len(tenant["users"]) == before:
        raise HTTPException(404, f"User '{email}' not found")

    _save_tenants()
    _write_audit_event(
        "auth.user_deleted", tenant=_user["tenant"], email=_user["email"],
        ip=_get_client_ip(request),
        metadata={"deleted_email": needle},
    )
    return {"ok": True, "deleted": needle, "remaining": len(tenant["users"])}


@router.post("/api/auth/change-password")
async def change_password(body: ChangePasswordBody, request: Request):
    """Self-service password change — open to any authenticated user."""
    user_ctx = get_user_from_request(request)
    if not user_ctx:
        raise HTTPException(401, "Authentication required")

    tenant = _TENANTS.get(user_ctx["tenant"])
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    target = _find_user(tenant, user_ctx["email"])
    if not target:
        raise HTTPException(404, "User record missing — re-login")

    if target.get("password") != body.current_password:
        _write_audit_event(
            "auth.password_change_fail", tenant=user_ctx["tenant"],
            email=user_ctx["email"], ip=_get_client_ip(request),
            metadata={"reason": "bad_current_password"},
        )
        raise HTTPException(401, "Current password is incorrect")
    if not body.new_password or len(body.new_password) < 4:
        raise HTTPException(422, "New password must be at least 4 characters")

    target["password"] = body.new_password
    _save_tenants()
    _write_audit_event(
        "auth.password_changed", tenant=user_ctx["tenant"],
        email=user_ctx["email"], ip=_get_client_ip(request),
    )
    return {"ok": True, "message": "Password changed"}


# ─────────────────────────────────────────────────────────────────────────────
# OIDC / SSO
# ─────────────────────────────────────────────────────────────────────────────

def _resolve_oidc_tenant(claims: dict, tenant_claim: str) -> Optional[dict]:
    """Pick which xREF tenant the SSO user lands in.

    Order:
      1. value of the configured tenant_claim (if it matches a known tenant)
      2. first tenant in the registry (deterministic — sorted by slug)
    Returns the tenant dict or ``None`` if the registry is empty."""
    claim_value = (claims.get(tenant_claim) or "").strip().lower() if tenant_claim else ""
    if claim_value and claim_value in _TENANTS:
        return _TENANTS[claim_value]
    if not _TENANTS:
        return None
    first_slug = sorted(_TENANTS.keys())[0]
    return _TENANTS[first_slug]


@router.get("/api/auth/oidc/config")
async def oidc_config_public():
    """Public — tells the SPA whether to show the 'Sign in with SSO' button."""
    cfg = get_oidc_config()
    if not cfg:
        return {"enabled": False, "provider": None, "login_url": None}
    return {
        "enabled": True,
        "provider": cfg.provider,
        "login_url": "/api/auth/oidc/login",
    }


@router.get("/api/auth/oidc/login")
async def oidc_login(request: Request, redirect_after: str = "/"):
    """Start the SSO flow — return the URL the SPA should redirect the browser to."""
    cfg = get_oidc_config()
    if not cfg:
        raise HTTPException(404, "OIDC not enabled")
    state = generate_state(redirect_after=redirect_after)
    redirect_url = build_authorization_url(cfg, state)
    _write_audit_event(
        "auth.oidc_login_start", tenant="", email="anonymous",
        ip=_get_client_ip(request),
        metadata={"provider": cfg.provider, "redirect_after": redirect_after},
    )
    return {"redirect_url": redirect_url, "state": state}


@router.get("/api/auth/oidc/callback")
async def oidc_callback(request: Request, code: str = "", state: str = "",
                        error: str = "", error_description: str = ""):
    """IdP redirects back here with ?code=...&state=... — exchange the code,
    fetch userinfo, auto-provision the user if needed, and issue an xREF JWT."""
    cfg = get_oidc_config()
    if not cfg:
        raise HTTPException(404, "OIDC not enabled")
    ip = _get_client_ip(request)

    if error:
        _write_audit_event(
            "auth.oidc_login_fail", tenant="", email="anonymous", ip=ip,
            metadata={"reason": "provider_error", "error": error,
                      "description": error_description},
        )
        raise HTTPException(401, f"OIDC error: {error} {error_description}".strip())

    if not code:
        raise HTTPException(400, "Missing authorization code")

    state_entry = consume_state(state)
    if state_entry is None:
        _write_audit_event(
            "auth.oidc_login_fail", tenant="", email="anonymous", ip=ip,
            metadata={"reason": "invalid_state"},
        )
        raise HTTPException(400, "Invalid or expired state token (possible CSRF)")
    redirect_after = state_entry.get("redirect_after", "/")

    # Heavy lifting off the event loop
    try:
        token_bundle = await asyncio.to_thread(exchange_code, cfg, code)
    except RuntimeError as e:
        _write_audit_event(
            "auth.oidc_login_fail", tenant="", email="anonymous", ip=ip,
            metadata={"reason": "token_exchange_failed", "error": str(e)},
        )
        raise HTTPException(401, "OIDC token exchange failed")

    access_token = token_bundle.get("access_token") or ""
    if not access_token:
        _write_audit_event(
            "auth.oidc_login_fail", tenant="", email="anonymous", ip=ip,
            metadata={"reason": "no_access_token"},
        )
        raise HTTPException(401, "OIDC response missing access_token")

    try:
        claims = await asyncio.to_thread(get_userinfo, cfg, access_token)
    except RuntimeError as e:
        _write_audit_event(
            "auth.oidc_login_fail", tenant="", email="anonymous", ip=ip,
            metadata={"reason": "userinfo_failed", "error": str(e)},
        )
        raise HTTPException(401, "OIDC userinfo failed")

    email = (claims.get("email") or "").strip().lower()
    if not email:
        _write_audit_event(
            "auth.oidc_login_fail", tenant="", email="anonymous", ip=ip,
            metadata={"reason": "no_email_claim"},
        )
        raise HTTPException(401, "OIDC provider did not return an email")

    tenant = _resolve_oidc_tenant(claims, cfg.tenant_claim)
    if not tenant:
        _write_audit_event(
            "auth.oidc_login_fail", tenant="", email=email, ip=ip,
            metadata={"reason": "no_matching_tenant"},
        )
        raise HTTPException(403, "No xREF workspace available for this user")
    tenant_slug = tenant["slug"]

    # Auto-provision the user inside the chosen tenant if they don't exist yet
    existing = _find_user(tenant, email)
    provisioned = False
    if not existing:
        new_user = claims_to_user(claims, default_tenant=tenant_slug,
                                  default_role=cfg.default_role)
        new_user["invited_at"] = _now_iso()
        tenant.setdefault("users", []).append(new_user)
        existing = new_user
        provisioned = True
        _write_audit_event(
            "auth.oidc_user_provisioned", tenant=tenant_slug, email=email, ip=ip,
            metadata={"role": new_user["role"], "provider": cfg.provider},
        )

    if existing.get("active") is False:
        _write_audit_event(
            "auth.oidc_login_fail", tenant=tenant_slug, email=email, ip=ip,
            metadata={"reason": "deactivated"},
        )
        raise HTTPException(403, "Account deactivated — contact your admin")

    existing["last_login"] = _now_iso()
    existing["sso"] = True
    try:
        _save_tenants()
    except Exception:
        pass

    ttl = _AUTH_TOKEN_TTL
    payload = {
        "tenant":      tenant_slug,
        "tenant_name": tenant["name"],
        "email":       email,
        "role":        existing.get("role", cfg.default_role),
        "plan":        tenant.get("plan", "standard"),
        "exp":         time.time() + ttl,
        "iat":         time.time(),
        "sso":         True,
    }
    token = _sign_token(payload)

    _write_audit_event(
        "auth.oidc_login_ok", tenant=tenant_slug, email=email, ip=ip,
        metadata={"provider": cfg.provider, "role": existing.get("role"),
                  "provisioned": provisioned},
    )

    accept = (request.headers.get("Accept") or "").lower()
    if "application/json" in accept:
        return {
            "ok":          True,
            "token":       token,
            "tenant":      tenant_slug,
            "tenant_name": tenant["name"],
            "email":       email,
            "role":        existing.get("role", cfg.default_role),
            "plan":        tenant.get("plan", "standard"),
            "expires_in":  ttl,
            "provisioned": provisioned,
            "redirect_after": redirect_after,
        }

    # Browser flow — bounce back into the SPA with the token in a fragment
    return RedirectResponse(
        url=f"/#oidc-success?token={token}&tenant={tenant_slug}",
        status_code=302,
    )


# ─────────────────────────────────────────────────────────────────────────────
# GDPR — data deletion
# ─────────────────────────────────────────────────────────────────────────────

@router.delete("/api/admin/users/{email}/data")
async def gdpr_delete_user_data(email: str, request: Request,
                                _user=Depends(require_admin)):
    """Erase a user from the caller's tenant and purge all of their sessions.

    Admin-scoped: the caller's tenant is the only one whose users are touched.
    This satisfies the GDPR right-to-erasure for the user's xREF account record
    plus any session blackboards they created in this workspace."""
    needle = (email or "").strip().lower()
    if not needle:
        raise HTTPException(422, "Email is required")

    tenant_slug = _user["tenant"]
    tenant = _TENANTS.get(tenant_slug)
    if not tenant:
        raise HTTPException(404, "Tenant not found")

    if needle == (_user.get("email") or "").lower():
        raise HTTPException(400, "You cannot delete your own data via this endpoint")

    users = tenant.get("users", []) or []
    before = len(users)
    tenant["users"] = [u for u in users if (u.get("email") or "").lower() != needle]
    user_removed = len(tenant["users"]) < before

    # Purge any sessions owned by this user. ``user_email`` was added on session
    # creation (see routers/sessions.py); older sessions without it stay intact.
    sessions_deleted = 0
    for sid in list(_sessions.keys()):
        sess = _sessions.get(sid) or {}
        owner = (sess.get("user_email") or "").lower()
        same_tenant = (sess.get("tenant") or "") == tenant_slug
        if owner == needle and same_tenant:
            _sessions.pop(sid, None)
            sessions_deleted += 1

    if user_removed:
        _save_tenants()
    if sessions_deleted:
        _save_sessions()

    _write_audit_event(
        "gdpr.data_deleted",
        tenant=tenant_slug,
        email=_user["email"],
        ip=_get_client_ip(request),
        metadata={
            "target_email":     needle,
            "user_removed":     user_removed,
            "sessions_deleted": sessions_deleted,
        },
    )
    return {
        "deleted":         True,
        "sessions_purged": sessions_deleted,
        "user_removed":    user_removed,
    }
