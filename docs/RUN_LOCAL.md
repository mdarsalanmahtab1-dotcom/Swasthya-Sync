# Run SwasthyaSync locally (Windows + VS Code)

You need **Python 3.11+**, **Node.js 20+** and **Git**. Open VS Code's terminal with **Ctrl+`** (PowerShell).

## 1. Get the code
```powershell
git clone https://github.com/mdarsalanmahtab1-dotcom/Swasthya-Sync.git
cd Swasthya-Sync
git checkout google-migration
```

## 2. Backend (terminal 1)
```powershell
cd backend
python -m venv .venv
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
python -m playwright install chromium
Copy-Item .env.example .env
code .env
```
Fill these in `backend/.env` (never commit this file):

| Variable | Value |
|---|---|
| `GEMINI_API_KEY` | Your Gemini key (AI Studio) |
| `DATABASE_URL` | Supabase connection string. **Use port 6543** if your Wi-Fi blocks 5432 |
| `ADMIN_EMAIL` / `ADMIN_BOOTSTRAP_PASSWORD` | Admin panel login |
| `SARVAM_API_KEY` | Sarvam key, so voice input uses Sarvam first. Without it, Gemini Transcribe is used |
| `STT_PROVIDER` | `sarvam` (default: Sarvam first, Gemini backup) or `google` |
| `SPEECH_PROVIDER` | `google` (default: Gemini voice for questions) or `sarvam` |
| `VOICE_MODE` | `live` (default: Gemini Live voice, about 1–4 s per turn, automatic fallback to STT/TTS) or `legacy` |

Start the API:
```powershell
python -m uvicorn main:app --reload --host 127.0.0.1 --port 8000
```
Check http://127.0.0.1:8000/health (should say `healthy`) and http://127.0.0.1:8000/ready (should say `"db":"ok"`).

## 3. Frontend (terminal 2, click **+** in the terminal panel)
```powershell
cd frontend
npm ci
npm run dev
```
Open http://localhost:5173.

## 4. Logins for testing
| Screen | Login |
|---|---|
| Patient Kiosk | none |
| Doctor Dashboard | demo doctor from the seed data, e.g. `doctor1` |
| Nurse Triage | prompts for a staff login, e.g. `staff_nurse` |
| Hospital Admin | `ADMIN_EMAIL` / `ADMIN_BOOTSTRAP_PASSWORD` from `.env` |

## 5. Optional checks (from `backend/`, venv active)
```powershell
python scripts/smoke_speech.py       # Gemini TTS -> STT round trip in hi/bn/en
python scripts/eval_intake.py        # 10 synthetic patients -> docs/eval_results.md
```

## Troubleshooting
- **Doctors don't load / "Database unavailable":** open `/ready`. If it says `no pool`, your network is probably blocking port 5432. Change the port in `DATABASE_URL` to `6543`, or switch networks. The backend retries every 20 s.
- **`Activate.ps1` cannot be loaded:** run the `Set-ExecutionPolicy -Scope Process ...` line again in that terminal.
- **Port already in use:** close the other terminal running uvicorn or Vite, or run `npx kill-port 8000 5173`.
- **Gemini Live:** the kiosk speaks and listens through Gemini Live while you hold **Hold to Speak**. If Live fails (for example on bad Wi-Fi), it switches to Gemini STT/TTS automatically and retries Live once at the next question. Set `VITE_VOICE_MODE=legacy` in `frontend/.env.local` to turn Live off in the browser.
- **Voice is slow:** each Gemini speech call can take several seconds on weak Wi-Fi. Sarvam (`STT_PROVIDER=sarvam` with a key) is usually faster for input.
