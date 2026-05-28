---
name: crewai-stm-mapper
description: >
  Implement, modify, or debug the CrewAI multi-agent Source-to-Target Mapping (STM)
  pipeline inside the xREF DataMapper system (app/core/crew_pipeline.py).
  Invoke this skill whenever the user mentions: improving mapping quality, adding
  CrewAI agents, reducing unmapped columns, resolving ambiguous mappings, boosting
  confidence scores, multi-agent reasoning for data mapping, Task #67, or any time
  the single-LLM mapping pass produces too many low-confidence or incorrect results.
  Also use this skill when wiring the crew pipeline into the FastAPI session run
  endpoint (/api/sessions/{sid}/run), when the user asks to "use agents" for mapping,
  or when working on the self-learning system (crew_learnings.py, pattern extraction,
  SKILL.md auto-refresh, feedback endpoints).
---

# CrewAI STM Mapper Skill

## Mental Model

The existing xREF pipeline uses a single LLM call per table chunk (L3 stage in
`app/core/pipeline.py`). This is fast but produces ambiguous or wrong mappings when:

- Source and target use different naming conventions (cust_id vs customer_id)
- Multiple source columns plausibly map to the same target column
- A single source column could map to multiple targets
- The schema is wide (100+ columns) and the LLM loses context across chunks
- Business logic for derived/computed columns requires reasoning across multiple fields

The CrewAI approach replaces the single-shot L3 call with a **five-agent crew** that
reasons progressively and challenges its own output before committing to mappings.
Think of it as four specialists in a room with a moderator — they debate, then agree.

Read `references/data-contracts.md` before writing any agent or task code — it defines
the exact dict shapes that flow between agents and into the session state.

---

## File Locations

| Purpose | Path |
|---|---|
| Crew pipeline entry point | `app/core/crew_pipeline.py` (create if absent) |
| Agent definitions | `app/core/crew_pipeline.py` (inline, not separate files) |
| Integration hook in main pipeline | `app/core/pipeline.py` — `_run_pipeline()` function |
| Existing single-LLM mapping | `app/core/pipeline.py` L3 block (~line 318) |
| Confidence scoring | `app/intelligence/confidence.py` |
| Mapping memory (cross-session hints) | `app/core/mapping_memory.py` |
| Session state shape | `app/state.py` → `_sessions[sid]` dict |
| System prompt for mapping | `app/core/pipeline.py` → `MAPPING_SYSTEM` constant |

The crew pipeline is **additive** — the existing L3 path stays intact. Add a feature
flag `"use_crew": true` in `session["api_config"]` to route sessions through CrewAI.

---

## Five-Agent Crew Architecture

```
Source Schema + Target Schema + Table Mappings
        │
        ▼
┌─────────────────────┐
│  1. Schema Analyst  │  Understands both schemas, flags ambiguities upfront
└──────────┬──────────┘
           │ structured schema brief + ambiguity list
           ▼
┌─────────────────────┐
│  2. Table Mapper    │  Proposes src→tgt table pairs with rationale
└──────────┬──────────┘
           │ confirmed table mapping pairs
           ▼
┌─────────────────────┐
│  3. Column Mapper   │  For each table pair: maps columns with business logic
└──────────┬──────────┘
           │ raw column mappings (may contain conflicts)
           ▼
┌─────────────────────┐
│  4. Ambiguity       │  Detects M:1 conflicts, low-confidence rows, missing
│     Resolver        │  mandatory columns — produces a resolution memo
└──────────┬──────────┘
           │ resolved mappings
           ▼
┌─────────────────────┐
│  5. QA Validator    │  Final consistency check, type compatibility, SQL validity
└──────────┬──────────┘
           │ final validated mapping list (xREF session format)
           ▼
        session["mappings"]
```

Agents run **sequentially** (each gets the output of the previous). This is intentional:
ambiguity resolution only makes sense after column mapping is complete, and QA only
makes sense after resolution.

---

## Installation

```bash
pip install crewai crewai-tools
# If using local Ollama models:
pip install ollama
```

Add to `requirements.txt`:
```
crewai>=0.28.0
crewai-tools>=0.1.0
```

---

## Core Implementation — `app/core/crew_pipeline.py`

Create this file. It exports one async function: `run_crew_mapping(session, emit)`.

### Imports and LLM setup

