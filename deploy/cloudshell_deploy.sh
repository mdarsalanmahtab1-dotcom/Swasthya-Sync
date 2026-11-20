#!/usr/bin/env bash
# ──────────────────────────────────────────────────────────────────────────────
# SwasthyaSync — one-shot deploy from Google Cloud Shell
#   Backend  -> Cloud Run (asia-south1 / Mumbai)
#   Frontend -> Firebase Hosting
#   Secrets  -> Secret Manager (you type them; they never appear in the repo or logs)
#   Files    -> private Cloud Storage bucket
#
# Usage (in Cloud Shell, from the repo root):
#   bash deploy/cloudshell_deploy.sh YOUR_PROJECT_ID
# Safe to re-run: existing secrets/bucket are kept unless you choose to replace them.
# Billing must be enabled on the project.
# ──────────────────────────────────────────────────────────────────────────────
set -euo pipefail

PROJECT_ID="${1:?Usage: bash deploy/cloudshell_deploy.sh YOUR_PROJECT_ID}"
REGION="asia-south1"
SERVICE="swasthyasync-api"
BUCKET="${PROJECT_ID}-patient-docs"
cd "$(dirname "$0")/.."

say() { printf '\n\033[1;34m== %s\033[0m\n' "$*"; }

gcloud config set project "$PROJECT_ID" >/dev/null
PROJECT_NUMBER="$(gcloud projects describe "$PROJECT_ID" --format='value(projectNumber)')"
SA="${PROJECT_NUMBER}-compute@developer.gserviceaccount.com"   # Cloud Run's runtime identity

say "1/7 Enabling Google Cloud services (1-2 min)"
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com firebase.googleapis.com firebasehosting.googleapis.com

say "2/7 Secrets (input is hidden; nothing is printed)"
put_secret() {  # name, prompt
  local name="$1" prompt="$2" value answer
  if gcloud secrets describe "$name" >/dev/null 2>&1; then
    read -r -p "Secret '$name' already exists. Replace it? (y/N) " answer
    [[ "$answer" == "y" || "$answer" == "Y" ]] || return 0
    read -r -s -p "$prompt: " value; echo
    printf '%s' "$value" | gcloud secrets versions add "$name" --data-file=- >/dev/null
  else
    read -r -s -p "$prompt: " value; echo
    printf '%s' "$value" | gcloud secrets create "$name" --data-file=- --replication-policy=automatic >/dev/null
  fi
  echo "   saved: $name"
}
put_secret gemini-api-key "Gemini API key (billing-enabled project)"
put_secret database-url "DATABASE_URL (Supabase pooler; port 6543 works everywhere)"
put_secret admin-bootstrap-password "Admin panel password"
read -r -p "Admin email (login for the admin panel): " ADMIN_EMAIL
SECRETS="GEMINI_API_KEY=gemini-api-key:latest,DATABASE_URL=database-url:latest,ADMIN_BOOTSTRAP_PASSWORD=admin-bootstrap-password:latest"
read -r -p "Add a Sarvam key for faster speech input? (y/N) " use_sarvam
if [[ "$use_sarvam" == "y" || "$use_sarvam" == "Y" ]]; then
  put_secret sarvam-api-key "Sarvam API key"
  SECRETS="${SECRETS},SARVAM_API_KEY=sarvam-api-key:latest"
fi

say "3/7 Permissions for Cloud Run's service account"
for role in roles/secretmanager.secretAccessor roles/run.builder; do
  # run.builder: needed by 'gcloud run deploy --source' on newer projects
  gcloud projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:${SA}" \
    --role="$role" --condition=None --quiet >/dev/null
  echo "   granted: $role"
done

say "4/7 Private bucket for patient documents"
if ! gcloud storage buckets describe "gs://${BUCKET}" >/dev/null 2>&1; then
  gcloud storage buckets create "gs://${BUCKET}" --location="$REGION" \
    --uniform-bucket-level-access --public-access-prevention
fi
gcloud storage buckets add-iam-policy-binding "gs://${BUCKET}" \
  --member="serviceAccount:${SA}" --role=roles/storage.objectAdmin >/dev/null
echo "   bucket ready: gs://${BUCKET}"

say "5/7 Building and deploying the backend to Cloud Run (5-10 min the first time)"
# One warm instance + session affinity: interview sessions live in memory and use a WebSocket.
# --timeout 3600: WebSocket interviews can run for many minutes.
# 2 CPU / 2 GiB: headless Chromium renders the PDF casesheet.
gcloud run deploy "$SERVICE" --source backend --region "$REGION" \
  --allow-unauthenticated --port 8080 \
  --max-instances 1 --min-instances 1 --session-affinity --timeout 3600 \
  --cpu 2 --memory 2Gi --no-cpu-throttling \
  --set-secrets "$SECRETS" \
  --set-env-vars "^@^CORS_ORIGINS=https://${PROJECT_ID}.web.app,https://${PROJECT_ID}.firebaseapp.com@GCS_BUCKET=${BUCKET}@ADMIN_EMAIL=${ADMIN_EMAIL}@OTP_PROVIDER=mock@VOICE_MODE=legacy@SPEECH_PROVIDER=google@STT_PROVIDER=sarvam" \
  --quiet
API_URL="$(gcloud run services describe "$SERVICE" --region "$REGION" --format='value(status.url)')"
echo "   backend: ${API_URL}"
curl -s --max-time 30 "${API_URL}/health" && echo
curl -s --max-time 60 "${API_URL}/ready" && echo

say "6/7 Building the frontend against ${API_URL}"
cat > frontend/.env.production <<EOF
# Written by deploy/cloudshell_deploy.sh (no secrets here)
VITE_API_URL=${API_URL}
VITE_WS_URL=${API_URL/https:/wss:}/ws/session
EOF
sed -i "s/\"default\": \"[^\"]*\"/\"default\": \"${PROJECT_ID}\"/" .firebaserc
(cd frontend && npm ci --no-audit --no-fund && npm run build)

say "7/7 Publishing to Firebase Hosting"
FIREBASE="firebase"
command -v firebase >/dev/null 2>&1 || FIREBASE="npx --yes firebase-tools"
$FIREBASE projects:list >/dev/null 2>&1 || $FIREBASE login --no-localhost
$FIREBASE projects:addfirebase "$PROJECT_ID" >/dev/null 2>&1 || true   # no-op if already added
$FIREBASE deploy --only hosting --project "$PROJECT_ID"

say "Done"
echo "   Kiosk / web app : https://${PROJECT_ID}.web.app"
echo "   Backend API     : ${API_URL}"
echo "   Health check    : ${API_URL}/ready"
echo "Note: the backend keeps 1 warm instance (needed for live interviews). After the event,"
echo "scale it down to save money:  gcloud run services update ${SERVICE} --region ${REGION} --min-instances 0"
