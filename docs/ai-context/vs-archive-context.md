# VS-Archive AI Context

VS-Archive is a Django backend project for managing historical family documents.

## Main domain

- Documents may be uploaded as IMAGE or PDF.
- Documents have metadata (`language`, `text_input_type`, etc.).
- OCR/HTR extracts text into `DocumentTextResult` rows.
- Translation to Hebrew is planned; not fully implemented for non-Hebrew documents.
- Text results are stored separately from the document.
- Admin review/verification matters (`verification_status` on results).

## Current implementation (OCR/HTR)

**Gemini** is the **production/default** engine: static `OCR_ROUTES` in `documents/services/ocr_routing.py` are all `GEMINI`.

**Transkribus** is **implemented** (Legacy TrpServer / PyLaia), but **not** broad production-default:

- Real adapter + engine: `TranskribusAdapter`, `transkribus_engine.py`.
- Dev/staging only unless production routing is explicitly approved later.
- Worker routing to Transkribus: `language=he` + `HANDWRITTEN` when `TRANSKRIBUS_DEV_OCR_ROUTE` + `TRANSKRIBUS_DEV_UPLOAD_MODE` are set (see decision log).
- Manual smoke: `python manage.py dev_transkribus_transcribe <file> --confirm-create-transkribus-doc`.

**Not implemented:** Gemini→Transkribus fallback, hybrid OCR routing, production-default Transkribus in `OCR_ROUTES`, persisted Transkribus `docId` on `Document`, cleanup automation.

## Routing layer (done)

Engine selection is implemented as a small routing layer (`select_ocr_route` → `transcribe_pages` → adapters), not as logic inside views or a provider-specific worker.

## Status semantics (important)

- `DocumentTextResult.status=SUCCEEDED` = technical pipeline success, **not** human quality approval.
- `verification_status` = human review path (`UNVERIFIED` until verified).
- Transkribus outputs should **default to `NEEDS_REVIEW`** as a **near-term intended policy**; **code does not enforce this yet** (follow-up behavior PR).

## Hebrew result types (current behavior)

For Hebrew documents, the worker persists **both** `SOURCE_TEXT` and `HEBREW_TEXT`; processing-state rollup for `READY` looks at **`HEBREW_TEXT` only**. See `decision-log.md` and `.cursor/rules/architecture.mdc`.

## Non-Hebrew `PARTIAL`

Non-Hebrew documents may remain **`PARTIAL`** because `HEBREW_TEXT` (translation) is not implemented — **intentional**, not an OCR failure.

## Where to read more

- `docs/ai-context/decision-log.md` — durable decisions, Transkribus PR history, operational boundaries.
- `.cursor/rules/architecture.mdc` — layer boundaries and contracts for code changes.

## Near-term roadmap (documentation sequence)

1. Docs/rules sync (this alignment).
2. Transkribus default `NEEDS_REVIEW` behavior (if approved).
3. Transkribus remote identity / `docId` persistence design.
4. Reprocess / duplicate-prevention policy.
5. Cleanup / retention runbook (no automation before identity + policy).
6. Broader Transkribus production routing only with explicit approval.

OCR quality/fidelity validation against the Transkribus UI is important but **not** the current docs/rules task.
