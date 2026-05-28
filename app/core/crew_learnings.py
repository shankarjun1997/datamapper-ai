"""
app/core/crew_learnings.py

Self-learning store for the CrewAI STM mapping pipeline.

How it works:
  1. Every user correction (manual edit, Gate 2 rejection, field-level approval)
     is recorded as a LearningEvent via record_learning().
  2. When enough events accumulate (PATTERN_THRESHOLD), extract_patterns() runs
     an LLM call to distill them into generalised mapping rules.
  3. At crew startup, inject_learnings() prepends the current rule set into each
     agent's task description so the crew benefits immediately.
  4. When SKILL_UPDATE_THRESHOLD is reached, refresh_skill_md() rewrites the
     "Ambiguity Avoidance Patterns" section of SKILL.md with the latest learnings.

Storage: runtime/crew_learnings.json  (list of LearningEvent dicts)
         runtime/crew_patterns.json   (distilled rule list, auto-refreshed)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("xref_agent.crew_learnings")

_PROJECT_ROOT     = Path(__file__).parent.parent.parent
_RUNTIME_DIR      = _PROJECT_ROOT / "runtime"
_RUNTIME_DIR.mkdir(exist_ok=True)

_LEARNINGS_PATH   = _RUNTIME_DIR / "crew_learnings.json"
_PATTERNS_PATH    = _RUNTIME_DIR / "crew_patterns.json"
_SKILL_MD_PATH    = _PROJECT_ROOT / "skills" / "crewai-stm-mapper" / "SKILL.md"

# How many raw events before we distill into patterns
PATTERN_THRESHOLD = 5
# How many patterns before we rewrite SKILL.md
SKILL_UPDATE_THRESHOLD = 10

_learnings: List[Dict] = []
_patterns:  List[Dict] = []


# ── Persistence ───────────────────────────────────────────────────────────────

def load_learnings() -> None:
    global _learnings, _patterns
    if _LEARNINGS_PATH.exists():
        try:
            _learnings = json.loads(_LEARNINGS_PATH.read_text())
            logger.info("Loaded %d crew learning events", len(_learnings))
        except Exception:
            _learnings = []
    if _PATTERNS_PATH.exists():
        try:
            _patterns = json.loads(_PATTERNS_PATH.read_text())
            logger.info("Loaded %d crew patterns", len(_patterns))
        except Exception:
            _patterns = []


def _flush_learnings() -> None:
    try:
        _LEARNINGS_PATH.write_text(json.dumps(_learnings, indent=2))
    except Exception as e:
        logger.warning("Failed to flush crew learnings: %s", e)


def _flush_patterns() -> None:
    try:
        _PATTERNS_PATH.write_text(json.dumps(_patterns, indent=2))
    except Exception as e:
        logger.warning("Failed to flush crew patterns: %s", e)


# ── Event recording ───────────────────────────────────────────────────────────

def record_learning(
    event_type: str,           # "manual_edit" | "gate2_rejected" | "gate2_approved" | "remapped"
    src_field: str,
    original: Dict,            # mapping row before the change
    corrected: Dict,           # mapping row after the change
    tenant: str = "unknown",
    session_id: str = "",
    feedback_text: str = "",   # optional free-text from user
) -> Dict:
    """Record a single learning event.

    Events are the raw material — patterns are extracted from them in batch.
    Returns the event dict so callers can log it.
    """
    evt = {
        "id":           f"le_{int(time.time()*1000)}",
        "ts":           datetime.now(timezone.utc).isoformat(),
        "event_type":   event_type,
        "tenant":       tenant,
        "session_id":   session_id,
        "src_field":    src_field,
        "src_type":     original.get("src_type", ""),
        "src_table":    original.get("src_table", ""),
        "original": {
            "tgt_table":        original.get("tgt_table", ""),
            "tgt_column":       original.get("tgt_column", ""),
            "mapping_type":     original.get("mapping_type", ""),
            "business_logic":   original.get("business_logic", ""),
            "confidence":       original.get("confidence", 0),
            "status":           original.get("status", ""),
        },
        "corrected": {
            "tgt_table":        corrected.get("tgt_table", ""),
            "tgt_column":       corrected.get("tgt_column", ""),
            "mapping_type":     corrected.get("mapping_type", ""),
            "business_logic":   corrected.get("business_logic", ""),
            "confidence":       corrected.get("confidence", 0),
            "status":           corrected.get("status", ""),
        },
        "feedback_text": feedback_text,
        "absorbed_into_pattern": False,
    }
    _learnings.append(evt)
    _flush_learnings()

    # Auto-trigger pattern extraction when threshold is hit
    unabsorbed = [e for e in _learnings if not e.get("absorbed_into_pattern")]
    if len(unabsorbed) >= PATTERN_THRESHOLD:
        logger.info("Learning threshold reached (%d events) — scheduling pattern extraction",
                    len(unabsorbed))
        # Extraction is async; caller should await this separately
        # We mark a flag so the next crew run triggers it
        pass

    return evt


def pending_extraction_count() -> int:
    """Return number of events not yet absorbed into patterns."""
    return sum(1 for e in _learnings if not e.get("absorbed_into_pattern"))


# ── Pattern extraction ────────────────────────────────────────────────────────

def extract_patterns(llm_client) -> List[Dict]:
    """Run an LLM call to distil raw learning events into generalisable rules.

    llm_client: a MultiLLMClient instance (from app.core.llm_client)
    Returns the updated _patterns list.
    """
    global _patterns

    unabsorbed = [e for e in _learnings if not e.get("absorbed_into_pattern")]
    if not unabsorbed:
        return _patterns

    events_json = json.dumps(unabsorbed, indent=2)

    system = (
        "You are a data mapping pattern analyst. "
        "Given a list of user corrections to an automated mapping system, "
        "extract generalised, reusable mapping rules that will prevent the same "
        "mistakes in future sessions. Focus on patterns, not one-off fixes."
    )

    prompt = f"""These are corrections a user made to automated Source-to-Target mappings.
