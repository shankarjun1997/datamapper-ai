"""
app/main.py — thin FastAPI factory (replaces server.py).

Import chain:
  config.py → state.py → core/* → parsers/* → connectors/* → intelligence/* → routers/* → main.py
"""
from __future__ import annotations

import asyncio
import os
import time
import uuid
from contextlib import asynccontextmanager
from contextvars import ContextVar

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request as StarletteRequest

# Configure structured logging before any other app module pulls a logger.
from app.core.logging_config import setup_logging
setup_logging()

from app.config import _ALLOWED_ORIGINS, _STATIC, logger
from app.state import (
    _load_audit_events,
    _load_mapping_memory,
    _load_sessions,
    _load_tenants,
)
import app.state as _state

# ── Routers ───────────────────────────────────────────────────────────────────
from app.routers import auth, sessions, schema, pipeline, mappings, exports, admin, providers


# ── Request-scoped context (used by JSON formatter) ───────────────────────────
_request_id_ctx: ContextVar[str] = ContextVar("request_id", default="")
_tenant_ctx: ContextVar[str] = ContextVar("tenant", default="")


# ── Lifespan ──────────────────────────────────────────────────────────────────
def _validate_startup_secrets() -> list[str]:
    """Check for missing or placeholder secrets and return warning strings.

    In production (DM_ENV=production) a missing JWT_SECRET is fatal.
    Otherwise we log warnings and continue — permissive for local dev.
    """
    warnings: list[str] = []
    jwt_secret = os.getenv("JWT_SECRET", "")
    if not jwt_secret:
        warnings.append("JWT_SECRET is not set — tokens are signed with a weak fallback")
    elif len(jwt_secret) < 16:
        warnings.append("JWT_SECRET is very short (< 16 chars) — use a strong random value")
    elif any(w in jwt_secret.lower() for w in ["change", "secret", "example", "default", "placeholder", "xref"]):
        warnings.append("JWT_SECRET looks like a placeholder — replace with a strong random value in production")

    dm_env = os.getenv("DM_ENV", "dev")
    if dm_env == "production":
        if not os.getenv("ANTHROPIC_API_KEY") and not os.getenv("OPENAI_API_KEY") and not os.getenv("DEEPSEEK_API_KEY"):
            warnings.append("No LLM API key configured in production (ANTHROPIC_API_KEY / OPENAI_API_KEY / DEEPSEEK_API_KEY)")
        if not jwt_secret:
            raise RuntimeError("JWT_SECRET must be set in production — refusing to start")
    return warnings


@asynccontextmanager
async def lifespan(app_: FastAPI):
    # Re-run setup_logging inside the lifespan so DM_ENV picked up from a .env
    # loaded after the first import still takes effect.
    setup_logging()

    # Secrets validation (warns in dev, fatal in production for missing JWT_SECRET)
    for w in _validate_startup_secrets():
        logger.warning("STARTUP SECURITY WARNING: %s", w)

    _state._L3_SEM = asyncio.Semaphore(2)

    # Probe Postgres. If reachable, flip _DB_MODE and ensure schema exists.
    db_mode = _state.activate_db_mode()
    if db_mode:
        logger.info("Running in DB mode (Postgres)")
        try:
            from app.core.db_store import ensure_schema, migrate_json_to_db
            # Best-effort schema bootstrap. Prefer `alembic upgrade head` in
            # production; this catches the case where alembic hasn't been run yet.
            ensure_schema()
            # One-time idempotent migration from JSON files into Postgres.
            _migrate_json_files_to_db(migrate_json_to_db)
        except Exception as _e:
            logger.error("DB bootstrap failed, falling back to JSON: %s", _e)
            _state._DB_MODE = False
            db_mode = False

    if not db_mode:
        logger.info("Running in file mode (JSON)")

    _load_sessions()
    _load_mapping_memory()
    _load_audit_events()
    _load_tenants()
    # Load CrewAI self-learning store (safe no-op if file absent)
    try:
        from app.core.crew_learnings import load_learnings
        load_learnings()
    except Exception as _le:
        logger.warning("Could not load crew learnings: %s", _le)
    yield


