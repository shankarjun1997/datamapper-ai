"""
app/core/mapping_memory.py — mapping memory operations
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.state import _mapping_memory, _save_mapping_memory


def _absorb_approved_mappings(mappings: List[Dict]) -> int:
    """Learn from Gate-2-approved mappings. Returns count of new/updated entries."""
    absorbed = 0
    for m in mappings:
        if m.get("status") not in ("mapped", "review"):
            continue
        if not m.get("tgt_column"):
            continue
        src = m["src_field"]
        existing = _mapping_memory.get(src)
        new_conf  = float(m.get("confidence", 0.5))

        if existing and existing.get("user_override") and new_conf < float(existing.get("confidence", 0)):
            continue

        if existing is None or new_conf >= float(existing.get("confidence", 0)):
            _mapping_memory[src] = {
                "tgt_table":        m.get("tgt_table", ""),
                "tgt_column":       m.get("tgt_column", ""),
                "mapping_type":     m.get("mapping_type", "Direct"),
                "mapping_relation": m.get("mapping_relation", "1:1"),
                "business_logic":   m.get("business_logic", ""),
                "confidence":       round(new_conf, 3),
                "uses":             existing["uses"] + 1 if existing else 1,
                "last_updated":     datetime.now(timezone.utc).isoformat(),
                "user_override":    existing.get("user_override", False) if existing else False,
            }
            absorbed += 1
    _save_mapping_memory()
    return absorbed


def _absorb_single_correction(row: Dict) -> None:
    """Immediately absorb a user-edited mapping row into memory as a ground-truth override."""
    src = row.get("src_field", "")
    tgt_col = row.get("tgt_column", "")
    if not src or not tgt_col:
        return
    existing = _mapping_memory.get(src)
    _mapping_memory[src] = {
        "tgt_table":        row.get("tgt_table", ""),
        "tgt_column":       tgt_col,
        "mapping_type":     row.get("mapping_type", "Direct"),
        "mapping_relation": row.get("mapping_relation", "1:1"),
        "business_logic":   row.get("business_logic", ""),
        "confidence":       1.0,
        "uses":             existing["uses"] + 1 if existing else 1,
        "last_updated":     datetime.now(timezone.utc).isoformat(),
        "user_override":    True,
    }
    _save_mapping_memory()


def _recall_mapping_hints(src_fields: List[str]) -> Dict[str, Dict]:
    """Return memory entries for the given source field names.

    Three-pass lookup (first match wins per field):
      1. Exact key match.
      2. Vendor-stripped + fuzzy ratio >= 85.
      3. Canonical concept match.
    """
    hints: Dict[str, Dict] = {}
    if not _mapping_memory:
        return hints

    # Import here to avoid circular — these are pure functions defined in intelligence
    from app.intelligence.confidence import _strip_vendor, _canonical_concept

    try:
        from rapidfuzz import fuzz as _fuzz
        _has_fuzz = True
    except ImportError:
        _has_fuzz = False

    for sf in src_fields:
        if sf in _mapping_memory:
            hints[sf] = _mapping_memory[sf]
            continue

        bare_sf = _strip_vendor(sf).lower()
        concept_sf = _canonical_concept(sf)

        best: Optional[Dict] = None
        best_score = 0.0

        for mem_key, mem_val in _mapping_memory.items():
            mem_bare = _strip_vendor(mem_key).lower()
            score = 0.0
            if _has_fuzz:
                r = _fuzz.ratio(bare_sf, mem_bare) / 100.0
                ts = _fuzz.token_sort_ratio(bare_sf, mem_bare) / 100.0
                score = max(r, ts)
            else:
                common = sum(c in mem_bare for c in bare_sf)
                score = common / max(len(bare_sf), len(mem_bare), 1)

            if score >= 0.85 and score > best_score:
                best = mem_val
                best_score = score

            concept_mem = _canonical_concept(mem_key)
            if (concept_sf and concept_mem
                    and concept_sf == concept_mem
                    and concept_sf != sf.lower()):
                concept_score = 0.90 if mem_val.get("user_override") else 0.88
                if concept_score > best_score:
                    best = mem_val
                    best_score = concept_score

        if best is not None:
            hints[sf] = best

    return hints