Each event shows the original (wrong) mapping and the corrected (right) mapping.

LEARNING EVENTS:
{events_json}

Analyse these corrections and extract generalised rules. Return a JSON array:
[
  {{
    "rule_id": "unique-slug",
    "category": "vendor_prefix" | "naming_alias" | "type_cast" | "table_routing" | "business_logic" | "audit_column" | "m1_resolution" | "other",
    "title": "Short human-readable title",
    "description": "What the rule says in plain English",
    "agent_instruction": "Exact instruction to inject into agent task prompts (imperative, ≤2 sentences)",
    "examples": [
      {{"wrong": "...", "right": "...", "reason": "..."}}
    ],
    "confidence": 0.0-1.0,
    "applicable_to": ["schema_analyst" | "table_mapper" | "column_mapper" | "ambiguity_resolver" | "qa_validator"],
    "derived_from_events": ["event_id_1", "event_id_2"]
  }}
]

Rules:
- Only extract rules that appear in 2+ events OR are very high-confidence single-event rules
- agent_instruction must be concise and actionable — this gets injected directly into prompts
- Do not extract rules for one-off typos or highly schema-specific corrections
- Return valid JSON only — no markdown, no prose"""

    try:
        new_rules = llm_client.complete_json(system, prompt)
        if not isinstance(new_rules, list):
            logger.warning("Pattern extraction returned non-list: %r", type(new_rules))
            return _patterns

        # Merge with existing patterns (avoid duplicates by rule_id)
        existing_ids = {p["rule_id"] for p in _patterns}
        added = 0
        for rule in new_rules:
            rid = rule.get("rule_id", "")
            if rid and rid not in existing_ids:
                _patterns.append(rule)
                existing_ids.add(rid)
                added += 1
            elif rid in existing_ids:
                # Update existing rule with new examples
                for p in _patterns:
                    if p["rule_id"] == rid:
                        p["examples"].extend(rule.get("examples", []))
                        p["confidence"] = max(p["confidence"], rule.get("confidence", 0))
                        break

        # Mark absorbed events
        absorbed_ids = set()
        for rule in new_rules:
            absorbed_ids.update(rule.get("derived_from_events", []))
        for evt in _learnings:
            if evt["id"] in absorbed_ids:
                evt["absorbed_into_pattern"] = True

        _flush_learnings()
        _flush_patterns()

        logger.info("Pattern extraction: +%d new rules, total=%d", added, len(_patterns))

        # Auto-trigger SKILL.md update if we have enough patterns
        if len(_patterns) >= SKILL_UPDATE_THRESHOLD:
            try:
                refresh_skill_md(llm_client)
            except Exception as e:
                logger.warning("SKILL.md auto-refresh failed: %s", e)

        return _patterns

    except Exception as e:
        logger.error("Pattern extraction failed: %s", e)
        return _patterns


# ── Runtime injection ─────────────────────────────────────────────────────────

def inject_learnings(task_descriptions: Dict[str, str]) -> Dict[str, str]:
    """Prepend relevant learned rules into agent task descriptions.

    task_descriptions: {"schema_analyst": "...", "table_mapper": "...", ...}
    Returns updated dict with learned rules prepended to each relevant task.
    """
    if not _patterns:
        return task_descriptions

    updated = dict(task_descriptions)

    for agent_name, task_text in task_descriptions.items():
        relevant = [
            p for p in _patterns
            if agent_name in p.get("applicable_to", [])
            and p.get("confidence", 0) >= 0.6
        ]
        if not relevant:
            continue

        rules_block = "\n".join(
            f"- [{p['category'].upper()}] {p['agent_instruction']}"
            for p in relevant
        )
        prefix = (
            f"LEARNED RULES FROM PRIOR USER CORRECTIONS (apply these first):\n"
            f"{rules_block}\n\n"
        )
        updated[agent_name] = prefix + task_text

    return updated


def get_patterns() -> List[Dict]:
    """Return current pattern list — used by the API endpoint."""
    return _patterns


def get_learnings(limit: int = 100) -> List[Dict]:
    """Return recent learning events — used by the API endpoint."""
    return _learnings[-limit:]


# ── SKILL.md self-update ──────────────────────────────────────────────────────

_SKILL_SECTION_MARKER_START = "## Ambiguity Avoidance Patterns"
_SKILL_SECTION_MARKER_END   = "## Confidence Score in Crew Context"


def refresh_skill_md(llm_client) -> bool:
    """Rewrite the 'Ambiguity Avoidance Patterns' section of SKILL.md
    using the current accumulated patterns.

    Returns True if the file was updated, False if skipped or failed.
    """
    if not _SKILL_MD_PATH.exists():
        logger.warning("SKILL.md not found at %s — skipping refresh", _SKILL_MD_PATH)
        return False

    if not _patterns:
        return False

    current_md = _SKILL_MD_PATH.read_text()

    # Find the section to replace
    start_idx = current_md.find(_SKILL_SECTION_MARKER_START)
    end_idx   = current_md.find(_SKILL_SECTION_MARKER_END)
    if start_idx == -1 or end_idx == -1:
        logger.warning("Could not find section markers in SKILL.md")
        return False

    patterns_json = json.dumps(_patterns, indent=2)

    system = (
        "You are a technical documentation writer specialising in AI agent systems. "
        "You update skill documentation to reflect newly learned patterns. "
        "Write in the same style as the existing documentation — concrete, prescriptive, "
        "with Pattern name, Symptom, and Prevention subsections. Use markdown."
    )

    prompt = f"""Rewrite the '## Ambiguity Avoidance Patterns' section of this CrewAI mapping skill.

