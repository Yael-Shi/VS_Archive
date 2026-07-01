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

### Two images (one interaction)

```bash
poetry run python ../../scripts/dev/gemini_interactions_smoke.py \
  --env-file .env --check antigravity-images \
  --image /path/to/page1.png \
  --image /path/to/page2.png \
  --background
```

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
