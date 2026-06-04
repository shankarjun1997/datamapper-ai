"""Tests for context->source / source->target schema normalization."""
from app.intelligence.source_infer import (
    _canon_type,
    build_source_prompt,
    build_target_prompt,
    normalize_schema,
)


def test_canon_type_variants():
    assert _canon_type("varchar(120)") == "STRING"
    assert _canon_type("BIGINT") == "INT64"
    assert _canon_type("decimal(10,2)") == "NUMERIC"
    assert _canon_type("timestamp") == "TIMESTAMP"
    assert _canon_type("") == "STRING"


def test_normalize_canonical_tables_shape():
    raw = {"tables": [{"name": "cust", "columns": [
        {"name": "id", "type": "int"}, {"name": "email", "data_type": "varchar(255)"}]}]}
    out = normalize_schema(raw)
    assert out["tables"][0]["name"] == "cust"
    cols = out["tables"][0]["columns"]
    assert cols[0] == {"name": "id", "type": "INT64", "sample": "", "nullable": True}
    assert cols[1]["type"] == "STRING"


def test_normalize_single_table_columns_key():
    raw = {"name": "orders", "columns": [{"field": "order_id", "type": "string"}]}
    out = normalize_schema(raw)
    assert out["tables"][0]["name"] == "orders"
    assert out["tables"][0]["columns"][0]["name"] == "order_id"


def test_normalize_flat_column_list_with_default_name():
    raw = [{"name": "a", "type": "string"}, {"name": "b", "type": "int"}]
    out = normalize_schema(raw, default_name="ctx_src")
    assert out["tables"][0]["name"] == "ctx_src"
    assert [c["name"] for c in out["tables"][0]["columns"]] == ["a", "b"]


def test_normalize_bare_string_columns():
    raw = {"columns": ["customer_id", "customer_name", "  "]}
    out = normalize_schema(raw, default_name="s")
    names = [c["name"] for c in out["tables"][0]["columns"]]
    assert names == ["customer_id", "customer_name"]  # blank dropped
    assert all(c["type"] == "STRING" for c in out["tables"][0]["columns"])


def test_normalize_nullable_strings():
    raw = {"columns": [{"name": "x", "type": "int", "is_nullable": "NO"},
                       {"name": "y", "type": "int", "required": "true"}]}
    cols = normalize_schema(raw)["tables"][0]["columns"]
    assert cols[0]["nullable"] is False           # "NO" -> not nullable
    assert cols[1]["nullable"] is False           # required -> not nullable


def test_normalize_garbage_returns_empty_tables():
    assert normalize_schema("not json")["tables"] == []
    assert normalize_schema({"unexpected": 1})["tables"] == []


def test_prompt_builders_include_inputs():
    assert "CONTEXT:" in build_source_prompt("a Jira story about customers", "cust")
    sd = {"tables": [{"name": "cust", "columns": [{"name": "id", "type": "INT64"}]}]}
    p = build_target_prompt(sd, "telecom", "split names")
    assert "cust" in p and "telecom" in p and "split names" in p
