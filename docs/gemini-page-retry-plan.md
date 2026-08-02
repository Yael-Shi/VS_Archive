# Gemini OCR bounded per-page retry — PR D planning note

> **Planning only.** This note describes PR D retry policy, not PR B
> persistence. Durable checkpoint/resume is implemented separately and
> documented in `docs/ai-context/gemini-page-checkpoint-design.md`. For current
> Gemini behavior and layer boundaries, see `.cursor/rules/architecture.mdc`
> and `docs/ai-context/decision-log.md`.

## Problem

Gemini OCR can fail a page when it returns an empty or otherwise transient
response (e.g. empty body, parse/format failure, rate-limit blip). PR B
preserves already-successful pages durably and a later intentional request
processes only failed/missing pages. It deliberately retains the current
within-delivery attempt policy.

PR D will define a **bounded per-page retry** so a transient page failure may
recover during the same delivery without an operator-triggered request.

## Constraints

- Do **not** silently accept empty Gemini output.
- Do **not** restart already successful durable checkpoints.
- Preserve **page order** in the combined transcript.
- Avoid **overlapping retry loops** across engine, adapter, and worker layers — one boundary only.
- Preserve existing behavior for:
  - model routing and fallback (within Gemini only)
  - prompt variant selection
  - generation config (temperature, top_k, top_p, max_output_tokens)
  - PR B attempt/page persistence and final `DocumentTextResult`
  - review reasons and routing metadata
  - logging shape and error classification (`GeminiError`, quota markers, etc.)

## Proposed investigation

1. Trace the current call chain:
   - `run_worker.py` → `transcribe_pages` → `GeminiAdapter` → `transcribe_pages_with_gemini` (`gemini_engine.py`)
2. Map existing retry/error boundaries:
   - per-page loop and `_is_retryable_gemini_response_error` in `gemini_engine.py`
   - quota / 429 handling in the same module
   - adapter-level model fallback in `gemini_adapter.py`
   - worker dispatch and `_save_ocr_failure` in `run_worker.py`
3. **Choose exactly one retry boundary** (likely `gemini_engine.py` per-page loop, unless investigation shows adapter fallback must own it).
4. Classify failures:
   - **Retryable:** empty response, transient API/429 (non-zero quota), retryable format/parse errors already flagged today.
   - **Permanent:** quota exhausted (`LIMIT: 0`), auth/config errors, non-retryable API errors, MAX_TOKENS after existing token retry.

## Proposed retry policy

- Small explicit **max attempts** per page (e.g. 2–3 total, including first attempt).
- **Bounded backoff** between attempts (fixed or short exponential; reuse existing sleep patterns where present).
- **Page-level logging** on each retry and final failure:
  - `page_index`
  - `model` (runtime model id)
  - `attempt` / `max_attempts`
  - error message or classification
- On exhaustion: raise the same class of error the layer uses today so worker persistence and classification stay unchanged.
- Successful pages: retain durable checkpoints; only the failing page re-enters
  the retry loop.

## Test plan

Add or extend tests in the Gemini engine/adapter test modules (exact file TBD during investigation):

1. First attempt fails transiently (empty response or retryable parse error); second attempt succeeds — document completes with all pages in order.
2. All attempts fail — persist the page failure and return an explicit
   `GEMINI_PAGES_INCOMPLETE` partial outcome; no partial silent success.
3. Permanent error (e.g. quota exhausted) — **not** retried beyond current policy.
4. Page 1 succeeds, page 2 fails then retries — page 1 is **not** reprocessed (mock/call-count assertion).
5. Logs or raised errors include **page index** and **attempt count** on retry and final failure.

## Non-goals

- No prompt changes.
- No model changes or new model fallback rules.
- No generation config changes.
- No CI, env, or dependency changes.
- No worker-level or cross-provider retry redesign.
- No change to Transkribus or non-Gemini routes.
- No checkpoint schema or attempt-identity redesign.
