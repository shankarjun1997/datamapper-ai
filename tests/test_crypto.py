"""Tests for encryption-at-rest helpers (Wave 4)."""
import importlib

import pytest

cryptography = pytest.importorskip("cryptography")
from cryptography.fernet import Fernet  # noqa: E402


@pytest.fixture()
def crypto_enabled(monkeypatch):
    monkeypatch.setenv("DM_ENCRYPTION_KEY", Fernet.generate_key().decode())
    import app.core.crypto as crypto
    importlib.reload(crypto)
    yield crypto
    monkeypatch.delenv("DM_ENCRYPTION_KEY", raising=False)
    importlib.reload(crypto)


def test_disabled_is_passthrough():
    import app.core.crypto as crypto
    importlib.reload(crypto)
    obj = {"bq_config": {"gcp_creds_json": {"private_key": "abc"}}}
    assert crypto.protect_obj(obj) == obj  # no key -> unchanged


def test_roundtrip_encrypts_sensitive_only(crypto_enabled):
    crypto = crypto_enabled
    session = {
        "id": "s1",
        "name": "keep me",
        "bq_config": {"project": "p", "gcp_creds_json": {"private_key": "SECRET"}},
        "api_config": {"provider": "claude", "api_key": "sk-secret"},
    }
    protected = crypto.protect_obj(session)
    # Non-sensitive untouched
    assert protected["name"] == "keep me"
    assert protected["bq_config"]["project"] == "p"
    # Sensitive encrypted (prefixed, not plaintext)
    assert protected["bq_config"]["gcp_creds_json"].startswith("enc:v1:")
    assert protected["api_config"]["api_key"].startswith("enc:v1:")
    assert "SECRET" not in str(protected)
    assert "sk-secret" not in str(protected)
    # Round-trips back to the original
    assert crypto.unprotect_obj(protected) == session


def test_protect_does_not_mutate_input(crypto_enabled):
    crypto = crypto_enabled
    original = {"api_config": {"api_key": "sk-123"}}
    crypto.protect_obj(original)
    assert original["api_config"]["api_key"] == "sk-123"  # unchanged
