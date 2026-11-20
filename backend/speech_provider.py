"""
SwasthyaSync — speech provider (STT + TTS).

SPEECH_PROVIDER=google (default) -> Gemini Transcribe (GEMINI_STT_MODEL) + Gemini TTS (GEMINI_TTS_MODEL)
SPEECH_PROVIDER=sarvam           -> existing sarvam_client (saaras / bulbul)

If the Google provider fails and SARVAM_API_KEY is set, we fall back to Sarvam.
Return shapes are exactly what sarvam_client returns, so main.py and the frontend don't change:
  speech_to_text(...) -> {"transcript", "language_code", "language_name", optional "error"}
  text_to_speech(...) -> WAV bytes (b"" on failure)

Privacy: never log transcripts or TTS text — only sizes, languages and timings.

Docs used (Gemini API, Interactions API, v1beta):
  - Transcribe: client.interactions.create(model=..., input=[audio],
      generation_config={"transcription_config": {"language_codes": [...]}}) -> response.output_text
    Accepts audio/webm, audio/ogg, audio/opus, audio/wav, audio/mp3, audio/mp4(m4a)... (no conversion needed)
  - TTS: client.interactions.create(model=..., input=[...], response_format={"type": "audio", ...},
      generation_config={"speech_config": [{"voice": "Kore"}]}) -> base64 interaction.output_audio.data
    Raw PCM (audio/l16) is 24 kHz, mono, 16-bit LE; we request L16 and wrap it in a WAV header.
"""

from __future__ import annotations

import base64
import io
import logging
import os
import re
import time
import wave

from dotenv import load_dotenv

import model_config

load_dotenv()
logger = logging.getLogger(__name__)

SPEECH_PROVIDER = (os.environ.get("SPEECH_PROVIDER", "google") or "google").strip().lower()

MAX_STT_BYTES = 2 * 1024 * 1024          # 2 MB
MAX_STT_SECONDS = 60
TTS_SAMPLE_RATE = 24000                  # per docs: 24 kHz mono 16-bit PCM
TTS_MAX_CHARS_PER_CHUNK = 1500

LANGUAGE_NAMES = {
    "hi-IN": "Hindi", "ta-IN": "Tamil", "te-IN": "Telugu", "kn-IN": "Kannada",
    "bn-IN": "Bengali", "mr-IN": "Marathi", "gu-IN": "Gujarati", "ml-IN": "Malayalam",
    "pa-IN": "Punjabi", "or-IN": "Odia", "en-IN": "English",
}

# Gemini TTS detects the language from the text; voices are language-independent.
# Female-sounding, calm voice for every kiosk language (configurable).
DEFAULT_TTS_VOICE = os.environ.get("GEMINI_TTS_VOICE", "Kore")
TTS_VOICE = {code: DEFAULT_TTS_VOICE for code in LANGUAGE_NAMES}

_MIME_BY_FMT = {
    "webm": "audio/webm", "ogg": "audio/ogg", "opus": "audio/opus", "wav": "audio/wav",
    "mp3": "audio/mp3", "mpeg": "audio/mpeg", "mp4": "audio/m4a", "m4a": "audio/m4a",
    "aac": "audio/aac", "flac": "audio/flac",
}


def _stt_result(transcript: str, lang: str, error: str | None = None) -> dict:
    out = {"transcript": transcript, "language_code": lang, "language_name": LANGUAGE_NAMES.get(lang, "Unknown")}
    if error:
        out["error"] = error
    return out


# ──────────────────────────────────────────────────────────────────────
# Google (Gemini) provider
# ──────────────────────────────────────────────────────────────────────

_speech_client = None


def _get_speech_client():
    """Interactions API lives on v1beta — separate client from the generateContent one."""
    global _speech_client
    if _speech_client is not None:
        return _speech_client
    try:
        from google import genai
    except ImportError:
        return None
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    opts = {"api_version": "v1beta"}
    if api_key:
        _speech_client = genai.Client(api_key=api_key, http_options=opts)
    elif (os.environ.get("GOOGLE_GENAI_USE_VERTEXAI", "") or "").lower() in ("1", "true", "yes"):
        _speech_client = genai.Client(http_options=opts)
    return _speech_client


