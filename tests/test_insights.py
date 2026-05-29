"""Tests for the schema/dataset insight engine and multi-CSV merge."""
from app.intelligence.insights import summarize_schema, merge_schemas as _merge_schemas


def test_summarize_detects_domain_and_counts():
    schema = {"tables": [
        {"name": "customer_accounts", "columns": [{"name": "customer_id"}, {"name": "email"}]},
        {"name": "invoices", "columns": [{"name": "bill_date"}, {"name": "amount"}, {"name": "tax"}]},
    ]}
    out = summarize_schema(schema, name="billing_db")
    assert out["table_count"] == 2 and out["column_count"] == 5
    assert out["domain"] in ("Customer / CRM", "Billing / Finance")
    assert "customer_accounts" in out["key_entities"]
    assert "tables" in out["summary"]


def test_summarize_empty():
    out = summarize_schema({"tables": []})
    assert out["table_count"] == 0 and "Empty" in out["summary"]


def test_telecom_domain():
    schema = {"tables": [{"name": "network_cells", "columns": [{"name": "cell_id"}, {"name": "bandwidth_mbps"}]}]}
    assert summarize_schema(schema)["domain"] == "Network / Telecom"


def test_merge_dedupes_by_table_name_last_wins():
    a = {"tables": [{"name": "cust", "columns": [{"name": "id"}]}]}
    b = {"tables": [{"name": "orders", "columns": [{"name": "oid"}]},
                    {"name": "CUST", "columns": [{"name": "id"}, {"name": "email"}]}]}  # replaces cust
    merged = _merge_schemas(a, b)
    names = [t["name"] for t in merged["tables"]]
    assert names == ["CUST", "orders"]  # order preserved, cust replaced (last wins), orders appended
    cust = next(t for t in merged["tables"] if t["name"].lower() == "cust")
    assert len(cust["columns"]) == 2  # the replacement
