"""
app/intelligence/migration_readiness.py — Migration Intelligence (Layer 5).

A deterministic rules engine that answers "can this migrate, and how safely?"
*before* any ETL is written. It normalizes each platform's native data types to
a canonical category, then scores source→target compatibility (0–100) with
explicit risks and a recommended target type.

No external dependencies — pure, fully testable logic.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional

# ── Canonical type categories ──────────────────────────────────────────────────
INTEGER, DECIMAL, FLOAT = "INTEGER", "DECIMAL", "FLOAT"
STRING, TEXT = "STRING", "TEXT"
BOOLEAN, DATE, TIME = "BOOLEAN", "DATE", "TIME"
TIMESTAMP, TIMESTAMP_TZ = "TIMESTAMP", "TIMESTAMP_TZ"
BINARY, JSON, UUID, ARRAY, UNKNOWN = "BINARY", "JSON", "UUID", "ARRAY", "UNKNOWN"

NUMERIC_CATS = {INTEGER, DECIMAL, FLOAT}

# Native-type keyword → canonical category. Checked as substring (longest first).
_TYPE_KEYWORDS = [
    # order matters: more specific first
    ("timestamp_tz", TIMESTAMP_TZ), ("timestamptz", TIMESTAMP_TZ),
    ("timestamp with time zone", TIMESTAMP_TZ), ("timestamp_ltz", TIMESTAMP_TZ),
    ("datetimeoffset", TIMESTAMP_TZ),
    ("timestamp", TIMESTAMP), ("datetime2", TIMESTAMP), ("datetime", TIMESTAMP),
    ("smalldatetime", TIMESTAMP),
    ("date", DATE),
    ("time", TIME),
    ("bigint", INTEGER), ("smallint", INTEGER), ("tinyint", INTEGER),
    ("integer", INTEGER), ("int64", INTEGER), ("int", INTEGER),
    ("serial", INTEGER), ("number", DECIMAL),   # oracle NUMBER → decimal-ish
    ("numeric", DECIMAL), ("decimal", DECIMAL), ("money", DECIMAL),
    ("float64", FLOAT), ("double", FLOAT), ("float", FLOAT), ("real", FLOAT),
    ("binary_double", FLOAT), ("binary_float", FLOAT),
    ("boolean", BOOLEAN), ("bool", BOOLEAN), ("bit", BOOLEAN),
    ("uuid", UUID), ("uniqueidentifier", UUID),
    ("json", JSON), ("variant", JSON), ("jsonb", JSON), ("struct", JSON),
    ("array", ARRAY),
    ("text", TEXT), ("clob", TEXT), ("nclob", TEXT), ("string", STRING),
    ("longtext", TEXT), ("mediumtext", TEXT),
    ("varchar", STRING), ("nvarchar", STRING), ("char", STRING), ("varchar2", STRING),
    ("blob", BINARY), ("bytea", BINARY), ("varbinary", BINARY), ("binary", BINARY),
    ("bytes", BINARY), ("raw", BINARY),
]

# Per-platform capabilities (what the TARGET can represent).
_PLATFORM = {
    "oracle":     {"max_num_precision": 38, "max_varchar": 4000,   "json": True,  "array": False, "boolean": False, "tz": True},
    "sqlserver":  {"max_num_precision": 38, "max_varchar": 8000,   "json": True,  "array": False, "boolean": True,  "tz": True},
    "postgres":   {"max_num_precision": 1000,"max_varchar": 10485760,"json": True, "array": True,  "boolean": True,  "tz": True},
    "mysql":      {"max_num_precision": 65, "max_varchar": 65535,  "json": True,  "array": False, "boolean": True,  "tz": False},
    "snowflake":  {"max_num_precision": 38, "max_varchar": 16777216,"json": True, "array": True,  "boolean": True,  "tz": True},
    "bigquery":   {"max_num_precision": 76, "max_varchar": 0,      "json": True,  "array": True,  "boolean": True,  "tz": True},
    "redshift":   {"max_num_precision": 38, "max_varchar": 65535,  "json": False, "array": False, "boolean": True,  "tz": True},
    "databricks": {"max_num_precision": 38, "max_varchar": 0,      "json": True,  "array": True,  "boolean": True,  "tz": True},
    "generic":    {"max_num_precision": 38, "max_varchar": 65535,  "json": True,  "array": True,  "boolean": True,  "tz": True},
}

# Preferred native target type per platform + canonical category (for recommendations).
_RECOMMEND = {
    "snowflake": {INTEGER: "NUMBER(38,0)", DECIMAL: "NUMBER", FLOAT: "FLOAT", STRING: "VARCHAR",
                  TEXT: "VARCHAR", BOOLEAN: "BOOLEAN", DATE: "DATE", TIME: "TIME",
                  TIMESTAMP: "TIMESTAMP_NTZ", TIMESTAMP_TZ: "TIMESTAMP_TZ", BINARY: "BINARY",
                  JSON: "VARIANT", UUID: "VARCHAR(36)", ARRAY: "ARRAY"},
    "bigquery": {INTEGER: "INT64", DECIMAL: "NUMERIC", FLOAT: "FLOAT64", STRING: "STRING",
                 TEXT: "STRING", BOOLEAN: "BOOL", DATE: "DATE", TIME: "TIME",
                 TIMESTAMP: "DATETIME", TIMESTAMP_TZ: "TIMESTAMP", BINARY: "BYTES",
                 JSON: "JSON", UUID: "STRING", ARRAY: "ARRAY"},
    "postgres": {INTEGER: "BIGINT", DECIMAL: "NUMERIC", FLOAT: "DOUBLE PRECISION", STRING: "VARCHAR",
                 TEXT: "TEXT", BOOLEAN: "BOOLEAN", DATE: "DATE", TIME: "TIME",
                 TIMESTAMP: "TIMESTAMP", TIMESTAMP_TZ: "TIMESTAMPTZ", BINARY: "BYTEA",
                 JSON: "JSONB", UUID: "UUID", ARRAY: "ARRAY"},
    "redshift": {INTEGER: "BIGINT", DECIMAL: "DECIMAL", FLOAT: "DOUBLE PRECISION", STRING: "VARCHAR",
                 TEXT: "VARCHAR(65535)", BOOLEAN: "BOOLEAN", DATE: "DATE", TIME: "TIME",
                 TIMESTAMP: "TIMESTAMP", TIMESTAMP_TZ: "TIMESTAMPTZ", BINARY: "VARBYTE",
                 JSON: "SUPER", UUID: "VARCHAR(36)", ARRAY: "SUPER"},
    "databricks": {INTEGER: "BIGINT", DECIMAL: "DECIMAL", FLOAT: "DOUBLE", STRING: "STRING",
                   TEXT: "STRING", BOOLEAN: "BOOLEAN", DATE: "DATE", TIME: "STRING",
                   TIMESTAMP: "TIMESTAMP", TIMESTAMP_TZ: "TIMESTAMP", BINARY: "BINARY",
                   JSON: "STRING", UUID: "STRING", ARRAY: "ARRAY"},
}

_LEVELS = [(90, "ready"), (75, "review"), (60, "risk"), (0, "blocker")]


def _level(score: int) -> str:
    for threshold, name in _LEVELS:
        if score >= threshold:
            return name
    return "blocker"


def normalize_platform(p: Optional[str]) -> str:
    p = (p or "generic").strip().lower()
    aliases = {"postgresql": "postgres", "mssql": "sqlserver", "azuresql": "sqlserver",
               "bq": "bigquery", "google bigquery": "bigquery", "sql server": "sqlserver"}
    p = aliases.get(p, p)
    return p if p in _PLATFORM else "generic"


def normalize_type(type_str: Optional[str]) -> Dict:
    """Parse a native type string into a canonical descriptor."""
    raw = (type_str or "").strip()
    low = raw.lower()
    category = UNKNOWN
    for kw, cat in _TYPE_KEYWORDS:
        if kw in low:
            category = cat
            break
    # precision/scale/length from parentheses
    precision = scale = length = None
    m = re.search(r"\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)", raw)
    if m:
        n1 = int(m.group(1))
        n2 = int(m.group(2)) if m.group(2) else None
        if category in (DECIMAL, FLOAT, INTEGER):
            precision, scale = n1, (n2 if n2 is not None else 0)
        elif category in (STRING, TEXT, BINARY):
            length = n1
    # NUMBER(p,0) is really an integer
    if category == DECIMAL and scale == 0 and precision is not None:
        category = INTEGER
    return {"raw": raw, "category": category, "precision": precision, "scale": scale, "length": length}


def recommend_target_type(canon: Dict, target_platform: str) -> str:
    tp = normalize_platform(target_platform)
    table = _RECOMMEND.get(tp, _RECOMMEND["snowflake"])
    cat = canon["category"]
    base = table.get(cat, "STRING")
    if cat in (DECIMAL,) and canon.get("precision"):
        return f"{base}({canon['precision']},{canon.get('scale',0)})"
    if cat in (STRING,) and canon.get("length") and "(" not in base:
        return f"{base}({canon['length']})"
    return base


def assess(src_platform: str, src_type: str, tgt_platform: str,
           tgt_type: Optional[str] = None) -> Dict:
    """Assess one column's migration readiness. Returns score + risks + recommendation."""
    tp = normalize_platform(tgt_platform)
    src = normalize_type(src_type)
    caps = _PLATFORM[tp]
    risks: List[str] = []
    score = 100

    src_cat = src["category"]
    recommended = recommend_target_type(src, tp)
    tgt = normalize_type(tgt_type) if tgt_type else normalize_type(recommended)
    tgt_cat = tgt["category"]

    if src_cat == UNKNOWN:
        risks.append(f"Unrecognized source type '{src['raw']}' — manual review required.")
        score -= 35

    # Feature support on the target platform
    if src_cat == BOOLEAN and not caps["boolean"]:
        risks.append(f"{tp} has no native BOOLEAN — map to NUMBER/CHAR(1).")
        score -= 20
    if src_cat == JSON and not caps["json"]:
        risks.append(f"{tp} has no native JSON type — store as large VARCHAR or restructure.")
        score -= 45
    if src_cat == ARRAY and not caps["array"]:
        risks.append(f"{tp} has no native ARRAY type — flatten or serialize.")
        score -= 45
    if src_cat == TIMESTAMP_TZ and not caps["tz"]:
        risks.append(f"{tp} lacks timezone-aware timestamps — timezone will be lost.")
        score -= 25

    # Numeric precision
    if src_cat in (DECIMAL, INTEGER) and src.get("precision"):
        if src["precision"] > caps["max_num_precision"]:
            risks.append(f"Precision {src['precision']} exceeds {tp} max {caps['max_num_precision']} — possible truncation.")
            score -= 30

    # String length
    if src_cat in (STRING, TEXT) and src.get("length") and caps["max_varchar"]:
        if src["length"] > caps["max_varchar"]:
            risks.append(f"Length {src['length']} exceeds {tp} VARCHAR max {caps['max_varchar']} — use TEXT/CLOB equivalent.")
            score -= 15

    # Category change between explicit source and target (cast risk)
    if tgt_type and tgt_cat != UNKNOWN and tgt_cat != src_cat:
        lossy = (
            (src_cat in NUMERIC_CATS and tgt_cat in (STRING, TEXT)) or
            (src_cat in (STRING, TEXT) and tgt_cat in NUMERIC_CATS) or
            (src_cat in (TIMESTAMP, TIMESTAMP_TZ, DATE) and tgt_cat in (STRING, TEXT)) or
            (src_cat == FLOAT and tgt_cat == INTEGER)
        )
        if lossy:
            risks.append(f"Type change {src_cat}→{tgt_cat} is lossy — validate transformation & data.")
            score -= 25
        else:
            risks.append(f"Type change {src_cat}→{tgt_cat} requires a cast.")
            score -= 8
        if src_cat == FLOAT and tgt_cat == INTEGER:
            risks.append("Float→Integer drops the fractional component.")

    score = max(0, min(100, score))
    return {
        "source_type": src["raw"],
        "source_category": src_cat,
        "target_platform": tp,
        "target_type": (tgt_type or recommended),
        "recommended_type": recommended,
        "readiness": score,
        "level": _level(score),
        "risks": risks,
    }


