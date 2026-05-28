"""
Smoke test for the CrewAI STM mapping pipeline.
Run from the project root: python skills/crewai-stm-mapper/scripts/test_crew_mapping.py

Tests:
  1. Basic mapping of 10 source columns to 8 target columns
  2. M:1 conflict: home_phone + mobile_phone → phone_number (resolver must pick one)
  3. Audit column auto-population: etl_run_id not in source → must appear in output
  4. Vendor prefix stripping: VZ_CUST_ID → customer_id

Requires:
  - pip install crewai crewai-tools
  - ANTHROPIC_API_KEY or LLM_API_KEY in .env
"""
import asyncio
import json
import os
import sys
from pathlib import Path

# Ensure project root is on path
PROJECT_ROOT = Path(__file__).parent.parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from dotenv import load_dotenv
load_dotenv(PROJECT_ROOT / ".env", override=False)


# ── Minimal session fixture ────────────────────────────────────────────────────
TEST_SESSION = {
    "id": "test-crew-001",
    "api_config": {
        "use_crew": True,
        "provider": os.getenv("DM_PROVIDER", "claude"),
        "model": os.getenv("DM_CLAUDE_MODEL", "claude-sonnet-4-6"),
    },
    "target_mode": "bq",
    "table_mappings": [
        {"src_table": "ORDERS", "tgt_table": "fact_orders", "confidence": 0.95},
    ],
    "src_columns": [
        {"src_table": "ORDERS", "src_field": "VZ_CUST_ID",      "src_type": "VARCHAR",   "sample": "C001234", "nullable": False},
        {"src_table": "ORDERS", "src_field": "ORDER_DATE",       "src_type": "DATETIME",  "sample": "2024-01-15 00:00:00", "nullable": False},
        {"src_table": "ORDERS", "src_field": "ORDER_AMOUNT",     "src_type": "DECIMAL",   "sample": "99.99", "nullable": False},
        {"src_table": "ORDERS", "src_field": "STATUS_CODE",      "src_type": "VARCHAR",   "sample": "ACTIVE", "nullable": True},
        {"src_table": "ORDERS", "src_field": "HOME_PHONE",       "src_type": "VARCHAR",   "sample": "555-1234", "nullable": True},
        {"src_table": "ORDERS", "src_field": "MOBILE_PHONE",     "src_type": "VARCHAR",   "sample": "555-5678", "nullable": True},
        {"src_table": "ORDERS", "src_field": "EMAIL_ADDRESS",    "src_type": "VARCHAR",   "sample": "user@example.com", "nullable": True},
        {"src_table": "ORDERS", "src_field": "BILLING_ZIP",      "src_type": "VARCHAR",   "sample": "90210", "nullable": True},
        {"src_table": "ORDERS", "src_field": "PRODUCT_SKU",      "src_type": "VARCHAR",   "sample": "SKU-001", "nullable": False},
        {"src_table": "ORDERS", "src_field": "QUANTITY_ORDERED", "src_type": "INT",        "sample": "3", "nullable": False},
    ],
    "tgt_columns": [
        {"tgt_table": "fact_orders", "tgt_column": "customer_id",  "type": "STRING",    "nullable": False},
        {"tgt_table": "fact_orders", "tgt_column": "order_date",   "type": "DATE",      "nullable": False},
        {"tgt_table": "fact_orders", "tgt_column": "order_amount", "type": "NUMERIC",   "nullable": False},
        {"tgt_table": "fact_orders", "tgt_column": "status",       "type": "STRING",    "nullable": True},
        {"tgt_table": "fact_orders", "tgt_column": "phone_number", "type": "STRING",    "nullable": True},
        {"tgt_table": "fact_orders", "tgt_column": "email",        "type": "STRING",    "nullable": True},
        {"tgt_table": "fact_orders", "tgt_column": "postal_code",  "type": "STRING",    "nullable": True},
        {"tgt_table": "fact_orders", "tgt_column": "sku",          "type": "STRING",    "nullable": False},
        {"tgt_table": "fact_orders", "tgt_column": "quantity",     "type": "INT64",     "nullable": False},
        # Mandatory audit columns with no source equivalent — resolver must auto-populate
        {"tgt_table": "fact_orders", "tgt_column": "etl_run_id",  "type": "STRING",    "nullable": False},
        {"tgt_table": "fact_orders", "tgt_column": "created_at",  "type": "TIMESTAMP", "nullable": False},
    ],
    "mapping_memory": [],
}


