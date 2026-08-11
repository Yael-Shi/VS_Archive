# VS-Archive AI Context

VS-Archive is a Django backend project for managing historical family documents.

## Main domain

- **`ArchiveItem`** is the long-term central archival content entity. The product archive is **`/archive/`** (unified list/detail direction), not separate mini-sites per item type.
- **`OCR_DOCUMENT`:** **`Document`** remains runtime source of truth for OCR/processing fields. For the six shared archival fields, **`ArchiveItem`** is canonical on create (`create_ocr_document`), staff OCR metadata edit (`/archive/manage/<id>/edit/`), and user-facing display; **`Document`** holds compatibility mirrors. **`Document`**-centric list/review paths remain for OCR operations (`/api/ui/documents/`, `/api/ui/admin/review/`). Primary OCR create/upload UI is **`/archive/manage/new/?item_type=ocr_document`** using **`/api/uploads/*`**; **`/api/ui/upload/`** remains a fallback. Do not assume sync from Django Admin **`Document`** edits. See **`docs/ai-context/ocr-archiveitem-cutover.md`**.
- **`MANUAL_TEXT`:** **`ArchiveItem`** + **`ManualTextContent`** are runtime source of truth. Staff/admin create via **`/archive/manage/new/`** (manual-text branch; legacy alias **`/archive/manage/new/manual-text/`**) and edit via **`/archive/manage/<id>/edit/`**. No **`Document`**, no OCR/HTR, no SQS. Body is **not** in **`DocumentTextResult`**.
- **`ArchiveItem.visibility`** is the access-control source of truth for all **`ArchiveItem`** types (`public` / `private`). **`private`** means private family archive content (visible to authenticated **`archive_family`** group members and staff/admin), not staff-only content. Helpers in **`archive_item_access.py`**. **`Document.visibility`** remains a temporary bridge field; OCR document list/detail access uses **`document.archive_item.visibility`**. Family invitation/account-management is deferred.
- **`PHOTO`:** **`ArchiveItem`** + **`PhotoContent`** are runtime source of truth. Staff create via **`/archive/manage/new/?item_type=photo`** (PHOTO-specific upload API — **not** OCR **`/api/uploads/*`**). V1 includes direct S3 upload, metadata, **`public`** / **`private`** visibility, public archive list/detail display, and staff metadata edit/delete. **No** **`Document`**, OCR/HTR, or SQS. Thumbnail generation, S3 cleanup on delete, and re-upload/retry remain deferred. Design/scope: **`docs/ai-context/photo-archive-items.md`**.
- **`VIDEO`:** **`ArchiveItem`** + **`VideoContent`** are runtime source of truth for URL/provider presentation metadata only (no media bytes/S3). Staff create/edit/delete via **`/archive/manage/`**. Public browse/detail support YouTube click-to-load (`youtube-nocookie.com` after explicit activation; iframe `referrerPolicy=strict-origin-when-cross-origin` to avoid YouTube error 153; local facade replaced in-place by the player in one media box — 16:9 by default with `min-height: 200px`, so narrow mobile may be slightly taller than 16:9) and KAN/OTHER external links. Site-wide Referrer-Policy remains `same-origin`; CSP `frame-src` allows only `'self'` and `https://www.youtube-nocookie.com`. Public render revalidates stored URL/provider/mode/id via `parse_video_url()`. Metadata-only search; type filter “סרטונים”. No captions/transcription/OCR. See decision-log VIDEO PR1–PR3 and the error-153 / unified-facade follow-up.
- Documents may be uploaded as IMAGE or PDF.
- Documents have metadata (`language`, `text_input_type`, etc.).
- OCR/HTR extracts text into `DocumentTextResult` rows.
- After OCR/HTR, Hebrew documents use the transcript for **`HEBREW_TEXT`**; non-Hebrew documents persist **`SOURCE_TEXT`**, then automatic Gemini Hebrew translation to **`HEBREW_TEXT`** (see **`docs/ocr-routing-reference.md`**).
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

**Durable `PROCESS_DOCUMENT` rollout:** Request model, typed worker outcomes, request-aware worker fencing, and the generic enqueue service are implemented. Successful single-file `upload_complete` and multi-image `upload_finalize` now use `enqueue_uploaded_document_processing` with `origin=UPLOAD_FINALIZE`; repeated completion coalesces against active history and treats matching terminal upload history as idempotent. Document-state updates are fenced by Request status, and expected enqueue failures use safe typed responses. The ECS task role has explicit SQS send permission. OCR reprocess and Hebrew translation retry remain on the legacy document-id payload; stranded pre-send `QUEUED` recovery remains deferred.

**Corrected/current staff sync queue (PR1 schema + PR2 worker + PR3 enqueue service + staff fetch UI):** durable **`TranskribusCorrectedCurrentSyncRequest`** per document enqueue intent; top-level SQS type **`SYNC_TRANSKRIBUS_CORRECTED_CURRENT`** handled by the existing worker (claim/reclaim, 45m lease + visibility, 2m defer, atomic Request↔Attempt link before provider I/O, idempotent terminal reconciliation). Linked stale **`STARTED`** (≥60m) → **`RECOVERY_REQUIRED`** (keeps **`lease_token`**, clears **`lease_expires_at`**; no provider rerun; late fenced worker may still terminalize). Guarantee: at-most-once provider orchestration per Request, not exactly-once delivery. **PR3** adds `enqueue_transkribus_corrected_current_sync` (Document-lock send-right; post-send CAS; no `QUEUED`/`RUNNING`/`RECOVERY_REQUIRED` resend; stranded pre-send `QUEUED` deferred to recovery/requeue). **Staff UI POST** on the existing corrected/current attempts page enqueues via that service only (no sync in the web process; no automatic activation). Feature gate / IAM send grant / recovery command remain deferred. Management command **`sync_transkribus_corrected_current`** unchanged (no Request correlation).

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

Non-Hebrew documents may remain **`PARTIAL`** when **`HEBREW_TEXT`** is missing or translation failed — **intentional**, not an OCR failure. **`READY`** rollup requires usable **`SOURCE_TEXT`** and **`HEBREW_TEXT`**.

## Where to read more

- `docs/ocr-routing-reference.md` — current OCR/HTR routing, models, and translation behavior.
- `docs/ai-context/decision-log.md` — durable decisions, Transkribus PR history, operational boundaries.
- `docs/ai-context/photo-archive-items.md` — PHOTO item design/scope (V1 implemented; deferred follow-ups documented there).
- `.cursor/rules/architecture.mdc` — layer boundaries and contracts for code changes.

## Near-term roadmap

1. ~~Transkribus remote identity schema + persistence wiring~~ → **done (PR1 + PR2)**.
2. ~~Duplicate upload guard (dev upload mode)~~ → **done (PR3)**.
3. ~~Recognition-only retry V1~~ → **done** (dev/staging recovery).
4. ~~Cleanup / retention V1 dry-run reporting~~ → **done**.
5. Remote deletion / cleanup automation later, only with explicit approval.
6. Broader Transkribus production routing only with explicit approval.

OCR quality/fidelity validation against the Transkribus UI is important but **not** the current docs/rules task.