```python
"""
app/core/crew_pipeline.py
CrewAI five-agent pipeline for Source-to-Target column mapping.
Activated when session["api_config"]["use_crew"] is True.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

from crewai import Agent, Crew, Process, Task

from app.config import _ANTHROPIC_API_KEY, _DEEPSEEK_API_KEY, _CLAUDE_MODEL, _DEEPSEEK_MODEL
from app.core.mapping_memory import _recall_mapping_hints
from app.intelligence.confidence import compute_confidence, conf_tier, _name_score, _type_score
from app.intelligence.business_logic import _auto_business_logic


def _crew_llm(session: Dict) -> Any:
    """Build a CrewAI-compatible LLM object from session config.

    CrewAI accepts either a string model identifier (uses LiteLLM under the hood)
    or a langchain ChatModel object. We use the string form for simplicity.
    """
    cfg = session.get("api_config", {})
    provider = cfg.get("provider", "claude").lower()
    model_id = cfg.get("model", "")

    if provider == "claude":
        # LiteLLM prefix for Anthropic
        model_id = model_id or _CLAUDE_MODEL
        return f"anthropic/{model_id}"
    elif provider == "openai":
        return model_id or "gpt-4o"
    elif provider == "deepseek":
        # LiteLLM routes deepseek via openai-compat
        return f"openai/{model_id or _DEEPSEEK_MODEL}"
    elif provider == "groq":
        return f"groq/{model_id}"
    elif provider == "ollama":
        return f"ollama/{model_id or 'llama3.2'}"
    else:
        # Fallback: let LiteLLM figure it out
        return model_id or "anthropic/claude-sonnet-4-6"
```

### Agent factory functions

Define each agent as a function that receives the LLM string and returns a
`crewai.Agent`. Keep agents **focused on one concern** — do not give the Column Mapper
agent responsibility for ambiguity resolution; that causes internal contradictions.

```python
def _schema_analyst_agent(llm: str) -> Agent:
    return Agent(
        role="Senior Data Architect",
        goal=(
            "Produce a structured brief of source and target schemas: table inventory, "
            "column counts, data types, naming conventions used, and a prioritised list "
            "of mapping ambiguities that downstream agents must resolve."
        ),
        backstory=(
            "You have mapped hundreds of enterprise schemas across Oracle, Postgres, "
            "Snowflake, and BigQuery. You know that cust_id and customer_id are the same, "
            "that VZ_ prefixes mean Verizon-specific, and that BILLING_ prefix fields "
            "always belong in billing target tables. Your job is to surface these insights "
            "before the mapping agents start work, so they don't make the same mistakes "
            "every junior analyst makes."
        ),
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )


def _table_mapper_agent(llm: str) -> Agent:
    return Agent(
        role="Table Mapping Specialist",
        goal=(
            "Propose confident, justified table-level pairs (src_table → tgt_table) "
            "using the schema brief. Every source table must be accounted for — either "
            "mapped, merged into another target, or marked as out-of-scope with a reason."
        ),
        backstory=(
            "You think at the table level, not the column level. You know that a source "
            "ORDERS table and ORDERLINES table often both feed a single DWH FACT_ORDERS "
            "target, and that lookup/reference tables frequently have no target equivalent. "
            "You produce a mapping plan that the column mapper can execute without ambiguity "
            "about which target table to use."
        ),
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )


def _column_mapper_agent(llm: str) -> Agent:
    return Agent(
        role="Column Mapping Engineer",
        goal=(
            "For each confirmed table pair, map every source column to the best target "
            "column. Produce the business_logic SQL expression for derived/computed columns. "
            "Flag any column where you are less than 70%% confident as 'needs_review=true'."
        ),
        backstory=(
            "You are a detail-oriented engineer who never leaves a source column unmapped "
            "without a documented reason. You know SQL cold — CAST, COALESCE, DATE_TRUNC, "
            "REGEXP_REPLACE — and you write business_logic that is valid for the target "
            "platform (BigQuery, Snowflake, or ANSI SQL). You always check: do two source "
            "columns map to the same target? If so, flag it. Does the data type require a "
            "cast? Write it. Is the field computed from multiple sources? Use COALESCE or "
            "CONCAT. Document every decision in the rationale field."
        ),
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )


def _ambiguity_resolver_agent(llm: str) -> Agent:
    return Agent(
        role="Mapping Ambiguity Resolver",
        goal=(
            "Examine the raw column mappings for: (1) M:1 conflicts — multiple source "
            "columns claiming the same target column, (2) low-confidence rows — "
            "needs_review=true, (3) missing mandatory target columns — columns in the "
            "target schema that appear in zero mappings. For each issue, produce a "
            "concrete resolution: pick a winner for M:1, propose a Derived mapping for "
            "mandatory misses, or mark as Unused with justification."
        ),
        backstory=(
            "You are the quality gate before human review. Your job is to eliminate "
            "mechanical ambiguity so reviewers spend their time on business decisions, "
            "not data plumbing. You have seen every common pattern: two source phone "
            "fields (home_phone and mobile_phone) fighting over one target phone_number "
            "column — you pick the primary one and make the other a COALESCE fallback. "
            "A mandatory target audit column (created_at, updated_at, etl_run_id) with "
            "no source — you auto-populate with CURRENT_TIMESTAMP() or a session constant."
        ),
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )


def _qa_validator_agent(llm: str) -> Agent:
    return Agent(
        role="Mapping QA Engineer",
        goal=(
            "Perform a final consistency and correctness sweep over the resolved mapping "
            "list. Check: SQL expressions are syntactically valid for the target dialect, "
            "data types are compatible (no silent truncations), every row has a non-empty "
            "tgt_table and tgt_column (except Unused rows), confidence scores are realistic "
            "(not all 1.0, not all 0.5). Return the final mapping list as valid JSON only."
        ),
        backstory=(
            "You are the last line of defence before mappings reach the human reviewer. "
            "You have found bugs like CAST(varchar_field AS INT64) on a field that contains "
            "letters, or a confidence of 1.0 on a field that was clearly guessed. You never "
            "let bad SQL reach production. You output ONLY the JSON array — no markdown, "
            "no prose, no code fences."
        ),
        llm=llm,
        verbose=True,
        allow_delegation=False,
    )
```

