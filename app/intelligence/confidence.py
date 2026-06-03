"""
app/intelligence/confidence.py — deterministic confidence scoring
"""
from __future__ import annotations

import re
import uuid
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


# Cross-platform category compatibility (keyed on canonical categories from
# migration_readiness). Symmetric — looked up both ways. Same category → 1.0.
_CAT_COMPAT = {
    ("INTEGER", "DECIMAL"): 0.9, ("INTEGER", "FLOAT"): 0.8, ("DECIMAL", "FLOAT"): 0.9,
    ("STRING", "TEXT"): 0.95,
    ("DATE", "TIMESTAMP"): 0.9, ("TIMESTAMP", "TIMESTAMP_TZ"): 0.9, ("DATE", "TIMESTAMP_TZ"): 0.8,
    ("UUID", "STRING"): 0.9, ("UUID", "TEXT"): 0.85,
    ("BOOLEAN", "INTEGER"): 0.6, ("BOOLEAN", "STRING"): 0.5,
    ("JSON", "STRING"): 0.7, ("JSON", "TEXT"): 0.75,
    # numeric/temporal ↔ text casts
    ("INTEGER", "STRING"): 0.5, ("DECIMAL", "STRING"): 0.5, ("FLOAT", "STRING"): 0.5,
    ("INTEGER", "TEXT"): 0.5, ("DECIMAL", "TEXT"): 0.5, ("FLOAT", "TEXT"): 0.5,
    ("DATE", "STRING"): 0.5, ("TIMESTAMP", "STRING"): 0.5, ("TIMESTAMP_TZ", "STRING"): 0.5,
    ("BINARY", "STRING"): 0.3, ("ARRAY", "STRING"): 0.4,
}


def _type_score(src_type: str, tgt_type: str) -> float:
    """Cross-platform type compatibility using canonical categories + precision.

    Reuses the migration-readiness type model so the score is correct across
    Oracle/Snowflake/BigQuery/etc. (not just BigQuery), and penalizes narrowing
    (precision/length shrink)."""
    from app.intelligence.migration_readiness import normalize_type, NUMERIC_CATS, UNKNOWN, STRING, TEXT
    s = normalize_type(src_type)
    t = normalize_type(tgt_type)
    sc, tc = s["category"], t["category"]

    if sc == UNKNOWN or tc == UNKNOWN:
        base = 0.3
    elif sc == tc:
        base = 1.0
    else:
        base = _CAT_COMPAT.get((sc, tc)) or _CAT_COMPAT.get((tc, sc)) or 0.3

    # Narrowing penalties (source wider than target).
    if sc in NUMERIC_CATS and tc in NUMERIC_CATS and s.get("precision") and t.get("precision"):
        if s["precision"] > t["precision"]:
            base *= 0.9
    if sc in (STRING, TEXT) and tc in (STRING, TEXT) and s.get("length") and t.get("length"):
        if s["length"] > t["length"]:
            base *= 0.95
    return round(base, 3)


def compute_confidence(name_sim: float, type_sim: float, llm_score: float | None = None,
                       value_sim: float | None = None) -> float:
    """Weighted composite confidence score.

    When ``llm_score`` is None (deterministic-only — no LLM signal), the name/type
    weights are renormalized so strong structural matches aren't capped by a
    missing 0.50 LLM term. An optional ``value_sim`` (shared sample-data pattern,
    0–1) adds a corroborating signal."""
    if llm_score is None:
        if value_sim is not None:
            base = name_sim * 0.50 + type_sim * 0.30 + value_sim * 0.20
        else:
            base = name_sim * 0.60 + type_sim * 0.40
    else:
        base = name_sim * 0.30 + type_sim * 0.20 + llm_score * 0.50
        if value_sim is not None:
            base = min(1.0, base + 0.08 * value_sim)  # small corroboration boost
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


# ─────────────────────────────────────────────────────────────────────────────
# (#2) Value / sample-data pattern signatures
# ─────────────────────────────────────────────────────────────────────────────
_VALUE_PATTERNS = [
    ("email",    re.compile(r'^[^@\s]+@[^@\s]+\.[^@\s]+$')),
    ("uuid",     re.compile(r'^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$')),
    ("date",     re.compile(r'^\d{4}-\d{2}-\d{2}([T ]\d{2}:\d{2})?')),
    ("currency", re.compile(r'^[$€£¥]\s?-?\d')),
    ("float",    re.compile(r'^-?\d+\.\d+$')),
    ("int",      re.compile(r'^-?\d+$')),
    ("bool",     re.compile(r'^(true|false|yes|no|y|n|t|f)$', re.IGNORECASE)),
    ("phone",    re.compile(r'^\+?[\d][\d\s().\-]{6,}$')),
]


