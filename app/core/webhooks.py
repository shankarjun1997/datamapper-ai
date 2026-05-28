"""
Webhook fanout — async HTTP POST to configured URLs on pipeline events.

Supported events: pipeline.completed, pipeline.failed, gate2.approved
Configure per-session via api_config.webhook_url (single URL) or
api_config.webhook_urls (list of URLs).

Payload shape:
{
  "event": "pipeline.completed",
  "session_id": "abc123",
  "tenant": "infinite",
  "ts": "2026-05-27T10:00:00Z",
  "data": { ...event-specific data... }
}
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

logger = logging.getLogger("xref_webhooks")

SUPPORTED_EVENTS = {
    "pipeline.completed",
    "pipeline.failed",
    "gate2.approved",
    "pipeline.started",
    "webhook.test",
}


def _collect_urls(session: Dict[str, Any]) -> List[str]:
    """Pull webhook URLs from api_config — supports single + list — dedup preserving order."""
    cfg = session.get("api_config") or {}
    urls: List[str] = []
    single = cfg.get("webhook_url")
    if single and isinstance(single, str):
        urls.append(single.strip())
    multi = cfg.get("webhook_urls")
    if multi and isinstance(multi, list):
        for u in multi:
            if u and isinstance(u, str):
                urls.append(u.strip())
    # dedupe, preserve order, drop empties
    seen = set()
    out: List[str] = []
    for u in urls:
        if u and u not in seen:
            seen.add(u)
            out.append(u)
    return out


async def fire_webhook(
    event: str,
    session: Dict[str, Any],
    data: Optional[Dict[str, Any]] = None,
    timeout: float = 10.0,
) -> None:
    """Send webhook POST for an event. Non-blocking — errors are logged, never raised."""
    try:
        urls = _collect_urls(session)
        if not urls:
            return

        if event not in SUPPORTED_EVENTS:
            logger.debug("fire_webhook: unsupported event '%s' (firing anyway)", event)

        try:
            import httpx  # type: ignore
        except Exception as imp_err:
            logger.error("fire_webhook: httpx not available — skipping (%s)", imp_err)
            return

        session_id = (
            session.get("id")
            or session.get("session_id")
            or session.get("sid")
            or ""
        )
        tenant = session.get("tenant") or "unknown"

        payload = {
            "event":      event,
            "session_id": session_id,
            "tenant":     tenant,
            "ts":         datetime.now(timezone.utc).isoformat(),
            "data":       data or {},
        }

        headers = {
            "User-Agent":   "xREF-Agent/2.0",
            "X-xREF-Event": event,
            "Content-Type": "application/json",
        }

        async with httpx.AsyncClient(timeout=timeout) as client:
            async def _post(url: str) -> None:
                try:
                    r = await client.post(url, json=payload, headers=headers)
                    if r.status_code >= 400:
                        logger.warning(
                            "Webhook POST to %s returned %d: %s",
                            url, r.status_code, (r.text or "")[:200],
                        )
                    else:
                        logger.info("Webhook %s -> %s (%d)", event, url, r.status_code)
                except Exception as e:
                    logger.warning("Webhook POST to %s failed: %s", url, e)

            await asyncio.gather(*[_post(u) for u in urls], return_exceptions=True)
    except Exception as e:
        # Final safety net — webhooks must never bring down a pipeline.
        logger.warning("fire_webhook outer error (suppressed): %s", e)
