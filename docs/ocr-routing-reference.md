# OCR/HTR Routing Reference

Concise reference for current OCR/HTR route selection, engine dispatch, and related translation behavior. Source of truth for routing logic: `app/backend/documents/services/ocr_routing.py`.

**Supported inputs:** `Document.language` ∈ `{he, en, fr, ar}` and `Document.text_input_type` ∈ `{HANDWRITTEN, PRINTED, MIXED}`. Any other value raises `ValueError` from `select_ocr_route`. There is no default/fallback route for unknown pairs, and nothing silently routes as `MIXED`.

`MIXED` (PR E) is an explicit **manual document-level** choice: it does not classify individual pages, and every page of a `MIXED` document uses the single mixed printed/handwritten Gemini prompt contract (pages may be entirely printed, entirely handwritten, mixed within the same page, or a printed form filled in by hand).

## Routing matrix

| Language | Text input type | Engine | `prompt_variant` (routing metadata) |
|----------|-----------------|--------|-------------------------------------|
| Hebrew (`he`) | Handwritten | **Transkribus** (see gate below) | `handwritten` |
| Hebrew (`he`) | Printed | **Gemini** | `printed` |
| Hebrew (`he`) | Mixed | **Gemini** | `mixed` |
| English (`en`) | Handwritten | **Gemini** | `handwritten` |
| English (`en`) | Printed | **Gemini** | `printed` |
| English (`en`) | Mixed | **Gemini** | `mixed` |
| French (`fr`) | Handwritten | **Gemini** | `handwritten` |
| French (`fr`) | Printed | **Gemini** | `printed` |
| French (`fr`) | Mixed | **Gemini** | `mixed` |
| Arabic (`ar`) | Handwritten | **Gemini** | `handwritten` |
| Arabic (`ar`) | Printed | **Gemini** (default) or **Antigravity** (see gate below) | `printed` |
| Arabic (`ar`) | Mixed | **Gemini** | `mixed` |

`MIXED` routes go through the static `OCR_ROUTES` table only: Hebrew mixed does **not** pass the Transkribus Hebrew-handwritten gate, and Arabic mixed is **not** routed to Antigravity.

Static Gemini routes live in `OCR_ROUTES`. Hebrew handwritten and Arabic printed (when Antigravity is enabled) are **not** selected from that table alone; they are handled separately.

### Hebrew handwritten gate

- `ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN=true` → route to **Transkribus** (`HEBREW_HANDWRITTEN_TRANSKRIBUS_ROUTE`).
- `ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN=false` (default) → routing **fails explicitly**. **No Gemini fallback.**

There is **no** Gemini↔Transkribus fallback in code. `ENABLE_HYBRID_HTR` only validates Transkribus credentials in env validation; it does **not** change routing.

### Arabic printed gate (Antigravity)

Antigravity is a **real** Gemini Interactions managed-agent adapter (`AntigravityAdapter`, `antigravity_engine.py`). It is **not** broad production-default routing. Current approved routing scope is **Arabic printed only**, behind **`ENABLE_ANTIGRAVITY_ARABIC_PRINTED`**.

- `ENABLE_ANTIGRAVITY_ARABIC_PRINTED=false` (default, safe state) → `ar` + `PRINTED` uses the existing **Gemini** route from `OCR_ROUTES`.
- `ENABLE_ANTIGRAVITY_ARABIC_PRINTED=true` → route to **Antigravity** (`ARABIC_PRINTED_ANTIGRAVITY_ROUTE`).
- `ar` + `HANDWRITTEN` is **not** routed to Antigravity; it remains **Gemini**.

Route activation is env-gated in `select_ocr_route` via `_env_bool` (same pattern as Hebrew handwritten → Transkribus). `AntigravityAdapter` also checks `worker_env.enable_antigravity_arabic_printed` as a **second safety gate** before calling the Interactions API.

- `ANTIGRAVITY_AGENT_ID` defaults to `antigravity-preview-05-2026` (override via env / `WorkerEnvConfig`).
- Auth uses the existing **`GEMINI_API_KEY`** (Interactions API).

**Production rollout (two phases):**

1. Deploy code/migration with **`ENABLE_ANTIGRAVITY_ARABIC_PRINTED=false`** (or unset). Default behavior is unchanged.
2. Enable the flag only for a **controlled Arabic printed OCR test** once wiring and credentials are ready.

There is **no** Gemini↔Antigravity fallback in code.