def detect_value_pattern(sample) -> str:
    """Classify a sample value into a coarse data pattern, or '' if unknown."""
    s = str(sample if sample is not None else "").strip()
    if not s:
        return ""
    for name, rx in _VALUE_PATTERNS:
        if rx.match(s):
            return name
    return ""


def value_affinity(src_sample, tgt_sample) -> float:
    """1.0 if both samples share a detectable data pattern, else 0.0
    (0.0 when either is unknown — value signal only corroborates, never blocks)."""
    p1, p2 = detect_value_pattern(src_sample), detect_value_pattern(tgt_sample)
    if not p1 or not p2:
        return 0.0
    return 1.0 if p1 == p2 else 0.0


# ─────────────────────────────────────────────────────────────────────────────
# (#6/#7) Deterministic TABLE matcher — name similarity + column-set overlap
# ─────────────────────────────────────────────────────────────────────────────
def _table_name(t: Dict) -> str:
    return t.get("name") or t.get("table") or ""


def _column_fingerprint(t: Dict) -> frozenset:
    """Union of normalized column-name tokens — a table's 'schema fingerprint'."""
    fp: set = set()
    for c in t.get("columns", []) or []:
        fp |= _norm_tokens(c.get("name", ""))
    return frozenset(fp)


def score_table_pair(src_table: Dict, tgt_table: Dict) -> float:
    """Score a source→target table pairing: 40% name similarity + 60% Jaccard
    overlap of their column fingerprints (column overlap dominates — that's the
    real signal that two tables describe the same entity)."""
    name_s = _name_score(_table_name(src_table), _table_name(tgt_table))
    f1, f2 = _column_fingerprint(src_table), _column_fingerprint(tgt_table)
    union = len(f1 | f2)
    col_jacc = len(f1 & f2) / union if union else 0.0
    return round(0.40 * name_s + 0.60 * col_jacc, 4)


def match_tables(src_tables: List[Dict], tgt_tables: List[Dict],
                 threshold: float = 0.25) -> List[Dict]:
    """Propose the best target table for each source table (above threshold)."""
    out: List[Dict] = []
    for st in src_tables or []:
        best, best_s = None, 0.0
        for tt in tgt_tables or []:
            sc = score_table_pair(st, tt)
            if sc > best_s:
                best, best_s = tt, sc
        if best is not None and best_s >= threshold:
            out.append({"src_table": _table_name(st), "tgt_table": _table_name(best),
                        "score": best_s, "tier": conf_tier(best_s)})
    return out


# ─────────────────────────────────────────────────────────────────────────────
# (#8/#9) Blocking column matcher — scale to large schemas
# ─────────────────────────────────────────────────────────────────────────────
def _type_category(type_str: str) -> str:
    from app.intelligence.migration_readiness import normalize_type
    return normalize_type(type_str)["category"]


def rank_column_matches(src_cols: List[Dict], tgt_cols: List[Dict], top_k: int = 3) -> Dict:
    """Return the top-k target candidates per source column.

    Uses *blocking* (bucket targets by canonical type category) so we only score
    type-compatible candidates instead of the full O(N×M) cross-product — the key
    to scaling to 10k+ columns. Falls back to scanning all targets for source
    columns whose category has no compatible bucket."""
    # Pre-bucket targets by type category (+ a precomputed name cache).
    buckets: Dict[str, List[Dict]] = {}
    for tc in tgt_cols or []:
        buckets.setdefault(_type_category(tc.get("type", "")), []).append(tc)

    # Which target categories are worth scoring for a given source category.
    def candidate_targets(src_cat: str) -> List[Dict]:
        cats = {src_cat}
        for (a, b) in _CAT_COMPAT:
            if a == src_cat:
                cats.add(b)
            if b == src_cat:
                cats.add(a)
        cats.add("UNKNOWN")
        cands: List[Dict] = []
        for c in cats:
            cands.extend(buckets.get(c, []))
        return cands or (tgt_cols or [])  # safety: never empty if targets exist

    results: Dict[str, List] = {}
    for sc in src_cols or []:
        s_name, s_type = sc.get("name", ""), sc.get("type", "")
        scored = []
        for tc in candidate_targets(_type_category(s_type)):
            ns = _name_score(s_name, tc.get("name", ""))
            tsc = _type_score(s_type, tc.get("type", ""))
            scored.append((tc.get("name", ""), compute_confidence(ns, tsc, None), round(ns, 3)))
        scored.sort(key=lambda x: x[1], reverse=True)
        results[s_name] = [
            {"tgt_column": n, "confidence": c, "name_sim": ns, "tier": conf_tier(c)}
            for n, c, ns in scored[:top_k]
        ]
    return results


