"""Tests for data-aware mapping validation (transform simulation + grading)."""
from app.intelligence.data_validation import (
    castable,
    simulate_transform,
    validate_mapping_row,
    validate_session,
)


# ── simulator ─────────────────────────────────────────────────────────────────
def test_simulate_split_first_and_last():
    row = {"customer_name": "JOHN SMITH"}
    assert simulate_transform("SPLIT(customer_name, ' ')[OFFSET(0)]", row) == "JOHN"
    assert simulate_transform("SPLIT(customer_name, ' ')[SAFE_OFFSET(1)]", row) == "SMITH"


def test_simulate_safe_offset_out_of_range_is_empty():
    assert simulate_transform("SPLIT(n, ' ')[SAFE_OFFSET(1)]", {"n": "MADONNA"}) == ""


def test_simulate_trim_split():
    out = simulate_transform("TRIM(SPLIT(addr, ',')[SAFE_OFFSET(1)])",
                             {"addr": "12 Main St, Springfield, IL"})
    assert out == "Springfield"


def test_simulate_concat_with_literal():
    out = simulate_transform("CONCAT(first_name, ' ', last_name)",
                             {"first_name": "Jane", "last_name": "Doe"})
    assert out == "Jane Doe"


def test_simulate_direct_and_unknown():
    assert simulate_transform("", {"email": "a@b.com"}) == "a@b.com"
    assert simulate_transform("REGEXP_EXTRACT(x, 'foo')", {"x": "1"}) is None


# ── castability ───────────────────────────────────────────────────────────────
def test_castable_matrix():
    assert castable("42", "INT64") and not castable("42.5", "INT64")
    assert castable("42.5", "NUMERIC") and not castable("abc", "FLOAT64")
    assert castable("2026-06-07", "DATE") and not castable("not a date", "TIMESTAMP")
    assert castable("yes", "BOOL") and not castable("maybe", "BOOL")
    assert castable("", "INT64")          # nulls are neutral
    assert castable("anything", "STRING")


# ── per-row grading ───────────────────────────────────────────────────────────
SPLIT_MAP = {"src_table": "cust", "src_field": "customer_name", "tgt_table": "t",
             "tgt_column": "first_name", "tgt_type": "STRING",
             "business_logic": "SPLIT(customer_name, ' ')[OFFSET(0)]", "status": "mapped"}


def test_split_full_coverage_passes_with_examples():
    rows = [{"customer_name": "JOHN SMITH"}, {"customer_name": "MARY ANN LEE"}]
    r = validate_mapping_row(dict(SPLIT_MAP), rows)
    assert r["status"] == "pass"
    assert r["examples"][0]["out"] == "JOHN"
    assert any(c["name"] == "split_coverage" and c["status"] == "pass" for c in r["checks"])


def test_split_single_token_samples_fail():
    rows = [{"customer_name": "MADONNA"}, {"customer_name": "CHER"}]
    r = validate_mapping_row({**SPLIT_MAP, "business_logic": "SPLIT(customer_name, ' ')[SAFE_OFFSET(1)]",
                              "tgt_column": "last_name"}, rows)
    assert r["status"] == "fail"          # 0% of samples have a second token


def test_type_cast_failure_detected():
    m = {"src_table": "c", "src_field": "zip_code", "tgt_table": "t",
         "tgt_column": "postal_num", "tgt_type": "INT64", "business_logic": "", "status": "mapped"}
    r = validate_mapping_row(m, [{"zip_code": "AB-123"}, {"zip_code": "99501"}])
    cast = next(c for c in r["checks"] if c["name"] == "type_cast")
    assert cast["status"] == "fail"       # 50% castable < 80%


def test_email_pattern_check():
    m = {"src_table": "c", "src_field": "contact", "tgt_table": "t",
         "tgt_column": "email_address", "tgt_type": "STRING", "business_logic": "", "status": "mapped"}
    r = validate_mapping_row(m, [{"contact": "a@b.com"}, {"contact": "c@d.io"}])
    assert any(c["name"] == "email_pattern" and c["status"] == "pass" for c in r["checks"])


def test_no_samples_is_no_data():
    assert validate_mapping_row(dict(SPLIT_MAP), [])["status"] == "no_data"


# ── session report ────────────────────────────────────────────────────────────
def test_validate_session_uses_uploaded_rows_and_schema_samples():
    session = {
        "schema_data": {"tables": [
            {"name": "orders", "columns": [{"name": "order_total", "type": "STRING", "sample": "12.50"}]}]},
        "sample_data": {"cust": {"customer_name": ["JOHN SMITH", "JANE DOE"]}},
        "mappings": [
            dict(SPLIT_MAP),
            {"id": "2", "src_table": "orders", "src_field": "order_total", "tgt_table": "t",
             "tgt_column": "total_amount", "tgt_type": "NUMERIC", "business_logic": "", "status": "mapped"},
            {"id": "3", "src_table": "ghost", "src_field": "x", "tgt_table": "t",
             "tgt_column": "y", "tgt_type": "STRING", "business_logic": "", "status": "mapped"},
        ],
    }
    report = validate_session(session)
    s = report["summary"]
    assert s["total"] == 3
    assert s["pass"] == 2 and s["no_data"] == 1
    # validation status is stamped onto the mapping rows for the UI/STM doc
    assert session["mappings"][0]["validation"] == "pass"
    assert session["data_validation"]["rows"][0]["examples"]
