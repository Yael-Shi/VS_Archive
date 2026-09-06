# Arabic printed banded OCR — ambiguous Vision / provider-create fences

Operational runbook for fail-closed checkpoints after Cloud Vision or
Antigravity create reservations. This is diagnosis and recovery policy only.
It does not change routing, page budget, retry/cancel code, or quality scoring.

Fail-closed behavior is intentional. An ambiguous reservation means a provider
call may already have been issued, while durable success state was never
persisted. Reclaim must not create, poll, cancel, or select Cloud Vision
low-quality for that unit.

Related code: `reserve_arabic_printed_vision_call`,
`reserve_arabic_printed_primary_create`,
`reserve_arabic_printed_fallback_create`,
`process_claimed_arabic_printed_page`,
`ocr_reprocess._arabic_printed_page_is_permanently_fenced`,
`arabic_printed_ambiguous_fence_resolution` /
`resolve_arabic_printed_ambiguous_fence`.
Staff reprocess eligibility: `docs/ai-context/decision-log.md`
(“Checkpoint-backed Arabic printed banded PARTIAL documents remain
reprocessable”). Request delivery repair (not fence reset):
`docs/ai-context/process-document-request-recovery.md`.

## Fence types

### Vision reserved without a durable plan

Page checkpoint has `cloud_vision_call_count=1` and no durable plan:

- `band_count` is 0, or
- `cloud_vision_response_sha256` is empty, or
- no `ArabicPrintedOcrBandCheckpoint` rows.

Typical cause: Vision reservation committed, then the process died, timed out,
or failed before `persist_arabic_printed_vision_plan`. The Vision call may have
run. The orchestrator persists `ARABIC_PRINTED_VISION_AMBIGUOUS` and never
retries Vision on that page.

### Ambiguous Antigravity create (primary or fallback)

A band has `create_call_count >= 1` (or the matching
`ARABIC_PRINTED_PRIMARY_AMBIGUOUS` / `ARABIC_PRINTED_FALLBACK_AMBIGUOUS`
failure code) without a trusted persisted interaction id for that attempt.
A live Interactions request may exist at the provider. Reclaim must not create,
poll, cancel, or take the low-quality path for that band.

## Read-only diagnosis

Do not call Cloud Vision, Gemini Interactions, or SQS. Do not UPDATE rows.

Inspect, for the document id:

1. `Document.processing_state_user`, `language`, `text_input_type`.
2. Latest `ProcessDocumentRequest` status / `failure_code` / `failure_message`
   (PARTIAL vs FAILED is request-attempt semantics; see decision-log).
3. `ArabicPrintedOcrAttempt` rows: `identity_fingerprint`,
   `route_fingerprint`, `prompt_fingerprint`, `config_fingerprint`,
   `status`, `updated_at`.
4. Each `ArabicPrintedOcrPageCheckpoint` on the latest current-contract
   attempt: `status`, `lease_expires_at`, `cloud_vision_call_count`,
   `band_count`, `cloud_vision_response_sha256`, `failure_code`.
5. Each `ArabicPrintedOcrBandCheckpoint`: `status`, `create_call_count`,
   `failure_code`, primary/fallback interaction ids, `cancel_confirmed_status`.

A page is permanently fenced when:

- `failure_code=ARABIC_PRINTED_VISION_AMBIGUOUS`, or
- Vision count is nonzero without a durable plan, or
- any band `failure_code` is `ARABIC_PRINTED_PRIMARY_AMBIGUOUS` or
  `ARABIC_PRINTED_FALLBACK_AMBIGUOUS`.

A live lease (`status=RUNNING` and `lease_expires_at` in the future) is busy,
not an invitation to steal the page.

## Why automatic retry is unsafe

Retry/reclaim after an ambiguous reservation can:

- spend a second Vision call (the schema allows at most one),
- create a second Antigravity interaction while the first is still live,
- poll/cancel the wrong interaction,
- treat truncated provider state as a clean FAILED page and take
  low-quality selection.

Staff OCR reprocess (`NORMAL_REENQUEUE`) is safe only for unfinished pages
that are **not** permanently fenced. It does not reset fences. If every
unfinished page on the current-contract attempt is fenced, the UI must not
present resume as available.

## What must never be blindly reset

Do not UPDATE or DELETE any of the following to “unblock” a document
outside the reviewed operator command below:

- `cloud_vision_call_count`
- `cloud_vision_response_sha256`, `band_count`, band plan identity fields
- `create_call_count`
- persisted interaction ids
- `ARABIC_PRINTED_*_AMBIGUOUS` failure codes
- identity fingerprints (source, route, prompt, config, oriented image)
- a still-valid page lease token / `lease_expires_at`

Resetting those counters on the automatic path is how a second provider
call happens.

## Operator command (reviewed, fail-closed)

`python manage.py resolve_arabic_printed_ambiguous_fence` is dry-run by
default. Pass `--apply` to write. It does not call Cloud Vision, Gemini,
Antigravity, SQS, or OCR, and it does not modify the search index.

