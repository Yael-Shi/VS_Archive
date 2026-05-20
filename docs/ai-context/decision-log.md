# VS-Archive Decision Log

## Current state — OCR/HTR and Transkribus (read this first)

**Last aligned:** OCR/HTR review lifecycle behavior + docs/rules sync (automatic success → `NEEDS_REVIEW`, rollup semantics).

### Routing (implemented)

- Static **`OCR_ROUTES`** is **Gemini-only** for all `(language, text_input_type)` pairs unless explicitly changed later.
- **Dev/staging Transkribus override** in `select_ocr_route`: `language=he` + `HANDWRITTEN` → `engine_key=TRANSKRIBUS` only when **`TRANSKRIBUS_DEV_OCR_ROUTE=true`**, **`TRANSKRIBUS_DEV_UPLOAD_MODE=true`**, and **`TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT=false`**. Misconfiguration raises `ValueError` (no silent fallback to Gemini on that pair).
- All other valid pairs → **Gemini** from `OCR_ROUTES`.
- **No** Gemini→Transkribus fallback. **No** hybrid OCR routing. **`ENABLE_HYBRID_HTR`** only gates credential validation in env loading, not engine selection.

### Transkribus (implemented, gated)

- **Real** Legacy TrpServer / PyLaia: upload ingest, existing-server-document dev mode, adapter + `transkribus_engine.py`, registry, `OcrEngineKey.TRANSKRIBUS`.
- **Not** production-default. Technical smoke proves **wiring**, not archival OCR quality or UI fidelity.
- Upload mode creates a **new** Trp document per run; VS-Archive does **not** persist Trp **`docId`** yet.

### OCR review lifecycle (implemented)

**Automatic success** (`run_worker._save_htr_results`):

- **`DocumentTextResult.status=NEEDS_REVIEW`** for all successful automatic OCR/HTR persistence (Gemini, Transkribus, any worker success path).
- **`verification_status=UNVERIFIED`** on that path.
- **`NEEDS_REVIEW`** = usable/displayable text requiring human review before ground-truth use; **not** a technical failure (distinct from **`FAILED`**).
- **`FAILED`** remains for pipeline/dispatch/routing failures.

**Human ground truth:** **`verification_status=VERIFIED`** is the human-approved layer. **`SUCCEEDED`** remains a valid enum for future trusted/manual paths; **current automatic worker persistence normally uses `NEEDS_REVIEW`**.

**Review reasons (worker):**

- **`AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW`** — policy-level on every automatic success.
- **`NEEDS_REVIEW_FLAG`** — only when **`HtrResult.needs_review=True`** (not the generic policy reason).
- **`MIN_TEXT_LENGTH`**, **`HAS_UNCLEAR`**, engine-provided reasons — content/engine signals; deduplicated with order preserved.

**`Document.processing_state_user` rollup:**

- **Usable/displayable** row: **`status`** in **`SUCCEEDED`** or **`NEEDS_REVIEW`**, non-empty stripped **`text`**.
- **`READY`** = all expected outputs exist and are usable — **not** human-verified. Valid: parent **`READY`** with child rows **`NEEDS_REVIEW`** + **`UNVERIFIED`**.
- **`PARTIAL`** = missing, incomplete, failed, or unusable expected outputs — **not** merely review pending. **`NEEDS_REVIEW` alone does not force `PARTIAL`** when expected usable rows exist.
- **`FAILED`** when all expected rows exist and all are **`FAILED`**.

### Hebrew `DocumentTextResult` types (current accepted behavior)

- **Worker** (`_save_htr_results`): Hebrew documents persist **both** `SOURCE_TEXT` and `HEBREW_TEXT` (same text/status per run; status normally **`NEEDS_REVIEW`** on automatic success).
- **Rollup** (`expected_outputs`): Hebrew **`READY`** requires **`HEBREW_TEXT` only** (usable per rollup rules); `SOURCE_TEXT` is not part of that expectation list.
- **Open decision (future):** whether to keep both rows long-term — document only; no behavior change in unrelated work.

### Non-Hebrew `PARTIAL` (intentional)

- Worker persists **`SOURCE_TEXT` only**; `expected_outputs` expects **`SOURCE_TEXT` + `HEBREW_TEXT`**. Missing translation → **`PARTIAL`**, not OCR failure. Do not fix opportunistically.

### Blockers before broader Transkribus use

Decide (then implement in focused PRs): persist Trp **`docId`** / remote identity; reprocess and duplicate prevention; cleanup/retention runbook. **No** production `OCR_ROUTES` expansion until decided or explicitly deferred.

### Near-term PR sequence

