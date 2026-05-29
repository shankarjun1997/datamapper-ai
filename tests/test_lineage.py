"""Tests for Lineage & Impact Analysis (#4/#5)."""
from app.intelligence import lineage as lg

MAPPINGS = [
    {"src_table": "cust", "src_field": "id", "tgt_table": "party", "tgt_column": "customer_ref",
     "mapping_type": "exact", "confidence": 0.98, "status": "approved"},
    {"src_table": "cust", "src_field": "name", "tgt_table": "party", "tgt_column": "full_name",
     "mapping_type": "fuzzy", "confidence": 0.9, "status": "approved", "business_logic": "TRIM(name)"},
    {"src_table": "orders", "src_field": "cust_id", "tgt_table": "party", "tgt_column": "customer_ref",
     "mapping_type": "exact", "confidence": 0.95, "status": "approved"},
    {"src_table": "cust", "src_field": "tmp", "tgt_table": "", "tgt_column": "", "status": "no_mapping"},
]


def test_build_lineage_graph():
    g = lg.build_lineage(MAPPINGS)
    assert g["stats"]["mappings"] == 3          # no_mapping excluded
    assert g["stats"]["source_tables"] == 2     # cust, orders
    assert g["stats"]["target_tables"] == 1     # party
    # nodes are deduped; customer_ref appears once as a target node
    tgt_nodes = [n for n in g["nodes"] if n["side"] == "target"]
    assert any(n["column"] == "customer_ref" for n in tgt_nodes)


def test_impact_forward_from_source():
    r = lg.impact(MAPPINGS, "cust.id", "forward")
    assert r["affected_count"] == 1
    assert r["affected_columns"][0]["column"] == "customer_ref"
    assert r["affected_tables"] == ["party"]


def test_impact_forward_whole_table():
    r = lg.impact(MAPPINGS, "cust", "forward")
    # cust.id and cust.name are active (tmp is no_mapping)
    assert r["affected_count"] == 2


def test_impact_reverse_from_target():
    # Which sources feed party.customer_ref? cust.id and orders.cust_id
    r = lg.impact(MAPPINGS, "party.customer_ref", "reverse")
    assert r["affected_count"] == 2
    cols = {a["column"] for a in r["affected_columns"]}
    assert cols == {"id", "cust_id"}


def test_impact_case_insensitive():
    r = lg.impact(MAPPINGS, "CUST.ID", "forward")
    assert r["affected_count"] == 1
