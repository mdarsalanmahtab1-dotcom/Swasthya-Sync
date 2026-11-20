"""
Small offline evaluation of the intake engine — 10 SYNTHETIC patients, text answers, no audio.

Runs each script through DialogueManager exactly like the kiosk WebSocket does and reports:
  - % of safety-floor fields filled
  - red flags caught (x/3) and red flags falsely raised
  - average turns per interview and average latency per turn
Writes docs/eval_results.md.

Run from backend/ with GEMINI_API_KEY set:   python scripts/eval_intake.py
Without AI credentials the script stops (use --allow-offline to run anyway; results are then
labelled OFFLINE and NOT written to docs/eval_results.md, so fallback numbers never reach the pitch).
"""
from __future__ import annotations

import argparse
import datetime as dt
import os
import statistics
import sys
import time

HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)

import model_config  # noqa: E402
from dialogue_manager import DialogueManager  # noqa: E402
from red_flag_library import get_safety_floor  # noqa: E402

# All patients are made up. red_flag=True means the script describes an emergency.
SCRIPTS = [
    {"id": "P01", "lang": "en-IN", "age": 56, "sex": "Male", "red_flag": True,
     "complaint": "chest pain",
     "answers": ["Pain in the centre of my chest since 1 hour", "It goes to my left arm and jaw",
                 "I am sweating a lot and feel breathless", "Pressure like heavy weight", "8 out of 10",
                 "I have diabetes and BP", "No allergies", "I smoke 10 cigarettes a day"]},
    {"id": "P02", "lang": "hi-IN", "age": 67, "sex": "Female", "red_flag": True,
     "complaint": "अचानक बोलने में दिक्कत",
     "answers": ["आज सुबह से अचानक बोलने में दिक्कत है", "दाहिना हाथ और पैर कमजोर हो गया है",
                 "चेहरा एक तरफ टेढ़ा हो गया है", "लगभग दो घंटे पहले शुरू हुआ", "बीपी की दवा लेती हूँ",
                 "कोई एलर्जी नहीं", "पहले ऐसा कभी नहीं हुआ"]},
    {"id": "P03", "lang": "bn-IN", "age": 27, "sex": "Female", "red_flag": True,
     "complaint": "গর্ভাবস্থায় রক্তপাত",
     "answers": ["আমি সাত মাসের গর্ভবতী", "আজ সকাল থেকে যোনিপথে রক্তপাত হচ্ছে", "পেটে খুব ব্যথা",
                 "বাচ্চার নড়াচড়া কম লাগছে", "মাথা ঘুরছে", "কোনো ওষুধ খাই না", "কোনো অ্যালার্জি নেই"]},
    {"id": "P04", "lang": "en-IN", "age": 34, "sex": "Male", "red_flag": False,
     "complaint": "fever",
     "answers": ["Fever for 3 days", "Up to 101 F, comes in the evening", "Mild body ache and headache",
                 "No cough, no breathing problem", "Took paracetamol once", "No allergies", "No travel"]},
    {"id": "P05", "lang": "hi-IN", "age": 45, "sex": "Female", "red_flag": False,
     "complaint": "घुटने में दर्द",
     "answers": ["दाहिने घुटने में दर्द", "छह महीने से है", "सीढ़ियाँ चढ़ने में ज्यादा होता है",
                 "सुबह थोड़ी अकड़न रहती है", "सूजन नहीं है", "कोई दवा नहीं", "कोई चोट नहीं लगी"]},
    {"id": "P06", "lang": "bn-IN", "age": 22, "sex": "Male", "red_flag": False,
     "complaint": "কাশি",
     "answers": ["পাঁচ দিন ধরে কাশি", "কফ সাদা", "জ্বর নেই", "শ্বাসকষ্ট নেই",
                 "ধূমপান করি না", "কোনো ওষুধ খাই না", "অ্যালার্জি নেই"]},
    {"id": "P07", "lang": "en-IN", "age": 29, "sex": "Female", "red_flag": False,
     "complaint": "skin rash",
     "answers": ["Itchy rash on both arms", "Since 1 week", "Red small bumps", "No fever",
                 "Started a new soap recently", "No medicines", "No known allergies"]},
    {"id": "P08", "lang": "hi-IN", "age": 52, "sex": "Male", "red_flag": False,
     "complaint": "पेट में जलन",
     "answers": ["खाने के बाद पेट में जलन", "दो हफ्ते से", "खट्टी डकार आती है", "उल्टी नहीं",
                 "काला मल नहीं", "चाय बहुत पीता हूँ", "कोई एलर्जी नहीं"]},
    {"id": "P09", "lang": "bn-IN", "age": 40, "sex": "Female", "red_flag": False,
     "complaint": "মাথাব্যথা",
     "answers": ["দুই দিন ধরে মাথাব্যথা", "কপালের দুই পাশে", "চাপ দেওয়ার মতো", "বমি নেই",
                 "চোখে ঝাপসা দেখা নেই", "কম ঘুম হচ্ছে", "কোনো ওষুধ খাই না"]},
    {"id": "P10", "lang": "en-IN", "age": 61, "sex": "Male", "red_flag": False,
     "complaint": "burning urination",
     "answers": ["Burning while passing urine", "Since 4 days", "Going more often at night",
                 "No blood in urine", "No fever", "I have diabetes", "No allergies"]},
]