### Task definitions

Each task receives the previous task's output via `context=[previous_task]`. This is
the CrewAI mechanism for sequential information flow. Keep task descriptions concrete
and output-format-prescriptive — vague task descriptions produce vague outputs.

```python
def _build_tasks(
    agents: dict,
    source_schema: dict,
    target_schema: dict,
    table_mappings: List[dict],
    hints: dict,
    target_dialect: str = "bigquery",
) -> List[Task]:
    """Build the five tasks. Returns them in execution order."""

    src_json = json.dumps(source_schema, indent=2)
    tgt_json = json.dumps(target_schema, indent=2)
    tbl_json = json.dumps(table_mappings, indent=2)
    hints_json = json.dumps(hints, indent=2) if hints else "{}"

    # ── Task 1: Schema Analysis ───────────────────────────────────────────────
    t1_analyse = Task(
        description=f"""Analyse these schemas and produce a structured brief.

SOURCE SCHEMA:
{src_json}

TARGET SCHEMA:
{tgt_json}

PRE-DEFINED TABLE MAPPINGS (if any):
{tbl_json}

MEMORY HINTS FROM PRIOR SESSIONS (high-confidence, prefer these):
{hints_json}

Produce a JSON object with these keys:
{{
  "source_tables": [{{"name": str, "column_count": int, "naming_convention": str}}],
  "target_tables": [{{"name": str, "column_count": int}}],
  "vendor_prefixes": ["list of detected prefixes like VZ_, CRM_, etc."],
  "semantic_groups": {{"concept": ["synonyms"]}},
  "ambiguities": [
    {{
      "type": "M:1_target" | "missing_mandatory" | "naming_alias" | "type_mismatch",
      "description": str,
      "affected_columns": [str]
    }}
  ],
  "recommendation": str
}}""",
        expected_output="Valid JSON object matching the schema above. No markdown.",
        agent=agents["analyst"],
    )

    # ── Task 2: Table Mapping ─────────────────────────────────────────────────
    t2_tables = Task(
        description=f"""Using the schema brief from the analyst, propose definitive
table-level mappings.

If pre-defined table mappings exist, validate and use them. If they are empty or
incomplete, propose new ones based on semantic similarity and domain alignment.

Return a JSON array:
[
  {{
    "src_table": str,
    "tgt_table": str,
    "confidence": 0.0-1.0,
    "rationale": str,
    "merge_strategy": "direct" | "union" | "join" | "split" | "out_of_scope"
  }}
]

Every source table must appear exactly once.
'out_of_scope' tables get tgt_table=null and an explanation in rationale.""",
        expected_output="Valid JSON array of table mapping objects. No markdown.",
        agent=agents["table_mapper"],
        context=[t1_analyse],
    )

    # ── Task 3: Column Mapping ────────────────────────────────────────────────
    t3_columns = Task(
        description=f"""Map every source column to the best target column.
Use the confirmed table pairs from the table mapper.
Use the analyst's semantic groups and memory hints to resolve aliases.
Target SQL dialect: {target_dialect}

For each source column produce:
{{
  "src_table": str,
  "src_field": str,
  "src_type": str,
  "tgt_table": str,
  "tgt_column": str,
  "mapping_type": "Direct" | "Derived" | "Lookup" | "Constant" | "Expression" | "Unused",
  "mapping_relation": "1:1" | "1:M" | "M:1",
  "business_logic": "valid SQL expression",
  "llm_confidence": 0.0-1.0,
  "rationale": str,
  "needs_review": true | false
}}

Rules:
- needs_review=true when llm_confidence < 0.70
- mapping_type=Unused ONLY when no plausible target exists — document why
- business_logic must be valid {target_dialect} SQL:
    Direct same type  →  column name only (e.g. customer_id)
    Cast needed       →  CAST(src AS TYPE) or DATE(src)
    Computed          →  COALESCE(a, b), CONCAT(a, ' ', b), etc.
- M:1 mappings: set mapping_relation="M:1" on every source column that shares a target
- Do NOT resolve M:1 conflicts yet — flag them and let the resolver handle it

Return a JSON array. No markdown, no explanations outside the JSON fields.""",
        expected_output="Valid JSON array of column mapping objects. No markdown.",
        agent=agents["column_mapper"],
        context=[t1_analyse, t2_tables],
    )

    # ── Task 4: Ambiguity Resolution ──────────────────────────────────────────
    t4_resolve = Task(
        description=f"""Resolve all ambiguities in the column mapping list.

TARGET SCHEMA (for mandatory column detection):
{tgt_json}

Detect and resolve:

1. M:1 CONFLICTS — multiple source columns with the same (tgt_table, tgt_column):
   - Pick the primary source (highest llm_confidence or most direct semantic match)
   - For others: change business_logic to a COALESCE(primary, secondary) expression
     on the primary row, and mark secondary rows as mapping_type=Unused with
     rationale="Merged into [primary_field] via COALESCE"

2. LOW-CONFIDENCE ROWS (needs_review=true):
   - Re-examine each one using the analyst's semantic groups
   - If a better match exists: update tgt_column and raise llm_confidence
   - If still uncertain: keep needs_review=true but improve the rationale

3. MISSING MANDATORY TARGET COLUMNS — target columns that appear in 0 mappings:
   - Audit columns (created_at, updated_at, etl_run_id, batch_id, load_ts):
     auto-map with business_logic=CURRENT_TIMESTAMP() and mapping_type=Constant
   - Business columns: add a new Derived mapping row with business_logic explaining
     how to derive it, or flag as Unused if truly out of scope

Return the complete resolved mapping list as a JSON array using the same schema
as the column mapper output. Every row from the input must appear in the output
(some may be modified, none deleted).""",
        expected_output="Valid JSON array — complete resolved mapping list. No markdown.",
        agent=agents["resolver"],
        context=[t1_analyse, t3_columns],
    )

    # ── Task 5: QA Validation ─────────────────────────────────────────────────
    t5_qa = Task(
        description=f"""Perform a final QA pass on the resolved mapping list.

Check every row for:
1. tgt_table and tgt_column are non-empty (except Unused rows)
2. business_logic is syntactically valid {target_dialect} SQL (no broken parentheses,
   unknown functions, or placeholder text like <column_name>)
3. llm_confidence is realistic — not 1.0 on clearly ambiguous fields, not 0.0 on
   obvious direct matches
4. mapping_type is one of: Direct | Derived | Lookup | Constant | Expression | Unused
5. mapping_relation is one of: 1:1 | 1:M | M:1

Fix any violations you find. Add a "qa_note" field to rows you modified explaining
what you changed and why.

Return ONLY the final JSON array. No markdown, no explanations, no code fences.
The output of this task is fed directly into the application database.""",
        expected_output="Valid JSON array ready for application consumption. Absolutely no markdown.",
        agent=agents["qa"],
        context=[t4_resolve],
    )

    return [t1_analyse, t2_tables, t3_columns, t4_resolve, t5_qa]
```

