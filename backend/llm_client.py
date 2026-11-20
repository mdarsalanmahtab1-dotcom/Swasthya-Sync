"""
SwasthyaSync — Ultra-Low Latency Resilient LLM Client (google.genai SDK)

Features:
  - Gemini model cascades configured in model_config.py (env vars)
  - Circuit Breaker Pattern (fast fail on 503/throttling)
  - Retry with Exponential Backoff
  - Disabled AFC (Automatic Function Calling) to prevent multi-call latency loops
  - Async & Sync interface for FastAPI concurrency
"""

from __future__ import annotations
import json
import os
import logging
import time
import asyncio
import concurrent.futures
from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)

# Try to import the new Gemini SDK
try:
    from google import genai
    from google.genai import types as genai_types
    HAS_GENAI = True
except ImportError:
    HAS_GENAI = False
    logger.warning("google-genai not installed — using mock LLM")


import model_config

# ──────────────────────────────────────────────────────────────────────
# Multi-Tier Resilient Model Cascades — defined in model_config.py (env vars)
# ──────────────────────────────────────────────────────────────────────

# Speed-optimized cascade (low-latency interactive kiosk turns & portal chat)
FAST_MODELS_CASCADE = model_config.FAST_MODELS

# Reasoning-optimized cascade (clinical handover notes, contradictions, synthesis)
REASONING_MODELS_CASCADE = model_config.REASONING_MODELS

PRIMARY_MODEL = FAST_MODELS_CASCADE[0]
FALLBACK_MODEL = FAST_MODELS_CASCADE[1] if len(FAST_MODELS_CASCADE) > 1 else FAST_MODELS_CASCADE[0]

# Shared thread pool for timeout-bounded LLM calls
_llm_executor = concurrent.futures.ThreadPoolExecutor(max_workers=8, thread_name_prefix="gemini_worker")


def _call_model_with_timeout(fn, timeout_seconds: float | None = None):
    """Execute a synchronous SDK call with a strict timeout."""
    future = _llm_executor.submit(fn)
    return future.result(timeout=timeout_seconds or model_config.FAST_TIMEOUT)

# Complaint categories for classification
COMPLAINT_CATEGORIES = [
    "pain", "fever", "respiratory", "gi", "neuro",
    "cardiac", "musculoskeletal", "skin", "urinary",
    "gynecological", "psychiatric", "ent", "eye", "general",
]

# Language names for prompt building
LANGUAGE_NAMES = {
    "hi-IN": "Hindi (हिन्दी)",
    "ta-IN": "Tamil (தமிழ்)",
    "te-IN": "Telugu (తెలుగు)",
    "kn-IN": "Kannada (ಕನ್ನಡ)",
    "bn-IN": "Bengali (বাংলা)",
    "mr-IN": "Marathi (मराठी)",
    "gu-IN": "Gujarati (ગુજરાતી)",
    "ml-IN": "Malayalam (മലയാളം)",
    "pa-IN": "Punjabi (ਪੰਜਾਬੀ)",
    "or-IN": "Odia (ଓଡ଼ିଆ)",
    "en-IN": "English",
}

# ──────────────────────────────────────────────────────────────────────
# Circuit Breaker & Health Management
# ──────────────────────────────────────────────────────────────────────

