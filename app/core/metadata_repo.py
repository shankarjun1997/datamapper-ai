"""
app/core/metadata_repo.py — Layer 3: canonical, versioned metadata repository.

A single, system-wide catalog of every discovered data asset (systems →
databases → schemas → tables → columns → relationships), tenant-scoped, with
full version history. Lineage, impact, and migration-readiness all build on this
canonical model rather than per-session blobs.

Storage follows the app's existing hybrid pattern: in-memory dicts persisted to
a JSON file (Postgres-backable later). Object identity is the type-prefixed FQN
so re-crawling updates the same object and creates a new version only when the
content actually changes (content-hash diff).
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Dict, List, Optional

logger = logging.getLogger("xref_agent")

OBJECT_TYPES = {"system", "database", "schema", "table", "column", "relationship"}

# tenant -> { object_id -> current object dict }
_objects: Dict[str, Dict[str, dict]] = {}
# tenant -> { object_id -> [ {version, attributes, hash, ts, by} ] }
_history: Dict[str, Dict[str, list]] = {}
_lock = threading.RLock()

_STORE_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(__file__))), "runtime", "metadata.json"
)
_loaded = False


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _hash(attributes: dict) -> str:
    return hashlib.sha256(json.dumps(attributes, sort_keys=True, default=str).encode()).hexdigest()[:16]


def _oid(otype: str, fqn: str) -> str:
    return f"{otype}:{fqn}"


def load() -> None:
    global _loaded
    with _lock:
        if _loaded:
            return
        _loaded = True
        if not os.path.exists(_STORE_PATH):
            return
        try:
            with open(_STORE_PATH) as f:
                data = json.load(f) or {}
            _objects.update(data.get("objects", {}))
            _history.update(data.get("history", {}))
            logger.info("Loaded metadata repo: %d tenants", len(_objects))
        except Exception as e:  # pragma: no cover
            logger.warning("Could not load metadata repo: %s", e)


def _save() -> None:
    try:
        os.makedirs(os.path.dirname(_STORE_PATH), exist_ok=True)
        with open(_STORE_PATH, "w") as f:
            json.dump({"objects": _objects, "history": _history}, f)
    except Exception as e:  # pragma: no cover
        logger.warning("Could not persist metadata repo: %s", e)


def upsert_object(tenant: str, otype: str, name: str, fqn: str,
                  attributes: Optional[dict] = None, parent_fqn: Optional[str] = None,
                  updated_by: str = "") -> dict:
    """Create or version an object. Returns the current object.

    A new version is recorded only when the content hash changes."""
    if otype not in OBJECT_TYPES:
        raise ValueError(f"Unknown object type: {otype}")
    attributes = attributes or {}
    with _lock:
        load()
        tobj = _objects.setdefault(tenant, {})
        thist = _history.setdefault(tenant, {})
        oid = _oid(otype, fqn)
        h = _hash(attributes)
        now = _now()
        existing = tobj.get(oid)
        if existing is None:
            obj = {
                "id": oid, "tenant": tenant, "type": otype, "name": name, "fqn": fqn,
                "parent": (_oid(_parent_type(otype), parent_fqn) if parent_fqn else None),
                "attributes": attributes, "hash": h, "version": 1,
                "created_at": now, "updated_at": now, "updated_by": updated_by,
            }
            tobj[oid] = obj
            thist[oid] = [{"version": 1, "attributes": attributes, "hash": h, "ts": now, "by": updated_by}]
        elif existing["hash"] != h:
            existing["version"] += 1
            existing["attributes"] = attributes
            existing["hash"] = h
            existing["name"] = name
            existing["updated_at"] = now
            existing["updated_by"] = updated_by
            thist[oid].append({"version": existing["version"], "attributes": attributes,
                               "hash": h, "ts": now, "by": updated_by})
        # else unchanged — no new version
        _save()
        return tobj[oid]


def _parent_type(otype: str) -> str:
    return {"column": "table", "table": "schema", "schema": "database",
            "database": "system", "relationship": "system"}.get(otype, "system")


def ingest_schema(tenant: str, system_name: str, platform: str,
                  schema_data: dict, updated_by: str = "") -> dict:
    """Ingest a crawled schema ({'tables':[{'name','columns':[...]}]}) into the
    canonical repo as system → table → column objects. Returns counts."""
    counts = {"systems": 0, "tables": 0, "columns": 0, "new_versions": 0}
    sys_fqn = system_name
    before = _version_total(tenant)
    upsert_object(tenant, "system", system_name, sys_fqn,
                  {"platform": (platform or "generic")}, updated_by=updated_by)
    counts["systems"] = 1
    for t in (schema_data or {}).get("tables", []) or []:
        tname = t.get("name", "")
        if not tname:
            continue
        tfqn = f"{sys_fqn}.{tname}"
        cols = t.get("columns", []) or []
        upsert_object(tenant, "table", tname, tfqn,
                      {"column_count": len(cols)}, parent_fqn=sys_fqn, updated_by=updated_by)
        counts["tables"] += 1
        for c in cols:
            cname = c.get("name", "")
            if not cname:
                continue
            upsert_object(tenant, "column", cname, f"{tfqn}.{cname}", {
                "data_type": c.get("type", ""),
                "nullable": c.get("nullable", True),
            }, parent_fqn=tfqn, updated_by=updated_by)
            counts["columns"] += 1
    counts["new_versions"] = _version_total(tenant) - before
    return counts


def _version_total(tenant: str) -> int:
    return sum(len(v) for v in _history.get(tenant, {}).values())


def list_objects(tenant: str, otype: Optional[str] = None, parent: Optional[str] = None,
                 q: Optional[str] = None, limit: int = 100, offset: int = 0) -> dict:
    """Paginated listing of canonical objects for a tenant."""
    with _lock:
        load()
        items = list(_objects.get(tenant, {}).values())
    if otype:
        items = [o for o in items if o["type"] == otype]
    if parent:
        items = [o for o in items if o.get("parent") == parent]
    if q:
        ql = q.lower()
        items = [o for o in items if ql in o["fqn"].lower()]
    items.sort(key=lambda o: (o["type"], o["fqn"]))
    total = len(items)
    limit = max(1, min(int(limit or 100), 1000))
    offset = max(0, int(offset or 0))
    return {"total": total, "limit": limit, "offset": offset,
            "items": items[offset:offset + limit]}


def get_object(tenant: str, oid: str) -> Optional[dict]:
    with _lock:
        load()
        return _objects.get(tenant, {}).get(oid)


def get_history(tenant: str, oid: str) -> List[dict]:
    with _lock:
        load()
        return list(_history.get(tenant, {}).get(oid, []))


def stats(tenant: str) -> dict:
    with _lock:
        load()
        objs = _objects.get(tenant, {})
    by_type: Dict[str, int] = {}
    for o in objs.values():
        by_type[o["type"]] = by_type.get(o["type"], 0) + 1
    return {"total_objects": len(objs), "by_type": by_type,
            "total_versions": _version_total(tenant)}
