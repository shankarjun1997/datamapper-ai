# DataMapper AI — Deployment Guide

## Architecture

```
Browser → Vercel  (index.html, static, free)
             ↓ API calls
          Render  (FastAPI backend, Docker, free)
             ↓ LLM
          DeepSeek API  (deepseek-chat, fast + cheap)
             ↓ optional
          BigQuery / Supabase
```

---

## Step 1 — Deploy Backend to Render (free)

1. Go to https://render.com → **New → Web Service**
2. Connect GitHub → select `datamapper-ai`, branch: `dev`
3. Runtime: **Docker** (auto-detected)
4. Plan: **Free** | Region: Singapore

### Environment variables (Render dashboard → Environment tab)
| Key | Value |
|-----|-------|
| `LLM_API_KEY` | DeepSeek API key (https://platform.deepseek.com) |
| `DM_PROVIDER` | `deepseek` |
| `LLM_MODEL` | `deepseek-chat` |
| `LLM_BASE_URL` | `https://api.deepseek.com/v1` |
| `SLACK_WEBHOOK_URL` | Slack webhook URL (optional) |
| `BQ_PROJECT_ID` | GCP project ID (optional) |
| `BQ_DATASET` | BigQuery dataset (optional) |

5. Click **Create Web Service** — first build ~3 min
6. Backend URL: `https://datamapper-ai-api.onrender.com`

> Free Render services sleep after 15 min idle — first request after sleep takes ~30s.

---

## Step 2 — Deploy Frontend to Vercel (free)

1. Go to https://vercel.com → **New Project**
2. Import from GitHub → select `datamapper-ai`
3. Framework preset: **Other**
4. Build command: `bash build.sh`
5. Output directory: `.`

### Environment variable
| Key | Value |
|-----|-------|
| `DM_API_URL` | `https://datamapper-ai-api.onrender.com` |

6. Click **Deploy**
7. Frontend URL: `https://datamapper-ai.vercel.app`

---

## DeepSeek API Setup

1. Sign up at https://platform.deepseek.com
2. API Keys → Create new key
3. Model: `deepseek-chat` (DeepSeek-V3 — fast, highly capable)
4. Cost: ~$0.14 / 1M input tokens (very low for mapping sessions)

---

## Local Development

```bash
# Add to sql gen/.env (already loaded by server.py)
LLM_API_KEY=sk-...your-deepseek-key
DM_PROVIDER=deepseek
LLM_MODEL=deepseek-chat

# Run
./run.sh   →   http://localhost:7788
```

---

## Auto-deploy on push

Both Render and Vercel auto-deploy when you push to `dev`.
No manual steps needed after initial setup.
