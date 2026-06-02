"""Tests for the enhanced deterministic name scoring + confidence (bundle #1)."""
from app.intelligence.confidence import (
    _name_score, _norm_tokens, compute_confidence, _type_score,
    detect_value_pattern, value_affinity, score_table_pair, match_tables,
    rank_column_matches,
)


def test_token_normalization_abbreviations():
    # cust_nm and customer_name normalize to the same token set
    assert _norm_tokens("cust_nm") == _norm_tokens("customer_name")
    assert _norm_tokens("phone_no") == _norm_tokens("phone_number")
    assert _norm_tokens("acct_id") == _norm_tokens("account_id")


def test_camelcase_and_digit_splitting():
    assert _norm_tokens("customerID") == _norm_tokens("customer_id")
    assert "address" in _norm_tokens("addrLine1") and "1" in _norm_tokens("addrLine1")


def test_exact_normalized_match_scores_high():
    assert _name_score("cust_nm", "customer_name") >= 0.95
    assert _name_score("phone_no", "phone_number") >= 0.95


def test_substring_false_positive_is_fixed():
    # 'id' must NOT score ~0.92 inside 'valid'/'void' anymore
    assert _name_score("id", "valid") < 0.6
    assert _name_score("id", "void") < 0.6
    # but a real id<->identifier token match still scores
    assert _name_score("cust_id", "customer_id") >= 0.95


def test_partial_token_overlap_scores_mid():
    s = _name_score("billing_amount", "invoice_amount")
    assert 0.4 <= s < 1.0  # share 'amount', differ on billing/invoice


def test_unrelated_low_score():
    assert _name_score("first_name", "monthly_revenue") < 0.5


def test_deterministic_only_renormalizes():
    # No LLM: a strong name+type match should still reach 'high', not be capped
    det = compute_confidence(0.95, 0.9, None)
    with_zero_llm = compute_confidence(0.95, 0.9, 0.0)
    assert det > with_zero_llm
    assert det >= 0.80


def test_llm_path_unchanged():
    # Backward-compatible: passing an llm score keeps the 0.3/0.2/0.5 blend
    # (use mid values that don't trip the >=0.80 score floor)
    assert compute_confidence(0.5, 0.5, 0.5) == round(0.5*0.3 + 0.5*0.2 + 0.5*0.5, 3)


# ── #3 unified cross-platform type scoring ─────────────────────────────────────
def test_type_score_cross_platform():
    assert _type_score("NUMBER(38,0)", "INT64") == 1.0          # both INTEGER
    assert _type_score("VARCHAR2(50)", "STRING") == 1.0          # both STRING
    assert _type_score("TIMESTAMP", "DATE") == 0.9               # temporal compat
    assert _type_score("BOOLEAN", "VARCHAR(1)") == 0.5           # bool→string cast
    # narrowing precision is penalized
    assert _type_score("NUMBER(40,2)", "NUMBER(10,2)") < 1.0


# ── #2 value pattern signatures ────────────────────────────────────────────────
def test_value_pattern_detection():
    assert detect_value_pattern("a@b.com") == "email"
    assert detect_value_pattern("2026-05-29") == "date"
    assert detect_value_pattern("123") == "int"
    assert detect_value_pattern("") == ""


def test_value_affinity():
    assert value_affinity("a@b.com", "x@y.org") == 1.0          # both email
    assert value_affinity("a@b.com", "123") == 0.0              # different
    assert value_affinity("", "123") == 0.0                     # unknown → no signal


# ── #6 deterministic table matcher ─────────────────────────────────────────────
def test_table_matcher_prefers_column_overlap():
    src = [{"name": "cust", "columns": [{"name": "cust_id"}, {"name": "email"}, {"name": "phone_no"}]}]
    tgt = [
        {"name": "party", "columns": [{"name": "customer_id"}, {"name": "email_address"}, {"name": "phone_number"}]},
        {"name": "product", "columns": [{"name": "sku"}, {"name": "price"}]},
    ]
    pairs = match_tables(src, tgt)
    assert len(pairs) == 1
    assert pairs[0]["tgt_table"] == "party"      # not 'product'
    assert pairs[0]["score"] > score_table_pair(src[0], tgt[1])


# ── #8 blocking column matcher ─────────────────────────────────────────────────
def test_rank_column_matches_top_pick():
    src = [{"name": "cust_nm", "type": "VARCHAR(50)"}]
    tgt = [
        {"name": "customer_name", "type": "STRING"},
        {"name": "amount", "type": "NUMERIC"},
        {"name": "created_at", "type": "TIMESTAMP"},
    ]
    res = rank_column_matches(src, tgt, top_k=2)
    assert res["cust_nm"][0]["tgt_column"] == "customer_name"
    assert res["cust_nm"][0]["confidence"] >= 0.8
    assert len(res["cust_nm"]) <= 2
