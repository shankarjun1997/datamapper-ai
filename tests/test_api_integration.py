"""HTTP-level integration tests (run in CI where full deps are installed).

Skips automatically in lightweight environments that can't import the full app
(missing LLM/CrewAI deps). Auth is forced on via env before the app imports.
"""
import os

import pytest

# Force auth enforcement, but stay in dev so Postgres isn't required at startup.
os.environ.setdefault("XREF_REQUIRE_AUTH", "true")
os.environ.setdefault("DM_ENV", "dev")

pytest.importorskip("anthropic")  # proxy for "full requirements installed"
fastapi_testclient = pytest.importorskip("fastapi.testclient")

try:
    from app.main import app
    from fastapi.testclient import TestClient
    _client = TestClient(app)
except Exception as e:  # pragma: no cover
    pytest.skip(f"app not importable in this env: {e}", allow_module_level=True)


def test_health_is_public():
    assert _client.get("/api/health").status_code == 200


def test_protected_route_requires_auth():
    # No token -> 401 because XREF_REQUIRE_AUTH=true.
    r = _client.get("/api/sessions")
    assert r.status_code == 401


def test_login_then_access(monkeypatch):
    # Demo tenant exists in dev seed.
    r = _client.post("/api/auth/login", json={"tenant": "demo", "email": "demo@xref.ai", "password": "demo"})
    assert r.status_code == 200
    token = r.json()["token"]
    # Cookie should be set httpOnly
    assert "xref_token" in r.cookies or any("xref_token" in c for c in r.headers.get("set-cookie", "").split(","))
    # Bearer access works
    r2 = _client.get("/api/sessions", headers={"Authorization": f"Bearer {token}"})
    assert r2.status_code == 200
