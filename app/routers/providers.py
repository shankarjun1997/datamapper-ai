"""
app/routers/providers.py — /api/providers + /api/global-config + bq-* + source-* + usage + version + health
"""
from __future__ import annotations

import asyncio
import json
import os
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Body, Depends, File, HTTPException, UploadFile
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel

from app.core.rbac import require_mapper

from app.config import (
    _ANTHROPIC_API_KEY,
    _BQ_DATASET,
    _BQ_PROJECT,
    _CLAUDE_MODEL,
    _DEFAULT_PROVIDER,
    _DEEPSEEK_API_KEY,
    _DEEPSEEK_MODEL,
    _GCP_CREDS,
    _PROVIDER_CATALOG,
)
from app.connectors.bigquery import crawl_bq, crawl_bq_project, list_bq_datasets
from app.connectors.databricks import DatabricksUnityRequest, _crawl_databricks_unity
from app.connectors.source_db import crawl_source_db
from app.core.llm_client import _pricing_for
from app.core.session_store import _session_or_404
from app.parsers.schema import parse_schema_file
from app.routers._helpers import _validate_upload
from app.state import _sessions, _save_sessions

router = APIRouter()


def _is_real_key(key: str) -> bool:
    """Return True only if a key looks genuinely configured.
    Rejects empty strings, template placeholders (contain '...' or 'your_'),
    and strings too short to be real API keys (< 20 chars)."""
    if not key:
        return False
    if "..." in key or "your_" in key.lower() or key.startswith("sk-ant-api03-..."):
        return False
    if len(key) < 20:
        return False
    return True


@router.get("/api/providers")
async def get_providers():
    """Return the full provider + model catalog."""
    return {
        "providers": _PROVIDER_CATALOG,
        "configured": {
            "claude":   _is_real_key(_ANTHROPIC_API_KEY),
            "deepseek": _is_real_key(_DEEPSEEK_API_KEY),
        },
        "defaults": {
            "provider": _DEFAULT_PROVIDER,
            "model":    _CLAUDE_MODEL if _DEFAULT_PROVIDER == "claude" else _DEEPSEEK_MODEL,
        },
    }


@router.get("/api/global-config")
async def global_config():
    return {
        "provider":          _DEFAULT_PROVIDER,
        "default_provider":  _DEFAULT_PROVIDER,
        "has_anthropic_key": _is_real_key(_ANTHROPIC_API_KEY),
        "has_deepseek_key":  _is_real_key(_DEEPSEEK_API_KEY),
        "has_bq_project":    bool(_BQ_PROJECT),
        "bq_project":        _BQ_PROJECT,
        "bq_dataset":        _BQ_DATASET,
        "claude_model":      _CLAUDE_MODEL,
        "deepseek_model":    _DEEPSEEK_MODEL,
        "gcp_creds":         bool(_GCP_CREDS),
    }


class APIConfig(BaseModel):
    provider:     str = "claude"
    api_key:      Optional[str] = ""
    base_url:     Optional[str] = ""
    model:        Optional[str] = ""
    llm_mode:     Optional[str] = "backend"
    webhook_url:  Optional[str] = ""
    webhook_urls: Optional[List[str]] = None


@router.post("/api/sessions/{sid}/api-config")
async def set_api_config(sid: str, cfg: APIConfig, _rbac=Depends(require_mapper)):
    s = _session_or_404(sid)
    s["api_config"] = cfg.model_dump()
    return {"ok": True, "llm_mode": cfg.llm_mode or "backend"}


@router.post("/api/sessions/{sid}/webhook-test")
async def test_webhook(sid: str, body: dict = Body(...), _rbac=Depends(require_mapper)):
    """Fire a one-off webhook test POST to the supplied URL."""
    url = (body or {}).get("url", "")
    if not url:
        raise HTTPException(400, "url required")
    from app.core.webhooks import fire_webhook
    session = _session_or_404(sid)
    # Build a transient session view that uses the supplied URL only
    # (don't mutate persisted api_config from a test call).
    transient = {
        "id":         sid,
        "tenant":     session.get("tenant"),
        "api_config": {"webhook_url": url},
    }
    await fire_webhook("webhook.test", transient, data={"message": "xREF webhook test"})
    return {"ok": True}


