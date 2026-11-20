"""
SwasthyaSync — FastAPI Backend Server (Optimized)

Changes:
  - STT/TTS endpoints are now truly async (run blocking I/O in thread pool)
  - WebSocket handler times each step to identify bottlenecks
  - Added logging for every major operation

Endpoints:
  WebSocket /ws/session  — real-time conversation loop
  POST     /api/session   — create a new session
  POST     /api/ocr       — upload and process a document image
  GET      /api/record/{session_id} — get the patient record
  POST     /api/stt       — speech to text (Gemini Transcribe / Sarvam)
  POST     /api/tts       — text to speech (Gemini TTS / Sarvam)
"""

from __future__ import annotations
import json
import logging
import time
import asyncio
import base64
import sys

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())

from contextlib import asynccontextmanager
from typing import List
from dotenv import load_dotenv

load_dotenv()  # Load .env before anything else

from fastapi import Depends, FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, Form, Response, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import os

os.makedirs("uploads", exist_ok=True)

from dialogue_manager import DialogueManager
from ocr_pipeline import process_document
import speech_provider
import auth_utils
from routes_extended import extended_router, set_sessions_ref, escalate_queue_priority
from routes_admin import admin_router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

# In-memory session store (for hackathon; would be Redis/DB in production)
sessions: dict[str, DialogueManager] = {}

async def cleanup_stale_sessions():
    """Background task to remove sessions inactive for > 30 mins."""
    import time
    while True:
        try:
            await asyncio.sleep(60)  # Check every minute
            now = time.time()
            stale_keys = [
                sid for sid, dm in sessions.items()
                if (now - getattr(dm, 'last_active_time', now)) > 1800
            ]
            for sid in stale_keys:
                logger.info(f"Purging stale session: {sid}")
                sessions.pop(sid, None)
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Error in cleanup task: {e}")

@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info("SwasthyaSync backend starting...")
    # Log which configured Gemini models the credentials can see (background, never fails startup)
    import model_config
    asyncio.create_task(asyncio.to_thread(model_config.log_visible_models))
    from database import setup_database
    await setup_database()
    _pdf_engine = None
    try:
        from pdf_generator import pdf_engine as _pe
        _pdf_engine = _pe
        await _pdf_engine.start()
        logger.info("PDFEngine started.")
    except ImportError as e:
        logger.warning(f"PDFEngine unavailable (missing dependency: {e}). PDF generation disabled.")
    except Exception as e:
        logger.error(f"Failed to start PDFEngine: {e}")
        
    cleanup_task = asyncio.create_task(cleanup_stale_sessions())
    
    yield
    
    logger.info("SwasthyaSync backend shutting down.")
    cleanup_task.cancel()
    try:
        await cleanup_task
    except asyncio.CancelledError:
        pass
    if _pdf_engine is not None:
        try:
            await _pdf_engine.stop()
            logger.info("PDFEngine stopped.")
        except Exception as e:
            logger.error(f"Failed to stop PDFEngine: {e}")

app = FastAPI(
    title="SwasthyaSync API",
    description="AI-powered clinical history-taking engine",
    version="2.0.0",
    lifespan=lifespan,
)

# CORS — only the origins in CORS_ORIGINS (comma-separated). Production: the Firebase Hosting URLs.
CORS_ORIGINS = [o.strip() for o in os.environ.get(
    "CORS_ORIGINS", "http://localhost:5173,http://127.0.0.1:5173").split(",") if o.strip()]