CURRENT SECTION (to replace):
{current_md[start_idx:end_idx]}

LEARNED PATTERNS TO INCORPORATE:
{patterns_json}

Instructions:
1. Keep all existing patterns that are still valid
2. Add new patterns derived from the learned rules (each gets a Pattern N: title, Symptom, Prevention format)
3. Update existing patterns if learned rules refine them
4. Keep the section header exactly: ## Ambiguity Avoidance Patterns
5. Do NOT include the next section header ({_SKILL_SECTION_MARKER_END})
6. Return only the replacement markdown text for this section — nothing else"""

    try:
        new_section = llm_client.complete(system, prompt)

        # Validate it starts with the right header
        if not new_section.strip().startswith("## Ambiguity Avoidance Patterns"):
            new_section = f"{_SKILL_SECTION_MARKER_START}\n\n" + new_section

        # Stitch back together
        updated_md = (
            current_md[:start_idx]
            + new_section.rstrip()
            + "\n\n"
            + current_md[end_idx:]
        )

        # Write atomically via temp file
        tmp = _SKILL_MD_PATH.with_suffix(".md.tmp")
        tmp.write_text(updated_md)
        tmp.replace(_SKILL_MD_PATH)

        logger.info("SKILL.md refreshed with %d patterns (%d chars → %d chars)",
                    len(_patterns), len(current_md), len(updated_md))
        return True

    except Exception as e:
        logger.error("SKILL.md refresh failed: %s", e)
        return False
