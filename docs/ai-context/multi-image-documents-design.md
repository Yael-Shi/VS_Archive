# Multi-Image Documents Design (Future)

This note captures a future-facing design exploration for supporting logical documents composed of multiple ordered image files. It is documentation only and does not implement behavior.

## 1. Problem statement

Some archival materials are not a single source file. One logical document may be composed of multiple image files, for example:

- Document A = image 1 + image 2 + image 3

The system should eventually support ingesting multiple source images as one logical `Document`, while preserving page order from upload through processing and review.

## 2. User/product requirement

- During upload, user/admin can select multiple image files for one logical `Document`.
- UI preserves and displays page order clearly.
- Catalog/search/review still treat this as one logical document record.
- Source preview and OCR/HTR review should remain usable when a document has multiple ordered pages.

## 3. Current-state assumptions and constraints

- Current upload and storage flow is primarily single-original-file oriented.
- Current models should be verified before proposing schema changes as implementation.
- Original source files remain canonical archive inputs.
- OCR/HTR/translation rows remain derived outputs.
- Current review/status semantics remain unchanged:
  - Automatic OCR/HTR success is persisted as `NEEDS_REVIEW` + `UNVERIFIED`.
  - `READY` means expected usable outputs exist, not human-verified ground truth.
- Current `SOURCE_TEXT` / `HEBREW_TEXT` behavior is not changed by this design note.
- Hebrew handwritten routing remains Transkribus-only under existing gate; no routing expansion is proposed here.

## 4. Possible modeling directions

These are options for future implementation discussion, not final decisions.

### A. Package images into one PDF

Idea: accept multiple uploaded images, assemble them into one ordered PDF (or equivalent internal single bundle), then run existing single-file-oriented processing on that packaged artifact.

Pros:

- Minimal short-term disruption to current worker and extraction contracts.
- Reuses existing single-input processing assumptions and tooling.
- Faster path for basic multi-image ingestion support.

Cons:

- Original per-image identity may be obscured unless explicitly preserved.
- Later page-level features (per-page provenance, page-level reprocess, coordinates) may be harder.
- Packaging step adds failure modes and ordering correctness requirements.

Risks:

- Ordering bugs during packaging could silently corrupt page sequence.
- Differences between original images and packaged representation may complicate debugging and review.

### B. Add `DocumentPage` / `DocumentFile` model

Idea: represent each source page/file as a first-class ordered record linked to one logical `Document` (for example, `DocumentPage` or `DocumentFile` with `page_index`, source key, and metadata).

Pros:

- Preserves canonical per-image provenance and order explicitly.
- Cleaner foundation for page-aware OCR/HTR, review, and future coordinate mapping.
- Better support for partial failures, page-level retries, and operational visibility.

Cons:

- Larger schema and API surface change.
- Requires broader updates across upload, worker input preparation, and review UI.
- More migration and backward-compatibility planning needed.

Risks:

- Broader change scope may increase delivery time and integration complexity.
- If introduced without clear lifecycle rules, page/document state semantics may drift.

### C. Hybrid phased approach

Idea: short-term packaging to unblock multi-image upload experience, then evolve toward explicit page model when page-aware workflows justify it.

Pros:

- Practical incremental path with lower near-term risk.
- Allows product validation before committing to larger schema/API changes.
- Reduces pressure to finalize all page-level contracts upfront.

Cons:

- Transitional architecture can create temporary duplication.
- Requires careful compatibility plan when moving from packaged input to page model.

Risks:

- Temporary approach could become sticky and delay foundational page modeling.
- Data migration and traceability between phases can become costly if not designed early.

## 5. Areas affected by future implementation

Future multi-image implementation likely touches:

- Upload API and presigned URL flow (single vs multi-file ingest contract)
- S3 key strategy (document-level and page-level object organization)
- Page ordering capture, persistence, and editability
- Database model shape and lifecycle rules
- Worker page extraction/input normalization behavior
- OCR/HTR adapter inputs (single bundle vs explicit pages)
- Transkribus upload mode and remote page handling/mapping
- Gemini processing input strategy for ordered pages
- Source preview UI (ordered page navigation)
- Transcription review UI (page-aware review context)
- Future hover/highlight/coordinate mapping model
- Reprocessing and cleanup/retention implications
- Test strategy across upload, processing, persistence, and UI

## 6. Recommended phased approach

This is a cautious proposal, not a committed plan.

- Phase 0 (this PR): design/documentation only.
- Phase 1: evaluate a minimal multi-image ingest path via ordered packaging (for example, generated PDF or internal ordered page bundle), if feasible.
- Phase 2: evaluate introducing explicit `DocumentPage` / `DocumentFile` model when page-level operations become necessary.
- Phase 3: add page-aware review UX and coordinate/hover workflows once page identity and geometry persistence contracts are explicit.

Cross-phase guidance:

- Prefer explicit order guarantees end-to-end.
- Keep original source files canonical and traceable.
- Avoid changing review/status semantics as part of initial ingest work.

## 7. Stop lines / non-goals

For this PR and this note:

- Do not implement multi-image behavior.
- Do not add migrations.
- Do not change upload API contracts.
- Do not change worker/routing/adapter behavior.
- Do not implement hover/highlight or coordinate persistence.
- Do not broaden Transkribus routing scope.
- Do not change review/status semantics.

## 8. Open questions

- Should V1 use ordered PDF packaging, or introduce `DocumentPage` directly?
- How should page order be edited after upload, and by whom?
- Should each page have independent source preview + OCR text mapping?
- How should partial page failures influence `Document.processing_state_user`?
- How should Transkribus remote page ids map back to local page identity/order?
- How does this align with future PAGE XML / ALTO / coordinate persistence?
- What is the migration path for existing single-file documents if a page model is introduced?
