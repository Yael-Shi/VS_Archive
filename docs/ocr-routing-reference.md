# OCR/HTR Routing Reference

Concise reference for current OCR/HTR route selection, engine dispatch, and related translation behavior. Source of truth for routing logic: `app/backend/documents/services/ocr_routing.py`.

**Supported inputs:** `Document.language` ∈ `{he, en, fr, ar}` and `Document.text_input_type` ∈ `{HANDWRITTEN, PRINTED}`. Any other value raises `ValueError` from `select_ocr_route`. There is no default/fallback route for unknown pairs.

## Routing matrix

| Language | Text input type | Engine | `prompt_variant` (routing metadata) |
|----------|-----------------|--------|-------------------------------------|
| Hebrew (`he`) | Handwritten | **Transkribus** (see gate below) | `handwritten` |
| Hebrew (`he`) | Printed | **Gemini** | `printed` |
| English (`en`) | Handwritten | **Gemini** | `handwritten` |
| English (`en`) | Printed | **Gemini** | `printed` |
| French (`fr`) | Handwritten | **Gemini** | `handwritten` |
| French (`fr`) | Printed | **Gemini** | `printed` |
| Arabic (`ar`) | Handwritten | **Gemini** | `handwritten` |
| Arabic (`ar`) | Printed | **Gemini** | `printed` |

Static Gemini routes live in `OCR_ROUTES`. Hebrew handwritten is **not** in that table; it is handled separately.

### Hebrew handwritten gate

- `ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN=true` → route to **Transkribus** (`HEBREW_HANDWRITTEN_TRANSKRIBUS_ROUTE`).
- `ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN=false` (default) → routing **fails explicitly**. **No Gemini fallback.**

There is **no** Gemini↔Transkribus fallback in code. `ENABLE_HYBRID_HTR` only validates Transkribus credentials in env validation; it does **not** change routing.

## Dispatch flow

1. Worker calls `select_ocr_route(language, text_input_type)` once.
2. `transcribe_pages(..., route=...)` dispatches via `get_htr_adapter(route.engine_key)`.
3. Adapter receives `prompt_variant` from the route. Routing metadata is persisted on `DocumentTextResult` from the worker-held route, not from `HtrResult`.

Registered engines: `GEMINI` (`GeminiAdapter`), `TRANSKRIBUS` (`TranskribusAdapter`).

## Gemini model selection

Resolved in `gemini_model_candidates()` (called from `htr_engine.transcribe_pages` when `worker_env` is present):

| Route context | Model candidate(s) |
|---------------|-------------------|
| Hebrew printed | Single model: `GEMINI_HEBREW_PRINTED_MODEL` env, default `gemini-3.1-flash-lite` |
| English/French handwritten | `gemini-2.5-flash` |
| English/French printed | `gemini-2.5-flash` |
| Arabic (both types) | Default chain: `gemini-2.5-flash` → `gemini-3.1-flash-lite` |
| Non-Gemini routes | Default chain (unused at runtime for Transkribus) |

`GeminiAdapter` tries candidates in order; on quota-style errors it advances to the next candidate. If all fail, raises `EngineRetryableError`.

Other Gemini execution knobs (from `worker_env`, not routing): `GEMINI_TEMPERATURE`, `GEMINI_TOP_K`, `GEMINI_TOP_P`, `GEMINI_MAX_OUTPUT_TOKENS`, `GEMINI_DOUBLE_PASS`, `GEMINI_CONSISTENCY_MIN_RATIO`, `MIN_TEXT_LENGTH`.

## Gemini prompt resolution

Routing stores `handwritten` or `printed`. Actual prompt text is chosen in `gemini_engine.transcribe_pages_with_gemini`:

| `prompt_variant` | Language hint | Prompt used | Output mode |
|------------------|---------------|-------------|-------------|
| `printed` | `en` / `fr` | Latin printed prompt | Plain text (`v1beta`, temperature 0) |
| `handwritten` | `en` / `fr` | Latin handwritten prompt | Plain text |
| `printed` | `he` (and other non-Latin, e.g. `ar`) | Hebrew-oriented printed prompt (`_PRINTED_TEXT_PROMPT`) | JSON |
| `handwritten` | non-Latin (e.g. `he`, `ar`) | Latin handwritten prompt body | JSON (not plain-text Latin path) |

`hebrew_translation` is **not** an OCR route variant. It is used only for Hebrew translation rows (below).

Transkribus ignores Gemini prompts; `prompt_variant=handwritten` on Transkribus routes is observability metadata only.

## Transkribus execution (not route selection)

After routing to Transkribus, the adapter requires exactly one dev/ops mode:

- `TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT=true` — PyLaia on a fixed server document.
- `TRANSKRIBUS_DEV_UPLOAD_MODE=true` — upload pages, then PyLaia.

If neither is enabled, the adapter fails before HTTP. Additional execution controls (not routing): `TRANSKRIBUS_FORCE_REPROCESS`, `TRANSKRIBUS_RECOGNITION_ONLY_RETRY`, plus credential/collection/model env vars.

## Translation behavior (high level)

Separate from OCR route selection.

| Document language | After successful OCR/HTR | `HEBREW_TEXT` source |
|-------------------|--------------------------|---------------------|
| Hebrew (`he`) | Persists same transcript to `SOURCE_TEXT` and `HEBREW_TEXT` | OCR/HTR output (no separate translation step) |
| Non-Hebrew (`en`, `fr`, `ar`) | Persists OCR to `SOURCE_TEXT`; then calls `translate_text_to_hebrew_with_gemini` | Gemini translation (`prompt_variant=hebrew_translation`, model `gemini-2.5-flash`) |

**Processing-state rollup** (`expected_outputs.expected_result_types_for_document`):

- Hebrew documents: only `HEBREW_TEXT` required for `READY`.
- Non-Hebrew documents: both `SOURCE_TEXT` and `HEBREW_TEXT` expected. Missing translation keeps the document `PARTIAL` (intentional until translation succeeds).

Translation failure persists a failed `HEBREW_TEXT` row (`HEBREW_TRANSLATION_FAILED`); it does not fail the OCR row. Manual re-translation is available via `hebrew_translation_retry` (separate worker operation).

**Out of scope / not implemented:** no automatic non-Hebrew→other-language translation; no OCR-route-driven translation; Hebrew OCR does not run a second translation pass.

## Operational notes

- Do **not** change routing casually. New `(language, text_input_type)` pairs require explicit `OCR_ROUTES` (or approved special-case) entries, adapter/registry wiring, tests, and a decision-log update.
- Keep **routing metadata** (`engine_key`, `prompt_variant`) separate from **runtime engine identity** (`DocumentTextResult.engine`, e.g. concrete Gemini model id or `transkribus-pylaia:{model_id}`).
- Do **not** add silent provider fallbacks.
- Update **this document** whenever routing, model overrides, or translation policy changes.