app.add_middleware(
    CORSMiddleware,
    allow_origins=CORS_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from routes_extended import require_role  # server-side staff auth


@app.exception_handler(Exception)
async def _unhandled_error(request, exc):
    """Never return stack traces or internal error text to clients.

    This handler runs outside CORSMiddleware, so CORS headers are added here; otherwise the
    browser hides the real status and reports a generic network failure.
    """
    import asyncpg
    db_down = isinstance(exc, (TimeoutError, ConnectionError, OSError, asyncpg.PostgresConnectionError,
                               asyncpg.InterfaceError))
    logger.error(f"Unhandled error on {request.method} {request.url.path}: {type(exc).__name__}")
    status, body = (503, {"error": "Database unavailable — please try again"}) if db_down         else (500, {"error": "Internal server error"})
    headers = {}
    origin = request.headers.get("origin")
    if origin and origin in CORS_ORIGINS:
        headers = {"Access-Control-Allow-Origin": origin, "Access-Control-Allow-Credentials": "true", "Vary": "Origin"}
    return JSONResponse(status_code=status, content=body, headers=headers)
app.include_router(extended_router)
app.include_router(admin_router)
set_sessions_ref(sessions)  # Bridge: let routes_extended read live PatientRecord data

os.makedirs("static/prescriptions", exist_ok=True)
app.mount("/static", StaticFiles(directory="static"), name="static")
# /uploads is NOT mounted publicly: patient documents are served only via authenticated endpoints.


# ──────────────────────────────────────────────────────────────────────
# REST Endpoints
# ──────────────────────────────────────────────────────────────────────

@app.get("/")
@app.get("/health")
async def health_check():
    """Deployment health probe endpoint."""
    return {"status": "healthy", "service": "SwasthyaSync API", "version": "2.0.0"}


@app.get("/ready")
async def readiness_check():
    """Readiness probe: checks the database pool with a cheap query."""
    import database
    if not database._pool:
        return JSONResponse(status_code=503, content={"status": "not_ready", "db": "no pool"})
    try:
        async with database._pool.acquire() as conn:
            await conn.fetchval("SELECT 1")
        return {"status": "ready", "db": "ok"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"status": "not_ready", "db": type(e).__name__})

@app.post("/api/session")
async def create_session(
    clinic_mode: str = "allopathic",
    language: str = "en-IN",
):
    """Create a new patient session and return the initial UI state."""
    dm = DialogueManager(clinic_mode=clinic_mode, language=language)
    ui = dm.start_session()
    session_id = dm.record.session_id
    sessions[session_id] = dm
    return {"session_id": session_id, "ui": ui}


@app.get("/api/documents/{doc_id}")
async def get_document(doc_id: str, index: int = 0, _auth: dict = Depends(require_role("DOCTOR", "NURSE", "ADMIN"))):
    """Stream an uploaded patient document from private storage (staff only)."""
    import database, storage
    if not database._pool:
        raise HTTPException(status_code=503, detail="DB not ready")
    async with database._pool.acquire() as conn:
        row = await conn.fetchrow("SELECT file_path FROM uploaded_documents WHERE document_id = $1", doc_id)
    if not row or not row["file_path"]:
        raise HTTPException(status_code=404, detail="Document not found")
    paths = [p.strip() for p in row["file_path"].split(",") if p.strip()]
    if index < 0 or index >= len(paths):
        raise HTTPException(status_code=404, detail="Document not found")
    data = await asyncio.to_thread(storage.download_bytes, paths[index])
    if data is None:
        raise HTTPException(status_code=404, detail="File unavailable")
    return Response(content=data, media_type=storage.guess_content_type(paths[index]),
                    headers={"Cache-Control": "private, no-store"})


@app.get("/api/record/{session_id}")
async def get_record(session_id: str, _auth: dict = Depends(require_role("DOCTOR", "NURSE", "ADMIN"))):
    """Get the full patient record for a session."""
    dm = sessions.get(session_id)
    if not dm:
        return {"error": "Session not found"}
    return dm.get_record()


MAX_IMAGE_BYTES = 10 * 1024 * 1024
MAX_IMAGES = 10
ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/heif", "application/pdf"}


def _check_image(content_type: str | None, data: bytes) -> str:
    ctype = (content_type or "image/jpeg").lower()
    if ctype not in ALLOWED_IMAGE_TYPES:
        raise HTTPException(status_code=415, detail="Unsupported file type")
    if len(data) > MAX_IMAGE_BYTES:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    return ctype


