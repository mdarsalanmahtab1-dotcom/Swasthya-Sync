"""
SwasthyaSync v4 — Dialogue Manager (Dynamic Schema-Driven)

Orchestrates the Two-Stage LLM Pipeline:
  Stage 1: After chief complaint → generate dynamic schema (once)
  Stage 2: Per turn → extract fields + generate next question (loop)

The Dialogue Manager:
  - Manages the simplified macro-FSM
  - Manages demographics collection
  - Calls schema_generator once after chief complaint
  - Runs the extract → select → question loop per turn
  - Runs the safety watchdog after every state update
  - Handles navigation (back, skip)
"""

from __future__ import annotations
import asyncio
import logging
import re

from macro_fsm import MacroFSM
from patient_record import PatientRecord, SlotValue, RedFlagEntry
import conversation_engine
import llm_client
import schema_generator
import field_selector
from red_flag_library import check_safety
from complaint_qualifier import needs_qualification, build_enriched_complaint

logger = logging.getLogger(__name__)

def _normalize_answer(value) -> str:
    """Make Live's answers match what the deterministic red-flag rules look for
    (they match words like "yes"/"no", so booleans must become Yes/No)."""
    if isinstance(value, bool):
        return "Yes" if value else "No"
    v = str(value or "").strip()
    low = v.lower()
    if low in ("true", "yes", "y", "present", "positive"):
        return "Yes"
    if low in ("false", "no", "n", "absent", "negative", "none"):
        return "No"
    return v


_OPEN_Q_EN = re.compile(r"^\s*(what|when|where|which|how|why|who|describe|tell)\b", re.I)
_YESNO_Q_EN = re.compile(r"^\s*(do|does|did|is|are|was|were|have|has|had|can|could|any|will|would|should)\b", re.I)
# Words that open a yes/no question in Indic languages (Hindi/Marathi, Bengali, Gujarati, Punjabi, Odia)
_YESNO_Q_INDIC = ("क्या", "कि ", "কি", "কী", "শুধু", "શું", "ਕੀ", "କଣ", "କି")
_OPEN_Q_INDIC = ("कब", "कहाँ", "कहां", "कैसे", "कितन", "कौन", "क्यों", "কখন", "কোথায়", "কত", "কেমন", "ક્યારે", "ਕਦੋਂ", "ਕਿੱਥੇ")


def _is_yes_no_question(field: dict, spoken_text: str) -> bool:
    """Show Yes/No buttons only for yes/no questions; open questions ("when…", "where…")
    get just "Not sure" (the patient speaks or types the real answer)."""
    text = (spoken_text or "").strip()
    intent = str(field.get("question_intent", "") or "")
    if _OPEN_Q_EN.match(text) or any(w in text for w in _OPEN_Q_INDIC):
        return False
    if _YESNO_Q_EN.match(text) or any(text.startswith(w) or f" {w}" in text[:40] for w in _YESNO_Q_INDIC):
        return True
    # Fall back to the field's English intent
    if _OPEN_Q_EN.match(intent) or re.search(r"\b(when|where|how long|how many|how much|which|duration|location|character|severity|onset)\b", intent, re.I):
        return False
    return bool(_YESNO_Q_EN.match(intent) or re.search(r"\b(presence|history of|yes/no|any)\b", intent, re.I))


# Tap options shown under questions spoken by Gemini Live
_YES_NO = {
    "en-IN": ("Yes", "No", "Not sure"),
    "hi-IN": ("हाँ", "नहीं", "पता नहीं"),
    "bn-IN": ("হ্যাঁ", "না", "জানি না"),
    "ta-IN": ("ஆம்", "இல்லை", "தெரியவில்லை"),
    "te-IN": ("అవును", "కాదు", "తెలియదు"),
    "kn-IN": ("ಹೌದು", "ಇಲ್ಲ", "ಗೊತ್ತಿಲ್ಲ"),
    "mr-IN": ("हो", "नाही", "माहीत नाही"),
    "gu-IN": ("હા", "ના", "ખબર નથી"),
    "ml-IN": ("അതെ", "ഇല്ല", "അറിയില്ല"),
    "pa-IN": ("ਹਾਂ", "ਨਹੀਂ", "ਪਤਾ ਨਹੀਂ"),
    "or-IN": ("ହଁ", "ନା", "ଜାଣି ନାହିଁ"),
}


