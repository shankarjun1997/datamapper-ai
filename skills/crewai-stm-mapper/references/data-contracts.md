# Data Contracts — CrewAI STM Mapper

This file defines the exact dict shapes that flow between agents and into
the xREF session state. Always keep this in sync with `app/state.py` and
the existing L3 pipeline output in `app/core/pipeline.py`.

---

## Input: Session State Keys Used

| Key | Type | Description |
|---|---|---|
| `session["src_columns"]` | `List[Dict]` | Source columns from L1/L2 parse |
| `session["tgt_columns"]` | `List[Dict]` | Target BQ/DB columns from BQ crawl |
| `session["table_mappings"]` | `List[Dict]` | Pre-defined table pairs (may be empty) |
| `session["api_config"]["use_crew"]` | `bool` | Feature flag to enable crew |
| `session["api_config"]["provider"]` | `str` | LLM provider (claude/deepseek/etc.) |
| `session["api_config"]["model"]` | `str` | Model ID |

### `src_columns` element shape
```json
{
  "src_table": "ORDERS",
  "src_field": "CUST_ID",
  "src_type": "VARCHAR",
  "sample": "C001234",
  "nullable": false
}
```

### `tgt_columns` element shape
```json
{
  "tgt_table": "fact_orders",
  "tgt_column": "customer_id",
  "type": "STRING",
  "nullable": false
}
```

### `table_mappings` element shape
```json
{
  "src_table": "ORDERS",
  "tgt_table": "fact_orders",
  "confidence": 0.95
}
```

---

## Agent Output Contracts (JSON only — no markdown)

### Task 1: Schema Analyst output
```json
{
  "source_tables": [
    {"name": "ORDERS", "column_count": 42, "naming_convention": "UPPER_SNAKE"}
  ],
  "target_tables": [
    {"name": "fact_orders", "column_count": 38}
  ],
  "vendor_prefixes": ["VZ_", "CRM_"],
  "semantic_groups": {
    "customer": ["cust", "customer", "client", "subscriber"],
    "amount": ["amt", "amount", "value", "revenue", "billing_amt"]
  },
  "ambiguities": [
    {
      "type": "M:1_target",
      "description": "home_phone and mobile_phone both match target phone_number",
      "affected_columns": ["home_phone", "mobile_phone"]
    }
  ],
  "recommendation": "Strip VZ_ prefix from all source fields before matching."
}
```

### Task 2: Table Mapper output
```json
[
  {
    "src_table": "ORDERS",
    "tgt_table": "fact_orders",
    "confidence": 0.95,
    "rationale": "Direct semantic match — same domain, same grain.",
    "merge_strategy": "direct"
  },
  {
    "src_table": "LOOKUP_STATUS",
    "tgt_table": null,
    "confidence": 0.0,
    "rationale": "Reference/lookup table with no target equivalent. Load separately.",
    "merge_strategy": "out_of_scope"
  }
]
```

### Task 3: Column Mapper output
```json
[
  {
    "src_table": "ORDERS",
    "src_field": "CUST_ID",
    "src_type": "VARCHAR",
    "tgt_table": "fact_orders",
    "tgt_column": "customer_id",
    "mapping_type": "Direct",
    "mapping_relation": "1:1",
    "business_logic": "CUST_ID",
    "llm_confidence": 0.92,
    "rationale": "CUST_ID → customer_id: same concept, alias resolved via semantic group 'customer'.",
    "needs_review": false
  },
  {
    "src_table": "ORDERS",
    "src_field": "HOME_PHONE",
    "src_type": "VARCHAR",
    "tgt_table": "fact_orders",
    "tgt_column": "phone_number",
    "mapping_type": "Direct",
    "mapping_relation": "M:1",
    "business_logic": "HOME_PHONE",
    "llm_confidence": 0.75,
    "rationale": "M:1 conflict with MOBILE_PHONE — both match phone_number. Flagged for resolver.",
    "needs_review": true
  }
]
```

### Task 4: Ambiguity Resolver output
Same schema as Task 3 output, with conflicts resolved:
```json
[
  {
    "src_table": "ORDERS",
    "src_field": "HOME_PHONE",
    "tgt_table": "fact_orders",
    "tgt_column": "phone_number",
    "mapping_type": "Expression",
    "mapping_relation": "M:1",
    "business_logic": "COALESCE(HOME_PHONE, MOBILE_PHONE)",
    "llm_confidence": 0.88,
    "rationale": "M:1 resolved: HOME_PHONE is primary. MOBILE_PHONE merged as COALESCE fallback.",
    "needs_review": false
  },
  {
    "src_table": "ORDERS",
    "src_field": "MOBILE_PHONE",
    "tgt_table": "fact_orders",
    "tgt_column": "phone_number",
    "mapping_type": "Unused",
    "mapping_relation": "M:1",
    "business_logic": "",
    "llm_confidence": 0.0,
    "rationale": "Merged into HOME_PHONE via COALESCE — see HOME_PHONE row.",
    "needs_review": false
  },
  {
    "src_table": "ORDERS",
    "src_field": "__auto__",
    "tgt_table": "fact_orders",
    "tgt_column": "etl_run_id",
    "mapping_type": "Constant",
    "mapping_relation": "1:1",
    "business_logic": "GENERATE_UUID()",
    "llm_confidence": 1.0,
    "rationale": "Mandatory audit column — auto-populated.",
    "needs_review": false
  }
]
```

### Task 5: QA Validator output
Same schema as Task 4 output, with `qa_note` added on modified rows:
```json
[
  {
    "src_field": "ORDER_DATE",
    "tgt_column": "order_date",
    "mapping_type": "Direct",
    "business_logic": "DATE(ORDER_DATE)",
    "llm_confidence": 0.90,
    "qa_note": "Added DATE() cast: src is DATETIME, target is DATE.",
    "needs_review": false
  }
]
```

---

## Output: Session Mapping Format

After `_normalise_to_session_format()`, each row stored in `session["mappings"]`
must have ALL of these keys:

```json
{
  "id": "uuid-v4",
  "src_table": "ORDERS",
  "src_field": "CUST_ID",
  "src_type": "VARCHAR",
  "tgt_table": "fact_orders",
  "tgt_column": "customer_id",
  "mapping_type": "Direct",
  "mapping_relation": "1:1",
  "business_logic": "CUST_ID",
  "confidence": 0.876,
  "tier": "high",
  "status": "mapped",
  "llm_confidence": 0.92,
  "rationale": "Alias resolved via semantic group.",
  "qa_note": "",
  "needs_review": false,
  "pii": false,
  "sample": "C001234",
  "nullable": false,
  "gate2_approved": false
}
```

### Confidence tiers (from `conf_tier()`)
| Score | Tier | `status` |
|---|---|---|
| ≥ 0.85 | `high` | `mapped` |
| 0.70–0.84 | `medium` | `mapped` |
| 0.50–0.69 | `low` | `review` |
| < 0.50 | `very_low` | `review` |
| 0.0 (Unused) | — | `unmapped` |
