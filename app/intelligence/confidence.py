"""
app/intelligence/confidence.py — deterministic confidence scoring
"""
from __future__ import annotations

import re
from typing import Dict, List

_VENDOR_PREFIXES = re.compile(
    r'^(frontier_|ftr_|vz_|verizon_|src_|tgt_|stg_|raw_|ods_|dw_|dwh_'
    r'|geo_|loc_|addr_|net_|nw_|svc_|srvc_|dim_|fact_|rpt_|acct_|cust_)',
    re.IGNORECASE,
)

_DOMAIN_ALIASES: Dict[str, str] = {
    "customer":        "client",
    "cust":            "client",
    "client":          "client",
    "subscriber":      "client",
    "sub":             "client",
    "acct":            "account",
    "account":         "account",
    "cust_name":       "client_name",
    "customer_name":   "client_name",
    "client_name":     "client_name",
    "subscriber_name": "client_name",
    "cust_id":         "customer_id",
    "customer_id":     "customer_id",
    "client_id":       "customer_id",
    "sub_id":          "customer_id",
    "subscriber_id":   "customer_id",
    "acct_id":         "account_id",
    "account_id":      "account_id",
    "cust_nbr":        "customer_id",
    "cust_num":        "customer_id",
    "addr":            "address",
    "address":         "address",
    "zip":             "postal_code",
    "postal_code":     "postal_code",
    "zipcode":         "postal_code",
    "state":           "state_code",
    "state_cd":        "state_code",
    "state_code":      "state_code",
    "phone":           "phone_number",
    "phone_no":        "phone_number",
    "phone_number":    "phone_number",
    "mobile":          "phone_number",
    "email":           "email_address",
    "email_addr":      "email_address",
    "email_address":   "email_address",
    "bill_amt":        "billing_amount",
    "billing_amount":  "billing_amount",
    "invoice_amt":     "billing_amount",
    "revenue":         "billing_amount",
    "plan":            "service_plan",
    "plan_name":       "service_plan",
    "service_plan":    "service_plan",
    "product":         "service_plan",
    "churn_risk":      "churn_risk_score",
    "churn_score":     "churn_risk_score",
    "churn_risk_score":"churn_risk_score",
    "risk_of_churn":   "churn_risk_score",
    "risk_of_churn_pct": "churn_risk_score",
    "device":          "device_id",
    "device_id":       "device_id",
    "equipment_id":    "device_id",
    "serial":          "serial_number",
    "serial_number":   "serial_number",
    "mac":             "mac_address",
    "mac_address":     "mac_address",
    "geo_zip":         "postal_code",
    "geo_zipcode":     "postal_code",
    "geo_postal":      "postal_code",
    "geo_state":       "state_code",
    "geo_city":        "city",
    "geo_country":     "country_code",
    "loc_zip":         "postal_code",
    "loc_state":       "state_code",
    "loc_city":        "city",
    "install_dt":      "install_date",
    "install_date":    "install_date",
    "activation_dt":   "activation_date",
    "activation_date": "activation_date",
    "created_dt":      "created_at",
    "created_at":      "created_at",
    "updated_dt":      "updated_at",
    "updated_at":      "updated_at",
    "autopay":         "auto_pay_enabled",
    "auto_pay":        "auto_pay_enabled",
    "auto_pay_enabled": "auto_pay_enabled",
    "auto_pay_flag":   "auto_pay_enabled",
    "vpn":             "vpn_enabled",
    "vpn_flag":        "vpn_enabled",
    "vpn_enabled":     "vpn_enabled",
    "fraud":           "fraud_alert",
    "fraud_flag":      "fraud_alert",
    "fraud_alert_flag":"fraud_alert",
    "fraud_alert":     "fraud_alert",
}


def _canonical_concept(name: str) -> str:
    bare = _strip_vendor(name).lower().strip()
    if bare in _DOMAIN_ALIASES:
        return _DOMAIN_ALIASES[bare]
    compact = bare.replace("_", "").replace("-", "")
    for alias_key, concept in _DOMAIN_ALIASES.items():
        if alias_key.replace("_", "") == compact:
            return concept
    return bare


def _strip_vendor(name: str) -> str:
    """Remove well-known vendor / layer prefixes."""
    n = name.strip()
    for _ in range(4):
        stripped = _VENDOR_PREFIXES.sub('', n)
        if stripped == n:
            break
        n = stripped
    return n


