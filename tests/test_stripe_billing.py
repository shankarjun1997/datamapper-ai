"""Tests for the Stripe billing layer — the pure event→tenant apply logic."""
from app.core import stripe_billing as sb


def test_enabled_false_without_key(monkeypatch):
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    assert sb.enabled() is False


def test_checkout_completed_activates_plan():
    tenants = {"acme": {"slug": "acme", "plan": "trial"}}
    ev = {"type": "checkout.session.completed", "data": {"object": {
        "customer": "cus_1", "client_reference_id": "acme",
        "metadata": {"tenant": "acme", "plan": "standard"}}}}
    assert sb.apply_event(ev, tenants) == "acme"
    assert tenants["acme"]["plan"] == "standard"
    assert tenants["acme"]["billing"]["status"] == "active"
    assert tenants["acme"]["billing"]["provider_customer_id"] == "cus_1"


def test_subscription_updated_maps_price(monkeypatch):
    monkeypatch.setenv("STRIPE_PRICE_ENTERPRISE", "price_ent")
    tenants = {"acme": {"slug": "acme", "plan": "standard",
                        "billing": {"provider_customer_id": "cus_1"}}}
    ev = {"type": "customer.subscription.updated", "data": {"object": {
        "customer": "cus_1", "status": "active",
        "items": {"data": [{"price": {"id": "price_ent"}}]}}}}
    sb.apply_event(ev, tenants)
    assert tenants["acme"]["plan"] == "enterprise"
    assert tenants["acme"]["billing"]["status"] == "active"


def test_subscription_deleted_cancels():
    tenants = {"acme": {"slug": "acme", "plan": "standard",
                        "billing": {"provider_customer_id": "cus_1"}}}
    sb.apply_event({"type": "customer.subscription.deleted",
                    "data": {"object": {"customer": "cus_1"}}}, tenants)
    assert tenants["acme"]["billing"]["status"] == "canceled"


def test_unknown_tenant_is_noop():
    assert sb.apply_event(
        {"type": "checkout.session.completed",
         "data": {"object": {"customer": "cus_x", "metadata": {"tenant": "ghost"}}}},
        {}) is None