class BQConfig(BaseModel):
    project:       str
    dataset:       str
    region:        Optional[str] = "us-central1"
    gcp_creds:     Optional[str] = ""
    target_tables: Optional[str] = ""


@router.post("/api/sessions/{sid}/bq-config")
async def set_bq_config(sid: str, cfg: BQConfig, _rbac=Depends(require_mapper)):
    s = _session_or_404(sid)
    existing_json = s.get("bq_config", {}).get("gcp_creds_json")
    s["bq_config"] = cfg.model_dump()
    if existing_json:
        s["bq_config"]["gcp_creds_json"] = existing_json
    return {"ok": True}


@router.get("/api/sessions/{sid}/bq-datasets")
async def list_datasets_for_session(sid: str):
    s = _session_or_404(sid)
    cfg     = s.get("bq_config", {})
    project = cfg.get("project") or _BQ_PROJECT
    if not project:
        raise HTTPException(422, "Set GCP Project ID first (via bq-config)")
    try:
        datasets = await asyncio.to_thread(
            list_bq_datasets,
            project,
            cfg.get("gcp_creds") or _GCP_CREDS,
            cfg.get("gcp_creds_json"),
        )
        return {"ok": True, "project": project, "datasets": datasets}
    except Exception as e:
        raise HTTPException(422, str(e))


@router.post("/api/sessions/{sid}/bq-test")
async def test_bq(sid: str, _rbac=Depends(require_mapper)):
    s = _session_or_404(sid)
    cfg     = s.get("bq_config", {})
    project = cfg.get("project") or _BQ_PROJECT
    dataset = cfg.get("dataset") or _BQ_DATASET

    if not project:
        raise HTTPException(422, "GCP Project ID is required")

    try:
        if dataset:
            tables = await asyncio.to_thread(
                crawl_bq, project, dataset,
                cfg.get("gcp_creds") or _GCP_CREDS, None,
                cfg.get("gcp_creds_json"),
            )
        else:
            tables = await asyncio.to_thread(
                crawl_bq_project,
                project,
                None,
                cfg.get("gcp_creds") or _GCP_CREDS,
                cfg.get("gcp_creds_json"),
            )

        s["bq_tables"]         = tables
        s["target_files_data"] = tables

        scanned_datasets = sorted({t.get("dataset", dataset or "unknown") for t in tables})
        return {
            "ok":        True,
            "tables":    len(tables),
            "table_names": [t["table"] for t in tables],
            "datasets":  scanned_datasets,
            "schema":    tables,
            "auto_discovered": not bool(dataset),
        }
    except Exception as e:
        raise HTTPException(422, str(e))


@router.get("/api/sessions/{sid}/target-schema")
async def get_target_schema(sid: str):
    s = _session_or_404(sid)
    tables = s.get("bq_tables") or s.get("target_files_data") or []
    return {"tables": tables, "count": len(tables),
            "source": s.get("target_db_type", "bq" if s.get("bq_tables") else "files")}


@router.post("/api/sessions/{sid}/gcp-creds")
async def upload_gcp_creds(sid: str, file: UploadFile = File(...), _rbac=Depends(require_mapper)):
    s = _session_or_404(sid)
    content = await file.read()
    if len(content) > 64 * 1024:
        raise HTTPException(400, "Credentials file too large (max 64KB)")
    try:
        creds_dict = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(422, "File is not valid JSON")
    required = {"type", "project_id", "private_key", "client_email"}
    if not required.issubset(creds_dict.keys()):
        raise HTTPException(422, f"Missing required service account fields: {required - set(creds_dict.keys())}")
    if "bq_config" not in s:
        s["bq_config"] = {}
    s["bq_config"]["gcp_creds_json"] = creds_dict
    if not s["bq_config"].get("project"):
        s["bq_config"]["project"] = creds_dict.get("project_id", "")
    return {
        "ok":         True,
        "project_id": creds_dict.get("project_id", ""),
        "client_email": creds_dict.get("client_email", ""),
    }


