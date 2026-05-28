"""
app/state.py — all shared mutable state variables.
Imports only from config.py.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
from typing import Any, Dict, List, Optional

from app.config import (
    _MEMORY_STORE_PATH,
    _SESSION_STORE_PATH,
    _AUDIT_STORE_PATH,
    _TENANTS_STORE_PATH,
    _SESSION_SKIP_KEYS,
    logger,
)

# ── In-memory session store ───────────────────────────────────────────────────
_sessions: Dict[str, Dict[str, Any]] = {}
_sse_queues: Dict[str, asyncio.Queue] = {}

# ── Audit logs ────────────────────────────────────────────────────────────────
_audit_log: List[Dict[str, Any]] = []       # Gate-2 audit records (mapping snapshots)
_audit_events: List[Dict[str, Any]] = []    # structured event log for traceability

# ── Cross-session persistent mapping memory ───────────────────────────────────
_mapping_memory: Dict[str, Dict[str, Any]] = {}

# ── Bounded concurrency for LLM calls ────────────────────────────────────────
# Initialized in lifespan (requires running event loop)
_L3_SEM: Optional[asyncio.Semaphore] = None

# ── Tenant registry ───────────────────────────────────────────────────────────
_TENANTS: Dict[str, Dict] = {}  # keyed by slug — populated at startup

# ── Persistence mode flag ─────────────────────────────────────────────────────
# Flipped to True by ``activate_db_mode()`` once Postgres is confirmed
# reachable at startup. While False, all persistence routes hit JSON files.
_DB_MODE: bool = False


def activate_db_mode() -> bool:
    """Probe Postgres and flip _DB_MODE if reachable. Returns the final flag."""
    global _DB_MODE
    try:
        from app.core.db import db_available
        _DB_MODE = bool(db_available())
    except Exception as _e:
        logger.warning("DB activation failed: %s", _e)
        _DB_MODE = False
    return _DB_MODE


def is_db_mode() -> bool:
    return _DB_MODE


# ─────────────────────────────────────────────────────────────────────────────
# Session persistence
# ─────────────────────────────────────────────────────────────────────────────

def _save_sessions() -> None:
    """Persist _sessions to DB (when configured) or JSON file. Best-effort."""
    slim: Dict[str, Dict[str, Any]] = {}
    for sid, s in _sessions.items():
        slim[sid] = {k: v for k, v in s.items() if k not in _SESSION_SKIP_KEYS}

    if _DB_MODE:
        try:
            from app.core.db_store import db_save_all_sessions
            db_save_all_sessions(slim)
            return
        except Exception as _e:
            logger.error("DB session save failed, falling back to JSON: %s", _e)

    try:
        with open(_SESSION_STORE_PATH, "w") as f:
            json.dump(slim, f)
    except Exception:
        pass


def _load_sessions() -> None:
    """Load persisted sessions (DB if available, else JSON file) into memory."""
    global _sessions
    if _DB_MODE:
        try:
            from app.core.db_store import db_load_all_sessions
            loaded = db_load_all_sessions()
            for sid, s in loaded.items():
                s.setdefault("running", False)
                s.setdefault("log", [])
                _sessions[sid] = s
            logger.info("Loaded %d sessions from Postgres", len(loaded))
            return
        except Exception as _e:
            logger.error("DB session load failed, trying JSON fallback: %s", _e)

    if not os.path.exists(_SESSION_STORE_PATH):
        return
    try:
        with open(_SESSION_STORE_PATH) as f:
            data = json.load(f)
        for sid, s in data.items():
            s.setdefault("running", False)
            s.setdefault("log", [])
            _sessions[sid] = s
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Mapping memory persistence
# ─────────────────────────────────────────────────────────────────────────────

def _save_mapping_memory() -> None:
    """Persist cross-session mapping memory (DB or JSON)."""
    if _DB_MODE:
        try:
            from app.core.db_store import db_save_mapping_memory
            db_save_mapping_memory(_mapping_memory)
            return
        except Exception as _e:
            logger.error("DB mapping_memory save failed, falling back to JSON: %s", _e)

    try:
        with open(_MEMORY_STORE_PATH, "w") as f:
            json.dump(_mapping_memory, f, indent=2)
    except Exception:
        pass


def _load_mapping_memory() -> None:
    """Load mapping memory (DB or JSON) at startup."""
    global _mapping_memory
    if _DB_MODE:
        try:
            from app.core.db_store import db_load_mapping_memory
            _mapping_memory = db_load_mapping_memory()
            logger.info("Loaded %d remembered mappings from Postgres", len(_mapping_memory))
            return
        except Exception as _e:
            logger.error("DB mapping_memory load failed, trying JSON: %s", _e)

    if not os.path.exists(_MEMORY_STORE_PATH):
        return
    try:
        with open(_MEMORY_STORE_PATH) as f:
            _mapping_memory = json.load(f)
        logger.info("Loaded %d remembered mappings from memory store", len(_mapping_memory))
    except Exception:
        pass


# ─────────────────────────────────────────────────────────────────────────────
# Audit event persistence
# ─────────────────────────────────────────────────────────────────────────────

def _flush_audit_events() -> None:
    """Write audit events (DB or JSON). Best-effort."""
    if _DB_MODE:
        try:
            from app.core.db_store import db_save_audit_events
            db_save_audit_events(_audit_events)
            return
        except Exception as _e:
            logger.error("DB audit flush failed, falling back to JSON: %s", _e)

    try:
        with open(_AUDIT_STORE_PATH, "w") as f:
            json.dump(_audit_events, f, separators=(",", ":"))
    except Exception:
        pass


def _load_audit_events() -> None:
    """Load persisted audit events (DB or JSON) at startup."""
    global _audit_events
    if _DB_MODE:
        try:
            from app.core.db_store import db_load_audit_events
            _audit_events = db_load_audit_events()
            logger.info("Loaded %d audit events from Postgres", len(_audit_events))
            return
        except Exception as _e:
            logger.error("DB audit load failed, trying JSON: %s", _e)

    if not os.path.exists(_AUDIT_STORE_PATH):
        return
    try:
        with open(_AUDIT_STORE_PATH) as f:
            _audit_events = json.load(f)
        logger.info("Loaded %d audit events from store", len(_audit_events))
    except Exception:
        _audit_events = []


# ─────────────────────────────────────────────────────────────────────────────
# Tenant loading
# ─────────────────────────────────────────────────────────────────────────────

def _migrate_tenant(t: Dict) -> Dict:
    """Backward-compat: convert old single-admin tenant format into users[] list.

    Old format:
        {slug, name, admin_email, admin_password, plan}
    New format:
        {slug, name, plan, users: [{email, password, role, active, ...}]}
    """
    t = dict(t)  # don't mutate caller
    if "users" not in t or not isinstance(t.get("users"), list):
        t["users"] = []
    if t.get("admin_email") and not any(
        (u.get("email") or "").lower() == (t.get("admin_email") or "").lower()
        for u in t["users"]
    ):
        t["users"].append({
            "email":        t.get("admin_email", ""),
            "password":     t.get("admin_password", ""),
            "role":         "admin",
            "active":       True,
            "invited_at":   None,
            "last_login":   None,
            "display_name": "Admin",
        })
    # Drop the legacy top-level keys after migration
    t.pop("admin_email", None)
    t.pop("admin_password", None)
    # Normalize each user record
    for u in t["users"]:
        u.setdefault("role", "admin")
        u.setdefault("active", True)
        u.setdefault("invited_at", None)
        u.setdefault("last_login", None)
        u.setdefault("display_name", (u.get("email", "") or "user").split("@")[0])
    return t


def _save_tenants() -> None:
    """Persist mutable tenant + user state to disk (best-effort)."""
    try:
        with open(_TENANTS_STORE_PATH, "w") as f:
            json.dump(list(_TENANTS.values()), f, indent=2)
    except Exception as e:
        logger.warning("Failed to save tenants: %s", e)


def _load_tenants() -> None:
    """Load tenants — disk first (mutable state), then env, then defaults.

    Each tenant is migrated to the new users[] format if it uses the old
    admin_email/admin_password top-level format. All loads merge into _TENANTS
    without overwriting an already-present slug (disk wins).
    """
    from app.config import _DEFAULT_TENANTS

    # 1) Disk-persisted mutable state (highest priority).
    if os.path.exists(_TENANTS_STORE_PATH):
        try:
            with open(_TENANTS_STORE_PATH) as f:
                stored = json.load(f)
            if isinstance(stored, list):
                for t in stored:
                    if not isinstance(t, dict) or not t.get("slug"):
                        continue
                    _TENANTS[t["slug"]] = _migrate_tenant(t)
            elif isinstance(stored, dict):
                # legacy dict-of-slug format
                for slug, t in stored.items():
                    if not isinstance(t, dict):
                        continue
                    t.setdefault("slug", slug)
                    _TENANTS[slug] = _migrate_tenant(t)
            logger.info("Loaded %d tenants from disk", len(_TENANTS))
        except Exception as e:
            logger.warning("Could not load tenants from %s: %s", _TENANTS_STORE_PATH, e)

    # 2) Env-supplied tenants (XREF_TENANTS JSON).
    raw = os.getenv("XREF_TENANTS", "")
    if raw:
        try:
            extra = json.loads(raw)
            for t in extra:
                if not isinstance(t, dict) or not t.get("slug"):
                    continue
                _TENANTS.setdefault(t["slug"], _migrate_tenant(t))
        except Exception:
            pass

    # 3) Default tenants (lowest priority).
    for t in _DEFAULT_TENANTS:
        _TENANTS.setdefault(t["slug"], _migrate_tenant(t))

    # Save once on first boot so the disk file gets created.
    if not os.path.exists(_TENANTS_STORE_PATH):
        _save_tenants()
