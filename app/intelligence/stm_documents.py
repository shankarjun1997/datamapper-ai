"""
app/intelligence/stm_documents.py — Source-to-Target Mapping (STM) document.

L4 of the pipeline doesn't just emit SQL; it produces the human-facing migration
deliverables. This module builds the consolidated **Source-to-Target Mapping
document** (Markdown) from the data the session already holds, and a small
**documents manifest** describing every artifact L4 makes available (STM doc,
column/table mapping CSV, XLSX workbook, SQL) so the UI can list them in one
place. Keeping all of these derived from the same session means they can never
disagree with each other.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
from typing import Dict, List


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def _is_unmapped(m: dict) -> bool:
    return (m.get("status") or "").lower() == "unmapped" or not m.get("tgt_table")


def _pct(n: float) -> str:
    return f"{round((n or 0) * 100)}%"


def _md_escape(v) -> str:
    """Escape pipes/newlines so a value is safe inside a Markdown table cell."""
    return str(v if v is not None else "").replace("|", "\\|").replace("\n", " ").strip()


def build_table_summary(mappings: List[dict]) -> List[dict]:
    """Per source→target table pair: counts, coverage, avg confidence, relations."""
    stats: Dict = defaultdict(lambda: {
        "total": 0, "mapped": 0, "review": 0, "unmapped": 0,
        "conf_sum": 0.0, "conf_count": 0, "relations": set(), "types": set(),
    })
    for m in mappings:
        src_t = m.get("src_table", "") or "(unknown)"
        tgt_t = m.get("tgt_table", "") or "(unmapped)"
        ps = stats[(src_t, tgt_t)]
        ps["total"] += 1
        st = (m.get("status") or "unmapped").lower()
        ps["mapped" if st == "mapped" else "review" if st == "review" else "unmapped"] += 1
        if m.get("confidence"):
            ps["conf_sum"] += float(m["confidence"])
            ps["conf_count"] += 1
        if m.get("mapping_relation"):
            ps["relations"].add(m["mapping_relation"])
        if m.get("mapping_type"):
            ps["types"].add(m["mapping_type"])
    out = []
    for (src_t, tgt_t), ps in sorted(stats.items()):
        total = ps["total"]
        out.append({
            "src_table": src_t, "tgt_table": tgt_t, "total": total,
            "mapped": ps["mapped"], "review": ps["review"], "unmapped": ps["unmapped"],
            "coverage": round(ps["mapped"] / max(total, 1) * 100, 1),
            "avg_confidence": round(ps["conf_sum"] / max(ps["conf_count"], 1) * 100, 1) if ps["conf_count"] else 0.0,
            "relations": ", ".join(sorted(ps["relations"])) or "1:1",
            "types": ", ".join(sorted(ps["types"])) or "Direct",
        })
    return out


def build_stm_markdown(sid: str, session: dict) -> str:
    """Render the full Source-to-Target Mapping document as Markdown."""
    mappings = session.get("mappings", []) or []
    stats = session.get("stats", {}) or {}
    cfg = session.get("bq_config", {}) or {}

    lines: List[str] = []
    lines.append("# Source-to-Target Mapping (STM)")
    lines.append("")
    lines.append(f"- **Session:** `{sid[:8]}`")
    if session.get("filename"):
        lines.append(f"- **Source:** {session['filename']}")
    if cfg.get("project") or cfg.get("dataset"):
        lines.append(f"- **Target:** {cfg.get('project', '')}.{cfg.get('dataset', '')}".rstrip("."))
    lines.append(f"- **Generated:** {_now()}")
    lines.append("")

    # ── Summary ──
    total = stats.get("total", len(mappings))
    lines.append("## Summary")
    lines.append("")
    lines.append(f"- Total source fields: **{total}**")
    lines.append(f"- Auto-mapped: **{stats.get('mapped', 0)}**")
    lines.append(f"- Needs review: **{stats.get('review', 0)}**")
    lines.append(f"- Unmapped: **{stats.get('unmapped', 0)}**")
    lines.append(f"- Average confidence: **{_pct(stats.get('avg_confidence'))}**")
    lines.append("")

    # ── Relation legend ──
    lines.append("> **Relation types** — 1:1 direct · 1:M split/derive (one source → many targets) "
                 "· M:1 combine (many sources → one target) · M:M restructure (bridge).")
    lines.append("")

    # ── Table-level mapping ──
    lines.append("## Table-Level Mapping")
    lines.append("")
    lines.append("| Source Table | Target Table | Relation | Cols | Mapped | Review | Unmapped | Coverage | Avg Conf |")
    lines.append("|---|---|---|---|---|---|---|---|---|")
    for r in build_table_summary(mappings):
        lines.append(
            f"| {_md_escape(r['src_table'])} | {_md_escape(r['tgt_table'])} | {_md_escape(r['relations'])} "
            f"| {r['total']} | {r['mapped']} | {r['review']} | {r['unmapped']} "
            f"| {r['coverage']}% | {r['avg_confidence']}% |"
        )
    lines.append("")

    # ── Column-level mapping, grouped by target table ──
    lines.append("## Column-Level Mapping")
    lines.append("")
    grouped: Dict[str, List[dict]] = defaultdict(list)
    for m in mappings:
        if _is_unmapped(m):
            continue
        grouped[m.get("tgt_table", "") or "(unmapped)"].append(m)

    if not grouped:
        lines.append("_No mapped columns yet._")
        lines.append("")
    for tgt in sorted(grouped):
        rows = grouped[tgt]
        lines.append(f"### → {tgt}")
        lines.append("")
        lines.append("| Source Field | Src Type | Target Column | Tgt Type | Relation | Mapping Type | Transform | Conf | Status |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for m in rows:
            lines.append(
                f"| {_md_escape(m.get('src_field'))} | {_md_escape(m.get('src_type'))} "
                f"| {_md_escape(m.get('tgt_column'))} | {_md_escape(m.get('tgt_type'))} "
                f"| {_md_escape(m.get('mapping_relation') or '1:1')} | {_md_escape(m.get('mapping_type') or 'Direct')} "
                f"| {_md_escape(m.get('business_logic') or '—')} | {_pct(m.get('confidence'))} "
                f"| {_md_escape(m.get('status'))} |"
            )
        lines.append("")

    # ── Gaps ──
    gaps = [m for m in mappings if _is_unmapped(m)]
    if gaps:
        lines.append("## Gaps — Unmapped Source Fields")
        lines.append("")
        lines.append("| Source Table | Source Field | Src Type | Note |")
        lines.append("|---|---|---|---|")
        for m in gaps:
            lines.append(
                f"| {_md_escape(m.get('src_table'))} | {_md_escape(m.get('src_field'))} "
                f"| {_md_escape(m.get('src_type'))} | {_md_escape(m.get('rationale') or 'No target match')} |"
            )
        lines.append("")

    lines.append("---")
    lines.append("_Generated by xREF DataMapper · L4 Document Generation._")
    return "\n".join(lines)


def build_documents_manifest(sid: str, session: dict) -> List[dict]:
    """List every L4 deliverable with a stable download endpoint, for the UI."""
    has_mappings = bool(session.get("mappings"))
    has_mapped = any(
        (m.get("status") or "").lower() != "unmapped" and m.get("tgt_table")
        for m in session.get("mappings", []) or []
    )
    base = f"/api/sessions/{sid}"
    return [
        {"key": "stm_doc", "label": "Source-to-Target Mapping document",
         "format": "md", "endpoint": f"{base}/export/mapping-doc", "ready": has_mappings},
        {"key": "stm_xlsx", "label": "STM workbook (multi-sheet)",
         "format": "xlsx", "endpoint": f"{base}/export/xlsx", "ready": has_mappings},
        {"key": "column_csv", "label": "Column mapping CSV",
         "format": "csv", "endpoint": f"{base}/export/csv", "ready": has_mappings},
        {"key": "table_csv", "label": "Table mapping CSV",
         "format": "csv", "endpoint": f"{base}/export/table-mappings", "ready": has_mappings},
        {"key": "sql", "label": "Materialized SQL",
         "format": "sql", "endpoint": f"{base}/export/sql", "ready": has_mapped},
    ]