def assess_session(mappings: List[Dict], src_platform: str = "generic",
                   tgt_platform: str = "generic") -> Dict:
    """Run readiness across a session's mappings and roll up an overall report."""
    columns = []
    counts = {"ready": 0, "review": 0, "risk": 0, "blocker": 0}
    blockers, top_risks = [], []
    total = 0
    for m in mappings or []:
        status = (m.get("status") or "").lower()
        if status in ("no_mapping", "skipped", "ignored"):
            continue
        if not (m.get("src_type") or m.get("tgt_type")):
            continue
        a = assess(src_platform, m.get("src_type", ""), tgt_platform, m.get("tgt_type"))
        total += a["readiness"]
        counts[a["level"]] += 1
        entry = {
            "src_table": m.get("src_table", ""), "src_field": m.get("src_field", ""),
            "tgt_table": m.get("tgt_table", ""), "tgt_column": m.get("tgt_column", ""),
            **a,
        }
        columns.append(entry)
        if a["level"] == "blocker":
            blockers.append(entry)
        for r in a["risks"]:
            top_risks.append({"column": f"{m.get('src_table','')}.{m.get('src_field','')}", "risk": r})

    n = len(columns)
    overall = round(total / n) if n else 0
    return {
        "source_platform": normalize_platform(src_platform),
        "target_platform": normalize_platform(tgt_platform),
        "overall_readiness": overall,
        "overall_level": _level(overall),
        "assessed_columns": n,
        "counts": counts,
        "blockers": blockers,
        "risks": top_risks[:100],
        "columns": columns,
    }