@app.post("/api/ocr")
async def upload_document(
    file: UploadFile = File(...),
    session_id: str = Form(""),
):
    """Upload a document image for OCR processing."""
    image_bytes = await file.read()
    content_type = _check_image(file.content_type, image_bytes)
    logger.info(f"OCR upload: session={session_id} size={len(image_bytes)}B type={content_type}")
    
    import uuid
    import storage
    safe_filename = f"{uuid.uuid4().hex[:8]}_{storage._safe_name(file.filename or 'doc.jpg')}"
    # Private bucket (or non-public local folder in dev); no public or signed URLs.
    storage_path = await asyncio.to_thread(
        storage.upload_bytes, f"patient-documents/{safe_filename}", image_bytes, content_type)

    result = await process_document(image_bytes, filename=file.filename or "doc.jpg", media_type=content_type)
    result["image_url"] = storage_path

    # If we have a session, merge OCR entities into the patient record
    logger.info(f"upload_document received session_id: {session_id}, in sessions? {session_id in sessions}")
    if session_id and session_id in sessions:
        logger.info(f"Found session in upload_document: {session_id}")
        logger.info(f"Entities to append: meds={len(result.get('medications', []))}, diags={len(result.get('diagnoses', []))}")
        dm = sessions[session_id]
        from patient_record import DocumentExtraction
        import database
        
        # Save to DB via asyncpg
        meds = result.get("medications", [])
        labs = result.get("lab_values", [])
        doc_id = await database.save_uploaded_document(
            session_id=session_id,
            file_path=storage_path,
            document_type=result.get("document_type", "OTHER"),
            ocr_raw=result,
            medications=meds,
            labs=labs
        )
        
        doc_ext = DocumentExtraction(
            doc_id=doc_id,
            doc_type=result.get("document_type", "unknown"),
            ocr_path=storage_path,
            entities=[result],
        )
        dm.record.document_extractions.append(doc_ext)
        
        # Pre-calculate unverifiable values so they can be shown in Screen 6
        from document_red_flags import check_document_flags
        check_document_flags([doc_ext.model_dump()], dm.record)

    return result

@app.post("/api/ocr/batch")
async def upload_document_batch(
    files: List[UploadFile] = File(...),
    session_id: str = Form(""),
    patient_name: str = Form("Unknown Patient")
):
    """Upload multiple document images for batch OCR processing."""
    if len(files) > min(5, MAX_IMAGES):
        raise HTTPException(status_code=413, detail="Maximum 5 images allowed per batch.")

    image_bytes_list = []
    media_types = []
    storage_paths = []
    
    import uuid
    import storage

    for file in files:
        img_bytes = await file.read()
        ctype = _check_image(file.content_type, img_bytes)
        image_bytes_list.append(img_bytes)
        media_types.append(ctype)
        
        safe_filename = f"{uuid.uuid4().hex[:8]}_{storage._safe_name(file.filename or 'doc.jpg')}"
        storage_paths.append(await asyncio.to_thread(
            storage.upload_bytes, f"patient-documents/{safe_filename}", img_bytes, ctype))

    from ocr_pipeline import process_batch_ocr
    result = await process_batch_ocr(image_bytes_list, media_types, patient_name)
    
    if not result.get("is_valid_medical_document") or result.get("name_match") is False:
        return {"status": "rejected", "reason": result.get("rejection_reason", "Document rejected due to patient name mismatch or invalid format.")}
        
    logger.info(f"batch upload received session_id: {session_id}, in sessions? {session_id in sessions}")
    if session_id and session_id in sessions:
        dm = sessions[session_id]
        from patient_record import DocumentExtraction
        import database
        
        # Save to DB via asyncpg
        doc_id = await database.save_uploaded_document(
            session_id=session_id,
            file_path=",".join(storage_paths), # Multiple paths for PDF attachment later
            document_type=result.get("document_type", "batch"),
            ocr_raw=result,
            medications=result.get("medications", []),
            labs=result.get("lab_values", [])
        )
        
        doc_ext = DocumentExtraction(
            doc_id=doc_id,
            doc_type=result.get("document_type", "batch"),
            ocr_path=",".join(storage_paths),
            entities=[result],
        )
        dm.record.document_extractions.append(doc_ext)
            
        from document_red_flags import check_document_flags
        doc_extractions_raw = [ext.model_dump() for ext in dm.record.document_extractions]
        check_document_flags(doc_extractions_raw, dm.record)

    return {"status": "success", "results": [result]}

