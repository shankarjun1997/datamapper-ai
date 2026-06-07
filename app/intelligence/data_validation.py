"""
app/intelligence/data_validation.py — prove mappings against real sample data.

The platform's confidence scores come from names, types and the LLM. This module
adds the missing leg: DATA. Given sample values for source columns (from the
uploaded schema's sample column and/or user-uploaded sample-row CSVs), it
  • simulates each mapping's transform in Python (SPLIT / CONCAT / TRIM /
    UPPER / LOWER / direct copy) and records before→after examples,
  • checks split coverage (does `customer_name` really hold 2+ tokens?),
  • checks type-cast feasibility against the target type,
  • checks semantic patterns (an email target should receive email-shaped data),
and grades every mapping pass / warn / fail with human-readable evidence.

Everything here is pure and deterministic — no LLM, no network — so the result
is something an auditor can re-run and trust.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Dict, List, Optional

from app.intelligence.confidence import detect_value_pattern

MAX_SAMPLE_ROWS = 200      # cap stored sample rows per table
MAX_EXAMPLES = 5           # before→after examples shown per mapping

# ── Transform simulator ───────────────────────────────────────────────────────
_SPLIT_RX = re.compile(
    r"^\s*(TRIM\(\s*)?SPLIT\(\s*([A-Za-z_]\w*)\s*,\s*'([^']*)'\s*\)\s*"
    r"\[\s*(SAFE_)?OFFSET\(\s*(\d+)\s*\)\s*\]\s*\)?\s*$", re.I)
_CONCAT_RX = re.compile(r"^\s*CONCAT\(\s*(.+)\s*\)\s*$", re.I | re.S)
_FUNC1_RX = re.compile(r"^\s*(UPPER|LOWER|TRIM)\(\s*([A-Za-z_]\w*)\s*\)\s*$", re.I)


def _split_args(s: str) -> List[str]:
    """Split CONCAT args on commas that aren't inside quotes."""
    args, buf, in_q = [], "", False
    for ch in s:
        if ch == "'":
            in_q = not in_q
            buf += ch
        elif ch == "," and not in_q:
            args.append(buf.strip()); buf = ""
        else:
            buf += ch
    if buf.strip():
        args.append(buf.strip())
    return args


def parse_split(logic: str):
    """Return (field, delim, safe, index) when logic is a SPLIT[...] expression."""
    m = _SPLIT_RX.match(logic or "")
    if not m:
        return None
    return m.group(2), m.group(3), bool(m.group(4)), int(m.group(5))


def simulate_transform(logic: str, row_values: Dict[str, str]) -> Optional[str]:
    """Apply a known transform to one sample row. None = expression not simulatable."""
    logic = (logic or "").strip()
    if not logic or logic in ("—", "direct"):
        if len(row_values) == 1:
            return str(next(iter(row_values.values())))
        return None

    sp = parse_split(logic)
    if sp:
        field, delim, safe, idx = sp
        if field not in row_values:
            return None
        parts = str(row_values[field]).split(delim or " ")
        out = parts[idx] if idx < len(parts) else ("" if safe else "")
        return out.strip() if logic.upper().startswith("TRIM") else out

    m = _CONCAT_RX.match(logic)
    if m:
        out = []
        for arg in _split_args(m.group(1)):
            if arg.startswith("'") and arg.endswith("'"):
                out.append(arg[1:-1])
            elif arg in row_values:
                out.append(str(row_values[arg]))
            else:
                return None
        return "".join(out)

    m = _FUNC1_RX.match(logic)
    if m:
        fn, field = m.group(1).upper(), m.group(2)
        if field not in row_values:
            return None
        v = str(row_values[field])
        return v.upper() if fn == "UPPER" else v.lower() if fn == "LOWER" else v.strip()

    return None


# ── Type-cast feasibility ─────────────────────────────────────────────────────
_DATE_FORMATS = ("%Y-%m-%d", "%Y/%m/%d", "%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y",
                 "%Y-%m-%d %H:%M:%S", "%d %b %Y", "%b %d, %Y")
_BOOL_VALUES = {"true", "false", "yes", "no", "y", "n", "t", "f", "0", "1"}


def _parses_as_date(v: str) -> bool:
    try:
        datetime.fromisoformat(v.replace("Z", "+00:00"))
        return True
    except ValueError:
        pass
    for fmt in _DATE_FORMATS:
        try:
            datetime.strptime(v, fmt)
            return True
        except ValueError:
            continue
    return False


def castable(value, tgt_type: str) -> bool:
    """Can this sample value plausibly land in the target type?"""
    v = str(value if value is not None else "").strip()
    if not v or v.lower() in ("null", "none", "nan"):
        return True                          # nulls are a nullability question, not a cast failure
    t = (tgt_type or "STRING").upper()
    base = re.split(r"[(\s]", t, 1)[0]
    if base in ("INT64", "INT", "INTEGER", "BIGINT", "SMALLINT"):
        try:
            return float(v).is_integer()
        except ValueError:
            return False
    if base in ("NUMERIC", "DECIMAL", "FLOAT64", "FLOAT", "DOUBLE", "REAL"):
        try:
            float(v); return True
        except ValueError:
            return False
    if base in ("BOOL", "BOOLEAN"):
        return v.lower() in _BOOL_VALUES
    if base in ("DATE", "DATETIME", "TIMESTAMP"):
        return _parses_as_date(v)
    return True                              # STRING/JSON/BYTES accept anything


# ── Per-mapping validation ────────────────────────────────────────────────────
_PATTERN_TARGETS = {"email": "email", "phone": "phone", "uuid": "uuid"}


def _pct(n: int, d: int) -> int:
    return round(n * 100 / d) if d else 0


