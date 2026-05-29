"""
app/routers/_helpers.py — shared utilities used by multiple routers
"""
from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List, Optional

from fastapi import HTTPException, Request

from app.config import (
    _ALLOWED_UPLOAD_EXTS,
    _MAX_UPLOAD_BYTES,
    _RATE_LIMIT,
    _RATE_WINDOW,
)

logger = logging.getLogger("xref_agent")

# ── Rate limiter ──────────────────────────────────────────────────────────────
# Prefer Redis (shared across workers/instances) when REDIS_URL is set; fall back
# to a per-process in-memory window otherwise. The in-memory path is fine for a
# single instance but does NOT coordinate across replicas.
_rate_store: Dict[str, List[float]] = defaultdict(list)

_redis_client = None
_redis_ready = False


def _get_redis():
    global _redis_client, _redis_ready
    if _redis_ready:
        return _redis_client
    _redis_ready = True  # only attempt once
    url = os.getenv("REDIS_URL", "")
    if not url:
        return None
    try:
        import redis  # type: ignore
        _redis_client = redis.from_url(url, socket_timeout=0.25, socket_connect_timeout=0.25)
        _redis_client.ping()
        logger.info("Rate limiter using Redis")
    except Exception as e:  # pragma: no cover - depends on env
        logger.warning("Redis unavailable for rate limiting, using in-memory: %s", e)
        _redis_client = None
    return _redis_client


def _check_rate_limit(key: str, limit: Optional[int] = None, window: Optional[int] = None) -> bool:
    """Return True if the request is allowed, False if rate-limited.

    ``key`` is typically the client IP (or ip+scope for stricter buckets).
    Uses a fixed-window counter in Redis when available, else an in-memory
    sliding window.
    """
    limit = limit or _RATE_LIMIT
    window = window or _RATE_WINDOW
    now = time.time()

    client = _get_redis()
    if client is not None:
        try:
            bucket = int(now // window)
            rkey = f"rl:{key}:{bucket}"
            count = client.incr(rkey)
            if count == 1:
                client.expire(rkey, window)
            return count <= limit
        except Exception as e:  # pragma: no cover
            logger.warning("Redis rate-limit error, falling back to memory: %s", e)

    window_start = now - window
    hits = [t for t in _rate_store[key] if t > window_start]
    if len(hits) >= limit:
        _rate_store[key] = hits
        return False
    hits.append(now)
    _rate_store[key] = hits
    return True


def _get_client_ip(request: Request) -> str:
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _validate_upload(filename: str, content: bytes) -> str:
    """Raise HTTPException for disallowed file type or oversized content."""
    safe_name = Path(filename).name
    ext = Path(safe_name).suffix.lower()
    if ext not in _ALLOWED_UPLOAD_EXTS:
        raise HTTPException(415, f"File type '{ext}' not allowed. Accepted: {', '.join(_ALLOWED_UPLOAD_EXTS)}")
    if len(content) > _MAX_UPLOAD_BYTES:
        raise HTTPException(413, f"File too large ({len(content)//1024}KB). Max: {_MAX_UPLOAD_BYTES//1024//1024}MB")
    return safe_name