@app.get("/api/record/{session_id}/timeline")
async def get_patient_timeline(session_id: str, _auth: dict = Depends(require_role("DOCTOR", "NURSE", "ADMIN"))):
    """Get a chronologically sorted timeline of patient documents with red flags."""
    dm = sessions.get(session_id)
    if not dm:
        return {"error": "Session not found"}
        
    from datetime import datetime
    
    docs = []
    for ext in dm.record.document_extractions:
        for ent in ext.entities:
            docs.append(ent)
            
    def parse_sort_date(doc: dict):
        issue_date = doc.get("issue_date")
        if not issue_date:
            return datetime.max
        try:
            return datetime.strptime(issue_date, "%Y-%m-%d")
        except ValueError:
            return datetime.max

    sorted_docs = sorted(docs, key=parse_sort_date)

    red_flags = []
    for doc in sorted_docs:
        for lab in doc.get("lab_values", []):
            if lab.get("is_abnormal"):
                test_name = lab.get("test_name", "Unknown Test")
                val = lab.get("value", "")
                unit = lab.get("unit", "")
                reason = lab.get("flag_reason", "Abnormal")
                red_flags.append(f"{test_name}: {val} {unit} — {reason}")

    return {
        "session_id": session_id,
        "total_documents": len(sorted_docs),
        "documents": sorted_docs,
        "critical_red_flags": red_flags
    }


# ──────────────────────────────────────────────────────────────────────
# Sarvam AI — STT / TTS Endpoints (async, non-blocking)
# ──────────────────────────────────────────────────────────────────────

@app.post("/api/stt", dependencies=[Depends(auth_utils.rate_limit("stt", 30))])
async def speech_to_text_endpoint(
    audio: UploadFile = File(...),
    language: str = Form("hi-IN"),
):
    """
    Convert patient voice to text (Gemini Transcribe by default; SPEECH_PROVIDER=sarvam optional).
    Accepts WebM/OGG/MP4/WAV/MP3 audio from the browser (max 2 MB).
    Returns transcript and language.
    """
    t0 = time.time()
    audio_bytes = await audio.read()
    if len(audio_bytes) > speech_provider.MAX_STT_BYTES:
        return JSONResponse(status_code=413, content={"error": "Audio too large (max 2 MB)"})

    # Detect format from content type
    content_type = (audio.content_type or "audio/webm").lower()
    if not content_type.startswith("audio/") and content_type not in ("application/octet-stream", "video/webm"):
        return JSONResponse(status_code=415, content={"error": "Unsupported audio type"})
    fmt = "webm"
    if "wav" in content_type:
        fmt = "wav"
    elif "mp3" in content_type or "mpeg" in content_type:
        fmt = "mp3"
    elif "ogg" in content_type:
        fmt = "ogg"
    elif "mp4" in content_type or "m4a" in content_type or "aac" in content_type:
        fmt = "mp4"

    loop = asyncio.get_event_loop()
    result = await loop.run_in_executor(
        None,
        speech_provider.speech_to_text,
        audio_bytes,
        language,
        fmt,
    )

    elapsed = time.time() - t0
    # Privacy: log sizes and timings only — never the transcript.
    logger.info(f"STT: {len(audio_bytes)}B {fmt} lang={language} -> {len(result.get('transcript', ''))} chars in {elapsed:.2f}s")
    return result


