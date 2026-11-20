# SwasthyaSync → Google Cloud migration plan (for Claude Code)

> **How to use this file:** put it in the repo root, open Claude Code in the repo and say:
> *"Read SWASTHYASYNC_GOOGLE_MIGRATION.md fully. Then do Phase 0 and stop for my confirmation. After that, do one phase at a time, run the checks at the end of each phase, commit, and tell me what changed before starting the next."*

---

## 0. Context for Claude Code (read first)

We are a student team at the **Google Startup Buildathon India 2026** (city round, 20–21 Nov). The submission deadline is **9:00 AM on 21 Nov** and it will not be extended. Judges score **"Use of Google AI & Google Cloud"** and **"Technical architecture"**, so the product must run on Google Cloud with Google AI at its core. We are beginners at cloud and security work: **explain each change in one or two plain sentences** when you finish a step.

**The goal is to move the existing, working product onto Google Cloud, replace Sarvam with Google speech models, and fix the critical security and clinical-safety issues. Do NOT rewrite the product, add new features, or redesign the UI.**

### Ground rules
1. Work on a new git branch `google-migration`. Commit after every step that works, using a clear message.
2. **Never** print, log, commit or hard-code secrets. Never edit or read `.env` files aloud; only edit `.env.example`.
3. **Ask me before running any command that creates, changes, deletes or bills cloud resources** (`gcloud ... create/delete/deploy`, `firebase deploy`, IAM changes). Read-only commands (`gcloud ... list/describe`) are fine.
4. Model APIs changed recently. **Before writing code against a Google API, read the current official docs** (ai.google.dev, cloud.google.com). Don't rely on memory for model IDs, SDK method names or audio formats.
5. Keep every existing feature working: the kiosk interview, OCR, AYUSH mode, triage queue, doctor dashboard, admin panel and the PDF casesheet. If a change risks breaking the demo, make it switchable with an env var and default to the safe option.
6. Only use **synthetic (made-up) patient data** in tests and seed scripts.
7. **Ignore `PatientPortal/`** (a separate app) unless I ask. Ignore `ui-ux-pro-max-skill/`, `docs/`, the `test_*.py`/`verify_*.py` scripts, and `backend/test_files/`.

---

## 1. What exists today (from reading the repo and the live site)

| Part | Today | Where in code |
|---|---|---|
| Kiosk + doctor + nurse + admin web app | React 19 + Vite + TS on **Vercel** (`swasthya-sync.vercel.app`) | `frontend/` (`vercel.json`, `.env.production`, `src/config.ts`) |
| API + WebSocket interview | FastAPI on **Railway** (`swasthyasync-production-...up.railway.app`), with a Cloud Run-ready `Dockerfile` already | `backend/main.py`, `routes_extended.py`, `routes_admin.py` |
| Interview engine | Two-stage pipeline: schema generation once, then extract → select field → next question loop. It also has a macro FSM, a deterministic red-flag watchdog, a safety floor and fork questions. **This design is good; keep it.** | `dialogue_manager.py`, `schema_generator.py`, `conversation_engine.py`, `field_selector.py`, `red_flag_library.py`, `safety_watchdog.py` |
| LLM | `google-genai` SDK with a Gemini API key. Model lists are **hard-coded in 3 places** and include `gemini-3.6-flash` (retired on Vertex on 19 Nov 2026) | `llm_client.py`, `schema_generator.py` (l.30–33), `ocr_pipeline.py` (l.27–30) |
| Speech | **Sarvam** `saaras:v3` (STT) and `bulbul:v3` (TTS), called server-side via `/api/stt` and `/api/tts`. TTS falls back to scraping `translate.google.com/translate_tts` (unofficial, and it sends patient text to an unsupported endpoint) | `sarvam_client.py`, `main.py` l.373–450; frontend `hooks/useSarvamSTT.ts` (records `audio/webm;codecs=opus`), `hooks/useSarvamTTS.ts` (plays WAV) |
| Database | **Supabase Postgres** via `asyncpg` (`SUPABASE_DB_URL`). Plain Postgres, so it moves easily | `database.py`, schema in `migrate_to_supabase.py`, seed in `seed_judge_demo_data.py` |
| File storage | **Supabase Storage** bucket `patient-records`, 30-day signed URLs, fallback to local `uploads/` which is **publicly mounted** at `/uploads` | `main.py` `/api/ocr`, `/api/ocr/batch`; `pdf_generator.py` |
| PDF | Playwright headless Chromium (already installed in the Dockerfile) | `pdf_generator.py` |
| Session state | `DialogueManager` objects are kept **in memory** in `sessions` dict (`main.py` l.55) | `main.py` |

