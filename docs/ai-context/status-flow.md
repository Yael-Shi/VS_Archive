# VS-Archive — Status Flow Guardrails

Purpose: keep the status layers in VS-Archive explicit, especially before future Transkribus reuse, cleanup, reprocess, and verification work.

## Rule zero: do not collapse status layers

VS-Archive has several independent status layers. They answer different questions.

| Layer | Field | Answers |
|---|---|---|
| Upload | `Document.upload_status` | Did the original file upload complete? |
| Processing readiness | `Document.processing_state_user` | Are the expected outputs present and usable/displayable? |
| Text row output | `DocumentTextResult.status` | Was this text row produced, failed, or produced but requiring review? |
| Human verification | `DocumentTextResult.verification_status` | Did a human approve/reject this text as ground truth? |
| Review explanation | `DocumentTextResult.review_reasons` | Why does this automatic text need review? |
| Gemini OCR identity | `GeminiOcrAttempt.status` | Is this exact source/route/prompt/config page set complete? |
| Gemini OCR page | `GeminiOcrPageCheckpoint.status` | Was this exact page claimed, produced, or failed? |
| External Transkribus attempt | `TranskribusRun.status` | What happened in the external Transkribus attempt lifecycle? |

These layers must not be treated as aliases.

## `Document.upload_status`

File-upload state only.

- `UPLOADING`: `Document` exists but upload is not completed.
- `UPLOADED`: original file exists in S3 and can be processed.
- `FAILED`: upload failed.

This says nothing about OCR/HTR, translation, text quality, Transkribus state, or human verification.

## `Document.processing_state_user`

User-visible processing/readiness state.

- `PROCESSING`: worker is still processing.
- `READY`: all expected outputs exist and are usable/displayable.
- `PARTIAL`: one or more expected outputs are missing, failed, blank, or unusable.
- `FAILED`: no required usable output / expected outputs failed.

Important:

`READY` does **not** mean verified, correct, human-approved, or ground truth.

Valid state:

```text
Document.processing_state_user = READY
DocumentTextResult.status = NEEDS_REVIEW
DocumentTextResult.verification_status = UNVERIFIED
```

Meaning: text exists and can be displayed/worked with, but it still needs human review.

`PARTIAL` must not mean merely “waiting for human review”.

For Gemini page checkpointing, `PARTIAL` also has a precise structural meaning:
the matching `GeminiOcrAttempt.missing_page_indices` contains one or more
failed, unattempted, or otherwise non-succeeded 1-based page indices. The
request failure metadata may repeat only those indices as
`missing_pages=...`; it must not include OCR text.

## `DocumentTextResult.status`

Row-level OCR/HTR output state.

- `NEEDS_REVIEW`: normal status for successful automatic OCR/HTR persisted by the worker.
- `SUCCEEDED`: still a valid enum for future trusted/imported/no-review-required paths, but not the normal automatic worker success status.
- `FAILED`: technical/pipeline/dispatch/routing/OCR failure for that row.

Important:

`NEEDS_REVIEW` is **not** a technical failure.

## `DocumentTextResult.verification_status`

Human review layer.

- `UNVERIFIED`: default after automatic OCR/HTR.
- `VERIFIED`: human-approved / ground-truth layer.
- `REJECTED`: human rejected this text result.

If you need to know whether text is ground truth, check `verification_status`, not `Document.processing_state_user`.

## `review_reasons`

Explanations, not lifecycle status.

Known policy/content reasons include:

- `AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW`: generic policy reason for automatic OCR/HTR.
- `NEEDS_REVIEW_FLAG`: only when `HtrResult.needs_review=True`; do not use as the generic policy reason.
- `MIN_TEXT_LENGTH`
- `HAS_UNCLEAR`
- engine-provided review reasons

Do not store Transkribus remote ids, job ids, page mappings, upload ids, or docIds in `review_reasons`.

## Gemini OCR page checkpoint states

`GeminiOcrAttempt` is independent of `ProcessDocumentRequest` and identifies
one exact source/route/model-candidate/prompt/configuration contract.