@app.post("/api/tts", dependencies=[Depends(auth_utils.rate_limit("tts", 30))])
async def text_to_speech_endpoint(
    text: str = Form(...),
    language: str = Form("hi-IN"),
    speaker: str = Form(""),
):
    """
    Convert text to speech (Gemini TTS by default; SPEECH_PROVIDER=sarvam optional).
    Returns WAV audio bytes directly for browser playback.
    """
    t0 = time.time()
    if len(text) > 5000:
        return JSONResponse(status_code=413, content={"error": "Text too long"})

    loop = asyncio.get_event_loop()
    audio_bytes = await loop.run_in_executor(
        None,
        speech_provider.text_to_speech,
        text,
        language,
        speaker if speaker else None,
    )

    elapsed = time.time() - t0
    # Privacy: log sizes and timings only — never the text.
    logger.info(f"TTS: {len(text)} chars lang={language} -> {len(audio_bytes)}B in {elapsed:.2f}s")

    if not audio_bytes:
        return JSONResponse(status_code=503, content={"error": "TTS unavailable"})

    return Response(
        content=audio_bytes,
        media_type="audio/wav",
        headers={"Content-Disposition": "inline; filename=speech.wav"},
    )


# ──────────────────────────────────────────────────────────────────────
# WebSocket — Real-time Conversation
# ──────────────────────────────────────────────────────────────────────

async def _after_engine_turn(dm: DialogueManager, prev_state: str):
    """After every engine turn (text or Live): red-flag queue escalation + DB checkpoint."""
    new_state = dm.fsm.state
    # --- Gap A & Timing ---
    # Check safety ALONE after interview completes
    if prev_state == "DYNAMIC_INTERVIEW" and new_state == "DOCUMENT_SCAN":
        import red_flag_library
        flags = red_flag_library.check_safety(dm.record.filled_state)
        if flags:
            existing_ids = {f.rule_id for f in dm.record.red_flags}
            new_flags = [f for f in flags if f.rule_id not in existing_ids]
            if new_flags:
                dm.record.red_flags.extend(new_flags)
                reasons = "; ".join(f"{f.rule_id}: {f.description}" for f in new_flags)
                escalate_queue_priority(dm.record.session_id, reasons)
                
    # PDF Generation fires on-demand via the GET endpoint in routes_extended.py

    # Bridge: if red flags fired (from dialogue), escalate queue priority automatically
    if dm.record.red_flags and dm.fsm.state == "EMERGENCY_PROTOCOL":
        latest_flag = dm.record.red_flags[-1]
        escalate_queue_priority(
            dm.record.session_id,
            f"{latest_flag.rule_id}: {latest_flag.description}"
        )

    # --- NEW: Safely save the session state to DB in the async loop ---
    try:
        import database
        record_payload = {
            "filled_state": dm.record.filled_state,
            "document_extractions": [e.model_dump() for e in dm.record.document_extractions],
            "red_flags": [r.model_dump() for r in dm.record.red_flags]
        }
        has_red_flags = len(dm.record.red_flags) > 0
        checkpoint_status = "WAITING" if dm.fsm.state == "COMPLETE" else "IN_PROGRESS"
        await database.commit_fsm_checkpoint(
            session_id=dm.record.session_id,
            filled_state=record_payload,
            chief_complaint=str(dm.record.chief_complaint.value or "") if dm.record.chief_complaint else "",
            interview_qa=dm.record.conversation_history,
            priority_flag=has_red_flags,
            status=checkpoint_status
        )
    except Exception as e:
        logger.error(f"🚨 Checkpoint failed for session {dm.record.session_id} - {e}")


