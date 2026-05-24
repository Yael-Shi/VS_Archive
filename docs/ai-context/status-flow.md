# VS-Archive — Status Flow Guardrails

Suggested repository path: `docs/ai-context/status-flow.md`

Purpose: keep the status layers in VS-Archive explicit, especially before Transkribus duplicate-prevention/reprocess work.

## Rule zero: do not collapse status layers

VS-Archive has several independent status layers. They answer different questions.

| Layer | Field | Answers |
|---|---|---|
| Upload | `Document.upload_status` | Did the original file upload complete? |
| Processing readiness | `Document.processing_state_user` | Are the expected outputs present and usable/displayable? |
| Text row output | `DocumentTextResult.status` | Was this text row produced, failed, or produced but requiring review? |
| Human verification | `DocumentTextResult.verification_status` | Did a human approve/reject this text as ground truth? |
| Review explanation | `DocumentTextResult.review_reasons` | Why does this automatic text need review? |
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

## Expected outputs

### Hebrew

Current accepted behavior:

- Rollup requires `HEBREW_TEXT`.
- Worker may also persist `SOURCE_TEXT`.
- Long-term simplification remains open.

### Non-Hebrew

Expected outputs:

- `SOURCE_TEXT`
- `HEBREW_TEXT`

Hebrew translation is not implemented yet, so non-Hebrew documents may intentionally remain `PARTIAL` when `HEBREW_TEXT` is missing. This is not an OCR failure.

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

## PR guardrails

When implementing Transkribus duplicate prevention / reprocess policy:

- Do not put Transkribus-specific logic in `run_worker.py`.
- Do not change production routing.
- Do not add Gemini→Transkribus fallback.
- Do not add hybrid routing.
- Do not change `HtrResult`.
- Do not store remote ids in `HtrResult`, `review_reasons`, `DocumentTextResult.engine`, or `prompt_variant`.
- Do not change READY/PARTIAL semantics.
- Do not change automatic OCR success persistence: `NEEDS_REVIEW + UNVERIFIED`.
- No live Transkribus calls in automated tests.