def validate_mapping_row(m: Dict, sample_rows: List[Dict[str, str]]) -> Dict:
    """Grade one mapping against aligned sample rows ({field: value} dicts)."""
    checks: List[Dict] = []
    examples: List[Dict] = []
    logic = (m.get("business_logic") or "").strip()
    src_fields = [f.strip() for f in str(m.get("src_field", "")).split("+")]
    rows = [r for r in sample_rows if any(f in r and str(r[f]).strip() for f in src_fields)]

    if not rows:
        return {"status": "no_data", "checks": [{"name": "samples", "status": "no_data",
                                                 "detail": "No sample values available for this field."}],
                "examples": []}

    # 1. Simulate the transform and collect before→after examples.
    outputs: List[str] = []
    simulated = 0
    for r in rows:
        out = simulate_transform(logic, {f: r.get(f, "") for f in src_fields if f in r})
        if out is None:
            continue
        simulated += 1
        outputs.append(out)
        if len(examples) < MAX_EXAMPLES:
            src_view = {f: r.get(f, "") for f in src_fields if f in r}
            examples.append({"in": src_view if len(src_view) > 1 else next(iter(src_view.values())),
                             "out": out})
    if simulated:
        checks.append({"name": "transform_simulation", "status": "pass",
                       "detail": f"Simulated on {simulated} sample(s)."})

    # 2. Split coverage — the core trust check for 1:M derived splits.
    sp = parse_split(logic)
    if sp:
        field, delim, _safe, idx = sp
        vals = [str(r[field]) for r in rows if field in r and str(r[field]).strip()]
        covered = sum(1 for v in vals if len(v.split(delim or " ")) > idx)
        pct = _pct(covered, len(vals))
        status = "pass" if pct == 100 else ("warn" if pct >= 60 else "fail")
        checks.append({"name": "split_coverage", "status": status,
                       "detail": f"{covered}/{len(vals)} samples ({pct}%) have a part at position {idx + 1} "
                                 f"when split on '{delim or ' '}'."})

    # 3. Type-cast feasibility against the target type.
    cast_inputs = outputs if outputs else [str(r.get(src_fields[0], "")) for r in rows]
    cast_inputs = [v for v in cast_inputs if str(v).strip()]
    if cast_inputs:
        ok = sum(1 for v in cast_inputs if castable(v, m.get("tgt_type", "STRING")))
        pct = _pct(ok, len(cast_inputs))
        status = "pass" if pct == 100 else ("warn" if pct >= 80 else "fail")
        checks.append({"name": "type_cast", "status": status,
                       "detail": f"{ok}/{len(cast_inputs)} values ({pct}%) cast cleanly to "
                                 f"{m.get('tgt_type', 'STRING')}."})

    # 4. Semantic pattern — an email/phone/uuid target should receive matching data.
    tgt_col = (m.get("tgt_column") or "").lower()
    for token, pattern in _PATTERN_TARGETS.items():
        if token in tgt_col and cast_inputs:
            hits = sum(1 for v in cast_inputs if detect_value_pattern(v) == pattern)
            pct = _pct(hits, len(cast_inputs))
            status = "pass" if pct >= 90 else ("warn" if pct >= 60 else "fail")
            checks.append({"name": f"{token}_pattern", "status": status,
                           "detail": f"{hits}/{len(cast_inputs)} values ({pct}%) look like a valid {token}."})
            break

    order = {"fail": 3, "warn": 2, "pass": 1}
    graded = [c["status"] for c in checks if c["status"] in order]
    overall = max(graded, key=lambda s: order[s]) if graded else "no_data"
    return {"status": overall, "checks": checks, "examples": examples}


# ── Session-level report ──────────────────────────────────────────────────────
def _collect_samples(session: Dict) -> Dict[str, List[Dict[str, str]]]:
    """{table: [aligned {field: value} rows]} from uploads + schema samples."""
    out: Dict[str, List[Dict[str, str]]] = {}
    uploaded = session.get("sample_data") or {}
    for table, cols in uploaded.items():
        n = max((len(v) for v in cols.values()), default=0)
        rows = []
        for i in range(min(n, MAX_SAMPLE_ROWS)):
            rows.append({f: vals[i] for f, vals in cols.items() if i < len(vals)})
        if rows:
            out[table] = rows
    # Fall back to the single sample value the schema parser captured per column.
    for t in (session.get("schema_data") or {}).get("tables", []) or []:
        name = t.get("name", "")
        if name in out:
            continue
        row = {c["name"]: str(c.get("sample", "")) for c in t.get("columns", [])
               if str(c.get("sample", "")).strip()}
        if row:
            out[name] = [row]
    return out


def validate_session(session: Dict) -> Dict:
    """Validate every active mapping against available samples; store + return report."""
    samples = _collect_samples(session)
    rows_out: List[Dict] = []
    counts = {"pass": 0, "warn": 0, "fail": 0, "no_data": 0}
    for m in session.get("mappings", []) or []:
        if (m.get("status") or "").lower() == "unmapped" or not m.get("tgt_column"):
            continue
        result = validate_mapping_row(m, samples.get(m.get("src_table", ""), []))
        m["validation"] = result["status"]
        counts[result["status"]] = counts.get(result["status"], 0) + 1
        rows_out.append({
            "id": m.get("id", ""), "src_table": m.get("src_table", ""),
            "src_field": m.get("src_field", ""), "tgt_table": m.get("tgt_table", ""),
            "tgt_column": m.get("tgt_column", ""), "transform": m.get("business_logic", ""),
            **result,
        })
    report = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "summary": {**counts, "total": sum(counts.values()),
                    "tables_with_samples": len(samples)},
        "rows": rows_out,
    }
    session["data_validation"] = report
    return report
