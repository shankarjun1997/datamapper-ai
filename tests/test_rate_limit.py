"""Unit tests for the in-memory rate limiter fallback (P4)."""
import app.routers._helpers as helpers


def test_allows_up_to_limit_then_blocks():
    key = "test-ip-1"
    helpers._rate_store.pop(key, None)
    # 3 allowed, 4th blocked within the window.
    assert helpers._check_rate_limit(key, limit=3, window=60) is True
    assert helpers._check_rate_limit(key, limit=3, window=60) is True
    assert helpers._check_rate_limit(key, limit=3, window=60) is True
    assert helpers._check_rate_limit(key, limit=3, window=60) is False


def test_separate_keys_are_independent():
    helpers._rate_store.pop("ip-a", None)
    helpers._rate_store.pop("ip-b", None)
    assert helpers._check_rate_limit("ip-a", limit=1, window=60) is True
    assert helpers._check_rate_limit("ip-a", limit=1, window=60) is False
    # Different key (e.g. another client) is unaffected.
    assert helpers._check_rate_limit("ip-b", limit=1, window=60) is True
