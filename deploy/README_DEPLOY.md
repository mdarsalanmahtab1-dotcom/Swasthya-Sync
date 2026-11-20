# Deploying SwasthyaSync on Google Cloud

Backend → **Cloud Run** (`swasthyasync-api`, asia-south1). Frontend → **Firebase Hosting**.
Secrets → **Secret Manager**. Files → **Cloud Storage** (private). DB → **AlloyDB** or Supabase Postgres via `DATABASE_URL`.

Replace `PROJECT_ID` / `PROJECT_NUMBER` everywhere (`gcloud projects describe PROJECT_ID --format='value(projectNumber)'`).
Every command below creates or changes billable resources, so run them yourself.

## 1. One-time setup
```bash
gcloud config set project PROJECT_ID
gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  secretmanager.googleapis.com storage.googleapis.com alloydb.googleapis.com aiplatform.googleapis.com firebase.googleapis.com
```

### Secrets (values never go in git)
```bash
printf '%s' "PASTE_GEMINI_KEY"                     | gcloud secrets create gemini-api-key --data-file=-
printf '%s' "postgresql://USER:PASS@HOST:5432/DB"  | gcloud secrets create database-url --data-file=-
printf '%s' "A_LONG_RANDOM_ADMIN_PASSWORD"         | gcloud secrets create admin-bootstrap-password --data-file=-
# optional
printf '%s' "PASTE_SARVAM_KEY"                     | gcloud secrets create sarvam-api-key --data-file=-
```
Let Cloud Run's service account read them (default compute SA):
```bash
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member=serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com \
  --role=roles/secretmanager.secretAccessor
```

### Private document bucket (Phase 5)
```bash
gcloud storage buckets create gs://PROJECT_ID-patient-docs --location=asia-south1 \
  --uniform-bucket-level-access --public-access-prevention
gcloud storage buckets add-iam-policy-binding gs://PROJECT_ID-patient-docs \
  --member=serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com --role=roles/storage.objectAdmin
```

### AlloyDB (Phase 6, only if using the connector)
```bash
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member=serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com --role=roles/alloydb.client
gcloud projects add-iam-policy-binding PROJECT_ID \
  --member=serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com --role=roles/serviceusage.serviceUsageConsumer
```
Then create the tables and synthetic demo data (from `backend/`, with `DATABASE_URL` set locally):
```bash
python scripts/init_schema.py
python seed_judge_demo_data.py
```

## 2. Deploy the backend
```bash
PROJECT_ID=PROJECT_ID ADMIN_EMAIL=admin@yourteam.example bash deploy/deploy_backend.sh
```
It prints the service URL, e.g. `https://swasthyasync-api-PROJECT_NUMBER.asia-south1.run.app`.
Why one instance and a 3600 s timeout: interview sessions live in memory and use a WebSocket (see comments in the script).

## 3. Deploy the frontend
Put the Cloud Run URL in `frontend/.env.production` (`VITE_API_URL`, `VITE_WS_URL=wss://.../ws/session`), set the project in `.firebaserc`, then:
```bash
npm i -g firebase-tools
firebase login
firebase projects:addfirebase PROJECT_ID
cd frontend && npm ci && npm run build && cd ..
firebase deploy --only hosting
```
Firebase rewrites don't proxy WebSockets, so the browser talks to Cloud Run directly.

## 4. Smoke tests
```bash
API=https://swasthyasync-api-PROJECT_NUMBER.asia-south1.run.app
curl -s -o /dev/null -w "%{http_code}\n" $API/health            # 200
curl -s -o /dev/null -w "%{http_code}\n" $API/ready             # 200 when DB is reachable
curl -s $API/api/departments | head -c 200
curl -s -o /dev/null -w "%{http_code}\n" -H "Connection: Upgrade" -H "Upgrade: websocket" \
  -H "Sec-WebSocket-Version: 13" -H "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==" $API/ws/session   # 101
bash backend/scripts/smoke_auth.sh $API                           # protected endpoints → 401
```

**Fallback:** if Cloud Run won't deploy within 45 minutes, keep the frontend on Vercel with `VITE_API_URL` pointing at Cloud Run.
