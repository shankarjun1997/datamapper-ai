"""Tests for the L4 Source-to-Target Mapping (STM) document + manifest."""
from app.intelligence.stm_documents import (
    build_documents_manifest,
    build_stm_markdown,
    build_table_summary,
)

SID = "abcdef12-0000-4000-8000-000000000000"
SESSION = {
    "id": SID,
    "filename": "subscriber_master.csv",
    "bq_config": {"project": "proj", "dataset": "cdm"},
    "stats": {"total": 4, "mapped": 3, "review": 1, "unmapped": 1, "avg_confidence": 0.92},
    "mappings": [
        {"src_table": "SUBSCR_MASTER", "src_field": "full_name", "src_type": "VARCHAR2",
         "tgt_table": "subscriber", "tgt_column": "first_name", "tgt_type": "STRING",
         "mapping_type": "Derived", "mapping_relation": "1:M",
         "business_logic": "SPLIT(full_name,' ')[OFFSET(0)]", "confidence": 1.0, "status": "mapped"},
        {"src_table": "SUBSCR_MASTER", "src_field": "full_name", "src_type": "VARCHAR2",
         "tgt_table": "subscriber", "tgt_column": "last_name", "tgt_type": "STRING",
         "mapping_type": "Derived", "mapping_relation": "1:M",
         "business_logic": "SPLIT(full_name,' ')[SAFE_OFFSET(1)]", "confidence": 1.0, "status": "mapped"},
        {"src_table": "SUBSCR_MASTER", "src_field": "msisdn", "src_type": "VARCHAR2",
         "tgt_table": "subscriber", "tgt_column": "phone_number", "tgt_type": "STRING",
         "mapping_type": "Direct", "mapping_relation": "1:1",
         "business_logic": "", "confidence": 0.95, "status": "review"},
        {"src_table": "SUBSCR_MASTER", "src_field": "legacy_flag", "src_type": "CHAR",
         "tgt_table": "", "tgt_column": "", "status": "unmapped", "rationale": "Deprecated"},
    ],
}


def test_table_summary_counts_and_relations():
    summary = build_table_summary(SESSION["mappings"])
    pair = next(r for r in summary if r["tgt_table"] == "subscriber")
    assert pair["total"] == 3
    assert pair["mapped"] == 2
    assert pair["review"] == 1
    assert "1:M" in pair["relations"] and "1:1" in pair["relations"]


def test_markdown_has_all_sections_and_relations():
    md = build_stm_markdown(SID, SESSION)
    assert "# Source-to-Target Mapping (STM)" in md
    assert "## Summary" in md
    assert "## Table-Level Mapping" in md
    assert "## Column-Level Mapping" in md
    assert "### → subscriber" in md
    # relation types and a transform appear in the column section
    assert "1:M" in md and "SPLIT(full_name" in md
    # unmapped field shows up in the gaps section
    assert "## Gaps" in md and "legacy_flag" in md


def test_markdown_escapes_pipes():
    s = {"id": SID, "stats": {}, "mappings": [
        {"src_table": "t", "src_field": "a|b", "src_type": "X",
         "tgt_table": "tg", "tgt_column": "c", "tgt_type": "Y",
         "mapping_type": "Direct", "mapping_relation": "1:1",
         "business_logic": "CASE WHEN x|y", "confidence": 0.5, "status": "mapped"}]}
    md = build_stm_markdown(SID, s)
    assert "a\\|b" in md  # pipe escaped so the table stays well-formed


def test_documents_manifest_endpoints_and_readiness():
    man = build_documents_manifest(SID, SESSION)
    keys = {d["key"] for d in man}
    assert {"stm_doc", "stm_xlsx", "column_csv", "table_csv", "sql"} <= keys
    stm = next(d for d in man if d["key"] == "stm_doc")
    assert stm["endpoint"] == f"/api/sessions/{SID}/export/mapping-doc"
    assert stm["ready"] is True
    # SQL ready because there is at least one mapped row with a target
    sql = next(d for d in man if d["key"] == "sql")
    assert sql["ready"] is True


def test_manifest_sql_not_ready_without_mapped_rows():
    s = {"id": SID, "mappings": [
        {"src_table": "t", "src_field": "x", "tgt_table": "", "status": "unmapped"}]}
    man = build_documents_manifest(SID, s)
    assert next(d for d in man if d["key"] == "sql")["ready"] is False
    assert next(d for d in man if d["key"] == "stm_doc")["ready"] is True
