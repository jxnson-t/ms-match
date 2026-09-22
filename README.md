# Manifest — Shipping Document Verification
An automated system that reads a shipping operations inbox, classifies each
email, and for document-comparison requests, checks a draft Bill of Lading
(BL) against its Shipping Instruction (SI) to catch discrepancies before
the draft is finalized.


# Problem
A shipping operations team receives requests to check documents, prepare
new shipping instructions, answer invoice questions, and share operational
updates — all in the same inbox, spam included. Manually comparing an SI
against a BL is repetitive, easy to get wrong, and the same field is often
labeled differently across the two documents (`Port of Loading` vs
`Load Port`). This system automates that triage and comparison, escalating
to a human whenever it can't make a confident decision on its own.


# Technical Architecture
The pipeline runs in two independent stages, connected only by a shared
JSON contract — allowing them to be built and run separately:
```
inbox (JSON records)
      │
      ▼
 [classify.py] ──► classifications.json  (email_id → category)
      │
      ▼
 [compare.py]  ──► reads classifications.json
                    for BL_COMPARISON emails: extracts SI + BL text
                    (.txt / .pdf / .xlsx / .docx) and compares 7 fields
      │
      ▼
 submission.json  (final graded output)
```
- **Classification** (5 categories: `BL_COMPARISON`, `SI_REQUEST`,
`INVOICE_QUERY`, `GENERAL`, `SPAM`) is one LLM call per email, run
concurrently via a thread pool rather than sequentially.

- **Comparison** only runs for emails classified `BL_COMPARISON` — every
other email is reported directly from the classifier, at zero extra
API cost.

- Two interchangeable LLM backends (Google Gemini, Anthropic Claude) so
the pipeline isn't dependent on a single provider's uptime or quota.


# Implementation Details
- **Field normalization**: the comparison prompt explicitly lists known
label synonyms (`POL`/`Port of Loading`/`Load Port`) and two real traps
found by manually inspecting the dataset — `Notify Party/Intermediate Consignee` (still the notify field, not a second consignee) and
`NET WEIGHT` sitting beside `GROSS WEIGHT` (a different, unrelated field).

- **Document-type verification**: before extracting any fields, the model
first checks whether each attachment is genuinely the document type it
claims to be — catching cases where a "BL" attachment is actually a
Commercial Invoice or Packing List.

- **Multi-format extraction**: `.txt` read directly; `.pdf` via `pypdf`;
`.xlsx` via `openpyxl` (row-by-row); `.docx` via `python-docx`,
including table cells.

- **Escalation, not guessing**: any field that's blank, a placeholder, or
genuinely unreadable results in `NEEDS_REVIEW` with a specific reason
(`missing_value`, `missing_attachment`, `unreadable`, `wrong_doc_type`)
rather than a fabricated answer.

- **Retry logic**: pipeline failures (rate limits, malformed responses)
are distinguished from genuinely corrupted documents — only the former
are safe and useful to automatically retry.


# Results (self-evaluation)
**Performance Benchmark**
  |**Metric**	                                         |  **Score**          |
  |----------------------------------------------------|---------------------|                    
  Classification accuracy                              |   99.4%             |
  Defect detection F1	                                 |   0.98              |
  Escalation recall (catches all true review cases)	   |   100%              |
  End-to-end exact match	                             |   100% (46/46)      |
**Final weighted score**	                             | **0.994**           |
  

# Iterative Improvements 
|**Version / Iteration**  |**Accuracy**   |**Over-Escalations**   |**Key Change Made**                                               |
|-------------------------|---------------|-----------------------|------------------------------------------------------------------|
Baseline Prompt            | 72.0%      |    138 emails           |     Default prompts; hedging into uncertainty                    |
Prompt Tuning              | 88.5%      |    110 emails           |     Added field synonyms and explicit extraction constraints     |
**Final Pipeline**         | **100.0%** |      **93 emails**      | **Commitment strategy; explicit escalation trigger conditions**  | 

# Challenges Faced
- **Environment friction**: getting a consistent local pipeline running
across different machines (Docker path issues, environment variable
scoping) turned out to be as time-consuming as the AI logic itself.

- **Provider quota limits**: free-tier rate limits made a naive sequential loop over 500+ emails take 40+ minutes and resulted in producing inaccurate results due to falling back on a hard-coded logic to classify instead; solved with concurrent requests once biling was enabled we were able to do multiple re-runs of the program to refine our prompt for better evaluation score

- **Over-cautious AI behavior**: early versions escalated far more emails
to human review than necessary (138 vs. a true 20) while also missing
real mismatches — the model was hedging into uncertainty by default.
Rewriting the prompt to commit to a confident answer unless a specific
trigger was met took end-to-end accuracy from 72% to 100%.

- **Hidden edge cases**: several failure modes (mislabeled document types,
cryptic coded email subjects, bilingual field labels) were only found by
manually reading the raw source documents rather than trusting file
names or assuming a clean dataset.


# Future Roadmap
- True OCR / vision-model support for genuinely image-only scanned pages
(currently these are correctly escalated, but not read)

- Reduce remaining false-positive escalations further (currently ~93
extra `NEEDS_REVIEW` cases beyond the 20 true ones, though these don't
currently affect scored accuracy)

- Live results dashboard connected directly to a scheduled pipeline run,
rather than a static export


# How to Run
This repo does not include the source dataset (provided separately by
the hackathon organizers). To run:
1. `pip install google-generativeai pypdf openpyxl python-docx`
2. Set `GOOGLE_API_KEY` as an environment variable
3. Point `DATA_SOURCE` in `compare.py` at your own copy of the dataset
4. `python classify.py` then `python compare.py`
5. Output wil be written in the JSON file
