"""
app/intelligence/business_logic.py — _auto_business_logic
"""
from __future__ import annotations

import re


def _auto_business_logic(
    src_field: str, src_type: str, tgt_type: str,
    mapping_type: str, mapping_relation: str,
    src_sample: str = "", tgt_name: str = "",
) -> str:
    """Generate a deterministic BQ SQL business_logic expression when LLM leaves it blank."""
    from app.parsers.schema import _normalize_type
    s = _normalize_type(src_type)
    t = _normalize_type(tgt_type)
    mt = (mapping_type or "Direct").strip()
    mr = (mapping_relation or "1:1").strip()

    if mt == "Unused":
        return ""
    if mt == "Constant":
        return "'<constant>'"
    if mt == "Lookup":
        return f"-- lookup via {src_field}"

    # Y/N string → BOOLEAN target
    if s == "STRING" and t == "BOOLEAN":
        sample_upper = (src_sample or "").strip().upper()
        yn_values = {"Y", "N", "YES", "NO", "TRUE", "FALSE", "1", "0", "T", "F"}
        if not sample_upper or sample_upper in yn_values:
            return (
                f"CASE UPPER(TRIM({src_field}))"
                f" WHEN 'Y' THEN TRUE WHEN 'YES' THEN TRUE WHEN '1' THEN TRUE WHEN 'T' THEN TRUE WHEN 'TRUE' THEN TRUE"
                f" WHEN 'N' THEN FALSE WHEN 'NO' THEN FALSE WHEN '0' THEN FALSE WHEN 'F' THEN FALSE WHEN 'FALSE' THEN FALSE"
                f" ELSE NULL END"
            )

    # Fraction → percentage
    _PCT_KEYWORDS = re.compile(r'pct|percent|rate|ratio', re.IGNORECASE)
    if s in ("FLOAT64", "NUMERIC") and t in ("FLOAT64", "NUMERIC"):
        tgt_is_pct = bool(_PCT_KEYWORDS.search(tgt_name or ""))
        try:
            src_val = float(src_sample)
            src_is_fraction = 0.0 <= src_val <= 1.0
        except (ValueError, TypeError):
            src_is_fraction = False
        if tgt_is_pct and src_is_fraction:
            return f"ROUND({src_field} * 100.0, 4)"

    # Generic type-cast rules
    if s != t:
        if t == "STRING":
            return f"CAST({src_field} AS STRING)"
        if t == "INT64":
            return f"CAST({src_field} AS INT64)"
        if t == "FLOAT64":
            return f"CAST({src_field} AS FLOAT64)"
        if t == "NUMERIC":
            return f"CAST({src_field} AS NUMERIC)"
        if t == "DATE":
            return f"DATE({src_field})" if s == "TIMESTAMP" else f"PARSE_DATE('%Y-%m-%d', CAST({src_field} AS STRING))"
        if t == "TIMESTAMP":
            return f"TIMESTAMP({src_field})"
        if t == "BOOLEAN":
            return f"CAST({src_field} AS BOOL)"

    if s == "STRING" and t == "STRING":
        if mt == "Derived":
            return f"UPPER(TRIM({src_field}))"
        return f"TRIM({src_field})"

    if mr == "M:1":
        return f"COALESCE({src_field}, NULL)"
    if mr == "1:M":
        return f"-- split: {src_field} fans out to multiple targets"

    return src_field
