"""
Shipping document comparison — SI vs BL checker, using Google Gemini.

WHAT THIS DOES
  1. Loads classifications.json (produced by classify.py) — one category
     per email_id: BL_COMPARISON, SI_REQUEST, INVOICE_QUERY, GENERAL, SPAM
  2. Reads every email from your data folder (or a running docker server)
  3. For emails classified BL_COMPARISON that have BOTH an SI and a BL
     attachment, extracts text from the attachment REGARDLESS of format
     (.txt, .pdf, .xlsx, .docx) and asks Gemini to compare them. Every
     other email is reported straight from the classifier — no Gemini
     call, no quota spent.
  4. Writes one submission.json in the required shape
  5. If pointed at a running docker server, self-scores automatically

The prompt was rewritten to stop over-escalating. The self-eval score showed escalation_precision of
0.145 (138 predicted NEEDS_REVIEW vs only 20 true cases) with
defect_recall of only 0.717 -- the model was hedging into NEEDS_REVIEW
whenever a field was slightly unusual to parse, instead of confidently
extracting it. The prompt now explicitly tells it: if you CAN read a
value, extract it and compare it, even if the wording/format is odd.
NEEDS_REVIEW is reserved for genuinely absent/corrupted/wrong-type
documents, not for "this took a bit of effort to parse."
"""

import os
import io
import json
import time
import concurrent.futures
from pathlib import Path
import google.generativeai as genai
from loader import Inbox

# ---------------------------------------------------------------------------
# CONFIG — change these to match your setup
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent

# Option A (no Docker needed): path to the unzipped static bundle folder
# DATA_SOURCE = str(SCRIPT_DIR / "sdoc-hackathon-bundle")

# Option B (once Docker is working): uncomment this instead
DATA_SOURCE = "http://localhost:8080"

CLASSIFICATIONS_FILE = SCRIPT_DIR / "classifications.json"
OUTPUT_FILE = "submission.json"
MODEL_NAME = "gemini-3.6-flash"

MAX_EMAILS = None
MAX_WORKERS = 10
SECONDS_BETWEEN_CALLS = 0
# ---------------------------------------------------------------------------

GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY") or "PASTE_YOUR_KEY_HERE"
genai.configure(api_key=GOOGLE_API_KEY)
model = genai.GenerativeModel(MODEL_NAME)

