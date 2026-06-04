# VS-Archive AI Context

VS-Archive is a Django backend project for managing historical family documents.

## Main domain

- **`ArchiveItem`** is the long-term central archival content entity. The product archive is **`/archive/`** (unified list/detail direction), not separate mini-sites per item type.
- **`OCR_DOCUMENT`:** **`Document`** remains runtime source of truth during the bridge for upload/list/detail/review. Shared **`ArchiveItem`** fields are copied at create/backfill only — **no** ongoing sync. Do not assume **`ArchiveItem`** copies stay current after **`Document`** edits. Before cutover, refresh/sync from **`Document`** or run an explicit migration strategy.
- **`MANUAL_TEXT`:** **`ArchiveItem`** + **`ManualTextContent`** are runtime source of truth. Staff/admin create/edit via **`/archive/manage/new/manual-text/`** and **`/archive/manage/<id>/edit/`**. No **`Document`**, no OCR/HTR, no SQS. Body is **not** in **`DocumentTextResult`**.
- **`ArchiveItem.visibility`** is the access-control source of truth for all **`ArchiveItem`** types (`public` / `private`). **`private`** means private family archive content (visible to authenticated **`archive_family`** group members and staff/admin), not staff-only content. Helpers in **`archive_item_access.py`**. **`Document.visibility`** remains a temporary bridge field; OCR document list/detail access uses **`document.archive_item.visibility`**. Family invitation/account-management is deferred.
- **`PHOTO`** archive items are **not** implemented yet (`PHOTO` remains enum-only).
- Documents may be uploaded as IMAGE or PDF.
- Documents have metadata (`language`, `text_input_type`, etc.).
- OCR/HTR extracts text into `DocumentTextResult` rows.
- Translation to Hebrew is planned; not fully implemented for non-Hebrew documents.
- Text results are stored separately from the document.
- Admin review/verification matters (`verification_status` on results).

## Current implementation (OCR/HTR)

**Gemini** is the **production/default** engine for all routed pairs except Hebrew handwritten. Static `OCR_ROUTES` in `documents/services/ocr_routing.py` remain Gemini-only for the pairs that still use Gemini.

**Transkribus** is **implemented** (Legacy TrpServer / PyLaia), but **not** broad production-default:

- Real adapter + engine: `TranskribusAdapter`, `transkribus_engine.py`.
- Worker routing: `language=he` + `HANDWRITTEN` requires Transkribus. `ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN=true` activates that route; if the flag is off, routing fails fast instead of using Gemini.
- Existing `TRANSKRIBUS_DEV_UPLOAD_MODE`, `TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT`, `TRANSKRIBUS_FORCE_REPROCESS`, and `TRANSKRIBUS_RECOGNITION_ONLY_RETRY` remain execution/recovery controls, not route-selection flags.
- Manual smoke: `python manage.py dev_transkribus_transcribe <file> --confirm-create-transkribus-doc`.

**Not implemented:** Gemini→Transkribus fallback, Transkribus→Gemini fallback, hybrid OCR routing, broader Transkribus routing beyond Hebrew handwritten, product/admin reprocess workflow, cleanup automation, general re-OCR on successful Trp runs, or remote Trp deletion.

**Recognition-only retry V1 (dev/staging):** when `TRANSKRIBUS_RECOGNITION_ONLY_RETRY=true`, dev upload mode may re-run PyLaia on an existing Trp `remote_doc_id` without a new upload — **recovery only** (failed/incomplete upload-created attempts). Excludes `SUCCEEDED` source runs; blocks if any `DocumentTextResult` is `VERIFIED`. `TRANSKRIBUS_FORCE_REPROCESS=true` still means a new upload/new Trp document. See `decision-log.md`.

**TranskribusRun persistence:** Transkribus adapter paths persist one row per attempt (remote ids, job ids, attempt status). Worker passes generic `document_id` only.

**Cleanup / retention V1:** `python manage.py report_transkribus_cleanup` is a dry-run local reporting command. It classifies `TranskribusRun` rows and grouped `remote_doc_id` lineages for operator review, but it does not call Transkribus, delete remote docs, or delete local rows.

**Duplicate upload guard (PR3):** dev upload mode blocks a second Trp upload for the same `(document_id, UPLOAD_CREATED, collection_id, model_id)` when a prior blocking run exists. Override with `TRANSKRIBUS_FORCE_REPROCESS=true` (new Trp doc) or recovery with `TRANSKRIBUS_RECOGNITION_ONLY_RETRY=true` when a reusable source run exists. See `decision-log.md`.

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

1. ~~Transkribus remote identity schema + persistence wiring~~ → **done (PR1 + PR2)**.
2. ~~Duplicate upload guard (dev upload mode)~~ → **done (PR3)**.
3. ~~Recognition-only retry V1~~ → **done** (dev/staging recovery).
4. ~~Cleanup / retention V1 dry-run reporting~~ → **done**.
5. Remote deletion / cleanup automation later, only with explicit approval.
6. Broader Transkribus production routing only with explicit approval.

OCR quality/fidelity validation against the Transkribus UI is important but **not** the current docs/rules task.
