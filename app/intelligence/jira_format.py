"""
app/intelligence/jira_format.py — Jira ADF helpers + auto mapping comment.

Jira Cloud descriptions/comments use Atlassian Document Format (ADF), a nested
JSON tree. These pure helpers convert ADF -> plain text (to read a ticket) and
plain text -> ADF (to post a comment), plus build a concise mapping-complete
summary from a session. Kept dependency-free and unit-tested.
"""
from __future__ import annotations

from typing import Any, Dict, List


def adf_to_text(node: Any) -> str:
    """Flatten an ADF document (or any node) to readable plain text."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    if isinstance(node, list):
        return "".join(adf_to_text(n) for n in node)
    if not isinstance(node, dict):
        return ""
    ntype = node.get("type")
    if ntype == "text":
        return node.get("text", "")
    if ntype == "hardBreak":
        return "\n"
    inner = adf_to_text(node.get("content", []))
    # Block-level nodes get trailing newlines so paragraphs/lists stay separated.
    if ntype in ("paragraph", "heading"):
        return inner + "\n"
    if ntype == "listItem":
        return "- " + inner.strip() + "\n"
    if ntype in ("bulletList", "orderedList", "blockquote", "codeBlock"):
        return inner + "\n"
    return inner


def text_to_adf(text: str) -> Dict:
    """Wrap plain text into a minimal ADF doc; '- ' lines become a bullet list."""
    content: List[Dict] = []
    bullets: List[Dict] = []

    def flush_bullets():
        if bullets:
            content.append({"type": "bulletList", "content": list(bullets)})
            bullets.clear()

    for raw in (text or "").split("\n"):
        line = raw.rstrip()
        if not line.strip():
            flush_bullets()
            continue
        if line.lstrip().startswith(("- ", "* ")):
            item = line.lstrip()[2:]
            bullets.append({"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": item}]}]})
        else:
            flush_bullets()
            content.append({"type": "paragraph", "content": [{"type": "text", "text": line}]})
    flush_bullets()
    if not content:
        content = [{"type": "paragraph", "content": [{"type": "text", "text": text or ""}]}]
    return {"type": "doc", "version": 1, "content": content}


def extract_subtasks(fields: Dict) -> List[Dict]:
    """Return [{key, summary, status}] for an issue's subtasks."""
    out = []
    for st in (fields or {}).get("subtasks", []) or []:
        f = st.get("fields", {}) or {}
        out.append({
            "key": st.get("key", ""),
            "summary": f.get("summary", ""),
            "status": (f.get("status", {}) or {}).get("name", ""),
        })
    return out


def build_mapping_comment(session: Dict, app_name: str = "xREF DataMapper") -> str:
    """Auto-compose a 'mapping complete' comment from session stats."""
    stats = session.get("stats", {}) or {}
    total = stats.get("total", 0)
    mapped = stats.get("mapped", 0)
    review = stats.get("review", 0)
    unmapped = stats.get("unmapped", 0)
    conf = round((stats.get("avg_confidence") or 0) * 100)
    src = session.get("filename", "source")
    tgt_tables = sorted({m.get("tgt_table") for m in session.get("mappings", []) if m.get("tgt_table")})

    lines = [
        f"{app_name} — source-to-target mapping complete ✅",
        "",
        f"Source: {src}",
        f"Target tables: {', '.join(tgt_tables) or '—'}",
        f"Columns: {total} total ({mapped} mapped, {review} in review, {unmapped} unmapped)",
        f"Average confidence: {conf}%",
        "",
        "Deliverables generated: Source-to-Target Mapping (STM) document, column & table mapping spec.",
    ]
    return "\n".join(lines)
