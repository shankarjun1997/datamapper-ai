# Deploying DataMapper AI to Fly.io (Free Tier)

## One-time setup

1. Install flyctl:
   curl -L https://fly.io/install.sh | sh

2. Sign up / log in (free, no credit card needed for free tier):
   fly auth signup
   # or: fly auth login

3. Launch the app (run once):
   cd ~/Desktop/dmapper
   fly launch --config fly.toml --no-deploy

4. Set your secrets (env vars):
   fly secrets set ANTHROPIC_API_KEY=sk-ant-...
   fly secrets set SLACK_WEBHOOK_URL=https://hooks.slack.com/services/...
   # Optional BQ:
   fly secrets set BQ_PROJECT_ID=your-project
   fly secrets set BQ_DATASET=your_dataset

5. Deploy:
   fly deploy

6. Open:
   fly open

## Subsequent deploys
   fly deploy   # rebuilds Docker image and deploys

## Logs
   fly logs

## Free tier limits
- 3 shared-cpu-1x-256mb machines (we use 1)
- 160GB outbound data/month
- Always-on (auto_stop_machines = false)
- Custom domain: fly certs add yourdomain.com
