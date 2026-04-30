# VS-Archive Decision Log

## OCR/HTR routing by language and text input type

### Decision
OCR/HTR processing will be routed by two explicit document metadata fields:

- `Document.language`
- `Document.text_input_type`

`text_input_type` is required.

Allowed values:
- `HANDWRITTEN`
- `PRINTED`

There is no `UNKNOWN` value.

### Existing documents
Existing documents should be migrated/defaulted to:

- `HANDWRITTEN`

Reason:
The current existing document set is known to be handwritten, so this is not treated as an unknown/default guess.

### Upload behavior
Upload must require selecting `text_input_type`.

The upload form should not allow submission without choosing whether the document is handwritten or printed.

Validation strategy:
- Enforce `text_input_type` at upload/create time.
- Also validate defensively during processing/routing.
- If metadata needed for routing is missing or invalid at processing time, persist an explicit failure (`OCR_ROUTING_INVALID`) and do not silently fallback to `GEMINI/handwritten`.

### Admin editing
Admins must be able to edit `text_input_type` in the existing admin/metadata edit flow.

Do not create a separate new UI unless no relevant edit flow exists.

### Language routing
`Document.language` already uses fixed enum values:

- `he`
- `en`
- `fr`
- `ar`

Routing should use explicit entries per language and text input type.

Do not create broad language groups such as `LATIN`.

Even if English and French currently route to the same engine/prompt, they should remain separate routing table entries so they can diverge later with a one-line change.

### Routing architecture
Routing logic must live in a dedicated routing module, for example:

- `documents/services/ocr_routing.py`

The worker must not contain language-specific or text-input-specific routing logic.

The selector should return configuration only, such as:

- `engine_key`
- `prompt_variant`

The selector should not call the engine directly.

### Gemini prompt selection
Gemini should receive a `prompt_variant` key, not arbitrary prompt text.

Gemini should choose the actual prompt internally based on that key.

The Gemini JSON output contract must remain unchanged.

### DocumentTextResult.engine
Do not change the semantics of `DocumentTextResult.engine` in the current routing PR.

If processing-state logic depends on the existing engine value, preserve that behavior.

Future work:
Add separate observability/audit fields if needed, such as:

- `engine_key`
- `engine_model`
- `prompt_variant`
- `routing_decision`

Do this in a separate PR, not as part of the initial routing implementation.

### Reprocessing behavior
Changing `Document.text_input_type` after a document has already been processed must not automatically trigger OCR/HTR reprocessing in this PR.

Future work:
Design an explicit reprocessing workflow.

### Deferred issue: non-Hebrew Hebrew translation result
There may be a mismatch between:

- `expected_outputs.py`
- `run_worker.py::_save_htr_results`

around expected `HEBREW_TEXT` results for non-Hebrew documents.

Do not fix this in the routing PR.

Future work:
Investigate and fix non-Hebrew `HEBREW_TEXT` persistence/status behavior separately.

## Non-Hebrew HEBREW_TEXT expected output — current intentional gap

### Decision
For non-Hebrew documents, the expected output policy remains:

- `SOURCE_TEXT`
- `HEBREW_TEXT`

However, real Hebrew translation is not implemented yet.

### Current intended behavior
Until translation is implemented:

- Non-Hebrew documents may produce only `SOURCE_TEXT`.
- In that case, `processing_state_user` should remain `PARTIAL`.
- This is intentional, not a bug.

### What would be a bug
It is a bug if:

- A non-Hebrew document is marked `READY` without a succeeded `HEBREW_TEXT`.
- The system creates `HEBREW_TEXT` for non-Hebrew documents with fake/placeholder translation.
- The worker silently treats missing translation as success.

### Deferred work
Implement real Hebrew translation for non-Hebrew documents in a separate PR.

When implemented, non-Hebrew documents should become `READY` only after both:

- `SOURCE_TEXT`
- `HEBREW_TEXT`

exist and succeeded.

## OCR routing observability

Current decision:
DocumentTextResult.engine keeps its existing semantics.

Future/current improvement:
Persist routing metadata separately from engine identity:
- engine_key
- engine_model
- prompt_variant

Reason:
As routing expands to multiple engines/prompts, each stored text result must remain traceable to the exact route that produced it.

Do not overload DocumentTextResult.engine with route names.

## Reprocessing workflow

Changing metadata such as text_input_type does not automatically trigger OCR/HTR reprocessing.

Future work:
Add explicit admin-triggered or management-command-based reprocessing.

Important constraint:
Do not overwrite VERIFIED text automatically.

## OCR/HTR observability and reprocessing — proposed next direction

### Context
The next planned major feature is adding a second OCR/HTR engine, likely Transkribus.

Before adding another engine, the system may need better traceability of OCR/HTR results and a safe way to re-run processing.

### Proposed direction, pending code review
Consider adding minimal OCR/HTR observability metadata to `DocumentTextResult`, without changing the existing semantics of `DocumentTextResult.engine`.

Possible new fields:
- `engine_key`
- `engine_model`
- `prompt_variant`
- `routing_reason`

The exact fields should be confirmed after inspecting the current code and how `engine` is currently used.

### Reprocessing direction, pending code review
Consider adding a basic explicit reprocessing mechanism before introducing a second engine.

Preferred initial shape:
- Management command only
- No UI yet
- No automatic reprocessing when metadata changes
- Do not overwrite `VERIFIED` text results unless an explicit `--force` flag is used
- Do not build full result versioning yet

The exact implementation should be decided after inspecting the current worker, queue, result persistence, and verification logic.

### Open questions
- Should observability metadata live on `DocumentTextResult`, or in a separate processing attempt/metric table?
- Which fields are actually needed now, and which are premature?
- Can existing `engine` values be safely copied into `engine_model`, or should they remain only in `engine` for now?
- Should reprocessing enqueue an existing worker message, call existing processing logic directly, or both?
- Which stages should be supported initially: OCR only, translation only, or full processing?
- What is the safest behavior for existing unverified results during reprocessing?


## Operational documentation needed

Document how to run Django management commands in production/AWS,
including one-off maintenance/reprocessing commands.

## OCR/HTR observability minimal scope

### Decision
Add minimal observability fields to `DocumentTextResult` before adding a second OCR/HTR engine.

Fields:
- `engine_key`
- `prompt_variant`

Do not add for now:
- `engine_model`
- `routing_reason`
- full processing attempt/versioning table

### Rationale
`DocumentTextResult.engine` already stores the runtime model/persistence identifier, so adding `engine_model` now would duplicate existing data.

`routing_reason` is deferred because routing is currently deterministic from `Document.language` and `Document.text_input_type`.

### Backfill
Existing rows may be backfilled as:
- `engine_key = GEMINI`
- `prompt_variant = handwritten`

This is acceptable because current data is testing data and current historical processing used the handwritten-oriented prompt.

### Deferred
Add `routing_reason` later only if routing becomes non-trivial, such as confidence fallback, manual override, or engine availability fallback.

### Future improvement / cleanup

Current implementation may duplicate route selection in worker failure paths
when no HtrResult is available.

This is acceptable for the initial observability PR to keep scope small.

Future refactor:
Propagate selected route metadata through the processing flow so failure paths
do not need to re-run route selection separately.

Current behavior for unresolved routing metadata:
- Persist failure results with `engine_key = UNRESOLVED` and `prompt_variant = UNRESOLVED`
- Set `error_code = OCR_ROUTING_INVALID`
- Keep normal success/failure paths unchanged when routing resolves normally
