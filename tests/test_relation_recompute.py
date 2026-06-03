"""Regression tests for _recompute_relation_types — table-scoped fan-out."""
from app.intelligence.confidence import _recompute_relation_types


def _rel(mappings, src_field, tgt_table):
    return next(m["mapping_relation"] for m in mappings
                if m["src_field"] == src_field and m["tgt_table"] == tgt_table)


def test_same_column_in_two_target_tables_is_1to1_not_1toM():
    # customer_id -> cust_master.customer_id AND fact_support.customer_id.
    # Each pair is its own 1:1; the old global keying wrongly made both 1:M.
    mappings = [
        {"src_table": "cust", "src_field": "customer_id", "tgt_table": "cust_master",
         "tgt_column": "customer_id", "status": "mapped"},
        {"src_table": "cust", "src_field": "customer_id", "tgt_table": "fact_support",
         "tgt_column": "customer_id", "status": "mapped"},
    ]
    _recompute_relation_types(mappings)
    assert _rel(mappings, "customer_id", "cust_master") == "1:1"
    assert _rel(mappings, "customer_id", "fact_support") == "1:1"


def test_split_stays_1toM_even_when_target_shared_with_another_source():
    # customer_name AND contact_name both split into first_name/last_name.
    # The old logic collapsed these to M:M; a split must read as 1:M.
    mappings = [
        {"src_table": "cust", "src_field": "customer_name", "tgt_table": "cust_master",
         "tgt_column": "first_name", "status": "mapped"},
        {"src_table": "cust", "src_field": "customer_name", "tgt_table": "cust_master",
         "tgt_column": "last_name", "status": "mapped"},
        {"src_table": "cust", "src_field": "contact_name", "tgt_table": "cust_master",
         "tgt_column": "first_name", "status": "mapped"},
        {"src_table": "cust", "src_field": "contact_name", "tgt_table": "cust_master",
         "tgt_column": "last_name", "status": "mapped"},
    ]
    _recompute_relation_types(mappings)
    for m in mappings:
        assert m["mapping_relation"] == "1:M", m


def test_combine_is_Mto1():
    # first_name + last_name -> full_name (two sources into one target column).
    mappings = [
        {"src_table": "p", "src_field": "first_name", "tgt_table": "party",
         "tgt_column": "full_name", "status": "mapped"},
        {"src_table": "p", "src_field": "last_name", "tgt_table": "party",
         "tgt_column": "full_name", "status": "mapped"},
    ]
    _recompute_relation_types(mappings)
    assert all(m["mapping_relation"] == "M:1" for m in mappings)


def test_direct_single_is_1to1():
    mappings = [{"src_table": "p", "src_field": "email", "tgt_table": "party",
                 "tgt_column": "email", "status": "mapped"}]
    _recompute_relation_types(mappings)
    assert mappings[0]["mapping_relation"] == "1:1"


def test_locked_relation_is_preserved():
    mappings = [
        {"src_table": "p", "src_field": "a", "tgt_table": "t", "tgt_column": "x",
         "status": "mapped", "mapping_relation": "M:M", "relation_locked": True},
        {"src_table": "p", "src_field": "a", "tgt_table": "t", "tgt_column": "y",
         "status": "mapped"},
    ]
    _recompute_relation_types(mappings)
    # locked row keeps its manual M:M; the unlocked sibling recomputes to 1:M
    assert mappings[0]["mapping_relation"] == "M:M"
    assert mappings[1]["mapping_relation"] == "1:M"
