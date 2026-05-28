# xREF Agent — Prompt Engineering & Confidence Score Reference

> **Audience:** Data engineers, migration architects, and product owners working with the xREF Agent mapping pipeline.  
> **Purpose:** Explain every lever that affects LLM mapping quality and confidence scoring so you can tune them for any source/target combination without touching Python.

---

## Table of Contents

1. [How the Pipeline Works](#1-how-the-pipeline-works)
2. [The System Prompt — What the LLM Sees](#2-the-system-prompt--what-the-llm-sees)
3. [Confidence Score Deep Dive](#3-confidence-score-deep-dive)
4. [Domain Alias Dictionary](#4-domain-alias-dictionary)
5. [Migration Context API](#5-migration-context-api)
6. [FK Propagation Rules](#6-fk-propagation-rules)
7. [Value-Scale Rules](#7-value-scale-rules)
8. [Cross-Session Memory](#8-cross-session-memory)
9. [Mapping Relation Auto-Detection](#9-mapping-relation-auto-detection)
10. [Common Failure Modes & Fixes](#10-common-failure-modes--fixes)
11. [Prompt Recipes for Common Scenarios](#11-prompt-recipes-for-common-scenarios)
12. [Tuning Checklist Before Running a Session](#12-tuning-checklist-before-running-a-session)

---

## 1. How the Pipeline Works

```
Source Schema (CSV / DDL / Unity Catalog)
         │
         ▼  L1 — Parse
   src_tables [{name, columns:[{name, type, sample, nullable}]}]
         │
         ▼  L2 — Crawl Target
   bq_tables  [{table, columns:[{name, type, sample}]}]
         │
         ▼  Gate 1 (auto-approved currently)
         │
         ▼  L3 — LLM Semantic Mapping   ← THIS is where prompt quality matters
   mappings   [{src_field, tgt_table, tgt_column, mapping_type,
                mapping_relation, business_logic, confidence, …}]
         │
         ▼  Gate 2 — Human Review
         │
         ▼  L4 — SQL Generation
   generated_sql (CREATE OR REPLACE TABLE …)
```

**L3** runs in batches of 15 source columns. For each batch the agent:

1. Builds a system prompt (`_build_mapping_system`) injecting all context layers.
2. Builds a user prompt with source column descriptions + full target schema.
3. Calls the LLM provider (Claude / DeepSeek / OpenAI).
4. Post-processes results: computes composite confidence, auto-detects mapping_relation, fills in business_logic if the LLM left it blank.

---

## 2. The System Prompt — What the LLM Sees

The system prompt is assembled from **six layers in order**. Later layers override or extend earlier ones.

### Layer 1 — Base `MAPPING_SYSTEM` (always present)

Defines the task, output JSON schema, and core rules:

```
You are a senior data engineer performing cross-system Source-to-Target column mapping…

IMPORTANT ALIAS AWARENESS:
  • "customer" / "cust" / "subscriber" / "sub" / "client"  →  same concept
  • "customer_id" / "cust_id" / "sub_id" / "client_id"     →  same concept
  • "zip" / "geo_zip" / "postal_code" / "zipcode"          →  same concept
  …

Output format: JSON array with fields:
  src_field, tgt_table, tgt_column, mapping_type, mapping_relation,
  business_logic, llm_confidence, rationale
```

**Key rules baked in:**

| Rule | Effect |
|------|--------|
| `mapping_type=Unused` only when truly no target exists | Reduces false "Unused" |
| `llm_confidence` must be 0–1.0 float | Feeds composite score |
| Alias list in prompt header | Catches cross-vendor name mismatches |
| "Never fabricate target columns" | Prevents hallucinated column names |

### Layer 2 — Migration Context (optional, from `/migration-context` API)

Injected when you call `POST /api/sessions/{sid}/migration-context`. Contains:

- **Domain description** — tells the LLM *which systems* are involved
- **FK propagation rules** — tells the LLM which source key fans into multiple targets
- **Value-scale rules** — explicit transforms like `churn_risk_score × 100 → risk_of_churn_pct`

### Layer 3 — Cross-batch FK anchor context

After batch 1 resolves FK anchor columns (customer_id, account_id, etc.), those resolved mappings are injected into every subsequent batch so surrogate key references remain consistent across target tables.

### Layer 4 — User instructions

Free-text instructions stored in `session["instructions"]`. These override any default LLM behaviour. Example:

```
Map all _flag columns as BOOLEAN even if the source type is STRING.
Never map etl_load_timestamp — it is an audit column added by the pipeline.
```

### Layer 5 — Jira business context (if connected)

If a Jira ticket is linked, its summary/description is injected here so the LLM understands the *business* purpose of the migration (e.g. "SCD2 dimension for device inventory").

### Layer 6 — Learned mappings from prior sessions

The top-20 most-used mappings from `runtime/.xref_mapping_memory.json` are shown so the LLM can reuse patterns from approved sessions without being told explicitly.

---

## 3. Confidence Score Deep Dive

### Formula

```
confidence = name_sim × 0.30 + type_sim × 0.20 + llm_score × 0.50
```

| Component | Weight | Source | Range |
|-----------|--------|--------|-------|
| `name_sim` | 30% | Deterministic — fuzzy name matching + alias table | 0.0 – 1.0 |
| `type_sim` | 20% | Deterministic — `_TYPE_COMPAT` lookup table | 0.0 – 1.0 |
| `llm_score` | 50% | LLM self-reported `llm_confidence` | 0.0 – 1.0 |

### Floor & Ceiling Rules

These prevent a poorly-calibrated LLM from under- or over-scoring obvious matches:

```
IF name_sim ≥ 0.80 AND type_sim ≥ 0.80  →  score ≥ 0.82   (floor)
IF name_sim ≥ 0.90 AND type_sim ≥ 0.90  →  score ≥ 0.90   (strong floor)
IF name_sim < 0.20                        →  score ≤ 0.78   (ceiling — force review)
```

**Example — zip → geo_zip (the 71% bug, now fixed):**

| Component | Old value | New value | Reason |
|-----------|-----------|-----------|--------|
| `name_sim` | 0.54 | **0.95** | `geo_` stripped by vendor prefix regex; "zip" is a suffix of "geozip" → containment bonus 0.95 |
| `type_sim` | 1.00 | 1.00 | STRING → STRING, no change |
| `llm_score` | 0.62 | ~0.90 | System prompt now lists zip/geo_zip as alias pair |
| **composite** | **0.71** | **≥ 0.90** | Floor rule fires: name≥0.90 AND type≥0.90 → min 0.90 |

### `name_sim` — Four-pass strategy

```python
def _name_score(src, tgt):
    # Pass 1: canonical alias match  (zip → postal_code, geo_zip → postal_code → same → 1.0)
    # Pass 2: substring containment  ("zip" is suffix of "geozip" → 0.95)
    # Pass 3: rapidfuzz.ratio on vendor-stripped, underscore-removed names
    # Pass 4: rapidfuzz.token_sort_ratio (handles "id_customer" vs "customerid")
    return max(pass1, pass2, pass3, pass4)
```

### `type_sim` — Compatibility matrix

| src → tgt | Score | Notes |
|-----------|-------|-------|
| STRING → STRING | 1.00 | Exact |
| INT64 → INT64 | 1.00 | Exact |
| DATE → TIMESTAMP | 0.90 | Widening |
| NUMERIC → FLOAT64 | 0.90 | Widening |
| INT64 → NUMERIC | 0.85 | Widening |
| INT64 → FLOAT64 | 0.80 | Widening |
| STRING → BOOLEAN | 0.40 | Y/N flag pattern |
| FLOAT64 → BOOLEAN | 0.20 | Unlikely, force review |

**Adding new pairs:** edit `_TYPE_COMPAT` in `app/server.py`. Both directions need separate entries.

### Tier thresholds

| Tier | Score range | Default action |
|------|-------------|----------------|
| `high` | ≥ 0.80 | Auto-marked "mapped" |
| `medium` | 0.50 – 0.79 | Marked "review" — human must confirm |
| `low` | 0.01 – 0.49 | Marked "review" — likely wrong |
| `none` | 0.00 | Marked "unmapped" |

**To raise the auto-approve bar** (more conservative), change the threshold in `_run_pipeline`:

```python
# Line ~1962 in server.py:
"status": "unmapped" if is_unused else ("review" if confidence < 0.85 else "mapped"),
#                                                                       ↑ was 0.80
```

**To lower it** (more aggressive auto-mapping), change 0.80 to 0.70.

---

## 4. Domain Alias Dictionary

`_DOMAIN_ALIASES` in `app/server.py` maps field name fragments to canonical concepts. Both source and target names are canonicalized before comparison — if they resolve to the same concept, `name_sim = 1.0`.

### Current aliases (excerpt)

```python
"customer":       "client",       "cust":        "client",
"subscriber":     "client",       "sub":         "client",
"customer_id":    "customer_id",  "cust_id":     "customer_id",
"sub_id":         "customer_id",  "client_id":   "customer_id",
"zip":            "postal_code",  "geo_zip":     "postal_code",
"zipcode":        "postal_code",  "postal_code": "postal_code",
"churn_risk_score":"churn_risk_score", "risk_of_churn_pct":"churn_risk_score",
```

### Adding project-specific aliases

Edit `_DOMAIN_ALIASES` directly — no restart required if using `--reload`. Example for a media company:

```python
"subscriber_mrn":  "account_id",
"content_sku":     "product_id",
"stream_session":  "session_id",
```

### Vendor prefix stripping

Before alias lookup, field names are run through `_strip_vendor()` which removes:

```
frontier_  ftr_  vz_  verizon_  src_  tgt_  stg_  raw_  ods_  dw_  dwh_
geo_  loc_  addr_  net_  nw_  svc_  srvc_  dim_  fact_  rpt_  acct_  cust_
```

This means `frontier_customer_id` → `customer_id` and `vz_customer_key` → `customer_key` before the alias and fuzzy comparison runs.

---

## 5. Migration Context API

Set per-session context before running the pipeline:

```bash
curl -X POST http://localhost:7788/api/sessions/{SID}/migration-context \
  -H "Content-Type: application/json" \
  -d '{
    "domain_context": "Frontier Communications (Databricks Unity Catalog) → Verizon GCP BigQuery. Telecom/ISP network migration. Source uses frontier_ prefix, target uses vz_ prefix.",
    "fk_rules": [
      "customer_id: frontier.customer_id → vz_raw_dev.dim_customer.vz_customer_key",
      "account_id: frontier.account_id → vz_raw_dev.dim_account.vz_account_key",
      "device_id: frontier.device_id → vz_raw_dev.dim_device.vz_device_key"
    ],
    "scale_rules": [
      "churn_risk_score × 100 → risk_of_churn_pct (fraction to percent)",
      "signal_strength_dbm: raw dBm value, no scale needed"
    ],
    "source_table_fqn": "main_catalog.network_ops.frontier_customer_master"
  }'
```

### Fields

| Field | Type | Purpose |
|-------|------|---------|
| `domain_context` | string | Free-text description injected into system prompt |
| `fk_rules` | string[] | FK propagation rules — one source key fans into N target tables |
| `scale_rules` | string[] | Explicit value-scale transforms (fraction↔percent, unit conversions) |
| `source_table_fqn` | string | Full Unity Catalog path for the FROM clause in generated SQL |

### What gets injected into the LLM prompt

```
MIGRATION CONTEXT:
  Frontier Communications (Databricks Unity Catalog) → Verizon GCP BigQuery…
  
  FK PROPAGATION RULES:
    • customer_id: frontier.customer_id → vz_raw_dev.dim_customer.vz_customer_key
    …
  
  VALUE-SCALE RULES:
    • churn_risk_score × 100 → risk_of_churn_pct (fraction to percent)
    …
```

---

## 6. FK Propagation Rules

One of the most common causes of mapping ambiguity in large migrations is **FK fan-out**: the same source `customer_id` must appear in 4–6 normalized target tables as a foreign key, each with a slightly different column name (`vz_customer_key`, `customer_fk`, `cust_id`, etc.).

### Without FK rules (broken)

Each batch independently asks "where does `customer_id` go?" and can produce:

- Batch 1: `customer_id` → `fact_billing.vz_customer_key`
- Batch 3: `customer_id` → `fact_usage.customer_id` ← different name!
- Batch 5: `customer_id` → `dim_device.cust_fk` ← yet another name!

The generated SQL then JOINs on mismatched column names — runtime error.

### With FK rules (correct)

Setting a FK rule like:

```json
"customer_id: frontier.customer_id → vz_raw_dev.dim_customer.vz_customer_key"
```

tells the LLM:

1. **Explicitly** that `customer_id` is the anchor key
2. **Which target column** it maps to in the canonical dim table
3. **Cross-batch persistence**: after batch 1 resolves `customer_id`, that resolution is stored in `fk_anchors` and injected into all subsequent batches so all targets use the same name

### FK anchor auto-detection

Even without explicit FK rules, the pipeline auto-detects any source field matching:

```regex
(customer_id|account_id|device_id|service_id|order_id|ticket_id|_key|_fk|_sk|_id)
```

and propagates its first resolved target as the anchor for subsequent batches.

---

## 7. Value-Scale Rules

Numeric fields frequently exist at different scales between source and target systems. Common examples in telco migrations:

| Source field | Source sample | Target field | Target sample | Transform needed |
|---|---|---|---|---|
| `churn_risk_score` | 0.19 | `risk_of_churn_pct` | 19.0 | `× 100` |
| `signal_quality_ratio` | 0.87 | `signal_quality_pct` | 87.0 | `× 100` |
| `latency_ms` | 42 | `latency_sec` | 0.042 | `÷ 1000` |
| `dl_speed_bps` | 100000000 | `dl_speed_mbps` | 100.0 | `÷ 1000000` |

### Automatic detection (without explicit rules)

`_auto_business_logic` auto-generates the `× 100` transform when:
- Source type is `FLOAT64` or `NUMERIC`
- Source sample value is `≤ 1.0` (fraction range)
- Target field name contains `pct`, `percent`, `rate`, or `ratio`

### Explicit scale rules

For non-obvious cases, add to the `scale_rules` list in the migration context:

```json
"scale_rules": [
  "latency_ms ÷ 1000 → latency_sec",
  "dl_speed_bps ÷ 1000000 → dl_speed_mbps"
]
```

The LLM sees this instruction directly and generates the correct `ROUND(field / 1000, 6)` expression.

---

## 8. Cross-Session Memory

After Gate 2 approval, every confirmed mapping is written to `runtime/.xref_mapping_memory.json`. On the next session, the top-20 most-used remembered mappings are shown in the system prompt.

### Memory file structure

```json
{
  "customer_id": {
    "tgt_table": "dim_customer",
    "tgt_column": "vz_customer_key",
    "mapping_type": "Direct",
    "business_logic": "customer_id",
    "confidence": 0.96,
    "uses": 7,
    "last_updated": "2026-05-26T10:30:00Z"
  },
  …
}
```

### Managing memory

```bash
# View all learned mappings
GET /api/mapping-memory

# Delete a wrong mapping
DELETE /api/mapping-memory/customer_id

# Memory is auto-updated on every Gate 2 approval — no manual action needed
```

### When memory helps most

- **Same source system, different target tables** — memory from a billing session carries over to a usage session
- **Incremental migrations** — re-processing updated source schemas reuses all prior approved column decisions
- **Multi-batch large tables** — memory built from the first 50 columns helps the next 50

---

## 9. Mapping Relation Auto-Detection

After the LLM batch loop completes, a deterministic post-processing pass corrects `mapping_relation` regardless of what the LLM said.

### Algorithm

```
For each source field sf and target column tgt:
  n_tgts = |distinct targets sf maps to|
  n_srcs = |distinct sources mapping to tgt|

  if n_tgts > 1 AND n_srcs > 1  →  "M:M"   (fan-out AND aggregation)
  elif n_tgts > 1                →  "1:M"   (one source, many targets — FK fan-out)
  elif n_srcs > 1                →  "M:1"   (many sources, one target — CONCAT, COALESCE)
  else                           →  "1:1"   (direct)
```

### Example — FK fan-out correctly detected as 1:M

`customer_id` maps to all four target tables → `mapping_relation = "1:M"` automatically, even if the LLM said `"1:1"`.

### Example — CONCAT correctly detected as M:1

`first_name` + `last_name` → `full_name` → both source fields map to the same target → `mapping_relation = "M:1"` automatically.

---

## 10. Common Failure Modes & Fixes

### Confidence too low for obvious matches

**Symptom:** `zip → geo_zip` scoring 71%.  
**Root cause:** vendor prefix not stripped, no alias entry, LLM under-confident.  
**Fix:**
1. Add field to `_DOMAIN_ALIASES` (both names → same concept)
2. Add domain prefix to `_VENDOR_PREFIXES` regex
3. Check `_TYPE_COMPAT` — if types mismatch, add the pair

### LLM maps all columns to the same target table

**Symptom:** 60 source columns, all mapped to `fact_billing`.  
**Root cause:** Target schema presented without clear table scoping. LLM picks the first table it sees.  
**Fix:**
1. Use `POST /api/sessions/{sid}/migration-context` with `fk_rules` so the LLM understands the fan-out pattern
2. Enable table-mapping scoping via `POST /api/sessions/{sid}/table-mappings` to tell the pipeline which source table pairs with which target tables

### FK key maps to different column names across target tables

**Symptom:** `customer_id → fact_billing.vz_cust_key` but `customer_id → fact_usage.customer_id` — inconsistent.  
**Root cause:** Batches run independently without shared FK context.  
**Fix:** Set explicit `fk_rules` in migration context BEFORE running the pipeline. The FK anchor accumulator then propagates the first resolution to all subsequent batches.

### Y/N flag mapped as STRING → STRING (no BOOLEAN conversion)

**Symptom:** `auto_pay_enabled_flag (STRING, sample=Y) → auto_pay_enabled (BOOLEAN)` gets `business_logic = "auto_pay_enabled_flag"` instead of `CASE UPPER(TRIM(…)) WHEN 'Y' THEN TRUE …`  
**Root cause:** `_auto_business_logic` only fires when `business_logic` is empty AND `mapping_type ≠ Direct`. LLM sometimes sets `mapping_type=Direct` for Y/N flags and provides a business_logic itself.  
**Fix:** Add to user instructions:
```
For all _flag columns with STRING type and Y/N sample values, set mapping_type=Expression and generate a CASE UPPER(TRIM(src)) WHEN 'Y' THEN TRUE … END expression.
```

### Fraction mapped as Direct instead of × 100

**Symptom:** `churn_risk_score (FLOAT64, sample=0.19) → risk_of_churn_pct (FLOAT64)` gets `business_logic = "churn_risk_score"` (direct passthrough).  
**Root cause:** LLM sees the same type and calls it Direct. `_auto_business_logic` only fires when `business_logic` is empty.  
**Fix:** Add explicit scale rule:
```json
"scale_rules": ["churn_risk_score × 100 → risk_of_churn_pct (fraction to percent)"]
```

---

## 11. Prompt Recipes for Common Scenarios

### Telecom ISP migration (Frontier → Verizon BQ)

```bash
curl -X POST http://localhost:7788/api/sessions/{SID}/migration-context \
  -H "Content-Type: application/json" -d '{
    "domain_context": "Telecom/ISP migration. Frontier Communications Databricks Unity Catalog → Verizon GCP BigQuery. Source prefix: frontier_/ftr_. Target prefix: vz_. Normalized target schema (3NF → star schema). SCD2 dims: dim_payment, dim_device.",
    "fk_rules": [
      "customer_id → dim_customer.vz_customer_key",
      "account_id  → dim_account.vz_account_key",
      "device_id   → dim_device.vz_device_key",
      "service_id  → dim_service.vz_service_key"
    ],
    "scale_rules": [
      "churn_risk_score × 100 → risk_of_churn_pct",
      "signal_strength: raw dBm INTEGER, no scale change",
      "dl_speed_mbps: already in Mbps, no scale change"
    ],
    "source_table_fqn": "main_catalog.network_ops.frontier_customer_master"
  }'
```

### Retail OLTP → data warehouse

```json
{
  "domain_context": "Retail migration. Postgres OLTP → Snowflake data warehouse. Source is 3NF with snake_case. Target is star schema with business-friendly names.",
  "fk_rules": [
    "customer_id → dim_customer.customer_key",
    "product_id → dim_product.product_key",
    "store_id → dim_store.store_key"
  ],
  "scale_rules": [
    "unit_price: already in USD dollars, no scale change",
    "discount_rate × 100 → discount_pct"
  ]
}
```

### Forcing a conservative mapping (high-value financial data)

Add to session instructions:

```
Be conservative. Set llm_confidence ≤ 0.70 for any mapping where:
  - The source and target names differ by more than one word
  - The mapping requires a type cast
  - The business_logic is anything other than a direct column reference
This ensures a human reviews all non-trivial mappings.
```

### Forcing aggressive auto-mapping (well-understood schema pair)

Add to session instructions:

```
This is a schema version upgrade within the same system.
Column names are identical or differ only by a table prefix.
Set llm_confidence = 0.95 for all direct name matches.
Only set llm_confidence < 0.80 when the target column name is materially different.
```

---

## 12. Tuning Checklist Before Running a Session

Use this before triggering the pipeline for a new source/target pair:

- [ ] **Vendor prefixes covered?** Check `_VENDOR_PREFIXES` in `server.py` — add any new prefixes from your source system
- [ ] **Aliases populated?** Check `_DOMAIN_ALIASES` — add domain-specific synonyms for your business (e.g. "member" → "customer" for insurance)
- [ ] **FK rules defined?** For any source key that fans into 2+ target tables, add a `fk_rules` entry in migration context
- [ ] **Scale rules defined?** For any fraction/percentage or unit conversion, add a `scale_rules` entry
- [ ] **Source FQN set?** For Unity Catalog sources, set `source_table_fqn` so generated SQL uses the full `catalog.schema.table` path
- [ ] **User instructions written?** For any field-level overrides (e.g. "never map etl_load_timestamp"), add to `session["instructions"]`
- [ ] **Memory checked?** Review `GET /api/mapping-memory` — if prior sessions mapped differently, clear stale entries with `DELETE /api/mapping-memory/{field}`
- [ ] **Confidence threshold appropriate?** Default is 0.80. Raise to 0.85 for critical financial data, lower to 0.70 for exploratory mapping
- [ ] **Type compat matrix current?** If your source has unusual types (e.g. BIGNUMERIC, GEOGRAPHY, JSON), add entries to `_TYPE_COMPAT`

---

## Appendix A — Confidence Score Worked Examples

### Example 1: Perfect match (should be ~0.98)

| | |
|---|---|
| Source | `customer_id (INT64, sample=10045)` |
| Target | `vz_customer_key (INT64)` |
| `name_sim` | `_canonical_concept("customer_id") = "customer_id"` = `_canonical_concept("vz_customer_key")`  (after stripping `vz_`) = 1.0 |
| `type_sim` | INT64 → INT64 = 1.0 |
| `llm_score` | LLM sees alias hint → ~0.95 |
| **composite** | `1.0×0.30 + 1.0×0.20 + 0.95×0.50 = 0.775` + floor (name≥0.90, type≥0.90) → **0.90** |

### Example 2: zip → geo_zip (fixed, should be ~0.95)

| | |
|---|---|
| Source | `zip (STRING, sample=90210)` |
| Target | `geo_zip (STRING)` |
| `name_sim` | `geo_` stripped → "zip"; "zip" is suffix of "geozip" → containment 0.95. Also `_canonical_concept` → both "postal_code" → 1.0. Returns **1.0** |
| `type_sim` | STRING → STRING = **1.0** |
| `llm_score` | Alias listed in system prompt → ~0.92 |
| **composite** | `1.0×0.30 + 1.0×0.20 + 0.92×0.50 = 0.96` + floor → **0.96** |

### Example 3: Weak match (should stay in review)

| | |
|---|---|
| Source | `etl_batch_run_id (STRING)` |
| Target | `vz_customer_key (INT64)` |
| `name_sim` | No alias match; fuzzy "etlbatchrunid" vs "customkey" = ~0.15 |
| `type_sim` | STRING → INT64 = 0.40 |
| `llm_score` | LLM correctly says ~0.10 |
| **composite** | `0.15×0.30 + 0.40×0.20 + 0.10×0.50 = 0.175` + ceiling (name<0.20 → cap at 0.78) → **0.175** (review) |

---

## Appendix B — Adding a New LLM Provider

The agent supports Claude, DeepSeek, and OpenAI. To add a new provider:

1. Add an entry to `_PROVIDER_CATALOG` in `server.py`
2. Implement a new `complete()` and `complete_json()` method in the `LLMClient` class
3. Set `DM_PROVIDER=your_provider` in `.env`

The confidence scoring and prompt assembly layers are provider-agnostic — they work identically regardless of which model processes the prompt.

---

*Generated by xREF Agent prompt engineering review — last updated 2026-05-26*
