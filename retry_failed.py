"""
Re-attempts ONLY the emails that failed for pipeline reasons (rate limits,
malformed JSON, timeouts) on a previous compare.py run -- without
re-running the other ~500 emails or spending API calls on them again.

It only touches entries where status == "NEEDS_REVIEW" and
review_reason == "unreadable". A genuinely corrupted document (like the
5 real unreadable edge cases) will fail extraction the SAME way every
time -- pypdf raises the same exception or returns 0 chars again -- so
retrying it just re-confirms NEEDS_REVIEW, harmlessly. Only entries
that failed for a TRANSIENT reason (a rate limit blip, a one-off
malformed response) will actually change on retry.
"""

import json
from pathlib import Path

# Reuse everything from compare.py instead of duplicating it -- if you
# improve the prompt or extraction logic there, this script picks it up
# automatically instead of drifting out of sync.
import compare

SCRIPT_DIR = Path(__file__).resolve().parent
SUBMISSION_FILE = SCRIPT_DIR / "submission.json"


def main():
    submission = json.loads(SUBMISSION_FILE.read_text())
    inbox = compare.Inbox(compare.DATA_SOURCE)
    all_emails = {e["email_id"]: e for e in inbox}

    # Find every entry currently reported as a pipeline/extraction failure
    retry_candidates = [
        eid for eid, entry in submission.items()
        if entry.get("category") == "BL_COMPARISON"
        and entry.get("status") == "NEEDS_REVIEW"
        and entry.get("review_reason") == "unreadable"
    ]

    print(f"Found {len(retry_candidates)} entries marked NEEDS_REVIEW/unreadable to retry.")
    if not retry_candidates:
        print("Nothing to retry -- submission.json already has zero unreadable entries.")
        return

    recovered = 0
    confirmed_still_failing = 0

    for eid in retry_candidates:
        email = all_emails.get(eid)
        if email is None:
            continue
        atts = email.get("attachments", [])
        si_path = next((a for a in atts if "_SI." in a), None)
        bl_path = next((a for a in atts if "_BL." in a), None)
        if not si_path or not bl_path:
            continue  # a real missing_attachment case, not a retry candidate

        before_status = submission[eid]["status"]
        new_entry = compare.process_one_comparison(inbox, eid, si_path, bl_path)
        submission[eid] = new_entry

        if new_entry["status"] != "NEEDS_REVIEW" or new_entry["review_reason"] != "unreadable":
            recovered += 1
            print(f"  {eid}: RECOVERED -> {new_entry['status']} "
                  f"(was stuck on a transient failure, not a real document problem)")
        else:
            confirmed_still_failing += 1
            print(f"  {eid}: still unreadable after retry -- likely a genuinely "
                  f"corrupted/unsupported document, not a transient issue")

    SUBMISSION_FILE.write_text(json.dumps(submission, indent=2))

    print(f"\nDone. {recovered} recovered, {confirmed_still_failing} confirmed as "
          f"genuinely unreadable (not just retried on a fluke).")
    print(f"Updated {SUBMISSION_FILE}")


if __name__ == "__main__":
    main()