@router.post("/api/sessions/{sid}/source-gcp-creds")
async def upload_source_gcp_creds(sid: str, file: UploadFile = File(...), _rbac=Depends(require_mapper)):
    s = _session_or_404(sid)
    content = await file.read()
    if len(content) > 64 * 1024:
        raise HTTPException(400, "Credentials file too large (max 64KB)")
    try:
        creds_dict = json.loads(content)
    except json.JSONDecodeError:
        raise HTTPException(422, "File is not valid JSON")
    required = {"type", "project_id", "private_key", "client_email"}
    if not required.issubset(creds_dict.keys()):
        raise HTTPException(422, f"Missing required service account fields: {required - set(creds_dict.keys())}")
    if "source_bq_config" not in s:
        s["source_bq_config"] = {}
    s["source_bq_config"]["gcp_creds_json"] = creds_dict
    if not s["source_bq_config"].get("project"):
        s["source_bq_config"]["project"] = creds_dict.get("project_id", "")
    return {
        "ok":           True,
        "project_id":   creds_dict.get("project_id", ""),
        "client_email": creds_dict.get("client_email", ""),
    }


class SourceBQRequest(BaseModel):
    project:      str
    dataset:      str
    table_filter: str = ""


@router.post("/api/sessions/{sid}/source-bq")
async def source_bq_connect(sid: str, req: SourceBQRequest, _rbac=Depends(require_mapper)):
    s = _session_or_404(sid)
    if "source_bq_config" not in s:
        s["source_bq_config"] = {}
    cfg = s["source_bq_config"]
    tbl_filter = [t.strip() for t in req.table_filter.split(",") if t.strip()]
    try:
        bq_tables = await asyncio.to_thread(
            crawl_bq,
            req.project,
            req.dataset,
            "",
            tbl_filter or None,
            cfg.get("gcp_creds_json"),
        )
    except Exception as e:
        raise HTTPException(422, f"BigQuery source crawl failed: {e}")

    if not bq_tables:
        raise HTTPException(422, f"No tables found in {req.project}.{req.dataset}")

    schema_data = {
        "tables": [
            {"name": t["table"], "columns": t["columns"]}
            for t in bq_tables
        ]
    }
    s["schema_data"]  = schema_data
    s["source_type"]  = "bigquery"
    s["filename"]     = f"bq://{req.project}.{req.dataset}"
    s["status"]       = "schema_uploaded"
    cfg["project"]    = req.project
    cfg["dataset"]    = req.dataset

    total = sum(len(t["columns"]) for t in bq_tables)
    return {
        "ok":          True,
        "tables":      len(bq_tables),
        "columns":     total,
        "table_names": [t["table"] for t in bq_tables],
        "preview":     bq_tables[0]["columns"][:8] if bq_tables else [],
    }


@router.post("/api/sessions/{sid}/target-files")
async def upload_target_files(sid: str, files: List[UploadFile] = File(...), _rbac=Depends(require_mapper)):
    s = _session_or_404(sid)
    if len(files) > 50:
        raise HTTPException(400, "Maximum 50 target files per session")
    target_tables = []

    from pathlib import Path
    for f in files:
        content   = await f.read()
        safe_name = _validate_upload(f.filename or "target.csv", content)
        file_stem = Path(safe_name).stem

        try:
            parsed = parse_schema_file(content, safe_name)
        except Exception as e:
            raise HTTPException(422, f"Could not parse {safe_name}: {e}")

        for tbl in parsed["tables"]:
            table_name = tbl["name"] if tbl["name"] not in ("source", "sheet") else file_stem
            columns    = [
                {"name": c["name"], "type": c["type"], "nullable": c.get("nullable", True)}
                for c in tbl["columns"]
            ]
            if columns:
                target_tables.append({"table": table_name, "columns": columns})

    s["target_mode"]       = "files"
    s["target_files_data"] = target_tables

    total_cols = sum(len(t["columns"]) for t in target_tables)
    return {
        "ok":            True,
        "tables":        len(target_tables),
        "table_names":   [t["table"] for t in target_tables],
        "total_columns": total_cols,
    }


