"""Tests for plan catalog + derived usage metering (billing Phase 1)."""
from datetime import datetime, timezone

from app.core import billing as b

NOW = datetime(2026, 5, 15, tzinfo=timezone.utc)


def test_catalog_and_effective_limits():
    assert {"trial", "standard", "enterprise", "custom"} <= set(b.PLAN_CATALOG)
    assert b.effective_limits({"plan": "trial"})["sessions_per_month"] == 5
    assert b.effective_limits({"plan": "enterprise"})["seats"] is None  # unlimited
    # per-tenant override wins
    t = {"plan": "trial", "billing": {"overrides": {"sessions_per_month": 50}}}
    assert b.effective_limits(t)["sessions_per_month"] == 50
    # unknown plan falls back to standard
    assert b.plan_of({"plan": "bogus"}) == "standard"


def test_compute_billing_meters_in_period_only():
    tenant = {"slug": "acme", "plan": "trial",
              "users": [{"active": True}, {"active": True}, {"active": False}]}
    sessions = {
        "s1": {"tenant": "acme", "created_at": "2026-05-02T00:00:00+00:00",
               "usage": {"input_tokens": 1000, "output_tokens": 500, "cost_usd": 0.02}},
        "s2": {"tenant": "acme", "created_at": "2026-04-30T00:00:00+00:00", "usage": {}},  # prior month
        "s3": {"tenant": "other", "created_at": "2026-05-03T00:00:00+00:00", "usage": {}},  # other tenant
    }
    audit = [
        {"event": "pipeline.started", "tenant": "acme", "ts": "2026-05-04T00:00:00+00:00"},
        {"event": "pipeline.started", "tenant": "acme", "ts": "2026-04-01T00:00:00+00:00"},  # prior month
        {"event": "session.created", "tenant": "acme", "ts": "2026-05-04T00:00:00+00:00"},
    ]
    out = b.compute_billing(tenant, sessions, audit, now=NOW)
    assert out["plan"] == "trial"
    assert out["usage"]["seats"] == 2          # active users only
    assert out["usage"]["sessions"] == 1       # only s1 (in period, this tenant)
    assert out["usage"]["runs"] == 1           # one pipeline.started in period
    assert out["usage"]["tokens"] == 1500
    assert out["percent_used"]["sessions"] == 20   # 1 / 5
    assert out["period"]["start"].startswith("2026-05-01")


def test_unlimited_plan_has_no_percent():
    out = b.compute_billing({"slug": "acme", "plan": "enterprise"}, {}, [], now=NOW)
    assert out["limits"]["sessions_per_month"] is None
    assert out["percent_used"]["sessions"] is None


def _sessions_in_period(n, tenant="acme"):
    return {f"s{i}": {"tenant": tenant, "created_at": "2026-05-02T00:00:00+00:00", "usage": {}}
            for i in range(n)}


def test_check_quota_hard_block_at_limit():
    q = b.check_quota({"slug": "acme", "plan": "trial"}, "create_session",
                      _sessions_in_period(5), [], now=NOW)  # trial sessions limit = 5
    assert q["allowed"] is False and q["hard"] is True and q["metric"] == "sessions"


def test_check_quota_soft_warn_at_80pct():
    q = b.check_quota({"slug": "acme", "plan": "trial"}, "create_session",
                      _sessions_in_period(4), [], now=NOW)  # 4/5 = 80%
    assert q["allowed"] is True and q["warn"] is True


def test_super_admin_exempt():
    q = b.check_quota({"slug": "infinite", "plan": "trial"}, "create_session",
                      _sessions_in_period(99, "infinite"), [], now=NOW, admin_tenant="infinite")
    assert q["allowed"] is True and q["warn"] is False


def test_enterprise_unlimited_allowed():
    q = b.check_quota({"slug": "acme", "plan": "enterprise"}, "run_pipeline", {}, [], now=NOW)
    assert q["allowed"] is True and q["warn"] is False
