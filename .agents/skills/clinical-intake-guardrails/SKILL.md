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