MAX_TURNS = 25


def run_script(sc: dict) -> dict:
    dm = DialogueManager(clinic_mode="allopathic", language=sc["lang"])
    dm.set_demographics("Synthetic Patient", sc["age"], sc["sex"])  # name stays out of prompts
    dm.start_session()
    latencies, turns = [], 0
    inputs = [sc["complaint"]] + sc["answers"]
    ui = {}
    i = 0
    while turns < MAX_TURNS:
        value = inputs[i] if i < len(inputs) else "I don't know"
        i += 1
        t0 = time.time()
        ui = dm.process_patient_input("voice", value) or {}
        latencies.append(time.time() - t0)
        turns += 1
        screen = ui.get("screen")
        if screen not in ("conversation", None):
            break
        if i >= len(inputs) + 3:  # answered everything plus a few "don't know"s
            break
    rec = dm.record
    category = getattr(rec, "complaint_category", None) or "general"
    floor_ids = [f.get("id") for f in get_safety_floor(category) if isinstance(f, dict)]
    filled = [fid for fid in floor_ids
              if isinstance(rec.filled_state.get(fid), dict) and rec.filled_state[fid].get("value")]
    return {
        "id": sc["id"], "lang": sc["lang"], "expected_red_flag": sc["red_flag"],
        "red_flag_raised": len(rec.red_flags) > 0, "category": category,
        "floor_total": len(floor_ids), "floor_filled": len(filled),
        "turns": turns, "avg_latency": statistics.mean(latencies) if latencies else 0.0,
        "end_screen": ui.get("screen"),
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--allow-offline", action="store_true")
    args = ap.parse_args()
    online = model_config.get_genai_client() is not None
    if not online and not args.allow_offline:
        print("No Gemini credentials (GEMINI_API_KEY / GOOGLE_GENAI_USE_VERTEXAI). Refusing to produce pitch numbers.")
        return 2

    results = []
    for sc in SCRIPTS:
        try:
            r = run_script(sc)
        except Exception as e:
            r = {"id": sc["id"], "lang": sc["lang"], "expected_red_flag": sc["red_flag"], "error": type(e).__name__}
        results.append(r)
        print(r)

    ok = [r for r in results if "error" not in r]
    floor_tot = sum(r["floor_total"] for r in ok) or 1
    floor_pct = 100.0 * sum(r["floor_filled"] for r in ok) / floor_tot
    rf_expected = [r for r in ok if r["expected_red_flag"]]
    caught = sum(1 for r in rf_expected if r["red_flag_raised"])
    false_rf = sum(1 for r in ok if not r["expected_red_flag"] and r["red_flag_raised"])
    avg_turns = statistics.mean(r["turns"] for r in ok) if ok else 0
    avg_lat = statistics.mean(r["avg_latency"] for r in ok) if ok else 0

    mode = model_config.client_mode() if online else "OFFLINE (no AI — not valid for the pitch)"
    summary = (
        f"- Safety-floor fields filled: **{floor_pct:.0f}%**\n"
        f"- Red flags caught: **{caught}/{len(rf_expected)}**\n"
        f"- Red flags falsely raised: **{false_rf}/{len(ok) - len(rf_expected)}**\n"
        f"- Average turns per interview: **{avg_turns:.1f}**\n"
        f"- Average latency per turn: **{avg_lat:.2f} s**\n"
        f"- Scripts with errors: {len(results) - len(ok)}\n"
    )
    print("\n" + summary)

    if not online:
        print("OFFLINE run — docs/eval_results.md NOT written.")
        return 0

    rows = "\n".join(
        f"| {r['id']} | {r['lang']} | {'yes' if r['expected_red_flag'] else 'no'} | "
        + (f"{'yes' if r['red_flag_raised'] else 'no'} | {r['floor_filled']}/{r['floor_total']} | {r['turns']} | {r['avg_latency']:.2f}s |"
           if "error" not in r else f"error: {r['error']} | | | |")
        for r in results)
    md = f"""# Intake engine evaluation (synthetic patients)

Run: {dt.datetime.now().strftime('%Y-%m-%d %H:%M')} · client: {mode}
Models: fast={model_config.FAST_MODELS[0]}, reasoning={model_config.REASONING_MODELS[0]}
10 made-up patients (Hindi / Bengali / English), text answers, no audio, via `DialogueManager`.

{summary}
| Patient | Lang | Red flag expected | Raised | Safety floor filled | Turns | Avg latency/turn |
|---|---|---|---|---|---|---|
{rows}
"""
    out = os.path.join(os.path.dirname(HERE), "docs", "eval_results.md")
    with open(out, "w", encoding="utf-8") as f:
        f.write(md)
    print(f"Wrote {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
