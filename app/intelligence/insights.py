"""
app/intelligence/insights.py — quick, deterministic schema/dataset insights.

Given a discovered schema ({'tables':[{'name','columns':[...]}]}) it returns a
few-word characterization: domain guess, key entities, and counts. Pure and
fast (no LLM call), so it can power dataset tiles and previews instantly. An
LLM-enriched summary can layer on top later via the same shape.
"""
from __future__ import annotations

from typing import Dict, List

# Domain → signal keywords (matched against table + column names).
_DOMAINS = {
    "Customer / CRM": ["customer", "party", "account", "contact", "subscriber", "lead", "crm"],
    "Billing / Finance": ["invoice", "bill", "payment", "charge", "price", "cost", "ledger", "tax", "revenue", "msrp"],
    "Product / Catalog": ["product", "catalog", "catalogue", "offering", "sku", "plan", "tariff", "bundle"],
    "Orders": ["order", "cart", "checkout", "purchase", "quote", "fulfil"],
    "Network / Telecom": ["network", "circuit", "device", "port", "bandwidth", "cell", "site", "node", "link", "msisdn", "imsi"],
    "Usage / Events": ["usage", "event", "log", "session", "cdr", "telemetry", "metric"],
    "HR / People": ["employee", "payroll", "staff", "department", "salary"],
    "Geography / Location": ["address", "region", "location", "geo", "country", "city", "postal"],
}


def merge_schemas(existing: Dict, incoming: Dict) -> Dict:
    """Merge two schemas, deduping tables by (case-insensitive) name — a later
    upload of the same table name replaces the earlier one (supports adding
    multiple CSVs repeatedly)."""
    by_name: Dict[str, Dict] = {}
    order: List[str] = []
    for t in ((existing or {}).get("tables") or []) + ((incoming or {}).get("tables") or []):
        key = (t.get("name") or "").lower()
        if key not in by_name:
            order.append(key)
        by_name[key] = t  # last wins
    return {"tables": [by_name[k] for k in order]}


def summarize_schema(schema_data: Dict, name: str = "") -> Dict:
    """Return a quick insight for a schema/dataset."""
    tables = (schema_data or {}).get("tables", []) or []
    table_count = len(tables)
    column_count = sum(len(t.get("columns", []) or []) for t in tables)

    # Score domains by keyword hits across table + column names.
    blob_parts: List[str] = []
    for t in tables:
        blob_parts.append((t.get("name") or "").lower())
        for c in t.get("columns", []) or []:
            blob_parts.append((c.get("name") or "").lower())
    blob = " ".join(blob_parts)

    scores = {dom: sum(blob.count(kw) for kw in kws) for dom, kws in _DOMAINS.items()}
    ranked = sorted([(s, d) for d, s in scores.items() if s > 0], reverse=True)
    domains = [d for _s, d in ranked[:2]]
    domain = domains[0] if domains else "General / mixed"

    key_entities = [t.get("name", "") for t in tables[:5] if t.get("name")]

    if table_count == 0:
        summary = "Empty — no tables discovered."
    else:
        ent = ", ".join(key_entities[:3])
        summary = (f"{table_count} table{'s' if table_count != 1 else ''}, "
                   f"{column_count} columns — looks like {domain} data"
                   + (f" (e.g. {ent})." if ent else "."))

    return {
        "name": name,
        "table_count": table_count,
        "column_count": column_count,
        "domain": domain,
        "domains": domains,
        "key_entities": key_entities,
        "summary": summary,
    }