COMPARE_PROMPT = """You are comparing a Shipping Instruction (SI) and a draft Bill of Lading (BL).

Your default posture is to COMMIT to a confident answer (OK or MISMATCH).
NEEDS_REVIEW is the exception, not a safe fallback -- only use it when one
of the specific triggers below is genuinely true, not merely because a
field was awkward to parse, oddly worded, or took effort to locate.

STEP 1 — Document type check (rare trigger).
Only flag review_reason "wrong_doc_type" if a document is UNAMBIGUOUSLY a
different kind of paperwork entirely -- a Commercial Invoice, Packing
List, Certificate of Origin, or similar, with no SI/BL structure at all.
An SI or BL that is oddly formatted, in a table/spreadsheet layout, has
OCR artifacts, or uses unfamiliar labels is STILL an SI or BL -- extract
from it normally. Do not use this trigger just because a document is
messy or hard to read; that is not the same as being the wrong document.

STEP 2 — Extract these 7 fields from EACH document, matching fields BY
MEANING even when the labels differ, wording is unusual, or the layout
is a table/spreadsheet rather than prose:
  shipper, consignee, notify_party, port_of_loading, port_of_discharge,
  container_count, gross_weight_kg

Known label variants — treat these as the SAME field:
  - port_of_loading: "Port of Loading", "POL", "Load Port"
  - port_of_discharge: "Discharge Port", "POD", "Port of Discharge"
  - notify_party: "Notify", "Notify Party", and also
    "Notify Party/Intermediate Consignee" — this is the notify field, NOT
    a second consignee, even though the word "Consignee" appears in it.
  - gross_weight_kg: "Gross Wt (kgs)", "Gross Weight (KG)", or "Gross
    Weight" followed by Chinese characters (毛重) before the unit — all
    the same field. Do NOT confuse this with "NET WEIGHT", a different,
    unrelated field you should ignore completely.

Ignore fields not in the list above (Net Weight, Booking Ref, HS Code,
Vessel Name, Bill of Lading No., etc.).

STEP 3 — Decide the status. Be precise about what counts as each:

  MISMATCH — use this whenever a field is present and readable in BOTH
    documents but the actual value genuinely differs, INCLUDING SMALL
    differences: a container count off by 1, a weight difference of a
    few hundred to a couple thousand kg, a different (but real) port
    name, a name spelled differently. Small does not mean unimportant --
    these are exactly the kind of real discrepancies this system exists
    to catch. Do not round these down to "close enough" or escalate them
    instead of reporting them as a mismatch.
    Ignore ONLY pure formatting/notation differences that carry no value
    change: commas in numbers (22,000 vs 22000), spacing, capitalization,
    equivalent notation ("1 x 40'HC" vs "1x40'HC").

  NEEDS_REVIEW ("missing_value") — use ONLY when a field is truly blank,
    a placeholder ("N/A", "___", "???"), or completely absent from a
    document -- not when it's merely phrased unusually, split across
    multiple lines/cells, or takes some effort to locate in a table. If
    you can identify a real value anywhere in the text, extract it and
    move on to comparing it -- do not escalate just because it wasn't
    obvious at first glance.

  OK — all 7 fields are readable in both documents and every one matches.

Default to OK or MISMATCH whenever you can read the fields. Only fall
back to NEEDS_REVIEW when a trigger above is genuinely met, not as a
hedge against uncertainty.

--- SI TEXT ---
{si_text}

--- BL TEXT ---
{bl_text}

Respond with ONLY this JSON, no other text, no markdown fences:
{{
  "status": "OK" or "MISMATCH" or "NEEDS_REVIEW",
  "has_defect": true or false,
  "defect_fields": ["field_name", ...],
  "review_reason": null or "missing_value" or "unreadable" or "wrong_doc_type",
  "notes": "one short sentence explaining the outcome"
}}
"""


def load_classifications() -> dict:
    if not CLASSIFICATIONS_FILE.exists():
        raise FileNotFoundError(
            f"Could not find {CLASSIFICATIONS_FILE}. Run classify.py first "
            f"(with MAX_EMAILS = None for a real submission) so this file exists."
        )
    data = json.loads(CLASSIFICATIONS_FILE.read_text(encoding="utf-8"))
    print(f"Loaded {len(data)} classifications from {CLASSIFICATIONS_FILE.name}")
    return data


def extract_text(inbox: Inbox, att_path: str) -> str:
    ext = att_path.rsplit(".", 1)[-1].lower()
    raw = inbox.read_bytes(att_path)

    if ext == "txt":
        return raw.decode("utf-8", errors="replace")

    if ext == "pdf":
        import pypdf
        reader = pypdf.PdfReader(io.BytesIO(raw))
        return "\n".join(page.extract_text() or "" for page in reader.pages)

    if ext == "xlsx":
        import openpyxl
        wb = openpyxl.load_workbook(io.BytesIO(raw), data_only=True)
        lines = []
        for ws in wb.worksheets:
            for row in ws.iter_rows(values_only=True):
                vals = [str(c) for c in row if c is not None]
                if vals:
                    lines.append(" | ".join(vals))
        return "\n".join(lines)

    if ext == "docx":
        import docx
        d = docx.Document(io.BytesIO(raw))
        lines = [p.text for p in d.paragraphs if p.text.strip()]
        for t in d.tables:
            for row in t.rows:
                lines.append(" | ".join(c.text for c in row.cells))
        return "\n".join(lines)

    raise ValueError(f"Unsupported attachment extension: .{ext}")


def compare_documents(si_text: str, bl_text: str) -> dict:
    prompt = COMPARE_PROMPT.format(si_text=si_text, bl_text=bl_text)
    for attempt in range(2):
        try:
            response = model.generate_content(prompt)
            raw = response.text.strip()
            raw = raw.replace("```json", "").replace("```", "").strip()
            return json.loads(raw)
        except Exception as e:
            if attempt == 0:
                print(f"  retrying after error: {e}")
                time.sleep(10)
            else:
                raise