### Crew assembly and execution

```python
async def run_crew_mapping(session: Dict, emit) -> List[Dict]:
    """
    Main entry point. Runs the five-agent crew and returns a list of mapping dicts
    in the same format as the existing L3 pipeline output.

    Args:
        session:  The full xREF session dict from _sessions[sid]
        emit:     The SSE emit coroutine — call await emit("stage", {...}) for progress

    Returns:
        List of mapping dicts ready to be stored in session["mappings"]
    """
    from app.intelligence.confidence import compute_confidence, conf_tier, _name_score, _type_score
    from app.intelligence.business_logic import _auto_business_logic

    await emit("stage", {"stage": "L3", "status": "running",
                         "msg": "CrewAI: initialising five-agent mapping crew..."})

    llm = _crew_llm(session)
    target_dialect = _detect_target_dialect(session)

    # Build agent roster
    agents = {
        "analyst":      _schema_analyst_agent(llm),
        "table_mapper": _table_mapper_agent(llm),
        "column_mapper": _column_mapper_agent(llm),
        "resolver":     _ambiguity_resolver_agent(llm),
        "qa":           _qa_validator_agent(llm),
    }

    # Gather source columns from session (already parsed by L1/L2 stages)
    source_schema = _extract_source_schema(session)
    target_schema = _extract_target_schema(session)
    table_mappings = session.get("table_mappings", [])

    # Pull memory hints for source fields
    src_fields = [col["name"] for tbl in source_schema.get("tables", [])
                  for col in tbl.get("columns", [])]
    hints = _recall_mapping_hints(src_fields)

    # Build and run tasks
    tasks = _build_tasks(agents, source_schema, target_schema,
                         table_mappings, hints, target_dialect)

    await emit("stage", {"stage": "L3", "status": "running",
                         "msg": "CrewAI: launching sequential crew (5 agents)..."})

    crew = Crew(
        agents=list(agents.values()),
        tasks=tasks,
        process=Process.sequential,  # each agent waits for the previous
        verbose=True,
        memory=False,  # we use our own mapping_memory; disable CrewAI's
    )

    # CrewAI is sync — run in thread to not block FastAPI event loop
    import asyncio
    result = await asyncio.to_thread(crew.kickoff)

    await emit("stage", {"stage": "L3", "status": "running",
                         "msg": "CrewAI: post-processing crew output..."})

    # Parse and normalise the final output
    raw_mappings = _parse_crew_output(result)
    final_mappings = _normalise_to_session_format(raw_mappings, session)

    await emit("stage", {"stage": "L3", "status": "done",
                         "msg": f"CrewAI: {len(final_mappings)} mappings produced."})

    return final_mappings
```

