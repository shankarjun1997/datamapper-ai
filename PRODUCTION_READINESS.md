# xREF DataMapper — Production Readiness & Onboarding Review

**Date:** 2026-05-29
**Reviewed:** current `app/` codebase (FastAPI modular v2.0.0), deploy configs, auth/RBAC/tenant model.

## Verdict

**Not production-ready for external clients yet.** The v2 architecture is genuinely good — clean modular layout, RBAC, audit logging, OIDC, Celery, Alembic, structured logging, security headers. But there are a few blockers that make multi-client onboarding unsafe today. The biggest one is a deployment mismatch: **production deploys the wrong app**. Realistic time to a safe first-client launch: ~1 week of focused work.

> Note: the previous version of this file described a `services/` + `core/security.py` layout that no longer exists. It was stale and has been replaced by this review of the actual code.

---

## ✅ Fixed in this pass (2026-05-29)

The four critical blockers are now addressed in code, with tests:

- **#1 Wrong app in prod** — `Dockerfile` now runs `app.main:app` (multi-stage, non-root `appuser`, `HEALTHCHECK`, honors `$PORT`). `render.yaml` / `fly.toml` set `DM_ENV=production` + `XREF_REQUIRE_AUTH=true` and declare the required secrets.
- **#2 Cross-tenant IDOR** — `_session_or_404` now enforces tenant ownership, and a `TenantIsolationMiddleware` chokepoint blocks cross-tenant `/sessions/{uuid}` access for *all* routes (returns 404, not 403). Covered by `tests/test_tenant_isolation.py`.
- **#3 Auth off / unguarded routes** — production defaults `XREF_REQUIRE_AUTH=true` and refuses to start if it's disabled or the secret is a placeholder. A global `AuthEnforcementMiddleware` requires a valid token on every `/api/*` route except a small public allowlist; `sessions`/`schema`/`exports` routers also carry role-level guards.
- **#4 Password storage** — passwords were stored/compared in **plaintext**; now bcrypt (`app/core/auth.py`) with backward-compatible verification and transparent upgrade-on-login. Default `demo` tenant and hardcoded admin password are no longer seeded in production. Covered by `tests/test_password_hashing.py`.

**Run the tests:** `pip install -r requirements.txt && pytest`

### Second hardening pass (priority backlog knocked out)