def process_one_comparison(inbox: Inbox, eid: str, si_path: str, bl_path: str) -> dict:
    try:
        si_text = extract_text(inbox, si_path)
        bl_text = extract_text(inbox, bl_path)
    except Exception as e:
        print(f"{eid}: EXTRACTION FAILURE on ({si_path}, {bl_path}): "
              f"{type(e).__name__}: {e} -> NEEDS_REVIEW/unreadable")
        return {"category": "BL_COMPARISON", "status": "NEEDS_REVIEW",
                "review_reason": "unreadable", "has_defect": False, "defect_fields": []}

    try:
        result = compare_documents(si_text, bl_text)
    except Exception as e:
        print(f"{eid}: PIPELINE FAILURE (not necessarily a bad document): "
              f"{type(e).__name__}: {e} -> reported as NEEDS_REVIEW/unreadable")
        return {"category": "BL_COMPARISON", "status": "NEEDS_REVIEW",
                "review_reason": "unreadable", "has_defect": False, "defect_fields": []}

    print(f"{eid}: {result.get('status')}  ({result.get('notes', '')})")
    return {
        "category": "BL_COMPARISON",
        "status": result.get("status", "NEEDS_REVIEW"),
        "review_reason": result.get("review_reason"),
        "has_defect": result.get("has_defect", False),
        "defect_fields": result.get("defect_fields", []),
    }


def main():
    classifications = load_classifications()
    inbox = Inbox(DATA_SOURCE)
    submission = {}
    unclassified_count = 0
    tally = {"non_comparison": 0, "missing_attachment": 0, "sent_to_gemini": 0}
    pending = []

    for i, email in enumerate(inbox):
        if MAX_EMAILS is not None and i >= MAX_EMAILS:
            break
        eid = email["email_id"]

        category = classifications.get(eid)
        if category is None:
            unclassified_count += 1
            category = "GENERAL"
            print(f"{eid}: NOT FOUND in classifications.json -> defaulting to GENERAL")

        if category != "BL_COMPARISON":
            submission[eid] = {"category": category, "status": "OK",
                                "review_reason": None, "has_defect": False, "defect_fields": []}
            tally["non_comparison"] += 1
            continue

        atts = email.get("attachments", [])
        si_path = next((a for a in atts if "_SI." in a), None)
        bl_path = next((a for a in atts if "_BL." in a), None)

        if not si_path or not bl_path:
            submission[eid] = {"category": "BL_COMPARISON", "status": "NEEDS_REVIEW",
                                "review_reason": "missing_attachment", "has_defect": False, "defect_fields": []}
            tally["missing_attachment"] += 1
            continue

        pending.append((eid, si_path, bl_path))

    print(f"{len(pending)} emails need a real comparison call "
          f"(running with {MAX_WORKERS} parallel workers)...")

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {
            executor.submit(process_one_comparison, inbox, eid, si_path, bl_path): eid
            for eid, si_path, bl_path in pending
        }
        for future in concurrent.futures.as_completed(futures):
            eid = futures[future]
            submission[eid] = future.result()
            tally["sent_to_gemini"] += 1

    with open(OUTPUT_FILE, "w") as f:
        json.dump(submission, f, indent=2)

    print(f"\nDone. {len(submission)} emails processed.")
    print(f"  {tally['non_comparison']} not a comparison request (classifier-only)")
    print(f"  {tally['missing_attachment']} BL_COMPARISON but no attachment at all -> NEEDS_REVIEW")
    print(f"  {tally['sent_to_gemini']} sent to Gemini for a real comparison")
    print(f"  {unclassified_count} had no classification (defaulted to GENERAL)")
    print(f"Wrote {OUTPUT_FILE}")

    if str(DATA_SOURCE).startswith("http"):
        try:
            score = inbox.submit(submission)
            print("\nSelf-eval score:")
            print(json.dumps(score, indent=2))
            score_file = SCRIPT_DIR / "self_eval_score.json"
            score_file.write_text(json.dumps(score, indent=2))
            print(f"Wrote {score_file}")
            # Every time compare.py runs it will update the json file with the latest eval score
      
        except Exception as e:
            print("Could not self-score:", e)


if __name__ == "__main__":
    main()
