"""
SwasthyaSync — central Gemini model + client configuration.

Every model ID used by the backend comes from here (env vars with safe defaults),
and every module gets its google-genai client from get_genai_client().
Do not hard-code model names anywhere else.

Env vars (comma-separated lists, first entry = primary, rest = fallbacks):
  GEMINI_FAST_MODELS        low-latency kiosk turns, extraction, classification
  GEMINI_REASONING_MODELS   schema generation, clinical summary, document entities
  GEMINI_VISION_MODELS      OCR of uploaded documents
  GEMINI_STT_MODEL          speech-to-text (Phase 2)
  GEMINI_TTS_MODEL          text-to-speech (Phase 2)

Auth (checked in this order):
  1. GEMINI_API_KEY set                -> Gemini Developer API with that key
  2. GOOGLE_GENAI_USE_VERTEXAI=true    -> Vertex AI / Agent Platform via ADC
                                          (GOOGLE_CLOUD_PROJECT, GOOGLE_CLOUD_LOCATION;
                                          on Cloud Run this is the service account, no key)
  3. neither                           -> None (callers use their non-AI fallbacks)
"""

from __future__ import annotations

import logging
import os
import threading

from dotenv import load_dotenv

load_dotenv()
logger = logging.getLogger(__name__)


def _env_list(name: str, default: str) -> list[str]:
    raw = os.environ.get(name, "") or default
    return [m.strip() for m in raw.split(",") if m.strip()]


FAST_MODELS: list[str] = _env_list("GEMINI_FAST_MODELS", "gemini-3.5-flash-lite,gemini-flash-lite-latest")
REASONING_MODELS: list[str] = _env_list("GEMINI_REASONING_MODELS", "gemini-3.8-flash,gemini-3.5-flash-lite")
VISION_MODELS: list[str] = _env_list("GEMINI_VISION_MODELS", "gemini-3.8-flash,gemini-3.5-flash-lite")
STT_MODEL: str = os.environ.get("GEMINI_STT_MODEL", "").strip() or "gemini-3.5-transcribe"
TTS_MODEL: str = os.environ.get("GEMINI_TTS_MODEL", "").strip() or "gemini-3.8-flash-lite-tts"


def all_configured_models() -> list[str]:
    seen: list[str] = []
    for m in [*FAST_MODELS, *REASONING_MODELS, *VISION_MODELS, STT_MODEL, TTS_MODEL]:
        if m not in seen:
            seen.append(m)
    return seen


# ──────────────────────────────────────────────────────────────────────
# Client factory (singleton)
# ──────────────────────────────────────────────────────────────────────

try:
    from google import genai
    HAS_GENAI = True
except ImportError:  # pragma: no cover
    genai = None
    HAS_GENAI = False

_client = None
_client_lock = threading.Lock()
_client_mode = "none"


def _truthy(v: str | None) -> bool:
    return (v or "").strip().lower() in ("1", "true", "yes", "on")


def get_genai_client():
    """Return the shared google-genai client, or None if no credentials are configured."""
    global _client, _client_mode
    if _client is not None:
        return _client
    if not HAS_GENAI:
        logger.warning("google-genai not installed — AI features disabled")
        return None
    with _client_lock:
        if _client is not None:
            return _client
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        try:
            if api_key:
                _client = genai.Client(api_key=api_key)
                _client_mode = "gemini-api-key"
            elif _truthy(os.environ.get("GOOGLE_GENAI_USE_VERTEXAI")):
                # Reads GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION and ADC credentials.
                _client = genai.Client()
                _client_mode = "vertex-adc"
            else:
                logger.warning("No GEMINI_API_KEY and GOOGLE_GENAI_USE_VERTEXAI not set — AI features disabled")
                return None
        except Exception as e:
            logger.error(f"Failed to create google-genai client ({type(e).__name__})")
            _client = None
            return None
        logger.info(f"google-genai client initialized (mode={_client_mode})")
    return _client


def client_mode() -> str:
    return _client_mode


def log_visible_models(timeout_seconds: float = 10.0) -> None:
    """Log which configured models the current credentials can see. Never raises."""
    client = get_genai_client()
    configured = all_configured_models()
    if client is None:
        logger.info(f"Configured Gemini models (no client, not checked): {configured}")
        return

    result: dict = {}

    def _list():
        try:
            names = set()
            for m in client.models.list():
                name = (getattr(m, "name", "") or "").split("/")[-1]
                if name:
                    names.add(name)
            result["names"] = names
        except Exception as e:
            result["error"] = type(e).__name__

    t = threading.Thread(target=_list, daemon=True)
    t.start()
    t.join(timeout_seconds)
    if "names" not in result:
        logger.warning(f"Could not list Gemini models ({result.get('error', 'timeout')}); configured: {configured}")
        return
    names = result["names"]
    visible = [m for m in configured if m in names]
    missing = [m for m in configured if m not in names]
    logger.info(f"Gemini models visible to this key: {visible}")
    if missing:
        # '-latest' aliases are often not listed but still callable.
        logger.warning(f"Configured Gemini models NOT listed for this key: {missing}")
