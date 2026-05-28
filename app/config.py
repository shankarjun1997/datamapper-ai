"""
app/config.py — all constants & env vars.
No imports from this project.
"""
from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Dict

from dotenv import load_dotenv

# ── Load .env from project root ───────────────────────────────────────────────
_PROJECT_ROOT = Path(__file__).parent.parent   # app/ -> project root
_ENV_PATH = _PROJECT_ROOT / ".env"
if _ENV_PATH.exists():
    load_dotenv(_ENV_PATH, override=False)

# Configure logging via the structured logging module.
# JSON mode auto-activates when DM_ENV=production; otherwise human-readable.
# Imported here so CLI scripts / tests that touch app.config get a logger
# without having to bootstrap the full app lifespan.
from app.core.logging_config import setup_logging as _setup_logging
_setup_logging()
logger = logging.getLogger("xref_agent")

# ── Global config (overridable per-session via user API keys) ─────────────────
_DEFAULT_PROVIDER   = os.getenv("DM_PROVIDER", "deepseek")
_ANTHROPIC_API_KEY  = os.getenv("ANTHROPIC_API_KEY", "")
_DEEPSEEK_API_KEY   = os.getenv("LLM_API_KEY", "")
_DEEPSEEK_BASE_URL  = os.getenv("LLM_BASE_URL", "https://api.deepseek.com/v1")
_DEEPSEEK_MODEL     = os.getenv("LLM_MODEL", "deepseek-chat")
_CLAUDE_MODEL       = os.getenv("DM_CLAUDE_MODEL", "claude-sonnet-4-6")
_BQ_PROJECT         = os.getenv("BQ_PROJECT_ID", "")
_BQ_DATASET         = os.getenv("BQ_DATASET", "")
_GCP_CREDS          = os.getenv("GOOGLE_APPLICATION_CREDENTIALS", "")
_JIRA_URL           = os.getenv("JIRA_URL", "")
_JIRA_EMAIL         = os.getenv("JIRA_EMAIL", "")
_JIRA_TOKEN         = os.getenv("JIRA_TOKEN", "")

# ── Auth configuration ─────────────────────────────────────────────────────────
_AUTH_SECRET     = os.getenv("XREF_SECRET_KEY", "xref-demo-secret-change-in-prod-2026")
_AUTH_TOKEN_TTL  = int(os.getenv("XREF_TOKEN_TTL_HOURS", "24")) * 3600  # seconds

_DEFAULT_TENANTS = [
    {
        "slug": "infinite",
        "name": "Infinite",
        "plan": "enterprise",
        "users": [
            {
                "email": os.getenv("XREF_ADMIN_EMAIL", "admin@infinite.io"),
                "password": os.getenv("XREF_ADMIN_PASSWORD", "xref2026"),
                "role": "admin",
                "active": True,
                "invited_at": None,
                "last_login": None,
                "display_name": "Admin",
            }
        ],
    },
    {
        "slug": "demo",
        "name": "Demo Workspace",
        "plan": "trial",
        "users": [
            {
                "email": "demo@xref.ai",
                "password": "demo",
                "role": "admin",
                "active": True,
                "invited_at": None,
                "last_login": None,
                "display_name": "Demo Admin",
            }
        ],
    },
]

# ── Runtime paths ─────────────────────────────────────────────────────────────
_RUNTIME_DIR          = _PROJECT_ROOT / "runtime"
_RUNTIME_DIR.mkdir(exist_ok=True)
_SESSION_STORE_PATH   = str(_RUNTIME_DIR / ".xref_sessions.json")
_MEMORY_STORE_PATH    = str(_RUNTIME_DIR / ".xref_mapping_memory.json")
_AUDIT_STORE_PATH     = str(_RUNTIME_DIR / "audit_events.json")
_TENANTS_STORE_PATH   = str(_RUNTIME_DIR / "xref_tenants.json")

# ── Static / frontend paths ───────────────────────────────────────────────────
_STATIC               = _PROJECT_ROOT / "frontend"

# ── Upload validation ─────────────────────────────────────────────────────────
_ALLOWED_UPLOAD_EXTS   = {".csv", ".xlsx", ".xls", ".ddl", ".sql", ".txt"}
_MAX_UPLOAD_BYTES      = int(os.getenv("DM_MAX_UPLOAD_MB", "20")) * 1024 * 1024
_ALLOWED_CONTENT_TYPES = {
    "text/csv", "application/vnd.ms-excel",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    "text/plain", "application/octet-stream",
}

# ── CORS ──────────────────────────────────────────────────────────────────────
_ALLOWED_ORIGINS_RAW = os.getenv("DM_ALLOWED_ORIGINS", "*")
_ALLOWED_ORIGINS = (
    ["*"] if _ALLOWED_ORIGINS_RAW.strip() == "*"
    else [o.strip() for o in _ALLOWED_ORIGINS_RAW.split(",") if o.strip()]
)

# ── Rate limiting ─────────────────────────────────────────────────────────────
_RATE_LIMIT  = int(os.getenv("DM_RATE_LIMIT", "60"))   # requests per window
_RATE_WINDOW = int(os.getenv("DM_RATE_WINDOW", "60"))  # seconds

