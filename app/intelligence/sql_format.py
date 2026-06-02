"""
app/intelligence/sql_format.py — _repair_select_commas + _format_sql + _format_single_sql + _split_cols
"""
from __future__ import annotations

import re
from typing import List


def _repair_select_commas(sql: str) -> str:
    """Safety-net pass: insert missing commas between SELECT column expressions."""
    lines = sql.split('\n')
    select_blocks: List[tuple] = []
    paren_depth = 0
    i = 0
    while i < len(lines):
        stripped = lines[i].strip().upper()
        paren_depth += lines[i].count('(') - lines[i].count(')')
        if re.match(r'^SELECT(\s+DISTINCT)?$', stripped) or stripped.startswith('SELECT ') or stripped.startswith('SELECT\t'):
            if paren_depth <= 1:
                start = i
                j = i + 1
                while j < len(lines):
                    s2 = lines[j].strip().upper()
                    if re.match(r'^(FROM|WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|QUALIFY|LIMIT|UNION|EXCEPT|INTERSECT|;|\))', s2):
                        select_blocks.append((start, j))
                        break
                    j += 1
                i = j
                continue
        i += 1

    patched = list(lines)
    for (start, end) in select_blocks:
        col_lines = []
        for idx in range(start + 1, end):
            stripped = patched[idx].strip()
            if not stripped or stripped.startswith('--'):
                continue
            col_lines.append(idx)
        for k, idx in enumerate(col_lines):
            stripped = patched[idx].rstrip()
            if k < len(col_lines) - 1:
                if stripped and not stripped.endswith(',') and not stripped.endswith('('):
                    patched[idx] = patched[idx].rstrip() + ','

    return '\n'.join(patched)


def _format_sql(sql: str) -> str:
    """Pretty-print BigQuery SQL. Handles multi-statement blocks."""
    sql = _repair_select_commas(sql)
    sql = re.sub(r'[ \t]+', ' ', sql.strip())
    sql = re.sub(r'\r\n|\r', '\n', sql)
    sql = re.sub(r'\n{3,}', '\n\n', sql)
    statements = [s.strip() for s in sql.split(';') if s.strip()]
    formatted_statements: List[str] = []
    for stmt in statements:
        formatted_statements.append(_format_single_sql(stmt))
    return ';\n\n'.join(formatted_statements) + ';\n'


def _format_single_sql(sql: str) -> str:
    """Format a single SQL statement with clause-level and column-level indentation."""
    INDENT = '    '

    TOP_CLAUSE = re.compile(
        r'(?<!\w)(CREATE\s+OR\s+REPLACE\s+TABLE|CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS|'
        r'CREATE\s+TABLE|INSERT\s+INTO|SELECT\s+DISTINCT|SELECT|WITH|'
        r'FROM|LEFT\s+OUTER\s+JOIN|RIGHT\s+OUTER\s+JOIN|FULL\s+OUTER\s+JOIN|'
        r'LEFT\s+JOIN|RIGHT\s+JOIN|INNER\s+JOIN|CROSS\s+JOIN|JOIN|'
        r'WHERE|GROUP\s+BY|ORDER\s+BY|HAVING|QUALIFY|LIMIT|'
        r'UNION\s+ALL|UNION|EXCEPT|INTERSECT|PARTITION\s+BY)(?!\w)',
        re.IGNORECASE,
    )

    depth = 0
    result: List[str] = []
    i = 0
    while i < len(sql):
        ch = sql[i]
        if ch == '(':
            depth += 1
            result.append(ch)
            i += 1
        elif ch == ')':
            depth -= 1
            result.append(ch)
            i += 1
        elif depth == 0:
            m = TOP_CLAUSE.match(sql, i)
            if m:
                kw = m.group()
                prefix = '\n' if result and result[-1] not in ('\n', ' ') else ''
                result.append(prefix + kw.upper())
                i += len(kw)
            else:
                result.append(ch)
                i += 1
        else:
            result.append(ch)
            i += 1

    sql = ''.join(result)

    raw_lines = sql.splitlines()
    out_lines: List[str] = []
    in_col_list = False
    in_with_cte = False

    for raw in raw_lines:
        line = raw.strip()
        if not line:
            continue

        up = line.upper().lstrip()

        if up.startswith('WITH '):
            in_with_cte = True
            out_lines.append(line)
            continue

        if re.match(r'SELECT(\s+DISTINCT)?\b', up, re.I):
            in_col_list = True
            in_with_cte = False  # noqa: F841
            out_lines.append(line)
            continue

        if in_col_list and re.match(
            r'(FROM|WHERE|JOIN|LEFT|RIGHT|INNER|FULL|CROSS|GROUP|ORDER|HAVING|QUALIFY|'
            r'LIMIT|UNION|EXCEPT|INTERSECT|CREATE)\b', up, re.I
        ):
            in_col_list = False

        if in_col_list:
            parts = _split_cols(line)
            for j, part in enumerate(parts):
                part = part.strip()
                if not part:
                    continue
                suffix = ',' if j < len(parts) - 1 else ''
                out_lines.append(INDENT + part + suffix)
        else:
            out_lines.append(line)

    SQL_KEYWORDS = re.compile(
        r'\b(SELECT|DISTINCT|FROM|WHERE|AND|OR|NOT|IN|IS|NULL|AS|'
        r'JOIN|LEFT|RIGHT|INNER|FULL|OUTER|CROSS|ON|'
        r'CASE|WHEN|THEN|ELSE|END|WITH|'
        r'GROUP\s+BY|ORDER\s+BY|PARTITION\s+BY|HAVING|QUALIFY|LIMIT|'
        r'UNION\s+ALL|UNION|INSERT|INTO|VALUES|UPDATE|SET|DELETE|'
        r'CREATE\s+OR\s+REPLACE\s+TABLE|CREATE\s+TABLE\s+IF\s+NOT\s+EXISTS|'
        r'CREATE\s+TABLE|OR\s+REPLACE|IF\s+NOT\s+EXISTS|'
        r'OVER|ROW_NUMBER|RANK|DENSE_RANK|'
        r'COALESCE|CAST|SAFE_CAST|TIMESTAMP|DATE|STRING|INT64|FLOAT64|NUMERIC|BOOL|'
        r'ARRAY|STRUCT|CURRENT_TIMESTAMP|GENERATE_UUID|'
        r'TRIM|UPPER|LOWER|CONCAT|IF|IFNULL|NULLIF|ROUND|ABS|'
        r'INITCAP|REGEXP_REPLACE|FORMAT_DATE|PARSE_DATE|'
        r'DESC|ASC|TRUE|FALSE)\b',
        re.IGNORECASE,
    )

    final_lines: List[str] = []
    for line in out_lines:
        if "'" not in line and '"' not in line:
            line = SQL_KEYWORDS.sub(lambda m: m.group().upper(), line)
        final_lines.append(line)

    return '\n'.join(final_lines)


