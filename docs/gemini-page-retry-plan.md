# Gemini OCR bounded per-page retry — PR D

> **Merged as PR #369.** Durable checkpoint/resume remains PR B
> (`docs/ai-context/gemini-page-checkpoint-design.md`). Current layer boundaries:
> `.cursor/rules/architecture.mdc` and `docs/ai-context/decision-log.md` (PR D
> entry).

## Problem

Gemini OCR can fail a page on transient empty output, parse failure, rate-limit
blips, or truncation (`MAX_TOKENS`). PR B preserves successful pages durably;
PR D adds bounded recovery **within the same delivery** for one page without
restarting successful checkpoints. A later follow-up adds one scoped,
budget-preserving candidate switch for English/French handwritten
`RECITATION`.

## Attempt budget scope

- **Engine boundary:** `transcribe_pages_with_gemini` receives a validated
  call window whose offset plus size cannot exceed three calls.
- **Scoped candidate-chain boundary:** English/French handwritten
  checkpoint-backed OCR shares at most **three provider calls per page across**
  `gemini-2.5-flash` and `gemini-3.1-flash-lite`.
- A `RECITATION` switch carries the current output cap and remaining call budget
  to the next candidate.
- Outside that scoped route, the pre-existing quota-only candidate fallback may
  still start a separate three-call budget on candidate *N+1*.
- Quota/rate-limit retries inside an engine call window count toward that
  window.
- Hebrew translation (`translate_text_to_hebrew_with_gemini`) is **unchanged**
  and retains its existing two-attempt path.

## Classifications

**Classify finish/block/candidate/empty before parsing.** Never call JSON parsing
after a classified response failure.

**Retryable within the three-call budget:**

- `EMPTY_RESPONSE` — deterministic backoff: **1s** before attempt 2, **2s**
  before attempt 3.
- `JSON_PARSE` — immediate retry, no token escalation, no repaired/incomplete
  JSON acceptance.
- `MAX_TOKENS` — immediate retry with token-cap ladder (no sleep).
- Transient quota/rate-limit API errors (existing classification; not
  `LIMIT: 0`).

**Permanent for the active model:**

- PR A finish/block codes remain non-retryable on the same model.
- `RECITATION` may advance only English/French handwritten
  checkpoint-backed OCR to its next configured candidate when global budget
  remains.
- `SAFETY`, `LANGUAGE`, `SPII`, prohibited/blocked content,
  `NO_CANDIDATES`, `OTHER`, and `JSON_SCHEMA` do not trigger candidate
  fallback.

## Token-cap ladder (`MAX_TOKENS`)

Deterministic escalation per page/candidate:

1. If current cap is `None` or below **8192**, next cap is **8192**.
2. Otherwise double the current cap.
3. Clamp to `GEMINI_MAX_OUTPUT_TOKENS_HARD_CAP`.
4. If the cap cannot increase, fail immediately with typed `MAX_TOKENS` — do
   not repeat an identical call.

Defaults and validation (`env_validation.py`):

- OCR-specific `GEMINI_OCR_MAX_OUTPUT_TOKENS` default: **4096**. This is a
  ceiling, not a target output length; shorter responses are not padded.
- Legacy/shared `GEMINI_MAX_OUTPUT_TOKENS` remains **2048** for Hebrew
  translation and does not control worker OCR.
- With the default OCR cap, the three-call `MAX_TOKENS` sequence is
  **4096 → 8192 → 16384**.
- Default hard cap: **32768**.
- Maximum allowed configured hard cap: **65536**.
- Reject booleans, non-integers, non-positive values, values below
  `GEMINI_OCR_MAX_OUTPUT_TOKENS`, and values above 65536.

## Checkpoint identity consequence

PR D retry policy and hard cap affect provider execution. They are hashed into
the Gemini attempt **configuration fingerprint**:

- `retry_policy_version` = `gemini-ocr-page-retry-v2`
- `max_provider_calls_per_page` = `3`
- `max_output_tokens_hard_cap`

The v2 marker records the bounded model-switch policy. These fields sit in
the config fingerprint **independently of the prompt-contract version**, so
the version bump is an intentional new identity boundary for **every Gemini
OCR route** — including PR C Hebrew printed, which stays on
`gemini-hebrew-printed-prompt-v2`; PR D adds a new config-identity boundary on
top of that route-specific prompt version. Source, route, prompt, page,
fencing, lease, persistence, and assembly semantics are unchanged. No
page-level `DocumentTextResult` rows are created.

## Privacy boundary

Unchanged from PR A/B/C: logs, raised errors, and persisted failure metadata
carry operational fields only (model, page, attempt, finish/block reason,
lengths, token counts, output cap, exception class). Never log or persist
prompts, raw provider responses, document text, or provider exception text.

## Rejected alternatives

- Infinite or unbounded retries.
- JSON repair/heuristics for truncated JSON responses.
- Continuation prompts or page splitting for `MAX_TOKENS`.
- Cross-provider or worker-level retry redesign.
- Changing OCR routing, prompts, translation, Transkribus, worker/SQS/DLQ, UI,
  or schema.

## Tests (synthetic fixtures only)

Focused modules:
`app/backend/documents/test_gemini_bounded_page_recovery.py` and
`app/backend/documents/test_gemini_recitation_model_fallback.py`.
Production documents **272** and **273** provide the live evidence for the
scoped model-switch follow-up; provider output is not embedded in tests.
Documents **271, 277, 289, 291** remain modeled PR D failure references.
Document **293** belongs to PR C and is not reimplemented here.

## Validation evidence

Focused PR D validation: the initial focused run executed **79** tests. One
test failed only because its expected final-attempt output-length metadata still
described the former second-attempt fixture; the test fixture/assertions were
corrected **without production-code changes**; the corrected test passed in
isolation; the new quota traceback-privacy test also passed in isolation. Because
the test-only correction followed the initial run, the focused evidence is
complementary rather than one uninterrupted 79/79 OK run.

Static validation: Ruff formatting was applied to the two affected test files;
the final format check reported all **10** scoped files formatted. Ruff lint
passed. Django system check passed. Migration check reported no changes. Scoped
Pyright: 0 errors and 0 warnings. Scoped mypy: no issues in **7** files.
`git diff --check` passed.

Full `documents` regression: one uninterrupted suite ran **2,286** tests in
**1,886.264** seconds; result **OK**; `TEST_EXIT_CODE=0`.

## 2026-08-03 RECITATION fallback follow-up validation

The scoped follow-up passed Ruff format/check, Django system and migration
checks, scoped Pyright and mypy, a 108-test expanded Gemini/checkpoint/routing
suite, and the full `documents` regression: **2,330 tests** in **1,777.543
seconds**, result **OK**, `FULL_TEST_STATUS=0`. `git diff --check` also passed.

## Validation still required

Live Gemini provider/fidelity validation; deployment through the existing
worker-first runbook; production-document retries. **PR D is not merged yet.**