# ── Provider catalog ──────────────────────────────────────────────────────────
_PROVIDER_CATALOG: Dict = {
    "claude": {
        "label": "Anthropic Claude",
        "base_url": "",
        "models": [
            {"id": "claude-sonnet-4-6",         "label": "Claude Sonnet 4.6 (balanced)",       "input": 3.00,  "output": 15.00},
            {"id": "claude-opus-4-6",            "label": "Claude Opus 4.6 (most capable)",     "input": 15.00, "output": 75.00},
            {"id": "claude-haiku-4-5-20251001",  "label": "Claude Haiku 4.5 (fastest / cheap)", "input": 0.80,  "output": 4.00},
        ],
    },
    "openai": {
        "label": "OpenAI",
        "base_url": "https://api.openai.com/v1",
        "models": [
            {"id": "gpt-4o",       "label": "GPT-4o (flagship)",   "input": 2.50,  "output": 10.00},
            {"id": "gpt-4o-mini",  "label": "GPT-4o mini (fast)",  "input": 0.15,  "output": 0.60},
            {"id": "gpt-4-turbo",  "label": "GPT-4 Turbo",         "input": 10.00, "output": 30.00},
            {"id": "o1",           "label": "o1 (reasoning)",      "input": 15.00, "output": 60.00},
            {"id": "o3-mini",      "label": "o3-mini (reasoning)", "input": 1.10,  "output": 4.40},
        ],
    },
    "deepseek": {
        "label": "DeepSeek",
        "base_url": "https://api.deepseek.com/v1",
        "models": [
            {"id": "deepseek-chat",     "label": "DeepSeek Chat V3",     "input": 0.27, "output": 1.10},
            {"id": "deepseek-reasoner", "label": "DeepSeek Reasoner R1", "input": 0.55, "output": 2.19},
        ],
    },
    "groq": {
        "label": "Groq",
        "base_url": "https://api.groq.com/openai/v1",
        "models": [
            {"id": "llama-3.3-70b-versatile", "label": "Llama 3.3 70B",   "input": 0.59, "output": 0.79},
            {"id": "llama-3.1-8b-instant",    "label": "Llama 3.1 8B",    "input": 0.05, "output": 0.08},
            {"id": "mixtral-8x7b-32768",       "label": "Mixtral 8x7B",    "input": 0.24, "output": 0.24},
        ],
    },
    "mistral": {
        "label": "Mistral AI",
        "base_url": "https://api.mistral.ai/v1",
        "models": [
            {"id": "mistral-large-latest", "label": "Mistral Large", "input": 2.00, "output": 6.00},
            {"id": "mistral-small-latest", "label": "Mistral Small", "input": 0.10, "output": 0.30},
            {"id": "open-mixtral-8x22b",   "label": "Mixtral 8x22B", "input": 2.00, "output": 6.00},
        ],
    },
    "ollama": {
        "label": "Ollama (local)",
        "base_url": "http://localhost:11434/v1",
        "models": [
            {"id": "llama3.2",       "label": "Llama 3.2",       "input": 0.0, "output": 0.0},
            {"id": "mistral",        "label": "Mistral 7B",       "input": 0.0, "output": 0.0},
            {"id": "gemma2",         "label": "Gemma 2",          "input": 0.0, "output": 0.0},
            {"id": "qwen2.5-coder",  "label": "Qwen 2.5-Coder",  "input": 0.0, "output": 0.0},
        ],
    },
    "custom": {
        "label": "Custom / OpenAI-compat",
        "base_url": "",
        "models": [],
    },
}

# DB connection examples and install hints (used by source_db connector)
_DB_CONN_EXAMPLES = {
    "postgres":   "postgresql+psycopg2://user:password@host:5432/dbname",
    "mysql":      "mysql+pymysql://user:password@host:3306/dbname",
    "mssql":      "mssql+pyodbc://user:password@host:1433/dbname?driver=ODBC+Driver+17+for+SQL+Server",
    "azuresql":   "mssql+pyodbc://user:password@server.database.windows.net:1433/dbname?driver=ODBC+Driver+17+for+SQL+Server",
    "snowflake":  "snowflake://user:password@account/dbname/schema",
    "databricks": "databricks://token:dapi_token@host/dbname",
    "oracle":     "oracle+cx_oracle://user:password@host:1521/?service_name=ORCLCDB",
    "redshift":   "redshift+psycopg2://user:password@host:5439/dbname",
    "teradata":   "teradatasql://user:password@host/dbname",
}

_DB_INSTALL_HINTS = {
    "postgres":   "pip install psycopg2-binary",
    "mysql":      "pip install pymysql",
    "mssql":      "pip install pyodbc",
    "azuresql":   "pip install pyodbc",
    "snowflake":  "pip install snowflake-connector-python",
    "databricks": "pip install databricks-sql-connector",
    "oracle":     "pip install cx_Oracle  # or oracledb",
    "redshift":   "pip install psycopg2-binary",
    "teradata":   "pip install teradatasql",
}

_INFOSYS_QUERY = """
    SELECT table_name, column_name, data_type, is_nullable
    FROM information_schema.columns
    WHERE table_schema NOT IN ('information_schema','pg_catalog','sys','INFORMATION_SCHEMA','performance_schema')
    ORDER BY table_name, ordinal_position
"""

# Session skip keys (fields too large / not serialisable to persist)
_SESSION_SKIP_KEYS = {"running", "log"}

# Admin tenant slug
_ADMIN_TENANT = os.getenv("XREF_ADMIN_TENANT", "infinite")