def _split_cols(line: str) -> List[str]:
    """Split a SELECT column list by commas, respecting parenthesis depth."""
    parts: List[str] = []
    depth = 0
    buf: List[str] = []
    for ch in line:
        if ch == '(':
            depth += 1
            buf.append(ch)
        elif ch == ')':
            depth -= 1
            buf.append(ch)
        elif ch == ',' and depth == 0:
            parts.append(''.join(buf).strip())
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append(''.join(buf).strip())
    return [p for p in parts if p]


# ─── Dialect-specific SQL renderers ──────────────────────────────────────────

def _pick_key_columns(mappings: list, key_columns: list | None) -> list:
    """Choose join-key columns. Prefer explicit list, then *_id / *_key, then first col."""
    if key_columns:
        return [k for k in key_columns if k]
    tgt_cols = [m.get("tgt_column", "") for m in mappings if m.get("tgt_column")]
    for c in tgt_cols:
        cl = c.lower()
        if cl.endswith("_id") or cl.endswith("_key") or cl == "id":
            return [c]
    return [tgt_cols[0]] if tgt_cols else []


def _build_select_pairs(mappings: list) -> list:
    """Return list of (tgt_column, source_expression) pairs for SELECT projection.

    If business_logic is provided it's used verbatim; otherwise we fall back to
    source.<src_field>. Mappings without a tgt_column are skipped.
    """
    pairs: list = []
    for m in mappings:
        tgt = (m.get("tgt_column") or "").strip()
        if not tgt:
            continue
        bl = (m.get("business_logic") or "").strip()
        src = (m.get("src_field") or "").strip()
        if bl:
            expr = bl
        elif src:
            expr = f"source.{src}"
        else:
            expr = "NULL"
        pairs.append((tgt, expr))
    return pairs


def _infer_source_table(mappings: list) -> str:
    """Pick a representative source table for the USING / FROM clause."""
    for m in mappings:
        st = (m.get("src_table") or "").strip()
        if st:
            return st
    return "source_table"


