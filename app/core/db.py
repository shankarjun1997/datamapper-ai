"""
app/core/db.py — SQLAlchemy sync engine + session factory.

Connection string priority:
  1. DATABASE_URL env var (full postgres:// or postgresql+psycopg2:// URL)
  2. Individual POSTGRES_* vars assembled into a URL
  3. Fallback: None (signals callers to use JSON-file store)

Synchronous engine on purpose — callers wrap blocking calls with
``asyncio.to_thread`` from FastAPI request handlers. Keeps the data
layer dependency-light and avoids forcing an async driver everywhere.
"""
from __future__ import annotations

import os
from typing import Optional
from urllib.parse import quote_plus

from app.config import logger

try:
    from sqlalchemy import create_engine
    from sqlalchemy.engine import Engine
    from sqlalchemy.orm import sessionmaker, Session
    _SA_AVAILABLE = True
except Exception as _e:  # pragma: no cover — SQLAlchemy missing
    logger.warning("SQLAlchemy not available: %s", _e)
    create_engine = None  # type: ignore
    sessionmaker = None   # type: ignore
    Engine = object       # type: ignore
    Session = object      # type: ignore
    _SA_AVAILABLE = False


# ─────────────────────────────────────────────────────────────────────────────
# Connection string resolution
# ─────────────────────────────────────────────────────────────────────────────
def _resolve_database_url() -> Optional[str]:
    """Return a SQLAlchemy URL or None when no Postgres is configured."""
    url = os.getenv("DATABASE_URL", "").strip()
    if url:
        # SQLAlchemy needs an explicit driver scheme; rewrite bare 'postgres://'
        # (Heroku-style) to 'postgresql+psycopg2://'.
        if url.startswith("postgres://"):
            url = "postgresql+psycopg2://" + url[len("postgres://"):]
        elif url.startswith("postgresql://") and "+" not in url.split("://", 1)[0]:
            url = "postgresql+psycopg2://" + url[len("postgresql://"):]
        return url

    host = os.getenv("POSTGRES_HOST", "").strip()
    if not host:
        return None
    user = os.getenv("POSTGRES_USER", "xref")
    password = os.getenv("POSTGRES_PASSWORD", "")
    port = os.getenv("POSTGRES_PORT", "5432")
    dbname = os.getenv("POSTGRES_DB", "xref")
    if password:
        return f"postgresql+psycopg2://{quote_plus(user)}:{quote_plus(password)}@{host}:{port}/{dbname}"
    return f"postgresql+psycopg2://{quote_plus(user)}@{host}:{port}/{dbname}"


# ─────────────────────────────────────────────────────────────────────────────
# Engine + session factory (lazy probe)
# ─────────────────────────────────────────────────────────────────────────────
DATABASE_URL: Optional[str] = _resolve_database_url()
engine: Optional["Engine"] = None
SessionLocal = None  # type: ignore[assignment]
_DB_REACHABLE: Optional[bool] = None  # tri-state cache: None=unknown


def _build_engine() -> None:
    """Create the global engine + sessionmaker if not yet built."""
    global engine, SessionLocal
    if not _SA_AVAILABLE or not DATABASE_URL:
        return
    if engine is not None:
        return
    try:
        engine = create_engine(
            DATABASE_URL,
            pool_pre_ping=True,
            pool_size=int(os.getenv("DM_DB_POOL_SIZE", "5")),
            max_overflow=int(os.getenv("DM_DB_MAX_OVERFLOW", "10")),
            future=True,
        )
        SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, future=True)
        logger.info("DB engine created for %s", _scrub(DATABASE_URL))
    except Exception as _e:  # pragma: no cover
        logger.error("Failed to build DB engine: %s", _e)
        engine = None
        SessionLocal = None


def _scrub(url: str) -> str:
    """Mask password in a DSN for logs."""
    try:
        if "@" in url and "://" in url:
            head, tail = url.split("://", 1)
            creds, host = tail.split("@", 1)
            if ":" in creds:
                user = creds.split(":", 1)[0]
                return f"{head}://{user}:***@{host}"
        return url
    except Exception:
        return "<dsn>"


_build_engine()


def db_available() -> bool:
    """Return True iff Postgres is configured AND a live connection succeeds.

    Result is cached after the first probe to avoid hammering the server on
    every write. Call ``reset_db_probe()`` to re-test (e.g., from tests).
    """
    global _DB_REACHABLE
    if not _SA_AVAILABLE or DATABASE_URL is None or engine is None:
        _DB_REACHABLE = False
        return False
    if _DB_REACHABLE is not None:
        return _DB_REACHABLE
    try:
        with engine.connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        _DB_REACHABLE = True
        logger.info("Postgres reachable — DB mode active")
    except Exception as _e:
        _DB_REACHABLE = False
        logger.warning("Postgres unreachable, falling back to JSON file store: %s", _e)
    return _DB_REACHABLE


def reset_db_probe() -> None:
    """Force re-probing on next ``db_available()`` call."""
    global _DB_REACHABLE
    _DB_REACHABLE = None


def get_db_session():
    """Context-managed Session. Use as ``with get_db_session() as s:``."""
    if SessionLocal is None:
        raise RuntimeError("DB not configured — db_available() returned False")
    return SessionLocal()
