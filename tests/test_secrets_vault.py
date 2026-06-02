"""Tests for the workspace secrets vault (masking + encryption at rest)."""
import importlib

import pytest

from app.routers.workspace import mask, _NAME_RE, get_tenant_secret
import app.state as state


def test_mask_hides_value_keeps_last4():
    assert mask("sk-1234567890abcd").endswith("abcd")
    assert "1234567890" not in mask("sk-1234567890abcd")
    assert mask("ab") == "••••"
    assert mask("") == ""


def test_name_validation():
    assert _NAME_RE.match("OPENAI_API_KEY")
    assert _NAME_RE.match("DB_CONN_2")
    assert not _NAME_RE.match("lowercase")
    assert not _NAME_RE.match("1STARTS_DIGIT")
    assert not _NAME_RE.match("HAS-DASH")


def test_get_tenant_secret_returns_plaintext():
    state._TENANTS.clear()
    state._TENANTS["acme"] = {"slug": "acme", "secrets": {"OPENAI_API_KEY": {"value": "sk-secret"}}}
    assert get_tenant_secret("acme", "openai_api_key") == "sk-secret"  # case-insensitive
    assert get_tenant_secret("acme", "MISSING") is None
    assert get_tenant_secret("nope", "OPENAI_API_KEY") is None
    state._TENANTS.clear()


def test_secrets_encrypted_for_disk_then_decrypted(monkeypatch):
    pytest.importorskip("cryptography.fernet")
    from cryptography.fernet import Fernet
    monkeypatch.setenv("DM_ENCRYPTION_KEY", Fernet.generate_key().decode())
    import app.core.crypto as crypto
    importlib.reload(crypto)

    state._TENANTS.clear()
    state._TENANTS["acme"] = {
        "slug": "acme",
        "secrets": {"DB_CONN": {"value": "postgres://u:p@h/db", "updated_by": "a@x"}},
    }
    disk = state._tenants_for_disk()
    blob = disk[0]["secrets"]["DB_CONN"]["value"]
    # On disk the value is ciphertext, not the plaintext connection string.
    assert blob.startswith("enc:v1:")
    assert "postgres://" not in blob
    # In-memory copy is untouched (still plaintext).
    assert state._TENANTS["acme"]["secrets"]["DB_CONN"]["value"] == "postgres://u:p@h/db"
    # And it decrypts back.
    assert crypto.decrypt_value(blob) == "postgres://u:p@h/db"

    state._TENANTS.clear()
    monkeypatch.delenv("DM_ENCRYPTION_KEY", raising=False)
    importlib.reload(crypto)
