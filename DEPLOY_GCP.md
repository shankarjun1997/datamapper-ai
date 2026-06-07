# Deploying xREF DataMapper on a GCP VM (Compute Engine)

A complete, copy-paste guide from zero to a live HTTPS app. One server runs
everything (app + worker + Postgres + Redis + HTTPS proxy) via
`docker-compose.prod.yml`. Budget: ~$25/mo for an e2-medium (4 GB) — covered by
GCP's $300 free credit for ~3 months.

---

## Phase 0 — What you need

1. A Google account with billing enabled (new accounts get **$300 credit / 90 days**).
2. Your **Anthropic API key** (`sk-ant-…`).
3. A **GitHub fine-grained token** with read access to this repo (Settings →
   Developer settings → Fine-grained tokens), so the VM can `git clone`.
4. *(Optional but recommended)* a domain name (~$10/yr) for HTTPS.

## Phase 1 — Create the VM

1. console.cloud.google.com → create/select a project (e.g. `datamapper`).
2. Menu → **Compute Engine → VM instances** → Enable the API → **Create instance**.
3. Settings:
   - **Name:** `dmapper-1`
   - **Region:** closest to your users (e.g. `asia-south1` Mumbai)
   - **Machine type:** `e2-medium` (2 vCPU, 4 GB) — don't go below 4 GB
   - **Boot disk:** Ubuntu 24.04 LTS, **30 GB**
   - **Firewall:** ✔ Allow HTTP traffic, ✔ Allow HTTPS traffic
4. Click **Create**. Note the **External IP**.
5. Make the IP permanent: **VPC network → IP addresses →** find the VM's
   ephemeral external IP → **Reserve** (otherwise it changes on restart).

## Phase 2 — Install Docker on the VM

Click **SSH** next to the VM (opens a terminal in your browser), then:

```bash
sudo apt-get update && sudo apt-get -y upgrade
curl -fsSL https://get.docker.com | sudo sh
sudo usermod -aG docker $USER
exit
```

Re-open SSH (the group change needs a new session). Verify: `docker ps` works
without sudo.

## Phase 3 — Get the code

```bash
git clone https://YOUR_GITHUB_TOKEN@github.com/shankarjun1997/datamapper-ai.git
cd datamapper-ai
```

## Phase 4 — Configure secrets

```bash
cp .env.example .env
nano .env
```

Set these (generate values where shown):

```ini
DM_ENV=production
XREF_REQUIRE_AUTH=true
DM_PROVIDER=claude
ANTHROPIC_API_KEY=sk-ant-your-real-key

# python3 -c "import secrets; print(secrets.token_urlsafe(48))"
XREF_SECRET_KEY=<paste generated value>

# python3 -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
DM_ENCRYPTION_KEY=<paste generated value>

XREF_ADMIN_EMAIL=you@example.com
XREF_ADMIN_PASSWORD=<strong password — this is your login>

# python3 -c "import secrets; print(secrets.token_urlsafe(24))"
POSTGRES_PASSWORD=<paste generated value>

# Leave SITE_ADDRESS unset for now (Phase 6 adds the domain):
# SITE_ADDRESS=dmapper.example.com
# DM_ALLOWED_ORIGINS=https://dmapper.example.com
```

`Ctrl+O` Enter to save, `Ctrl+X` to exit.

## Phase 5 — Launch

The container runs as a non-root user (uid 10001); give it ownership of the
state folders it writes to, then start everything:

```bash
mkdir -p runtime audits
sudo chown -R 10001:10001 runtime audits
docker compose -f docker-compose.prod.yml up -d --build
```

First build ≈ 5–10 min. Then check:

```bash
docker compose -f docker-compose.prod.yml ps        # all "Up (healthy)"
curl -s localhost/api/health                        # {"status": ...}
```

Open `http://YOUR_EXTERNAL_IP` in a browser → login page → sign in with the
admin email/password from `.env`. (Plain HTTP for now — don't share this URL yet.)

## Phase 6 — Domain + automatic HTTPS

1. At your domain registrar, add an **A record**: `dmapper` → `YOUR_EXTERNAL_IP`.
2. On the VM:

```bash
nano .env       # set both lines:
# SITE_ADDRESS=dmapper.yourdomain.com
# DM_ALLOWED_ORIGINS=https://dmapper.yourdomain.com
docker compose -f docker-compose.prod.yml up -d
```

Caddy obtains a Let's Encrypt certificate automatically (~30 s).
`https://dmapper.yourdomain.com` is live; HTTP redirects to HTTPS.

## Phase 7 — Operations (the part that keeps you safe)

**Deploy an update:**

```bash
cd ~/datamapper-ai && git pull && docker compose -f docker-compose.prod.yml up -d --build
```

**Nightly database backup (3 AM, kept 14 days):**

```bash
mkdir -p ~/backups
( crontab -l 2>/dev/null; echo '0 3 * * * docker exec xref-postgres pg_dump -U xref xref | gzip > ~/backups/xref-$(date +\%F).sql.gz && find ~/backups -name "*.sql.gz" -mtime +14 -delete' ) | crontab -
```

**Restore from a backup:**

```bash
gunzip -c ~/backups/xref-2026-06-01.sql.gz | docker exec -i xref-postgres psql -U xref xref
```

**Logs / status / restart:**

```bash
docker compose -f docker-compose.prod.yml logs -f datamapper
docker compose -f docker-compose.prod.yml ps
docker compose -f docker-compose.prod.yml restart datamapper
```

**OS security updates (monthly):** `sudo apt-get update && sudo apt-get -y upgrade && sudo reboot`
(containers restart automatically — `restart: unless-stopped`).

**Firewall:** GCP blocks everything inbound except what you allowed (22/80/443).
Postgres/Redis have no public ports in the prod compose. Don't add any.

## Troubleshooting

| Symptom | Fix |
|---|---|
| Build dies / VM freezes | RAM too small — use e2-medium (4 GB)+ |
| `https://` shows cert error | DNS not propagated yet — wait 5–15 min; confirm the A record points at the reserved IP |
| 401 from AI features | `ANTHROPIC_API_KEY` invalid — check `.env`, then `docker compose -f docker-compose.prod.yml up -d` |
| App up but login fails | Wrong `XREF_ADMIN_*` in `.env` — fix and restart |
| Disk filling up | `docker system prune -af` clears old build layers |
