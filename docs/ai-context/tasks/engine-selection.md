# Task: OCR/HTR Engine Selection

> **Task doc / historical.** The routing selector and adapter dispatch described here are **implemented**. For **current** engine state, routing policy, and Transkribus gates, read **`docs/ai-context/decision-log.md`** (“Current state — OCR/HTR and Transkribus”) and **`.cursor/rules/architecture.mdc`**. Do not treat this file as the sole source of truth.

## Original goal

Route OCR/HTR processing based on:

- source language
- handwritten vs printed

## Original constraints (task era)

- First inspect whether these metadata fields already exist.
- Do not change models or create migrations unless explicitly approved.
- If only one OCR/HTR engine exists today, keep behavior compatible and isolate routing logic for future engines.
- Do not implement new external integrations in this task.
- Add selector logic in a dedicated service/function.
- Keep worker/view code simple.

## What was built (summary)

- `documents/services/ocr_routing.py` — `OCR_ROUTES`, `select_ocr_route`
- `documents/services/htr_engine.py` — `transcribe_pages`
- Adapter registry + Gemini and Transkribus adapters
- Second engine (Transkribus) added in follow-up PRs with **dev/staging env gates**, not static production routing

## Superseded assumptions

- “Only one engine” / “do not add external integrations in this task” — **superseded** by Transkribus integration PRs; production default remains Gemini unless explicitly changed.
