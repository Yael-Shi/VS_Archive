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

- **Engine layer only:** helpers, parsers, job polling rules, and **`run_trp_upload_page_images_through_ingest`** in `documents/services/transkribus_engine.py` for the Legacy **`POST /uploads` → `PUT /uploads/{uploadId}` (multipart `img`) → ingest `jobId` → `GET /jobs/{jobId}`** flow documented in the [Transkribus REST upload article](https://www.transkribus.org/blog/transkribus/docu/rest-api/upload) and **`/uploads` resources in** `https://transkribus.eu/TrpServer/rest/application.wadl` (and `?detail=true`).
- **Verified on the real Transkribus account (authenticated trace):** `POST /uploads?collId=…` returns top-level **`uploadId`**; **`PUT …/{uploadId}`** with **img-only** multipart succeeds for PNG pages; the final ingest job is **`GET /jobs/{jobId}`** with **`state=FINISHED`**, **`success=true`**, **`type=Create Document`**, **`jobImpl=UploadImportJob`**, and top-level **`docId`**; **`GET /collections/{collId}/{docId}/pages?pages=…`** returns page metadata including **`pageNr`**, **`imgFileName`** matching the synthetic upload name, and **`tsList.transcripts`** (e.g. status NEW with PAGE XML URL).
- **PR #3 does not wire the adapter** (`TranskribusAdapter` unchanged).
- **PR #3 does not add env flags** for Transkribus upload (no new `WorkerEnvConfig` / `validate_required_env` fields for this flow).
- **No** `OCR_ROUTES`, **`run_worker.py`**, **`HtrResult`**, **`DocumentTextResult.engine`** semantics, or **DB schema** for Transkribus document ids in PR #3.
- **PR #3 does not run recognition** after upload; PR #2 PyLaia / transcript flow remains separate (no duplication inside the upload orchestrator).
- **`docId`:** **verified** as **top-level** on successful **UploadImportJob** for this account; parser remains **narrow** (top-level `docId` only).
- **Job polling (shared with PyLaia and other TrpServer jobs):** terminal **success** requires **`success is True`** and either a completed **`state`** (`FINISHED`, `DONE`, or `COMPLETED`) or a **missing/blank** `state` (legacy payloads). Terminal **failure** uses explicit states **`FAILED` / `ERROR` / `CANCELLED` / `CANCELED`**, or completed `state` **without** success. **`success=false` with `CREATED`** (e.g. queue description) is **non-terminal**—keep polling. Do **not** treat **`success=false` alone** as failure while the job is still queued or running. **`nrOfErrors` > 0** is terminal failure **only** when `state` is **not** in the non-terminal set (`''`, `CREATED`, `RUNNING`, `WAITING`, `QUEUED`); while still in those states, polling continues (covers in-progress noise without aborting early).
- **Production intent (upload path):** one VS-Archive **document upload / processing run** that uses Transkribus upload should create **one new** Transkribus document inside the configured **`TRANSKRIBUS_COLLECTION_ID`** via **`POST /uploads`** (new `docId`). Do **not** append pages to a fixed, pre-existing Transkribus `docId` as part of that design—that remains the separate **existing-server-document** dev path only.
- **No** cleanup/retention in PR #3. **Once adapter/worker use upload in production**, dev uploads may **accumulate** and retries may create **duplicate** server-side documents until an explicit retention/dedup policy is designed.

### Implemented in code (PR #3)

- **`run_trp_upload_page_images_through_ingest`:** descriptor → `POST /uploads` → ordered `PUT` of each PNG → last non-empty **`jobId`** → **`poll_job_until_done`** → **`parse_doc_id_from_successful_trp_job`** → **`fetch_pages_metadata`** → **`strict_map_page_index_to_trp_page_nr`** → **`TrpUploadOutcome`** (includes `pages_query` for follow-on calls).

### Deferred

- **Adapter / worker** calling upload orchestration then PR #2 recognition in one product path (and any new env flags for that) remains a **follow-up PR**.
- **Retention/dedup** for Transkribus-side documents.

## Transkribus PR #4 — Engine-only upload + PyLaia composition (`transkribus_engine.py`)

### Decision

- **`pylaia_transcribe_document_with_session`:** behavior-preserving extraction of the PyLaia start → poll → pages → transcripts → PAGE XML text path (formerly inline in **`transcribe_existing_server_document`**). **`transcribe_existing_server_document`** now only creates a session, logs in, and calls this helper—no duplicate recognition logic.
- **`upload_then_transcribe_page_images_with_pylaia`:** engine-only composition: session/login → **`run_trp_upload_page_images_through_ingest`** (new Trp **`docId`** + **`pages_query`**) → same **`pylaia_transcribe_document_with_session`** → returns **`HtrResult`** with the same **`engine_name`** pattern as **`TranskribusAdapter`** (`transkribus-pylaia:{model_id}`). **No** adapter wiring, **no** new env flags, **no** `OCR_ROUTES` / **`run_worker.py`** / **`env_validation`** / **models** / **migrations** changes in this PR.
- **`HtrResult` import:** `HtrResult` lives in `documents/services/htr_adapters/base.py` as the **shared HTR/OCR result payload** (also the return type of **`htr_engine.transcribe_pages`**). Importing it in **`transkribus_engine`** is a dependency on that **minimal dataclass module**, not on **`TranskribusAdapter`** or adapter wiring. This PR’s upload+recognition orchestration **constructs `HtrResult` at the engine boundary** for convenience; the adapter’s **`execute`** path still builds **`HtrResult`** from **`transcribe_existing_server_document`**’s tuple as before—**`needs_review = bool(review_reasons)`** matches **`TranskribusAdapter`** exactly for consistency.
- **Timeouts (behavior-preserving for PR #2 path):** **`transcribe_existing_server_document`** passes **`timeout_sec=DEFAULT_HTTP_TIMEOUT_SEC`** (60) into **`pylaia_transcribe_document_with_session`**, which forwards it to **`start_pylaia_recognition`**, **`poll_job_until_done`** (per-`get_job` request), **`fetch_pages_metadata`**, and **`fetch_transcript_xml`**. That matches the prior implicit default (**60s**) on each of those calls; **`login_trp_server`** is still invoked **without** an explicit timeout on the existing-document entrypoint (unchanged from pre-refactor). **`upload_then_transcribe_page_images_with_pylaia`** passes its **`timeout_sec`** through login, upload ingest, and the shared PyLaia helper for one consistent knob on that path only.
- **Production intent unchanged:** one VS-Archive document flow using this path should correspond to **one new** Transkribus document in **`TRANSKRIBUS_COLLECTION_ID`** (via upload), not appending pages to a fixed pre-existing **`docId`**.
- **Still engine-only:** no **adapter** wiring, no **env flags**, no **`OCR_ROUTES`** or **`run_worker.py`** edits, **no production routing**; **adapter** integration of **`upload_then_transcribe_page_images_with_pylaia`** remains **deferred**.

### Deferred

- **Retention/dedup** for Transkribus-side documents created by upload.

## Transkribus PR #5 — `TranskribusAdapter` dev upload mode (adapter wiring only)

### Decision

- **`TRANSKRIBUS_DEV_UPLOAD_MODE`** (default false): when true, **`TranskribusAdapter.execute`** calls **`upload_then_transcribe_page_images_with_pylaia`** with the **`PageImage[]`** from the caller (upload → new Trp **`docId`** in **`TRANSKRIBUS_COLLECTION_ID`** → PyLaia → **`HtrResult`**). Still **not** production routing: **`OCR_ROUTES`** and **`run_worker.py`** are unchanged; no new **`DocumentTextResult`** semantics.
- **`TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT`** path is **unchanged** (same env vars, same **`transcribe_existing_server_document`** call).
- The two flags are **mutually exclusive**. If both are true, **`TranskribusAdapter`** raises **`EnginePermanentError`** before any HTTP or engine call.
- **Default:** if **neither** flag is true, **`TranskribusAdapter`** still fails fast before HTTP (message names both env toggles).
- **Upload dev mode** requires **`TRANSKRIBUS_USERNAME`**, **`TRANSKRIBUS_PASSWORD`**, **`TRANSKRIBUS_API_TOKEN`**, **`TRANSKRIBUS_COLLECTION_ID`**, **`TRANSKRIBUS_MODEL_ID`** only; it does **not** require **`TRANSKRIBUS_DEV_EXISTING_DOCUMENT_ID`** or **`TRANSKRIBUS_DEV_EXISTING_PAGES`** (those remain for the separate existing-document dev path).
- **Retention / duplicate Transkribus documents** on retries and **production route selection** for Transkribus remain **deferred**.

## Transkribus — adapter dev upload smoke verification & PyLaia auth (recorded findings)

This section records **local smoke-test results against real Legacy TrpServer**. It does **not** mean Transkribus is production-enabled: **`OCR_ROUTES`** still do not select Transkribus for production documents, and **`run_worker.py`** was not changed for this path.

### End-to-end adapter dev upload mode (verified)

With **`TRANSKRIBUS_DEV_UPLOAD_MODE=true`** and a **`TranskribusAdapter.execute([PageImage], …)`** call against a real account, the following chain was observed to complete successfully:

**`PageImage[]` → `TranskribusAdapter` → upload new document into `TRANSKRIBUS_COLLECTION_ID` → `UploadImportJob` yields top-level `docId` → `GET …/pages` metadata (`pageNr`) → PyLaia recognition start → poll → transcript XML fetch → PAGE XML → plain text → `HtrResult`.**

Smoke output (technical check only): **`engine_name`** `transkribus-pylaia:564149`, **`needs_review`** false, **`review_reasons`** empty, **`text_length`** 6, short/low-quality text preview from a **synthetic** PNG. That outcome **validates wiring** (adapter → upload → recognition → transcript → parse → `HtrResult`); it is **not** archival OCR quality validation. A later smoke run on a **real local Hebrew image** likewise confirmed **technical** end-to-end execution only (same scope as below).

### PyLaia `POST /pylaia/{colId}/{modelId}/recognition` — media type and auth

**Working request shape on real Legacy TrpServer:**

- **Session:** logged-in **`login_trp_server`** session cookie (same flow as upload/create).
- **Headers:** `Accept: application/json, text/plain, */*` only.
- **No** `Authorization` header on this POST.
- **No** `Content-Type` and **no** request body (`json` / `data` omitted).

**Diagnostics (same account):**

- Sending **`Content-Type`** / a **body** on PyLaia start produced **HTTP 415**.
- Sending **`Authorization: Bearer`** (with or without an existing session cookie) produced **HTTP 401**.
- **Session cookie only** (no Bearer on this POST) returned **HTTP 200** and a **plain-text job id** in the response body.

**Auth split (unchanged intent in code):**

- **Upload / create-document / PyLaia start / Trp `GET` jobs & pages:** Legacy **session** after **`login_trp_server`**.
- **Transcript XML fetch** (`files.transkribus.eu` / transcript URLs): **Bearer** token, as already implemented in **`fetch_transcript_xml`**.

### Deferred validation: smoke scope vs integration fidelity

The successful adapter **dev upload-mode** smoke test(s) validate **technical connectivity and end-to-end execution only**. They do **not** yet validate **transcript fidelity** between what Transkribus shows in the **UI / native output** and VS-Archive’s **parsed `HtrResult`**. We have **not** yet checked whether the adapter/parser preserves **all lines**, **page order**, **transcript selection** (e.g. which `tsList` entry), **encoding**, or **exact text** as produced by Transkribus. This should be revisited **before production routing** or **before relying on Transkribus outputs at scale**.

This deferred work is **separate from OCR/HTR model quality**: we are **not** claiming to have evaluated **word-level correctness** of the Transkribus PyLaia model in the smoke step. The open gap is **integration fidelity**—whether VS-Archive **receives, parses, and (when persisted) reflects** what Transkribus actually produced for the chosen transcript path.

### Still deferred / not implied by the smoke test

- **No** production **`OCR_ROUTES`** entry selecting Transkribus; **no** production “Transkribus is the default engine” claim.
- **No** **`run_worker.py`** changes for this integration step.
- **No** cleanup / retention policy for Transkribus-side documents created by dev upload or retries.
- **No** DB persistence of Transkribus **`docId`** on the VS-Archive `Document` row.
- **No** quality evaluation on real archival handwriting; **no** layout / line-polygon policy decision for production documents beyond what the current PyLaia path already does.
