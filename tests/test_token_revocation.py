"""Tests for token revocation (Wave 3): deactivation and tokens_valid_after."""
import time

import pytest

import app.state as state
from app.core.auth import _sign_token, _verify_token


@pytest.fixture(autouse=True)
def seed_tenant():
    state._TENANTS.clear()
    state._TENANTS["acme"] = {
        "name": "Acme", "plan": "std",
        "users": [{"email": "u@acme.io", "role": "mapper", "active": True}],
    }
    yield
    state._TENANTS.clear()


def _token(iat_offset=0, exp_offset=3600):
    now = time.time()
    return _sign_token({
        "tenant": "acme", "email": "u@acme.io", "role": "mapper",
        "exp": now + exp_offset, "iat": now + iat_offset,
    })


def test_valid_token_passes():
    assert _verify_token(_token()) is not None


def test_token_issued_before_watermark_is_revoked():
    user = state._TENANTS["acme"]["users"][0]
    old = _token(iat_offset=-10)
    user["tokens_valid_after"] = time.time()
    assert _verify_token(old) is None


def test_token_issued_after_watermark_survives():
    user = state._TENANTS["acme"]["users"][0]
    user["tokens_valid_after"] = time.time() - 5
    assert _verify_token(_token(iat_offset=0)) is not None


def test_deactivated_user_token_rejected():
    tok = _token()
    state._TENANTS["acme"]["users"][0]["active"] = False
    assert _verify_token(tok) is None


def test_expired_token_rejected():
    assert _verify_token(_token(exp_offset=-1)) is None