def _rest_interaction(body: dict, timeout: float) -> dict:
    """Same request over REST (used if the installed SDK rejects a newer argument)."""
    import httpx
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("REST fallback needs GEMINI_API_KEY")
    r = httpx.post(
        "https://generativelanguage.googleapis.com/v1beta/interactions",
        headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
        json=body, timeout=timeout,
    )
    r.raise_for_status()
    return r.json()


def _interaction(client, body: dict, timeout: float = 30.0):
    """Interactions call. With an API key we use the REST endpoint directly: google-genai 1.x sends
    a legacy Interactions schema the API now rejects (needs SDK >= 2). Vertex/ADC mode uses the SDK."""
    if os.environ.get("GEMINI_API_KEY", "").strip():
        return _rest_interaction(body, timeout)
    return client.interactions.create(**body)


def _get(obj, name, default=None):
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


def _output_text(resp) -> str:
    text = _get(resp, "output_text")
    if text:
        return str(text)
    parts = []
    for step in _get(resp, "steps", None) or []:
        for c in _get(step, "content", None) or []:
            if _get(c, "type") == "text" and _get(c, "text"):
                parts.append(_get(c, "text"))
    return "".join(parts)


def _output_audio_b64(resp) -> str | None:
    audio = _get(resp, "output_audio")
    if audio is not None and _get(audio, "data"):
        return _get(audio, "data")
    data = None
    for step in _get(resp, "steps", None) or []:
        for c in _get(step, "content", None) or []:
            if _get(c, "data"):
                data = _get(c, "data")  # last audio block
    return data


def _wav_seconds(audio: bytes) -> float | None:
    try:
        with wave.open(io.BytesIO(audio)) as w:
            return w.getnframes() / float(w.getframerate())
    except Exception:
        return None


def _pcm_to_wav(pcm: bytes, sample_rate: int = TTS_SAMPLE_RATE) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sample_rate)
        w.writeframes(pcm)
    return buf.getvalue()


def _wav_to_pcm(wav_bytes: bytes) -> bytes:
    with wave.open(io.BytesIO(wav_bytes)) as w:
        return w.readframes(w.getnframes())


def _chunk_text(text: str, max_chars: int = TTS_MAX_CHARS_PER_CHUNK) -> list[str]:
    text = (text or "").strip()
    if len(text) <= max_chars:
        return [text] if text else []
    sentences = re.split(r"(?<=[.!?।॥?])\s+", text)
    chunks, cur = [], ""
    for s in sentences:
        while len(s) > max_chars:
            chunks.append(s[:max_chars])
            s = s[max_chars:]
        if len(cur) + len(s) + 1 > max_chars and cur:
            chunks.append(cur.strip())
            cur = ""
        cur += " " + s
    if cur.strip():
        chunks.append(cur.strip())
    return chunks