### Problems found that must be fixed (ordered by severity)
1. **Patient data APIs are open to anyone.** `routes_admin.py` (`/api/admin/*`: `export-db`, `patients`, `reset-clinic`, `doctors` CRUD, `rules`, `logs`) has **no auth at all**. Also open: `/api/record/{id}`, `/api/session/{id}`, `/api/triage/queue`, `/api/queue`, `/api/pdf/{id}`, `/api/summary/{id}/pdf`, `/api/patient/{id}/summaries`, `/api/ocr-audit/{id}`, doctor status/instructions, notifications, and the static `/uploads` mount.
2. **Admin login is client-side only.** `frontend/src/screens/AdminPanel.tsx` compares against hard-coded `ADMIN_EMAIL`/`ADMIN_PASS` in the JS bundle and stores `swasthya_admin_auth = 'true'`. The frontend sends **no `Authorization` header** anywhere.
3. **Passwords:** the `doctors.password` column is plaintext; staff use unsalted SHA-256; session tokens never expire.
4. **Fake clinical data on failure:** `llm_client._mock_doc_extract` returns made-up medicines (Paracetamol, Amlodipine) and lab values, and `_mock_clinical_summary` returns a generic summary, whenever Gemini fails. In a health product, **silently inventing clinical data is dangerous**.
5. **Patient name is sent to the LLM** (`conversation_engine.generate_opening_question`, l.437–457), yet the landing page claims "All patient identifiers are anonymized during LLM processing".
6. `CORS allow_origins=["*"]` (the `CORS_ORIGINS` env var in `render.yaml` is ignored).
7. Transcripts and TTS text are written to logs (`main.py` STT/TTS endpoints).
8. The landing page over-claims: "ISO 27001 Architecture", "End-to-End Encryption", "ABDM & DPDP Compliant", "TLS 1.3/AES-256". It also says "Built for SIH 2025".

---

## 2. Target architecture (what we are moving to)