# ─────────────────────────────────────────────────────────────────────────────
# Derived-split rules (one source → many targets, 1:M) — e.g. full name splits
# deterministically into first_name + last_name at 100% confidence.
# ─────────────────────────────────────────────────────────────────────────────
_FULLNAME_TOKENS = {"customer", "client", "contact", "person", "full", "subscriber"}


def _is_full_name(field: str) -> bool:
    """True if a source field looks like a person's full name (the split parent)."""
    toks = _norm_tokens(field)
    if "name" not in toks:
        return False
    return bool(toks & _FULLNAME_TOKENS) or toks == {"name"} or toks <= {"full", "name"}


def _name_part(field: str):
    """Classify a target column as a 'first' or 'last' name part (else None)."""
    toks = _norm_tokens(field)
    if "first" in toks or "given" in toks or "forename" in toks:
        return "first"
    if "last" in toks or "surname" in toks or "family" in toks:
        return "last"
    return None


_SPLIT_SQL = {
    "first": "SPLIT({src}, ' ')[OFFSET(0)]",
    "last":  "SPLIT({src}, ' ')[SAFE_OFFSET(1)]",
}


def apply_split_rules(mappings: List[Dict], src_tables: List[Dict], tgt_tables: List[Dict]) -> List[Dict]:
    """Ensure known derived splits exist: a source full-name column maps to the
    target first_name AND last_name columns at 100% confidence as a 1:M Derived
    mapping (with a SPLIT() transform). Upgrades any existing weak row in place
    and appends missing parts. Idempotent."""
    tgt_parts: Dict[str, Dict[str, str]] = {}
    for t in tgt_tables or []:
        tname = t.get("name") or t.get("table") or ""
        for c in t.get("columns", []) or []:
            part = _name_part(c.get("name", ""))
            if part:
                tgt_parts.setdefault(tname, {})[part] = c.get("name", "")
    if not tgt_parts:
        return mappings

    src_to_tgt: Dict[str, Dict[str, int]] = {}
    for m in mappings:
        if m.get("tgt_table"):
            d = src_to_tgt.setdefault(m.get("src_table", ""), {})
            d[m["tgt_table"]] = d.get(m["tgt_table"], 0) + 1

    def primary_tgt(src_table: str):
        for tt in sorted(src_to_tgt.get(src_table, {}), key=lambda x: -src_to_tgt[src_table][x]):
            if tt in tgt_parts:
                return tt
        return next(iter(tgt_parts), None)

    existing = {(m.get("src_table"), m.get("src_field"), m.get("tgt_table"), m.get("tgt_column")): m
                for m in mappings}

    for st in src_tables or []:
        stable = st.get("name") or st.get("table") or ""
        for c in st.get("columns", []) or []:
            sfield = c.get("name", "")
            if not _is_full_name(sfield):
                continue
            ttable = primary_tgt(stable)
            if not ttable or ttable not in tgt_parts:
                continue
            for part, tcol in tgt_parts[ttable].items():
                sql = _SPLIT_SQL[part].format(src=sfield)
                key = (stable, sfield, ttable, tcol)
                if key in existing:
                    existing[key].update({"confidence": 1.0, "tier": "high", "mapping_type": "Derived",
                                          "mapping_relation": "1:M", "business_logic": sql, "status": "mapped"})
                else:
                    mappings.append({
                        "id": uuid.uuid4().hex[:8], "src_table": stable, "src_field": sfield,
                        "src_type": c.get("type", "STRING"), "tgt_table": ttable, "tgt_column": tcol,
                        "tgt_type": "STRING", "mapping_type": "Derived", "mapping_relation": "1:M",
                        "business_logic": sql, "confidence": 1.0, "tier": "high", "status": "mapped",
                        "rationale": "Derived split of full name into name parts.", "modified": False,
                    })
    return mappings
