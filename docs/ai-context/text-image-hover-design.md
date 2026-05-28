# Text-to-Source Hover/Highlight Design (Future)

This note captures a future-facing design exploration for text-to-source alignment assistance during transcription review. It is documentation only and does not implement behavior.

## 1. Problem statement

Reviewers need to compare extracted/transcribed text against the original source image while editing and verifying transcription results.

With long handwritten text, it is difficult to locate the matching source line or word in the image quickly. This slows review and increases the risk of review mistakes.

## 2. User/product requirement

In transcription review, users should eventually have visual assistance that connects reviewed text and source image regions.

Potential interaction examples:

- Hover over a text line and highlight the corresponding source image line/region.
- Hover over a source image region and highlight the related text line.
- Click/select a text line and scroll/focus the source preview to the matching region.
- Support page-aware navigation for multi-page/multi-image documents.

This assistance should support human review/edit workflows, not replace reviewer judgment.

## 3. Why not implement directly now

Accurate hover/highlight requires reliable alignment data between text segments and source-image geometry.

The current system should not infer line positions by guessing from rendered text layout in the browser. A fake or approximate highlight may be worse than no highlight because it can mislead reviewers into approving incorrect text-image correspondences.

Before UI behavior is added, a persistence and mapping design is needed so alignment data has explicit provenance and predictable semantics.

## 4. Required data/model concepts for future implementation

Future implementation likely needs explicit concepts (not final schema decisions in this note):

- Page identity and stable page ordering (`page_index` or equivalent).
- Source image dimensions (width/height) per page.
- OCR/HTR output geometry by page.
- Line-level polygons or bounding boxes.
- Optional word-level boxes for finer interactions.
- Mapping relationships between `Document`, page identity, `DocumentTextResult`, and line/segment alignment.
- Engine source metadata references (for example PAGE XML or ALTO sources).
- Coordinate strategy (normalized coordinates vs raw pixel coordinates).
- Versioning/staleness markers when text is edited after OCR/HTR output generation.

## 5. Possible geometry sources

Potential geometry inputs to evaluate:

- Transkribus PAGE XML outputs.
- ALTO XML outputs where available.
- Engine-provided line polygons or line bounding boxes.
- Engine-provided word segmentation boxes (optional).
- Original page image dimensions used for coordinate normalization.

Current engine caveat:

- Gemini may not return robust line/word geometry in current usage; geometry availability can differ by model/endpoint/output mode.

This suggests a future engine-agnostic geometry abstraction that can represent rich geometry when available and explicitly represent missing geometry when unavailable.

## 6. Relationship to multi-image documents

This design is closely related to future multi-image/multi-page support documented in `docs/ai-context/multi-image-documents-design.md`.

Hover/highlight must be page-aware:

- A logical `Document` may contain multiple source pages/images.
- Text-to-image mapping should include which page a line belongs to.
- Page identity must remain stable enough for review navigation and alignment lookups.

This strengthens the case for explicit page identity modeling (for example `DocumentPage`/`DocumentFile` or an equivalent ordered page identity contract), while leaving final modeling choices open.

## 7. Possible implementation approaches

These options are exploratory and not final decisions.

### A. Store raw PAGE XML / ALTO and parse on demand

Pros:

- Preserves original engine output with high fidelity/provenance.
- Lower up-front schema complexity for normalized geometry tables.

Cons:

- Parsing cost moves to read-time and may affect UI latency.
- UI-facing queries become harder without precomputed normalized structures.

Risks:

- Inconsistent parser behavior across engines/versions can create subtle mismatches.
- Runtime parsing can become operationally expensive for large documents.

### B. Parse and persist normalized line/word geometry in local models

Pros:

- Faster and simpler UI queries for hover/highlight.
- Clear local contract for geometry independent of raw engine format.

Cons:

- Higher schema and migration complexity.
- Requires careful handling of engine differences and parser evolution.

Risks:

- Normalization bugs can permanently encode incorrect mappings.
- Storage footprint can grow significantly for word-level geometry.

### C. Hybrid: keep raw engine output plus selected normalized geometry

Pros:

- Balances provenance (raw) with UI performance (normalized subset).
- Supports re-derivation/reprocessing when normalization logic changes.

Cons:

- More moving parts and lifecycle rules to maintain.
- Requires clear ownership of "source of truth" at each layer.

Risks:

- Drift between raw payloads and normalized records if updates are not coordinated.
- More complex staleness/version management.

### D. UI-only approximate highlighting

Pros:

- Fastest path to visible interaction prototype.
- No immediate persistence changes.

Cons:

- Approximations can be inaccurate and misleading.
- Weak provenance/auditability for review-critical workflows.

Risks:

- Reviewer trust degradation if highlights are wrong.
- Potentially harmful review outcomes if approximation looks authoritative.

Given review-critical use cases, this should generally be avoided unless clearly marked as approximate and non-authoritative.

## 8. Areas affected by future implementation

Future implementation could touch:

- Transkribus adapter output handling (geometry/source metadata access paths).
- Gemini adapter limitations and future capability checks for geometry availability.
- Worker persistence boundaries for alignment/geometry metadata.
- Document/page modeling and page identity strategy.
- S3/source preview asset handling for page-aware navigation and dimensions.
- Review detail UI interactions (text hover, image hover, focus/scroll linking).
- Text editing workflow and staleness handling after edits.
- Reprocessing policy and alignment invalidation/version semantics.
- Automated tests for mapping correctness and UX behaviors.
- Accessibility and keyboard navigation for non-pointer users.
- Performance behavior for large page counts and dense geometry.

## 9. Recommended phased approach

This is a cautious proposal, not a committed implementation plan.

- Phase 0: design/documentation only (this note).
- Phase 1: inspect available PAGE XML / ALTO (especially Transkribus outputs) and document what geometry is actually available.
- Phase 2: define an engine-agnostic geometry representation and staleness semantics.
- Phase 3: persist page/line geometry for new OCR/HTR runs only.
- Phase 4: add read-only hover/highlight UI interactions.
- Phase 5: integrate with edit/review flows and handle post-edit staleness explicitly.

## 10. Stop lines / non-goals

For this PR and this design note:

- Do not implement hover/highlight behavior.
- Do not add migrations.
- Do not change worker behavior.
- Do not change Transkribus/Gemini adapters.
- Do not change review/edit/verify semantics.
- Do not change OCR routing behavior.
- Do not broaden Transkribus usage scope.
- Do not add fake/guessed geometry to the UI.
- Do not make final schema/UI/API/model/persistence decisions in this note.

## 11. Open questions

- Should raw PAGE XML / ALTO be stored permanently or retained with lifecycle limits?
- Should V1 geometry be line-level only, or include word-level geometry?
- How should edited text affect line-to-image mapping validity?
- What should happen when a reviewer edits line breaks/paragraph segmentation?
- Should geometry belong to `DocumentTextResult`, a page model, or a dedicated alignment model?
- How should legacy documents without geometry behave in the review UI?
- How should multi-image/multi-page documents map text results to specific pages?
- What is the minimum useful hover/highlight interaction for a safe V1?
