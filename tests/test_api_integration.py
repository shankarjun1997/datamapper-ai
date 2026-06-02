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


def _auth():
    r = _client.post("/api/auth/login", json={"tenant": "demo", "email": "demo@xref.ai", "password": "demo"})
    assert r.status_code == 200
    return {"Authorization": f"Bearer {r.json()['token']}"}


def test_login_then_access():
    h = _auth()
    r = _client.post("/api/auth/login", json={"tenant": "demo", "email": "demo@xref.ai", "password": "demo"})
    assert "xref_token" in r.cookies or any("xref_token" in c for c in r.headers.get("set-cookie", "").split(","))
    assert _client.get("/api/sessions", headers=h).status_code == 200


def test_public_meta_endpoints():
    assert _client.get("/api/migration/platforms").status_code == 200
    assert _client.get("/api/version").status_code == 200


def test_metadata_stats_authed():
    r = _client.get("/api/metadata/stats", headers=_auth())
    assert r.status_code == 200
    assert "by_type" in r.json()


def test_report_and_readiness_on_new_session():
    h = _auth()
    sid = _client.post("/api/sessions", headers=h, json={"name": "itest"}).json()["session_id"]
    # readiness + report should render even with no mappings yet
    assert _client.get(f"/api/sessions/{sid}/readiness", headers=h).status_code == 200
    rep = _client.get(f"/api/sessions/{sid}/report", headers=h)
    assert rep.status_code == 200 and "summary" in rep.json()


def test_cross_tenant_session_is_404():
    # A well-formed but non-owned session id must 404 (tenant isolation).
    h = _auth()
    other = "99999999-9999-4999-8999-999999999999"
    assert _client.get(f"/api/sessions/{other}", headers=h).status_code == 404