### Helper functions

```python
def _detect_target_dialect(session: Dict) -> str:
    """Infer the SQL dialect from session target config."""
    target_mode = session.get("target_mode", "bq")
    if target_mode == "bq":
        return "bigquery"
    conn_str = session.get("target_conn_str", "").lower()
    if "snowflake" in conn_str:
        return "snowflake"
    if "spark" in conn_str or "databricks" in conn_str:
        return "spark"
    return "ansi"


def _extract_source_schema(session: Dict) -> Dict:
    """Build a clean source schema dict from session columns."""
    tables: Dict[str, List] = {}
    for col in session.get("src_columns", []):
        tbl = col.get("src_table", "unknown")
        tables.setdefault(tbl, []).append({
            "name": col["src_field"],
            "type": col.get("src_type", "STRING"),
            "sample": col.get("sample", ""),
            "nullable": col.get("nullable", True),
        })
    return {"tables": [{"name": t, "columns": c} for t, c in tables.items()]}


def _extract_target_schema(session: Dict) -> Dict:
    """Build a clean target schema dict from session BQ/target columns."""
    tables: Dict[str, List] = {}
    for col in session.get("tgt_columns", []):
        tbl = col.get("tgt_table", "unknown")
        tables.setdefault(tbl, []).append({
            "name": col.get("tgt_column") or col.get("name", ""),
            "type": col.get("type", "STRING"),
            "nullable": col.get("nullable", True),
        })
    return {"tables": [{"name": t, "columns": c} for t, c in tables.items()]}


def _parse_crew_output(crew_result) -> List[Dict]:
    """Extract the JSON array from the final crew task output.

    CrewAI returns the last task's output as a string. We extract the JSON array
    robustly — the QA agent is instructed not to use markdown but we defend
    against it anyway.
    """
    raw = str(crew_result)

    # Strip markdown fences if present (defensive)
    fence = re.search(r"```(?:json)?\s*([\s\S]*?)```", raw)
    if fence:
        raw = fence.group(1).strip()

    # Try direct parse first
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return result
        # If the crew wrapped it: {"mappings": [...]}
        for key in ("mappings", "result", "data", "output"):
            if key in result and isinstance(result[key], list):
                return result[key]
    except json.JSONDecodeError:
        pass

    # Fallback: find the first JSON array in the string
    arr_match = re.search(r"(\[[\s\S]*\])", raw)
    if arr_match:
        try:
            return json.loads(arr_match.group(1))
        except json.JSONDecodeError:
            pass

    raise ValueError(
        f"CrewAI QA agent did not return valid JSON. Output preview:\n{raw[:500]}"
    )


def _normalise_to_session_format(raw: List[Dict], session: Dict) -> List[Dict]:
    """Convert crew output to the exact format session['mappings'] expects.

    The existing pipeline stores mappings with these required keys:
    id, src_table, src_field, src_type, tgt_table, tgt_column,
    mapping_type, mapping_relation, business_logic, confidence, tier,
    status, llm_confidence, rationale, pii, sample, nullable

    The crew may omit some of these — we fill in defaults and compute
    the ensemble confidence score.
    """
    import uuid as _uuid
    from app.intelligence.confidence import compute_confidence, conf_tier, _name_score, _type_score
    from app.intelligence.business_logic import _auto_business_logic

    normalised = []
    for row in raw:
        src_field = row.get("src_field", "")
        tgt_column = row.get("tgt_column", "")
        src_type = row.get("src_type", "STRING")

        # Find target column type from session target schema
        tgt_type = _lookup_tgt_type(session, row.get("tgt_table", ""), tgt_column)

        # Ensemble confidence: blend name similarity, type similarity, and LLM score
        llm_conf = float(row.get("llm_confidence", 0.5))
        is_unused = row.get("mapping_type", "Direct") == "Unused"
        if is_unused:
            confidence = 0.0
        else:
            name_sim = _name_score(src_field, tgt_column)
            type_sim = _type_score(src_type, tgt_type)
            confidence = compute_confidence(name_sim, type_sim, llm_conf)

        # Auto-fill business_logic if empty on Direct mappings
        biz_logic = row.get("business_logic", "").strip()
        if not biz_logic and not is_unused:
            biz_logic = _auto_business_logic(src_field, tgt_column, src_type, tgt_type)

        status = "unmapped" if is_unused else ("review" if confidence < 0.8 else "mapped")

        normalised.append({
            "id":               str(_uuid.uuid4()),
            "src_table":        row.get("src_table", ""),
            "src_field":        src_field,
            "src_type":         src_type,
            "tgt_table":        row.get("tgt_table", ""),
            "tgt_column":       tgt_column,
            "mapping_type":     row.get("mapping_type", "Direct"),
            "mapping_relation": row.get("mapping_relation", "1:1"),
            "business_logic":   biz_logic,
            "confidence":       round(confidence, 3),
            "tier":             conf_tier(confidence),
            "status":           status,
            "llm_confidence":   round(llm_conf, 3),
            "rationale":        row.get("rationale", ""),
            "qa_note":          row.get("qa_note", ""),
            "needs_review":     row.get("needs_review", confidence < 0.7),
            "pii":              row.get("pii", False),
            "sample":           row.get("sample", ""),
            "nullable":         row.get("nullable", True),
            "gate2_approved":   False,
        })

    return normalised


def _lookup_tgt_type(session: Dict, tgt_table: str, tgt_column: str) -> str:
    """Look up the data type of a target column from session BQ schema."""
    for col in session.get("tgt_columns", []):
        if (col.get("tgt_table") == tgt_table and
                (col.get("tgt_column") or col.get("name")) == tgt_column):
            return col.get("type", "STRING")
    return "STRING"
```