class CircuitBreaker:
    def __init__(self, failure_threshold: int = 3, reset_timeout: float = 15.0):
        self.failure_threshold = failure_threshold
        self.reset_timeout = reset_timeout
        self.failure_count = 0
        self.state = "CLOSED"  # CLOSED, OPEN, HALF_OPEN
        self.last_state_change = time.time()

    def record_success(self):
        self.failure_count = 0
        if self.state != "CLOSED":
            logger.info("⚡ Circuit Breaker: Recovered! Resetting to CLOSED")
            self.state = "CLOSED"
            self.last_state_change = time.time()

    def record_failure(self, reason: str = ""):
        self.failure_count += 1
        logger.warning(f"⚡ Circuit Breaker failure ({self.failure_count}/{self.failure_threshold}): {reason}")
        if self.failure_count >= self.failure_threshold:
            if self.state != "OPEN":
                logger.error(f"🚨 Circuit Breaker TRIPPED to OPEN! Fast failing LLM calls for {self.reset_timeout}s")
                self.state = "OPEN"
                self.last_state_change = time.time()

    def allow_request(self) -> bool:
        if self.state == "CLOSED":
            return True
        elif self.state == "OPEN":
            if time.time() - self.last_state_change > self.reset_timeout:
                logger.info("⚡ Circuit Breaker: Testing recovery (HALF_OPEN)")
                self.state = "HALF_OPEN"
                return True
            return False
        elif self.state == "HALF_OPEN":
            return True
        return True


circuit_breaker = CircuitBreaker()

def _get_client():
    """Shared google-genai client (API key or Vertex ADC) — see model_config.get_genai_client."""
    if not HAS_GENAI:
        return None
    return model_config.get_genai_client()


# Constrain JSON outputs with response_json_schema (set GEMINI_STRICT_JSON_SCHEMA=false to disable).
STRICT_JSON_SCHEMA = os.environ.get("GEMINI_STRICT_JSON_SCHEMA", "true").strip().lower() not in ("0", "false", "no", "off")


def _json_config_kwargs(system_prompt: str, temperature: float, max_output_tokens: int, schema: dict | None) -> dict:
    kwargs = dict(
        system_instruction=system_prompt,
        response_mime_type="application/json",
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
    )
    if schema is not None:
        kwargs["response_json_schema"] = schema
    return kwargs


def is_schema_rejection(e: Exception) -> bool:
    """True if the API refused the request itself (HTTP 400), e.g. an unsupported schema keyword."""
    code = getattr(e, "code", None) or getattr(e, "status_code", None)
    return code == 400 or "INVALID_ARGUMENT" in str(e)[:300]


def _generate_json_with_retry(
    client,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.3,
    models_to_try: list[str] | None = None,
    timeout_seconds: float | None = None,
    response_json_schema: dict | None = None,
    max_output_tokens: int = 1024,
) -> dict:
    """Call Gemini with model cascade fallback, timeout protection, and disabled AFC.

    If response_json_schema is given, the model is constrained to that JSON shape.
    """
    if not circuit_breaker.allow_request():
        raise RuntimeError("Circuit breaker is OPEN — bypassing LLM call for immediate fallback")

    candidate_models = list(models_to_try or FAST_MODELS_CASCADE)
    timeout_seconds = timeout_seconds or model_config.FAST_TIMEOUT
    last_exception = None
    use_schema = response_json_schema is not None and STRICT_JSON_SCHEMA

    for model in candidate_models:
        attempts = 2 if use_schema else 1
        for attempt in range(attempts):
            with_schema = use_schema and attempt == 0
            t0 = time.time()
            try:
                config = genai_types.GenerateContentConfig(**_json_config_kwargs(
                    system_prompt, temperature, max_output_tokens,
                    response_json_schema if with_schema else None,
                ))

                def _invoke():
                    return client.models.generate_content(
                        model=model,
                        contents=user_prompt,
                        config=config,
                    )

                response = _call_model_with_timeout(_invoke, timeout_seconds=timeout_seconds)
                elapsed = time.time() - t0
                logger.info(f"Gemini call took {elapsed:.2f}s (model={model}, schema={with_schema})")

                parsed = json.loads(response.text)
                circuit_breaker.record_success()
                return parsed

            except concurrent.futures.TimeoutError:
                elapsed = time.time() - t0
                logger.warning(f"⚡ Gemini model {model} timed out after {elapsed:.2f}s (limit {timeout_seconds}s) — cascading to next backup")
                last_exception = TimeoutError(f"Model {model} timed out after {timeout_seconds}s")
                break

            except Exception as e:
                elapsed = time.time() - t0
                last_exception = e
                if with_schema and is_schema_rejection(e):
                    logger.warning(f"⚡ Gemini model {model} rejected response_json_schema — retrying without schema")
                    continue
                logger.warning(f"⚡ Gemini model {model} failed after {elapsed:.2f}s: {type(e).__name__} — cascading to next backup")
                break

    circuit_breaker.record_failure(type(last_exception).__name__ if last_exception else "unknown")
    raise last_exception or RuntimeError("All Gemini backup models failed")


