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

### Deferred: `UNRESOLVED` routing-failure markers vs TextChoices
When routing metadata cannot be resolved (`OCR_ROUTING_INVALID`), failed rows persist `engine_key` and `prompt_variant` as the literal string **`UNRESOLVED`** so the outcome is explicit and avoids misleading `GEMINI` / `handwritten` fallbacks.

That sentinel is **not** listed on `DocumentTextResult.OcrEngineKey` or `OcrPromptVariant` today. Django persists it without running model validation in the normal `save()` path; forms or admin that assume only declared choices may need care.

**Future work (separate PR):** add first-class choice values, use nullable fields, or otherwise align the schema with the sentinel—out of scope for the pre-Transkribus cleanup PR.

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

## Transkribus integration — PR #1 (skeleton / stable connection point)

### Decision

The first Transkribus PR establishes only the **plumbing** so a second engine can exist in the same architecture as Gemini, **without** changing production routing or calling Transkribus.

### Current behavior (after PR #1)

- `DocumentTextResult.OcrEngineKey` includes **`TRANSKRIBUS`** (with migration updating the field choices).
- `TranskribusAdapter` is registered in `documents/services/htr_adapters/registry.py` (`get_htr_adapter`) with `engine_key = "TRANSKRIBUS"`.
- The adapter’s `execute` raises **`EnginePermanentError`** with an explicit “not implemented yet” message (no HTTP, no multi-page policy).
- `documents/services/ocr_routing.py` (`OCR_ROUTES`) remains **all GEMINI**; no document is routed to Transkribus until a follow-up PR changes routing.

### Deferred (follow-up PRs)

- Routing entries that select `TRANSKRIBUS` for specific `(language, text_input_type)` pairs.
- Hybrid or fallback between engines (still out of scope unless explicitly requested).

## Transkribus PR #2 — Legacy TrpServer PyLaia (existing server document only)

### What this is not

- **Not** a full Transkribus integration and **not** the complete VS-Archive path: user upload → Transkribus document creation → recognition → `DocumentTextResult`.
- **`OCR_ROUTES` unchanged**; production OCR remains **GEMINI** until a later routing PR.

### Scope

- **Legacy TrpServer only** (account has no Metagrapho / processing v2 under current plan).
- **Session login:** `POST https://transkribus.eu/TrpServer/rest/auth/login` with form fields `user` and `pw` (URL-encoded). PyLaia recognition **POST** uses this session (Bearer alone insufficient for recognition in verified testing).
- **PyLaia start:** `POST /pylaia/{colId}/{modelId}/recognition` with UI-aligned query parameters; **response body is plain-text job id** (not JSON).
- **Polling:** `GET /jobs/{jobId}` until success/failure/timeout rules in code.
- **Polling constants** in `transkribus_engine.py` (`POLL_INTERVAL_SEC`, `POLL_MAX_WAIT_SEC`, default HTTP timeouts) are **dev/demo defaults**. Before **production** Transkribus is selected via **`OCR_ROUTES`**, revisit **SQS visibility timeout vs. worker polling / job duration** (and any related queue behavior) so messages are not released mid-poll or held too long. PR #2 does not change `OCR_ROUTES` or `run_worker.py`.
- **Page metadata:** `GET /collections/{colId}/{docId}/pages?pages=…` returns a **JSON array** of page objects; transcript choice uses **`jobId` / `modelId`** when present in `tsList.transcripts`, not `transcripts[0]` blindly.
- **Transcript:** `GET` transcript URL; **Bearer** (`TRANSKRIBUS_API_TOKEN`) for `files.transkribus.eu` in verified testing.
- **PAGE XML** (`PcGts` namespace) → plain text via `TextLine` / `TextEquiv` / `Unicode`; lines joined with `\n`, pages with `\n\n`.

### Safeguards

- **`TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT`** (default false): if false, **`TranskribusAdapter` raises `EnginePermanentError` before any HTTP** (no silent no-op).
- **Dev-only document id:** `TRANSKRIBUS_DEV_EXISTING_DOCUMENT_ID` — not a VS-Archive document id; names the pre-existing TrpServer doc for dev/demo.
- **Dev-only pages query:** `TRANSKRIBUS_DEV_EXISTING_PAGES` supplies the `pages=` query value. **Do not** assume `PageImage.page_index` matches TrpServer page numbers in PR #2; `PageImage[]` is **validation-only** (non-empty).
- **Config** for collection/model: `TRANSKRIBUS_COLLECTION_ID`, `TRANSKRIBUS_MODEL_ID`. **No** hard-coded account ids in code.

### Explicit non-scope (PR #2)

- **No** upload / create-document on Transkribus (**PR #3** must implement upload + page mapping from VS-Archive bytes/`PageImage[]`).
- **No** `run_worker.py` changes.
- **No** `/recognition/atr` or **`htrCITlab`** for this flow (UI used `/pylaia/.../recognition`; `htrCITlab` tied to deprecated HTR+).

### Credentials

- Do **not** log username, password, session cookies, or tokens.

## Transkribus PR #3 — Legacy `/uploads` ingest (engine-only; no adapter wiring yet)

### Decision

- **Engine layer only** in this phase: narrow helpers and parsers in `documents/services/transkribus_engine.py` for the Legacy **`POST /uploads` → `PUT /uploads/{uploadId}` (multipart `img`) → ingest `jobId` → `GET /jobs/{jobId}`** flow documented in the [Transkribus REST upload article](https://www.transkribus.org/blog/transkribus/docu/rest-api/upload) and **`/uploads` resources in** `https://transkribus.eu/TrpServer/rest/application.wadl` (and `?detail=true`).
- **PR #3 does not implement full upload orchestration** (no single function chaining create → N×PUT → poll → `docId` → pages map in this PR).
- **PR #3 does not wire the adapter** (`TranskribusAdapter` unchanged).
- **PR #3 does not add env flags** for Transkribus upload (no new `WorkerEnvConfig` / `validate_required_env` fields for this flow).
- **No** `OCR_ROUTES`, **`run_worker.py`**, **`HtrResult`**, **`DocumentTextResult.engine`** semantics, or **DB schema** for Transkribus document ids in PR #3.
- **`docId` parsing is provisional:** code assumes **top-level** **`docId`** on a terminal-success `GET /jobs/{jobId}` JSON object **only until** an **authenticated redacted** successful ingest job response from **Yael** confirms or corrects that shape for this account. It is **not** claimed as proven for this deployment; third-party client key lists are hints only.
- **No** cleanup/retention in PR #3. **Once orchestration exists in a later PR**, dev uploads may **accumulate** in the target collection and retries may create **duplicate** server-side documents until an explicit retention/dedup policy is designed.

### Verified / narrow contracts encoded in code

- **Descriptor JSON:** `md` (optional) + `pageList.pages[]` with **`fileName`** and **`pageNr`** only (no `pageXmlName`), so each page can use **`img`**-only multipart `PUT` per upload docs (“all other fields optional”; `xml` only if `pageXmlName` was set). Page rows are ordered by ascending **`PageImage.page_index`**.
- **`uploadId`:** parsed only from **top-level** JSON **`uploadId`** on create response (fixture-aligned test).
- **`jobId` after PUT:** parsed only when the `PUT` response body is non-empty JSON with top-level **`jobId`** (optional until last page).
- **`docId` after ingest:** see provisional rule above; parser is intentionally narrow pending redacted trace.

### Deferred

- Full **orchestration** helper and PR #2 **recognition** composition after upload (reuse existing PyLaia functions without duplicating their bodies) once the manual trace is archived.
- **Adapter** wiring (and any new env flag) remains deferred until orchestration is stable.
