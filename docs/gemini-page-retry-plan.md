# Gemini OCR bounded per-page retry — PR D

> **Implemented and validated** on branch `feat/gemini-bounded-page-recovery-pr-d`
> (**not yet merged**). Durable checkpoint/resume remains PR B
> (`docs/ai-context/gemini-page-checkpoint-design.md`). Current layer boundaries:
> `.cursor/rules/architecture.mdc` and `docs/ai-context/decision-log.md` (PR D
> entry).

## Problem

Gemini OCR can fail a page on transient empty output, parse failure, rate-limit
blips, or truncation (`MAX_TOKENS`). PR B preserves successful pages durably;
PR D adds bounded recovery **within the same delivery** for one page against
one model candidate without restarting successful checkpoints.

## Attempt budget scope

- **Boundary:** `transcribe_pages_with_gemini` per-page loop in
  `gemini_engine.py`.
- **Budget:** at most **three provider calls** per page **per model candidate**.
- **Outer boundary unchanged:** ordered model-candidate fallback in
  `GeminiAdapter` remains quota-only; exhausting quota on candidate *N* may
  continue on candidate *N+1* with a **separate** three-call budget.
- Quota/rate-limit retries inside the engine loop count toward the same
  three-call budget for that candidate.
- Hebrew translation (`translate_text_to_hebrew_with_gemini`) is **unchanged**
  (still uses its existing two-attempt path).

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

**Permanent (no retry):**

- PR A permanent finish/block codes: `SAFETY`, `RECITATION`, `LANGUAGE`,
  `SPII`, prohibited/blocked content, `NO_CANDIDATES`, `OTHER`, etc.
- `JSON_SCHEMA` — non-retryable schema rejection.

## Token-cap ladder (`MAX_TOKENS`)

Deterministic escalation per page/candidate:

1. If current cap is `None` or below **8192**, next cap is **8192**.
2. Otherwise double the current cap.
3. Clamp to `GEMINI_MAX_OUTPUT_TOKENS_HARD_CAP`.
4. If the cap cannot increase, fail immediately with typed `MAX_TOKENS` — do
   not repeat an identical call.

Defaults and validation (`env_validation.py`):

- Default hard cap: **32768**.
- Maximum allowed configured hard cap: **65536**.
- Reject booleans, non-integers, non-positive values, values below
  `GEMINI_MAX_OUTPUT_TOKENS`, and values above 65536.

## Checkpoint identity consequence

PR D retry policy and hard cap affect provider execution. They are hashed into
the Gemini attempt **configuration fingerprint**:

- `retry_policy_version` = `gemini-ocr-page-retry-v1`
- `max_provider_calls_per_page` = `3`
- `max_output_tokens_hard_cap`

These fields sit in the config fingerprint **independently of the
prompt-contract version**, so this is an intentional identity change for
**every Gemini OCR route** — including PR C Hebrew printed, which stays on
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

Focused module: `app/backend/documents/test_gemini_bounded_page_recovery.py`.
Production reference documents **271, 277, 289, 291** are cited only as modeled
failure classes. Document **293** belongs to PR C and is not reimplemented here.

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

## Validation still required

Live Gemini provider/fidelity validation; deployment through the existing
worker-first runbook; production-document retries. **PR D is not merged yet.**
