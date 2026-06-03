"""Tests for tolerant normalization of LLM table-pair suggestions."""
from app.routers.mappings import _normalize_pair


def test_canonical_keys():
    p = _normalize_pair({"src_table": "ORDERS", "tgt_table": "DIM_ORDERS",
                         "relation": "1:M", "reason": "ok"})
    assert p == {"src_table": "ORDERS", "tgt_table": "DIM_ORDERS",
                 "relation": "1:M", "reason": "ok"}


def test_alternate_key_names_source_target():
    # Models often answer with source/target instead of src_table/tgt_table.
    p = _normalize_pair({"source": "CUST", "target": "DIM_CUSTOMER"})
    assert p["src_table"] == "CUST" and p["tgt_table"] == "DIM_CUSTOMER"
    assert p["relation"] == "1:1"  # default when unspecified


def test_alternate_relation_and_reason_keys():
    p = _normalize_pair({"source_table": "A", "target_table": "B",
                         "relation_type": "M:1", "rationale": "merge"})
    assert p["relation"] == "M:1" and p["reason"] == "merge"


def test_invalid_relation_falls_back():
    p = _normalize_pair({"src": "A", "tgt": "B", "relation": "one-to-many"})
    assert p["relation"] == "1:1"


def test_missing_side_returns_none():
    assert _normalize_pair({"src_table": "A"}) is None
    assert _normalize_pair({"target": "B"}) is None
    assert _normalize_pair("not a dict") is None
