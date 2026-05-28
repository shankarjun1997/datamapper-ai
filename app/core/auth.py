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

from app.config import _AUTH_SECRET, _AUTH_TOKEN_TTL
from app.state import _TENANTS


def _hash_password(password: str) -> str:
    """Return a hex SHA-256 digest of the password (deterministic, no salt).
    For production, replace with bcrypt — this is intentionally simple for
    the MVP where secrets are managed externally."""
    return hashlib.sha256(password.encode()).hexdigest()


def _sign_token(payload: dict) -> str:
    """Create a simple HMAC-signed token: base64(json) + '.' + hex_sig."""
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).decode()
    sig = hmac.new(_AUTH_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


def _verify_token(token: str) -> Optional[dict]:
    """Verify and decode a signed token. Returns payload or None."""
    try:
        body, sig = token.rsplit(".", 1)
        expected = hmac.new(_AUTH_SECRET.encode(), body.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(sig, expected):
            return None
        payload = json.loads(base64.urlsafe_b64decode(body + "=="))
        if payload.get("exp", 0) < time.time():
            return None
        return payload
    except Exception:
        return None


def _get_tenant_from_request(request: Request) -> Optional[str]:
    """Extract the tenant slug from the Authorization bearer token.
    Returns None if no valid token (caller decides whether to enforce)."""
    auth_header = request.headers.get("Authorization", "")
    token = ""
    if auth_header.startswith("Bearer "):
        token = auth_header[7:]
    if not token:
        token = request.query_params.get("token", "")
    if not token:
        return None
    payload = _verify_token(token)
    return payload.get("tenant") if payload else None


def _get_auth_info_from_request(request: Request) -> Optional[dict]:
    """Like _get_tenant_from_request but returns the whole {tenant, email, role}
    triple from the JWT payload. Returns None if missing/invalid token."""
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
    return {
        "tenant": payload.get("tenant"),
        "email":  payload.get("email"),
        "role":   payload.get("role", "readonly"),
        "plan":   payload.get("plan", "standard"),
    }
