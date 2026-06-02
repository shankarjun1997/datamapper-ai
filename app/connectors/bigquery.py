"""
app/connectors/bigquery.py — crawl_bq + list_bq_datasets + crawl_bq_project
"""
from __future__ import annotations

from typing import Dict, List, Optional

from app.config import logger

def crawl_bq(
    project: str,
    dataset: str,
    gcp_creds: str = "",
    target_tables: List[str] = None,
    gcp_creds_json: Optional[Dict] = None,
) -> List[Dict]:
    """Return list of {table, columns:[{name, type, nullable}]} from BQ INFORMATION_SCHEMA."""
    from google.cloud import bigquery
    from google.oauth2 import service_account

    if gcp_creds_json:
        creds = service_account.Credentials.from_service_account_info(
            gcp_creds_json,
            scopes=["https://www.googleapis.com/auth/bigquery"],
        )
        client = bigquery.Client(project=project, credentials=creds)
    elif gcp_creds and __import__("os").path.exists(gcp_creds):
        creds = service_account.Credentials.from_service_account_file(
            gcp_creds,
            scopes=["https://www.googleapis.com/auth/bigquery"],
        )
        client = bigquery.Client(project=project, credentials=creds)
    else:
        client = bigquery.Client(project=project)

    filter_clause = ""
    if target_tables:
        quoted = ", ".join(f"'{t}'" for t in target_tables)
        filter_clause = f"AND c.table_name IN ({quoted})"

    query = f"""
        SELECT
            c.table_name,
            c.column_name,
            c.data_type,
            c.is_nullable
        FROM `{project}.{dataset}.INFORMATION_SCHEMA.COLUMNS` c
        JOIN `{project}.{dataset}.INFORMATION_SCHEMA.TABLES` t
            ON c.table_name = t.table_name
        WHERE t.table_type = 'BASE TABLE' {filter_clause}
        ORDER BY c.table_name, c.ordinal_position
    """
    rows = list(client.query(query).result())

    tables: Dict[str, List] = {}
    for r in rows:
        tbl = r["table_name"]
        if tbl not in tables:
            tables[tbl] = []
        tables[tbl].append({
            "name": r["column_name"],
            "type": r["data_type"],
            "nullable": r["is_nullable"] == "YES",
        })

    return [{"table": t, "columns": cols} for t, cols in tables.items()]


def list_bq_datasets(
    project: str,
    gcp_creds: str = "",
    gcp_creds_json: Optional[Dict] = None,
) -> List[str]:
    """Return all dataset IDs in a GCP project."""
    from google.cloud import bigquery
    from google.oauth2 import service_account

    if gcp_creds_json:
        creds = service_account.Credentials.from_service_account_info(
            gcp_creds_json,
            scopes=["https://www.googleapis.com/auth/bigquery"],
        )
        client = bigquery.Client(project=project, credentials=creds)
    elif gcp_creds and __import__("os").path.exists(gcp_creds):
        creds = service_account.Credentials.from_service_account_file(
            gcp_creds,
            scopes=["https://www.googleapis.com/auth/bigquery"],
        )
        client = bigquery.Client(project=project, credentials=creds)
    else:
        client = bigquery.Client(project=project)

    return [ds.dataset_id for ds in client.list_datasets()]


def crawl_bq_project(
    project: str,
    dataset_filter: Optional[List[str]] = None,
    gcp_creds: str = "",
    gcp_creds_json: Optional[Dict] = None,
) -> List[Dict]:
    """Crawl ALL datasets in a project (or a filtered subset)."""
    datasets = list_bq_datasets(project, gcp_creds, gcp_creds_json)
    if dataset_filter:
        datasets = [d for d in datasets if d in dataset_filter]

    if not datasets:
        raise RuntimeError(f"No datasets found in project '{project}'. Check project ID and permissions.")

    all_tables: List[Dict] = []
    errors: List[str] = []

    for ds in datasets:
        try:
            tables = crawl_bq(
                project=project,
                dataset=ds,
                gcp_creds=gcp_creds,
                gcp_creds_json=gcp_creds_json,
            )
            for t in tables:
                t["table"]   = f"{ds}.{t['table']}"
                t["dataset"] = ds
            all_tables.extend(tables)
        except Exception as e:
            errors.append(f"{ds}: {e}")
            logger.warning("crawl_bq_project: skipping dataset %s — %s", ds, e)

    if not all_tables:
        detail = "; ".join(errors) if errors else "no tables found"
        raise RuntimeError(f"No tables discovered across {len(datasets)} dataset(s): {detail}")

    return all_tables