Dry-run uses ordinary reads. `--apply` re-evaluates every precondition from
rows locked in one transaction, in this order: Document; DocumentTextResult
rows by id (reject VERIFIED); current-contract ArabicPrintedOcrAttempt
(matching attempts locked by id, then the latest updated/pk is selected);
that attempt's page checkpoints by id (reject any live lease); the target
page's band checkpoints by id. It does not apply an unlocked dry-run plan.

Preconditions (all must match exactly or the command fails closed):

- worker-reusable current-contract attempt
- no live page lease on that attempt
- exact document / page / band
- exact `--expected-failure-code` and counter/status shape
- no conflicting persisted interaction id
- page/band not `SUCCEEDED`
- no `VERIFIED` `DocumentTextResult`

Modes:

1. **`--mode no-provider-call`** — use only after external verification that
   the reserved call was never created or sent.
   - Vision (`ARABIC_PRINTED_VISION_AMBIGUOUS`, count=1, no band rows, no
     plan hash): after the locked checks, set `cloud_vision_call_count=0` and
     replace the fence code with `ARABIC_PRINTED_OPERATOR_RESOLVED` so the
     existing worker path may reserve Vision once. Append a bounded JSON
     event to `operator_resolution_audit` (schema
     `arabic-printed-operator-fence-resolution-v1`: mode, target, reason,
     expected failure code, prior Vision count, page index). Worker
     claim/reclaim/success/failure must not clear that field. Dry-run prints
     the planned audit write. `failure_message` is not the audit trail.
   - Primary (`ARABIC_PRINTED_PRIMARY_AMBIGUOUS`, `FAILED`, count=1, empty
     primary id): restore `PENDING` and `create_call_count=0`.
   - Fallback (`ARABIC_PRINTED_FALLBACK_AMBIGUOUS`, `FAILED`, count=2, empty
     fallback id, persisted primary id): restore `create_call_count=1` and
     `ARABIC_PRINTED_PRIMARY_FAILED` so the worker may reserve fallback once.
   - Band audit appends to `primary_safe_diagnostics` /
     `fallback_safe_diagnostics` (survives later page claim).
   - Unsafe when an interaction id is already persisted for that slot.

2. **`--mode bind-interaction --interaction-id …`** — for ambiguous
   primary/fallback create when the exact provider interaction id has been
   identified. The id must match the shared Antigravity contract
   (`A-Za-z0-9._:-`) and the persisted field max length 128. Persists that id
   on the empty slot and restores `PRIMARY_RUNNING` / `FALLBACK_RUNNING`.
   Does not create or poll. Later worker reclaim polls/reconciles it.

The command does not enqueue reprocess. After a successful apply, use the
existing staff OCR reprocess / worker path if the page is otherwise
resumable.

## What to verify before any manual intervention

1. The attempt is the worker-reusable current contract (route/prompt/config
   fingerprints match current printed-Arabic Antigravity constants). Older
   identities are leftover, not resume targets.
2. No page on that attempt has a live lease.
3. SUCCEEDED pages and durable Vision plans are intact.
4. Provider-side evidence, if an operator is allowed to inspect it at all:
   whether a Vision request or Interactions id actually exists. Absence of a
   local interaction id is **not** proof that the provider never created one.
5. `ProcessDocumentRequest` recovery (requeue `QUEUED` / `ENQUEUE_FAILED`)
   does not repair fenced pages. Do not treat `recover_process_document_requests`
   as fence clearance.

## Evidence that permits recovery (without fence reset)

Safe to resume via existing staff OCR reprocess / worker reclaim:

- page `PLANNING`, or expired `RUNNING`, or unfenced `FAILED`
- durable Vision plan present (`cloud_vision_call_count=1`, `band_count>=1`,
  response hash, band rows) so Vision is not called again
- bands that are `PENDING` / unfenced `FAILED` with `create_call_count=0`
- SUCCEEDED pages reused as-is
- stale `CANCEL_PENDING` with a known interaction id, using the existing
  recovery poll (not a new create); see the CANCEL_PENDING decision-log entry

Needs the operator command above (not automatic reclaim, not a SQL counter
reset):

- Vision ambiguous / reserved without plan, after verifying the Vision call
  was never sent (`no-provider-call`)
- ambiguous primary or fallback create, after verifying the create was never
  sent (`no-provider-call`) or after identifying the exact interaction id
  (`bind-interaction`)

Still not recoverable by this command:

- every unfinished page fenced until each fence is resolved separately
- live lease still held
- partial Vision plans (band rows present without a trusted plan); this
  command does not delete checkpoints
- `SUCCEEDED` pages/bands
- `VERIFIED` text results

Hebrew translation retry is a different operation. It does not clear Arabic
page fences. Usable `SOURCE_TEXT` with failed/missing `HEBREW_TEXT` is
document `PARTIAL` and is not this runbook.

## JSON Antigravity path

When `ENABLE_ANTIGRAVITY_ARABIC_PRINTED_BANDED` is false, the worker uses the
whole-document JSON Interactions path. These Vision/band create fences do not
apply. Truncated-JSON split recovery on that path is still in force and must
not be removed.