def render_bq_merge(table_name: str, mappings: list, key_columns: list | None = None) -> str:
    """BigQuery MERGE statement — idempotent upsert pattern."""
    pairs = _build_select_pairs(mappings)
    if not pairs:
        return f"-- No mappings available for target table {table_name}\n"
    keys = _pick_key_columns(mappings, key_columns)
    source_table = _infer_source_table(mappings)

    col_list = ",\n    ".join(f"{expr} AS {tgt}" for tgt, expr in pairs)
    col_names = ", ".join(tgt for tgt, _ in pairs)
    col_values = ", ".join(f"source.{tgt}" for tgt, _ in pairs)
    update_set = ",\n    ".join(
        f"target.{tgt} = source.{tgt}" for tgt, _ in pairs if tgt not in keys
    ) or "/* no non-key columns to update */"
    join_condition = " AND ".join(
        f"target.{k} = source.{k}" for k in keys
    ) if keys else "FALSE /* no key columns identified */"

    sql = (
        f"MERGE `{{project}}.{{dataset}}.{table_name}` AS target\n"
        f"USING (\n"
        f"  SELECT\n"
        f"    {col_list}\n"
        f"  FROM `{source_table}`\n"
        f") AS source\n"
        f"ON {join_condition}\n"
        f"WHEN MATCHED THEN UPDATE SET\n"
        f"    {update_set}\n"
        f"WHEN NOT MATCHED THEN INSERT ({col_names}) VALUES ({col_values})\n"
        f";\n"
    )
    return sql


def render_snowflake_merge(table_name: str, mappings: list, key_columns: list | None = None) -> str:
    """Snowflake MERGE — similar to BQ but different identifier quoting."""
    pairs = _build_select_pairs(mappings)
    if not pairs:
        return f"-- No mappings available for target table {table_name}\n"
    keys = _pick_key_columns(mappings, key_columns)
    source_table = _infer_source_table(mappings)

    col_list = ",\n    ".join(f"{expr} AS {tgt}" for tgt, expr in pairs)
    col_names = ", ".join(tgt for tgt, _ in pairs)
    col_values = ", ".join(f"source.{tgt}" for tgt, _ in pairs)
    update_set = ",\n    ".join(
        f"target.{tgt} = source.{tgt}" for tgt, _ in pairs if tgt not in keys
    ) or "/* no non-key columns to update */"
    join_condition = " AND ".join(
        f"target.{k} = source.{k}" for k in keys
    ) if keys else "FALSE /* no key columns identified */"

    sql = (
        f"MERGE INTO {{schema}}.{table_name} AS target\n"
        f"USING (\n"
        f"  SELECT {col_list}\n"
        f"  FROM {source_table}\n"
        f") AS source\n"
        f"ON {join_condition}\n"
        f"WHEN MATCHED THEN UPDATE SET\n"
        f"    {update_set}\n"
        f"WHEN NOT MATCHED THEN INSERT ({col_names}) VALUES ({col_values})\n"
        f";\n"
    )
    return sql


def render_spark_insert(table_name: str, mappings: list, key_columns: list | None = None) -> str:
    """Spark SQL — INSERT OVERWRITE with partition placeholder."""
    pairs = _build_select_pairs(mappings)
    if not pairs:
        return f"-- No mappings available for target table {table_name}\n"
    source_table = _infer_source_table(mappings)
    col_list = ",\n  ".join(f"{expr} AS {tgt}" for tgt, expr in pairs)

    sql = (
        f"-- Spark SQL: configure PARTITION (<partition_col>=<value>) if the\n"
        f"-- target table is partitioned. Omit the clause for non-partitioned tables.\n"
        f"INSERT OVERWRITE TABLE {{catalog}}.{{database}}.{table_name}\n"
        f"SELECT\n"
        f"  {col_list}\n"
        f"FROM {source_table}\n"
        f";\n"
    )
    return sql


def render_ansi_insert(table_name: str, mappings: list, key_columns: list | None = None) -> str:
    """Standard ANSI INSERT INTO ... SELECT pattern."""
    pairs = _build_select_pairs(mappings)
    if not pairs:
        return f"-- No mappings available for target table {table_name}\n"
    source_table = _infer_source_table(mappings)
    col_names = ", ".join(tgt for tgt, _ in pairs)
    col_list = ",\n  ".join(f"{expr} AS {tgt}" for tgt, expr in pairs)

    sql = (
        f"INSERT INTO {table_name} ({col_names})\n"
        f"SELECT\n"
        f"  {col_list}\n"
        f"FROM {source_table}\n"
        f";\n"
    )
    return sql


DIALECT_RENDERERS = {
    "bigquery": render_bq_merge,
    "snowflake": render_snowflake_merge,
    "spark": render_spark_insert,
    "ansi": render_ansi_insert,
}


def render_sql_for_dialect(
    dialect: str,
    table_name: str,
    mappings: list,
    key_columns: list | None = None,
) -> str:
    """Dispatch to the right renderer. Falls back to ansi if dialect unknown."""
    renderer = DIALECT_RENDERERS.get((dialect or "").lower(), render_ansi_insert)
    return renderer(table_name, mappings, key_columns)
