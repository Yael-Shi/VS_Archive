# Gemini Interactions / Antigravity local spike

Standalone investigation only. **No production OCR routing or `gemini_engine.py` changes.**

## May 2026 schema (current)

As of the [May 2026 breaking-change migration](https://ai.google.dev/gemini-api/docs/interactions-breaking-changes-may-2026):

- Responses use a **`steps`** array (not legacy **`outputs`**).
- Text is read from `model_output` steps: `steps[].content[].text`.
- REST calls should send **`Api-Revision: 2026-05-20`** (required before the default flip; harmless after).
- **`google-genai` ≥ 2.0.0** is required if you use the Python SDK for Interactions. Production OCR still uses **`generate_content`** on **`google-genai` 1.x** and is unaffected.

This spike uses **`requests`** + the REST schema so it works **without bumping `poetry.lock`**. When Interactions is integrated into production later, plan a separate **`google-genai` ≥ 2.0.0** upgrade (Interactions-only breaking change; `generate_content` unchanged per [release notes](https://github.com/googleapis/python-genai/releases/tag/v2.0.0)).

## What this verifies

| Check | API surface | Purpose |
|-------|-------------|---------|
| `model` | `POST /v1beta/interactions` with `model` | Fastest key + Interactions API smoke test |
| `antigravity` | Same endpoint with `agent=antigravity-preview-05-2026` | Managed Antigravity agent (remote sandbox) |
| `antigravity-image` | Antigravity + multimodal `input` | Local image → inline base64 OCR spike |

Default model for the fast check: **`gemini-2.5-flash-lite`** (cheap/fast).

Auth: **`GEMINI_API_KEY`** via `x-goog-api-key`.

Production OCR today: **`client.models.generate_content`** in `documents/services/gemini_engine.py` — separate endpoint and contract.

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

Antigravity + local image OCR spike:

```bash
poetry run python ../../scripts/dev/gemini_interactions_smoke.py \
  --env-file .env --check antigravity-image \
  --image /path/to/page.png --background
```

The script prints interaction `id`, `status`, and a short text preview from **`steps`** — **never the API key**.

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

Response shape (read text from `steps`, not `outputs`):

```json
{
  "id": "int_123",
  "status": "completed",
  "steps": [
    {
      "type": "model_output",
      "content": [{ "type": "text", "text": "interactions-api-ok" }]
    }
  ]
}
```

## Equivalent curl (Antigravity text-only)

```bash
curl -sS -X POST "https://generativelanguage.googleapis.com/v1beta/interactions" \
  -H "Content-Type: application/json" \
  -H "x-goog-api-key: $GEMINI_API_KEY" \
  -H "Api-Revision: 2026-05-20" \
  -d '{
    "agent": "antigravity-preview-05-2026",
    "input": "Reply with exactly: antigravity-ok. Do not browse the web or run code.",
    "environment": "remote"
  }'
```

For long-running agent work, add `"background": true` and poll `GET /v1beta/interactions/{id}` with the same headers.

## Local image files for OCR (Antigravity path)

Antigravity multimodal input supports **`text` + `image`**. Images are inline base64 in `input`:

```json
{
  "agent": "antigravity-preview-05-2026",
  "input": [
    {"type": "text", "text": "Transcribe all visible text faithfully."},
    {
      "type": "image",
      "mime_type": "image/png",
      "data": "<BASE64_BYTES>"
    }
  ],
  "environment": "remote"
}
```

**Not the same as production today:** `gemini_engine.transcribe_pages_with_gemini` uses `types.Part.from_bytes(...)` on **`generate_content`**. A future Interactions integration would be a new adapter path.

### Alternative: Interactions with `model` (non-agent)

```json
{
  "model": "gemini-2.5-flash-lite",
  "input": [
    {"type": "text", "text": "Transcribe this document page."},
    {"type": "image", "mime_type": "image/png", "data": "<BASE64_BYTES>"}
  ]
}
```

## Expected outcomes / failures

- **`status: completed`** + `model_output` step with text — key works.
- Error mentioning legacy schema / upgrade to **`google-genai` ≥ 2.0.0** — you are on SDK 1.x calling Interactions; use this REST spike or upgrade the SDK for Interactions-only code paths.
- **403 / PERMISSION_DENIED** — key may work for `generate_content` but not Interactions/Antigravity preview.
- **Long `in_progress`** — normal for `environment=remote`; use `--background`.

## References

- [Interactions API: Breaking changes (May 2026)](https://ai.google.dev/gemini-api/docs/interactions-breaking-changes-may-2026)
- [Gemini Interactions API](https://ai.google.dev/api/interactions-api)
- [Antigravity Agent](https://ai.google.dev/gemini-api/docs/antigravity-agent)
- In-repo OCR client: `app/backend/documents/services/gemini_engine.py`
