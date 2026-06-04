"""Tests for Jira ADF helpers + auto mapping comment."""
from app.intelligence.jira_format import (
    adf_to_text,
    build_mapping_comment,
    extract_subtasks,
    text_to_adf,
)

ADF = {
    "type": "doc", "version": 1, "content": [
        {"type": "paragraph", "content": [{"type": "text", "text": "Migrate the customer table."}]},
        {"type": "bulletList", "content": [
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Split full name"}]}]},
            {"type": "listItem", "content": [
                {"type": "paragraph", "content": [{"type": "text", "text": "Normalize address"}]}]},
        ]},
    ],
}


def test_adf_to_text_flattens_paragraphs_and_lists():
    txt = adf_to_text(ADF)
    assert "Migrate the customer table." in txt
    assert "- Split full name" in txt
    assert "- Normalize address" in txt


def test_adf_to_text_handles_strings_and_none():
    assert adf_to_text("plain") == "plain"
    assert adf_to_text(None) == ""


def test_text_to_adf_roundtrips_paragraphs_and_bullets():
    doc = text_to_adf("Intro line\n\n- one\n- two\nOutro")
    assert doc["type"] == "doc" and doc["version"] == 1
    types = [c["type"] for c in doc["content"]]
    assert "paragraph" in types and "bulletList" in types
    bl = next(c for c in doc["content"] if c["type"] == "bulletList")
    assert len(bl["content"]) == 2  # two bullet items


def test_text_to_adf_empty():
    doc = text_to_adf("")
    assert doc["content"][0]["type"] == "paragraph"


def test_extract_subtasks():
    fields = {"subtasks": [
        {"key": "SCRUM-33", "fields": {"summary": "Build DDL", "status": {"name": "To Do"}}},
        {"key": "SCRUM-34", "fields": {"summary": "Validate", "status": {"name": "Done"}}},
    ]}
    subs = extract_subtasks(fields)
    assert subs == [
        {"key": "SCRUM-33", "summary": "Build DDL", "status": "To Do"},
        {"key": "SCRUM-34", "summary": "Validate", "status": "Done"},
    ]
    assert extract_subtasks({}) == []


def test_build_mapping_comment_has_counts():
    session = {
        "filename": "frontier_customers.csv",
        "stats": {"total": 20, "mapped": 16, "review": 3, "unmapped": 1, "avg_confidence": 0.91},
        "mappings": [{"tgt_table": "cust_master"}, {"tgt_table": "fact_support"}],
    }
    c = build_mapping_comment(session)
    assert "mapping complete" in c.lower()
    assert "20 total" in c and "16 mapped" in c and "91%" in c
    assert "cust_master" in c and "fact_support" in c