**Future cleanup:** OCR routing feature gates may be centralized through `WorkerEnvConfig` passed into `select_ocr_route`, but any such change should migrate **all** env-gated OCR routes together (Hebrew handwritten, Arabic printed, etc.), not Arabic Antigravity alone.

## Dispatch flow

1. Worker calls `select_ocr_route(language, text_input_type)` once.
2. `transcribe_pages(..., route=...)` dispatches via `get_htr_adapter(route.engine_key)`.
3. Adapter receives `prompt_variant` from the route. Routing metadata is persisted on `DocumentTextResult` from the worker-held route, not from `HtrResult`.

Registered engines: `GEMINI` (`GeminiAdapter`), `TRANSKRIBUS` (`TranskribusAdapter`), `ANTIGRAVITY` (`AntigravityAdapter`).

## Gemini model selection

Resolved in `gemini_model_candidates()` (called from `htr_engine.transcribe_pages` when `worker_env` is present):

| Route context | Model candidate(s) |
|---------------|-------------------|
| Hebrew printed | Single model: `GEMINI_HEBREW_PRINTED_MODEL` env, default `gemini-3.1-flash-lite` |
| English handwritten | Ordered chain: `gemini-2.5-flash` → `gemini-3.1-flash-lite` |
| French handwritten | Single full-page model: `gemini-3.6-flash` |
| English/French printed | `gemini-2.5-flash` |
| Arabic (handwritten/printed) | Default chain: `gemini-2.5-flash` → `gemini-3.1-flash-lite` |
| Mixed (all languages) | Default chain: `gemini-2.5-flash` → `gemini-3.1-flash-lite` |
| Non-Gemini routes | Default chain (unused at runtime for Transkribus) |

`GeminiAdapter` tries candidates in order. Quota-style errors retain the
existing candidate fallback. In the durable checkpoint-backed worker path,
English handwritten `RECITATION` may also advance from `gemini-2.5-flash` to
`gemini-3.1-flash-lite`, within one shared maximum of three provider calls per
page. The current output cap and remaining budget are carried forward. French
handwriting instead uses one direct full-page `gemini-3.6-flash` candidate and
has no `RECITATION` candidate switch. Other permanent response classifications
do not advance. Exhaustion is persisted as a failed page checkpoint and
produces an explicit partial outcome. Legacy direct adapter calls without
document identity retain the prior `EngineRetryableError` behavior and do not
gain the scoped `RECITATION` fallback.

Other Gemini execution knobs (from `worker_env`, not routing):
`GEMINI_TEMPERATURE`, `GEMINI_TOP_K`, `GEMINI_TOP_P`,
`GEMINI_MAX_OUTPUT_TOKENS` (legacy/shared Hebrew translation cap; default 2048),
`GEMINI_OCR_MAX_OUTPUT_TOKENS` (worker OCR initial cap; default 4096),
`GEMINI_MAX_OUTPUT_TOKENS_HARD_CAP` (PR D bounded OCR recovery hard cap;
default 32768, max 65536, must be ≥ `GEMINI_OCR_MAX_OUTPUT_TOKENS`),
`GEMINI_DOUBLE_PASS`, `GEMINI_CONSISTENCY_MIN_RATIO`, and `MIN_TEXT_LENGTH`.

French handwritten `gemini-3.6-flash` is the explicit exception to the legacy
decoding knobs: OCR requests use `thinking_level=MINIMAL` and omit temperature,
top-k, and top-p so the provider model defaults apply. Other Gemini OCR and
Hebrew translation profiles are unchanged.

### Page checkpoint model provenance

For worker OCR, Gemini candidates are applied per missing page.
Each successful `GeminiOcrPageCheckpoint` stores the actual model that produced
that page. A later intentional OCR execution with the same source/route/prompt/
configuration identity reuses that success instead of restarting it.

- If every page used the same model, `DocumentTextResult.engine` remains that
  concrete model id.
- If quota or scoped English handwritten `RECITATION` fallback caused
  different pages to use different Gemini models, `DocumentTextResult.engine`
  is the deterministic runtime marker
  `gemini-mixed:<fingerprint>`. Full page-to-model provenance remains on the
  checkpoints.
- French handwritten successes record `gemini-3.6-flash` directly.

This provenance behavior does not alter route selection. It records quota
fallback, the scoped English handwritten `RECITATION` fallback, and the direct
French model truthfully without discarding successful pages.

