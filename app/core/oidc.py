"""
app/core/oidc.py — OpenID Connect / SSO client + in-memory state store.

This module is intentionally framework-agnostic: it only knows how to talk to
an OIDC provider (Okta, Azure AD, Google) and translate claims into the xREF
user dict format. The FastAPI routes in ``app/routers/auth.py`` wire it into
the login flow.

Environment variables
---------------------
OIDC_ENABLED         "true" to enable the SSO routes
OIDC_PROVIDER        okta | azure | google
OIDC_CLIENT_ID       OAuth client id
OIDC_CLIENT_SECRET   OAuth client secret
OIDC_ISSUER          full issuer URL (okta + azure). Google can be omitted.
OIDC_TENANT_ID       azure tenant id (only used when issuer not given)
OIDC_REDIRECT_URI    callback URI registered with the IdP
OIDC_SCOPES          space-separated scopes (default: "openid email profile")
OIDC_DEFAULT_ROLE    role granted to auto-provisioned users (default readonly)
OIDC_TENANT_CLAIM    JWT/userinfo claim to use as xREF tenant slug

All HTTP traffic uses ``httpx`` synchronously — callers wrap with
``asyncio.to_thread`` so the FastAPI event loop is never blocked.
"""
from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlencode

import httpx

from app.config import logger

# ─────────────────────────────────────────────────────────────────────────────
# Config
# ─────────────────────────────────────────────────────────────────────────────

_DEFAULT_SCOPES = "openid email profile"
_HTTP_TIMEOUT = 10.0


@dataclass
class OIDCConfig:
    """Resolved OIDC client configuration. Built once from the environment."""
    provider:       str
    client_id:      str
    client_secret:  str
    issuer:         str
    redirect_uri:   str
    scopes:         str = _DEFAULT_SCOPES
    default_role:   str = "readonly"
    tenant_claim:   str = "tenant"
    tenant_id:      str = ""       # azure-only
    endpoints:      dict = field(default_factory=dict)  # discovered

    @property
    def authorization_endpoint(self) -> str:
        return self.endpoints.get("authorization_endpoint", "")

    @property
    def token_endpoint(self) -> str:
        return self.endpoints.get("token_endpoint", "")

    @property
    def userinfo_endpoint(self) -> str:
        return self.endpoints.get("userinfo_endpoint", "")

    @property
    def jwks_uri(self) -> str:
        return self.endpoints.get("jwks_uri", "")


# Cached singleton; rebuilt only if ``get_oidc_config`` is called after env change
_oidc_config: Optional[OIDCConfig] = None
_oidc_load_attempted: bool = False
_oidc_load_error: Optional[str] = None


# ─────────────────────────────────────────────────────────────────────────────
# Discovery
# ─────────────────────────────────────────────────────────────────────────────

def _discover_endpoints(issuer: str) -> dict:
    """Fetch the provider's OIDC discovery document and return the JSON dict.

    Raises ``RuntimeError`` if the discovery call fails — caller decides
    whether that is fatal."""
    issuer = (issuer or "").rstrip("/")
    if not issuer:
        raise RuntimeError("OIDC issuer is empty — cannot discover endpoints")
    url = f"{issuer}/.well-known/openid-configuration"
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            r = client.get(url)
            r.raise_for_status()
            data = r.json()
            if not isinstance(data, dict):
                raise RuntimeError(f"OIDC discovery returned non-object: {type(data)}")
            return data
    except httpx.HTTPError as e:
        raise RuntimeError(f"OIDC discovery failed for {url}: {e}") from e


def _resolve_issuer(provider: str, raw_issuer: str, tenant_id: str) -> str:
    """Compute the issuer URL based on provider/tenant when one isn't supplied."""
    if raw_issuer:
        return raw_issuer.rstrip("/")
    p = (provider or "").lower()
    if p == "google":
        return "https://accounts.google.com"
    if p == "azure" and tenant_id:
        return f"https://login.microsoftonline.com/{tenant_id}/v2.0"
    raise RuntimeError(f"OIDC_ISSUER is required for provider '{provider}'")


