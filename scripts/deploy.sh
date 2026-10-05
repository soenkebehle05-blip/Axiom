#!/usr/bin/env bash
# Deploy to Cloud Run with settings that stay inside the always-free tier.
#
# Usage: PROJECT_ID=my-project ./scripts/deploy.sh [region]
# Reads ANTHROPIC_API_KEY (or GEMINI_API_KEY / OPENAI_API_KEY) from .env and token.json from the project folder.
set -euo pipefail
cd "$(dirname "$0")/.."

REGION="${1:-us-east1}"
SERVICE="smart-scheduler"
: "${PROJECT_ID:?Set PROJECT_ID to your Google Cloud project id}"
[ -f token.json ] || { echo "token.json missing: run 'uv run scripts/authorize_google.py' first"; exit 1; }
set -a; [ -f .env ] && . ./.env; set +a
: "${ANTHROPIC_API_KEY:?ANTHROPIC_API_KEY not set in .env}"

gcloud config set project "$PROJECT_ID" >/dev/null
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  texttospeech.googleapis.com calendar-json.googleapis.com secretmanager.googleapis.com

# Secrets (create once, add a version on later runs).
for pair in "anthropic-api-key:$ANTHROPIC_API_KEY" "calendar-creds-json:$(tr -d '\n' < token.json)"; do
  name="${pair%%:*}"; value="${pair#*:}"
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null
  else
    printf '%s' "$value" | gcloud secrets create "$name" --data-file=- --replication-policy=automatic >/dev/null
  fi
done

# Let the Cloud Run service account read the secrets.
PROJECT_NUMBER=$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"
for name in anthropic-api-key calendar-creds-json; do
  gcloud secrets add-iam-policy-binding "$name" --member="serviceAccount:$SA" --role=roles/secretmanager.secretAccessor >/dev/null
done

# Free-tier friendly: scale to zero (no --min-instances), 1 vCPU / 512 MiB, capped instances.
gcloud run deploy "$SERVICE" --source . --region "$REGION" --allow-unauthenticated \
  --session-affinity --timeout 3600 --cpu 1 --memory 512Mi --max-instances 2 --concurrency 40 --cpu-boost \
  --set-env-vars "TTS_ENABLED=true,DEFAULT_TIMEZONE=${DEFAULT_TIMEZONE:-Asia/Kolkata},ANTHROPIC_MODEL=${ANTHROPIC_MODEL:-claude-opus-5}" \
  --set-secrets "ANTHROPIC_API_KEY=anthropic-api-key:latest,CALENDAR_CREDS_JSON=calendar-creds-json:latest"

# Keep only the newest images so Artifact Registry stays under its free 0.5 GB.
REPO="cloud-run-source-deploy"
if gcloud artifacts repositories describe "$REPO" --location "$REGION" >/dev/null 2>&1; then
  cat > /tmp/ar-cleanup.json <<'JSON'
[{"name": "keep-latest-2", "action": {"type": "Keep"}, "mostRecentVersions": {"keepCount": 2}},
 {"name": "delete-old", "action": {"type": "Delete"}, "condition": {"olderThan": "1d"}}]
JSON
  gcloud artifacts repositories set-cleanup-policies "$REPO" --location "$REGION" --policy=/tmp/ar-cleanup.json --no-dry-run >/dev/null || true
fi

URL=$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')
echo; echo "Deployed: $URL"; echo "Health:   $(curl -s "$URL/health")"