async def emit(event_type, data):
    """Mock SSE emitter — just prints to console."""
    print(f"[SSE] {event_type}: {data.get('msg', data)}")


async def run_test():
    print("=" * 60)
    print("CrewAI STM Mapper — Smoke Test")
    print("=" * 60)

    from app.core.crew_pipeline import run_crew_mapping
    mappings = await run_crew_mapping(TEST_SESSION, emit)

    print(f"\n✓ Crew returned {len(mappings)} mappings\n")

    # ── Assertion 1: VZ_CUST_ID should map to customer_id ─────────────────────
    cust_map = next((m for m in mappings if m["src_field"] == "VZ_CUST_ID"), None)
    assert cust_map is not None, "VZ_CUST_ID not found in output"
    assert cust_map["tgt_column"] == "customer_id", \
        f"Expected customer_id, got {cust_map['tgt_column']}"
    print(f"✓ Vendor prefix stripped: VZ_CUST_ID → {cust_map['tgt_column']} (conf={cust_map['confidence']:.2f})")

    # ── Assertion 2: M:1 conflict resolved — only one mapped phone row ─────────
    phone_mapped = [m for m in mappings
                    if m["tgt_column"] == "phone_number" and m["status"] != "unmapped"]
    assert len(phone_mapped) <= 1, \
        f"M:1 conflict not resolved: {len(phone_mapped)} rows still mapped to phone_number"
    print(f"✓ M:1 resolved: {len(phone_mapped)} row(s) mapped to phone_number")

    # ── Assertion 3: audit columns auto-populated ──────────────────────────────
    etl_run = next((m for m in mappings if m["tgt_column"] == "etl_run_id"), None)
    created_at = next((m for m in mappings if m["tgt_column"] == "created_at"), None)
    assert etl_run is not None, "etl_run_id not auto-populated by resolver"
    assert created_at is not None, "created_at not auto-populated by resolver"
    print(f"✓ Audit columns auto-populated: etl_run_id={etl_run['business_logic']!r}")

    # ── Assertion 4: no unmapped mandatory columns ─────────────────────────────
    mandatory = {"customer_id", "order_date", "order_amount", "sku", "quantity"}
    mapped_tgt = {m["tgt_column"] for m in mappings if m["status"] != "unmapped"}
    missing_mandatory = mandatory - mapped_tgt
    assert not missing_mandatory, f"Mandatory columns not mapped: {missing_mandatory}"
    print(f"✓ All mandatory columns mapped: {mandatory}")

    # ── Assertion 5: ORDER_DATE cast to DATE ───────────────────────────────────
    date_map = next((m for m in mappings if m["src_field"] == "ORDER_DATE"), None)
    if date_map:
        has_cast = "DATE" in (date_map.get("business_logic") or "").upper()
        print(f"{'✓' if has_cast else '⚠'} ORDER_DATE business_logic: {date_map.get('business_logic')!r}")

    # ── Summary ────────────────────────────────────────────────────────────────
    high_conf    = sum(1 for m in mappings if m["confidence"] >= 0.85)
    review_count = sum(1 for m in mappings if m["status"] == "review")
    unmapped     = sum(1 for m in mappings if m["status"] == "unmapped")

    print(f"\nSummary: {high_conf} high-confidence | {review_count} needs review | {unmapped} unmapped")
    print("\n" + "=" * 60)
    print("All assertions passed ✓")
    print("=" * 60)

    # Dump full output for inspection
    output_path = Path(__file__).parent.parent / "test_output.json"
    output_path.write_text(json.dumps(mappings, indent=2))
    print(f"\nFull output → {output_path}")


if __name__ == "__main__":
    asyncio.run(run_test())
