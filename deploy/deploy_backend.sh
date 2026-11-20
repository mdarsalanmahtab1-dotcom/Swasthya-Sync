#!/usr/bin/env bash
# Deploy the FastAPI backend to Cloud Run (asia-south1 / Mumbai).
# Usage:  PROJECT_ID=my-project bash deploy/deploy_backend.sh      (run from the repo root)
#
# Why these flags:
#  --max-instances 1 / --min-instances 1 / --session-affinity
#      Interview sessions (DialogueManager objects) live in memory in ONE process, and the
#      kiosk keeps a WebSocket open. A second instance would not know the session, so we run
#      exactly one warm instance and pin each browser to it.
#  --timeout 3600      WebSocket interviews can last many minutes; Cloud Run closes requests
#                      (including WebSockets) at the timeout, max 60 min.
#  --no-cpu-throttling Background tasks (stale-session cleanup, WebSocket keep-alive) need CPU
#                      between requests.
#  --cpu 2 --memory 2Gi  Headless Chromium (Playwright) renders the PDF casesheet.
#  --set-secrets       Keys come from Secret Manager, never from the image or the repo.
#  "^;^"               Changes gcloud's list separator to ";" so values containing commas or "@"
#                      (CORS_ORIGINS, model lists) are not split.
set -euo pipefail
: "${PROJECT_ID:?Set PROJECT_ID}"
REGION="${REGION:-asia-south1}"
BUCKET="${GCS_BUCKET:-${PROJECT_ID}-patient-docs}"
SECRETS="GEMINI_API_KEY=gemini-api-key:latest,DATABASE_URL=database-url:latest,ADMIN_BOOTSTRAP_PASSWORD=admin-bootstrap-password:latest"
# Optional: append ,SARVAM_API_KEY=sarvam-api-key:latest  to enable the Sarvam fallback.

gcloud run deploy swasthyasync-api --source backend --region "$REGION" --project "$PROJECT_ID" \
  --allow-unauthenticated --port 8080 \
  --max-instances 1 --min-instances 0 --cpu-throttling --session-affinity --timeout 3600 \
  --cpu 1 --memory 2Gi \
  --set-secrets "$SECRETS" \
  --set-env-vars "^;^VOICE_MODE=legacy;SPEECH_PROVIDER=google;CORS_ORIGINS=https://${PROJECT_ID}.web.app,https://${PROJECT_ID}.firebaseapp.com;GCS_BUCKET=${BUCKET};ADMIN_EMAIL=${ADMIN_EMAIL:-admin@swasthyasync.local};OTP_PROVIDER=mock"

gcloud run services describe swasthyasync-api --region "$REGION" --project "$PROJECT_ID" --format='value(status.url)'
