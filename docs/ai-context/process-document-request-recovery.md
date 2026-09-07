# PROCESS_DOCUMENT Request recovery runbook

Use `recover_process_document_requests` only to repair delivery of an existing,
already-approved durable Request. It does not create new OCR or translation
intent and it never replays `RUNNING`, `RECOVERY_REQUIRED`, or terminal work.

## Eligibility

A Request is eligible when its `updated_at` is older than the cooldown
(default: 15 minutes) and it is either:

- `QUEUED` with no `last_enqueued_at`; or
- `ENQUEUE_FAILED`, including `ENQUEUE_OUTCOME_UNKNOWN`.

A successful recovery may produce a duplicate SQS message if an earlier
ambiguous send was actually delivered. Request locking and lease fencing ensure
that only one worker receives execution right.

Before reservation, the service also revalidates current intent:

- OCR reprocess must still be eligible and must resolve to the same retry
  mode/source run stored on the Request.
- Hebrew translation retry must still pass source and overwrite protection.
- Upload finalize must still reference an uploaded OCR Document with no
  verified text and no usable existing source text.

Failed reassessment is reported as `INTENT_NO_LONGER_VALID` or
`REQUEST_PAYLOAD_NO_LONGER_MATCHES` and is never sent.

## Safe operating sequence

Run from `app/backend` with the normal Django environment.

1. Report all currently eligible Requests. This is always read-only:

   ```bash
   DJANGO_ENV=local DJANGO_DEBUG=1 poetry run python manage.py \
     recover_process_document_requests
   ```

2. Inspect one Request or all active Requests for one Document without writing:

   ```bash
   DJANGO_ENV=local DJANGO_DEBUG=1 poetry run python manage.py \
     recover_process_document_requests --request-id REQUEST_ID

   DJANGO_ENV=local DJANGO_DEBUG=1 poetry run python manage.py \
     recover_process_document_requests --document-id DOCUMENT_ID
   ```

3. Apply only the reviewed scope:

   ```bash
   DJANGO_ENV=local DJANGO_DEBUG=1 poetry run python manage.py \
     recover_process_document_requests \
     --request-id REQUEST_ID \
     --apply
   ```

   Repeat `--request-id` to recover several explicitly reviewed Requests.
   `--document-id` may also be repeated.

4. Use bulk apply only after reviewing the unscoped dry-run:

   ```bash
   DJANGO_ENV=local DJANGO_DEBUG=1 poetry run python manage.py \
     recover_process_document_requests \
     --all-eligible \
     --limit 100 \
     --apply
   ```

## Controls

- `--older-than-minutes N` changes the cooldown. The default is 15 and the
  minimum is 1; zero would remove the reservation's exclusion window.
- `--limit N` limits inspected/recovered rows. Default 100; maximum 1000.
- `--apply` without Request/Document scope or `--all-eligible` is rejected.
- Missing explicit Request ids, explicit scopes larger than `--limit`, and
  invalid option values fail before mutation.
- Expected queue failures are persisted safely and make the command exit
  nonzero after printing its summary.
- Unexpected programming exceptions propagate.

## Output

Dry-run rows use `action=would_requeue`. Apply rows include
`enqueue_outcome` and `observed_status`. The final summary reports selected,
eligible, handled, skipped, and send-failure counts.

`BLOCKED_RECOVERY_REQUIRED` is not authorization to replay execution. It means
the Request remains fenced for separate execution recovery work.

When a `RUNNING` Request is fenced to `RECOVERY_REQUIRED`, a related Document
that is still `PROCESSING` is updated to `RECOVERY_REQUIRED` in the same
transaction. That Document state is a request-lifecycle overlay, not a
substitute for engine-scoped DTR rollup. A late fenced worker may terminalize
the Request only after the Document is `READY` / `PARTIAL` / `FAILED`.
`RECOVERY_REQUIRED` does not authorize a new provider execution. This
command still does not replay `RECOVERY_REQUIRED` execution.

## Expired RUNNING lease fencing (separate command)

SQS redelivery is not required to fence a stale `RUNNING` Request. Use
`fence_expired_process_document_requests` for that. It is dry-run by
default, never resends work, never calls a provider, and never redives the
DLQ.

Eligible writes are only `ProcessDocumentRequest.status=RUNNING` whose
`lease_expires_at` is `<= now` or `NULL` (defensive; `NULL` is illegal on
`RUNNING` by constraint). The locked transition is the same helper worker
claim uses: Request → `RECOVERY_REQUIRED`, retain `lease_token`, clear
`lease_expires_at`, overlay Document `PROCESSING` → `RECOVERY_REQUIRED`,
leave `READY` / `PARTIAL` / `FAILED` / existing Document overlay unchanged.
Late retained-token completion / terminalize rules are unchanged.

```bash
DJANGO_ENV=local DJANGO_DEBUG=1 poetry run python manage.py \
  fence_expired_process_document_requests --request-id REQUEST_ID

DJANGO_ENV=local DJANGO_DEBUG=1 poetry run python manage.py \
  fence_expired_process_document_requests --request-id REQUEST_ID --apply

DJANGO_ENV=local DJANGO_DEBUG=1 poetry run python manage.py \
  fence_expired_process_document_requests --all-eligible --limit 100 --apply
```

`--apply` requires `--request-id`, `--document-id`, or `--all-eligible`.
Do not combine `--all-eligible` with id scopes. `TranskribusCorrectedCurrentSyncRequest`
is out of scope. Do not use this command to replay `RECOVERY_REQUIRED`.
