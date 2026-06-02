"""Tests for the enhanced deterministic name scoring + confidence (bundle #1)."""
from app.intelligence.confidence import (
    _name_score, _norm_tokens, compute_confidence,
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