def get_oidc_config() -> Optional[OIDCConfig]:
    """Return the cached OIDC config, or ``None`` if OIDC is disabled.

    Lazily builds + validates the config on first call. Validation errors are
    cached so subsequent calls don't re-spam discovery endpoints. The cached
    error is logged once at WARNING level."""
    global _oidc_config, _oidc_load_attempted, _oidc_load_error

    if (os.getenv("OIDC_ENABLED") or "").strip().lower() != "true":
        return None

    if _oidc_config is not None:
        return _oidc_config

    if _oidc_load_attempted and _oidc_load_error:
        # Don't keep retrying discovery on every request
        return None

    _oidc_load_attempted = True

    provider      = (os.getenv("OIDC_PROVIDER") or "").strip().lower()
    client_id     = (os.getenv("OIDC_CLIENT_ID") or "").strip()
    client_secret = (os.getenv("OIDC_CLIENT_SECRET") or "").strip()
    raw_issuer    = (os.getenv("OIDC_ISSUER") or "").strip()
    tenant_id     = (os.getenv("OIDC_TENANT_ID") or "").strip()
    redirect_uri  = (os.getenv("OIDC_REDIRECT_URI") or "").strip()
    scopes        = (os.getenv("OIDC_SCOPES") or _DEFAULT_SCOPES).strip()
    default_role  = (os.getenv("OIDC_DEFAULT_ROLE") or "readonly").strip()
    tenant_claim  = (os.getenv("OIDC_TENANT_CLAIM") or "tenant").strip()

    missing = []
    if provider not in {"okta", "azure", "google"}:
        missing.append(f"OIDC_PROVIDER (got '{provider}', expected okta|azure|google)")
    if not client_id:
        missing.append("OIDC_CLIENT_ID")
    if not client_secret:
        missing.append("OIDC_CLIENT_SECRET")
    if not redirect_uri:
        missing.append("OIDC_REDIRECT_URI")
    if missing:
        msg = "OIDC misconfigured — missing: " + ", ".join(missing)
        _oidc_load_error = msg
        logger.warning(msg)
        return None

    try:
        issuer = _resolve_issuer(provider, raw_issuer, tenant_id)
        endpoints = _discover_endpoints(issuer)
    except RuntimeError as e:
        _oidc_load_error = str(e)
        logger.warning("OIDC disabled: %s", e)
        return None

    _oidc_config = OIDCConfig(
        provider=provider,
        client_id=client_id,
        client_secret=client_secret,
        issuer=issuer,
        redirect_uri=redirect_uri,
        scopes=scopes,
        default_role=default_role,
        tenant_claim=tenant_claim,
        tenant_id=tenant_id,
        endpoints=endpoints,
    )
    logger.info("OIDC configured: provider=%s issuer=%s", provider, issuer)
    return _oidc_config


def reset_oidc_config_cache() -> None:
    """Test/admin hook: force ``get_oidc_config`` to rebuild from env on next call."""
    global _oidc_config, _oidc_load_attempted, _oidc_load_error
    _oidc_config = None
    _oidc_load_attempted = False
    _oidc_load_error = None


# ─────────────────────────────────────────────────────────────────────────────
# Authorization-code flow helpers
# ─────────────────────────────────────────────────────────────────────────────

def build_authorization_url(config: OIDCConfig, state: str) -> str:
    """Build the IdP authorization URL with all required query params."""
    if not config.authorization_endpoint:
        raise RuntimeError("OIDC provider has no authorization_endpoint")
    params = {
        "client_id":     config.client_id,
        "redirect_uri":  config.redirect_uri,
        "response_type": "code",
        "scope":         config.scopes,
        "state":         state,
    }
    # Azure / Google like to see prompt=select_account for multi-tenant logins
    if config.provider in {"azure", "google"}:
        params["prompt"] = "select_account"
    return f"{config.authorization_endpoint}?{urlencode(params)}"


def exchange_code(config: OIDCConfig, code: str) -> dict:
    """Exchange an authorization code for a token bundle. Returns the raw
    JSON response (access_token, id_token, refresh_token, expires_in, ...).

    Raises ``RuntimeError`` if the call fails — caller turns this into a 401."""
    if not config.token_endpoint:
        raise RuntimeError("OIDC provider has no token_endpoint")
    data = {
        "grant_type":    "authorization_code",
        "code":          code,
        "redirect_uri":  config.redirect_uri,
        "client_id":     config.client_id,
        "client_secret": config.client_secret,
    }
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            r = client.post(
                config.token_endpoint,
                data=data,
                headers={"Accept": "application/json"},
            )
            if r.status_code >= 400:
                raise RuntimeError(
                    f"Token exchange failed ({r.status_code}): {r.text[:200]}"
                )
            payload = r.json()
            if not isinstance(payload, dict):
                raise RuntimeError("Token endpoint returned non-object payload")
            return payload
    except httpx.HTTPError as e:
        raise RuntimeError(f"Token exchange HTTP error: {e}") from e