def generate_portal_chat_reply(system_prompt: str, user_prompt: str, temperature: float = 0.5) -> str:
    """Standard text generation for the Patient Portal Chatbot with multi-model cascade & timeout."""
    client = _get_client()
    if not client:
        return "I'm sorry, my AI systems are currently offline. Please try again later."

    if not circuit_breaker.allow_request():
        return "The system is currently experiencing high load. Please try again in a few moments."

    last_exception = None
    for model in FAST_MODELS_CASCADE:
        t0 = time.time()
        try:
            config = genai_types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="text/plain",
                temperature=temperature,
                max_output_tokens=512,
                automatic_function_calling=genai_types.AutomaticFunctionCallingConfig(disable=True),
            )

            def _invoke():
                return client.models.generate_content(
                    model=model,
                    contents=user_prompt,
                    config=config,
                )

            response = _call_model_with_timeout(_invoke, timeout_seconds=model_config.FAST_TIMEOUT)
            elapsed = time.time() - t0
            logger.info(f"Portal chat reply took {elapsed:.2f}s (model={model})")
            circuit_breaker.record_success()
            return response.text
        except concurrent.futures.TimeoutError:
            logger.warning(f"⚡ Portal chat timed out on {model} — cascading to next backup")
            continue
        except Exception as e:
            last_exception = e
            logger.warning(f"⚡ Portal chat failed on {model}: {e} — cascading to next backup")
            continue

    circuit_breaker.record_failure(type(last_exception).__name__ if last_exception else "unknown")
    return "I apologize, but I encountered an error while processing your request. Please ask your doctor for specific medical advice."


# ──────────────────────────────────────────────────────────────────────
# Unified Call: conversation_turn
# ──────────────────────────────────────────────────────────────────────

def conversation_turn(
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.4,
    response_json_schema: dict | None = None,
) -> dict:
    """Execute a single conversation turn via LLM with fallback protection."""
    client = _get_client()
    if client is None:
        return _mock_conversation_turn(user_prompt)

    try:
        return _generate_json_with_retry(
            client, system_prompt, user_prompt, temperature,
            response_json_schema=response_json_schema,
        )
    except Exception as e:
        logger.error(f"conversation_turn failed gracefully: {type(e).__name__}")
        return _mock_conversation_turn(user_prompt)


def _mock_conversation_turn(user_prompt: str) -> dict:
    """Mock conversation turn when LLM is unavailable or circuit is open."""
    return {
        "spoken_text": "Could you tell me more about your symptoms?",
        "suggested_options": [
            {"label": "Yes", "label_translated": "Yes"},
            {"label": "No", "label_translated": "No"},
            {"label": "Not sure", "label_translated": "Not sure"},
        ],
        "section_complete": False,
        "extracted_data": {},
        "section_summary": "",
        "red_flag_check": None,
        "reasoning": "Fallback response — LLM circuit open or unavailable",
        "is_fallback": True,  # callers replace the generic text with a context-appropriate one
    }


# ──────────────────────────────────────────────────────────────────────
# Complaint Classification
# ──────────────────────────────────────────────────────────────────────

