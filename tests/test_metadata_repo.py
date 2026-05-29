"""Tests for the canonical versioned metadata repository (Layer 3)."""
import importlib

import pytest


@pytest.fixture()
def repo(tmp_path, monkeypatch):
    import app.core.metadata_repo as r
    importlib.reload(r)
    # Isolate persistence to a temp file.
    monkeypatch.setattr(r, "_STORE_PATH", str(tmp_path / "metadata.json"))
    r._objects.clear(); r._history.clear(); r._loaded = True
    return r


SCHEMA = {"tables": [
    {"name": "customers", "columns": [
        {"name": "id", "type": "INT64", "nullable": False},
        {"name": "email", "type": "STRING", "nullable": True},
    ]},
    {"name": "orders", "columns": [{"name": "order_id", "type": "INT64", "nullable": False}]},
]}


def test_ingest_builds_hierarchy(repo):
    counts = repo.ingest_schema("acme", "billing_db", "postgres", SCHEMA, "a@x")
    assert counts["systems"] == 1 and counts["tables"] == 2 and counts["columns"] == 3
    st = repo.stats("acme")
    assert st["by_type"]["system"] == 1
    assert st["by_type"]["table"] == 2
    assert st["by_type"]["column"] == 3


def test_versioning_only_on_change(repo):
    repo.ingest_schema("acme", "billing_db", "postgres", SCHEMA, "a@x")
    v_before = repo.stats("acme")["total_versions"]
    # Re-ingest identical schema → no new versions.
    repo.ingest_schema("acme", "billing_db", "postgres", SCHEMA, "a@x")
    assert repo.stats("acme")["total_versions"] == v_before
    # Change a column type → exactly one new version on that column.
    changed = {"tables": [{"name": "customers", "columns": [
        {"name": "id", "type": "NUMERIC", "nullable": False},   # type changed
        {"name": "email", "type": "STRING", "nullable": True},
    ]}]}
    repo.ingest_schema("acme", "billing_db", "postgres", changed, "a@x")
    col = repo.get_object("acme", "column:billing_db.customers.id")
    assert col["version"] == 2
    hist = repo.get_history("acme", "column:billing_db.customers.id")
    assert len(hist) == 2 and hist[-1]["attributes"]["data_type"] == "NUMERIC"


def test_tenant_isolation(repo):
    repo.ingest_schema("acme", "db", "postgres", SCHEMA, "a@x")
    assert repo.stats("globex")["total_objects"] == 0
    assert repo.list_objects("globex")["total"] == 0


def test_pagination_and_filter(repo):
    repo.ingest_schema("acme", "db", "postgres", SCHEMA, "a@x")
    cols = repo.list_objects("acme", otype="column", limit=2, offset=0)
    assert cols["total"] == 3 and len(cols["items"]) == 2
    page2 = repo.list_objects("acme", otype="column", limit=2, offset=2)
    assert len(page2["items"]) == 1
    # FQN search
    found = repo.list_objects("acme", q="orders")
    assert all("orders" in o["fqn"] for o in found["items"])