# Generic abbreviation + token-level synonym normalization. Applied per-token so
# it generalizes far beyond the hand-curated _DOMAIN_ALIASES (which still wins as
# an exact whole-name concept match). A mapping may expand to multiple words.
_TOKEN_MAP: Dict[str, str] = {
    # abbreviations
    "nbr": "number", "no": "number", "num": "number", "qty": "quantity",
    "amt": "amount", "dt": "date", "ts": "timestamp", "tmstmp": "timestamp",
    "desc": "description", "addr": "address", "cd": "code", "cde": "code",
    "nm": "name", "flg": "flag", "pct": "percent", "perc": "percent",
    "cnt": "count", "ind": "indicator", "fname": "first name", "lname": "last name",
    "dob": "birth date", "yr": "year", "mo": "month", "bal": "balance",
    "id": "id", "uid": "id", "uuid": "id", "pk": "id", "fk": "id",
    # domain synonyms (token level)
    "cust": "customer", "client": "customer", "subscriber": "customer", "sub": "customer",
    "acct": "account", "org": "organization", "mobile": "phone", "cell": "phone",
    "tel": "phone", "ph": "phone", "mail": "email", "zip": "postal",
    "zipcode": "postal", "st": "state", "ctry": "country", "cntry": "country",
    "prod": "product", "svc": "service", "srvc": "service", "txn": "transaction",
    "trans": "transaction",
}

# Tokens that carry no matching signal — dropped before comparison.
_STOPWORDS = {"the", "a", "of", "to", "field", "col", "column", "value", "val"}
# Vendor / warehouse-layer noise tokens — dropped (but NOT domain tokens like
# cust/acct/addr, which carry meaning and are normalized via _TOKEN_MAP).
_DROP_TOKENS = {"src", "tgt", "stg", "raw", "ods", "dw", "dwh", "vz", "verizon",
                "frontier", "ftr", "rpt", "dim", "fact"}


def _split_tokens(name: str) -> List[str]:
    """Split a name into lowercase word tokens — handles snake_case, kebab-case,
    camelCase, and letter/digit boundaries. (No prefix stripping here — domain
    prefixes like cust_/acct_ carry meaning and are normalized downstream.)"""
    s = re.sub(r'(?<=[a-z0-9])(?=[A-Z])', ' ', name)                    # camelCase
    s = re.sub(r'(?<=[A-Za-z])(?=[0-9])|(?<=[0-9])(?=[A-Za-z])', ' ', s)  # letter/digit
    return [p for p in re.split(r'[\s_\-./]+', s.lower()) if p]


def _norm_tokens(name: str) -> frozenset:
    """Normalized token SET (abbreviations + synonyms expanded; stopwords and
    vendor/layer noise tokens dropped)."""
    out: set = set()
    for tok in _split_tokens(name):
        if tok in _DROP_TOKENS:
            continue
        for sub in _TOKEN_MAP.get(tok, tok).split():
            if sub and sub not in _STOPWORDS and sub not in _DROP_TOKENS:
                out.add(sub)
    return frozenset(out)


def _name_score(src: str, tgt: str) -> float:
    """Boundary-safe name similarity using normalized token sets.

    Avoids char-substring false positives (e.g. 'id' inside 'valid') by matching
    on whole normalized tokens, and generalizes via per-token abbreviation/
    synonym expansion."""
    # 1) Exact canonical concept (domain alias dict) wins outright.
    c1 = _canonical_concept(src)
    c2 = _canonical_concept(tgt)
    if c1 and c2 and c1 == c2:
        return 1.0

    t1 = _norm_tokens(src)
    t2 = _norm_tokens(tgt)
    if not t1 or not t2:
        return 0.0
    if t1 == t2:
        return 1.0

    union = len(t1 | t2)
    jaccard = len(t1 & t2) / union if union else 0.0

    # Boundary-safe containment: one token set fully contained in the other.
    contain = 0.0
    if t1 <= t2 or t2 <= t1:
        smaller, larger = (t1, t2) if len(t1) <= len(t2) else (t2, t1)
        contain = 0.90 + 0.05 * (len(smaller) / max(len(larger), 1))

    n1, n2 = " ".join(sorted(t1)), " ".join(sorted(t2))
    try:
        from rapidfuzz import fuzz
        fuzzy = max(fuzz.token_set_ratio(n1, n2), fuzz.token_sort_ratio(n1, n2)) / 100.0
    except ImportError:
        a, b = n1.replace(" ", ""), n2.replace(" ", "")
        fuzzy = sum(ch in b for ch in a) / max(len(a), len(b), 1)

    # No shared concept (no token overlap or containment) → discount coincidental
    # character similarity, so short unrelated names (e.g. 'id' vs 'void') don't
    # score high off raw char overlap. Genuine typo-variants (>=0.85) survive.
    if jaccard == 0 and contain == 0 and fuzzy < 0.85:
        fuzzy *= 0.5

    return round(max(jaccard, contain, fuzzy), 4)


