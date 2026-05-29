"""Tests for token-source extraction + CSRF source detection (Wave 6)."""
from types import SimpleNamespace

from app.core.auth import _extract_token, _token_is_from_cookie


def _req(headers=None, cookies=None, qp=None):
    return SimpleNamespace(headers=headers or {}, cookies=cookies or {}, query_params=qp or {})


def test_bearer_header_wins():
    r = _req(headers={"Authorization": "Bearer abc"}, cookies={"xref_token": "ck"})
    assert _extract_token(r) == "abc"
    assert _token_is_from_cookie(r) is False


def test_query_token():
    r = _req(qp={"token": "qq"})
    assert _extract_token(r) == "qq"
    assert _token_is_from_cookie(r) is False


def test_cookie_fallback():
    r = _req(cookies={"xref_token": "ck"})
    assert _extract_token(r) == "ck"
    assert _token_is_from_cookie(r) is True


def test_no_token():
    r = _req()
    assert _extract_token(r) == ""
    assert _token_is_from_cookie(r) is False
