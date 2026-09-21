import os
import json
import time
from pathlib import Path
from google import genai
 
# ---------------------------------------------------------------------------
# CONFIG
# ---------------------------------------------------------------------------
SCRIPT_DIR = Path(__file__).resolve().parent
INBOX_DIR = SCRIPT_DIR / "inbox"
OUTPUT_FILE = SCRIPT_DIR / "classifications.json"
MODEL_NAME = "gemini-3.6-flash"
 
MAX_EMAILS = None
SECONDS_BETWEEN_CALLS = 4.5
# ---------------------------------------------------------------------------
 
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY") or "PASTE_YOUR_KEY_HERE"
os.environ["GEMINI_API_KEY"] = GEMINI_API_KEY
client = genai.Client()
 
if not INBOX_DIR.exists():
    raise FileNotFoundError(
        f"Could not find an 'inbox' folder at {INBOX_DIR}. "
        f"Make sure this script sits in the same folder as inbox/ and attachments/."
    )
 
# Required category values — must match these exactly, not free text
VALID_CATEGORIES = {"BL_COMPARISON", "SI_REQUEST", "INVOICE_QUERY", "GENERAL", "SPAM"}
 
PROMPT_TEMPLATE = """Classify the email into EXACTLY ONE category. Respond with
ONLY the category name, nothing else:
 
BL_COMPARISON - asks to check/confirm a draft Bill of Lading against a
  Shipping Instruction. Signals include plain phrases like "TO CONFIRM DOCS",
  "REQUEST BL DRAFT", "Draft BL ... amend" -- AND also cryptic coded subject
  lines like "AFEMY - HOCHIMINH CITY_VIETNAM - HAPAG(HLCUSIN016481880) -
  5ALT-36381 - 525..." or "AIE - CALLAO_PERU - EVER(EGLV577449160936) -
  5RUS-14911 - ...". These coded PORT_COUNTRY - CARRIER(reference) - ...
  strings are STILL comparison requests, not general messages, even though
  they don't read like plain English.
SI_REQUEST - asks for a NEW Shipping Instruction. Signals: "SI NEEDED",
  "REQUEST SI", "CUST SI", or coded lines like "SI - <bl> -
  DIRECT(<carrier>) - <OC> - <POD> - <BLtype>".
INVOICE_QUERY - about billing/charges/invoices (e.g. "CANCEL INVOICE",
  "LOCAL CHARGES", "D&D charges", "MISSING GR").
GENERAL - internal updates/reports/reminders, not a specific request
  (e.g. "UPDATE SUMMARY", "Berthing Report", HR/holiday notices).
SPAM - unrelated prize/phishing/mailbox-full messages, unconnected to
  actual shipping operations.
 
If a subject line is a cryptic reference code rather than plain English,
do NOT default to GENERAL just because it's hard to read -- look for the
structural signals above (coded route/carrier/BL-reference formats belong
to BL_COMPARISON or SI_REQUEST, not GENERAL).
 
IGNORE THESE WHEN JUDGING SPAM -- they are routine artifacts of this
inbox's email system, not evidence of phishing or junk mail:
  - A banner at the very top of the body reading something like "WARNING:
    This email originated outside of our organisation. As a security
    measure, please exercise caution..." -- this is an automatic gateway
    stamp added to EVERY external email, not a signal about this specific
    email's content. Do not let its presence push you toward SPAM.
  - Quoted/forwarded reply chains where an internal staff member just
    wrote something terse like "Please follow the previous instruction.
    Thank you." -- this is normal internal shorthand for "proceed as
    already discussed," not a vague or suspicious phrase. Read past it to
    the substance of the thread (what document is being discussed, what
    action is being asked) to decide the category.
A real shipping-ops email (an actual SI/BL request, invoice query, etc.)
does not stop being that just because it carries a security banner or a
short forwarded reply. Judge SPAM only on the actual substance of the
message: unrelated prize/lottery offers, phishing links asking for
credentials, mailbox-full notices, or content with no connection
whatsoever to an actual shipment, invoice, or document request.
 
Subject: {subject}
Body: {body}
"""
 
 
def classify_fast(email_data: dict) -> str:
    """Keyword-based fallback if the API call fails."""
    text = f"{email_data.get('subject', '')} {email_data.get('body', '')}".lower()
    if any(k in text for k in ["compare", "comparison", "si vs bl", "confirm docs"]):
        return "BL_COMPARISON"
    if any(k in text for k in ["new si", "shipping instruction", "si needed"]):
        return "SI_REQUEST"
    if any(k in text for k in ["invoice", "payment", "billing", "charges"]):
        return "INVOICE_QUERY"
    if any(k in text for k in ["casino", "crypto", "free", "prize", "winner"]):
        return "SPAM"
    return "GENERAL"
 
 
def classify_email(email_data: dict) -> str:
    prompt = PROMPT_TEMPLATE.format(
        subject=email_data.get("subject", ""),
        body=email_data.get("body", ""),
    )
    try:
        response = client.models.generate_content(model=MODEL_NAME, contents=prompt)
        label = response.text.strip().upper()
        for cat in VALID_CATEGORIES:
            if cat in label:
                return cat
        # model responded but not with a recognizable category — fall back
        return classify_fast(email_data)
    except Exception as e:
        print(f"  API error ({e}), using keyword fallback")
        return classify_fast(email_data)
 
 
def main():
    json_files = sorted(INBOX_DIR.glob("*.json"))
    print(f"Found {len(json_files)} emails in {INBOX_DIR}")
 
    results = {}
    for idx, file_path in enumerate(json_files, 1):
        if MAX_EMAILS is not None and idx > MAX_EMAILS:
            break
 
        email_data = json.loads(file_path.read_text(encoding="utf-8"))
        email_id = email_data["email_id"]
 
        category = classify_email(email_data)
        results[email_id] = category
 
        print(f"[{idx}] {email_id} -> {category}")
        time.sleep(SECONDS_BETWEEN_CALLS)
 
    OUTPUT_FILE.write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(f"\nDone. {len(results)} emails classified -> {OUTPUT_FILE}")
 
 
if __name__ == "__main__":
    main()
