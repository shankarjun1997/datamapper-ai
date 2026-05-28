"""
app/connectors/databricks.py — _crawl_databricks_unity + DatabricksUnityRequest model
"""
from __future__ import annotations

import re
from typing import Dict

from pydantic import BaseModel

from app.config import logger
from app.parsers.schema import _normalize_type


class DatabricksUnityRequest(BaseModel):
    server_hostname: str
    http_path:       str
    access_token:    str
    catalog:         str = ""
    schema_filter:   str = ""
    table_filter:    str = ""


def _crawl_databricks_unity(
    server_hostname: str,
    http_path: str,
    access_token: str,
    catalog: str,
    schema_filter: str,
    table_filter: str,
) -> Dict:
    """Crawl Databricks Unity Catalog via the SQL Connector and return schema dict."""
    try:
        from databricks import sql as dbsql  # type: ignore
    except ImportError:
        raise RuntimeError(
            "databricks-sql-connector not installed. "
            "Run: pip install databricks-sql-connector"
        )

    schema_list = [s.strip() for s in schema_filter.split(",") if s.strip()]
    table_list  = [t.strip().lower() for t in table_filter.split(",") if t.strip()]

    with dbsql.connect(
        server_hostname=server_hostname,
        http_path=http_path,
        access_token=access_token,
    ) as conn:
        with conn.cursor() as cur:
            if catalog:
                if not re.match(r'^[\w\-]+$', catalog):
                    raise RuntimeError(f"Invalid catalog name: {catalog!r}")
                cur.execute(f"USE CATALOG `{catalog}`")

            cur.execute("SHOW SCHEMAS")
            all_schemas = [row[0] for row in cur.fetchall() if row[0] not in ("information_schema",)]
            if schema_list:
                all_schemas = [s for s in all_schemas if s in schema_list]

            tables: Dict[str, list] = {}
            for schema in all_schemas:
                if not re.match(r'^[\w\-]+$', schema):
                    continue
                try:
                    cur.execute(f"SHOW TABLES IN `{schema}`")
                    schema_tables = [(row[0], row[1]) for row in cur.fetchall()]
                except Exception:
                    continue

                for _, tbl_name in schema_tables:
                    if table_list and tbl_name.lower() not in table_list:
                        continue
                    full_name = f"{schema}.{tbl_name}"
                    try:
                        cur.execute(f"DESCRIBE TABLE `{schema}`.`{tbl_name}`")
                        rows = cur.fetchall()
                        cols = []
                        col_names_ordered = []
                        for row in rows:
                            col_name = str(row[0]).strip()
                            col_type_raw = str(row[1]).strip() if len(row) > 1 else "STRING"
                            if col_name.startswith("#") or col_name == "":
                                break
                            cols.append({
                                "name":     col_name,
                                "type":     _normalize_type(col_type_raw.split("(")[0].strip()),
                                "sample":   "",
                                "nullable": True,
                            })
                            col_names_ordered.append(col_name)

                        if cols:
                            try:
                                backtick_cols = ", ".join(f"`{cn}`" for cn in col_names_ordered)
                                cur.execute(
                                    f"SELECT {backtick_cols} FROM `{schema}`.`{tbl_name}` LIMIT 1"
                                )
                                sample_row = cur.fetchone()
                                if sample_row:
                                    for idx, col_def in enumerate(cols):
                                        raw_val = sample_row[idx]
                                        col_def["sample"] = "" if raw_val is None else str(raw_val)
                            except Exception as sample_err:
                                logger.debug(
                                    "Sample fetch failed for %s.%s: %s",
                                    schema, tbl_name, sample_err
                                )

                        if cols:
                            tables[full_name] = cols
                    except Exception:
                        continue

    if not tables:
        raise RuntimeError("No tables discovered. Check catalog/schema filters and permissions.")

    return {
        "tables": [
            {"name": name, "columns": cols}
            for name, cols in tables.items()
        ]
    }
