# AGENTS.md — xREF Agent / DataMapper AI

Cross-system schema mapping engine. Takes source schemas (files, live DBs, DDL,
or natural-language context) and maps them to a target system (typically
BigQuery) using LLM-powered semantic matching. Generates column mappings with
confidence scores, materialized SQL, migration docs, lineage graphs, and
readiness reports.

## Tech Stack

| Layer       | Tech                                                           |
|-------------|----------------------------------------------------------------|
| Backend     | Python 3 + FastAPI (Uvicorn), Celery + Redis for async jobs    |
| Auth        | Custom JWT cookies, tenant-based RBAC                          |
| LLM         | Anthropic, OpenAI, DeepSeek, Groq, Mistral, Ollama, custom     |
| DB          | PostgreSQL (SQLAlchemy/Alembic), BigQuery (target), JSON file  |
| Frontend    | Classic SPA (`frontend/index.html`, ~8k lines vanilla JS)     |
| Frontend 2  | React + TypeScript + Vite + Tailwind (`frontend-react/`, WIP) |
| Deploy      | Docker Compose, Caddy reverse proxy, GCP Cloud Run, Vercel    |

## Architecture: 4-Stage Pipeline

```
L1: Parse source schema → L2: Crawl target schema → L3: Semantic mapping → L4: SQL & doc generation
```

Pipeline defined in `app/core/pipeline.py`. Each stage has LLM-powered logic.

## Source Schema — 4 Entry Points

All in `app/routers/schema.py`:

| Endpoint                              | Method | How                                     |
|---------------------------------------|--------|-----------------------------------------|
| `/api/sessions/{sid}/upload`          | POST   | File upload (CSV, XLSX, SQL, DDL, TXT) |
| `/api/sessions/{sid}/parse-ddl`       | POST   | Paste raw DDL text                      |
| `/api/sessions/{sid}/source-connect`  | POST   | Live DB crawl via SQLAlchemy            |
| `/api/sessions/{sid}/source-from-context` | POST | LLM infers from free text / Jira ticket |

Schema is stored in session under key `schema_data` with canonical format:
```json
{"tables": [{"name": "...", "columns": [{"name": "...", "type": "...", "sample": "", "nullable": true}]}]}
```

## SQL/DDL Upload (including Stored Procedures)

**Backend** — `app/parsers/ddl.py`:
- `parse_ddl(ddl_text)` — regex-based parser for `CREATE TABLE` statements.
  Extracts table names, column names, types (normalized to BigQuery types).
  Handles MySQL, PostgreSQL, SQL Server, Oracle dialects.
- `has_stored_procedures(sql_text)` — detects `CREATE PROCEDURE`/`PROC`/`FUNCTION`.
- `extract_stored_procedures(sql_text)` — extracts procedure/function names.
  Handles `[schema].[name]`, backtick-quoted, double-quoted names.

**Backend** — `app/routers/schema.py` upload handler:
When a `.sql`/`.ddl`/`.txt` file contains stored procedures:
1. Parse CREATE TABLE statements normally (exact column definitions).
2. Send full SQL text to LLM with `_SP_SOURCE_SYS` prompt (in
   `app/intelligence/source_infer.py`) to infer tables/columns referenced
   inside procedure bodies (SELECT, INSERT, UPDATE, DELETE, MERGE analysis).
3. Merge both results via `merge_schemas()` in `app/intelligence/insights.py`.
LLM inference is best-effort — falls back to parsed results on failure.

**Backend** — `app/parsers/schema.py`:
- `parse_schema_file()` dispatches `.sql`, `.ddl`, `.txt` → `parse_ddl()`.
  Previous bug: only `.sql` was routed; `.ddl`/`.txt` raised ValueError. Fixed.

**Frontend** — `frontend/index.html`:
- Source dropzone: badges XLSX, XLS, CSV, SQL, DDL. Accepts all + .txt.
- Target dropzone: badges CSV, SQL, DDL, Multiple files. Accepts .csv,.sql,.ddl,.txt.
- Upload handler `handleFiles()` sends FormData to `/api/sessions/{sid}/upload`.

## Key Files

| File | Role |
|------|------|
| `app/main.py` | FastAPI app, static file serving, CORS, logging |
| `app/config.py` | Env config, LLM provider catalog, allowed upload exts |
| `app/routers/schema.py` | Upload, DDL parse, context-infer, source-connect |
| `app/routers/providers.py` | Target files upload, GCP creds, DB connect |
| `app/routers/sessions.py` | Session CRUD |
| `app/routers/pipeline.py` | Trigger mapping pipeline |
| `app/core/pipeline.py` | L1→L4 orchestrator |
| `app/core/session_store.py` | Session I/O (JSON file or Postgres) |
| `app/parsers/ddl.py` | DDL regex parser + SP detection |
| `app/parsers/schema.py` | File dispatch + type normalization |
| `app/intelligence/source_infer.py` | LLM prompts + schema normalization |
| `app/intelligence/insights.py` | Schema merge, summarization |
| `frontend/index.html` | Classic SPA (all uploads, pipeline UI) |
| `frontend-react/` | New React workspace UI (read-only, no upload yet) |

## Deploy / Rebuild

**Local VM** (Docker Compose):
```bash
docker compose build datamapper   # rebuild image from source
docker compose up -d datamapper   # restart container
```

Caddy is the reverse proxy (`xref.builder-os.in` → `datamapper:7788`).
Caddy config: `deploy/Caddyfile`.

**Vercel / Cloudflare Pages**:
Uses `build.sh` which runs `cd frontend && npm run build` (Vite) and deploys
`frontend/dist/`. Injects `window.__DM_API_URL__` at build time.

## Notes

- Target schema is stored under `target_files_data` in the session.
- Classic frontend is served directly from `frontend/` via uvicorn's
  `FileResponse` (no build step for local dev).
- The React frontend (`frontend-react/`) is a new re-platform, currently
  read-only (no upload/ingest). All source/target schema management
  still goes through the classic SPA.
- Type normalization maps SQL types → BigQuery types in
  `app/parsers/schema.py:_normalize_type()`.
