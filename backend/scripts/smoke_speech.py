"""
Smoke test: TTS -> STT round trip with Gemini speech models (synthetic sentences only).

Run from backend/:  python scripts/smoke_speech.py
Needs GEMINI_API_KEY in the environment (or backend/.env). Prints timings and a rough match score.
"""
import os
import sys
import time
from difflib import SequenceMatcher

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import speech_provider as sp  # noqa: E402

CASES = [
    ("hi-IN", "मुझे तीन दिन से बुखार और सिर में दर्द है।"),
    ("bn-IN", "আমার দুই দিন ধরে কাশি আর বুকে ব্যথা হচ্ছে।"),
    ("en-IN", "I have had stomach pain and vomiting since yesterday."),
]


def norm(s: str) -> str:
    return "".join(ch for ch in s.lower() if ch.isalnum())


def main() -> int:
    g = sp.GoogleSpeechProvider()
    ok = True
    for lang, text in CASES:
        try:
            t0 = time.time()
            wav = g.text_to_speech(text, lang)
            t_tts = time.time() - t0
            t0 = time.time()
            res = g.speech_to_text(wav, lang, "wav")
            t_stt = time.time() - t0
            score = SequenceMatcher(None, norm(text), norm(res.get("transcript", ""))).ratio()
            match = "MATCH" if score >= 0.6 else "WEAK"
            ok &= score >= 0.6
            print(f"{lang}: TTS {t_tts:.2f}s ({len(wav)} B) | STT {t_stt:.2f}s | similarity {score:.2f} {match}")
            print(f"   said: {text}\n   heard: {res.get('transcript', '')}  {res.get('error', '')}")
        except Exception as e:
            ok = False
            print(f"{lang}: FAILED ({type(e).__name__}: {str(e)[:200]})")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