def get_userinfo(config: OIDCConfig, access_token: str) -> dict:
    """Call the userinfo endpoint with the access token and return the
    claims dict. Raises ``RuntimeError`` if the call fails."""
    if not config.userinfo_endpoint:
        raise RuntimeError("OIDC provider has no userinfo_endpoint")
    if not access_token:
        raise RuntimeError("No access_token to call userinfo")
    try:
        with httpx.Client(timeout=_HTTP_TIMEOUT) as client:
            r = client.get(
                config.userinfo_endpoint,
                headers={"Authorization": f"Bearer {access_token}",
                         "Accept": "application/json"},
            )
            if r.status_code >= 400:
                raise RuntimeError(
                    f"Userinfo call failed ({r.status_code}): {r.text[:200]}"
                )
            claims = r.json()
            if not isinstance(claims, dict):
                raise RuntimeError("Userinfo returned non-object payload")
            return claims
    except httpx.HTTPError as e:
        raise RuntimeError(f"Userinfo HTTP error: {e}") from e


# ─────────────────────────────────────────────────────────────────────────────
# Claim → xREF user mapping
# ─────────────────────────────────────────────────────────────────────────────

def claims_to_user(claims: dict, default_tenant: str, default_role: str = "readonly") -> dict:
    """Translate an OIDC claims dict into the user shape used by xREF tenant
    user lists. The returned dict mirrors the structure created in
    ``routers/auth.py::invite_user`` so it can be appended directly.

    The ``password`` field is left blank — SSO users authenticate via the IdP."""
    email = (claims.get("email") or "").strip().lower()
    name  = (
        claims.get("name")
        or claims.get("preferred_username")
        or (email.split("@")[0] if email else "")
    )
    sub = claims.get("sub") or ""
    return {
        "email":        email,
        "password":     "",                  # SSO — no local password
        "role":         default_role,
        "active":       True,
        "invited_at":   None,
        "last_login":   None,
        "display_name": name or email,
        "sso":          True,
        "sso_subject":  sub,
        "tenant":       default_tenant,
    }


# ─────────────────────────────────────────────────────────────────────────────
# In-memory state store (CSRF protection for the redirect flow)
# ─────────────────────────────────────────────────────────────────────────────

_pending_states: dict[str, dict] = {}
_STATE_TTL = 300  # 5 minutes


def _purge_expired_states(now: Optional[float] = None) -> None:
    """Drop any state entries whose TTL has expired."""
    now = now if now is not None else time.time()
    cutoff = now - _STATE_TTL
    expired = [k for k, v in _pending_states.items() if v.get("created_at", 0) < cutoff]
    for k in expired:
        _pending_states.pop(k, None)


def generate_state(redirect_after: str = "/") -> str:
    """Mint a random state token, remember when it was issued and where to
    bounce the user after a successful callback. Returns the token string."""
    _purge_expired_states()
    token = secrets.token_urlsafe(32)
    _pending_states[token] = {
        "created_at":     time.time(),
        "redirect_after": redirect_after or "/",
    }
    return token


def validate_state(state: str) -> bool:
    """Verify a state token: must exist and be within TTL. Consumes the token
    (single-use) regardless of outcome to prevent replay."""
    if not state:
        return False
    _purge_expired_states()
    entry = _pending_states.pop(state, None)
    if not entry:
        return False
    age = time.time() - entry.get("created_at", 0)
    return age <= _STATE_TTL


def consume_state(state: str) -> Optional[dict]:
    """Like ``validate_state`` but returns the stored entry (so callers can
    read ``redirect_after``). Returns ``None`` if invalid/expired."""
    if not state:
        return None
    _purge_expired_states()
    entry = _pending_states.pop(state, None)
    if not entry:
        return None
    age = time.time() - entry.get("created_at", 0)
    if age > _STATE_TTL:
        return None
    return entry
