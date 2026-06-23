"""
app/intelligence/source_infer.py — derive a SOURCE schema from free-form context
(a Jira story, a pasted description, a flat note) and SUGGEST a TARGET schema.

The point: you should be able to map without ever connecting to a source
database. Drop in a file you already have, or paste the context that describes
the data — a Jira ticket, an email, a data dictionary blurb — and the LLM infers
the source fields. When you don't have a target model either, it proposes one.

Everything funnels through tolerant normalizers that coerce whatever shape the
LLM returns into the platform's canonical schema structure:

    {"tables": [{"name": str,
                 "columns": [{"name": str, "type": str, "sample": str, "nullable": bool}, ...]}, ...]}

The normalizers are pure and unit-tested; the LLM call is a thin wrapper around
them, so a malformed model response degrades gracefully instead of crashing.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List

# Canonical BigQuery-ish type vocabulary we coerce loose type names into.
_TYPE_CANON = {
    "str": "STRING", "string": "STRING", "text": "STRING", "varchar": "STRING",
    "char": "STRING", "nvarchar": "STRING", "uuid": "STRING", "char varying": "STRING",
    "int": "INT64", "integer": "INT64", "bigint": "INT64", "smallint": "INT64",
    "int64": "INT64", "number": "NUMERIC", "numeric": "NUMERIC", "decimal": "NUMERIC",
    "money": "NUMERIC", "float": "FLOAT64", "double": "FLOAT64", "real": "FLOAT64",
    "float64": "FLOAT64", "bool": "BOOL", "boolean": "BOOL",
    "date": "DATE", "datetime": "TIMESTAMP", "timestamp": "TIMESTAMP",
    "time": "TIME", "json": "JSON", "jsonb": "JSON", "bytes": "BYTES",
}

def _truthy(v: Any) -> bool:
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("true", "yes", "y", "t", "1", "required")


_NAME_KEYS = ("name", "field", "column", "field_name", "column_name", "col")
_TYPE_KEYS = ("type", "data_type", "datatype", "dtype", "column_type")
_NULL_KEYS = ("nullable", "null", "is_nullable", "required")
_COLS_KEYS = ("columns", "fields", "cols", "schema")


def _canon_type(raw: Any) -> str:
    t = str(raw or "").strip().lower()
    if not t:
        return "STRING"
    base = re.split(r"[(\s]", t, 1)[0]            # strip length/precision: varchar(120) -> varchar
    return _TYPE_CANON.get(base, _TYPE_CANON.get(t, raw if str(raw).isupper() else "STRING"))


def _first(d: dict, keys) -> Any:
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return None


def _coerce_column(col: Any) -> Dict | None:
    """Accept a string, or a dict in any of the common shapes, -> canonical column."""
    if isinstance(col, str):
        name = col.strip()
        return {"name": name, "type": "STRING", "sample": "", "nullable": True} if name else None
    if not isinstance(col, dict):
        return None
    name = _first(col, _NAME_KEYS)
    if not name:
        return None
    # 'required' is the inverse of nullable; 'nullable'/'is_nullable' is direct.
    req = _first(col, ("required",))
    nul = _first(col, ("nullable", "null", "is_nullable"))
    if req is not None:
        nullable = not _truthy(req)
    elif nul is not None:
        nullable = _truthy(nul) if isinstance(nul, bool) else (
            str(nul).strip().upper() not in ("NO", "NOT NULL", "FALSE", "0"))
    else:
        nullable = True
    return {
        "name": str(name).strip(),
        "type": _canon_type(_first(col, _TYPE_KEYS)),
        "sample": str(_first(col, ("sample", "example", "sample_value")) or ""),
        "nullable": nullable,
    }


def _coerce_columns(raw_cols: Any) -> List[Dict]:
    out = []
    for c in (raw_cols or []):
        cc = _coerce_column(c)
        if cc:
            out.append(cc)
    return out


def normalize_schema(raw: Any, default_name: str = "source") -> Dict:
    """Coerce tolerant LLM output into {"tables":[{name, columns:[...]}]}.

    Handles: {"tables":[...]}, a bare list of tables, a single {"name","columns"}
    table, {"columns":[...]} with no table name, or a flat list of columns.
    """
    tables: List[Dict] = []

    def add_table(name, cols):
        cols = _coerce_columns(cols)
        if cols:
            tables.append({"name": str(name or default_name).strip() or default_name, "columns": cols})

    if isinstance(raw, dict):
        if any(k in raw for k in _COLS_KEYS) and "tables" not in raw:
            # single table or a flat column list under "columns"/"fields"
            cols = _first(raw, _COLS_KEYS)
            add_table(_first(raw, _NAME_KEYS) or default_name, cols)
        else:
            container = raw.get("tables") or raw.get("entities") or []
            if isinstance(container, dict):           # {tableName: [cols...]}
                for tname, cols in container.items():
                    add_table(tname, cols.get("columns") if isinstance(cols, dict) else cols)
            else:
                for t in container:
                    if isinstance(t, dict):
                        add_table(_first(t, _NAME_KEYS) or default_name, _first(t, _COLS_KEYS))
    elif isinstance(raw, list):
        # list of tables, or a flat list of columns
        if raw and isinstance(raw[0], dict) and any(k in raw[0] for k in _COLS_KEYS):
            for t in raw:
                add_table(_first(t, _NAME_KEYS) or default_name, _first(t, _COLS_KEYS))
        else:
            add_table(default_name, raw)

    return {"tables": tables}


# ── Prompt builders ───────────────────────────────────────────────────────────
_SOURCE_SYS = (
    "You are a senior data engineer. From the unstructured context provided "
    "(a Jira story, a description, notes, or a flat extract), infer the SOURCE "
    "data schema: the table(s) and their columns with best-guess data types. "
    "Use telecom/enterprise conventions when the domain is clear. "
    "Return ONLY JSON (no prose, no markdown fence) shaped exactly as: "
    '{"tables":[{"name":"...","columns":[{"name":"...","type":"STRING","nullable":true}]}]}. '
    "Types must be one of STRING, INT64, NUMERIC, FLOAT64, BOOL, DATE, TIMESTAMP, JSON. "
    "If only one table is implied, return a single table. Prefer snake_case column names."
)

_SP_SOURCE_SYS = (
    "You are a senior data engineer analysing SQL stored procedures and functions. "
    "Read the SQL file below — it contains CREATE PROCEDURE / CREATE FUNCTION "
    "definitions and possibly CREATE TABLE statements. "
    "Analyse every SELECT, INSERT, UPDATE, DELETE, MERGE, and DECLARE statement "
    "inside the procedure bodies to discover the tables and columns the code "
    "operates on. Infer the full SOURCE data schema for every table referenced: "
    "table name, each column with its name, best-guess BigQuery data type, and "
    "whether it is nullable. "
    "Return ONLY JSON (no prose, no markdown fence) shaped exactly as: "
    '{"tables":[{"name":"...","columns":[{"name":"...","type":"STRING","nullable":true}]}]}. '
    "Types must be one of STRING, INT64, NUMERIC, FLOAT64, BOOL, DATE, TIMESTAMP, JSON. "
    "Prefer snake_case column names."
)

_TARGET_SYS = (
    "You are a senior data architect designing a clean target schema for a data "
    "migration. Given the SOURCE tables/columns and optional context, propose the "
    "TARGET table(s) and columns that the source should map into — normalized, "
    "warehouse-friendly (BigQuery), with sensible names and types. Favor names that "
    "are close to the source so mapping is high-confidence, but normalize where it "
    "obviously helps (split full names, separate address parts, surrogate keys). "
    "Return ONLY JSON shaped as "
    '{"tables":[{"name":"...","columns":[{"name":"...","type":"STRING","nullable":true}]}]}. '
    "Types: STRING, INT64, NUMERIC, FLOAT64, BOOL, DATE, TIMESTAMP, JSON."
)


def build_source_prompt(context_text: str, source_name: str = "") -> str:
    hint = f"Suggested source table name: {source_name}\n\n" if source_name else ""
    return f"{hint}CONTEXT:\n{context_text.strip()}\n\nReturn the inferred source schema JSON."


def build_target_prompt(schema_data: Dict, context_text: str = "", instructions: str = "") -> str:
    lines = []
    for t in (schema_data or {}).get("tables", []):
        cols = ", ".join(f"{c['name']} {c.get('type', 'STRING')}" for c in t.get("columns", []))
        lines.append(f"  {t.get('name', 'source')}: [{cols}]")
    src_block = "\n".join(lines) or "  (no source columns provided)"
    extra = f"\n\nUSER INSTRUCTIONS (treat as ground truth):\n{instructions.strip()}" if instructions else ""
    ctx = f"\n\nADDITIONAL CONTEXT:\n{context_text.strip()}" if context_text else ""
    return f"SOURCE TABLES:\n{src_block}{ctx}{extra}\n\nReturn the proposed target schema JSON."