def _migrate_json_files_to_db(migrate_json_to_db) -> None:
    """Idempotently copy existing JSON-file state into Postgres on first boot.

    Reads each JSON file directly (we don't want to populate the in-memory
    caches yet — that happens via _load_* after the DB is the source of truth)
    and hands them to ``migrate_json_to_db``. Safe to run on every startup; the
    DB-side upsert handles duplicates.
    """
    import json as _json
    import os as _os
    from app.config import _SESSION_STORE_PATH, _AUDIT_STORE_PATH, _MEMORY_STORE_PATH

    sessions: dict = {}
    audit_events: list = []
    mapping_memory: dict = {}

    if _os.path.exists(_SESSION_STORE_PATH):
        try:
            with open(_SESSION_STORE_PATH) as f:
                sessions = _json.load(f) or {}
        except Exception:
            sessions = {}

    if _os.path.exists(_AUDIT_STORE_PATH):
        try:
            with open(_AUDIT_STORE_PATH) as f:
                audit_events = _json.load(f) or []
        except Exception:
            audit_events = []

    if _os.path.exists(_MEMORY_STORE_PATH):
        try:
            with open(_MEMORY_STORE_PATH) as f:
                mapping_memory = _json.load(f) or {}
        except Exception:
            mapping_memory = {}

    if not (sessions or audit_events or mapping_memory):
        return

    counts = migrate_json_to_db(sessions, audit_events, mapping_memory)
    if any(counts.values()):
        logger.info(
            "JSON->Postgres migration: %d sessions, %d audit events, %d memory entries",
            counts.get("sessions", 0),
            counts.get("audit_events", 0),
            counts.get("mapping_memory", 0),
        )


# ── App factory ───────────────────────────────────────────────────────────────
app = FastAPI(title="xREF Agent", version="2.0.0", lifespan=lifespan)

# CORS
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Content-Type", "Authorization", "X-Session-Id"],
    allow_credentials=False,
    max_age=600,
)


# Security headers
_CSP = (
    "default-src 'self'; "
    "script-src 'self' 'unsafe-inline' "
        "https://cdn.tailwindcss.com https://unpkg.com https://cdnjs.cloudflare.com; "
    "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; "
    "font-src 'self' https://fonts.gstatic.com; "
    "connect-src 'self'; "
    "img-src 'self' data:; "
    "frame-ancestors 'none'"
)


class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: StarletteRequest, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"]  = "nosniff"
        response.headers["X-Frame-Options"]         = "DENY"
        response.headers["Referrer-Policy"]         = "strict-origin-when-cross-origin"
        response.headers["X-XSS-Protection"]        = "1; mode=block"
        if os.getenv("DM_ENV", "dev") == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
            response.headers["Content-Security-Policy"] = _CSP
        return response


app.add_middleware(SecurityHeadersMiddleware)


# ── Request logging + request-id propagation ──────────────────────────────────
class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """Tag every request with a stable request_id, propagate it via header,
    and log start/end with method, path, status, and latency."""

    async def dispatch(self, request: StarletteRequest, call_next):
        req_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())[:8]
        _request_id_ctx.set(req_id)

        tenant = request.headers.get("X-Tenant", "") or ""
        if tenant:
            _tenant_ctx.set(tenant)

        start = time.time()
        response = None
        try:
            response = await call_next(request)
            return response
        except Exception as e:
            logger.error(
                "Unhandled exception",
                extra={
                    "request_id": req_id,
                    "path": str(request.url.path),
                    "method": request.method,
                    "error": str(e),
                },
            )
            raise
        finally:
            duration_ms = round((time.time() - start) * 1000, 1)
            # Skip the noisy basic health check from access logs
            if "/api/health" not in str(request.url.path):
                logger.info(
                    "request",
                    extra={
                        "request_id": req_id,
                        "method": request.method,
                        "path": str(request.url.path),
                        "status_code": response.status_code if response is not None else 500,
                        "duration_ms": duration_ms,
                    },
                )
            if response is not None:
                response.headers["X-Request-ID"] = req_id


app.add_middleware(RequestLoggingMiddleware)

# ── Static frontend ───────────────────────────────────────────────────────────
if (_STATIC / "index.html").exists():
    @app.get("/", include_in_schema=False)
    async def root():
        return FileResponse(_STATIC / "index.html")

    # Alias so login.html redirect to 'index.html' also works
    @app.get("/index.html", include_in_schema=False)
    async def root_alias():
        return FileResponse(_STATIC / "index.html")

if (_STATIC / "login.html").exists():
    @app.get("/login", include_in_schema=False)
    async def login_page():
        return FileResponse(_STATIC / "login.html")

# ── Include routers ───────────────────────────────────────────────────────────
app.include_router(auth.router)
app.include_router(sessions.router)
app.include_router(schema.router)
app.include_router(pipeline.router)
app.include_router(mappings.router)
app.include_router(exports.router)
app.include_router(admin.router)
app.include_router(providers.router)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=7788, reload=True, log_level="info")
