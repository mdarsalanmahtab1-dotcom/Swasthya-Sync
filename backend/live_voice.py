"""
SwasthyaSync — Gemini Live voice layer (low-latency speech in/out + form filling).

The DialogueManager stays in charge. Gemini Live only:
  * listens to the patient (push-to-talk audio, manual turn control — no echo, no free talk),
  * reports what the patient said through ONE tool, `submit_answer`, for the fields the
    engine tells it are open (unknown field ids and repeats are dropped by the engine),
  * speaks exactly what the engine decides: a fixed sentence (SAY) or one question about
    the field the engine selected (ASK).

What Live never does: choose the next question, decide red flags, end the interview,
or see the patient's name/phone/ABHA. The engine's field selector picks every question,
the deterministic rules check red flags after every answer, and each field is asked at
most DialogueManager.MAX_ASKS times (then marked Unanswered), so there are no loops.

Fallback (any time, even mid-turn): if Live errors, times out, or answers without the tool,
the buffered patient audio is transcribed with the backup STT (speech_provider), the
normal text engine handles the turn, and the client is told to switch to STT/TTS mode.

Privacy: no transcripts or spoken text are logged — only sizes, field ids and timings.
"""

from __future__ import annotations

import asyncio
import base64
import io
import logging
import re
import time
import wave
from typing import Awaitable, Callable

from google.genai import types

import model_config

logger = logging.getLogger(__name__)

IN_RATE = 16000   # browser -> server -> Live (16-bit PCM mono)
OUT_RATE = 24000  # Live -> browser (16-bit PCM mono)

LANGUAGE_NAMES = {
    "hi-IN": "Hindi", "ta-IN": "Tamil", "te-IN": "Telugu", "kn-IN": "Kannada",
    "bn-IN": "Bengali", "mr-IN": "Marathi", "gu-IN": "Gujarati", "ml-IN": "Malayalam",
    "pa-IN": "Punjabi", "or-IN": "Odia", "en-IN": "English (Indian)",
}

SUBMIT_ANSWER_TOOL = {
    "name": "submit_answer",
    "description": (
        "Report what the patient just said. Call this after EVERY patient reply, before speaking. "
        "Include only facts the patient clearly stated that match the open fields given by the kiosk."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "answers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "field_id": {"type": "string", "description": "Exact field_id from the kiosk's open fields"},
                        "value": {"type": "string", "description": "Concise clinical value in English: 'Yes', 'No', or a short phrase (e.g. '3 days')"},
                        "verbatim": {"type": "string", "description": "The patient's own words"},
                        "confidence": {"type": "number", "description": "0.5 unclear .. 0.95 clearly stated"},
                    },
                    "required": ["field_id", "value", "verbatim"],
                },
            },
        },
        "required": ["answers"],
    },
}


def _system_instruction(language: str) -> str:
    lang = LANGUAGE_NAMES.get(language, "English (Indian)")
    return f"""You are the voice of a hospital OPD intake kiosk in India. Speak ONLY {lang}, warmly, in short simple sentences.
You do not decide what to ask. Messages starting with [KIOSK] come from the kiosk system, never from the patient.
RULES:
1. [KIOSK] SAY: speak exactly the given text in {lang}, nothing more.
2. [KIOSK] ASK, or a submit_answer result with next=ask: ask exactly ONE short question (one sentence) about that topic only. Do not comment on, repeat or summarise earlier answers. No other questions, explanations, advice or greetings.
3. After the patient speaks or taps an answer: IMMEDIATELY call submit_answer and say nothing until you get the result. Fill answers only with facts the patient clearly stated that match the open fields (use the exact field_id). If nothing matches or it is unclear, call submit_answer with an empty answers list.
4. Never invent or assume answers. Never diagnose or give medical advice. Never repeat or rephrase a question unless the result says attempt 2.
5. If the result says next=end: say one short sentence thanking the patient. Do not ask anything.
6. If the patient asks you something, say briefly that the doctor will help, then follow the kiosk."""


def _clean_speech(text: str) -> str:
    """Drop transcription artefacts: "<no speech>", "{pause}", and stray CJK/Hangul characters
    (none of the kiosk's 11 languages use them)."""
    text = re.sub(r"<[^>]*>|\{[^}]*\}", " ", text or "")
    text = re.sub(r"[　-ヿ㐀-䶿一-鿿가-힯＀-￯]+", "", text)
    return re.sub(r"\s{2,}", " ", text).strip()


def _pcm_to_wav(pcm: bytes, rate: int) -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


