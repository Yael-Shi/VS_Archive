# VS-Archive AI Context

VS-Archive is a Django backend project for managing historical family documents.

## Main domain

- Documents may be uploaded as IMAGE or PDF.
- Documents have metadata (`language`, `text_input_type`, etc.).
- OCR/HTR extracts text into `DocumentTextResult` rows.
- Translation to Hebrew is planned; not fully implemented for non-Hebrew documents.
- Text results are stored separately from the document.
- Admin review/verification matters (`verification_status` on results).

## Current implementation (OCR/HTR)

**Gemini** is the **production/default** engine: static `OCR_ROUTES` in `documents/services/ocr_routing.py` are all `GEMINI`.

**Transkribus** is **implemented** (Legacy TrpServer / PyLaia), but **not** broad production-default:

- Real adapter + engine: `TranskribusAdapter`, `transkribus_engine.py`.
- Dev/staging only unless production routing is explicitly approved later.
- Worker routing to Transkribus: `language=he` + `HANDWRITTEN` when `TRANSKRIBUS_DEV_OCR_ROUTE` + `TRANSKRIBUS_DEV_UPLOAD_MODE` are set (see decision log).
- Manual smoke: `python manage.py dev_transkribus_transcribe <file> --confirm-create-transkribus-doc`.

**Not implemented:** Gemini→Transkribus fallback, hybrid OCR routing, production-default Transkribus in `OCR_ROUTES`, **TranskribusRun persistence wiring** (schema landed PR1; worker/adapter writes deferred PR2), cleanup automation.

## Routing layer (done)

Engine selection is implemented as a small routing layer (`select_ocr_route` → `transcribe_pages` → adapters), not as logic inside views or a provider-specific worker.

## OCR review lifecycle (current)

**Automatic OCR/HTR success** (`run_worker._save_htr_results`):

- **`DocumentTextResult.status=NEEDS_REVIEW`** (Gemini, Transkribus, and any worker success path)
- **`verification_status=UNVERIFIED`**
- **`NEEDS_REVIEW`** = usable/displayable text that still needs human review; **not** a technical failure
- **`FAILED`** = pipeline/dispatch/routing failures only

**Human ground truth:** **`verification_status=VERIFIED`** is the human-approved layer. Row **`status`** does not replace that.

**Parent document rollup:**

- **`READY`** = all expected outputs exist and are usable/displayable (non-empty text; `SUCCEEDED` or `NEEDS_REVIEW`). **Not** human-verified.
- A document may be **`READY`** while results are **`NEEDS_REVIEW`** + **`UNVERIFIED`**.
- **`PARTIAL`** = missing, incomplete, failed, or unusable expected outputs — **not** merely review pending.
- **`NEEDS_REVIEW` alone does not force `PARTIAL`** when expected usable rows exist.

**Review reasons:** policy token **`AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW`** on every automatic success; **`NEEDS_REVIEW_FLAG`** only when **`HtrResult.needs_review=True`**; plus **`MIN_TEXT_LENGTH`**, **`HAS_UNCLEAR`**, and engine-provided reasons.

**`SUCCEEDED`** remains valid in the schema for future trusted paths; current automatic worker persistence normally uses **`NEEDS_REVIEW`**.

## Hebrew result types (current behavior)

For Hebrew documents, the worker persists **both** `SOURCE_TEXT` and `HEBREW_TEXT`; processing-state rollup for **`READY`** requires **`HEBREW_TEXT` only** (usable per rollup rules above). See `decision-log.md` and `.cursor/rules/architecture.mdc`.

## Non-Hebrew `PARTIAL`

Non-Hebrew documents may remain **`PARTIAL`** because `HEBREW_TEXT` (translation) is not implemented — **intentional**, not an OCR failure.

## Where to read more

- `docs/ai-context/decision-log.md` — durable decisions, Transkribus PR history, operational boundaries.
- `.cursor/rules/architecture.mdc` — layer boundaries and contracts for code changes.

## Near-term roadmap

1. ~~Transkribus remote identity schema~~ → **`TranskribusRun` (PR1 done)**; **persistence wiring (PR2)** next.
2. Reprocess / duplicate-prevention policy.
3. Cleanup / retention runbook (no automation before identity wiring + policy).
4. Broader Transkribus production routing only with explicit approval.

OCR quality/fidelity validation against the Transkribus UI is important but **not** the current docs/rules task.