1. Trp identity persistence design + migration.
2. Reprocess / duplicate policy.
3. Cleanup runbook (automation later).
4. Broader production routing only if explicitly approved.

---

## OCR/HTR routing by language and text input type

> **Historical / partially superseded.** Core routing **decisions** below remain valid. Facts that are **obsolete** are marked inline; for **current** Transkribus and execution-layer state, see **“Current state — OCR/HTR and Transkribus”** above and Transkribus PR sections below.

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

4. **Provider adapters** (e.g. `documents/services/htr_adapters/gemini_adapter.py`, `transkribus_adapter.py`): own provider-specific execution. **Superseded:** “Transkribus is not implemented yet” — Transkribus is implemented behind dev/staging gates; static `OCR_ROUTES` remain Gemini-only unless changed.

### Route metadata vs OCR result payload
`engine_key` and `prompt_variant` are **routing metadata**. They are selected by the routing layer and carried through the worker for persistence on `DocumentTextResult`. They are **not** part of the minimal OCR result payload (`HtrResult`: text, review flags, runtime engine name, etc.).

### DocumentTextResult fields
- **`engine`**: continues to mean the runtime processing identity used for uniqueness and processing-state rollups (e.g. concrete Gemini model id, or failure-path markers such as `ocr-dispatch` / `unsupported:<key>`). Do not repurpose this field for provider routing keys.

- **`engine_key` / `prompt_variant`**: stored on `DocumentTextResult` for auditability and reproducibility. Values come from the **selected route** on success (worker-held `OcrRouteConfig`) and from route re-selection or explicit unresolved markers on failure paths.