class MigrationContextRequest(BaseModel):
    fk_rules:         List[str] = []
    domain_context:   str = ""
    scale_rules:      List[str] = []
    source_table_fqn: str = ""


@router.post("/api/sessions/{sid}/migration-context")
async def set_migration_context(sid: str, req: MigrationContextRequest, _rbac=Depends(require_mapper)):
    s = _session_or_404(sid)
    s["migration_context"] = {
        "fk_rules":         req.fk_rules,
        "domain_context":   req.domain_context,
        "scale_rules":      req.scale_rules,
        "source_table_fqn": req.source_table_fqn,
    }
    if req.source_table_fqn:
        s["source_table_fqn"] = req.source_table_fqn
    _save_sessions()
    return {
        "status": "ok",
        "fk_rules_count":    len(req.fk_rules),
        "scale_rules_count": len(req.scale_rules),
        "source_table_fqn":  req.source_table_fqn,
    }


class TargetConnectRequest(BaseModel):
    db_type:           str
    connection_string: str
    schema_filter:     Optional[str] = ""
    table_filter:      Optional[str] = ""


@router.post("/api/sessions/{sid}/target-connect")
async def connect_target_db(sid: str, req: TargetConnectRequest, _rbac=Depends(require_mapper)):
    s = _session_or_404(sid)
    try:
        result = await asyncio.to_thread(
            crawl_source_db, req.db_type, req.connection_string,
            req.schema_filter, req.table_filter,
        )
    except Exception as e:
        raise HTTPException(422, str(e))

    target_tables = [{"table": t["name"], "columns": t["columns"]} for t in result["tables"]]
    s["target_mode"]       = "files"
    s["target_files_data"] = target_tables
    s["target_db_type"]    = req.db_type

    total_cols = sum(len(t["columns"]) for t in target_tables)
    return {
        "ok":            True,
        "tables":        len(target_tables),
        "table_names":   [t["table"] for t in target_tables],
        "total_columns": total_cols,
        "db_type":       req.db_type,
    }


@router.post("/api/sessions/{sid}/source-databricks")
async def source_databricks(sid: str, req: DatabricksUnityRequest, _rbac=Depends(require_mapper)):
    s = _session_or_404(sid)
    try:
        schema_data = await asyncio.to_thread(
            _crawl_databricks_unity,
            req.server_hostname.strip(),
            req.http_path.strip(),
            req.access_token,
            req.catalog.strip(),
            req.schema_filter,
            req.table_filter,
        )
    except RuntimeError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        raise HTTPException(500, f"Databricks connection failed: {e}")

    s["schema_data"]          = schema_data
    s["source_type"]          = "databricks_unity"
    s["source_conn_display"]  = f"databricks://{req.server_hostname}/{req.catalog or 'default'}"
    s["filename"]             = s["source_conn_display"]
    s["status"]               = "schema_uploaded"
    _save_sessions()

    total = sum(len(t["columns"]) for t in schema_data["tables"])
    return {
        "ok":          True,
        "tables":      len(schema_data["tables"]),
        "columns":     total,
        "table_names": [t["name"] for t in schema_data["tables"]],
        "schema":      schema_data["tables"],
    }


@router.get("/api/sessions/{sid}/usage")
async def get_session_usage(sid: str):
    s = _sessions.get(sid)
    if not s:
        raise HTTPException(status_code=404, detail="Session not found")
    usage = s.get("usage", {
        "calls": 0, "input_tokens": 0, "output_tokens": 0,
        "cost_usd": 0.0, "provider": "", "model": "", "breakdown": [],
    })
    provider = usage.get("provider", "")
    model    = usage.get("model", "")
    pricing  = _pricing_for(provider, model)
    return {
        **usage,
        "pricing": pricing,
        "total_tokens": usage.get("input_tokens", 0) + usage.get("output_tokens", 0),
    }


@router.get("/api/version")
async def version():
    return {"version": "2.0.0", "name": "xREF Agent", "stage": "v2-multi-source"}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/api/health")