---

## Integration — Wiring into `_run_pipeline()`

In `app/core/pipeline.py`, find the L3 block (around `session["stage"] = "L3"`).
Add the crew branch **before** the existing single-shot LLM path:

```python
# ── L3: Semantic Mapping ───────────────────────────────────────────────────
session["stage"] = "L3"

use_crew = session.get("api_config", {}).get("use_crew", False)

if use_crew:
    # ── CrewAI multi-agent path ──────────────────────────────────────────
    try:
        from app.core.crew_pipeline import run_crew_mapping
        all_mappings = await run_crew_mapping(session, emit)
        session["mappings"] = all_mappings
        # Fall through to stats calculation below
    except Exception as crew_err:
        logger.warning("CrewAI mapping failed (%s) — falling back to single-LLM L3", crew_err)
        use_crew = False  # triggers the existing path below

if not use_crew:
    # ── Existing single-LLM path (unchanged) ────────────────────────────
    ...  # existing L3 code stays here
```

The `use_crew` flag is set in `session["api_config"]`. Expose it in the frontend
Settings panel as a toggle: **"Use CrewAI multi-agent mapping (slower, higher quality)"**.

---

## Ambiguity Avoidance Patterns

These are the most common pipeline ambiguities and how the crew is designed to
prevent them. Reference these when writing or debugging agent prompts.

