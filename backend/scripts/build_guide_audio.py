"""Pre-record the kiosk's fixed guide prompts (welcome, OTP, consent…) as static audio files.

These sentences never change, so they are synthesised once here and shipped with the frontend
(frontend/public/audio/guide/). The kiosk then plays them without any TTS API call: no cost,
no quota, no browser-voice fallback. Prompts with placeholders ({token}) still use live TTS.

Usage (from backend/):  python scripts/build_guide_audio.py [--provider sarvam|google] [--force]
Re-run after editing frontend/src/utils/audioTranslations.ts; unchanged prompts are skipped.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND = os.path.dirname(HERE)
sys.path.insert(0, BACKEND)
os.chdir(BACKEND)

from dotenv import load_dotenv  # noqa: E402

load_dotenv()
import speech_provider as sp  # noqa: E402

FRONTEND = os.path.join(os.path.dirname(BACKEND), "frontend")
SRC = os.path.join(FRONTEND, "src", "utils", "audioTranslations.ts")
OUT = os.path.join(FRONTEND, "public", "audio", "guide")

_LANG_RE = re.compile(r'^\s*"([a-z]{2}-IN)":\s*\{\s*$')
_ENTRY_RE = re.compile(r'^\s*([a-z_]+):\s*"((?:[^"\\]|\\.)*)",?\s*$')


def parse_prompts() -> dict[str, dict[str, str]]:
    prompts: dict[str, dict[str, str]] = {}
    lang = None
    for line in io.open(SRC, encoding="utf-8"):
        m = _LANG_RE.match(line)
        if m:
            lang = m.group(1)
            prompts[lang] = {}
            continue
        m = _ENTRY_RE.match(line)
        if lang and m:
            prompts[lang][m.group(1)] = json.loads(f'"{m.group(2)}"')
    return prompts


def synth(text: str, lang: str, provider: str) -> bytes:
    text = sp.pronounceable(text, lang)
    if provider == "sarvam":
        return sp.SarvamSpeechProvider().text_to_speech(text, lang)
    return sp.GoogleSpeechProvider().text_to_speech(text, lang)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--provider", choices=["sarvam", "google"], default="sarvam")
    ap.add_argument("--force", action="store_true", help="re-record every prompt")
    args = ap.parse_args()

    manifest_path = os.path.join(OUT, "manifest.json")
    old = {}
    if os.path.exists(manifest_path):
        old = json.load(io.open(manifest_path, encoding="utf-8"))
    manifest: dict[str, dict[str, str]] = {}
    made = skipped = failed = 0
    for lang, entries in parse_prompts().items():
        os.makedirs(os.path.join(OUT, lang), exist_ok=True)
        manifest[lang] = {}
        for key, text in entries.items():
            if "{" in text:          # dynamic prompt (token number etc.) -> live TTS
                continue
            path = os.path.join(OUT, lang, f"{key}.wav")
            if not args.force and old.get(lang, {}).get(key) == text and os.path.exists(path):
                manifest[lang][key] = text
                skipped += 1
                continue
            try:
                audio = synth(text, lang, args.provider)
            except Exception as e:  # noqa: BLE001
                audio = b""
                print(f"  {lang}/{key}: {type(e).__name__}")
            if not audio:
                failed += 1
                print(f"  FAILED {lang}/{key}")
                continue
            with open(path, "wb") as f:
                f.write(audio)
            manifest[lang][key] = text
            made += 1
            print(f"  {lang}/{key}: {len(audio) // 1024} KB")
            time.sleep(0.3)
    with io.open(manifest_path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=1)
    print(f"done: {made} recorded, {skipped} unchanged, {failed} failed -> {OUT}")


if __name__ == "__main__":
    main()
