# xREF Migration Workspaces (React)

Enterprise frontend re-platform: **React + TypeScript + Vite + Tailwind +
TanStack Query + Zustand**. Replaces the legacy single-file HTML UI with a
workspace-oriented app.

## Workspaces
- **Discovery** — canonical metadata catalog stats (`/api/metadata/*`)
- **Mapping** — confidence-based mapping grid (migrating from classic app)
- **Lineage** — source → target column lineage (`/api/sessions/{id}/lineage`)
- **Migration** — readiness dashboard (`/api/sessions/{id}/readiness`)
- **Governance** — approvals / audit (next)

## Auth
Uses the backend's httpOnly cookie session + CSRF double-submit (see
`src/lib/api.ts`). No tokens in localStorage.

## Develop
```bash
npm install
npm run dev      # http://localhost:5173 — proxies /api to http://localhost:7788
```
Run the FastAPI backend (`./run.sh`) alongside it.

## Build
```bash
npm run build    # type-checks then bundles to dist/
```
Deploy `dist/` to Vercel/Cloudflare Pages, or serve behind the API. Set
`VITE_API_URL` if the API is on a different origin (and pin `DM_ALLOWED_ORIGINS`
+ keep cookies cross-site).
