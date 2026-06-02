"""
app/routers/workspace.py — tenant-level secrets vault.

Admins store reusable secrets (LLM keys, DB connection strings, service-account
JSON, etc.) once at the workspace level. Values are:
  - encrypted at rest (see app/core/crypto + state._tenants_for_disk),
  - never returned in plaintext over the API (reads are masked),
  - tenant-scoped (an admin only ever sees their own workspace's secrets),
  - audited on every write/delete.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Body, Depends, HTTPException, Request
from pydantic import BaseModel

from app.core.audit import _write_audit_event
from app.core import billing as _billing
from app.core.rbac import require_admin, require_readonly
from app.routers._helpers import _get_client_ip
from app.state import _TENANTS, _audit_events, _save_tenants, _sessions

router = APIRouter()

_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


class SecretBody(BaseModel):
    value: str
    description: Optional[str] = ""


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def mask(value: str) -> str:
    """Mask a secret for display: keep the last 4 chars, hide the rest."""
    if not value:
        return ""
    v = str(value)
    if len(v) <= 4:
        return "••••"
    return "••••••••" + v[-4:]


def _public_secret(name: str, rec: dict) -> dict:
    return {
        "name":        name,
        "description": rec.get("description", ""),
        "masked":      mask(rec.get("value", "")),
        "updated_at":  rec.get("updated_at"),
        "updated_by":  rec.get("updated_by"),
    }


@router.get("/api/workspace/secrets")
async def list_secrets(_user=Depends(require_admin)):
    """List the calling tenant's secrets (masked — never plaintext)."""
    tenant = _TENANTS.get(_user["tenant"]) or {}
    secrets = tenant.get("secrets", {}) or {}
    items = [_public_secret(k, v) for k, v in secrets.items() if isinstance(v, dict)]
    items.sort(key=lambda s: s["name"])
    return {"secrets": items}


@router.put("/api/workspace/secrets/{name}")
async def put_secret(name: str, body: SecretBody, request: Request, _user=Depends(require_admin)):
    """Create or update a workspace secret (admin only)."""
    name = (name or "").strip().upper()
    if not _NAME_RE.match(name):
        raise HTTPException(422, "Name must be UPPER_SNAKE_CASE (letters, digits, underscore; 2–64 chars)")
    if not body.value:
        raise HTTPException(422, "value is required")

    tenant = _TENANTS.get(_user["tenant"])
    if not tenant:
        raise HTTPException(404, "Tenant not found")
    secrets = tenant.setdefault("secrets", {})
    existed = name in secrets
    secrets[name] = {
        "value":       body.value,
        "description": body.description or "",
        "updated_at":  _now_iso(),
        "updated_by":  _user.get("email", ""),
    }
    _save_tenants()
    _write_audit_event(
        "workspace.secret_updated" if existed else "workspace.secret_created",
        tenant=_user["tenant"], email=_user.get("email"), ip=_get_client_ip(request),
        metadata={"name": name},  # never log the value
    )
    return {"ok": True, "secret": _public_secret(name, secrets[name])}


@router.delete("/api/workspace/secrets/{name}")
async def delete_secret(name: str, request: Request, _user=Depends(require_admin)):
    """Delete a workspace secret (admin only)."""
    name = (name or "").strip().upper()
    tenant = _TENANTS.get(_user["tenant"]) or {}
    secrets = tenant.get("secrets", {}) or {}
    if name not in secrets:
        raise HTTPException(404, f"Secret '{name}' not found")
    del secrets[name]
    _save_tenants()
    _write_audit_event(
        "workspace.secret_deleted", tenant=_user["tenant"], email=_user.get("email"),
        ip=_get_client_ip(request), metadata={"name": name},
    )
    return {"ok": True}


def get_tenant_secret(tenant_slug: str, name: str) -> Optional[str]:
    """Internal helper for consumers (pipeline/connectors) to read a plaintext
    secret value by name. Returns None if absent."""
    tenant = _TENANTS.get(tenant_slug or "") or {}
    rec = (tenant.get("secrets", {}) or {}).get((name or "").strip().upper())
    return rec.get("value") if isinstance(rec, dict) else None


# ── Billing / plan usage (Phase 1: read-only, derived metering) ─────────────────
@router.get("/api/billing/plans")
async def billing_plans(_user=Depends(require_readonly)):
    """The plan catalog (limits + features) for the upgrade UI."""
    return {"plans": _billing.public_catalog()}


@router.get("/api/workspace/billing")
async def workspace_billing(_user=Depends(require_readonly)):
    """Current plan + this period's usage vs limits for the caller's workspace."""
    tenant = _TENANTS.get(_user["tenant"]) or {"slug": _user["tenant"]}
    tenant.setdefault("slug", _user["tenant"])
    out = _billing.compute_billing(tenant, _sessions, _audit_events)
    from app.core import stripe_billing as _sb
    out["checkout_enabled"] = _sb.enabled()
    return out


# ── Stripe (Phase 3) — checkout / portal / webhook ──────────────────────────────
@router.post("/api/billing/checkout")
async def billing_checkout(request: Request, body: dict = Body(...), _user=Depends(require_admin)):
    """Create a Stripe Checkout session for a plan; returns the redirect URL."""
    from app.core import stripe_billing as sb
    if not sb.enabled():
        raise HTTPException(503, "Online checkout is not configured. Contact your account team to change plans.")
    plan = (body.get("plan") or "").lower()
    if plan not in _billing.PLAN_CATALOG:
        raise HTTPException(422, f"Unknown plan '{plan}'")
    tenant = _TENANTS.get(_user["tenant"]) or {}
    customer = (tenant.get("billing", {}) or {}).get("provider_customer_id")
    base = str(request.base_url).rstrip("/")
    try:
        url = sb.create_checkout(_user["tenant"], plan, base + "/?billing=success",
                                 base + "/?billing=cancel", customer_id=customer)
    except Exception as e:
        raise HTTPException(502, f"Checkout failed: {e}")
    _write_audit_event("billing.checkout_started", tenant=_user["tenant"],
                       email=_user.get("email"), ip=_get_client_ip(request), metadata={"plan": plan})
    return {"url": url}


@router.post("/api/billing/portal")
async def billing_portal(request: Request, _user=Depends(require_admin)):
    """Stripe billing-portal link to manage/cancel the subscription."""
    from app.core import stripe_billing as sb
    if not sb.enabled():
        raise HTTPException(503, "Billing portal is not configured.")
    tenant = _TENANTS.get(_user["tenant"]) or {}
    customer = (tenant.get("billing", {}) or {}).get("provider_customer_id")
    if not customer:
        raise HTTPException(409, "No billing account yet — start a checkout first.")
    try:
        url = sb.create_portal(customer, str(request.base_url).rstrip("/") + "/")
    except Exception as e:
        raise HTTPException(502, f"Portal failed: {e}")
    return {"url": url}


@router.post("/api/billing/webhook")
async def billing_webhook(request: Request):
    """Stripe webhook — verifies signature and syncs plan/status onto the tenant."""
    from app.core import stripe_billing as sb
    payload = await request.body()
    sig = request.headers.get("Stripe-Signature", "")
    try:
        event = sb.construct_event(payload, sig)
    except Exception as e:
        raise HTTPException(400, f"Invalid webhook: {e}")
    slug = sb.apply_event(event, _TENANTS)
    if slug:
        _save_tenants()
        _write_audit_event("billing.webhook", tenant=slug,
                           metadata={"type": (event or {}).get("type")})
    return {"ok": True, "tenant": slug}
