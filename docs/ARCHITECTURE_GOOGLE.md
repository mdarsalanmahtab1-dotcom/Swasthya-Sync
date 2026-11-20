# SwasthyaSync on Google Cloud

```
Patient kiosk / Doctor / Nurse / Admin  (Firebase Hosting, React)
        │ HTTPS REST + WSS (/ws/session)
        ▼
Cloud Run: swasthyasync-api (FastAPI, asia-south1) ── Secret Manager
   ├── Gemini 3.5 Flash-Lite / 3.8 Flash  (schema, extraction, questions, summary, OCR)
   ├── Gemini 3.5 Transcribe (speech → text)   ├── Gemini 3.8 TTS (text → speech)
   ├── AlloyDB / Postgres (asyncpg)            └── Cloud Storage (patient documents)
```

## Google services and why we use each

| Service | What it does for us | Why |
|---|---|---|
| **Gemini 3.5 Flash-Lite** (`GEMINI_FAST_MODELS`) | Per-turn field extraction, next question, complaint classification | Low latency for a live kiosk conversation |
| **Gemini 3.8 Flash** (`GEMINI_REASONING_MODELS`, `GEMINI_VISION_MODELS`) | One-time interview schema, doctor handover summary, prescription/lab OCR | Better clinical reasoning and vision; called only a few times per patient |
| **Gemini 3.5 Transcribe** (`GEMINI_STT_MODEL`) | Patient voice → text in 11 Indian languages | Takes the browser's webm/opus audio directly; we pass the kiosk language as a hint |
| **Gemini 3.8 Flash-Lite TTS** (`GEMINI_TTS_MODEL`) | Reads each question aloud | Many languages from a single voice; returns 24 kHz PCM, which we wrap as WAV |
| **Cloud Run** (asia-south1) | Hosts the FastAPI API and WebSocket interview | Serverless containers, India region, WebSockets supported; one warm instance because sessions live in memory |
| **Firebase Hosting** | Serves the React app | Global CDN with SPA rewrites; the app talks to Cloud Run directly (also for WebSockets) |
| **Secret Manager** | Gemini key, DB URL, admin bootstrap password | Secrets are injected at deploy time and never stored in the image or git |
| **Cloud Storage** (private bucket) | Uploaded prescriptions/lab reports, casesheet PDF copies | Uniform access and public-access prevention; files are served only via an authenticated endpoint |
| **AlloyDB for PostgreSQL** (or Supabase Postgres) | Patients, sessions, queue, summaries, staff | Plain Postgres through `asyncpg`; DSN or the AlloyDB connector (`ALLOYDB_INSTANCE_URI`) |

## Data flow for one patient turn

1. The kiosk records the patient's answer and sends the audio (webm/opus, max 2 MB) to `POST /api/stt`.
2. Cloud Run sends it to **Gemini Transcribe** with the kiosk language as a hint. The transcript is returned; only sizes and timings are logged.
3. The browser sends the text over the `/ws/session` WebSocket. `DialogueManager`, held in memory on the single Cloud Run instance:
   - **extracts** field values with Gemini Flash-Lite, keeping the patient's verbatim words next to each value;
   - runs the **deterministic red-flag watchdog**. An emergency is flagged by code rules, not only by the model;
   - **selects the next field** from the schema, which always includes the safety floor;
   - **asks the next question** with Gemini Flash-Lite. No name, phone or ABHA number is in the prompt.
4. The question text goes to `POST /api/tts`. **Gemini TTS** returns PCM, which is wrapped as WAV and played in the kiosk.
5. The session state is saved to Postgres. If a red flag fires, the patient is moved up to urgent in the doctor queue.
6. At the end, Gemini 3.8 Flash writes the doctor handover note from de-identified data. If the AI fails, the note says "AI unavailable — verify manually" and nothing is invented. Playwright renders the PDF casesheet.