### Pattern 1: M:1 Target Conflict
**Symptom**: Two source columns (e.g. `home_phone`, `mobile_phone`) both map to
`target.phone_number` with high confidence. The session ends up with duplicate
target assignments.

**Prevention**: The Column Mapper is explicitly told to flag M:1 with
`mapping_relation="M:1"` but NOT to resolve it. The Ambiguity Resolver owns
resolution: it picks the primary field (higher confidence) and rewrites the
secondary as `COALESCE(home_phone, mobile_phone)` on the primary row.

### Pattern 2: Naming Alias Confusion
**Symptom**: `customer_id` in source does not match `client_id` in target, so
the LLM marks it Unused despite them being the same concept.

**Prevention**: The Schema Analyst extracts `semantic_groups` (e.g. `"customer":
["customer", "cust", "client", "subscriber"]`) which the Column Mapper uses as
context. Memory hints from prior sessions (`_recall_mapping_hints`) are injected
into Task 1 as high-priority signals.

### Pattern 3: Audit Column Orphans
**Symptom**: Target schema has `created_at`, `updated_at`, `etl_run_id` that no
source column maps to. These ship unmapped, causing NOT NULL violations at load time.

**Prevention**: The Ambiguity Resolver explicitly scans for target columns appearing
in zero mappings and auto-populates them: `CURRENT_TIMESTAMP()` for timestamps,
session constants for IDs. These get `mapping_type="Constant"` and `confidence=1.0`.

### Pattern 4: Chunk Context Loss
**Symptom**: On wide tables (100+ columns), the single-LLM path processes columns
in chunks of 30. Columns in chunk 2 lose context about decisions made in chunk 1
(e.g. chunk 1 already assigned `customer_id`; chunk 2 tries to assign it again).

**Prevention**: CrewAI agents receive the ENTIRE schema, not chunks. The Column
Mapper sees all columns at once. For very wide schemas (200+ columns), split by
`src_table` rather than arbitrary chunks — each table run is a separate crew kickoff.

### Pattern 5: Type Mismatch Silent Corruption
**Symptom**: `VARCHAR(10)` source field maps to `INT64` target. The pipeline
accepts it; the ETL job fails at load time with a cast error.

**Prevention**: The QA Validator explicitly checks type compatibility and rewrites
`business_logic` to include the correct CAST. It also flags `needs_review=True`
on any field where the type mismatch is semantic (e.g. an amount stored as VARCHAR
that should become NUMERIC — this needs business confirmation).

### Pattern 6: Vendor-Prefix Stripping
**Symptom**: Source field `VZ_CUST_ACCT_ID` does not match target `account_id`.
The LLM treats `VZ_` as meaningful and misses the connection.

**Prevention**: The Schema Analyst detects vendor prefixes (patterns like `VZ_`,
`CRM_`, `DW_`, `STG_`) and lists them in `vendor_prefixes`. The Column Mapper
is instructed to strip these before comparing field names.

---

## Confidence Score in Crew Context

The crew uses an **ensemble confidence** approach — do not let the LLM's
`llm_confidence` alone determine the final score. Always run it through
`compute_confidence()` from `app/intelligence/confidence.py`:

```python
# How compute_confidence works (from app/intelligence/confidence.py):
# final = 0.4 * name_sim + 0.2 * type_sim + 0.4 * llm_score
# where name_sim comes from _name_score() (vendor-stripped fuzzy ratio)
# and type_sim from _type_score() (BigQuery type compatibility)
```

A crew agent that says `llm_confidence=1.0` on a vendor-prefixed field that
barely name-matches will be pulled down by `name_sim ≈ 0.3`, producing a
realistic final confidence of ~0.6 (review tier). This is intentional — it
prevents the crew from being overconfident.

---

## Adding a New Agent

When adding a new agent to the crew (e.g. a Business Rules Agent that validates
against documented data dictionary rules):

1. Define a `_my_new_agent(llm)` function following the pattern above
2. Add it to the `agents` dict in `run_crew_mapping()`
3. Create a new `Task` with `context=[relevant_previous_tasks]`
4. Insert it at the right point in the `tasks` list
5. Update the `Crew(agents=..., tasks=...)` call
6. Do NOT change the QA Validator task — it must always be last

Do not add agents just because you can. Each agent adds latency (~15-30s on
Claude Sonnet). The five-agent design is the sweet spot for mapping quality vs. speed.

---

## Testing

Run a quick smoke test from the project root:

```bash
cd /path/to/dmapper
python scripts/test_crew_mapping.py
```

