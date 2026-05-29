"""Tests for the Migration Readiness engine (Layer 5)."""
from app.intelligence import migration_readiness as mr


def test_normalize_type_numeric_with_precision():
    n = mr.normalize_type("NUMBER(38,10)")
    assert n["category"] == mr.DECIMAL
    assert n["precision"] == 38 and n["scale"] == 10


def test_number_with_zero_scale_is_integer():
    assert mr.normalize_type("NUMBER(10,0)")["category"] == mr.INTEGER


def test_varchar_length_parsed():
    n = mr.normalize_type("VARCHAR2(255)")
    assert n["category"] == mr.STRING and n["length"] == 255


def test_oracle_number_to_snowflake_is_ready():
    # Directive's headline example.
    a = mr.assess("oracle", "NUMBER(38,10)", "snowflake", "NUMBER")
    assert a["readiness"] >= 90 and a["level"] == "ready"
    assert a["risks"] == []


def test_precision_overflow_is_flagged():
    a = mr.assess("oracle", "NUMBER(40,5)", "snowflake", "NUMBER")
    assert a["readiness"] < 90
    assert any("exceeds" in r for r in a["risks"])


def test_json_to_redshift_is_blocker():
    a = mr.assess("postgres", "JSONB", "redshift")
    assert a["level"] == "blocker"
    assert any("JSON" in r for r in a["risks"])


def test_boolean_to_oracle_flagged():
    a = mr.assess("postgres", "BOOLEAN", "oracle")
    assert any("BOOLEAN" in r for r in a["risks"])


def test_lossy_type_change_float_to_int():
    a = mr.assess("postgres", "DOUBLE PRECISION", "bigquery", "INT64")
    assert any("lossy" in r.lower() or "fractional" in r.lower() for r in a["risks"])
    assert a["readiness"] < 80


def test_recommend_target_type():
    assert "NUMERIC" in mr.recommend_target_type(mr.normalize_type("NUMBER(20,4)"), "bigquery")
    assert mr.recommend_target_type(mr.normalize_type("VARCHAR(50)"), "snowflake").startswith("VARCHAR")


def test_assess_session_rollup():
    mappings = [
        {"src_table": "cust", "src_field": "id", "src_type": "NUMBER(10,0)",
         "tgt_table": "party", "tgt_column": "id", "tgt_type": "INT64", "status": "approved"},
        {"src_table": "cust", "src_field": "meta", "src_type": "JSONB",
         "tgt_table": "party", "tgt_column": "meta", "status": "approved"},
        {"src_table": "cust", "src_field": "old", "src_type": "VARCHAR(10)",
         "tgt_table": "", "tgt_column": "", "status": "no_mapping"},  # skipped
    ]
    rep = mr.assess_session(mappings, "postgres", "redshift")
    assert rep["assessed_columns"] == 2  # no_mapping excluded
    assert rep["counts"]["blocker"] >= 1  # JSON→redshift
    assert 0 <= rep["overall_readiness"] <= 100
    assert rep["source_platform"] == "postgres" and rep["target_platform"] == "redshift"
