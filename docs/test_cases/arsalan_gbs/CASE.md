# Test case: "Arsalan Ahmed": Guillain-Barré syndrome (synthetic)

> **Fully fictional patient.** Name, UHID, hospital, doctor and all values are made up for testing.
> Rare condition: Guillain-Barré syndrome (GBS), a weakness that rises up both legs 1–3 weeks after a
> gut infection (classically *Campylobacter*). It can stop breathing, so it **must be triaged urgently**.

Files in this folder:
- `CASE.md`: this script and what to check
- `discharge_summary_arsalan.pdf`: fake 2-page discharge summary from 2 weeks ago (upload at "past records")
- `discharge_summary_arsalan_page1.jpg`: page 1 as a phone-style photo (test the camera/image path)

---

## 1. Registration (kiosk → guest)

| Field | Enter |
|---|---|
| Name | **Arsalan Ahmed** (must match the document so the name check passes) |
| Age | 34 |
| Sex | Male |
| Weight / Height | 72 kg / 175 cm |
| Vitals | BP 128/82, HR 96, SpO2 95% |
| Department | General Medicine |
| Language | English (or Hindi; the Hindi answers are below) |

## 2. What to say (one answer per question; adapt to what is asked)

| Topic | English | Hindi |
|---|---|---|
| **Chief complaint** | "My legs are getting weak for 4 days. It started in my feet and is now going up to my thighs. My fingers also tingle." | "चार दिन से मेरे पैरों में कमज़ोरी है। पहले तलवों में शुरू हुई, अब जांघों तक आ गई है। उंगलियों में झनझनाहट भी है।" |
| Onset / speed | "It came slowly over 4 days and is getting worse every day." | "चार दिन में धीरे-धीरे आई, हर दिन बढ़ रही है।" |
| Which side | "Both legs equally, not one side." | "दोनों पैरों में बराबर, एक तरफ नहीं।" |
| Function | "I can't climb stairs and need help to get up from a chair." | "सीढ़ियाँ नहीं चढ़ पाता, कुर्सी से उठने में मदद चाहिए।" |
| Recent illness | "Two weeks ago I was admitted for bad diarrhoea and fever." | "दो हफ्ते पहले दस्त और बुखार के लिए भर्ती हुआ था।" |
| Breathing | "I feel a little breathless when I lie flat." | "लेटने पर थोड़ी सांस फूलती है।" |
| Headache | "No headache." | "सिरदर्द नहीं है।" |
| Speech | "My speech is normal." | "बोलने में कोई दिक्कत नहीं।" |
| Vision | "No change in vision." | "आँखों में कोई बदलाव नहीं।" |
| Face | "Maybe slight difficulty closing my eyes tightly." | "आँखें कसकर बंद करने में थोड़ी दिक्कत।" |
| Seizures / fainting | "No fits, no fainting." | "कोई दौरा नहीं, बेहोशी नहीं।" |
| Neck stiffness | "No." | "नहीं।" |
| Back pain | "Mild lower back pain." | "कमर में हल्का दर्द।" |
| Bladder / bowel | "Normal." | "सामान्य।" |
| Medicines / allergy | "I finished the antibiotics. I'm allergic to penicillin." | "एंटीबायोटिक खत्म कर ली। पेनिसिलिन से एलर्जी है।" |

## 3. What the kiosk MUST ask (safety floor)

If the complaint is classified **neuro**, these 8 are mandatory (always added to the AI's plan). Tick each one you were asked:

- [ ] Sudden (thunderclap) vs gradual onset
- [ ] Worst headache of life
- [ ] Weakness / numbness / tingling on one side
- [ ] Slurred speech / difficulty finding words
- [ ] Sudden vision loss / double vision
- [ ] Seizures
- [ ] Neck stiffness
- [ ] Loss of consciousness / altered mental state

**A good interview should also ask** (AI-generated plan, so wording varies):
- [ ] Where the weakness started and whether it is spreading upwards
- [ ] Both sides or one side
- [ ] Recent infection, diarrhoea or vaccination
- [ ] Breathing difficulty / breathlessness lying flat
- [ ] Tingling or numbness in hands and feet
- [ ] Difficulty walking / climbing stairs
- [ ] Bladder or bowel problems
- [ ] Back pain
- [ ] Current medicines and allergies

**Must NOT happen:**
- [ ] the same question asked more than twice
- [ ] questions about something already answered
- [ ] a diagnosis or treatment advice given to the patient

## 4. Expected safety behaviour

| Answer | Expected |
|---|---|
| "Both legs weak…" (weakness reported as present) | Urgent neuro red flag → **triage alert**, patient moved to top of the doctor queue |
| "A little breathless when I lie flat" | Respiratory red flag (`SEVERE_RESPIRATORY_DISTRESS`, "cannot lie flat") → triage alert |
| "No headache", "No change in vision", "No fits" | **No** alert from these (negations must not trigger rules) |

## 5. Document upload: what should be extracted from the PDF

Upload `discharge_summary_arsalan.pdf` (or the JPG) at "Got any past records?". Expected:

| Section | Values |
|---|---|
| Patient name on document | Arsalan Ahmed → **name match = yes** |
| Diagnoses | Acute infective gastroenteritis (Campylobacter jejuni); moderate dehydration; hypokalaemia |
| Medicines | Azithromycin 500 mg once daily × 3 days · ORS 1 sachet after each loose stool · Ondansetron 4 mg SOS · Racecadotril 100 mg three times daily × 3 days · Potassium chloride syrup 15 ml twice daily × 5 days |
| Abnormal labs | WBC 13,800 /µL (high) · Sodium 131 mmol/L (low) · Potassium 3.1 mmol/L (low) · CRP 48 mg/L (high) |
| Normal labs | Hb 13.9 g/dL · Creatinine 1.0 mg/dL |
| Microbiology | Stool culture: *Campylobacter jejuni* isolated |
| Allergy | Penicillin (rash) |
| Follow-up | Review in 10 days; return immediately if weakness, tingling or breathing difficulty |

**Name-mismatch test:** register as "Rahul Verma" and upload the same PDF → the document must be **rejected** (it belongs to someone else).

## 6. On the doctor side, check

- [ ] Patient appears in the queue as **urgent / red flag**
- [ ] Summary mentions ascending bilateral weakness, recent gastroenteritis, breathlessness lying flat
- [ ] Document data (medicines, labs, penicillin allergy) appears in the casesheet PDF
- [ ] If the AI summary failed, the badge "AI unavailable — verify manually" shows (no invented data)
