"""
app/core/email.py — provider-agnostic transactional email.

Auto-detects the provider from env:
  - RESEND_API_KEY   → Resend  (https://resend.com)
  - SENDGRID_API_KEY → SendGrid

Common env:
  - EMAIL_FROM   sender address, e.g. "xREF <noreply@yourdomain.com>"
  - DM_PUBLIC_URL  base URL used to build links in emails (e.g. https://app.client.com)

If no provider key is set, send_email() logs and returns False (no-op) so the
app keeps working in dev without email configured.
"""
from __future__ import annotations

import logging
import os
from typing import Optional

import httpx

logger = logging.getLogger("xref_agent")


def provider() -> Optional[str]:
    if os.getenv("RESEND_API_KEY"):
        return "resend"
    if os.getenv("SENDGRID_API_KEY"):
        return "sendgrid"
    return None


def _from_address() -> str:
    return os.getenv("EMAIL_FROM", "xREF <noreply@example.com>")


def public_base_url() -> str:
    return os.getenv("DM_PUBLIC_URL", "").rstrip("/")


async def send_email(to: str, subject: str, html: str) -> bool:
    """Send an email via the configured provider. Returns True on success."""
    prov = provider()
    if not prov:
        logger.warning("Email not configured (no RESEND_API_KEY/SENDGRID_API_KEY) — skipping send to %s", to)
        return False
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if prov == "resend":
                r = await client.post(
                    "https://api.resend.com/emails",
                    headers={"Authorization": f"Bearer {os.getenv('RESEND_API_KEY')}"},
                    json={"from": _from_address(), "to": [to], "subject": subject, "html": html},
                )
            else:  # sendgrid
                r = await client.post(
                    "https://api.sendgrid.com/v3/mail/send",
                    headers={"Authorization": f"Bearer {os.getenv('SENDGRID_API_KEY')}"},
                    json={
                        "personalizations": [{"to": [{"email": to}]}],
                        "from": {"email": _from_address()},
                        "subject": subject,
                        "content": [{"type": "text/html", "value": html}],
                    },
                )
            if r.status_code >= 400:
                logger.error("Email send failed (%s): %s %s", prov, r.status_code, r.text[:200])
                return False
            logger.info("Email sent to %s via %s", to, prov)
            return True
    except Exception as e:  # pragma: no cover - network
        logger.error("Email send error (%s): %s", prov, e)
        return False