async def health():
    """Basic liveness check — returns 200 if the process is alive."""
    return {"status": "ok", "ts": _now()}


@router.get("/api/health/detailed")
async def health_detailed():
    """Deep health check — verifies each subsystem and returns per-component status."""
    from app.state import _sessions, _mapping_memory

    checks: dict = {}

    # Session store
    try:
        checks["session_store"] = {"status": "ok", "count": len(_sessions)}
    except Exception as e:
        checks["session_store"] = {"status": "error", "error": str(e)}

    # Mapping memory store
    try:
        checks["mapping_memory"] = {"status": "ok", "count": len(_mapping_memory)}
    except Exception as e:
        checks["mapping_memory"] = {"status": "error", "error": str(e)}

    # Database (Postgres) — optional
    try:
        from app.core.db import db_available, engine
        from sqlalchemy import text  # local import — sqlalchemy may be optional
        if db_available() and engine is not None:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            checks["database"] = {"status": "ok", "mode": "postgres"}
        else:
            checks["database"] = {"status": "ok", "mode": "file"}
    except Exception as e:
        checks["database"] = {"status": "error", "error": str(e)}

    # Redis — optional
    try:
        redis_url = os.getenv("REDIS_URL", "")
        if redis_url:
            import redis as redis_lib  # type: ignore
            r = redis_lib.from_url(redis_url, socket_timeout=2)
            r.ping()
            checks["redis"] = {"status": "ok"}
        else:
            checks["redis"] = {"status": "not_configured"}
    except Exception as e:
        checks["redis"] = {"status": "error", "error": str(e)}

    # LLM API key
    try:
        has_anthropic = _is_real_key(_ANTHROPIC_API_KEY)
        has_deepseek  = _is_real_key(_DEEPSEEK_API_KEY)
        checks["llm"] = {
            "status":          "ok" if (has_anthropic or has_deepseek) else "warning",
            "provider":        _DEFAULT_PROVIDER,
            "key_configured":  has_anthropic or has_deepseek,
        }
    except Exception as e:
        checks["llm"] = {"status": "error", "error": str(e)}

    overall = (
        "ok"
        if all(c.get("status") in ("ok", "not_configured") for c in checks.values())
        else "degraded"
    )
    status_code = 200 if overall == "ok" else 207

    return JSONResponse(
        status_code=status_code,
        content={
            "status":   overall,
            "ts":       _now(),
            "checks":   checks,
            "version":  "2.0.0",
        },
    )


@router.get("/api/ready")
async def readiness():
    """Kubernetes readiness probe — returns 200 with the app's load state."""
    from app.state import _sessions
    return {"ready": True, "sessions_loaded": len(_sessions)}


@router.get("/api/metrics")
async def prometheus_metrics():
    """Prometheus-format metrics endpoint.
    Compatible with Datadog, Grafana, and any Prometheus scraper."""
    from app.state import _sessions, _audit_events

    total_sessions = len(_sessions)
    running        = sum(1 for s in _sessions.values() if s.get("running"))
    done           = sum(1 for s in _sessions.values() if s.get("status") == "done")
    total_mappings = sum(len(s.get("mappings", [])) for s in _sessions.values())

    lines = [
        "# HELP xref_sessions_total Total number of sessions",
        "# TYPE xref_sessions_total gauge",
        f"xref_sessions_total {total_sessions}",
        "# HELP xref_sessions_running Currently running pipeline sessions",
        "# TYPE xref_sessions_running gauge",
        f"xref_sessions_running {running}",
        "# HELP xref_sessions_done Completed sessions",
        "# TYPE xref_sessions_done gauge",
        f"xref_sessions_done {done}",
        "# HELP xref_mappings_total Total column mappings across all sessions",
        "# TYPE xref_mappings_total gauge",
        f"xref_mappings_total {total_mappings}",
        "# HELP xref_audit_events_total Total audit events captured",
        "# TYPE xref_audit_events_total gauge",
        f"xref_audit_events_total {len(_audit_events)}",
    ]
    return PlainTextResponse(
        "\n".join(lines) + "\n",
        media_type="text/plain; version=0.0.4",
    )
