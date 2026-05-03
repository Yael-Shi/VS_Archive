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

### Execution layer (OCR/HTR engines)
After routing, execution is layered as follows:

1. **Routing** (`documents/services/ocr_routing.py`): `select_ocr_route` returns `OcrRouteConfig` (`engine_key`, `prompt_variant`) only. It does not call providers.

2. **Dispatcher** (`documents/services/htr_engine.py`): `transcribe_pages` resolves the adapter by `engine_key` and calls `adapter.execute(...)`. An optional `route` argument allows the caller (worker) to pass the same route used for persistence so routing is not re-derived inside the dispatcher.

3. **Adapter registry** (`documents/services/htr_adapters/registry.py`): static map from `engine_key` to adapter implementation. Unknown keys raise `UnsupportedEngineError`.

4. **Provider adapters** (e.g. `documents/services/htr_adapters/gemini_adapter.py`): own provider-specific execution (model fallback list, quota / exhaustion handling, error mapping). **Transkribus is not implemented yet**; no Transkribus adapter or route entries exist at this time.

### Route metadata vs OCR result payload
`engine_key` and `prompt_variant` are **routing metadata**. They are selected by the routing layer and carried through the worker for persistence on `DocumentTextResult`. They are **not** part of the minimal OCR result payload (`HtrResult`: text, review flags, runtime engine name, etc.).

### DocumentTextResult fields
- **`engine`**: continues to mean the runtime processing identity used for uniqueness and processing-state rollups (e.g. concrete Gemini model id, or failure-path markers such as `ocr-dispatch` / `unsupported:<key>`). Do not repurpose this field for provider routing keys.

- **`engine_key` / `prompt_variant`**: stored on `DocumentTextResult` for auditability and reproducibility. Values come from the **selected route** on success (worker-held `OcrRouteConfig`) and from route re-selection or explicit unresolved markers on failure paths.

### DocumentTextResult.OcrEngineKey schema limitation
`DocumentTextResult.OcrEngineKey` currently allows **GEMINI** only. Adding a second live engine (e.g. Transkribus) will require **enum and migration expansion** in the first real Transkribus implementation PR. That work is intentionally deferred until Transkribus is actually implemented.

### Gemini prompt selection
Gemini should receive a `prompt_variant` key, not arbitrary prompt text.

Gemini should choose the actual prompt internally based on that key.

The Gemini JSON output contract must remain unchanged.

Gemini-specific tuning (temperature, model candidates, quota fallback, etc.) belongs in `GeminiAdapter` and/or `gemini_engine.py`, not in generic worker retry loops.

### SQS / adapter errors (current policy)
For now:

- Adapter and processing errors are persisted as document OCR failures (`DocumentTextResult` failed rows, appropriate `error_code` where defined).
- SQS messages are **acked** after handling (including failures): there is no automatic re-drive or DLQ split based on error class.

Engine-specific retry, backoff, visibility-timeout tuning, and DLQ policy are **deferred** and not part of the current worker contract.

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
