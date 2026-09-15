# Gemini Interactions / Antigravity local spike

Standalone investigation only. **No production OCR routing or `gemini_engine.py` changes.**

## May 2026 schema (current)

As of the [May 2026 breaking-change migration](https://ai.google.dev/gemini-api/docs/interactions-breaking-changes-may-2026):

- Responses use a **`steps`** array (not legacy **`outputs`**).
- Text is read from `model_output` steps: `steps[].content[].text`.
- REST calls should send **`Api-Revision: 2026-05-20`** (required before the default flip; harmless after).
- **`google-genai` ≥ 2.0.0** is required if you use the Python SDK for Interactions. Production OCR still uses **`generate_content`** on **`google-genai` 1.x** and is unaffected.

This spike uses **`requests`** + the REST schema so it works **without bumping `poetry.lock`**.

## What this verifies

| Check | API surface | Purpose |
|-------|-------------|---------|
| `model` | `POST /v1beta/interactions` with `model` | Fastest key + Interactions API smoke test |
| `antigravity` | Same endpoint with `agent=antigravity-preview-05-2026` | Managed Antigravity agent (remote sandbox) |
| `antigravity-image` | Antigravity + one image | Single-image OCR spike |
| `antigravity-images` | Antigravity + multiple images in one interaction | Multi-image OCR spike |

Default model for the fast check: **`gemini-2.5-flash-lite`** (cheap/fast).

Auth: **`GEMINI_API_KEY`** via `x-goog-api-key`.

## Prerequisites

- `app/backend` Poetry env (`requests`, `python-dotenv` already in `pyproject.toml`)
- `GEMINI_API_KEY` in the environment or `app/backend/.env` (never commit real keys)

## Text-only smoke test (recommended first)

```bash
cd app/backend
poetry run python ../../scripts/dev/gemini_interactions_smoke.py --env-file .env --check model
```

Antigravity text-only (slower; remote sandbox):

```bash
poetry run python ../../scripts/dev/gemini_interactions_smoke.py \
  --env-file .env --check antigravity --background
```

## Antigravity OCR spikes

OCR prompt rules (all image modes):

- OCR/transcription only — **no translation**
- Preserve Arabic, Hebrew, and Latin scripts
- Preserve names, dates, page numbers, document numbers, punctuation
- Include cover/catalog page text and occasional handwritten additions
- Prefer **`[UNCLEAR]`** over invented text
- Output one section per image with headings like **`[IMAGE 1: filename.png]`**

Always use **`--background`** for image OCR (Antigravity can take minutes).

### One image

```bash
poetry run python ../../scripts/dev/gemini_interactions_smoke.py \
  --env-file .env --check antigravity-image \
  --image /path/to/page1.png --background
```

### Repository-owned printed-Arabic fixture

A small synthetic, non-sensitive fixture is tracked at:

- image: `scripts/dev/fixtures/printed_arabic_smoke.png`
- ground truth: `scripts/dev/fixtures/printed_arabic_smoke_expected.txt`

It contains three clearly printed Arabic lines rendered right-to-left with
DejaVu Sans and Pillow RAQM. For the VS-Archive AWS environment, run from
`app/backend` with the key scoped to this command only:

```bash
GEMINI_API_KEY="$(aws secretsmanager get-secret-value --secret-id "vs-archive-dev/gemini_api_key" --region eu-central-1 --profile default --query SecretString --output text)" poetry run python ../../scripts/dev/gemini_interactions_smoke.py --check antigravity-image --image ../../scripts/dev/fixtures/printed_arabic_smoke.png --background --timeout-seconds 600 --output-file /tmp/antigravity-printed-arabic-smoke-output.txt
```

This performs one read-only Secrets Manager access and one real provider call.
Run it only deliberately. The key is scoped to the command and is not printed
or persisted. The smoke script requires non-empty OCR output; compare the
saved transcription with `printed_arabic_smoke_expected.txt` to validate the
printed-Arabic result.

The repository fixture was live-validated on 2026-08-27: the interaction
completed and all three ground-truth lines matched after whitespace
normalization (`missing_lines=[]`, `fixture_match=True`).

### Two images (one interaction)

```bash
poetry run python ../../scripts/dev/gemini_interactions_smoke.py \
  --env-file .env --check antigravity-images \
  --image /path/to/page1.png \
  --image /path/to/page2.png \
  --background
```

Write the full transcription to a file (terminal still shows a 500-char preview):

```bash
poetry run python ../../scripts/dev/gemini_interactions_smoke.py \
  --env-file .env --check antigravity-images \
  --image /path/to/page1.png \
  --image /path/to/page2.png \
  --background \
  --output-file /path/to/ocr-result.md
```

The output file contains extracted OCR text only — not API keys or raw JSON.

### Full directory (filename sort order)

Reads `*.png`, `*.jpg`, `*.jpeg`, `*.webp`, `*.gif`, `*.bmp`, `*.tif`, `*.tiff`, `*.heic`, `*.heif` from the directory, sorted by filename:

```bash
poetry run python ../../scripts/dev/gemini_interactions_smoke.py \
  --env-file .env --check antigravity-images \
  --image-dir /path/to/pages/ --background
```

Combine directory + extra CLI images (directory files first, then `--image` paths in order):

```bash
poetry run python ../../scripts/dev/gemini_interactions_smoke.py \
  --env-file .env --check antigravity-images \
  --image-dir /path/to/pages/ \
  --image /path/to/extra-cover.png \
  --background
```

### Output summary

Image OCR modes print a concise summary:

- `interaction_id`
- `status`
- `step_count`
- `images` (count and filenames)
- `output_preview` (first 500 characters of transcription)
- `output_file` path and byte count when `--output-file` is set

The script never prints the API key.

## Equivalent curl (text-only, new schema)

```bash
curl -sS -X POST "https://generativelanguage.googleapis.com/v1beta/interactions" \
  -H "Content-Type: application/json" \
  -H "x-goog-api-key: $GEMINI_API_KEY" \
  -H "Api-Revision: 2026-05-20" \
  -d '{
    "model": "gemini-2.5-flash-lite",
    "input": "Reply with exactly: interactions-api-ok"
  }'
```

## Expected outcomes / failures

- **`status: completed`** + `model_output` step with text — key works.
- **403 / PERMISSION_DENIED** — key may work for `generate_content` but not Interactions/Antigravity preview.
- **Long `in_progress`** — normal for `environment=remote`; use `--background`.

## References

- [Interactions API: Breaking changes (May 2026)](https://ai.google.dev/gemini-api/docs/interactions-breaking-changes-may-2026)
- [Gemini Interactions API](https://ai.google.dev/api/interactions-api)
- [Antigravity Agent](https://ai.google.dev/gemini-api/docs/antigravity-agent)
- In-repo OCR client: `app/backend/documents/services/gemini_engine.py`

## SDK v1 diagnostic preflight (google-genai ≥ 2.3)

Isolated script: `scripts/dev/gemini_interactions_sdk_preflight.py`.

Purpose: empirically record how the **Python SDK** exposes Interactions **v1** unary
`store=false` responses — especially `incomplete`, usage attributes, and inline
image request shape. Output is **diagnostic JSON**: types, status, documented
`code`/`reason`/`type`/`finish_reason`/`stop_reason` fields, usage numbers,
present/length for `output_text`, public attribute names, Pydantic
`model_fields` / `model_dump` keys, and nested object types/keys. It does not
print output-text previews, prompts, source, image bytes, or API keys directly. For
Python API exceptions, it may print the provider-supplied `body.error.code` and
`body.error.message` fields as diagnostic metadata; provider error text is free-form
and should be treated accordingly.

Returned Interactions include `deep_structure` for the Interaction and each
step. That exists to find a typed incomplete/reason discriminator that shallow
status/usage fields might miss.

This is **not** production OCR. It does **not** import `gemini_engine`, change
routing, checkpoints, prompts, models, or `app/backend` dependencies.

### Why a separate environment

`app/backend/pyproject.toml` still pins `google-genai (>=1.63.0,<2.0.0)`
(lock: **1.63.0**). Official Interactions Python support starts at
[`google-genai` 2.3.0](https://ai.google.dev/gemini-api/docs/interactions-overview).
`Client.interactions.create` is not available on 1.63.0.

The script accepts any installed `google-genai>=2.3.0` and always reports the
**exact installed version** in JSON (`sdk_version`).

**Do not bump `pyproject.toml` / `poetry.lock` for this spike.** Use a throwaway
venv pinned to the current PyPI 2.x release
[`google-genai==2.23.0`](https://pypi.org/project/google-genai/2.23.0/)
(released 2026-09-10; no documented Interactions v1 incompatibility vs 2.3+):

```bash
python3 -m venv /tmp/vs-archive-genai23
/tmp/vs-archive-genai23/bin/pip install 'google-genai==2.23.0' python-dotenv
```

### What it calls

- `genai.Client(api_key=..., http_options={"api_version": "v1"})`
- `client.interactions.create(..., store=False)` (non-streaming)
- Diagnostic model default: `gemini-2.5-flash-lite` (not a production route change)
- Inline image: `{"type": "image", "data": "<base64>", "mime_type": "..."}` with
  `resolution` unset

### Cases

| `--case` | What it sends | What to look for in JSON |
|---|---|---|
| `success` | Short text, no token cap | `delivery`, `status`, `output_text` present/length, `usage`, `deep_structure` |
| `incomplete` | Long prompt, `max_output_tokens=1` | `status=incomplete` vs exception; `deep_structure` on Interaction and steps |
| `ocr-shape` | Spike-only caption + repo fixture image | Inline image request form; success/error shape |
| `negative-control` | Harmless text only (alias: `blocked-probe`) | Negative control only. See below. |
| `recitation-probe` | Caller-supplied `--image`, `--prompt-file`, `--model`, optional generation-config flags | Structural delivery; `generation_config_keys` / `generation_config_values`. **Not** in default `all`. |
| `all` (default) | Built-in four (not `recitation-probe`) | Combined report |

**`negative-control` is not a generation-blocked test.** It is a benign
negative control. It never attempts to force recitation, safety, or other
documented generation-blocked codes. A successful response (or any
NOT-OBSERVED block) is **not evidence** of how those errors are delivered.

### Deep incomplete inspection

The `incomplete` case still uses a tiny `max_output_tokens` cap. Diagnostics now
walk the Interaction and every returned step for:

- Python type/module
- public field names
- `model_fields` / `model_dump` keys
- enum/scalar values only for `type`, `status`, `code`, `reason`,
  `finish_reason`, `stop_reason`
- numeric usage/token fields
- nested object types and keys

This is how to see whether `incomplete` carries a hidden/typed reason the
shallow `status` field does not show.

### Generic `recitation-probe`

Diagnostic only. **No document-specific logic**: no hardcoded document ids,
page numbers, coordinates, or production IDs. You must pass an image path,
a prompt file, and a model. There is no production-document default image.

```bash
/tmp/vs-archive-genai23/bin/python scripts/dev/gemini_interactions_sdk_preflight.py \
  --env-file app/backend/.env.local \
  --case recitation-probe \
  --image /path/to/image.png \
  --prompt-file /path/to/prompt.txt \
  --model gemini-2.5-flash \
  --temperature 0.0 \
  --top-p 0.95 \
  --top-k 40 \
  --max-output-tokens 4096
```

Supply only generation-config fields you intend to send. Each flag is included
in `generation_config` **only if passed**. Do not invent equivalents: there is
no `thinking_budget` flag, and `thinking-level` is **not** mapped from
production `thinking_budget=0`. If Interactions v1 / the SDK rejects a supplied
field (for example `top_k`), that rejection is captured structurally; the spike
does not drop it.

Prefer documented Interactions `generation_config` fields
([REST v1](https://ai.google.dev/api/interactions-api-v1),
[text generation](https://ai.google.dev/gemini-api/docs/text-generation)).
`top_k` is included so a production-shaped request can be compared; it may not
be a documented Interactions v1 sampling field.

The prompt file is sent as the text part and is never printed. Media
`resolution` is left unset. Same Interactions v1 / `store=False` / non-streaming
client as the other cases. JSON reports `generation_config_keys` and
`generation_config_values` (caller-supplied scalars only).

### Commands (do not run until explicitly asked)

From repo root, after the temporary venv exists:

```bash
/tmp/vs-archive-genai23/bin/python scripts/dev/gemini_interactions_sdk_preflight.py \
  --env-file app/backend/.env.local
```

One case:

```bash
/tmp/vs-archive-genai23/bin/python scripts/dev/gemini_interactions_sdk_preflight.py \
  --env-file app/backend/.env.local \
  --case incomplete
```

OCR-shape against the tracked fixture:

```bash
/tmp/vs-archive-genai23/bin/python scripts/dev/gemini_interactions_sdk_preflight.py \
  --env-file app/backend/.env.local \
  --case ocr-shape \
  --image scripts/dev/fixtures/printed_arabic_smoke.png
```

The current Poetry env is expected to **exit 2** with `spike_error_kind` /
`google_genai_below_2_3` (and still reports `sdk_version`):

```bash
cd app/backend && poetry run python ../../scripts/dev/gemini_interactions_sdk_preflight.py
```