- **Deprecated code removed** — legacy `server.py` and the stale root `index.html` deleted; `frontend/` is now the single source of truth (API URL resolution unified for both same-origin and split/Vercel hosting). `run.sh`, `build.sh`, `vercel.json` rewired.
- **Postgres mandatory in prod (#6/#13)** — the app now refuses to start in production if the database isn't reachable, so a client never runs on disposable in-memory state. (Uploads are parsed into session state, not written to disk, so no object store is required.)
- **CI pipeline (#12)** — `.github/workflows/ci.yml`: ruff lint + pytest + Docker build on push/PR.
- **Per-write RBAC (#5 follow-up)** — all mutating provider/session-config endpoints now require `mapper`+.
- **Redis-backed rate limiting (#10)** — shared limiter when `REDIS_URL` is set (falls back to in-memory); login is throttled to 10 attempts / 5 min / IP (brute-force protection).
- **Sentry (#11)** — optional error tracking, activated when `SENTRY_DSN` is set.

Tests now: **14 passing** (`tests/test_password_hashing.py`, `test_tenant_isolation.py`, `test_rate_limit.py`).

### Third hardening pass (remaining backlog)

- **CSP tightened** — unused CDN allowances removed (frontend only loads Google Fonts).
- **Instance sizing** — `fly.toml` → 1 GB; `render.yaml` → `standard` plan. Compose Postgres password parameterised off `xref/xref`.
- **Alembic on deploy** — `docker-entrypoint.sh` runs `alembic upgrade head` before the web app boots (skipped for the Celery worker).
- **Token refresh + revocation** — `/api/auth/refresh` (sliding session); deactivation, password change, and logout set a `tokens_valid_after` watermark that immediately invalidates old tokens. Covered by `tests/test_token_revocation.py`.
- **Secrets encrypted at rest** — `app/core/crypto.py` Fernet-encrypts sensitive session fields (GCP keys, DB connection strings, API keys) at the persistence boundary when `DM_ENCRYPTION_KEY` is set; in-memory stays plaintext so consumers are unchanged. Covered by `tests/test_crypto.py`.
- **Transactional email + password reset** — provider-agnostic mailer (`RESEND_API_KEY`/`SENDGRID_API_KEY`); invites are emailed; `/api/auth/forgot-password` + `/api/auth/reset-password` flow (rate-limited, no account-existence leak).
- **httpOnly cookie auth + CSRF** — login/refresh set an httpOnly `xref_token` cookie + readable CSRF cookie; cookie-authenticated unsafe requests require the `X-CSRF-Token` double-submit. The existing Bearer flow still works (backward compatible). CORS credentials auto-enable when origins are pinned.
- **Frontend XSS hardening** — dynamic/error strings routed through `escHtml()` in the high-risk `innerHTML` sinks (toast, connection/upload status, audit, tenant options).
- **Vite build** — `frontend/` is now a Vite multi-page project (`npm run build` → `dist/`); `build.sh`/`vercel.json` use it. Build verified.

Tests now: **26 passing + 1 CI-only integration test** (`tests/`).

Still open (lower priority): drop `unsafe-inline` from CSP (requires extracting inline scripts to modules — a larger frontend refactor), DB backups + restore drill, and HA (second instance). The cookie-auth + Vite changes should be smoke-tested in a real browser before the client demo, since they couldn't be browser-verified here. See `DEPLOYMENT_GUIDE.md`.

---

## Critical (blockers — fix before any client)

### 1. Production deploys the legacy monolith, not the secured v2 app
`Dockerfile` runs `uvicorn server:app`. `render.yaml` and `fly.toml` both build from that Dockerfile, so **production runs `server.py`** (title "DataMapper" v1.0.0), which has **zero RBAC, no tenant isolation, no admin, no OIDC** (`grep` for those terms in `server.py` returns 0). All the access-control work lives in `app/main.py`, which only `run.sh` (local dev) launches.

Net effect: every security control described below isn't even running in prod. Anything deployed today is effectively open.

**Fix:** change the Dockerfile CMD to `uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-7788}`, delete or archive `server.py` to avoid future confusion, and redeploy.

### 2. Cross-tenant data access (IDOR) on every by-ID endpoint
`_session_or_404(sid)` (`app/core/session_store.py`) validates the UUID format and existence but **never checks the session belongs to the caller's tenant**. It's called ~40 times across `sessions`, `pipeline`, `mappings`, `exports`, `schema`, and `providers` routers. Only the *list* endpoints filter by tenant; direct fetch/execute/export by `sid` does not.

So an authenticated user in tenant A who learns a tenant B session UUID can read its schema, mappings, and exports (DDL/data), run pipeline actions, and read/modify provider + API config. UUID randomness is the only thing protecting client data — and session IDs appear in URLs, logs, and audit exports.

**Fix:** add a tenant-scoped accessor, e.g. `_session_or_404(sid, user)` that 404s when `session["tenant"] != user["tenant"]` (super-admin tenant exempt). Apply everywhere.

### 3. Auth is off by default and most endpoints have no role guard
`_REQUIRE_AUTH` defaults to `false` (`app/config.py`), so unauthenticated requests are treated as `_GUEST_USER` with **admin** role. Of 91 endpoints, only ~18 carry a `Depends(require_*)` guard — `sessions`, `schema`, `exports`, and `providers` routers have **none**.

**Fix:** set `XREF_REQUIRE_AUTH=true` for any client-facing deploy (and fail startup if it's false in production), and attach `require_mapper`/`require_readonly` dependencies to the session/schema/export/provider routes.

### 4. Weak password hashing + committed default credentials
`_hash_password` is unsalted **SHA-256** (`app/core/auth.py`) — fast to brute-force and rainbow-table-able. Defaults ship in `config.py`: `admin@infinite.io` / `xref2026` and `demo` / `demo`. The signing secret also defaults to a placeholder (`xref-demo-secret-change-in-prod-2026`).

**Fix:** switch to `bcrypt` or `argon2` (add to `requirements.txt`); remove default users in production / force a password change on first login; require `XREF_SECRET_KEY` from env with no fallback in prod (startup already warns — make it fatal).

---

## High

5. **No automated tests and no CI.** Only a CrewAI demo script exists; no `tests/`, no `.github/workflows`. The fixes above need regression coverage — especially a test proving tenant A cannot fetch tenant B's session. Add pytest + a lint/test/build pipeline.

6. **State durability / horizontal scaling.** Sessions live in an in-memory dict hydrated from JSON in `runtime/`; uploads go to local disk. On Render/Fly free tier (ephemeral disk, single 256 MB instance) client data is lost on restart and can't scale to a second instance. Postgres mode exists — make it mandatory in prod and move uploads to object storage (S3/GCS).

7. **Dockerfile is not production-grade.** Runs as root, single-stage, `COPY . .` pulls local cruft, hardcodes `--port 7788` (ignores `$PORT`), creates `audits/sessions/uploads` but the app now uses `runtime/`. Add a non-root user, multi-stage build, `HEALTHCHECK`, and honor `$PORT`.

8. **Resourcing.** 256 MB RAM running CrewAI + LLM SDKs + pandas + multiple DB drivers will likely OOM, and `min_machines_running = 1` with a single worker is a single point of failure. Size up before clients.

---

## Medium

9. **CORS defaults to `*`** (`DM_ALLOWED_ORIGINS`). Credentials are off so it's not catastrophic, but pin to the real frontend origin in prod.
10. **In-memory rate limiter** (`_helpers.py`) is per-process — useless across multiple workers/instances and resets on restart. Move to Redis (already in the compose stack), and ensure `/auth/login` is covered (brute-force protection).
11. **No error tracking / metrics.** Structured logging is in place; add Sentry (or similar) and basic metrics before clients depend on uptime.
12. **Token lifetime 24h, no refresh/revocation.** Deactivating a user doesn't invalidate live tokens until expiry (role is re-checked from the tenant store on each request, which helps, but `active` flips only apply on next request — verify). Consider shorter tokens + refresh, or a revocation list.
13. **Hardcoded DB creds in `docker-compose.yml`** (`xref/xref`). Fine for local, never for shared environments.

---

## Onboarding-specific gaps

The tenant model itself is sound (slug-based tenants, users with roles, super-admin tenant `infinite`, audit events scoped by tenant in the admin router). The gap is **enforcement**: isolation is implemented for *lists and admin queries* but not for *by-ID resource access* (finding #2). Until #1–#4 are closed, onboarding a second client means client A and client B can reach each other's data.

For onboarding to be smooth you'll also want: a self-serve or scripted tenant-provisioning flow (an admin endpoint exists — confirm it's the path you want), password reset / invite emails (currently absent), and a per-tenant data-export + deletion path for GDPR (sessions are stamped with `user_email` for erasure, which is a good start).

---

## Suggested order of work

1. Point production at `app.main:app`; archive `server.py` (#1).
2. Tenant-scope `_session_or_404` and add an isolation test (#2, #5).
3. Force `XREF_REQUIRE_AUTH=true` in prod + add role guards to unguarded routers (#3).
4. bcrypt/argon2 hashing, remove default creds, mandatory env secret (#4).
5. Make Postgres + object storage mandatory in prod; size up instance (#6, #8).
6. Harden Dockerfile, add CI, Redis rate limiting, Sentry, CORS pinning (#7, #9–11).

**What's already solid:** clean module separation, RBAC primitives, audit logging with SIEM exports, OIDC support, Alembic migrations, structured JSON logging, security headers + CSP, `.env` correctly gitignored, startup secret validation. These are real foundations — the remaining work is targeted hardening, not a rewrite.
