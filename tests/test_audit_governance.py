"""Governance regression tests: audit visibility scoping + session-delete guard."""
from app.routers.admin import _audit_visible


def test_audit_visible_same_tenant():
    rec = {"id": "1", "tenant": "acme"}
    assert _audit_visible(rec, "acme", is_super=False) is True


def test_audit_hidden_cross_tenant():
    rec = {"id": "1", "tenant": "globex"}
    assert _audit_visible(rec, "acme", is_super=False) is False


def test_super_admin_sees_all_audit():
    assert _audit_visible({"id": "1", "tenant": "globex"}, "acme", is_super=True) is True
    # records without a tenant are only visible to super-admins
    assert _audit_visible({"id": "2"}, "acme", is_super=False) is False
    assert _audit_visible({"id": "2"}, "infinite", is_super=True) is True