- `IN_PROGRESS`: at least one page may be claimed or processing.
- `PARTIAL`: one or more expected pages are not `SUCCEEDED`; exact indices are
  stored in `missing_page_indices`.
- `COMPLETED`: every expected page is `SUCCEEDED`,
  `missing_page_indices=[]`, and deterministic assembly is available.

`GeminiOcrPageCheckpoint`:

- `RUNNING`: a token/lease currently owns provider execution.
- `SUCCEEDED`: durable page text and actual runtime model are stored.
- `FAILED`: safe failure classification is stored; a later intentional OCR
  execution may reclaim this page.

A `SUCCEEDED` checkpoint is not human verification and is not itself a
displayable `DocumentTextResult`. Human review status remains on the assembled
document-level result.

An expired page lease may be reclaimed with a new token, but this does not
change request-level recovery: `ProcessDocumentRequest.RECOVERY_REQUIRED`
remains fenced and is not automatically replayed.

## Expected outputs

### Hebrew

Current accepted behavior:

- Rollup requires `HEBREW_TEXT`.
- Worker may also persist `SOURCE_TEXT`.
- Long-term simplification remains open.

### Non-Hebrew

Expected outputs:

- `SOURCE_TEXT` (OCR/HTR output)
- `HEBREW_TEXT` (Gemini Hebrew translation after OCR; see **`docs/ocr-routing-reference.md`**)

After successful OCR/HTR, the worker persists **`SOURCE_TEXT`**, then calls automatic Gemini Hebrew translation and persists **`HEBREW_TEXT`**. Translation failure persists a failed **`HEBREW_TEXT`** row; the document may remain **`PARTIAL`** until translation succeeds or is retried. This is not an OCR failure.

## `TranskribusRun.status`

External Transkribus attempt lifecycle only.

Current statuses:

- `STARTED`
- `UPLOADED`
- `RECOGNITION_STARTED`
- `SUCCEEDED`
- `FAILED`

Important:

`TranskribusRun.status=SUCCEEDED` means the external Transkribus attempt completed.

It does **not** mean:

- `DocumentTextResult.status=SUCCEEDED`
- `DocumentTextResult.verification_status=VERIFIED`
- human approval
- ground truth
- document is archivally approved

Valid state:

```text
TranskribusRun.status = SUCCEEDED
DocumentTextResult.status = NEEDS_REVIEW
DocumentTextResult.verification_status = UNVERIFIED
Document.processing_state_user = READY
```

## Recognition-only retry (dev/staging V1)

A **new** `TranskribusRun` row is created for each recognition-only recovery attempt. That row tracks the **new** PyLaia attempt lifecycle; it does **not** mean a new Trp upload occurred.

`TranskribusRun.SUCCEEDED` on a prior upload-created run is **not** a reusable source for V1 recognition-only retry (recovery targets failed or incomplete attempts only).

## Cleanup / retention reporting (V1)

`report_transkribus_cleanup` is an **operational dry-run report**, not a status layer.

- A cleanup bucket such as `review_failed_after_upload_remote_doc` or `review_stale_in_progress_run` does **not** change:
  - `Document.processing_state_user`
  - `DocumentTextResult.status`
  - `DocumentTextResult.verification_status`
  - `TranskribusRun.status`
- `review_*` buckets are **manual investigation hints**, not deletion approval.
- `retain_*` buckets are operational safety classifications, not human verification.

## PR guardrails

When implementing Transkribus duplicate prevention / reprocess policy:

- Do not put Transkribus-specific logic in `run_worker.py`.
- Do not broaden Transkribus routing beyond Hebrew handwritten, and do not route Hebrew handwritten documents to Gemini when `ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN` is disabled.
- Do not add Gemini→Transkribus fallback.
- Do not add Transkribus→Gemini fallback.
- Do not add hybrid routing.
- Do not change `HtrResult`.
- Do not store remote ids in `HtrResult`, `review_reasons`, `DocumentTextResult.engine`, or `prompt_variant`.
- Do not change READY/PARTIAL semantics.
- Do not change automatic OCR success persistence: `NEEDS_REVIEW + UNVERIFIED`.
- No live Transkribus calls in automated tests.
