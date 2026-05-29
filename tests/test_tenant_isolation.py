"""Unit tests for cross-tenant session isolation (Fix #2).

These exercise the access-control core that every session-scoped route and the
TenantIsolationMiddleware rely on: a caller from tenant A must not be able to
reach a session owned by tenant B, while the super-admin tenant may.
"""
import pytest
from fastapi import HTTPException

import app.state as state
from app.core.session_store import _session_or_404, _tenant_can_access

# A well-formed UUID (matches the session id regex).
SID_A = "11111111-1111-4111-8111-111111111111"
SID_B = "22222222-2222-4222-8222-222222222222"
BAD_SID = "not-a-uuid"


@pytest.fixture(autouse=True)
def seed_sessions():
    state._sessions.clear()
    state._sessions[SID_A] = {"id": SID_A, "tenant": "acme"}
    state._sessions[SID_B] = {"id": SID_B, "tenant": "globex"}
    yield
    state._sessions.clear()


def test_same_tenant_can_access_its_own_session():
    s = _session_or_404(SID_A, caller_tenant="acme")
    assert s["id"] == SID_A


def test_cross_tenant_access_is_404_not_403():
    # globex must not learn that acme's session exists.
    with pytest.raises(HTTPException) as exc:
        _session_or_404(SID_A, caller_tenant="globex")
    assert exc.value.status_code == 404


def test_super_admin_tenant_can_access_any_session():
    # _ADMIN_TENANT defaults to "infinite".
    assert _tenant_can_access("infinite", state._sessions[SID_A]) is True
    s = _session_or_404(SID_A, caller_tenant="infinite")
    assert s["id"] == SID_A


def test_no_caller_tenant_skips_enforcement():
    # Dev/guest mode (no token) is handled by route auth guards, not here.
    assert _tenant_can_access(None, state._sessions[SID_A]) is True
    s = _session_or_404(SID_A)  # no caller_tenant -> existence check only
    assert s["id"] == SID_A


def test_unknown_session_is_404():
    with pytest.raises(HTTPException) as exc:
        _session_or_404("33333333-3333-4333-8333-333333333333", caller_tenant="acme")
    assert exc.value.status_code == 404


def test_malformed_session_id_is_400():
    with pytest.raises(HTTPException) as exc:
        _session_or_404(BAD_SID, caller_tenant="acme")
    assert exc.value.status_code == 400