### DocumentTextResult.OcrEngineKey schema limitation
**Superseded:** `OcrEngineKey` now includes **`TRANSKRIBUS`** (migration landed in Transkribus PR #1). Historical note: first engine addition required enum + migration.

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

### Non-Hebrew Hebrew translation — intentional `PARTIAL` (current behavior)
**Current behavior (accepted):**

- `run_worker._save_htr_results` persists **`SOURCE_TEXT` only** for non-Hebrew documents.
- `expected_outputs.expected_result_types_for_document` expects **`SOURCE_TEXT` + `HEBREW_TEXT`**.
- Until real Hebrew translation exists, documents stay **`PARTIAL`** — **not** an OCR failure.

Do not fix this opportunistically unless explicitly requested (translation feature).

## Transkribus integration — PR #1 (skeleton / stable connection point)

> **Historical.** PR #1 landed enum + registry + fail-fast adapter. Later PRs added live TrpServer integration, dev upload mode, and env-gated `select_ocr_route`. See **“Current state — OCR/HTR and Transkribus”** at the top of this file.

### Decision (historical)

The first Transkribus PR establishes only the **plumbing** so a second engine can exist in the same architecture as Gemini, **without** changing production routing or calling Transkribus.

### Behavior after PR #1 only (superseded)

- `DocumentTextResult.OcrEngineKey` includes **`TRANSKRIBUS`** (with migration updating the field choices).
- `TranskribusAdapter` registered with `engine_key = "TRANSKRIBUS"`.
- At PR #1: adapter raised **`EnginePermanentError`** (“not implemented yet”). **Superseded:** adapter now runs real HTTP when dev env gates are on.
- At PR #1: `OCR_ROUTES` all **GEMINI**. **Still true** for static table; dev routing override added later (env-gated, not static table entries).

### Deferred (still in force unless explicitly requested)

- Static production `OCR_ROUTES` entries for Transkribus (separate approval).
- Hybrid or fallback between engines.

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

## Transkribus PR — dev-only dispatch smoke command (`dev_transkribus_transcribe`)

### Decision

- Add **`documents/management/commands/dev_transkribus_transcribe.py`**: a **dev/staging-only** Django management command that reads a **local** image or PDF path, runs **`extract_pages`** (same helper as the worker), builds an **explicit** **`OcrRouteConfig`** with **`engine_key=TRANSKRIBUS`** and a **`prompt_variant`** from CLI (default **handwritten**), loads **`WorkerEnvConfig`** via **`validate_required_env()`**, and calls **`htr_engine.transcribe_pages(..., route=…, worker_env=cfg)`** so the normal **dispatcher + registry + adapter** path runs **without** going through **`select_ocr_route`** or **`OCR_ROUTES`**.
- **Safety guard:** the command refuses to run unless **`--confirm-create-transkribus-doc`** is passed, with a clear message that it **creates a real Transkribus document** and **does not clean up**.
- **Env:** requires **`TRANSKRIBUS_DEV_UPLOAD_MODE=true`** and the same **upload-mode** credential/collection/model vars as **`TranskribusAdapter`** dev upload mode (**username/password**, **API token**, **collection id**, **model id**). **`GEMINI_API_KEY`** and other worker vars still load via **`validate_required_env`** (unchanged global worker env contract).
- **Automated tests** mock **`transcribe_pages`** / env loading at the command boundary; **no live Transkribus** calls in CI.

### Explicit non-scope

- **No** changes to **`OCR_ROUTES`**, **`run_worker.py`**, **`htr_engine.py`** behavior, or production routing.
- **No** Gemini→Transkribus fallback, hybrid routing, **DB schema**, or persistence of Transkribus **`docId`**.
- **No** cleanup/retention for server-side documents created when the command is run manually against real TrpServer.

## Transkribus — dev/staging env-gated OCR routing (`select_ocr_route`)

### Decision

- **`documents/services/ocr_routing.py`** may return **`engine_key=TRANSKRIBUS`** only when **all** of the following hold (read from **`os.environ`** in **`select_ocr_route`**, so **`run_worker.py`** and **`htr_engine.py`** stay unchanged):
  - **`TRANSKRIBUS_DEV_OCR_ROUTE=true`** (new flag; default **false** / unset → behavior identical to pre-change routing).
  - **`TRANSKRIBUS_DEV_UPLOAD_MODE=true`** so routing matches the **TranskribusAdapter** dev **upload** path.
  - **`TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT=false`** — existing-server-document dev mode is **not** supported for this routing override; if the existing-doc flag is true while dev OCR routing is enabled, **`select_ocr_route`** raises **`ValueError`** with an explicit message.
  - Document metadata is **`language=he`** and **`text_input_type=HANDWRITTEN`** only (narrow allowlist). Any other valid pair still returns the **normal Gemini** route from **`OCR_ROUTES`**.
- If **`TRANSKRIBUS_DEV_OCR_ROUTE=true`** but **`TRANSKRIBUS_DEV_UPLOAD_MODE`** is not true, **`select_ocr_route`** raises **`ValueError`** (clear configuration message) for **`he` + HANDWRITTEN** — no silent fallback to Gemini on that pair.
- **`OCR_ROUTES`** remains **static and Gemini-only**; production default with all dev flags unset is unchanged (**Gemini-only**).
- **No** Gemini→Transkribus fallback, **no** hybrid routing, **no** **`run_worker.py`** edits, **no** **`TranskribusAdapter`** changes, **no** models/migrations for this step.

This routing gate still does not add cleanup/retention for Transkribus-side documents; retries may create duplicate Trp documents and cleanup remains deferred.

### Operational note

Enabling **`TRANSKRIBUS_DEV_OCR_ROUTE`** on a worker that consumes real **SQS** jobs will route **Hebrew handwritten** documents through Transkribus when upload mode is on. Treat as **dev/staging-only** until an explicit production routing decision.

## Transkribus — manual dev/staging SQS worker smoke (verified)

### Record (manual confirmation)

A **full real** dev/staging path was exercised end-to-end (no mocks): **SQS** → **`run_worker`** → **S3** download → **`extract_pages`** → **`select_ocr_route`** (env-gated **TRANSKRIBUS**) → **`transcribe_pages`** → **TranskribusAdapter** dev **upload** mode → **Legacy TrpServer** → **`DocumentTextResult`** persistence.

**Document (example run):**

- **`id=9`**
- **`title`:** `ניסיון נוסף מהסלולרי`
- **`upload_status=UPLOADED`**
- **`language=he`**
- **`text_input_type=HANDWRITTEN`**
- **`mime_type=image/jpeg`**
- **`file_s3_key=documents/9/original.jpeg`**

**Environment:**

- **`TRANSKRIBUS_DEV_OCR_ROUTE=true`**
- **`TRANSKRIBUS_DEV_UPLOAD_MODE=true`**
- **`TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT`** unset / **false**
- **Dev** SQS queue and **dev** S3 object (object existed at **`file_s3_key`**)

**SQS queue attributes:**

- **Before worker:** **`ApproximateNumberOfMessages=1`**, **`ApproximateNumberOfMessagesNotVisible=0`**
- **Worker command:** `poetry run python manage.py run_worker --once --max-messages 1 --wait-seconds 20`
- **After worker:** **`ApproximateNumberOfMessages=0`**, **`ApproximateNumberOfMessagesNotVisible=0`**

**Persistence:**

- **`Document.processing_state_user=READY`**
- **`DocumentTextResult`** rows for **`SOURCE_TEXT`** and **`HEBREW_TEXT`**
- For **both** rows: **`engine=transkribus-pylaia:564149`**, **`engine_key=TRANSKRIBUS`**, **`prompt_variant=handwritten`**, **`status=SUCCEEDED`**, **`verification_status=UNVERIFIED`**, **`error_code` None**, **`text` length 1205**

  > **Historical smoke snapshot.** Observed **`status=SUCCEEDED`** at the time of this run. **Current worker policy** persists automatic OCR/HTR success as **`NEEDS_REVIEW`** (see “Current state — OCR review lifecycle” above).

### Meaning

This run **validates wiring and operability** of the **dev/staging** worker pipeline with **real** SQS, S3, DB, and **Legacy TrpServer** for the gated **Hebrew handwritten** route. It is **not** a claim about production readiness, default routing, or OCR quality.

### Limitations (unchanged policy)

- Still **dev/staging gated** only (**env flags**); **no** production-default Transkribus routing.
- **`OCR_ROUTES`** static table remains **Gemini-only**; production behavior with flags **off** is unchanged.
- **No** cleanup/retention for Transkribus-side documents; retries may create **duplicate** Trp documents.
- **No** Transkribus **`docId`** persisted on the VS-Archive **`Document`**.
- **No** transcript **quality** or **fidelity** validation is implied by this smoke.
- **No** Gemini→Transkribus **hybrid** or **fallback** is implemented.

## Transkribus — operational safety (dev/staging routing)

This section records **operational risks and semantics** for env-gated **Transkribus** routing and **upload** mode. It does **not** change product behavior; it informs **broader dev/staging use** and future schema/tooling work.

### Server-side documents and VS-Archive state

1. **Every** Transkribus **upload-mode** run that reaches Legacy TrpServer **creates a new server-side Transkribus document** (new **`docId`** on the Trp side for that upload path).

2. VS-Archive **does not** currently persist the Transkribus **`docId`** anywhere in the database.

3. **Reprocessing** the same VS-Archive **`Document`** (e.g. another **`PROCESS_DOCUMENT`** message for the same **`document_id`**) can therefore create **additional** Transkribus documents—**duplicates on Trp** are possible even when VS-Archive still represents “one” archive document.

### How VS-Archive rows interact with Trp duplicates

4. **`DocumentTextResult`** persistence uses **`update_or_create`** keyed by **`(document, result_type, engine)`** (see model **`UniqueConstraint`** / worker **`_save_htr_results`**). **`engine`** is the **runtime** identity (e.g. **`transkribus-pylaia:{model_id}`**). So:
   - If a **second** run produces the **same** **`engine`** string, the **same** result row(s) may be **updated in place** (new text, same key)—while Trp may still have received a **new** upload document on the second run.
   - If **`engine`** differs between runs (e.g. different model id in **`engine_name`**), **additional** rows can appear for the same **`result_type`** under different **`engine`** values.

### Cleanup and retention

5. **Cleanup / retention** for Transkribus-side documents is **not implemented** in the product. **Manual** deletion or archival in the **Transkribus UI** (or future external scripts) remains the **only** supported option today.

### Where not to stash `docId` (until an approved schema PR)

6. Do **not** store Transkribus **`docId`** in **`DocumentTextResult.error_details`** or **`review_reasons`**—those fields have **failure** / **review-reason** semantics and are a poor fit for external identifiers.

   If/when we **persist** **`docId`**, prefer **either**:
   - **Explicit nullable field(s) on `Document`**, **or**
   - **A dedicated link / history model** (one row per Trp document / run),

   **Both require a separate, explicitly approved schema / migration PR** (out of scope for doc-only updates).

### Status and verification semantics

7. **`verification_status=UNVERIFIED`** means **human verification** in VS-Archive has **not** been completed. A successful dev smoke **does not** imply **OCR quality**, **transcript fidelity**, or agreement with the Transkribus UI.

   **Current worker policy:** automatic OCR/HTR success persists **`status=NEEDS_REVIEW`** (not a technical failure). **`SUCCEEDED`** remains valid in the schema; older smoke notes that record **`SUCCEEDED`** are **historical snapshots** of behavior at run time.

### Decisions still required before broader use

8. Before expanding dev/staging volume or moving toward production routing, we still need explicit decisions on:
   - **Whether** (and **where**) to **persist Transkribus `docId`** for audit, dedupe, and cleanup.
   - **Whether** to **allow reprocessing** the same **`Document`** through Transkribus upload mode (and under what guards, e.g. **`VERIFIED`** results, file changed, explicit admin action).
   - **Whether** to add **cleanup tooling** (e.g. management command calling Trp APIs) once **`docId`** is stored.
   - **Automatic OCR review lifecycle:** implemented in worker — see **“OCR review lifecycle (implemented)”** in Current state above (worker-wide **`NEEDS_REVIEW`**, not Transkribus-only).

**`docId` persistence / reprocess / cleanup automation** remain undecided and unimplemented.