| Part | Move to | Notes |
|---|---|---|
| Backend API + WebSocket | **Cloud Run** service `swasthyasync-api`, region **asia-south1** (Mumbai), built from `backend/Dockerfile` | Because sessions are in memory: `--max-instances 1 --min-instances 1 --session-affinity --timeout 3600 --no-cpu-throttling`. Use `--memory 2Gi --cpu 2` (Playwright) |
| Frontend | **Firebase Hosting** (Google), SPA rewrite to `index.html` | Calls the Cloud Run URL directly via `VITE_API_URL` / `VITE_WS_URL`. Firebase rewrites don't proxy WebSockets, so don't proxy `/ws` |
| LLM | Gemini via `google-genai`, model IDs from **env vars** in one place | Use a billing-linked key (paid tier, so prompts aren't used to improve Google products). Optional switch to Agent Platform (Vertex) with the Cloud Run service account and no key |
| STT | **`gemini-3.5-transcribe`** | Browser sends webm/opus. Check in the docs whether that's accepted; if not, convert with ffmpeg to 16 kHz mono WAV |
| TTS | **`gemini-3.8-flash-lite-tts`** (or `gemini-3.8-flash-tts`) | Check the output format in the docs (likely raw PCM). Wrap it in a WAV header because the frontend plays `audio/wav` |
| Sarvam | Kept only as an optional fallback behind `SPEECH_PROVIDER=sarvam` | Default `google` |
| Database | **AlloyDB Express** (PostgreSQL 18, preview) in asia-south1, with **fallback: keep Supabase** if the connection isn't working by the cut-off | Read the DSN from `DATABASE_URL` (keep `SUPABASE_DB_URL` as a backward-compatible fallback) |
| Files | **Cloud Storage** private bucket in asia-south1 | Serve files through an authenticated backend endpoint; no public URLs, no 30-day links |
| Secrets | **Secret Manager** → injected into Cloud Run with `--set-secrets` | `gemini-api-key`, `database-url`, `fast2sms-api-key` (optional), `sarvam-api-key` (optional), `admin-bootstrap-password` |
| Coding guardrails | Agent Skill `.agents/skills/clinical-intake-guardrails/SKILL.md` + root `CLAUDE.md` pointing to it | The event judges Agent Skills / Antigravity usage |

```
Patient kiosk / Doctor / Nurse / Admin  (Firebase Hosting, React)
        │ HTTPS REST + WSS (/ws/session)
        ▼
Cloud Run: swasthyasync-api (FastAPI, asia-south1) ── Secret Manager
   ├── Gemini 3.5 Flash-Lite / 3.8 Flash  (schema, extraction, questions, summary, OCR)
   ├── Gemini 3.5 Transcribe (speech → text)   ├── Gemini 3.8 TTS (text → speech)
   ├── AlloyDB Express (Postgres)              └── Cloud Storage (patient documents)
```

---

## 3. Phases (do them in this order; time budget in brackets)

### Phase 0: Recon and guardrails (15 min). Then STOP and report.
1. Create branch `google-migration`.
2. Create `.agents/skills/clinical-intake-guardrails/SKILL.md` with the content in **Appendix A**. `.gitignore` currently ignores `.agents/`: change it so this skill folder **is** committed (e.g. add `!.agents/skills/clinical-intake-guardrails/`), without un-ignoring anything else.
3. Create a root `CLAUDE.md`: "Always follow `.agents/skills/clinical-intake-guardrails/SKILL.md` and `SWASTHYASYNC_GOOGLE_MIGRATION.md`. Never print secrets."
4. Search git history for leaked secrets: `git log -p --all -S "AIza"`, and the same for `SARVAM`, `postgresql://postgres:`, `FAST2SMS`, `supabase.co` with a key. **Only report which file and commit, never the value.** If anything is found, tell me which keys I must rotate.
5. Report back: a short list of the files you will touch in Phases 1–7, and anything in this plan that doesn't match the code.

### Phase 1: Central model config + remove fake clinical data (45 min). Priority: MUST
1. Create `backend/model_config.py` that reads from env with these defaults, and use it everywhere models are listed (`llm_client.py`, `schema_generator.py`, `ocr_pipeline.py`):
   - `GEMINI_FAST_MODELS` = `gemini-3.5-flash-lite,gemini-flash-lite-latest`
   - `GEMINI_REASONING_MODELS` = `gemini-3.8-flash,gemini-3.5-flash-lite`
   - `GEMINI_VISION_MODELS` = `gemini-3.8-flash,gemini-3.5-flash-lite`
   - `GEMINI_STT_MODEL` = `gemini-3.5-transcribe`
   - `GEMINI_TTS_MODEL` = `gemini-3.8-flash-lite-tts`

   Remove `gemini-3.6-flash`, `gemini-2.5-*` and `gemini-1.5-*` from all lists and comments. **Before choosing the defaults, check the current model list in the Gemini API docs**, and add a startup log line listing which configured models the API key can actually see (`client.models.list()`, tolerant of failure).
2. Client creation in one function: if `GEMINI_API_KEY` is set, use `genai.Client(api_key=...)`. Else, if `GOOGLE_GENAI_USE_VERTEXAI=true`, use `genai.Client()` (reads `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION`, using the Cloud Run service account; no key). Make OCR and schema generation use the same client.
3. **Replace the fake-data fallbacks**:
   - `_mock_doc_extract` must return empty lists plus `"ai_unavailable": true`.
   - `_mock_clinical_summary` must return `"clinical_narrative": "AI summary unavailable — please review the raw Q&A below."` with `ai_unavailable: true`.
   - Make the doctor dashboard show a visible "AI unavailable — verify manually" badge when this flag is present.
   - The keyword-classifier and generic-question fallbacks may stay; they don't invent clinical facts.
4. Where a call returns a fixed JSON shape (schema generation, field extraction, document entities, clinical summary), add a `response_json_schema` (or Pydantic `response_schema`) in `GenerateContentConfig` if the installed `google-genai` version supports it. Keep `response_mime_type="application/json"`. Pin `google-genai` to the current version in `requirements.txt`.
5. **Patient name out of the LLM:** in `generate_opening_question`, don't pass the name to the model. Ask the model to put the literal placeholder `{{NAME}}` where the name goes, then substitute it in Python. Check that no other prompt receives name, phone, ABHA number or address (grep the prompt builders).

✅ **Checks:** `python -c "import main"` succeeds from `backend/`; grep shows no `gemini-3.6`, `gemini-2.5` or `gemini-1.5` in `backend/*.py` (excluding tests); a unit-style call of the mock functions shows no invented medicines.

### Phase 2: Google speech instead of Sarvam (1.5–2 h). Priority: MUST
1. Create `backend/speech_provider.py` with an interface (`speech_to_text(audio_bytes, hint_language, fmt) -> dict` and `text_to_speech(text, language_code, speaker=None) -> bytes`). The return shapes must be **exactly** what `sarvam_client` returns today: STT returns `{"transcript", "language_code", "language_name", optional "error"}`, TTS returns WAV bytes. That way `main.py` and the frontend don't change.
   - `GoogleSpeechProvider` uses `GEMINI_STT_MODEL` and `GEMINI_TTS_MODEL`. **Read the official docs pages for Gemini 3.5 Transcribe and Gemini 3.8 TTS first**, for the request shape, supported input audio MIME types, language-code format and TTS output format and sample rate.
   - **Audio in:** the browser sends `audio/webm;codecs=opus` (or mp4/ogg on Safari/Firefox). If the transcribe model doesn't accept that format, add `ffmpeg` to the Dockerfile apt line and convert to 16 kHz mono WAV in memory before sending. Reject audio over 2 MB or 60 s.
   - **Language:** pass the kiosk-selected language (`hi-IN`, `bn-IN`, ...) as a hint. Map the detected language back to the same BCP-47 codes used in `LANGUAGE_NAMES`.
   - **Audio out:** if TTS returns raw PCM, wrap it in a proper WAV header (sample rate and bit depth from the docs; use the stdlib `wave` module). Chunk long text the way `sarvam_client._chunk_text` does, and concatenate the PCM before wrapping.
   - Map our 11 languages to TTS voices. If a voice or language isn't supported, fall back to English and log a warning without the text.
   - `SarvamSpeechProvider` wraps the existing `sarvam_client` functions. **Delete the `translate.google.com` scraping fallback** (`_google_tts_fallback`) entirely.
   - Choose the provider via `SPEECH_PROVIDER` (`google` default, `sarvam` optional). If the Google provider throws and a `SARVAM_API_KEY` exists, fall back to Sarvam; otherwise return the existing error shapes (503 for TTS).
2. Update `main.py` `/api/stt` and `/api/tts` to call `speech_provider`. Update their docstrings and comments, and **stop logging transcript or TTS text** (log sizes and timings only).
3. Frontend: no behaviour change needed. Optionally rename the hooks later; don't rename now.
4. Add `backend/scripts/smoke_speech.py`. It runs TTS → STT round-trip for Hindi, Bengali and English with one synthetic medical sentence each, and prints timing plus whether the transcript roughly matches. Run it locally with my key and show me the output.

✅ **Checks:** run the smoke script; run the backend locally and do one kiosk turn by voice in Hindi from the browser at `http://localhost:5173`.

### Phase 3: Deploy to Cloud Run + Firebase Hosting (1–1.5 h). Priority: MUST
**Backend config changes first:**
1. CORS: read `CORS_ORIGINS` (comma-separated). Default to `http://localhost:5173,http://127.0.0.1:5173`. In production we'll set it to the Firebase URLs (`https://PROJECT_ID.web.app,https://PROJECT_ID.firebaseapp.com`).
2. `database.py`: read `DATABASE_URL`, falling back to `SUPABASE_DB_URL`.
3. Make sure the app listens on `$PORT` (Cloud Run sets 8080; the Dockerfile CMD already uses `${PORT}`). Keep `/health` cheap with no DB call. Add `/ready`, which checks the DB pool.
4. Create `backend/.dockerignore` (`.env`, `__pycache__`, `uploads/`, test PDFs, `*.ogg`, `test_files/`).

**Deployment files** (create them; I will run the commands or approve you running them):
5. `deploy/README_DEPLOY.md` with the exact commands below, filled with placeholders, and short explanations.
6. `deploy/deploy_backend.sh`:
   ```bash
   gcloud run deploy swasthyasync-api --source backend --region asia-south1 \
     --allow-unauthenticated --port 8080 \
     --max-instances 1 --min-instances 1 --session-affinity --timeout 3600 \
     --cpu 2 --memory 2Gi --no-cpu-throttling \
     --set-secrets GEMINI_API_KEY=gemini-api-key:latest,DATABASE_URL=database-url:latest \
     --set-env-vars "^@^SPEECH_PROVIDER=google@CORS_ORIGINS=https://PROJECT_ID.web.app,https://PROJECT_ID.firebaseapp.com@GCS_BUCKET=PROJECT_ID-patient-docs"
   ```
   The `^@^` prefix changes gcloud's separator to `@`, so values that contain commas (like `CORS_ORIGINS` or the model lists) aren't split. Explain in comments why there is one instance (in-memory sessions and WebSocket) and why the timeout is 3600 (WebSocket interviews).
7. One-time setup commands in the deploy README (for me to run):
   ```bash
   gcloud config set project PROJECT_ID
   gcloud services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
     secretmanager.googleapis.com storage.googleapis.com alloydb.googleapis.com aiplatform.googleapis.com firebase.googleapis.com
   printf '%s' "PASTE_KEY" | gcloud secrets create gemini-api-key --data-file=-
   printf '%s' "postgresql://USER:PASS@HOST:5432/DB" | gcloud secrets create database-url --data-file=-
   # give Cloud Run's service account access to secrets (default compute SA unless we create one)
   gcloud projects add-iam-policy-binding PROJECT_ID \
     --member=serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com \
     --role=roles/secretmanager.secretAccessor
   ```
8. **Frontend:**
   - `frontend/src/config.ts`: remove the hard-coded Railway `FALLBACK_PROD_BACKEND`. In production, **require** `VITE_API_URL` and derive the WS URL from it (or from `VITE_WS_URL`). Keep the localhost and LAN logic.
   - Replace `frontend/.env.production` values with the Cloud Run URL placeholder (no secrets live there; it's fine to commit).
   - Add `firebase.json` (hosting `public: frontend/dist`, SPA rewrite `** → /index.html`, cache headers for `/assets/**`) and `.firebaserc` with a `PROJECT_ID` placeholder.
   - Remove the Railway proxy from `frontend/vercel.json` (keep the file so the old deploy doesn't break, or delete it if I confirm).
   - Deploy commands for the README: `npm i -g firebase-tools && firebase login && firebase projects:addfirebase PROJECT_ID`, then `cd frontend && npm ci && npm run build && cd .. && firebase deploy --only hosting`.
9. After the deploy (I'll paste you the URLs), smoke-test with curl: `/health`, `/api/departments`, and a WebSocket handshake to `/ws/session`. Then I test the kiosk in a browser.

✅ **Checks:** `npm run build` passes; `docker build backend` works locally if Docker is available (otherwise rely on Cloud Build); the deployed `/health` returns 200; a full kiosk interview works on the Firebase URL.

**Fallback:** if the Cloud Run deploy fails for more than 45 minutes, deploy only the backend to Cloud Run, temporarily keep the frontend on Vercel pointing to Cloud Run, and tell me.

### Phase 4: Critical security fixes (1.5–2 h). Priority: MUST (judges will ask)
Keep the kiosk patient flow working **without** login (it's a walk-in kiosk). Protect everything staff-facing.
1. **Real server-side auth for staff:**
   - `/api/doctor/login`, `/api/auth/staff/login` and a new `/api/admin/login` all issue a token via the existing `_issue_token` (store it in `staff_sessions`).
   - Add a `created_at` column (`ALTER TABLE ... ADD COLUMN IF NOT EXISTS`) and expire tokens after 12 hours in `_get_current_staff`.
   - Use secure random tokens (`secrets.token_urlsafe(32)`) instead of `role:id:hex`; keep the role in the DB row, not in the token.
2. **Passwords:** hash with the stdlib `hashlib.scrypt` plus a random salt, stored as `scrypt$salt$hash`. On successful login with a legacy plaintext or SHA-256 password, re-hash and upgrade it in place. Update `seed_judge_demo_data.py` and `routes_admin` doctor create/update to store hashes. The admin account comes from env `ADMIN_EMAIL` plus secret `ADMIN_BOOTSTRAP_PASSWORD`, created or updated in the `staff` table at startup.
3. **Protect routes** with the existing `require_role` checker:
   - `admin_router`: router-level dependency `require_role("ADMIN")`.
   - Doctor/nurse read and write endpoints (`/api/triage/queue`, `/api/queue`, `/api/queue/{id}/status`, `/api/session/{id}` GET, `/api/session/{id}/nurse-triage`, `/api/session/{id}/complete`, `/api/pdf/{id}`, `/api/summary/{id}/pdf`, `/api/patient/{id}/summaries`, `/api/ocr-audit/{id}`, `/api/record/{id}`, `/api/record/{id}/timeline`, doctor status/instructions/notify, notifications): `require_role("DOCTOR","NURSE","ADMIN")` as appropriate.
   - **Before protecting any endpoint, grep the frontend to see which screen calls it.** If the kiosk itself calls it (e.g. `/api/queue/token/{sessionId}`, `/api/session/{id}/submit`, `/api/ocr/*`, `/api/session/start`), keep it open but scoped by the unguessable session UUID.
4. **Frontend:** create `src/lib/api.ts` with an `apiFetch(path, init)` that adds `Authorization: Bearer <token>` from `localStorage` (`swasthya_doctor_auth` / admin / nurse token) and redirects to login on 401. Replace staff-screen `fetch` calls with it (`DoctorQueue`, `DoctorDashboard`, `Dashboard_Triage`, `AdminPanel`, `AnalyticsDashboard`). **Remove the hard-coded `ADMIN_EMAIL`/`ADMIN_PASS`** from `AdminPanel.tsx`; use `/api/admin/login`.
5. Remove the public `app.mount("/uploads", ...)`. Files are served by Phase 5's authenticated endpoint. Keep `/static` only if it doesn't hold patient data; otherwise protect it too.
6. Add upload limits: max 2 MB for STT, max 10 MB per image and 10 images for OCR, with allowed content types only. Return 413/415 otherwise.
7. Add a simple in-memory rate limit on `/api/stt`, `/api/tts`, the OTP endpoints and the login endpoints (per IP, e.g. 30 requests/min; 5 logins/min).
8. Make sure no handler logs patient text, names, phone numbers or ABHA numbers. Replace those log lines with IDs, sizes and timings.
9. Error responses must not include stack traces.

✅ **Checks:** `curl https://API/api/admin/export-db` returns 401 without a token. Doctor login → queue → encounter → PDF still works with a token. A full kiosk interview still works with no login. Write `backend/scripts/smoke_auth.sh`, which checks five protected endpoints return 401 and two kiosk endpoints still return 200.

### Phase 5: Cloud Storage for documents (45 min). Priority: SHOULD
1. Add `google-cloud-storage` to `requirements.txt`. Create `backend/storage.py` with `upload_bytes(path, data, content_type) -> gs_path`, `download_bytes(gs_path) -> bytes`, and `delete(gs_path)`. The bucket comes from `GCS_BUCKET`; credentials come from the Cloud Run service account (ADC).
2. Replace the Supabase Storage upload blocks in `main.py` (`/api/ocr`, `/api/ocr/batch`). Store `gs://...` paths in the DB, not signed URLs.
3. Add `GET /api/documents/{doc_id}` (staff auth required) that streams the file from GCS. Update the frontend wherever it shows uploaded document images to use this endpoint via `apiFetch` (blob → object URL).
4. Update `pdf_generator.py` so it can load images from `gs://` paths via `storage.download_bytes` (inline them as data URIs for Playwright).
5. If `GCS_BUCKET` isn't set (local dev), fall back to a local `uploads/` folder that is **not** publicly mounted.
6. Commands for the deploy README:
   ```bash
   gcloud storage buckets create gs://PROJECT_ID-patient-docs --location=asia-south1 \
     --uniform-bucket-level-access --public-access-prevention
   gcloud storage buckets add-iam-policy-binding gs://PROJECT_ID-patient-docs \
     --member=serviceAccount:PROJECT_NUMBER-compute@developer.gserviceaccount.com --role=roles/storage.objectAdmin
   ```

✅ **Checks:** upload a synthetic prescription image in the kiosk → it appears in the bucket → the doctor sees it → the PDF includes it.

### Phase 6: AlloyDB Express instead of Supabase Postgres (1 h, hard cut-off). Priority: SHOULD
**Cut-off:** if this isn't working within 1 hour, or by 6:00 PM on 20 Nov, keep Supabase via `DATABASE_URL`, mention AlloyDB as roadmap, and move on.
1. I'll create the AlloyDB Express instance in the Cloud Console (AlloyDB → AlloyDB Express card → Create, region asia-south1) and give you the connection details. Ask me for them; don't create the instance yourself.
2. Read the current AlloyDB Express docs to decide how Cloud Run should connect:
   - **(a)** a direct Postgres DSN (with SSL) in `DATABASE_URL` if Express gives a public endpoint with password auth; or
   - **(b)** the official `google-cloud-alloydb-connector` with asyncpg, enabled by env `ALLOYDB_INSTANCE_URI` (read its README for the exact asyncpg pool pattern).

   Implement whichever works in `database.init_db_pool()`, keeping the plain-DSN path as default.
3. Create `backend/scripts/init_schema.py`, which takes the `CREATE TABLE` statements from `migrate_to_supabase.py` plus the startup `ALTER TABLE` patches (and the new `staff_sessions.created_at`), and is idempotent (`IF NOT EXISTS`). **Don't migrate real Supabase data.** Seed fresh synthetic data with `seed_judge_demo_data.py` (now using hashed passwords).
4. AlloyDB Express pauses after about 15 minutes idle. Make `init_db_pool` retry with backoff for up to 30 s on startup, and make DB calls retry once on connection errors.
5. Grant the Cloud Run service account the roles AlloyDB needs (per the docs: likely `roles/alloydb.client` and `roles/serviceusage.serviceUsageConsumer`). Put these commands in the deploy README.

✅ **Checks:** `/ready` is OK on Cloud Run; a kiosk session row appears in AlloyDB; doctor queue loads; admin analytics loads.

### Phase 7: Honest copy, docs and a small evaluation (45 min). Priority: MUST for the pitch
1. Landing page (`frontend/src/...` wherever the home/landing content is), "Privacy & Data Security" and footer:
   - Replace "All patient identifiers are anonymized during LLM processing" → "Patient name, phone and ABHA number are never sent to the AI model."
   - Replace "ISO 27001 Architecture", "End-to-End Encryption", "ABDM & DPDP Compliant" → "Designed to align with DPDP Act 2023 & ABDM guidelines", "Encrypted in transit (HTTPS) and at rest (Google Cloud)", "Hosted in India (Mumbai region)".
   - "Built for SIH 2025" → "Built by Team CodeNode · Google Startup Buildathon 2026".
   - Keep the CDSS disclaimer.
2. Root `README.md`: update the tech-stack badges and architecture section to the Google stack (Gemini 3.5 Flash-Lite / 3.8 Flash, Gemini Transcribe + TTS, Cloud Run, Firebase Hosting, AlloyDB or Supabase, Cloud Storage, Secret Manager). Keep the rest. Add a "Security & privacy" section listing **what we actually do**.
3. `docs/ARCHITECTURE_GOOGLE.md`: an ASCII diagram (Section 2 above), a table of Google services and why we use each, and the data-flow for one patient turn.
4. `backend/scripts/eval_intake.py`: run 10 **synthetic** patient scripts (text answers, mixed Hindi/Bengali/English, including 3 with red flags such as chest pain with sweating, stroke signs, or bleeding in pregnancy) through `DialogueManager` without audio. Print: % of safety-floor fields filled, red flags caught (x/3), red flags falsely raised, average turns, and average latency per turn. Save the results to `docs/eval_results.md`. These numbers go in our pitch.

✅ **Checks:** `npm run build` passes; the eval script runs end-to-end and writes results.

---

## 4. Environment variables (final list for `.env.example` and Cloud Run)

| Var | Where | Example / default |
|---|---|---|
| `GEMINI_API_KEY` | Secret Manager `gemini-api-key` | — |
| `GOOGLE_GENAI_USE_VERTEXAI` / `GOOGLE_CLOUD_PROJECT` / `GOOGLE_CLOUD_LOCATION` | env (optional, keyless mode) | `false` / project / `asia-south1` (check model availability per region) |
| `GEMINI_FAST_MODELS`, `GEMINI_REASONING_MODELS`, `GEMINI_VISION_MODELS`, `GEMINI_STT_MODEL`, `GEMINI_TTS_MODEL` | env | see Phase 1 |
| `SPEECH_PROVIDER` | env | `google` |
| `SARVAM_API_KEY` | Secret (optional) | — |
| `DATABASE_URL` (fallback `SUPABASE_DB_URL`) | Secret `database-url` | `postgresql://...` |
| `ALLOYDB_INSTANCE_URI` | env (only for connector mode) | `projects/.../locations/asia-south1/clusters/.../instances/...` |
| `GCS_BUCKET` | env | `PROJECT_ID-patient-docs` |
| `CORS_ORIGINS` | env | Firebase URLs |
| `ADMIN_EMAIL` / `ADMIN_BOOTSTRAP_PASSWORD` | env / Secret | — |
| `OTP_PROVIDER` etc. | env | keep `mock` for the demo |
| `VITE_API_URL`, `VITE_WS_URL` | `frontend/.env.production` | Cloud Run URL, `wss://.../ws/session` |

Remove `SUPABASE_URL` / `SUPABASE_KEY` usage once Phase 5 is done (keep `supabase` in requirements only if something else still imports it; otherwise remove it).

---

## 5. Definition of done (check before 9 PM on 20 Nov)
- [ ] Frontend on Firebase Hosting, backend on Cloud Run (asia-south1), and the old Railway URL is no longer used anywhere (`grep -r railway` is clean except docs).
- [ ] A voice interview in Hindi and Bengali works end-to-end on the deployed URL with Google speech models.
- [ ] Red flag → urgent in the doctor queue still works.
- [ ] `/api/admin/export-db` and the other staff endpoints return 401 without a token; the admin password isn't in the JS bundle (`grep -r ADMIN_PASS frontend/dist` is empty).
- [ ] No secrets in git; no patient text in logs.
- [ ] The AI-unavailable path shows a badge and never invents data.
- [ ] `docs/eval_results.md` exists with numbers.
- [ ] README, architecture doc and landing-page copy are honest and updated.

---

## Appendix A: `.agents/skills/clinical-intake-guardrails/SKILL.md`

```markdown
---
name: clinical-intake-guardrails
description: Clinical-safety, privacy and security rules for the SwasthyaSync OPD intake kiosk. Use whenever changing interview prompts, schema generation, red-flag logic, OCR/document extraction, patient data storage, auth, logging, or anything shown to patients or doctors.
---
# Clinical safety
1. The AI never diagnoses, prescribes or gives treatment advice to the patient. It asks questions and drafts a summary for a doctor to confirm.
2. Safety-floor fields (red_flag_library.get_safety_floor) must always be in every schema. Never remove them to save time.
3. Red flags are detected by deterministic rules in code (red_flag_library / safety_watchdog) AND the model. Never make red-flag detection depend on the LLM alone.
4. If an AI call fails, show "AI unavailable — verify manually". Never return mock or invented medicines, lab values or diagnoses.
5. Keep the patient's own words (verbatim) next to each extracted field so the doctor can verify.

# Privacy
6. Never send patient name, phone number, ABHA number or address to an LLM. Use placeholders such as {{NAME}} and substitute in code.
7. Never log transcripts, TTS text, names, phone numbers, ABHA numbers or document contents. Log IDs, sizes and timings only.
8. Use only synthetic patients in tests, seeds, evals and demos.
9. Patient files live in a private Cloud Storage bucket and are served only through authenticated endpoints. No public URLs.

# Security
10. Secrets come from environment variables / Secret Manager only. Never hard-code keys or passwords, especially in frontend code.
11. Every staff/admin endpoint requires a server-validated token (require_role). Client-side-only auth is not allowed.
12. Passwords are stored salted and hashed (scrypt). SQL is always parameterized ($1, $2 ...).
13. Upload size and type limits on every file/audio endpoint. No stack traces in error responses.

# Google stack
14. Model IDs come from backend/model_config.py env vars. Do not hard-code model names elsewhere. Do not use retired models (gemini-1.5-*, gemini-2.5-*, gemini-3.6-flash).
15. Deploy target: Cloud Run (asia-south1) for the API, Firebase Hosting for the frontend.
```
