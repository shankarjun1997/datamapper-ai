"""
app/core/auth.py — JWT sign/verify + tenant management + _get_tenant_from_request
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Optional

from fastapi import Request

from app.config import _AUTH_SECRET
from app.state import _TENANTS


try:
    import bcrypt  # type: ignore
    _HAS_BCRYPT = True
except Exception:  # pragma: no cover - bcrypt should be installed in prod
    _HAS_BCRYPT = False


def _hash_password(password: str) -> str:
    """Return a salted bcrypt hash of the password.

    Falls back to a salted SHA-256 only if bcrypt is unavailable (e.g. a
    stripped dev image); install bcrypt for production."""
    if _HAS_BCRYPT:
        return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    return "sha256$" + hashlib.sha256(password.encode()).hexdigest()


def _looks_hashed(stored: str) -> bool:
    """True if ``stored`` is already a bcrypt (or sha256$) hash, not plaintext."""
    return isinstance(stored, str) and (
        stored.startswith(("$2a$", "$2b$", "$2y$", "sha256$"))
    )


def _verify_password(provided: str, stored: str) -> bool:
    """Constant-time-ish password check supporting bcrypt and legacy formats.

    Legacy stores may hold plaintext (early MVP) or bare SHA-256 digests; both
    are accepted so existing accounts keep working, and callers should re-hash
    on the next successful login (see ``_needs_rehash``)."""
    if not stored or provided is None:
        return False
    if stored.startswith(("$2a$", "$2b$", "$2y$")):
        try:
            return bcrypt.checkpw(provided.encode(), stored.encode())
        except Exception:
            return False
    if stored.startswith("sha256$"):
        digest = hashlib.sha256(provided.encode()).hexdigest()
        return hmac.compare_digest("sha256$" + digest, stored)
    # Legacy plaintext, or a bare (unprefixed) sha256 digest.
    if hmac.compare_digest(provided, stored):
        return True
    return hmac.compare_digest(hashlib.sha256(provided.encode()).hexdigest(), stored)


def _needs_rehash(stored: str) -> bool:
    """True if the stored credential is not a modern bcrypt hash and should be
    upgraded after a successful login."""
    return not (isinstance(stored, str) and stored.startswith(("$2a$", "$2b$", "$2y$")))


def _sign_token(payload: dict) -> str:
    """Create a simple HMAC-signed token: base64(json) + '.' + hex_sig."""
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(_AUTH_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _is_token_revoked(payload: dict) -> bool:
    """True if the token should be rejected based on current user state.

    A token is revoked when the user is deactivated, or when it was issued
    before the user's ``tokens_valid_after`` watermark (set on logout-everywhere,
    password change, or deactivation). If the tenant/user can't be resolved we
    do NOT revoke (avoids locking out during transient store states)."""
    tenant = _TENANTS.get(payload.get("tenant") or "")
    if not tenant:
        return False
    email = (payload.get("email") or "").lower()
    for u in tenant.get("users", []) or []:
        if (u.get("email") or "").lower() == email:
            if u.get("active") is False:
                return True
            tva = u.get("tokens_valid_after")
            if tva and float(payload.get("iat", 0) or 0) < float(tva):
                return True
            return False
    return False


def _verify_token(token: str) -> Optional[dict]:
    """Verify and decode a signed token. Returns payload or None.

    Checks signature, expiry, and revocation (deactivation / tokens_valid_after).
    """
    try:
        body, sig = token.rsplit(".", 1)
        expected = hmac.new(_AUTH_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=="))
        if payload.get("exp", 0) < time.time():
            return None
        if _is_token_revoked(payload):
            return None
        return payload
    except Exception:
        return None


_COOKIE_NAME = "xref_token"
_CSRF_COOKIE = "xref_csrf"


def _extract_token(request: Request) -> str:
    """Pull the auth token from (in order): Authorization bearer header, the
    ?token query param, or the httpOnly ``xref_token`` cookie."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return auth_header[7:]
    q = request.query_params.get("token", "")
    if q:
        return q
    return request.cookies.get(_COOKIE_NAME, "")


def _token_is_from_cookie(request: Request) -> bool:
    """True when auth is via cookie only (no bearer header / query token) —
    used to decide whether CSRF protection applies."""
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        return False
    if request.query_params.get("token"):
        return False
    return bool(request.cookies.get(_COOKIE_NAME, ""))


def _get_tenant_from_request(request: Request) -> Optional[str]:
    """Extract the tenant slug from the auth token (header/query/cookie).
    Returns None if no valid token (caller decides whether to enforce)."""
    token = _extract_token(request)
    if not token:
        return None
    payload = _verify_token(token)
    return payload.get("tenant") if payload else None


def _get_auth_info_from_request(request: Request) -> Optional[dict]:
    """Like _get_tenant_from_request but returns the whole {tenant, email, role}
    triple from the JWT payload. Returns None if missing/invalid token."""
    token = _extract_token(request)
    if not token:
        return None
    payload = _verify_token(token)
    if not payload:
        return None
    return {
        "tenant": payload.get("tenant"),
        "email":  payload.get("email"),
        "role":   payload.get("role", "readonly"),
        "plan":   payload.get("plan", "standard"),
    }