class LiveVoice:
    """One Gemini Live session per kiosk conversation."""

    def __init__(
        self,
        dm,
        send: Callable[[dict], Awaitable[None]],
        dm_lock: asyncio.Lock,
        after_engine_turn: Callable[[object, str], Awaitable[None]],
    ):
        self.dm = dm
        self.send = send
        self.dm_lock = dm_lock
        self.after_engine_turn = after_engine_turn
        self.session = None
        self._cm = None
        self._rx_task: asyncio.Task | None = None
        self._watchdog: asyncio.Task | None = None
        self.active = False
        self.failed = False

        # per-turn state
        self.phase = "idle"            # idle | speaking | listening | thinking
        self.pending_field: dict | None = None
        self.in_text = ""
        self.out_text = ""
        self.audio_buf = bytearray()
        self.turn_handled = False
        self.sent_audio = False
        self.current_field_id: str | None = None   # field the last question was about
        self._empty_retry = False

    # ── lifecycle ────────────────────────────────────────────────────────

    async def start(self) -> bool:
        if model_config.VOICE_MODE != "live":
            return False
        import os
        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        try:
            from google import genai
            client = (genai.Client(api_key=api_key, http_options={"api_version": "v1beta"}) if api_key
                      else genai.Client(http_options={"api_version": "v1beta"}))
            config = types.LiveConnectConfig(
                response_modalities=["AUDIO"],
                speech_config=types.SpeechConfig(voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(voice_name=model_config.LIVE_VOICE))),
                system_instruction=_system_instruction(self.dm.language),
                input_audio_transcription=types.AudioTranscriptionConfig(),
                output_audio_transcription=types.AudioTranscriptionConfig(),
                tools=[types.Tool(function_declarations=[SUBMIT_ANSWER_TOOL])],
                # Push-to-talk: we mark the start/end of each patient turn ourselves.
                realtime_input_config=types.RealtimeInputConfig(
                    automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)),
            )
            t0 = time.time()
            self._cm = client.aio.live.connect(model=model_config.LIVE_MODEL, config=config)
            self.session = await asyncio.wait_for(self._cm.__aenter__(), timeout=model_config.LIVE_TIMEOUT)
            self._rx_task = asyncio.create_task(self._receive_loop())
            self.active = True
            self.dm.live_mode = True
            logger.info(f"Live session open ({model_config.LIVE_MODEL}) in {time.time() - t0:.2f}s")
            return True
        except Exception as e:
            logger.warning(f"Live session could not start ({type(e).__name__}) — using STT/TTS")
            await self._cleanup()
            return False

    async def close(self):
        self.active = False
        await self._cleanup()

    async def _cleanup(self):
        self.dm.live_mode = False  # engine writes its own questions again
        self._cancel_watchdog()
        if self._rx_task:
            self._rx_task.cancel()
            self._rx_task = None
        if self._cm is not None:
            try:
                await self._cm.__aexit__(None, None, None)
            except Exception:
                pass
            self._cm = None
        self.session = None

    # ── kiosk → Live: speech instructions ────────────────────────────────

    def _strip_identifiers(self, text: str) -> str:
        name = (self.dm.record.patient_name or "").strip()
        if not name:
            return text
        for part in sorted({name, *name.split()}, key=len, reverse=True):
            if len(part) >= 2:
                text = re.sub(re.escape(part), "", text, flags=re.I)
        return re.sub(r"\s{2,}", " ", re.sub(r"\s+([,.!?।])", r"\1", text)).strip(" ,")

    async def say(self, text: str, field_id: str | None = None):
        """Speak an engine-written sentence. Name is never sent to Live.
        In the form phase, a silent note tells Live which field the question is about."""
        if not self.active or not text:
            return
        import speech_provider
        spoken = speech_provider.pronounceable(self._strip_identifiers(text), self.dm.language)
        msg = f"[KIOSK] SAY: {spoken}"
        field = self.dm.field_by_id(field_id) if field_id else None
        self.current_field_id = field["id"] if field is not None else None
        if field is not None:
            import json as _json
            msg += ("\n[KIOSK NOTE — never speak this] This question is about field "
                    f"'{field['id']}'. Open fields: {_json.dumps(self.dm.open_fields(), ensure_ascii=False)}")
        await self._instruct(msg, field=None)

    async def ask(self, field: dict, attempt: int):
        if not self.active:
            return
        self.current_field_id = field["id"]
        self._empty_retry = False
        await self._instruct(self._ask_text("[KIOSK] ASK", field, attempt), field=field)

    def _ask_text(self, prefix: str, field: dict, attempt: int) -> str:
        import json as _json
        extra = " This is attempt 2: rephrase it more simply." if attempt >= 2 else ""
        return (f"{prefix} about: {field.get('question_intent', field['id'])}.{extra}\n"
                f"[KIOSK NOTE — never speak this] This question is about field '{field['id']}'. "
                f"Open fields: {_json.dumps(self.dm.open_fields(), ensure_ascii=False)}")

    async def _instruct(self, text: str, field: dict | None):
        self.phase = "speaking"
        self.pending_field = field
        self.out_text = ""
        self.sent_audio = False
        try:
            await self.session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=text)]), turn_complete=True)
            self._arm_watchdog("speaking")
        except Exception as e:
            await self._fail(f"send failed ({type(e).__name__})")

    # ── client → Live: patient input ─────────────────────────────────────

    async def audio_start(self):
        if not self.active:
            return
        if self.phase == "speaking":
            await self.send({"type": "audio_out_stop"})  # barge-in: stop playback
        self.phase = "listening"
        self.in_text = ""
        self.audio_buf = bytearray()
        self.turn_handled = False
        self._cancel_watchdog()
        try:
            await self.session.send_realtime_input(activity_start=types.ActivityStart())
        except Exception as e:
            await self._fail(f"activity_start failed ({type(e).__name__})")

    async def audio_chunk(self, pcm: bytes):
        if self.phase != "listening":
            return
        if len(self.audio_buf) + len(pcm) > 60 * IN_RATE * 2:  # 60 s cap
            return
        self.audio_buf.extend(pcm)
        if not self.active:
            return
        try:
            await self.session.send_realtime_input(
                audio=types.Blob(data=pcm, mime_type=f"audio/pcm;rate={IN_RATE}"))
        except Exception as e:
            await self._fail(f"audio send failed ({type(e).__name__})")

    async def audio_end(self):
        if self.phase != "listening":
            return
        self.phase = "thinking"
        await self.send({"type": "orb_state", "orb_state": "processing"})
        if len(self.audio_buf) < IN_RATE // 5:  # < 0.1 s: nothing said
            self.phase = "idle"
            await self.send({"type": "live_notice", "message": "no_audio"})
            return
        if not self.active:
            await self._fallback_turn()
            return
        try:
            await self.session.send_realtime_input(activity_end=types.ActivityEnd())
            self._arm_watchdog("thinking")
        except Exception as e:
            await self._fail(f"activity_end failed ({type(e).__name__})")

    async def tap_answer(self, value: str) -> bool:
        """Yes / No / Not sure tapped for the current question: applied directly to that field
        (deterministic, no AI interpretation). Returns False if there is no current field."""
        field = self.dm.field_by_id(self.current_field_id)
        if field is None or self.dm.fsm.state != "DYNAMIC_INTERVIEW":
            return False
        self._cancel_watchdog()
        if self.phase == "speaking":
            await self.send({"type": "audio_out_stop"})
        self.phase = "thinking"
        self.turn_handled = True
        self.audio_buf = bytearray()
        result = await self._apply_answers(
            value, [{"field_id": field["id"], "value": value, "verbatim": value, "confidence": 0.95}])
        if result.get("next") == "ask":
            nf = self.dm.field_by_id(result["field_id"])
            await self.ask(nf, result["attempt"])
        else:  # interview left the question loop (ui already sent)
            self.phase = "idle"
            asyncio.create_task(self.close())
        return True

    async def text_answer(self, value: str):
        """Typed (free-text) answer during the form-filling phase — Live interprets it."""
        self.phase = "thinking"
        self.in_text = value
        self.audio_buf = bytearray()
        self.turn_handled = False
        try:
            await self.session.send_client_content(
                turns=types.Content(role="user", parts=[types.Part(text=f"(patient tapped/typed) {value}")]),
                turn_complete=True)
            self._arm_watchdog("thinking")
        except Exception as e:
            await self._fail(f"text send failed ({type(e).__name__})")

    # ── Live → kiosk ─────────────────────────────────────────────────────

    async def _receive_loop(self):
        try:
            while self.active:
                async for msg in self.session.receive():
                    if msg.tool_call:
                        await self._on_tool_call(msg.tool_call)
                    sc = msg.server_content
                    if sc:
                        if sc.input_transcription and sc.input_transcription.text:
                            self.in_text += sc.input_transcription.text
                        if sc.output_transcription and sc.output_transcription.text and self.phase == "speaking":
                            self.out_text += sc.output_transcription.text
                        if sc.model_turn and self.phase == "speaking":
                            for part in sc.model_turn.parts or []:
                                if part.inline_data and part.inline_data.data:
                                    if not self.sent_audio:
                                        self.sent_audio = True
                                        self._arm_watchdog("speaking")  # reset: audio is flowing
                                    await self.send({"type": "audio_out", "rate": OUT_RATE,
                                                     "data": base64.b64encode(part.inline_data.data).decode()})
                        if sc.turn_complete:
                            await self._on_turn_complete()
                    if msg.go_away is not None:
                        logger.info("Live server asked to reconnect (go_away) — switching to STT/TTS")
                        await self._fail("go_away")
                        return
        except asyncio.CancelledError:
            raise
        except Exception as e:
            await self._fail(f"receive failed ({type(e).__name__})")

    async def _on_turn_complete(self):
        if self.phase == "speaking" and not (self.sent_audio or self.out_text):
            return  # end of the tool-call turn; the spoken question follows (watchdog guards it)
        if self.phase == "speaking":
            self._cancel_watchdog()
            spoken = _clean_speech(self.out_text)
            if self.pending_field is not None and not spoken:
                # Live produced no real question (e.g. "<no speech>"): retry once, then fall back.
                if not self._empty_retry:
                    self._empty_retry = True
                    logger.info("Live gave no spoken question — retrying once")
                    await self._instruct(self._ask_text(
                        "[KIOSK] ASK", self.pending_field,
                        self.dm.asked_counts.get(self.pending_field["id"], 1)), field=self.pending_field)
                    return
                await self._fail("no spoken question after retry")
                return
            await self.send({"type": "audio_out_end"})
            if self.pending_field is not None:
                async with self.dm_lock:
                    ui = self.dm.live_question_ui(self.pending_field, spoken)
                await self.send({"type": "ui", **ui, "voice": "live"})
            self.phase = "idle"
            if self.dm.fsm.state not in ("CHIEF_COMPLAINT", "DYNAMIC_INTERVIEW"):
                asyncio.create_task(self.close())  # thank-you spoken; interview moved on
        elif self.phase == "thinking" and not self.turn_handled:
            # Live answered without calling the tool: let the normal engine handle this turn.
            logger.info("Live replied without submit_answer — handling turn with the text engine")
            self.turn_handled = True
            self._cancel_watchdog()
            await self._engine_turn(self.in_text.strip())

    async def _on_tool_call(self, tool_call):
        responses = []
        for fc in tool_call.function_calls or []:
            if fc.name != "submit_answer" or self.turn_handled or self.phase != "thinking":
                responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response={
                    "next": "wait", "instruction": "Duplicate call ignored. Say nothing extra."}))
                continue
            self.turn_handled = True
            self._cancel_watchdog()
            args = dict(fc.args or {})
            answers = [a for a in (args.get("answers") or []) if isinstance(a, dict)]
            transcript = self.in_text.strip() or "; ".join(str(a.get("verbatim", "")) for a in answers)
            result = await self._apply_answers(transcript, answers)
            responses.append(types.FunctionResponse(id=fc.id, name=fc.name, response=result))
        try:
            await self.session.send_tool_response(function_responses=responses)
        except Exception as e:
            await self._fail(f"tool response failed ({type(e).__name__})")

    async def _apply_answers(self, transcript: str, answers: list[dict]) -> dict:
        state = self.dm.fsm.state
        if state != "DYNAMIC_INTERVIEW":
            # Chief-complaint phase: the engine (reliable model) handles it, Live stays silent.
            asyncio.create_task(self._engine_turn(transcript))
            self.phase = "idle"
            return {"next": "wait", "instruction": "Say nothing."}

        loop = asyncio.get_running_loop()
        async with self.dm_lock:
            res = await loop.run_in_executor(None, self.dm.apply_live_answers, transcript, answers)
        # DB checkpoint / queue escalation off the critical path (Live answers first)
        asyncio.create_task(self.after_engine_turn(self.dm, state))
        if "ui" in res:
            ui = res["ui"]
            await self.send({"type": "ui", **ui})
            self.phase = "speaking"
            self.pending_field = None
            self.out_text = ""
            self.sent_audio = False
            return {"next": "end", "saved": res.get("saved", [])}

        field = res["next_field"]
        self.phase = "speaking"
        self.pending_field = field
        self.current_field_id = field["id"]
        self._empty_retry = False
        self.out_text = ""
        self.sent_audio = False
        self._arm_watchdog("speaking")
        out = {
            "saved": res.get("saved", []),
            "next": "ask",
            "field_id": field["id"],
            "ask_about": field.get("question_intent", field["id"]),
            "attempt": res["attempt"],
            "open_fields": self.dm.open_fields(),
        }
        if res["attempt"] >= 2:
            out["instruction"] = ("Attempt 2 (last): the patient did not answer this. Ask it once more in "
                                  "DIFFERENT, simpler words, e.g. with a short example. Do not repeat the same sentence.")
        return out

    # ── engine path (chief complaint, tool skipped, fallback) ────────────

    async def _engine_turn(self, transcript: str):
        if not transcript:
            self.phase = "idle"
            await self.send({"type": "live_notice", "message": "not_heard"})
            return
        loop = asyncio.get_running_loop()
        async with self.dm_lock:
            prev = self.dm.fsm.state
            await self.send({"type": "orb_state", "orb_state": "processing"})
            ui = await loop.run_in_executor(None, self.dm.process_patient_input, "voice", transcript)
        await self.after_engine_turn(self.dm, prev)
        await self.speak_ui(ui)

    async def speak_ui(self, ui: dict):
        """Send an engine UI to the client and have Live speak its prompt (else the client uses TTS)."""
        if self.active and ui.get("live_ask") and ui.get("current_field_id"):
            # Engine chose the field; Live phrases the question. The UI (with the spoken text)
            # is sent when Live finishes speaking.
            field = self.dm.field_by_id(ui["current_field_id"])
            if field is not None:
                await self.send({"type": "orb_state", "orb_state": "processing"})
                await self.ask(field, self.dm.asked_counts.get(field["id"], 1))
                return
        live_ok = self.active and ui.get("screen") == "conversation" and ui.get("prompt")
        await self.send({"type": "ui", **ui, **({"voice": "live"} if live_ok else {})})
        if live_ok:
            await self.say(ui["prompt"], field_id=ui.get("current_field_id"))
        else:
            self.phase = "idle"
            if ui.get("screen") != "conversation":
                asyncio.create_task(self.close())  # interview over: release the Live session

    async def _fallback_turn(self):
        """Live is unavailable mid-turn: transcribe the buffered audio with the backup STT."""
        pcm = bytes(self.audio_buf)
        self.audio_buf = bytearray()
        self.turn_handled = True
        loop = asyncio.get_running_loop()
        if not pcm:
            typed = self.in_text.strip()
            if typed:
                # The patient tapped/typed this turn: let the text engine handle it
                logger.info("Live fallback: handling the typed/tapped answer with the text engine")
                await self._engine_turn(typed)
            else:
                # Nothing to process: re-send the current question so the kiosk never stays stuck
                async with self.dm_lock:
                    ui = await loop.run_in_executor(None, self.dm.resume_session)
                await self.send({"type": "ui", **ui})
                self.phase = "idle"
            return
        logger.info(f"Live fallback: transcribing {len(pcm)} B of buffered audio with the backup STT")
        import speech_provider
        res = await loop.run_in_executor(
            None, speech_provider.speech_to_text, _pcm_to_wav(pcm, IN_RATE), self.dm.language, "wav")
        await self._engine_turn((res or {}).get("transcript", "").strip())

    # ── failure handling ─────────────────────────────────────────────────

    def _arm_watchdog(self, phase: str):
        self._cancel_watchdog()

        async def _wd():
            await asyncio.sleep(model_config.LIVE_TIMEOUT)
            if self.phase == phase and self.active:
                await self._fail(f"no reply in {model_config.LIVE_TIMEOUT:.0f}s while {phase}")

        self._watchdog = asyncio.create_task(_wd())

    def _cancel_watchdog(self):
        if self._watchdog and not self._watchdog.done() and self._watchdog is not asyncio.current_task():
            self._watchdog.cancel()
        self._watchdog = None

    async def _fail(self, reason: str):
        if self.failed:
            return
        self.failed = True
        was_phase = self.phase
        logger.warning(f"Live voice failed: {reason} — switching to STT/TTS")
        self.active = False
        await self.send({"type": "live_status", "state": "fallback"})
        await self.send({"type": "audio_out_stop"})
        try:
            if was_phase in ("listening", "thinking") and not self.turn_handled:
                await self._fallback_turn()
            elif was_phase == "speaking":
                # Re-send the current question as a normal UI so the client speaks it with TTS.
                async with self.dm_lock:
                    loop = asyncio.get_running_loop()
                    ui = await loop.run_in_executor(None, self.dm.resume_or_current_ui)
                await self.send({"type": "ui", **ui})
        finally:
            self.phase = "idle"
            asyncio.create_task(self._cleanup())