See `scripts/test_crew_mapping.py` for a minimal session fixture with 10 source
columns, 2 source tables, and a BQ target schema.

To test the M:1 conflict resolution specifically, include two source columns
(`home_phone`, `mobile_phone`) that both match `target_phone_number` — the
resolver should output exactly one mapped row with `COALESCE` and one Unused row.

---

## Common Errors

| Error | Cause | Fix |
|---|---|---|
| `ModuleNotFoundError: crewai` | Package not installed | `pip install crewai crewai-tools` |
| `AuthenticationError` from LiteLLM | API key not set for provider | Check `ANTHROPIC_API_KEY` or `LLM_API_KEY` in `.env`; `_is_real_key()` in providers.py will show if it's detected |
| `ValueError: CrewAI QA agent did not return valid JSON` | QA agent wrapped output in markdown | Add stronger instruction in QA task description; check agent temperature (lower = more predictable) |
| Crew hangs indefinitely | `crew.kickoff()` called on event loop thread | Always wrap in `await asyncio.to_thread(crew.kickoff)` |
| All mappings have `status="review"` | `llm_confidence` all ~0.5 | Check that the agents are receiving the target schema correctly; empty target = no name/type signal |
| Duplicate tgt_column assignments after crew | Resolver did not run | Check that `t4_resolve` has `context=[t1_analyse, t3_columns]` — it needs the analyst's semantic groups |

---

## Self-Learning System

The skill improves itself over time. Every user correction feeds back into the
agents. No manual SKILL.md edits needed — the system rewrites itself.

### How the learning loop works

```
User corrects mapping (edit / reject / feedback)
        │
        ▼
record_learning()  →  runtime/crew_learnings.json
        │
        │  (every 5 new events)
        ▼
extract_patterns()  →  LLM distils events into generalised rules
        │               runtime/crew_patterns.json
        │
        │  (injected at every crew run)
        ▼
inject_learnings()  →  prepends rules into agent task descriptions
        │
        │  (every 10 patterns accumulated)
        ▼
refresh_skill_md()  →  LLM rewrites Ambiguity Avoidance Patterns section
                        skills/crewai-stm-mapper/SKILL.md  ← this file
```

### Event sources (automatic — no code changes needed)

| User action | Event type | Hook location |
|---|---|---|
| Manually edits a mapping cell | `manual_edit` | `PATCH /api/sessions/{sid}/mappings/{row_id}` |
| Rejects a row at Gate 2 | `gate2_rejected` | `POST /api/sessions/{sid}/approve-gate2/fields` |
| Submits text feedback on a row | `user_feedback` | `POST /api/sessions/{sid}/crew-feedback` |

### API endpoints

| Endpoint | Purpose |
|---|---|
| `GET /api/crew/learnings` | View raw learning events |
| `GET /api/crew/patterns` | View distilled rules |
| `POST /api/crew/extract-patterns` | Trigger pattern extraction manually |
| `POST /api/crew/refresh-skill` | Rewrite SKILL.md (admin only) |
| `POST /api/sessions/{sid}/crew-feedback` | Submit explicit feedback on a mapping row |

### Key files

| File | Purpose |
|---|---|
| `app/core/crew_learnings.py` | All learning logic |
| `runtime/crew_learnings.json` | Raw event log (auto-created) |
| `runtime/crew_patterns.json` | Distilled rules (auto-created) |

### Tuning thresholds

In `app/core/crew_learnings.py`:
```python
PATTERN_THRESHOLD = 5       # events before pattern extraction triggers
SKILL_UPDATE_THRESHOLD = 10 # patterns before SKILL.md is rewritten
```

Lower these for faster iteration in early deployments.
Set higher for production stability (avoids rewriting the skill on every correction).

### What gets learned

The LLM pattern extractor looks for **repeating corrections** across events:
- The same vendor prefix being stripped consistently → vendor_prefix rule
- The same semantic alias being resolved → naming_alias rule
- The same cast being applied to a type mismatch → type_cast rule
- The same audit column being auto-populated → audit_column rule
- The same M:1 resolution winner being picked → m1_resolution rule

One-off corrections are NOT turned into rules — the extractor requires 2+ events
or very high-confidence single-event patterns.

### Safety guardrails

- SKILL.md is written atomically via a temp file → no partial writes
- The learning loop never blocks the pipeline — all failures are caught and logged
- Pattern injection is confidence-gated (≥ 0.6) — low-confidence guesses are not injected
- Manual `POST /api/crew/refresh-skill` is admin-only (infinite tenant by default)
- The original SKILL.md section is preserved in git — you can always `git checkout` to revert
