"""
app/parsers/ddl.py — parse_ddl + _parse_ddl_column
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from app.parsers.schema import _normalize_type


def _parse_ddl_column(line: str) -> Optional[Dict]:
    """Parse one column definition; return None for constraint lines."""
    line = line.strip().rstrip(",").strip()
    if not line:
        return None
    if re.match(
        r"(PRIMARY\s+KEY|UNIQUE(\s+KEY)?|CHECK|FOREIGN\s+KEY|CONSTRAINT|"
        r"INDEX|KEY\s+\w|FULLTEXT|SPATIAL|\))",
        line, re.I,
    ):
        return None
    col_m = re.match(
        r"[`\"\[]?(\w+)[`\"\]]?\s+"
        r"(\w+(?:\s*\([^)]*\))?)"
        r"(.*)",
        line,
        re.DOTALL,
    )
    if col_m:
        col_name = col_m.group(1)
        raw_type = col_m.group(2).split("(")[0].strip()
        col_type = _normalize_type(raw_type)
        is_null  = "NOT NULL" not in col_m.group(3).upper()
        return {"name": col_name, "type": col_type, "sample": "", "nullable": is_null}
    return None


_SP_HDR = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(?:PROC(?:EDURE)?|FUNCTION)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?",
    re.IGNORECASE,
)

_SP_NAME = re.compile(
    r"[`\"\[]?(\w+)[`\"\]]?(?:\s*\.\s*[`\"\[]?(\w+)[`\"\]]?)?",
)


def has_stored_procedures(sql_text: str) -> bool:
    """Return True if the SQL text contains stored procedure/function definitions."""
    return bool(_SP_HDR.search(sql_text))


_SP_HDR_DETAILED = re.compile(
    r"CREATE\s+(?:OR\s+REPLACE\s+)?"
    r"(PROCEDURE|PROC|FUNCTION)\s+"
    r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"([`\"\[]?\w+[`\"\]]?(?:\s*\.\s*[`\"\[]?\w+[`\"\]]?)?)",
    re.IGNORECASE,
)


def extract_stored_procedures(sql_text: str) -> List[Dict[str, Any]]:
    """Extract stored procedure/function names from SQL text.

    Returns a list of {name, type} dicts. The full SQL text is best sent to
    an LLM for schema inference — this just detects presence and extracts
    metadata for prompting.
    """
    procs: List[Dict[str, Any]] = []
    for m in _SP_HDR_DETAILED.finditer(sql_text):
        kind_raw = (m.group(1) or "").upper()
        kind = "FUNCTION" if kind_raw == "FUNCTION" else "PROCEDURE"
        full_name = (m.group(2) or "").strip("`\"[] ")
        if full_name:
            # Extract the last segment as the canonical name (e.g. dbo.CleanupData → CleanupData)
            parts = re.split(r"\s*\.\s*", full_name)
            name = parts[-1].strip("`\"[] ")
            if name:
                procs.append({"name": name, "type": kind})
    return procs


def parse_ddl(ddl_text: str) -> Dict[str, Any]:
    """Parse SQL DDL (any dialect) into schema format."""
    tables: List[Dict] = []

    _SEG = r"(?:`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|[\w\$]+)"
    header_pat = re.compile(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?(?:TEMP(?:ORARY)?\s+)?TABLE\s+"
        r"(?:IF\s+NOT\s+EXISTS\s+)?"
        r"(?:" + _SEG + r"\.)?"
        r"(?:" + _SEG + r"\.)?"
        + _SEG +
        r"(?:\s+AS)?"
        r"\s*\(",
        re.IGNORECASE,
    )

    for header in header_pat.finditer(ddl_text):
        hdr_text  = header.group(0)
        all_segs = re.findall(
            r"(?:TABLE|IF\s+NOT\s+EXISTS)\s|"
            r"`([^`]+)`|\"([^\"]+)\"|\[([^\]]+)\]|([\w\$]+)",
            hdr_text, re.IGNORECASE,
        )
        name_segs = []
        for s in all_segs:
            seg = next((x for x in s if x), None)
            if seg and seg.upper() not in ("TABLE", "CREATE", "OR", "REPLACE",
                                           "TEMP", "TEMPORARY", "IF", "NOT", "EXISTS", "AS"):
                name_segs.append(seg)
        if not name_segs:
            continue
        tbl_name = name_segs[-1].strip("`\"[] ")
        if not tbl_name:
            continue

        paren_pos = header.end() - 1

        depth      = 0
        body_start = paren_pos + 1
        body_end   = body_start
        in_str     = False
        str_char   = ""
        for idx in range(paren_pos, len(ddl_text)):
            ch = ddl_text[idx]
            if in_str:
                if ch == str_char:
                    in_str = False
            elif ch in ("'", '"'):
                in_str = True
                str_char = ch
            elif ch == "(":
                depth += 1
            elif ch == ")":
                depth -= 1
                if depth == 0:
                    body_end = idx
                    break

        if body_end <= body_start:
            continue

        body = ddl_text[body_start:body_end]

        columns: List[Dict] = []
        current: List[str] = []
        depth2  = 0
        for ch in body:
            if ch == "(":
                depth2 += 1
                current.append(ch)
            elif ch == ")":
                depth2 -= 1
                current.append(ch)
            elif ch == "," and depth2 == 0:
                col = _parse_ddl_column("".join(current))
                if col:
                    columns.append(col)
                current = []
            else:
                current.append(ch)
        if current:
            col = _parse_ddl_column("".join(current))
            if col:
                columns.append(col)

        if columns:
            tables.append({"name": tbl_name, "columns": columns})

    return {"tables": tables}
