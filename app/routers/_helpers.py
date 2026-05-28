"""
app/routers/_helpers.py — shared utilities used by multiple routers
"""
from __future__ import annotations

import time
from collections import defaultdict
from pathlib import Path
from typing import Dict, List

from fastapi import HTTPException, Request

from app.config import (
    _ALLOWED_UPLOAD_EXTS,
    _MAX_UPLOAD_BYTES,
    _RATE_LIMIT,
    _RATE_WINDOW,
)

# ── Rate limiter ──────────────────────────────────────────────────────────────
_rate_store: Dict[str, List[float]] = defaultdict(list)


def _check_rate_limit(client_ip: str) -> bool:
    """Returns True if request is allowed, False if rate-limited."""
    now = time.time()
    window_start = now - _RATE_WINDOW
    hits = _rate_store[client_ip]
    _rate_store[client_ip] = [t for t in hits if t > window_start]
    if len(_rate_store[client_ip]) >= _RATE_LIMIT:
        return False
    _rate_store[client_ip].append(now)
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