class GoogleSpeechProvider:
    name = "google"

    def speech_to_text(self, audio_bytes: bytes, hint_language: str = "hi-IN", fmt: str = "webm") -> dict:
        if len(audio_bytes) > MAX_STT_BYTES:
            return _stt_result("", hint_language, "Audio too large (max 2 MB)")
        secs = _wav_seconds(audio_bytes) if fmt == "wav" else None
        if secs is not None and secs > MAX_STT_SECONDS:
            return _stt_result("", hint_language, "Audio too long (max 60 s)")

        client = _get_speech_client()
        if client is None:
            raise RuntimeError("No Gemini credentials for speech")
        mime = _MIME_BY_FMT.get(fmt, "audio/webm")
        audio_item = {"type": "audio", "data": base64.b64encode(audio_bytes).decode(), "mime_type": mime}
        lang_codes = [hint_language] if hint_language in LANGUAGE_NAMES else []
        gen_cfg = {"transcription_config": {"language_codes": lang_codes}}
        t0 = time.time()
        resp = _interaction(
            client, {"model": model_config.STT_MODEL, "input": [audio_item], "generation_config": gen_cfg},
        )
        transcript = _output_text(resp).strip()
        logger.info(f"Google STT: {len(audio_bytes)}B {mime} lang_hint={hint_language} "
                    f"-> {len(transcript)} chars in {time.time() - t0:.2f}s")
        # The API returns no detected-language field; the kiosk language is authoritative.
        return _stt_result(transcript, hint_language)

    def _tts_chunk(self, client, text: str, voice: str) -> bytes:
        body = {
            "model": model_config.TTS_MODEL,
            "input": [{"type": "user_input", "content": [{"type": "text", "text": text}]}],
            "response_format": {"type": "audio", "mime_type": "audio/l16", "sample_rate": TTS_SAMPLE_RATE},
            "generation_config": {"speech_config": [{"voice": voice}]},
        }
        resp = _interaction(client, body)
        b64 = _output_audio_b64(resp)
        if not b64:
            raise RuntimeError("TTS returned no audio")
        raw = base64.b64decode(b64)
        if raw[:4] == b"RIFF":  # some responses are already WAV — unwrap to PCM so chunks concatenate
            raw = _wav_to_pcm(raw)
        return raw

    def text_to_speech(self, text: str, language_code: str = "hi-IN", speaker: str | None = None) -> bytes:
        client = _get_speech_client()
        if client is None:
            raise RuntimeError("No Gemini credentials for speech")
        if language_code not in TTS_VOICE:
            logger.warning(f"Google TTS: language {language_code} not mapped — using English voice")
        voice = TTS_VOICE.get(language_code, DEFAULT_TTS_VOICE)
        t0 = time.time()
        pcm = b"".join(self._tts_chunk(client, c, voice) for c in _chunk_text(text))
        if not pcm:
            return b""
        wav = _pcm_to_wav(pcm)
        logger.info(f"Google TTS: {len(text)} chars lang={language_code} -> {len(wav)}B in {time.time() - t0:.2f}s")
        return wav


class SarvamSpeechProvider:
    name = "sarvam"

    def speech_to_text(self, audio_bytes: bytes, hint_language: str = "hi-IN", fmt: str = "webm") -> dict:
        import sarvam_client
        return sarvam_client.speech_to_text(audio_bytes, hint_language, fmt)

    def text_to_speech(self, text: str, language_code: str = "hi-IN", speaker: str | None = None) -> bytes:
        import sarvam_client
        return sarvam_client.text_to_speech(text, language_code, speaker)


def _sarvam_available() -> bool:
    return bool(os.environ.get("SARVAM_API_KEY", "").strip())


def speech_to_text(audio_bytes: bytes, hint_language: str = "hi-IN", fmt: str = "webm") -> dict:
    if SPEECH_PROVIDER == "sarvam":
        return SarvamSpeechProvider().speech_to_text(audio_bytes, hint_language, fmt)
    try:
        return GoogleSpeechProvider().speech_to_text(audio_bytes, hint_language, fmt)
    except Exception as e:
        logger.error(f"Google STT failed ({type(e).__name__})")
        if _sarvam_available():
            logger.info("Falling back to Sarvam STT")
            try:
                return SarvamSpeechProvider().speech_to_text(audio_bytes, hint_language, fmt)
            except Exception as e2:
                logger.error(f"Sarvam STT fallback failed ({type(e2).__name__})")
        return _stt_result("", hint_language, "Speech recognition unavailable")


def text_to_speech(text: str, language_code: str = "hi-IN", speaker: str | None = None) -> bytes:
    if not (text or "").strip():
        return b""
    if SPEECH_PROVIDER == "sarvam":
        return SarvamSpeechProvider().text_to_speech(text, language_code, speaker)
    try:
        return GoogleSpeechProvider().text_to_speech(text, language_code, speaker)
    except Exception as e:
        logger.error(f"Google TTS failed ({type(e).__name__})")
        if _sarvam_available():
            logger.info("Falling back to Sarvam TTS")
            try:
                return SarvamSpeechProvider().text_to_speech(text, language_code, speaker)
            except Exception as e2:
                logger.error(f"Sarvam TTS fallback failed ({type(e2).__name__})")
        return b""
