"""
app/core/logging_config.py

Structured JSON logging for production observability.
Each log line is a valid JSON object — parseable by Datadog, Splunk, CloudWatch.

Fields on every log line:
  ts          — ISO8601 timestamp
  level       — DEBUG/INFO/WARNING/ERROR/CRITICAL
  logger      — logger name (e.g. "xref_agent.pipeline")
  message     — the log message
  request_id  — UUID from X-Request-ID header (if in request context)
  tenant      — tenant slug (if in request context)
  session_id  — session ID (if available)
  env         — DM_ENV value (dev/staging/production)
  service     — "xref-datamapper"
  version     — app version string

NOTE: This module deliberately imports ONLY stdlib (`logging`, `os`, `json`,
`datetime`). It must remain free of any `app.*` imports so it can be called
before the rest of the app is loaded (CLI scripts, tests, lifespan).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone


class JSONFormatter(logging.Formatter):
    """Serialize every LogRecord as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        log_obj = {
            "ts":      datetime.now(timezone.utc).isoformat(),
            "level":   record.levelname,
            "logger":  record.name,
            "message": record.getMessage(),
            "service": "xref-datamapper",
            "env":     os.getenv("DM_ENV", "dev"),
            "version": "2.0.0",
        }
        # Include exception info if present
        if record.exc_info:
            log_obj["exception"] = self.formatException(record.exc_info)
        # Include any extra fields attached to the record via logger.X(..., extra={...})
        for key in (
            "request_id", "tenant", "session_id",
            "duration_ms", "status_code", "path",
            "method", "error",
        ):
            if hasattr(record, key):
                log_obj[key] = getattr(record, key)
        return json.dumps(log_obj, default=str)


def setup_logging(json_mode: bool | None = None) -> None:
    """Configure the root logger.

    Args:
        json_mode: If True, emit JSON. If False, emit human-readable text.
                   If None, default to True only when DM_ENV=production.
    """
    if json_mode is None:
        json_mode = os.getenv("DM_ENV", "dev") == "production"

    root = logging.getLogger()
    # Clear any existing handlers (e.g. from a previous setup_logging call
    # or from logging.basicConfig in stale imports).
    root.handlers.clear()

    handler = logging.StreamHandler()
    if json_mode:
        handler.setFormatter(JSONFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    root.setLevel(getattr(logging, level_name, logging.INFO))
    root.addHandler(handler)

    # Silence noisy third-party loggers
    for noisy in ("httpx", "httpcore", "openai", "anthropic", "google", "urllib3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