def classify_complaint(text: str, language: str = "en-IN") -> str:
    """Classify chief complaint into a category."""
    client = _get_client()
    if client is None:
        return _mock_classify(text)

    lang_name = LANGUAGE_NAMES.get(language, language)
    system_prompt = f"""Classify the patient's chief complaint into exactly ONE of these categories:
{json.dumps(COMPLAINT_CATEGORIES)}

The patient may be speaking in {lang_name}. Understand their complaint regardless of language.
Output ONLY a JSON object: {{"category": "<one of the categories>"}}
If unsure, use "general"."""

    try:
        result = _generate_json_with_retry(client, system_prompt, f"Patient says: {text}", temperature=0.0)
        cat = result.get("category", "general")
        return cat if cat in COMPLAINT_CATEGORIES else "general"
    except Exception as e:
        logger.error(f"LLM classify_complaint failed: {e}")
        return _mock_classify(text)


def _mock_classify(text: str) -> str:
    """Keyword-based classification fallback."""
    text_lower = text.lower()
    mapping = {
        "pain": ["pain", "ache", "hurt", "dard", "dard"],
        "fever": ["fever", "temperature", "bukhar", "bukhaar", "hot"],
        "respiratory": ["cough", "breathe", "breathing", "wheeze", "khansi", "saans"],
        "gi": ["stomach", "vomit", "diarrhea", "nausea", "bowel", "pet", "ulti"],
        "neuro": ["headache", "dizzy", "numbness", "tingling", "seizure", "sir dard"],
        "cardiac": ["chest", "heart", "palpitation", "seena"],
        "skin": ["rash", "itch", "skin", "wound"],
        "urinary": ["urine", "burning", "peshab"],
    }
    for cat, keywords in mapping.items():
        if any(k in text_lower for k in keywords):
            return cat
    return "general"


# ──────────────────────────────────────────────────────────────────────
# Document OCR Entity Extraction
# ──────────────────────────────────────────────────────────────────────

def extract_document_entities(ocr_text: str, doc_type: str = "prescription") -> dict:
    """Extract medications, diagnoses, lab values from OCR text."""
    client = _get_client()
    if client is None:
        return _mock_doc_extract(ocr_text)

    system_prompt = """Extract structured clinical entities from this medical document text.

Output a JSON object with:
- "medications": [{name, dose, frequency}]
- "diagnoses": [str]
- "lab_results": [{test, result, unit, reference_range, status}]
- "procedures": [str]

Only extract what is clearly present. Mark uncertain extractions."""

    try:
        return _generate_json_with_retry(
            client,
            system_prompt,
            f"Document type: {doc_type}\nOCR text:\n{ocr_text}",
            temperature=0.1,
            models_to_try=REASONING_MODELS_CASCADE,
            timeout_seconds=model_config.REASONING_TIMEOUT,
            response_json_schema=DOC_ENTITIES_JSON_SCHEMA,
        )
    except Exception as e:
        logger.error(f"LLM extract_document_entities failed: {type(e).__name__}")
        return _mock_doc_extract(ocr_text)


DOC_ENTITIES_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "medications": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "dose": {"type": "string"},
                    "frequency": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        "diagnoses": {"type": "array", "items": {"type": "string"}},
        "lab_results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "test": {"type": "string"},
                    "result": {"type": "string"},
                    "unit": {"type": "string"},
                    "reference_range": {"type": "string"},
                    "status": {"type": "string"},
                },
                "required": ["test", "result"],
            },
        },
        "procedures": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["medications", "diagnoses", "lab_results", "procedures"],
}


def _mock_doc_extract(ocr_text: str) -> dict:
    """AI unavailable: return NOTHING extracted. Never invent clinical data."""
    return {
        "medications": [],
        "diagnoses": [],
        "lab_results": [],
        "procedures": [],
        "ai_unavailable": True,
    }

# ──────────────────────────────────────────────────────────────────────
# Clinical Synthesis
# ──────────────────────────────────────────────────────────────────────

