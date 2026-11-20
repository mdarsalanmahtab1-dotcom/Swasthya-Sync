"""
SwasthyaSync — speech provider (STT + TTS).

Speech-to-text  (STT_PROVIDER):
  sarvam (default) -> Sarvam saaras:v3 first; Gemini Transcribe (GEMINI_STT_MODEL) as backup
                      if Sarvam errors or SARVAM_API_KEY is missing
  google           -> Gemini Transcribe first; Sarvam as backup when SARVAM_API_KEY is set
Text-to-speech  (SPEECH_PROVIDER):
  google (default) -> Gemini TTS (GEMINI_TTS_MODEL); Sarvam backup when SARVAM_API_KEY is set
  sarvam           -> Sarvam bulbul (also makes STT default to Sarvam)
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
import threading
import time
from collections import OrderedDict
import wave

from dotenv import load_dotenv

import model_config

load_dotenv()
logger = logging.getLogger(__name__)

SPEECH_PROVIDER = (os.environ.get("SPEECH_PROVIDER", "google") or "google").strip().lower()   # TTS
STT_PROVIDER = (os.environ.get("STT_PROVIDER", "sarvam") or "sarvam").strip().lower()        # STT

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


def _sarvam_stt_ok(res: dict) -> bool:
    """Sarvam reports failures as an 'error' key. 'Audio too short' is a real answer, not an outage."""
    err = (res or {}).get("error")
    return not err or err == "Audio too short"


def speech_to_text(audio_bytes: bytes, hint_language: str = "hi-IN", fmt: str = "webm") -> dict:
    sarvam_first = STT_PROVIDER == "sarvam" or SPEECH_PROVIDER == "sarvam"
    if sarvam_first:
        if _sarvam_available():
            try:
                res = SarvamSpeechProvider().speech_to_text(audio_bytes, hint_language, fmt)
                if _sarvam_stt_ok(res):
                    return res
                logger.warning("Sarvam STT returned an error — falling back to Gemini Transcribe")
            except Exception as e:
                logger.warning(f"Sarvam STT failed ({type(e).__name__}) — falling back to Gemini Transcribe")
        else:
            logger.info("SARVAM_API_KEY not set — using Gemini Transcribe")
        try:
            return GoogleSpeechProvider().speech_to_text(audio_bytes, hint_language, fmt)
        except Exception as e:
            logger.error(f"Gemini Transcribe backup failed ({type(e).__name__})")
            return _stt_result("", hint_language, "Speech recognition unavailable")

    # STT_PROVIDER=google: Gemini first, Sarvam backup
    try:
        return GoogleSpeechProvider().speech_to_text(audio_bytes, hint_language, fmt)
    except Exception as e:
        logger.error(f"Google STT failed ({type(e).__name__})")
        if _sarvam_available():
            logger.info("Falling back to Sarvam STT")
            try:
                res = SarvamSpeechProvider().speech_to_text(audio_bytes, hint_language, fmt)
                if _sarvam_stt_ok(res):
                    return res
            except Exception as e2:
                logger.error(f"Sarvam STT fallback failed ({type(e2).__name__})")
        return _stt_result("", hint_language, "Speech recognition unavailable")


# The brand name written in Latin letters inside an Indic sentence gets spelled out letter by
# letter by TTS voices ("S W A S T H Y A…"). Speak it in the sentence's own script instead.
_BRAND_SPOKEN = {
    "hi-IN": "स्वास्थ्य सिंक", "mr-IN": "स्वास्थ्य सिंक", "bn-IN": "স্বাস্থ্য সিঙ্ক",
    "gu-IN": "સ્વાસ્થ્ય સિંક", "pa-IN": "ਸਵਾਸਥ ਸਿੰਕ", "or-IN": "ସ୍ୱାସ୍ଥ୍ୟ ସିଙ୍କ",
    "ta-IN": "ஸ்வாஸ்த்ய சிங்க்", "te-IN": "స్వాస్థ్య సింక్", "kn-IN": "ಸ್ವಾಸ್ಥ್ಯ ಸಿಂಕ್",
    "ml-IN": "സ്വാസ്ഥ്യ സിങ്ക്", "en-IN": "Swasthya Sync",
}
_BRAND_RE = re.compile(r"swasth?ya[\s\-]?sync|स्वास्थ्यसिंक", re.IGNORECASE)


#   Capital-letter acronyms in an Indic sentence are also read oddly; say them in that script.
_ACRONYMS_SPOKEN = {
    "hi-IN": {"ABHA": "आभा", "OTP": "ओटीपी", "ID": "आईडी", "BP": "बीपी", "PDF": "पीडीएफ़"},
    "mr-IN": {"ABHA": "आभा", "OTP": "ओटीपी", "ID": "आयडी", "BP": "बीपी"},
    "bn-IN": {"ABHA": "আভা", "OTP": "ওটিপি", "ID": "আইডি", "BP": "বিপি", "PDF": "পিডিএফ"},
}
_ACRONYM_RE = re.compile(r"\b(ABHA|OTP|ID|BP|PDF)\b")


def pronounceable(text: str, language_code: str) -> str:
    """Rewrite words TTS voices tend to spell out (brand name, common acronyms)."""
    out = _BRAND_RE.sub(_BRAND_SPOKEN.get(language_code, "Swasthya Sync"), text or "")
    spoken = _ACRONYMS_SPOKEN.get(language_code)
    if spoken:
        out = _ACRONYM_RE.sub(lambda m: spoken.get(m.group(1), m.group(1)), out)
    return out


# Small in-memory cache: the kiosk's guide prompts (welcome, consent, OTP…) are identical for
# every patient, so each is synthesised once instead of costing a TTS request every time.
_TTS_CACHE: "OrderedDict[tuple, bytes]" = OrderedDict()
_TTS_CACHE_MAX = int(os.environ.get("TTS_CACHE_SIZE", "300"))
_TTS_CACHE_LOCK = threading.Lock()


# Interview questions can use a different voice provider from the kiosk's guide prompts.
# INTERVIEW_TTS_PROVIDER=sarvam (default when SARVAM_API_KEY is set) -> Sarvam Bulbul first,
# Gemini TTS as backup. Guide prompts keep SPEECH_PROVIDER (Gemini by default).
INTERVIEW_TTS_PROVIDER = (os.environ.get("INTERVIEW_TTS_PROVIDER", "sarvam") or "sarvam").strip().lower()


def text_to_speech(text: str, language_code: str = "hi-IN", speaker: str | None = None,
                   purpose: str = "") -> bytes:
    if not (text or "").strip():
        return b""
    text = pronounceable(text, language_code)
    provider = SPEECH_PROVIDER
    if purpose == "interview" and INTERVIEW_TTS_PROVIDER == "sarvam" and _sarvam_available():
        provider = "sarvam"
    key = (text, language_code, speaker or "", provider)
    with _TTS_CACHE_LOCK:
        hit = _TTS_CACHE.get(key)
        if hit:
            _TTS_CACHE.move_to_end(key)
    if hit:
        logger.info(f"TTS cache hit ({len(text)} chars lang={language_code})")
        return hit
    audio = _text_to_speech_uncached(text, language_code, speaker, provider,
                                     sarvam_backup=(purpose == "interview"))
    if audio:
        with _TTS_CACHE_LOCK:
            _TTS_CACHE[key] = audio
            while len(_TTS_CACHE) > _TTS_CACHE_MAX:
                _TTS_CACHE.popitem(last=False)
    return audio


def _text_to_speech_uncached(text: str, language_code: str, speaker: str | None,
                             provider: str = SPEECH_PROVIDER, sarvam_backup: bool = True) -> bytes:
    if provider == "sarvam":
        audio = SarvamSpeechProvider().text_to_speech(text, language_code, speaker)
        if audio:
            return audio
        logger.warning("Sarvam TTS gave no audio — falling back to Gemini TTS")
        try:
            return GoogleSpeechProvider().text_to_speech(text, language_code, speaker)
        except Exception as e:
            logger.error(f"Gemini TTS backup failed ({type(e).__name__})")
            return b""
    try:
        return GoogleSpeechProvider().text_to_speech(text, language_code, speaker)
    except Exception as e:
        status = getattr(getattr(e, "response", None), "status_code", None)
        if status == 429:
            logger.error("Google TTS failed: 429 quota exceeded — the Gemini key's TTS limit is used up "
                         "(Free Tier allows ~10/day). Use a billing-enabled key. Browser voice will be used.")
        else:
            logger.error(f"Google TTS failed ({type(e).__name__}{f' {status}' if status else ''})")
        if sarvam_backup and _sarvam_available():   # Sarvam is reserved for the interview
            logger.info("Falling back to Sarvam TTS")
            try:
                return SarvamSpeechProvider().text_to_speech(text, language_code, speaker)
            except Exception as e2:
                logger.error(f"Sarvam TTS fallback failed ({type(e2).__name__})")
        return b""