## Gemini prompt resolution

Routing stores `handwritten` or `printed`. Actual prompt text is chosen in `gemini_engine.transcribe_pages_with_gemini`:

| `prompt_variant` | Language hint | Prompt used | Output mode |
|------------------|---------------|-------------|-------------|
| `printed` | `en` / `fr` | Latin printed prompt | Plain text (`v1beta`, temperature 0) |
| `handwritten` | `en` / `fr` | Latin handwritten prompt | Plain text |
| `printed` | `he` (canonical hint only) | Hebrew printed plain-text prompt (`_HEBREW_PRINTED_PROMPT`) | Plain text (`v1beta`, temperature 0) |
| `printed` | other non-Latin (e.g. `ar`) or missing hint | Hebrew-oriented printed prompt (`_PRINTED_TEXT_PROMPT`) | JSON |
| `handwritten` | non-Latin (e.g. `he`, `ar`) | Latin handwritten prompt body | JSON (not plain-text Latin path) |
| `hebrew_general_handwritten` | any | General Hebrew handwritten prompt (`_HEBREW_GENERAL_HANDWRITTEN_PROMPT`) | Plain text (`v1beta`, temperature 0) |
| `mixed` | any | Approved mixed printed/handwritten prompt (`_MIXED_CONTENT_PROMPT`, closed product contract — do not edit) | Plain text (`v1beta`, temperature 0), never JSON |

Hebrew printed moved from the JSON response contract to plain text in PR C.
That route alone uses the route-specific contract version
`GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION` =
`gemini-hebrew-printed-prompt-v2`. The mixed route (PR E) uses its own
route-specific contract version `GEMINI_MIXED_PROMPT_CONTRACT_VERSION` =
`gemini-mixed-content-prompt-v1`, selected for any language hint. General
Hebrew handwritten uses the shared `GEMINI_OCR_PROMPT_CONTRACT_VERSION` =
`gemini-ocr-prompt-v1`. PR #371 temporarily introduced
`gemini-hebrew-general-handwritten-prompt-v2`, but that prompt-only experiment
failed live on document 291 and was rolled back; its attempts/checkpoints are
incident history, not an active contract. Only the canonical `he` hint selects
the plain-text Hebrew printed prompt; other non-Latin or missing hints are
**not** treated as Hebrew and keep the JSON contract. Uncertainty metadata
(`has_unclear`, `unclear_count`, `needs_review`) is derived from the `[?]` /
`[UNCLEAR]` markers in the returned transcription for all plain-text routes,
including mixed and general Hebrew handwritten. See the decision-log entries
“Gemini Hebrew printed plain-text OCR response contract (PR C)”, “Explicit
MIXED printed/handwritten Gemini OCR route (PR E)”, and “General Hebrew
handwritten prompt anti-runaway experiment (PR #371)”.

`hebrew_translation` is **not** an OCR route variant. It is used only for Hebrew translation rows (below).

Transkribus ignores Gemini prompts; `prompt_variant=handwritten` on Transkribus routes is observability metadata only.

## Transkribus execution (not route selection)

After routing to Transkribus, the adapter requires exactly one dev/ops mode:

- `TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT=true` — PyLaia on a fixed server document.
- `TRANSKRIBUS_DEV_UPLOAD_MODE=true` — upload pages, then PyLaia.

If neither is enabled, the adapter fails before HTTP. Additional execution controls (not routing): `TRANSKRIBUS_FORCE_REPROCESS`, `TRANSKRIBUS_RECOGNITION_ONLY_RETRY`, plus credential/collection/model env vars.

## Antigravity execution (not route selection)

After routing to Antigravity, the adapter requires `worker_env` and `worker_env.enable_antigravity_arabic_printed=true`. It calls the Gemini Interactions API with `agent_id` from `ANTIGRAVITY_AGENT_ID` (default `antigravity-preview-05-2026`). Antigravity ignores Gemini OCR prompt variants; `prompt_variant=printed` on Antigravity routes is observability metadata only.

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
- Keep **routing metadata** (`engine_key`, `prompt_variant`) separate from
  **runtime engine identity** (`DocumentTextResult.engine`, e.g. a concrete
  Gemini model id, deterministic `gemini-mixed:<fingerprint>`, or
  `transkribus-pylaia:{model_id}`).
- Do **not** add silent provider fallbacks.
- Update **this document** whenever routing, model overrides, or translation policy changes.