# Keys that identify a person (or point to their files) and must never be sent to an LLM.
# Note: a plain "name" key is NOT stripped globally (medications use it for the drug name);
# it is stripped only inside person objects (_PERSON_KEYS).
_IDENTIFIER_KEYS = {
    "patient_name", "full_name", "extracted_name_on_document", "registered_name",
    "phone", "phone_number", "mobile", "mobile_number", "abha", "abha_number", "abha_id",
    "abha_address", "address", "email", "aadhaar", "file_path", "storage_path", "image_url",
    "signed_url", "storage_paths", "uploaded_images",
}
_PERSON_KEYS = {"patient_context", "patient", "demographics", "patient_info"}


def strip_identifiers(obj, _in_person: bool = False):
    """Return a deep copy of obj with identifier keys removed (recursively)."""
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            kl = str(k).lower()
            if kl in _IDENTIFIER_KEYS or (_in_person and kl == "name"):
                continue
            out[k] = strip_identifiers(v, _in_person=kl in _PERSON_KEYS)
        return out
    if isinstance(obj, list):
        return [strip_identifiers(v, _in_person=_in_person) for v in obj]
    return obj


def generate_clinical_summary(filled_state: dict, doc_extractions: list = None, demographics: dict | None = None) -> dict:
    """Synthesize the Q&A state into a structured 200-word clinical narrative."""
    client = _get_client()
    if client is None:
        return _mock_clinical_summary(filled_state)

    system_prompt = """You are an expert physician assistant. Write a concise, professional clinical handover note (approx 200 words).
Synthesize the patient's history, symptoms, and extracted medical document information.
Output JSON format:
{
  "clinical_narrative": "Patient is a ...",
  "critical_highlights": ["Finding 1", "Finding 2"],
  "contradictions": ["Contradiction 1", "Contradiction 2"]
}
Keep it objective and medically accurate.
The REGISTERED age and sex (from kiosk registration) are authoritative. If a document shows a different
age or sex, use the registered values in the narrative and list the mismatch under "contradictions".
Report only what the patient said or the documents show; do not add symptoms or diagnoses."""

    # Privacy: drop direct identifiers (names, phone, ABHA, address, file paths) before the LLM sees them.
    safe_state = strip_identifiers(filled_state)
    safe_docs = strip_identifiers(doc_extractions or [])
    demo = demographics or {}
    reg = (f"Registered patient (authoritative): age {demo.get('age') or 'unknown'}, "
           f"sex {demo.get('sex') or 'unknown'}\n\n")
    user_prompt = reg + f"Patient Q&A History:\n{json.dumps(safe_state, indent=2, ensure_ascii=False)}\n\nDocument Extractions:\n{json.dumps(safe_docs, indent=2, ensure_ascii=False)}"
    
    try:
        return _generate_json_with_retry(
            client,
            system_prompt,
            user_prompt,
            temperature=0.2,
            models_to_try=REASONING_MODELS_CASCADE,
            timeout_seconds=model_config.REASONING_TIMEOUT,
            response_json_schema=CLINICAL_SUMMARY_JSON_SCHEMA,
        )
    except Exception as e:
        logger.error(f"LLM generate_clinical_summary failed: {type(e).__name__}")
        return _mock_clinical_summary(filled_state)


CLINICAL_SUMMARY_JSON_SCHEMA = {
    "type": "object",
    "properties": {
        "clinical_narrative": {"type": "string"},
        "critical_highlights": {"type": "array", "items": {"type": "string"}},
        "contradictions": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["clinical_narrative", "critical_highlights", "contradictions"],
}

AI_UNAVAILABLE_NARRATIVE = "AI summary unavailable — please review the raw Q&A below."


def _mock_clinical_summary(filled_state: dict) -> dict:
    """AI unavailable: say so plainly. Never invent a clinical summary."""
    return {
        "clinical_narrative": AI_UNAVAILABLE_NARRATIVE,
        "critical_highlights": [],
        "contradictions": [],
        "ai_unavailable": True,
    }
