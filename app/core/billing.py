"""
app/core/billing.py — plan catalog + derived usage metering (Phase 1).

Plans are code-defined; per-tenant usage is computed on read from data the
platform already captures (sessions, pipeline.started audit events, per-session
token/cost usage, active seats) — no new store, always consistent. A Stripe
layer can later become the source of truth for `plan`/`status` via webhook.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Dict, List, Optional

UNLIMITED = None  # a limit of None means unlimited

# ── Plan catalog ────────────────────────────────────────────────────────────
PLAN_CATALOG: Dict[str, Dict] = {
    "trial": {
        "label": "Trial",
        "limits": {"seats": 2, "sessions_per_month": 5, "runs_per_month": 10, "monthly_tokens": 200_000},
        "features": ["Core mapping", "Lineage & readiness"],
    },
    "standard": {
        "label": "Standard",
        "limits": {"seats": 10, "sessions_per_month": 100, "runs_per_month": 300, "monthly_tokens": 5_000_000},
        "features": ["Core mapping", "Lineage & readiness", "Report & certificate", "Secrets vault"],
    },
    "enterprise": {
        "label": "Enterprise",
        "limits": {"seats": UNLIMITED, "sessions_per_month": UNLIMITED,
                   "runs_per_month": UNLIMITED, "monthly_tokens": UNLIMITED},
        "features": ["Everything in Standard", "SSO / OIDC", "Audit (SIEM) export", "Priority support"],
    },
    "custom": {
        "label": "Custom",
        "limits": {"seats": UNLIMITED, "sessions_per_month": UNLIMITED,
                   "runs_per_month": UNLIMITED, "monthly_tokens": UNLIMITED},
        "features": ["Per-contract limits"],
    },
}

_LIMIT_KEYS = ("seats", "sessions_per_month", "runs_per_month", "monthly_tokens")


def plan_of(tenant: Dict) -> str:
    code = (tenant.get("plan") or "standard").lower()
    return code if code in PLAN_CATALOG else "standard"


def effective_limits(tenant: Dict) -> Dict:
    """Catalog limits for the tenant's plan, with per-tenant overrides applied."""
    code = plan_of(tenant)
    limits = dict(PLAN_CATALOG[code]["limits"])
    overrides = (tenant.get("billing", {}) or {}).get("overrides", {}) or {}
    for k, v in overrides.items():
        if k in _LIMIT_KEYS:
            limits[k] = v
    return limits


def current_period(now: Optional[datetime] = None) -> Dict:
    """Current calendar-month window (UTC)."""
    now = now or datetime.now(timezone.utc)
    start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    renews = (start.replace(year=start.year + 1, month=1) if start.month == 12
              else start.replace(month=start.month + 1))
    return {"start": start.isoformat(), "renews_at": renews.isoformat(),
            "_start_dt": start, "_end_dt": renews}


def _in_period(ts: str, start: datetime, end: datetime) -> bool:
    if not ts:
        return False
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return start <= dt < end
    except Exception:
        return False


def _pct(used: int, limit) -> Optional[int]:
    if limit in (None, 0):
        return None
    return min(100, round(100 * used / limit))


def compute_billing(tenant: Dict, sessions: Dict, audit_events: List[Dict],
                    now: Optional[datetime] = None) -> Dict:
    """Build the billing payload for a tenant from existing usage data."""
    code = plan_of(tenant)
    slug = tenant.get("slug", "")
    limits = effective_limits(tenant)
    period = current_period(now)
    start, end = period["_start_dt"], period["_end_dt"]

    # Seats = active users in the tenant.
    seats = sum(1 for u in (tenant.get("users", []) or []) if u.get("active") is not False)

    # Sessions + token/cost usage from this tenant's sessions created in-period.
    sess_used = 0
    tokens = 0
    cost = 0.0
    for s in (sessions or {}).values():
        if s.get("tenant") != slug:
            continue
        if _in_period(s.get("created_at", ""), start, end):
            sess_used += 1
            u = s.get("usage", {}) or {}
            tokens += int(u.get("input_tokens", 0)) + int(u.get("output_tokens", 0))
            cost += float(u.get("cost_usd", 0.0))

    # Pipeline runs from audit events.
    runs_used = sum(1 for e in (audit_events or [])
                    if e.get("event") == "pipeline.started"
                    and e.get("tenant") == slug
                    and _in_period(e.get("ts", ""), start, end))

    usage = {"seats": seats, "sessions": sess_used, "runs": runs_used,
             "tokens": tokens, "cost_usd": round(cost, 4)}
    percent = {
        "seats": _pct(seats, limits["seats"]),
        "sessions": _pct(sess_used, limits["sessions_per_month"]),
        "runs": _pct(runs_used, limits["runs_per_month"]),
        "tokens": _pct(tokens, limits["monthly_tokens"]),
    }
    return {
        "plan": code,
        "plan_label": PLAN_CATALOG[code]["label"],
        "status": (tenant.get("billing", {}) or {}).get("status", "active"),
        "features": PLAN_CATALOG[code]["features"],
        "period": {"start": period["start"], "renews_at": period["renews_at"]},
        "limits": limits,
        "usage": usage,
        "percent_used": percent,
    }


def public_catalog() -> Dict:
    """Plan catalog for the upgrade UI (limits + labels + features)."""
    return {code: {"label": p["label"], "limits": p["limits"], "features": p["features"]}
            for code, p in PLAN_CATALOG.items()}