@app.websocket("/ws/session")
@app.websocket("/ws/intake")
async def websocket_session(ws: WebSocket, session_id: str = Query(None)):
    """
    Real-time conversation WebSocket.

    Client sends:
      {"type": "start", "clinic_mode": "allopathic", "language": "en-IN"}
      {"type": "input", "input_type": "tap"|"voice"|"skip"|"back"|"next", "value": "..."}
      {"type": "redflag"}
      {"type": "clear_redflag"}
      {"type": "get_record"}

    Server sends:
      {"type": "ui", ...}  — UI instruction from the Dialogue Manager
      {"type": "record", ...}  — Full patient record
      {"type": "error", "message": "..."}
    """
    await ws.accept()
    dm: DialogueManager | None = None
    live = None                      # LiveVoice for this connection (Gemini Live), if active
    send_lock = asyncio.Lock()       # Live streams from a background task: serialise sends
    dm_lock = asyncio.Lock()         # one engine operation at a time per session

    async def send(obj: dict):
        async with send_lock:
            await ws.send_json(obj)

    if session_id and session_id in sessions:
        dm = sessions[session_id]
        logger.info(f"Rehydrated session: {session_id}")
        ui = dm.resume_session()
        await send({"type": "ui", **ui})

    try:
        while True:
            raw = await ws.receive_text()
            try:
                import json as _json
                msg = _json.loads(raw)
            except _json.JSONDecodeError:
                await send({"type": "error", "message": "Invalid JSON"})
                continue

            msg_type = msg.get("type", "")

            if msg_type == "start":
                clinic_mode = msg.get("clinic_mode", "allopathic")
                language = msg.get("language", "en-IN")
                dm = DialogueManager(clinic_mode=clinic_mode, language=language)

                # Set demographics if provided
                patient_name = msg.get("patient_name", "")
                patient_age = msg.get("patient_age")
                patient_sex = msg.get("patient_sex", "")
                patient_weight = msg.get("patient_weight")
                patient_height = msg.get("patient_height")
                patient_vitals = msg.get("patient_vitals")
                if patient_name or patient_age or patient_sex:
                    dm.set_demographics(
                        name=patient_name,
                        age=int(patient_age) if patient_age else None,
                        sex=patient_sex,
                        weight=float(patient_weight) if patient_weight else None,
                        height=patient_height,
                        vitals=patient_vitals,
                    )
                
                # Check for follow-up history
                previous_history = msg.get("previous_history")
                if previous_history:
                    dm.set_previous_history(previous_history)

                # Bridge: set patient_id so summaries/queue can find this patient
                patient_id = msg.get("patient_id")
                if patient_id:
                    dm.record.patient_id = patient_id

                # If a pre-created session_id was provided, use it instead of auto-generated
                provided_session_id = msg.get("session_id")
                if provided_session_id:
                    dm.record.session_id = provided_session_id
                    try:
                        import database
                        if database._pool:
                            async with database._pool.acquire() as conn:
                                doc_data = await conn.fetchrow("""
                                    SELECT d.custom_instructions
                                    FROM patient_sessions ps
                                    JOIN doctors d ON ps.doctor_id = d.doctor_id
                                    WHERE ps.session_id = $1
                                """, provided_session_id)
                                if doc_data and doc_data['custom_instructions']:
                                    dm.record.doctor_custom_instructions = doc_data['custom_instructions']
                    except Exception as e:
                        logger.error(f"Failed to fetch doctor custom instructions: {e}")

                ui = dm.start_session()
                sessions[dm.record.session_id] = dm
                logger.info(f"Session started: {dm.record.session_id} | lang={language} | mode={clinic_mode} | patient_id={patient_id}")
                await send({"type": "ui", **ui})

            elif msg_type == "resume":
                provided_session_id = msg.get("session_id")
                if provided_session_id and provided_session_id in sessions:
                    dm = sessions[provided_session_id]
                    logger.info(f"Session resumed: {dm.record.session_id}")
                    ui = dm.resume_session()
                    await send({"type": "ui", **ui})
                else:
                    await send({"type": "error", "message": "Session not found or expired"})

            elif msg_type == "input":
                if not dm:
                    await send({"type": "error", "message": "No active session"})
                    continue
                input_type = msg.get("input_type", "tap")
                value = msg.get("value", "")

                # Live form-filling phase: taps/typed answers go through the same Live turn
                if live and live.active and dm.fsm.state == "DYNAMIC_INTERVIEW" and input_type in ("tap", "voice"):
                    await send({"type": "orb_state", "orb_state": "processing"})
                    # Yes / No / Not sure buttons: applied directly to the current field
                    if input_type == "tap" and value in ("Yes", "No", "Not sure") and await live.tap_answer(value):
                        continue
                    await live.text_answer(value)
                    continue

                # Send "processing" state to the frontend immediately
                await send({
                    "type": "orb_state",
                    "orb_state": "processing",
                })

                t0 = time.time()

                prev_state = dm.fsm.state if hasattr(dm, 'fsm') else ""
                
                # Run the dialogue manager in thread pool (it calls LLM synchronously)
                loop = asyncio.get_event_loop()
                async with dm_lock:
                    ui = await loop.run_in_executor(
                        None,
                        dm.process_patient_input,
                        input_type,
                        value,
                    )
                dm.record.macro_state = dm.fsm.state

                new_state = dm.fsm.state if hasattr(dm, 'fsm') else ""

                logger.info(f"State transition: {prev_state} -> {new_state} | session={dm.record.session_id}")

                await _after_engine_turn(dm, prev_state)

                elapsed = time.time() - t0
                logger.info(f"DialogueManager.process_patient_input took {elapsed:.2f}s | state={dm.fsm.state}")

                if live and live.active:
                    await live.speak_ui(ui)   # Live speaks the engine's question
                else:
                    await send({"type": "ui", **ui})

            elif msg_type == "live_start":
                if not dm:
                    await send({"type": "live_status", "state": "fallback"})
                    continue
                if not (live and live.active):
                    from live_voice import LiveVoice
                    live = LiveVoice(dm, send, dm_lock, _after_engine_turn)
                    ok = await live.start()
                    await send({"type": "live_status", "state": "active" if ok else "fallback"})
                    # resume=True: Live re-enabled after a fallback — don't re-speak the current question
                    if ok and not msg.get("resume") and dm.fsm.state in ("CHIEF_COMPLAINT", "DYNAMIC_INTERVIEW"):
                        last_q = next((m["content"] for m in reversed(dm.record.conversation_history)
                                       if m.get("role") == "assistant"), "")
                        if last_q:
                            await live.say(last_q)
                else:
                    await send({"type": "live_status", "state": "active"})

            elif msg_type == "audio_start":
                if live:
                    await live.audio_start()

            elif msg_type == "audio":
                if live:
                    try:
                        chunk = base64.b64decode(msg.get("data", ""))
                    except Exception:
                        chunk = b""
                    if chunk:
                        await live.audio_chunk(chunk)

            elif msg_type == "audio_end":
                if live:
                    await live.audio_end()

            elif msg_type == "live_stop":
                if live:
                    await live.close()
                    live = None

            elif msg_type == "redflag":
                if dm:
                    ui = dm.process_redflag()
                    await send({"type": "ui", **ui})

            elif msg_type == "clear_redflag":
                if dm:
                    loop = asyncio.get_event_loop()
                    ui = await loop.run_in_executor(None, dm.clear_redflag)
                    await send({"type": "ui", **ui})

            elif msg_type == "get_record":
                if dm:
                    await send({"type": "record", **dm.get_record()})
                else:
                    await send({"type": "error", "message": "No active session"})

            else:
                await send({"type": "error", "message": f"Unknown type: {msg_type}"})

    except WebSocketDisconnect:
        logger.info("WebSocket disconnected")
        if live:
            await live.close()
        # Session intentionally left in memory for rehydration
    except Exception as e:
        logger.error(f"WebSocket error: {type(e).__name__}")
        if live:
            await live.close()
        try:
            await send({"type": "error", "message": "Server error"})
        except Exception:
            pass


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=8000, reload=True)
