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
- If metadata needed for routing is missing or invalid at processing time, fail with a clear error (no silent fallback).

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

Status update:
- This policy mismatch remains deferred.
- It is intentionally out of scope for the pre-Transkribus engine-dispatch refactor PR.
- Revisit in a dedicated PR before or during Transkribus integration if still applicable.

Future work:
Investigate and fix non-Hebrew `HEBREW_TEXT` persistence/status behavior separately.
