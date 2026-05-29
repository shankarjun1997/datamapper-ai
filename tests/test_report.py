"""Tests for the Mapping Report (ReportSpec + renderers)."""
import pytest

from app.intelligence import report as rpt

SESSION = {
    "id": "11111111-1111-4111-8111-111111111111",
    "filename": "Billing → Warehouse",
    "tenant": "acme",
    "created_at": "2026-05-01T00:00:00Z",
    "bq_config": {"project": "p"},  # → target platform bigquery inferred elsewhere
    "mapping_versions": [{"v": 1}, {"v": 2}],
    "mappings": [
        {"src_table": "cust", "src_field": "id", "src_type": "NUMBER(10,0)",
         "tgt_table": "party", "tgt_column": "id", "tgt_type": "INT64",
         "mapping_type": "exact", "confidence": 0.98, "status": "approved"},
        {"src_table": "cust", "src_field": "meta", "src_type": "JSONB",
         "tgt_table": "party", "tgt_column": "meta", "status": "approved"},
        {"src_table": "cust", "src_field": "tmp", "src_type": "VARCHAR(5)",
         "tgt_table": "", "tgt_column": "", "status": "no_mapping"},
    ],
}
AUDIT = [
    {"ts": "t1", "event": "gate2.approved", "email": "a@acme", "session_id": SESSION["id"], "meta": {}},
    {"ts": "t2", "event": "export.csv", "email": "a@acme", "session_id": "other", "meta": {}},
]


def test_report_spec_structure_and_summary():
    spec = rpt.build_report_spec(SESSION, "postgres", "redshift", audit_events=AUDIT)
    assert spec["meta"]["session_id"] == SESSION["id"]
    assert spec["summary"]["active_mappings"] == 2          # no_mapping excluded
    assert spec["summary"]["approved_mappings"] == 2
    assert 0 <= spec["summary"]["overall_readiness"] <= 100
    assert spec["summary"]["blockers"] >= 1                 # JSONB → redshift
    # governance: only this session's audit + approval events
    assert spec["governance"]["versions"] == 2
    assert spec["governance"]["audit_count"] == 1
    assert len(spec["governance"]["approval_events"]) == 1


def test_render_html_contains_key_sections():
    spec = rpt.build_report_spec(SESSION, "postgres", "redshift", audit_events=AUDIT)
    html = rpt.render_html(spec)
    assert "Mapping Report" in html
    assert "Mapping specification" in html
    assert "Governance" in html
    assert "party.id" in html  # a mapped target column rendered
    # HTML is escaped (no raw script injection surface from data)
    assert "<script>" not in html


def test_render_xlsx_produces_workbook():
    pytest.importorskip("openpyxl")
    spec = rpt.build_report_spec(SESSION, "postgres", "redshift", audit_events=AUDIT)
    data = rpt.render_xlsx(spec)
    assert isinstance(data, (bytes, bytearray)) and len(data) > 0
    assert data[:2] == b"PK"  # xlsx is a zip
