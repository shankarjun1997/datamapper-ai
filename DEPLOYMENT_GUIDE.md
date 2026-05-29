# xREF DataMapper — Deployment & Onboarding Guide

How to ship the hardened build, where each piece should run, and the checklist
to go live with real clients.

---

## 1. The right topology (and why not "all Cloudflare")

This app has two very different parts:

| Part | What it is | Where it should run |
|------|-----------|---------------------|
| **Frontend** | Static `index.html` / `login.html` (HTML + Tailwind CDN + JS) | **Vercel** or **Cloudflare Pages** (static hosting / CDN) |
| **Backend** | FastAPI app (`app.main:app`) with CrewAI, LLM SDKs, pandas, SQLAlchemy DB drivers, a Celery worker, Postgres + Redis, persistent state | **A container host** — Render, Fly.io, Railway, or Google Cloud Run |

**Why Cloudflare Workers can't host the backend.** Cloudflare's Python Workers
now run FastAPI and even pandas/numpy (via Pyodide), but they **cannot
`pip install` arbitrary native code** — that rules out `psycopg2`/the SQL DB
drivers and the CrewAI stack — and Workers are a short-lived, edge serverless
runtime with CPU-time limits and **no long-running server or persistent local
disk**. Your backend is a stateful, long-running container, so it belongs on a
container platform. Cloudflare is great for the *frontend* (Pages) or as a
CDN/proxy in front of the API — not for the Python service itself.
([Cloudflare Python packages docs](https://developers.cloudflare.com/workers/languages/python/packages/),
[Workers limits](https://developers.cloudflare.com/workers/platform/limits/))

**Recommended:** Frontend on **Vercel**, backend on **Render** (already wired)
or **Fly.io**. Put Cloudflare in front as DNS/CDN/WAF if you want.

> Simpler alternative for a first client: the FastAPI app already serves the
> `frontend/` folder as static files, so you can deploy **just the backend
> container** and reach the whole app at one origin (no CORS, no second host).
> Use the split (Vercel + API) when you want a CDN-fronted marketing/login UX.

---

## 2. Deploy the frontend to Vercel

The repo is already set up for this: `vercel.json` + `build.sh` inject the API
URL into `index.html` at build time.

1. Import the repo in Vercel.
2. Set env var `DM_API_URL` = your backend URL (e.g. `https://datamapper-ai-api.onrender.com`).
3. Build command: `bash build.sh` (already referenced); output: repo root.
4. Deploy. The login page will call the backend at `DM_API_URL`.

(Cloudflare Pages equivalent: same idea — set `DM_API_URL`, run `build.sh`,
serve the static root.)

---

## 3. Deploy the backend

### Option A — Render (config in `render.yaml`)
`render.yaml` now sets `DM_ENV=production` and `XREF_REQUIRE_AUTH=true` and
declares the secrets as `sync: false` (you set them in the dashboard). Steps:

1. New → Blueprint → point at the repo (it reads `render.yaml`).
2. In the dashboard, set the secret env vars (section 4 below).
3. Add a **Render Postgres** and **Redis** instance; copy their URLs into
   `DATABASE_URL` and `REDIS_URL`.
4. Deploy. Health check is `/api/health`.

> ⚠️ The `free` plan sleeps and has ~512 MB RAM — fine for a demo, **not** for a
> client. Move to a paid instance with ≥1 GB RAM (CrewAI + LLM SDKs + pandas are
> memory-hungry) before onboarding.

### Option B — Fly.io (config in `fly.toml`)
1. `fly launch --config fly.toml` (or `fly deploy`).
2. `fly secrets set XREF_SECRET_KEY=... XREF_ADMIN_PASSWORD=... LLM_API_KEY=... DATABASE_URL=... DM_ALLOWED_ORIGINS=...`
3. Provision Fly Postgres + Upstash Redis; set their URLs as secrets.
4. **Bump `memory` from 256 MB to ≥1024 MB** in `fly.toml` `[[vm]]`.

### Option C — Local / self-host (Docker Compose)
`docker-compose.yml` brings up the API + Celery worker + Postgres + Redis.
Change the Postgres password from `xref/xref` and set a real `.env` first.

The `Dockerfile` is now multi-stage, runs as a non-root user, has a
`HEALTHCHECK`, honors `$PORT`, and launches the **secured** `app.main:app`
(not the legacy `server.py`).

---

## 4. Required production env / secrets

| Variable | Required | Notes |
|----------|----------|-------|
| `DM_ENV` | ✅ | Must be `production` (enables auth, HSTS/CSP, JSON logs). |
| `XREF_REQUIRE_AUTH` | ✅ | `true`. App refuses to boot if `false` in prod. |
| `XREF_SECRET_KEY` | ✅ | Strong random (e.g. `openssl rand -hex 32`). App refuses to boot on a placeholder value. |
| `XREF_ADMIN_EMAIL` / `XREF_ADMIN_PASSWORD` | ✅ | Bootstrap admin. No default in prod. |
| `DM_ALLOWED_ORIGINS` | ✅ | Pin to your frontend origin, e.g. `https://app.client.com`. Don't ship `*`. |
| `DATABASE_URL` | ✅ | Postgres. Without it, state is in-memory JSON and lost on restart. |
| `REDIS_URL` | ✅ if using Celery / multi-instance | Enables shared rate limiting + async pipeline queue. Without it, rate limiting is per-process. |
| `LLM_API_KEY` (+ `DM_PROVIDER`, `LLM_MODEL`, `LLM_BASE_URL`) | ✅ | Your LLM provider creds. |
| `SENTRY_DSN` | optional | Enables error tracking. `SENTRY_TRACES_SAMPLE_RATE` (default 0.1) tunes tracing. |
| `DM_ENCRYPTION_KEY` | strongly recommended | Fernet key — encrypts secrets at rest (GCP keys, DB strings, API keys). Generate: `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. |
| `RESEND_API_KEY` **or** `SENDGRID_API_KEY` | needed for email | Enables invite + password-reset emails. Set `EMAIL_FROM` (e.g. `xREF <noreply@yourdomain.com>`). |
| `DM_PUBLIC_URL` | needed for email links | Base URL used in email links, e.g. `https://app.client.com`. |
| `GOOGLE_APPLICATION_CREDENTIALS`, `BQ_*`, `JIRA_*`, `SLACK_WEBHOOK_URL` | optional | Per-feature. |

### Frontend build (Vite)

`frontend/` is a Vite multi-page project. `build.sh` runs `npm run build` → `frontend/dist/`, then injects `DM_API_URL`. Locally: `cd frontend && npm install && npm run build`. Vercel uses `build.sh` automatically (outputDirectory `frontend/dist`). The backend can still serve the un-built source pages same-origin.

### Auth transport

Login/refresh set an **httpOnly `xref_token` cookie** plus a readable CSRF cookie; the frontend sends `credentials: 'include'` and an `X-CSRF-Token` header on writes. The legacy Bearer/localStorage flow still works. For split (Vercel) hosting, pin `DM_ALLOWED_ORIGINS` so CORS credentials are allowed.

Generate the secret: `openssl rand -hex 32`.

---

## 5. Pre-launch checklist (before the first client)

Done:
- [x] Production runs the secured `app.main:app` (not `server.py`, now deleted).
- [x] Cross-tenant session access blocked (middleware + `_session_or_404`).
- [x] Auth enforced on every `/api/*` route in production.
- [x] Passwords hashed with bcrypt; no default demo login in prod.
- [x] Hardened Docker image (non-root, multi-stage, healthcheck, `$PORT`).
- [x] **Postgres mandatory in prod** — app refuses to start if DB unreachable.
- [x] **Per-write RBAC** on provider/session-config routes (`mapper`+).
- [x] **Redis-backed rate limiting** + login brute-force throttle (10 / 5 min / IP).
- [x] **Sentry** error tracking (set `SENTRY_DSN`).
- [x] **CI pipeline** — `.github/workflows/ci.yml` (lint + pytest + docker build).
- [x] Security tests (`pytest`, 14 passing).

Still recommended before scaling beyond the first client:
- [ ] **Run Alembic migrations** on deploy (`alembic upgrade head`) rather than
      relying on the `ensure_schema()` bootstrap.
- [ ] **Right-size the instance** (≥1 GB RAM) and add a second instance for HA.
- [ ] **Backups** for Postgres; a documented restore drill.
- [ ] **Token refresh / revocation** so deactivating a user takes effect before
      token expiry.
- [ ] **Frontend hardening** — reduce `innerHTML` XSS surface; consider moving
      the token out of `localStorage`.
- [ ] **Secrets at rest** — GCP service-account keys are stored in session state;
      consider encrypting sensitive session fields.

---

## 6. Onboarding a new client (tenant)

1. As super-admin (the `infinite` tenant), create the client's tenant/workspace.
2. Invite their admin via `POST /api/auth/users/invite` (sends a temporary,
   bcrypt-hashed password). They change it on first login (min 8 chars).
3. Assign roles: `admin`, `mapper`, `reviewer`, `readonly`.
4. Verify isolation: log in as the new tenant and confirm they only see their
   own sessions; another tenant's session URL returns 404.
5. Pin `DM_ALLOWED_ORIGINS` to their domain if they use a custom frontend.

---

## Is it production-ready to onboard clients?

**The critical security blockers are fixed** — with the production env vars in
section 4 set, it is safe to onboard a **first** client. Before onboarding
*multiple* clients at scale, close the durability/observability items in
section 5 (Postgres + object storage, backups, monitoring, CI, instance sizing).
