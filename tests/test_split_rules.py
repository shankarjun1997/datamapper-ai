"""Tests for derived-split mapping rules (full name → first + last, 1:M)."""
from app.intelligence.confidence import (
    _is_full_name, _name_part, apply_split_rules, _recompute_relation_types,
)


def test_full_name_detection():
    assert _is_full_name("customer_name") is True
    assert _is_full_name("full_name") is True
    assert _is_full_name("contactName") is True
    assert _is_full_name("first_name") is False   # a part, not the parent
    assert _is_full_name("email") is False


def test_name_part_classification():
    assert _name_part("first_name") == "first"
    assert _name_part("fname") == "first"
    assert _name_part("given_name") == "first"
    assert _name_part("last_name") == "last"
    assert _name_part("surname") == "last"
    assert _name_part("email") is None


SRC = [{"name": "customers", "columns": [
    {"name": "customer_name", "type": "STRING"}, {"name": "email", "type": "STRING"}]}]
TGT = [{"name": "party", "columns": [
    {"name": "first_name", "type": "STRING"}, {"name": "last_name", "type": "STRING"},
    {"name": "email_address", "type": "STRING"}]}]


def test_split_creates_first_and_last_at_full_confidence():
    mappings = [{"src_table": "customers", "src_field": "email", "tgt_table": "party",
                 "tgt_column": "email_address", "status": "mapped", "confidence": 0.95}]
    apply_split_rules(mappings, SRC, TGT)
    splits = [m for m in mappings if m["src_field"] == "customer_name"]
    assert {m["tgt_column"] for m in splits} == {"first_name", "last_name"}
    for m in splits:
        assert m["confidence"] == 1.0
        assert m["mapping_type"] == "Derived"
        assert m["status"] == "mapped"
        assert "SPLIT(" in m["business_logic"]


def test_relation_is_1_to_m_after_recompute():
    mappings = []
    apply_split_rules(mappings, SRC, TGT)
    _recompute_relation_types(mappings)
    splits = [m for m in mappings if m["src_field"] == "customer_name"]
    assert len(splits) == 2
    assert all(m["mapping_relation"] == "1:M" for m in splits)


def test_upgrades_existing_weak_row():
    mappings = [{"src_table": "customers", "src_field": "customer_name", "tgt_table": "party",
                 "tgt_column": "first_name", "status": "review", "confidence": 0.4,
                 "mapping_type": "Direct", "business_logic": ""}]
    apply_split_rules(mappings, SRC, TGT)
    row = next(m for m in mappings if m["tgt_column"] == "first_name")
    assert row["confidence"] == 1.0 and row["mapping_type"] == "Derived"
    assert "SPLIT(" in row["business_logic"]


def test_idempotent():
    mappings = []
    apply_split_rules(mappings, SRC, TGT)
    n = len(mappings)
    apply_split_rules(mappings, SRC, TGT)  # second pass shouldn't duplicate
    assert len(mappings) == n
