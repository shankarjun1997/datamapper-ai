"""
app/core/stripe_billing.py — Stripe checkout/portal + webhook (Phase 3).

Optional: activates only when STRIPE_SECRET_KEY is set. The SDK calls are thin
wrappers; the event→tenant state transition (`apply_event`) is a PURE function
so it's fully unit-testable without the Stripe SDK or network.

Env:
  STRIPE_SECRET_KEY        — server key (enables the feature)
  STRIPE_WEBHOOK_SECRET    — for signature verification
  STRIPE_PRICE_STANDARD    — Stripe price id for the Standard plan
  STRIPE_PRICE_ENTERPRISE  — Stripe price id for the Enterprise plan
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timezone
from typing import Dict, Optional

logger = logging.getLogger("xref_agent")


def enabled() -> bool:
    return bool(os.getenv("STRIPE_SECRET_KEY"))


def price_for(plan: str) -> Optional[str]:
    return os.getenv(f"STRIPE_PRICE_{(plan or '').upper()}") or None


def plan_for_price(price_id: str) -> Optional[str]:
    """Reverse-map a Stripe price id back to a plan code."""
    if not price_id:
        return None
    for plan in ("standard", "enterprise", "custom", "trial"):
        if os.getenv(f"STRIPE_PRICE_{plan.upper()}") == price_id:
            return plan
    return None


def _client():
    import stripe  # lazy — only when enabled
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    return stripe


def create_checkout(tenant_slug: str, plan: str, success_url: str, cancel_url: str,
                    customer_id: Optional[str] = None) -> str:
    """Create a Stripe Checkout session and return its URL."""
    price = price_for(plan)
    if not price:
        raise RuntimeError(f"No Stripe price configured for plan '{plan}' (set STRIPE_PRICE_{plan.upper()})")
    stripe = _client()
    session = stripe.checkout.Session.create(
        mode="subscription",
        line_items=[{"price": price, "quantity": 1}],
        success_url=success_url,
        cancel_url=cancel_url,
        client_reference_id=tenant_slug,
        customer=customer_id or None,
        metadata={"tenant": tenant_slug, "plan": plan},
        subscription_data={"metadata": {"tenant": tenant_slug, "plan": plan}},
    )
    return session.url


def create_portal(customer_id: str, return_url: str) -> str:
    """Create a Stripe billing-portal session (manage/cancel subscription)."""
    stripe = _client()
    sess = stripe.billing_portal.Session.create(customer=customer_id, return_url=return_url)
    return sess.url


def construct_event(payload: bytes, sig_header: str) -> dict:
    """Verify the webhook signature and return the parsed event."""
    secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
    stripe = _client()
    if secret:
        return stripe.Webhook.construct_event(payload, sig_header, secret)
    # No secret configured — parse without verification (dev only).
    import json
    return json.loads(payload)


def _find_tenant(tenants: Dict, slug: Optional[str], customer_id: Optional[str]) -> Optional[dict]:
    if slug and slug in tenants:
        return tenants[slug]
    if customer_id:
        for t in tenants.values():
            if (t.get("billing", {}) or {}).get("provider_customer_id") == customer_id:
                return t
    return None


def apply_event(event: dict, tenants: Dict) -> Optional[str]:
    """PURE: apply a Stripe event to the tenant map. Returns the updated slug.

    Handles checkout completion (activate plan), subscription updates (status +
    plan from price), and cancellation (status canceled)."""
    etype = event.get("type", "")
    obj = (event.get("data", {}) or {}).get("object", {}) or {}
    meta = obj.get("metadata", {}) or {}
    slug = meta.get("tenant") or obj.get("client_reference_id")
    customer_id = obj.get("customer")
    now = datetime.now(timezone.utc).isoformat()

    t = _find_tenant(tenants, slug, customer_id)
    if t is None:
        logger.warning("Stripe event %s: no matching tenant (slug=%s customer=%s)", etype, slug, customer_id)
        return None
    billing = t.setdefault("billing", {})

    if etype == "checkout.session.completed":
        if meta.get("plan"):
            t["plan"] = meta["plan"]
        billing["status"] = "active"
        if customer_id:
            billing["provider_customer_id"] = customer_id
        billing["updated_at"] = now

    elif etype in ("customer.subscription.updated", "customer.subscription.created"):
        status = obj.get("status")  # active, trialing, past_due, canceled…
        if status:
            billing["status"] = "active" if status in ("active", "trialing") else status
        try:
            price_id = obj["items"]["data"][0]["price"]["id"]
            plan = plan_for_price(price_id) or meta.get("plan")
            if plan:
                t["plan"] = plan
        except (KeyError, IndexError, TypeError):
            pass
        if customer_id:
            billing["provider_customer_id"] = customer_id
        billing["updated_at"] = now

    elif etype == "customer.subscription.deleted":
        billing["status"] = "canceled"
        billing["updated_at"] = now

    return t.get("slug")