_TYPE_COMPAT = {
    ("STRING", "STRING"): 1.0, ("INT64", "INT64"): 1.0,
    ("FLOAT64", "FLOAT64"): 1.0, ("BOOLEAN", "BOOLEAN"): 1.0,
    ("DATE", "DATE"): 1.0, ("TIMESTAMP", "TIMESTAMP"): 1.0,
    ("NUMERIC", "NUMERIC"): 1.0, ("BYTES", "BYTES"): 1.0,
    ("INT64", "FLOAT64"): 0.8,  ("FLOAT64", "INT64"): 0.7,
    ("INT64", "NUMERIC"): 0.85, ("FLOAT64", "NUMERIC"): 0.9,
    ("NUMERIC", "FLOAT64"): 0.9, ("NUMERIC", "INT64"): 0.8,
    ("INT64", "STRING"): 0.5,   ("STRING", "INT64"): 0.4,
    ("FLOAT64", "STRING"): 0.5, ("STRING", "FLOAT64"): 0.4,
    ("NUMERIC", "STRING"): 0.5, ("STRING", "NUMERIC"): 0.4,
    ("DATE", "TIMESTAMP"): 0.9, ("TIMESTAMP", "DATE"): 0.8,
    ("STRING", "DATE"): 0.5,    ("STRING", "TIMESTAMP"): 0.5,
    ("DATE", "STRING"): 0.5,    ("TIMESTAMP", "STRING"): 0.5,
    ("STRING", "BOOLEAN"): 0.4, ("BOOLEAN", "STRING"): 0.5,
    ("FLOAT64", "BOOLEAN"): 0.2, ("INT64", "BOOLEAN"): 0.2,
}


def _type_score(src_type: str, tgt_type: str) -> float:
    from app.parsers.schema import _normalize_type
    key = (_normalize_type(src_type), _normalize_type(tgt_type))
    return _TYPE_COMPAT.get(key, 0.3)


def compute_confidence(name_sim: float, type_sim: float, llm_score: float | None = None) -> float:
    """Weighted composite confidence score.

    When ``llm_score`` is None (deterministic-only — no LLM signal), the name/type
    weights are renormalized (0.60 / 0.40) so strong structural matches aren't
    capped by a missing 0.50 LLM term."""
    if llm_score is None:
        base = name_sim * 0.60 + type_sim * 0.40
    else:
        base = name_sim * 0.30 + type_sim * 0.20 + llm_score * 0.50
    if name_sim >= 0.80 and type_sim >= 0.80:
        base = max(base, 0.82)
    if name_sim >= 0.90 and type_sim >= 0.90:
        base = max(base, 0.90)
    if name_sim < 0.20:
        base = min(base, 0.78)
    return round(base, 3)


def conf_tier(score: float) -> str:
    if score >= 0.80:
        return "high"
    if score >= 0.50:
        return "medium"
    if score > 0:
        return "low"
    return "none"


def _recompute_relation_types(mappings: List[Dict]) -> None:
    """Deterministically set mapping_relation on every row based on actual fan-out counts."""
    src_to_tgts: Dict[str, set] = {}
    tgt_to_srcs: Dict[str, set] = {}

    for m in mappings:
        if m.get("status") == "unmapped" or not m.get("tgt_column"):
            continue
        sf  = m["src_field"]
        tgt = f"{m.get('tgt_table', '')}.{m.get('tgt_column', '')}"
        src_to_tgts.setdefault(sf, set()).add(tgt)
        tgt_to_srcs.setdefault(tgt, set()).add(sf)

    for m in mappings:
        if m.get("status") == "unmapped" or not m.get("tgt_column"):
            m["mapping_relation"] = "1:1"
            continue
        sf     = m["src_field"]
        tgt    = f"{m.get('tgt_table', '')}.{m.get('tgt_column', '')}"
        n_tgts = len(src_to_tgts.get(sf, set()))
        n_srcs = len(tgt_to_srcs.get(tgt, set()))
        if n_tgts > 1 and n_srcs > 1:
            m["mapping_relation"] = "M:M"
        elif n_tgts > 1:
            m["mapping_relation"] = "1:M"
        elif n_srcs > 1:
            m["mapping_relation"] = "M:1"
        else:
            m["mapping_relation"] = "1:1"