class DialogueManager:
    """
    Orchestrates a single patient session with dynamic schema-driven interview.
    """

    def __init__(self, clinic_mode: str = "allopathic", language: str = "en-IN"):
        import time
        self.fsm = MacroFSM(clinic_mode=clinic_mode)
        self.record = PatientRecord(clinic_mode=clinic_mode, language=language)
        self.language = language
        self.last_active_time = time.time()
        # Qualifier state: if the chief complaint is vague, we hold
        # the pending qualifier here and ask ONE clarifying question
        # before generating the schema.
        self._pending_qualifier: dict | None = None
        self._raw_chief_complaint: str = ""
        # Loop guard: how many times each field has been asked. After MAX_ASKS without an
        # answer the field is marked "Unanswered" and never asked again (no repeats, no loops).
        self.asked_counts: dict[str, int] = {}
        # True while Gemini Live is speaking for this session: Live phrases each question from the
        # field the engine selected, so the text-model question writer is skipped (faster, and no
        # dependency on the text API inside the loop). Set by live_voice.LiveVoice.
        self.live_mode: bool = False

    # ──────────────────────────────────────────────────────────────────
    # PUBLIC API
    # ──────────────────────────────────────────────────────────────────

    def start_session(self) -> dict:
        """Initialize and return the first UI instruction."""
        import time
        self.last_active_time = time.time()
        self.fsm.set_state("CHIEF_COMPLAINT")
        self.record.macro_state = "CHIEF_COMPLAINT"
        return self._build_ui_instruction()

    def set_demographics(self, name: str, age: int | None, sex: str, weight: float | None = None, height: str | None = None, vitals: str | None = None):
        """Set patient demographics (called from frontend before interview)."""
        import time
        self.last_active_time = time.time()
        self.record.patient_name = name
        self.record.patient_age = age
        self.record.patient_sex = sex
        if weight:
            self.record.update_filled_state("weight", weight, confidence=1.0)
        if height:
            self.record.update_filled_state("height", height, confidence=1.0)
        if vitals:
            self.record.update_filled_state("vitals", vitals, confidence=1.0)
        logger.info(f"Demographics set: age={age}, sex={sex}, weight={weight}, height={height}, vitals={vitals}")

    def set_previous_history(self, history: dict | None):
        """Inject previous encounter history for follow-up context."""
        import time
        self.last_active_time = time.time()
        if history:
            self.record.previous_history = history
            logger.info("Previous history injected for follow-up.")

    def process_patient_input(self, input_type: str, value: str) -> dict:
        """
        Process a patient's response and return the next UI instruction.
        input_type: "tap" | "voice" | "skip" | "back" | "next"
        value: the selected option text or voice transcript
        """
        import time
        self.last_active_time = time.time()
        state = self.fsm.state

        # ── Navigation actions ──
        if input_type == "back":
            self.fsm.go_back()
            if self.fsm.state == "SCHEMA_GENERATION":
                self.fsm.go_back()
            self.record.macro_state = self.fsm.state
            return self._build_ui_instruction()

        if input_type == "skip":
            self.fsm.advance()
            self.record.macro_state = self.fsm.state
            return self._build_ui_instruction()

        # ── INIT: advance to demographics ──
        if state == "INIT":
            self.fsm.advance()
            self.record.macro_state = self.fsm.state
            return self._build_ui_instruction()

        # ── DEMOGRAPHICS: advance to chief complaint ──
        if state == "DEMOGRAPHICS":
            self.fsm.advance()
            self.record.macro_state = self.fsm.state
            return self._build_ui_instruction()

        # ── CHIEF_COMPLAINT: capture + classify + generate schema ──
        if state == "CHIEF_COMPLAINT":
            return self._handle_chief_complaint(value)

        # ── SCHEMA_GENERATION: should auto-advance (schema already generated) ──
        if state == "SCHEMA_GENERATION":
            self.fsm.advance()  # → DYNAMIC_INTERVIEW
            self.record.macro_state = self.fsm.state
            return self._build_ui_instruction()

        # ── DYNAMIC_INTERVIEW: the main loop ──
        if state == "DYNAMIC_INTERVIEW":
            return self._handle_dynamic_turn(input_type, value)

        # ── DOCUMENT_SCAN / SUMMARY_CONFIRMATION: advance on next ──
        if state in ("DOCUMENT_SCAN", "SUMMARY_CONFIRMATION"):
            # 1. The Database Checkpoint: Save before advancing
            if state == "DOCUMENT_SCAN":
                try:
                    import database
                    # Serialize the volatile RAM data
                    record_payload = {
                        "filled_state": self.record.filled_state,
                        "document_extractions": [e.model_dump() for e in self.record.document_extractions],
                        "red_flags": [r.model_dump() for r in self.record.red_flags]
                    }
                    has_red_flags = len(self.record.red_flags) > 0
                    pass
                except Exception as e:
                    logger.error(f"🚨 CRITICAL FAULT: Failed to persist session {self.record.session_id} - {e}")

            # 2. Advance the FSM safely
            self.fsm.advance()
            self.record.macro_state = self.fsm.state
            return self._build_ui_instruction()

        # Fallback
        return self._build_ui_instruction()

    def resume_session(self) -> dict:
        """Re-construct the UI state for the current step without side-effects."""
        state = self.fsm.state
        if state == "DYNAMIC_INTERVIEW":
            # Find the last assistant message
            last_msg = None
            for msg in reversed(self.record.conversation_history):
                if msg["role"] == "assistant":
                    last_msg = msg["content"]
                    break
            
            import field_selector
            schema = self.record.dynamic_schema or {"fields": []}
            progress = field_selector.get_progress(schema, self.record.filled_state)
            category_label = field_selector.get_current_category_label(schema, self.record.filled_state)
            
            return {
                "macro_state": "DYNAMIC_INTERVIEW",
                "clinic_mode": self.record.clinic_mode,
                "session_id": self.record.session_id,
                "language": self.language,
                "screen": "conversation",
                "orb_state": "idle",
                "prompt": last_msg or "Let's continue.",
                "options": [], 
                "section_label": category_label,
                "can_skip": True,
                "progress": progress,
                "section_summary": self.record.get_filled_summary(),
                "conversation_history": self.record.conversation_history[-6:],
            }
        elif state == "CHIEF_COMPLAINT":
            last_msg = None
            for msg in reversed(self.record.conversation_history):
                if msg["role"] == "assistant":
                    last_msg = msg["content"]
                    break
            return {
                "macro_state": state,
                "clinic_mode": self.record.clinic_mode,
                "session_id": self.record.session_id,
                "language": self.language,
                "screen": "conversation",
                "orb_state": "idle",
                "prompt": last_msg or "What brings you in today?",
                "options": [],
                "section_label": "Chief Complaint",
                "can_skip": False,
                "progress": {"done": 0, "total": 1, "percent": 0, "label": "Getting started"},
                "section_summary": "",
                "conversation_history": [],
            }
        else:
            return self._build_ui_instruction()

    def process_redflag(self) -> dict:
        self.fsm.trigger_redflag()
        self.record.macro_state = self.fsm.state
        return self._build_ui_instruction()

    def clear_redflag(self) -> dict:
        self.fsm.resolve_interrupt()
        self.record.macro_state = self.fsm.state
        return self._build_ui_instruction()

    def get_record(self) -> dict:
        return self.record.model_dump()

    # ──────────────────────────────────────────────────────────────────
    # CHIEF COMPLAINT HANDLER
    # ──────────────────────────────────────────────────────────────────

    def _handle_chief_complaint(self, value: str) -> dict:
        """
        Capture chief complaint → qualify if vague → classify → generate schema → advance.

        Two-turn flow for vague complaints:
          Turn 1: Patient says 'pain' → we detect it's vague → ask 'where is the pain?'
          Turn 2: Patient says 'chest' → enriched complaint = 'pain in the Chest'
                  → schema generated with full context → cardiac questions, not joint questions
        """
        # ── TURN 2: Qualifier answer arrived ──
        if self._pending_qualifier is not None:
            qualifier = self._pending_qualifier
            self._pending_qualifier = None
            enriched = build_enriched_complaint(self._raw_chief_complaint, qualifier, value)
            logger.info(f"Qualifier answer: '{value}' → enriched complaint: '{enriched}'")
            # Store the enriched complaint and proceed to schema generation
            return self._generate_schema_and_advance(enriched)

        # ── TURN 1: Fresh chief complaint ──
        self._raw_chief_complaint = value
        self.record.add_conversation_message("patient", value, "CHIEF_COMPLAINT")

        # Check if this complaint is too vague to generate a good schema
        qualifier = needs_qualification(value)
        if qualifier:
            # Hold the qualifier, ask the clarifying question now
            self._pending_qualifier = qualifier
            logger.info(f"Chief complaint '{value}' is vague — asking qualifier: {qualifier['id']}")

            # Translate options for non-English sessions
            options_out = []
            for opt in qualifier["options"]:
                options_out.append({
                    "label": opt.get("label_translated", opt.get("label", "")),
                    "value": opt.get("label", ""),
                    "icon": None,
                })

            self.record.add_conversation_message(
                "assistant", qualifier["question"], "CHIEF_COMPLAINT"
            )
            return {
                "macro_state": "CHIEF_COMPLAINT",
                "clinic_mode": self.record.clinic_mode,
                "session_id": self.record.session_id,
                "language": self.language,
                "screen": "conversation",
                "orb_state": "idle",
                "prompt": qualifier["question"],
                "options": options_out,
                "section_label": "Chief Complaint",
                "can_skip": False,
                "progress": {"done": 0, "total": 1, "percent": 0, "label": "Getting started"},
                "section_summary": "",
                "conversation_history": self.record.conversation_history[-4:],
            }

        # Complaint is already specific — go straight to schema generation
        return self._generate_schema_and_advance(value)

    def _generate_schema_and_advance(self, enriched_complaint: str) -> dict:
        """Classify complaint, generate schema, advance FSM to DYNAMIC_INTERVIEW."""
        # Store the (possibly enriched) chief complaint
        self.record.chief_complaint = SlotValue(
            value=enriched_complaint, confidence=0.95, source="direct"
        )

        # Classify
        category = llm_client.classify_complaint(enriched_complaint, self.language)
        self.record.complaint_category = category
        logger.info(f"Chief complaint classified: '{enriched_complaint}' → {category}")

        # Generate dynamic schema (Stage 1)
        logger.info("Starting Stage 1: dynamic schema generation...")
        schema = schema_generator.generate_schema(
            chief_complaint=enriched_complaint,
            patient_age=self.record.patient_age,
            patient_sex=self.record.patient_sex,
            category=category,
            doctor_custom_instructions=self.record.doctor_custom_instructions,
            clinic_mode=self.record.clinic_mode,
        )
        self.record.dynamic_schema = schema

        # Initialize filled_state with all fields as unfilled
        for field in schema.get("fields", []):
            self.record.filled_state[field["id"]] = {"value": None, "confidence": 0.0}

        field_count = len(schema.get("fields", []))
        logger.info(f"Schema generated: {field_count} fields for category '{category}'")

        # Pre-fill schema slots directly volunteered in chief complaint
        if self.record.clinic_mode != "ayush":
            try:
                pre_extracted = conversation_engine.extract_from_response(
                    patient_message=enriched_complaint,
                    unfilled_fields=schema.get("fields", []),
                    filled_summary="",
                    conversation_history=self.record.conversation_history,
                    language=self.language,
                    doctor_custom_instructions=self.record.doctor_custom_instructions,
                )
                for fid, entry in pre_extracted.items():
                    if entry.get("value"):
                        self.record.update_filled_state(
                            fid,
                            entry.get("value"),
                            entry.get("confidence", 0.8),
                        )
            except Exception as e:
                logger.warning(f"Chief complaint pre-extraction failed: {e}")

        # Skip SCHEMA_GENERATION state and go directly to DYNAMIC_INTERVIEW
        self.fsm.set_state("DYNAMIC_INTERVIEW")
        self.record.macro_state = "DYNAMIC_INTERVIEW"

        return self._build_ui_instruction()

    # ──────────────────────────────────────────────────────────────────
    # DYNAMIC INTERVIEW HANDLER (Stage 2 loop)
    # ──────────────────────────────────────────────────────────────────

    def _handle_dynamic_turn(self, input_type: str, value: str) -> dict:
        """
        Handle one turn of the dynamic interview.
        1. Extract fields from patient's message
        2. Run safety check
        3. Check if interview is complete
        4. Select next field
        5. Generate question for that field
        """
        schema = self.record.dynamic_schema or {"fields": []}

        # 1. Record patient's message
        self.record.add_conversation_message("patient", value, "DYNAMIC_INTERVIEW")
        self.record.interview_turn_count += 1

        # 2. EXTRACTION: get field values from patient's response
        unfilled_fields = [
            f for f in schema.get("fields", [])
            if not (self.record.filled_state.get(f["id"], {}).get("value"))
        ]

        extracted = conversation_engine.extract_from_response(
            patient_message=value,
            unfilled_fields=unfilled_fields,
            filled_summary=self.record.get_filled_summary(),
            conversation_history=self.record.conversation_history,
            language=self.language,
            doctor_custom_instructions=self.record.doctor_custom_instructions,
        )

        # 3. Update filled_state with extracted data
        for fid, entry in extracted.items():
            self.record.update_filled_state(
                fid,
                entry.get("value"),
                entry.get("confidence", 0.8),
            )
            logger.debug(f"Filled: {fid} = {entry.get('value')}")

        ui, next_field = self._after_answers(extracted)
        if ui is not None:
            return ui

        if self.live_mode:
            self.note_asked(next_field["id"])
            return self.live_field_ui(next_field)

        # 7. GENERATE QUESTION for the selected field
        result = conversation_engine.generate_question(
            target_field=next_field,
            filled_summary=self.record.get_filled_summary(),
            conversation_history=self.record.conversation_history,
            language=self.language,
            patient_message=value,
            chief_complaint=self.record.chief_complaint.value if self.record.chief_complaint else "",
            patient_age=self.record.patient_age,
            patient_sex=self.record.patient_sex,
            previous_history=self.record.previous_history,
            doctor_custom_instructions=self.record.doctor_custom_instructions,
        )

        # Store assistant's question
        self.record.add_conversation_message(
            "assistant", result.spoken_text, next_field.get("category", "HPI")
        )
        self.note_asked(next_field["id"])

        # Removed db checkpoint here, handled in main.py

        # 8. Build UI response
        return self._build_dynamic_ui(result, next_field)

    # ──────────────────────────────────────────────────────────────────
    # SHARED: after answers are known (used by the text engine AND Gemini Live)
    # ──────────────────────────────────────────────────────────────────

    MAX_ASKS = 2  # upper bound (red-flag safety fields); ordinary fields are asked once

    @staticmethod
    def max_asks(field: dict) -> int:
        """Ordinary fields are asked once. Red-flag safety fields get ONE simpler re-ask,
        because silently skipping e.g. a stroke-sign question is clinically risky."""
        return 2 if field.get("red_flag") else 1

    def note_asked(self, field_id: str) -> int:
        self.asked_counts[field_id] = self.asked_counts.get(field_id, 0) + 1
        return self.asked_counts[field_id]

    def select_next_field(self) -> dict | None:
        """next_field() with the loop guard: fields asked max_asks() times are marked Unanswered."""
        schema = self.record.dynamic_schema or {"fields": []}
        for _ in range(len(schema.get("fields", [])) + 1):
            nf = field_selector.next_field(schema, self.record.filled_state)
            if nf is None:
                return None
            if self.asked_counts.get(nf["id"], 0) >= self.max_asks(nf):
                self.record.filled_state[nf["id"]] = {
                    "value": field_selector.UNANSWERED_VALUE, "confidence": 0.0, "unanswered": True,
                }
                logger.info(f"Field '{nf['id']}' asked {self.asked_counts[nf['id']]}x without an answer — marked Unanswered")
                continue
            return nf
        return None

    def _after_answers(self, extracted: dict) -> tuple[dict | None, dict | None]:
        """Fork check -> deterministic safety rules -> completion -> next field.
        Returns (ui, None) when the interview leaves the question loop, else (None, next_field)."""
        schema = self.record.dynamic_schema or {"fields": []}

        # FORK CHECK: did we just fill a fork_eligible field with a significant answer?
        for fid, entry in extracted.items():
            field_spec = next((f for f in schema.get("fields", []) if f["id"] == fid), None)
            if field_spec and field_spec.get("fork_eligible", False):
                fork_result = conversation_engine.check_and_generate_fork_questions(
                    parent_field=field_spec,
                    patient_answer=str(entry.get("value", "")),
                    chief_complaint=self.record.chief_complaint.value if self.record.chief_complaint else "",
                    language=self.language,
                    doctor_custom_instructions=self.record.doctor_custom_instructions,
                )
                if fork_result:
                    existing_ids = {f["id"] for f in schema["fields"]}
                    for sub_field in fork_result:
                        sub_id = sub_field["id"]
                        if sub_id not in existing_ids:
                            schema["fields"].append(sub_field)
                            self.record.filled_state[sub_id] = {"value": None, "confidence": 0.0}
                    logger.info(f"Fork triggered on '{fid}': injected {len(fork_result)} sub-fields")

        # SAFETY CHECK: deterministic rules (never LLM-only)
        safety_flags = check_safety(self.record.filled_state)
        if safety_flags:
            existing_ids = {f.rule_id for f in self.record.red_flags}
            new_flags = [f for f in safety_flags if f.rule_id not in existing_ids]
            if new_flags:
                self.record.red_flags.extend(new_flags)
                self.fsm.trigger_redflag()
                self.record.macro_state = self.fsm.state
                return self._build_ui_instruction(red_flags=new_flags), None

        # COMPLETION
        if field_selector.is_interview_complete(
            schema, self.record.filled_state, self.record.interview_turn_count
        ):
            logger.info(f"Interview complete at turn {self.record.interview_turn_count}")
            self.fsm.advance()  # -> DOCUMENT_SCAN
            self.record.macro_state = self.fsm.state
            return self._build_ui_instruction(), None

        # NEXT FIELD (with loop guard)
        next_field = self.select_next_field()
        if next_field is None:
            self.fsm.advance()
            self.record.macro_state = self.fsm.state
            return self._build_ui_instruction(), None
        return None, next_field

    # ──────────────────────────────────────────────────────────────────
    # GEMINI LIVE HOOKS (Live fills the form; this engine stays in charge)
    # ──────────────────────────────────────────────────────────────────

    def open_fields(self, limit: int = 15) -> list[dict]:
        schema = self.record.dynamic_schema or {"fields": []}
        out = []
        for f in schema.get("fields", []):
            entry = self.record.filled_state.get(f["id"], {})
            if not (isinstance(entry, dict) and entry.get("value")):
                out.append({"field_id": f["id"], "about": f.get("question_intent", f["id"])})
        return out[:limit]

    def apply_live_answers(self, transcript: str, answers: list[dict]) -> dict:
        """Apply answers Gemini Live extracted (tool call) for one patient utterance.

        Only known, still-open field ids are accepted; duplicates/unknown ids are dropped.
        Returns {"ui": dict} when leaving the question loop (red flag / complete), otherwise
        {"next_field": dict, "attempt": int, "saved": [ids]}.
        """
        schema = self.record.dynamic_schema or {"fields": []}
        valid = {f["id"] for f in schema.get("fields", [])}
        self.record.add_conversation_message("patient", transcript or "", "DYNAMIC_INTERVIEW")
        self.record.interview_turn_count += 1

        extracted: dict = {}
        for a in answers or []:
            fid = str(a.get("field_id", "")).strip()
            val = _normalize_answer(a.get("value", ""))
            if not fid or not val or fid not in valid or fid in extracted:
                continue
            current = self.record.filled_state.get(fid, {})
            if isinstance(current, dict) and current.get("value") and not current.get("unanswered"):
                continue  # already answered — ignore repeats
            try:
                conf = float(a.get("confidence", 0.85))
            except (TypeError, ValueError):
                conf = 0.85
            extracted[fid] = {"value": val, "verbatim": a.get("verbatim", ""), "confidence": conf}
            self.record.update_filled_state(fid, val, conf)
        if extracted:
            logger.info(f"Live answers saved: {list(extracted)}")

        ui, next_field = self._after_answers(extracted)
        if ui is not None:
            return {"ui": ui, "saved": list(extracted)}
        attempt = self.note_asked(next_field["id"])
        return {"next_field": next_field, "attempt": attempt, "saved": list(extracted)}

    def live_field_ui(self, field: dict) -> dict:
        """UI for the next field when Gemini Live will phrase the question (prompt filled in
        after Live has spoken it, via live_question_ui)."""
        result = conversation_engine.ConversationResult({
            "spoken_text": "", "suggested_options": [], "extracted_fields": {},
            "red_flag_check": None, "reasoning": "live", "current_category": field.get("category", "HPI"),
        })
        ui = self._build_dynamic_ui(result, field)
        ui["live_ask"] = True
        return ui

    def field_by_id(self, field_id: str | None) -> dict | None:
        schema = self.record.dynamic_schema or {"fields": []}
        return next((f for f in schema.get("fields", []) if f["id"] == field_id), None)

    def resume_or_current_ui(self) -> dict:
        """Question for the current field written by the text engine (used when Live drops
        mid-question). Does not count as a new ask."""
        if self.fsm.state != "DYNAMIC_INTERVIEW":
            return self.resume_session()
        nf = self.select_next_field()
        if nf is None:
            return self._build_ui_instruction()
        result = conversation_engine.generate_question(
            target_field=nf,
            filled_summary=self.record.get_filled_summary(),
            conversation_history=self.record.conversation_history,
            language=self.language,
            chief_complaint=str(self.record.chief_complaint.value or "") if self.record.chief_complaint else "",
            patient_age=self.record.patient_age,
            patient_sex=self.record.patient_sex,
            previous_history=self.record.previous_history,
        )
        self.record.add_conversation_message("assistant", result.spoken_text, nf.get("category", "HPI"))
        return self._build_dynamic_ui(result, nf)

    def live_question_ui(self, field: dict, spoken_text: str) -> dict:
        """UI for a question Gemini Live just spoke (recorded in the transcript for the doctor)."""
        self.record.add_conversation_message("assistant", spoken_text, field.get("category", "HPI"))
        yes, no, unsure = _YES_NO.get(self.language, _YES_NO["en-IN"])
        options = [{"label": "Not sure", "label_translated": unsure}]
        if _is_yes_no_question(field, spoken_text):
            options = [{"label": "Yes", "label_translated": yes},
                       {"label": "No", "label_translated": no}] + options
        result = conversation_engine.ConversationResult({
            "spoken_text": spoken_text,
            "suggested_options": options,
            "extracted_fields": {}, "red_flag_check": None, "reasoning": "live",
            "current_category": field.get("category", "HPI"),
        })
        ui = self._build_dynamic_ui(result, field)
        ui["current_field_id"] = field["id"]
        return ui

    # ──────────────────────────────────────────────────────────────────
    # UI INSTRUCTION BUILDER
    # ──────────────────────────────────────────────────────────────────

    def _build_dynamic_ui(
        self,
        result: conversation_engine.ConversationResult,
        current_field: dict | None = None,
    ) -> dict:
        """Build UI instruction for the dynamic interview."""
        schema = self.record.dynamic_schema or {"fields": []}
        progress = field_selector.get_progress(schema, self.record.filled_state)
        category_label = field_selector.get_current_category_label(schema, self.record.filled_state)

        options_out = []
        for opt in result.suggested_options:
            options_out.append({
                "label": opt.get("label_translated", opt.get("label", "")),
                "value": opt.get("label", ""),
                "icon": None,
            })

        return {
            "macro_state": "DYNAMIC_INTERVIEW",
            "clinic_mode": self.record.clinic_mode,
            "session_id": self.record.session_id,
            "language": self.language,
            "screen": "conversation",
            "orb_state": "idle",
            "prompt": result.spoken_text,
            "options": options_out,
            "section_label": category_label,
            "can_skip": True,
            "progress": progress,
            "section_summary": self.record.get_filled_summary(),
            "conversation_history": self.record.conversation_history[-6:],
            "current_field_id": current_field["id"] if current_field else None,
        }

    def _build_ui_instruction(
        self,
        red_flags: list | None = None,
    ) -> dict:
        """Build the UI instruction dict for the frontend."""
        state = self.fsm.state

        base = {
            "macro_state": state,
            "clinic_mode": self.record.clinic_mode,
            "session_id": self.record.session_id,
            "language": self.language,
        }

        # ── Interrupt states ──
        if state == "EMERGENCY_PROTOCOL":
            flag_data = red_flags or self.record.red_flags
            return {
                **base,
                "screen": "triage_alert",
                "orb_state": "alert",
                "red_flags": [
                    {"rule_id": f.rule_id, "description": f.description}
                    for f in (flag_data if flag_data else [])
                ],
            }

        if state == "STAFF_ASSIST":
            return {**base, "screen": "staff_assist", "orb_state": "idle"}

        # ── Non-conversational states ──
        if state == "INIT":
            return {**base, "screen": "welcome", "orb_state": "idle"}

        if state == "DEMOGRAPHICS":
            return {**base, "screen": "demographics", "orb_state": "idle"}

        if state == "CHIEF_COMPLAINT":
            # Generate opening question
            result = conversation_engine.generate_opening_question(
                language=self.language,
                patient_name=self.record.patient_name,
                patient_age=self.record.patient_age,
                patient_sex=self.record.patient_sex,
                previous_history=self.record.previous_history,
                doctor_custom_instructions=self.record.doctor_custom_instructions,
            )
            self.record.add_conversation_message(
                "assistant", result.spoken_text, "CHIEF_COMPLAINT"
            )

            options_out = []
            for opt in result.suggested_options:
                options_out.append({
                    "label": opt.get("label_translated", opt.get("label", "")),
                    "value": opt.get("label", ""),
                    "icon": None,
                })

            return {
                **base,
                "screen": "conversation",
                "orb_state": "idle",
                "prompt": result.spoken_text,
                "options": options_out,
                "section_label": "Chief Complaint",
                "can_skip": False,
                "progress": {"done": 0, "total": 1, "percent": 0, "label": "Getting started"},
                "section_summary": "",
                "conversation_history": [],
            }

        if state == "SCHEMA_GENERATION":
            return {
                **base,
                "screen": "schema_generating",
                "orb_state": "processing",
            }

        if state == "DYNAMIC_INTERVIEW":
            # Generate first question for the dynamic interview
            next_f = self.select_next_field()

            if next_f is None:
                self.fsm.advance()
                self.record.macro_state = self.fsm.state
                return self._build_ui_instruction()

            if self.live_mode:
                self.note_asked(next_f["id"])
                return self.live_field_ui(next_f)

            result = conversation_engine.generate_question(
                target_field=next_f,
                filled_summary=self.record.get_filled_summary(),
                conversation_history=self.record.conversation_history,
                language=self.language,
                chief_complaint=str(self.record.chief_complaint.value or ""),
                patient_age=self.record.patient_age,
                patient_sex=self.record.patient_sex,
                previous_history=self.record.previous_history,
            )
            self.record.add_conversation_message(
                "assistant", result.spoken_text, next_f.get("category", "HPI")
            )
            self.note_asked(next_f["id"])

            return self._build_dynamic_ui(result, next_f)

        if state == "DOCUMENT_SCAN":
            return {**base, "screen": "document_scan", "orb_state": "idle", "patient_name": self.record.patient_name}

        if state == "SUMMARY_CONFIRMATION":
            return {
                **base,
                "screen": "summary",
                "orb_state": "success",
                "patient_record": self.record.model_dump(),
            }

        if state == "COMPLETE":
            return {
                **base,
                "screen": "complete",
                "orb_state": "success",
                "patient_record": self.record.model_dump(),
            }

        # Fallback
        return {**base, "screen": "unknown", "orb_state": "idle"}
