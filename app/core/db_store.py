"""
app/core/db_store.py — Postgres-backed implementations of the
JSON-file store functions in ``app/state.py``.

All operations are synchronous; callers in async paths should wrap with
``asyncio.to_thread``. Routines are idempotent and safe to call when the
DB is unreachable — they raise instead of silently no-op'ing so the
caller can decide whether to fall through to the JSON store.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from sqlalchemy import and_, delete, select

from app.config import logger
from app.core.db import db_available, get_db_session
from app.models.platform import DBAuditEvent, DBMappingMemory, DBSession


# Columns that are first-class on DBSession (everything else goes into `extra`)
_SESSION_MODELLED = {
    "id", "tenant", "name", "status", "stage", "created_at", "filename",
    "instructions", "mappings", "stats", "bq_config", "api_config", "usage",
    "src_columns", "tgt_columns", "table_mappings", "jira_context",
    "mapping_versions",
}


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────
def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _split_session(s: Dict[str, Any]) -> Dict[str, Any]:
    """Split a session dict into (modelled columns, extra blob)."""
    modelled: Dict[str, Any] = {}
    extra: Dict[str, Any] = {}
    for k, v in s.items():
        if k in _SESSION_MODELLED:
            modelled[k] = v
        else:
            extra[k] = v
    modelled["extra"] = extra
    return modelled


def _session_row_to_dict(row: DBSession) -> Dict[str, Any]:
    out: Dict[str, Any] = {
        "id":            row.id,
        "tenant":        row.tenant or "default",
        "name":          row.name or "",
        "status":        row.status or "new",
        "stage":         row.stage or "idle",
        "created_at":    row.created_at or "",
        "filename":      row.filename or "",
        "instructions":  row.instructions or "",
        "mappings":         row.mappings or [],
        "stats":            row.stats or {},
        "bq_config":        row.bq_config or {},
        "api_config":       row.api_config or {},
        "usage":            row.usage or {},
        "src_columns":      row.src_columns or [],
        "tgt_columns":      row.tgt_columns or [],
        "table_mappings":   row.table_mappings or [],
        "jira_context":     row.jira_context or {},
        "mapping_versions": row.mapping_versions or [],
    }
    extra = row.extra or {}
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k not in out:
                out[k] = v
    # Re-seed non-persisted runtime keys
    out.setdefault("running", False)
    out.setdefault("log", [])
    return out


# ─────────────────────────────────────────────────────────────────────────────
# Sessions
# ─────────────────────────────────────────────────────────────────────────────
def db_save_session(session_dict: Dict[str, Any]) -> None:
    """Upsert a single session row."""
    sid = session_dict.get("id")
    if not sid:
        return
    parts = _split_session(session_dict)
    with get_db_session() as s:
        row = s.get(DBSession, sid)
        if row is None:
            row = DBSession(id=sid)
            s.add(row)
        for col, val in parts.items():
            setattr(row, col, val)
        # Ensure tenant default
        if not row.tenant:
            row.tenant = session_dict.get("tenant") or "default"
        s.commit()


def db_save_all_sessions(sessions: Dict[str, Dict[str, Any]]) -> None:
    """Upsert every session in the in-memory store (used by _save_sessions)."""
    if not sessions:
        return
    with get_db_session() as s:
        for sid, sess in sessions.items():
            if not sid:
                continue
            parts = _split_session(sess)
            parts["id"] = sid
            row = s.get(DBSession, sid)
            if row is None:
                row = DBSession(id=sid)
                s.add(row)
            for col, val in parts.items():
                setattr(row, col, val)
            if not row.tenant:
                row.tenant = sess.get("tenant") or "default"
        s.commit()


def db_load_all_sessions() -> Dict[str, Dict[str, Any]]:
    """Return ``{sid: session_dict}`` for every persisted session."""
    out: Dict[str, Dict[str, Any]] = {}
    with get_db_session() as s:
        for row in s.execute(select(DBSession)).scalars():
            out[row.id] = _session_row_to_dict(row)
    return out


def db_delete_session(sid: str) -> None:
    if not sid:
        return
    with get_db_session() as s:
        s.execute(delete(DBSession).where(DBSession.id == sid))
        s.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Audit events
# ─────────────────────────────────────────────────────────────────────────────
def db_write_audit_event(evt: Dict[str, Any]) -> None:
    """Insert a single audit event. Idempotent on `id`."""
    with get_db_session() as s:
        row = DBAuditEvent(
            id         = evt.get("id") or str(uuid.uuid4()),
            ts         = evt.get("ts") or _now(),
            event      = evt.get("event") or "unknown",
            tenant     = evt.get("tenant") or "unknown",
            email      = evt.get("email") or "anonymous",
            session_id = evt.get("session_id"),
            ip         = evt.get("ip") or "unknown",
            meta       = evt.get("meta") or {},
        )
        # Upsert: insert, ignore on duplicate primary key
        existing = s.get(DBAuditEvent, row.id)
        if existing is None:
            s.add(row)
            s.commit()


def db_load_audit_events(limit: int = 10000) -> List[Dict[str, Any]]:
    """Return audit events ordered by ts asc, capped at ``limit``."""
    out: List[Dict[str, Any]] = []
    with get_db_session() as s:
        stmt = select(DBAuditEvent).order_by(DBAuditEvent.ts.asc()).limit(limit)
        for row in s.execute(stmt).scalars():
            out.append({
                "id":         row.id,
                "ts":         row.ts,
                "event":      row.event,
                "tenant":     row.tenant,
                "email":      row.email,
                "session_id": row.session_id,
                "ip":         row.ip,
                "meta":       row.meta or {},
            })
    return out


def db_query_audit_events(
    tenant: Optional[str] = None,
    event: Optional[str] = None,
    email: Optional[str] = None,
    since: Optional[str] = None,
    until: Optional[str] = None,
    limit: int = 500,
) -> List[Dict[str, Any]]:
    """Filtered query — used by the admin audit dashboard."""
    conds = []
    if tenant:
        conds.append(DBAuditEvent.tenant == tenant)
    if event:
        conds.append(DBAuditEvent.event == event)
    if email:
        conds.append(DBAuditEvent.email == email)
    if since:
        conds.append(DBAuditEvent.ts >= since)
    if until:
        conds.append(DBAuditEvent.ts <= until)

    with get_db_session() as s:
        stmt = select(DBAuditEvent).order_by(DBAuditEvent.ts.desc()).limit(limit)
        if conds:
            stmt = stmt.where(and_(*conds))
        out: List[Dict[str, Any]] = []
        for row in s.execute(stmt).scalars():
            out.append({
                "id":         row.id,
                "ts":         row.ts,
                "event":      row.event,
                "tenant":     row.tenant,
                "email":      row.email,
                "session_id": row.session_id,
                "ip":         row.ip,
                "meta":       row.meta or {},
            })
        return out


def db_save_audit_events(events: List[Dict[str, Any]]) -> None:
    """Bulk upsert the entire in-memory audit list. Used by ``_flush_audit_events``."""
    if not events:
        return
    with get_db_session() as s:
        # Pull existing ids in one pass to avoid N selects
        ids = [e.get("id") for e in events if e.get("id")]
        existing_ids: set[str] = set()
        if ids:
            for r in s.execute(select(DBAuditEvent.id).where(DBAuditEvent.id.in_(ids))).all():
                existing_ids.add(r[0])
        for evt in events:
            eid = evt.get("id") or str(uuid.uuid4())
            if eid in existing_ids:
                continue
            s.add(DBAuditEvent(
                id         = eid,
                ts         = evt.get("ts") or _now(),
                event      = evt.get("event") or "unknown",
                tenant     = evt.get("tenant") or "unknown",
                email      = evt.get("email") or "anonymous",
                session_id = evt.get("session_id"),
                ip         = evt.get("ip") or "unknown",
                meta       = evt.get("meta") or {},
            ))
        s.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Mapping memory
# ─────────────────────────────────────────────────────────────────────────────
def _memory_row_to_dict(row: DBMappingMemory) -> Dict[str, Any]:
    return {
        "tgt_table":        row.tgt_table or "",
        "tgt_column":       row.tgt_column or "",
        "mapping_type":     row.mapping_type or "Direct",
        "mapping_relation": row.mapping_relation or "1:1",
        "business_logic":   row.business_logic or "",
        "confidence":       float(row.confidence or 0.5),
        "uses":             int(row.uses or 1),
        "last_updated":     row.last_updated or "",
        "user_override":    bool(row.user_override),
    }


def db_save_mapping_memory(memory_dict: Dict[str, Dict[str, Any]]) -> None:
    """Bulk upsert the entire mapping_memory dict."""
    if memory_dict is None:
        return
    with get_db_session() as s:
        for src_field, entry in memory_dict.items():
            if not src_field:
                continue
            row = s.get(DBMappingMemory, src_field)
            if row is None:
                row = DBMappingMemory(src_field=src_field)
                s.add(row)
            row.tgt_table        = entry.get("tgt_table", "")
            row.tgt_column       = entry.get("tgt_column", "")
            row.mapping_type     = entry.get("mapping_type", "Direct")
            row.mapping_relation = entry.get("mapping_relation", "1:1")
            row.business_logic   = entry.get("business_logic", "")
            row.confidence       = float(entry.get("confidence", 0.5))
            row.uses             = int(entry.get("uses", 1))
            row.last_updated     = entry.get("last_updated", "")
            row.user_override    = bool(entry.get("user_override", False))
        s.commit()


def db_load_mapping_memory() -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    with get_db_session() as s:
        for row in s.execute(select(DBMappingMemory)).scalars():
            out[row.src_field] = _memory_row_to_dict(row)
    return out


def db_upsert_memory_entry(src_field: str, entry: Dict[str, Any]) -> None:
    if not src_field:
        return
    with get_db_session() as s:
        row = s.get(DBMappingMemory, src_field)
        if row is None:
            row = DBMappingMemory(src_field=src_field)
            s.add(row)
        row.tgt_table        = entry.get("tgt_table", "")
        row.tgt_column       = entry.get("tgt_column", "")
        row.mapping_type     = entry.get("mapping_type", "Direct")
        row.mapping_relation = entry.get("mapping_relation", "1:1")
        row.business_logic   = entry.get("business_logic", "")
        row.confidence       = float(entry.get("confidence", 0.5))
        row.uses             = int(entry.get("uses", 1))
        row.last_updated     = entry.get("last_updated", "")
        row.user_override    = bool(entry.get("user_override", False))
        s.commit()


def db_delete_memory_entry(src_field: str) -> None:
    if not src_field:
        return
    with get_db_session() as s:
        s.execute(delete(DBMappingMemory).where(DBMappingMemory.src_field == src_field))
        s.commit()


# ─────────────────────────────────────────────────────────────────────────────
# Schema bootstrap (Base.metadata.create_all wrapper)
# ─────────────────────────────────────────────────────────────────────────────
def ensure_schema() -> None:
    """Create all tables if missing — safe to call on every startup.

    Prefer ``alembic upgrade head`` in production; this is the fallback for
    fresh local dev when alembic hasn't been run.
    """
    from app.core.db import engine
    from app.models.platform import Base
    if engine is None:
        return
    try:
        Base.metadata.create_all(engine)
    except Exception as _e:
        logger.error("ensure_schema failed: %s", _e)
        raise


# ─────────────────────────────────────────────────────────────────────────────
# One-time JSON → DB migration
# ─────────────────────────────────────────────────────────────────────────────
def migrate_json_to_db(
    sessions: Dict[str, Dict[str, Any]],
    audit_events: List[Dict[str, Any]],
    mapping_memory: Dict[str, Dict[str, Any]],
) -> Dict[str, int]:
    """Idempotently copy JSON-file state into Postgres.

    Each entity is upserted by primary key, so re-running is safe.
    Returns counts inserted/updated per entity.
    """
    counts = {"sessions": 0, "audit_events": 0, "mapping_memory": 0}
    if not db_available():
        return counts

    try:
        if sessions:
            db_save_all_sessions(sessions)
            counts["sessions"] = len(sessions)
    except Exception as _e:
        logger.error("Session migration failed: %s", _e)

    try:
        if audit_events:
            db_save_audit_events(audit_events)
            counts["audit_events"] = len(audit_events)
    except Exception as _e:
        logger.error("Audit event migration failed: %s", _e)

    try:
        if mapping_memory:
            db_save_mapping_memory(mapping_memory)
            counts["mapping_memory"] = len(mapping_memory)
    except Exception as _e:
        logger.error("Mapping memory migration failed: %s", _e)

    return counts
