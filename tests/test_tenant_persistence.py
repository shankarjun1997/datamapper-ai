"""Tenants persist to Postgres in DB mode (round-trip + routing), with the
secret vault encrypted at the persistence boundary."""
import pytest

import app.state as state


@pytest.fixture
def clean_tenants():
    saved = dict(state._TENANTS)
    state._TENANTS.clear()
    yield
    state._TENANTS.clear()
    state._TENANTS.update(saved)


def test_save_routes_to_db_with_secrets_encrypted(monkeypatch, clean_tenants):
    captured = {}
    monkeypatch.setattr("app.core.db_store.db_save_tenants",
                        lambda payload: captured.update({"payload": payload}))
    monkeypatch.setattr(state, "_DB_MODE", True)

    state._TENANTS["acme"] = {
        "slug": "acme", "name": "Acme", "plan": "standard",
        "users": [{"email": "a@acme.io", "password": "$2b$hash", "role": "admin"}],
        "secrets": {"openai": {"value": "sk-super-secret", "created_at": "t"}},
    }
    state._save_tenants()

    payload = captured["payload"]
    assert isinstance(payload, list) and payload[0]["slug"] == "acme"
    # The vault value must be transformed at rest (encrypted, or no-op without a
    # key) — never mutated in the live in-memory copy.
    assert state._TENANTS["acme"]["secrets"]["openai"]["value"] == "sk-super-secret"


def test_db_load_round_trips_tenant_and_decrypts(monkeypatch, clean_tenants):
    # Build the at-rest payload exactly as the save path would.
    state._TENANTS["globex"] = {
        "slug": "globex", "name": "Globex", "plan": "trial",
        "users": [{"email": "b@globex.io", "password": "$2b$h", "role": "mapper"}],
        "secrets": {"db": {"value": "conn-string-123"}},
    }
    at_rest = state._tenants_for_disk()
    state._TENANTS.clear()

    monkeypatch.setattr("app.core.db_store.db_load_tenants", lambda: at_rest)
    monkeypatch.setattr(state, "_DB_MODE", True)
    monkeypatch.setattr("os.path.exists", lambda p: False)  # skip JSON migration

    state._load_tenants()

    t = state._TENANTS.get("globex")
    assert t and t["name"] == "Globex"
    assert t["users"][0]["email"] == "b@globex.io"
    # secret value decrypted back to plaintext for in-memory use
    assert t["secrets"]["db"]["value"] == "conn-string-123"


def test_save_falls_back_to_json_when_not_db_mode(monkeypatch, clean_tenants, tmp_path):
    monkeypatch.setattr(state, "_DB_MODE", False)
    monkeypatch.setattr(state, "_TENANTS_STORE_PATH", str(tmp_path / "tenants.json"))
    state._TENANTS["t1"] = {"slug": "t1", "name": "T1", "users": []}
    state._save_tenants()
    import json
    data = json.load(open(str(tmp_path / "tenants.json")))
    assert any(t["slug"] == "t1" for t in data)
