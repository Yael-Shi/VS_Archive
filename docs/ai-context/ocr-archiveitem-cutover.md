# OCR_DOCUMENT Shared-Field Source-of-Truth Cutover

Design document for moving the six duplicated shared archival fields on **`OCR_DOCUMENT`** items toward **`ArchiveItem`** as the canonical source of truth, while **`Document`** remains the OCR/runtime/processing source of truth.

**Status:** Design approved (PR5a). **Implementation deferred** to PR5b+.

**Related docs:**

- `docs/ai-context/decision-log.md` — durable decisions and PR history
- `docs/ai-context/vs-archive-context.md` — broad project context

**Key code references (current behavior):**

- `documents/services/archive_items.py` — `ARCHIVE_ITEM_SHARED_FIELD_NAMES`, `create_ocr_document`, `update_ocr_document_metadata`, `sync_document_shared_fields_from_archive_item`, `sync_archive_item_shared_fields_from_document`
- `documents/services/archive_item_access.py` — visibility access control
- `documents/services/document_access.py` — document view access via `archive_item.visibility`
- `documents/views.py` — OCR edit, document list/API, metadata backlog
- `documents/migrations/0020_archiveitem_foundation.py` — ArchiveItem backfill

---

## 1. Purpose and scope

### Purpose

Define how VS-Archive should complete the **ArchiveItem-first** direction for **`OCR_DOCUMENT`** shared archival metadata, eliminating split-brain reads/writes between **`Document`** and **`ArchiveItem`** without disturbing OCR processing.

### In scope

- Source-of-truth model for six shared fields: **`title`**, **`visibility`**, **`metadata_status`**, **`date_start`**, **`date_end`**, **`date_precision`**
- Read-path and write-path cutover strategy
- Data reconciliation before cutover
- Sync and Django Admin policy during/after transition
- Small, reviewable implementation PR sequence (PR5b+)

### Out of scope (this design and PR5a)

- OCR/HTR processing, routing, Gemini/Transkribus
- Upload API contract or behavior changes
- Worker / SQS behavior
- **`DocumentTextResult`** review, status, or verification semantics
- Migration of **`DocumentMetadata`**, **`category_event`**, or **`tags_m2m`** to **`ArchiveItem`**
- **`PHOTO`** item implementation
- Full-text search
- Family/private user management or permissions redesign
- Infra, settings, or deployment changes
- Dropping duplicated **`Document`** shared columns (deferred to optional later schema PR)

---

## 2. Current state

### ArchiveItem-first direction

**`ArchiveItem`** is the long-term central archival content entity. **`OCR_DOCUMENT`** rows link via **`Document.archive_item`** (`OneToOneField`). **`MANUAL_TEXT`** already uses **`ArchiveItem`** + **`ManualTextContent`** as runtime source of truth with no **`Document`** row.

### Document remains OCR runtime source of truth (bridge)

During the bridge phase, **`Document`** remains canonical for OCR-specific and processing fields: **`doc_type`**, **`language`**, **`text_input_type`**, upload/processing state, file storage, **`DocumentSourceFile`**, **`DocumentTextResult`**, and related runtime artifacts.

### Edit-time sync (PR5d — ArchiveItem canonical)

On successful staff OCR metadata edit at **`/archive/manage/<archive_item_id>/edit/`**, **`update_ocr_document_metadata`** saves shared fields on linked **`ArchiveItem`** first, then mirrors them to **`Document`** via **`sync_document_shared_fields_from_archive_item`**. Catalog scalar metadata and tags save on **`Document`** only (no **`ArchiveItem`** mirror). The combined save is wrapped in **`transaction.atomic()`**.

OCR edit form GET seed reads shared fields from **`ArchiveItem`** (via **`shared_archive_item_for_document`**).

### ArchiveItem.visibility is already access source of truth

**`can_view_archive_item`** and **`filter_documents_for_user`** gate document list/detail/API access using **`document.archive_item.visibility`**, not **`Document.visibility`** alone.

### Split read paths (split-brain)

| Surface | Shared fields read from |
|---------|-------------------------|
| **`/archive/`** list, **`/archive/manage/`** (staff) | **`ArchiveItem`** |
| **`/archive/<id>/`** MANUAL_TEXT detail | **`ArchiveItem`** |
| **`/archive/<id>/`** OCR_DOCUMENT detail | Redirects to document detail |
| **`/api/ui/documents/`** list, detail, API serialization | **`ArchiveItem`** (via `doc.archive_item`; PR5c) |
| Metadata completion backlog | **`Document`** queryset; row **title** display from **`ArchiveItem`** (PR5c) |
| OCR edit form (GET seed data) | **`ArchiveItem`** (PR5d) |
| Search/filter (`title`, `metadata_status`, `visibility` admin filter) | **`Document`** |
| Date display (`format_document_date` / `document_date_display`) | Duck-typed object — used with both **`doc`** and **`item`** depending on template |

### Main drift vector

**Django Admin `Document`** allows editing shared fields **without** syncing **`ArchiveItem`**. **`ArchiveItemAdmin`** is view-only. Drift can occur when staff use the secondary technical admin path instead of first-party OCR edit.

Upload/create copies shared fields onto both models at create time via **`create_ocr_document`**. The worker does not modify shared fields.

---

## 3. Duplicated shared fields map

Shared field set: **`ARCHIVE_ITEM_SHARED_FIELD_NAMES`** in `documents/services/archive_items.py`.

### title

| Aspect | Detail |
|--------|--------|
| **Document** | `Document.title` (`CharField`, required) |
| **ArchiveItem** | `ArchiveItem.title` (`CharField`, required) |
| **Write paths** | `create_ocr_document` / upload API (both at create); `update_ocr_document_metadata` (ArchiveItem → Document mirror; PR5d); Django Admin Document (no sync) |
| **Read paths** | Document list, detail, review, backlog, API; archive list/manage; OCR edit form seed |
| **Target owner** | **`ArchiveItem`** |

### visibility

| Aspect | Detail |
|--------|--------|
| **Document** | `Document.visibility` (`private` / `public`) |
| **ArchiveItem** | `ArchiveItem.visibility` (same choices) |
| **Write paths** | Same as title |
| **Read paths** | **Access:** `archive_item.visibility`. **Display:** document detail badge uses `doc.visibility`; archive list/manage use `item.visibility`; document list admin filter uses `Document.visibility` |
| **Target owner** | **`ArchiveItem`** (already access SoT; must align display and writes) |

### metadata_status

| Aspect | Detail |
|--------|--------|
| **Document** | `Document.metadata_status` (`NEEDS_COMPLETION` / `COMPLETED`) |
| **ArchiveItem** | `ArchiveItem.metadata_status` (same choices) |
| **Write paths** | Create default `NEEDS_COMPLETION`; OCR edit sync; Django Admin Document |
| **Read paths** | Backlog filters `Document.metadata_status`; document list filter; archive manage list shows `item.metadata_status`; OCR edit form seed |
| **Target owner** | **`ArchiveItem`** |

### date_start / date_end / date_precision

| Aspect | Detail |
|--------|--------|
| **Document** | `Document.date_start`, `Document.date_end`, `Document.date_precision` |
| **ArchiveItem** | `ArchiveItem.date_start`, `ArchiveItem.date_end`, `ArchiveItem.date_precision` |
| **Write paths** | Upload API, `create_ocr_document`, OCR edit sync, Django Admin Document |
| **Read paths** | `format_document_date()` used on both `doc` and `item` in templates; upload does not set precision explicitly (defaults `UNKNOWN`) |
| **Target owner** | **`ArchiveItem`** |

### ArchiveItem-only fields (not duplicated)

**`author_name`** and **`source_title`** exist on **`ArchiveItem`** only. No OCR edit UI yet. Future catalog/metadata design may extend **`ArchiveItem`**; out of scope for this cutover.

### Document-only catalog fields (remain Document-side for now)

**`DocumentMetadata`** (donor, collection, original_location, notes), **`category_event`**, and **`tags_m2m`** are edited via the same OCR metadata form but persist on **`Document`** only. Unified cross-item-type catalog design is deferred.

---

## 4. Target source-of-truth model

### ArchiveItem owns shared archival fields

For **`OCR_DOCUMENT`** (and already for **`MANUAL_TEXT`**):

- **`title`**
- **`visibility`**
- **`metadata_status`**
- **`date_start`**, **`date_end`**, **`date_precision`**

### Document owns OCR/runtime fields

- **`doc_type`**, **`language`**, **`text_input_type`**
- **`upload_status`**, **`processing_state_user`**
- File fields: **`file_s3_key`**, **`file_original_name`**, **`mime_type`**, **`size_bytes`**, **`upload_error`**, **`expected_source_file_count`**
- **`DocumentSourceFile`**
- **`DocumentTextResult`** and OCR/HTR routing inputs
- **`TranskribusRun`**, **`ProcessingMetric`**, and other processing artifacts

### Temporary compatibility mirror

Duplicated shared fields on **`Document`** remain as a **compatibility mirror** during transition. They are updated from **`ArchiveItem`** on write paths until a later optional schema cleanup PR drops the columns.

### DocumentMetadata / category_event / tags

**Remain Document-side for now.** Moving catalog metadata to **`ArchiveItem`** (or a shared catalog model) requires a **separate design** when **`MANUAL_TEXT`**, **`PHOTO`**, or unified archive catalog workflows need the same fields. Do not block shared-field cutover on catalog migration.

---

## 5. Options considered

### Option A — Keep current bridge/sync longer

**Description:** Continue Document-first writes with `sync_archive_item_shared_fields_from_document` on OCR edit only; accept split read paths.

| | |
|--|--|
| **Pros** | Minimal immediate work; PR1 sync already exists |
| **Cons** | Permanent dual maintenance; admin drift; inconsistent user-visible behavior |
| **Risks** | Visibility/display divergence; backlog vs archive manage disagree |
| **Verdict** | Acceptable short bridge only — **reject as long-term strategy** |

### Option B — Partial read-path cutover

**Description:** User-facing UI reads shared fields from **`ArchiveItem`**; **`Document`** fields kept for legacy/admin/upload compatibility; writes still Document-first.

| | |
|--|--|
| **Pros** | Incremental; fixes display without schema change |
| **Cons** | Dual-write remains; sync direction ambiguous; admin drift persists |
| **Verdict** | Useful **intermediate PR** (PR5c), not the end state |

### Option C — Phased strong cutover (recommended)

**Description:** **`ArchiveItem`** becomes canonical for shared fields; **`Document`** shared fields become compatibility mirrors updated from **`ArchiveItem`** on write; phased PRs for reconcile → reads → writes → admin/backlog alignment.

| | |
|--|--|
| **Pros** | Matches ArchiveItem-first intent and **`MANUAL_TEXT`** pattern; single write SoT; visibility unified; **`PHOTO`**-ready |
| **Cons** | Requires reconciliation, read/write flips, admin policy, test updates |
| **Verdict** | **Recommended end state**, implemented in small phases |

---

## 6. Proposed PR sequence after PR5a

### PR5b — Data reconciliation

| | |
|--|--|
| **Goal** | Ensure all **`OCR_DOCUMENT`** **`ArchiveItem`** rows match **`Document`** shared fields before read/write cutover |
| **Scope** | Management command or equivalent with **dry-run** and apply modes; report mismatches (especially **visibility**); idempotent Document → ArchiveItem sync for OCR rows |
| **Out of scope** | Read/write path changes; schema changes |
| **Tests** | Command dry-run/apply tests; mismatch reporting; idempotency |
| **Risk** | **Medium** — production data; mitigate with dry-run first |

**Implementation (done):** Command **`python manage.py reconcile_ocr_shared_fields`**. Service **`documents/services/ocr_shared_field_reconciliation.py`**. Default **dry-run**. **`--apply`** reconciles five non-visibility fields (**`title`**, **`metadata_status`**, **`date_start`**, **`date_end`**, **`date_precision`**) from **`Document`** → **`ArchiveItem`**. **`--apply --include-visibility`** additionally copies **`visibility`** (explicit opt-in — affects access). **`--include-visibility`** without **`--apply`** is rejected. Optional **`--document-id`**, **`--json`**. Per-document **`transaction.atomic()`**; idempotent re-run.

### PR5c — Read-path cutover

| | |
|--|--|
| **Goal** | User-facing display of shared fields reads from **`ArchiveItem`** (via `doc.archive_item` or shared helper) on document list, detail, review, API serialization where appropriate |
| **Scope** | `format_document_date` generalization or wrapper; templates; `_serialize_doc`; visibility badges from **`archive_item`** |
| **Out of scope** | Write-path flip; backlog queryset; admin readonly |
| **Tests** | List/detail parity; access unchanged; visibility badge from ArchiveItem |
| **Risk** | **Medium** — user-visible labels |

**Implementation (done):** User-facing OCR document **display** reads the six shared archival fields from linked **`ArchiveItem`** (list/detail/review HTML, metadata backlog row titles, JSON **`/api/documents/`** via **`_serialize_doc`**, visibility badges, dates). Helper **`shared_archive_item_for_document`**. **`format_document_date`** accepts any object with date fields. Querysets use **`select_related("archive_item")`** where needed.

**Unchanged (deferred):** Write paths; list/backlog/review **filters and search** (still **`Document`** fields — PR5f); **`admin_backlog_page`** membership rule; Django Admin.

### PR5d — Write-path flip

| | |
|--|--|
| **Goal** | **`update_ocr_document_metadata`** writes **`ArchiveItem`** first; add **`sync_document_shared_fields_from_archive_item`** for compatibility mirror |
| **Scope** | `archive_items.py` service layer; OCR edit view transaction (atomic preserved) |
| **Out of scope** | Upload API changes; removing `sync_archive_item_shared_fields_from_document` until mirror policy stable |
| **Tests** | Edit round-trip; both models in sync after save; validation blocking unchanged |
| **Risk** | **Medium** |

**Implementation (done):** **`update_ocr_document_metadata`** saves six shared fields on **`ArchiveItem`**, then **`sync_document_shared_fields_from_archive_item`** mirrors to **`Document`**. OCR edit GET seed reads from **`ArchiveItem`**. **`sync_archive_item_shared_fields_from_document`** retained for PR5b reconciliation only.

### PR5e — Upload/create alignment

| | |
|--|--|
| **Goal** | **`create_ocr_document`** treats **`ArchiveItem`** as create-time source of truth with explicit ordering; prevent create-time divergence |
| **Scope** | `create_ocr_document` in `archive_items.py` |
| **Out of scope** | Upload API request/response contract |
| **Tests** | Upload create parity tests (existing patterns in `test_archive_item.py`) |
| **Risk** | **Low–Medium** |

### PR5f — Backlog/search/filter/admin alignment

| | |
|--|--|
| **Goal** | Metadata backlog, document list filters, and search use **`archive_item__*`** joins or shared helpers; **`Document`** admin shared fields **read-only** with pointer to first-party OCR edit |
| **Scope** | `views.py` (`_base_queryset`, `admin_backlog_page`); `admin.py` fieldsets |
| **Out of scope** | Enabling **`ArchiveItemAdmin`** edit; catalog field migration |
| **Tests** | Backlog/filter tests; admin cannot mutate shared fields via POST |
| **Risk** | **Medium** |

### PR5g — Compatibility mirror deprecation (optional, later)

| | |
|--|--|
| **Goal** | Stop writing or remove duplicated **`Document`** shared columns |
| **Scope** | Schema migration + code cleanup — **requires explicit approval** |
| **Out of scope** | Until several releases of stable ArchiveItem-canonical writes |
| **Tests** | Migration tests; full regression |
| **Risk** | **High** |

---

## 7. Data / backfill / conflict policy

### Linked ArchiveItem guarantee

- Migration **`0020_archiveitem_foundation`** backfilled **`ArchiveItem`** for all existing **`Document`** rows.
- **`Document.archive_item`** is required (non-null) after foundation migration.
- Production OCR create paths use **`create_ocr_document`**, which always creates both rows.
- **`MANUAL_TEXT`** items have **`ArchiveItem`** without **`Document`** — expected; reconciliation applies to **`OCR_DOCUMENT`** only.

### Reconciliation before cutover (PR5b)

Run reconciliation **before** read-path and write-path cutover PRs:

1. **Dry-run** (default) — `python manage.py reconcile_ocr_shared_fields` lists rows where any of the six fields differ between **`Document`** and linked **`ArchiveItem`**. Optional **`--json`**.
2. **Apply** — **`--apply`** for **`OCR_DOCUMENT`**, copy non-visibility shared fields from **`Document`** → **`ArchiveItem`**. Optional **`--document-id`** filter.
3. **Visibility apply** — **`--apply --include-visibility`** copies all six fields including **`visibility`** (explicit; affects access).
4. **Idempotent** — safe to re-run.

**Visibility policy (resolved):** Dry-run always reports visibility mismatches in a dedicated warning section. Default **`--apply`** **skips** **`visibility`**. Copy visibility only with **`--apply --include-visibility`** after reviewing dry-run output. **`--include-visibility`** without **`--apply`** raises **`CommandError`**.

### Conflict resolution

| Phase | Winner | Policy |
|-------|--------|--------|
| **Pre-cutover reconcile (PR5b)** | **`Document`** for bulk apply (non-visibility fields) | Bulk repair before cutover; do not re-run casually after PR5d without dry-run review |
| **Visibility mismatches (PR5b)** | **Opt-in only** | Dry-run reports high-severity visibility drift. Default **`--apply`** skips **`visibility`**. Use **`--apply --include-visibility`** only after reviewing dry-run — copying visibility changes who can view items |
| **Post write-cutover (PR5d+)** | **`ArchiveItem`** | **`Document`** mirror updated from **`ArchiveItem`** on every canonical write |
| **Django Admin Document edits** | Prevented (PR5f) | Read-only shared fields eliminate new drift |

### After cutover

**`Document`** duplicated shared fields are **compatibility mirrors only**, not authoritative. Dropping columns is a separate optional PR (PR5g).

---

## 8. Sync and admin policy

### OCR metadata edit (PR5d — current)

- **`update_ocr_document_metadata`** — **`ArchiveItem`** canonical save, then **`sync_document_shared_fields_from_archive_item`** (ArchiveItem → Document mirror).
- **`sync_archive_item_shared_fields_from_document`** — PR5b reconciliation command and legacy repair only; not used on the canonical OCR edit write path.
- Upload create copies fields at create time only (no ongoing sync except OCR edit and reconciliation).

### Deferred

- **`create_ocr_document`** ArchiveItem-first create ordering (PR5e).
- Sync on **explicit staff edit** and **atomic create** only — no background job.

### Django Admin

- **`Document`**: shared fields become **read-only / de-emphasized** (PR5f); help text points to **`/archive/manage/<archive_item_id>/edit/`**.
- **`ArchiveItemAdmin`**: remains view-only until a deliberate archive-item admin policy exists; first-party OCR edit remains primary staff path.

---

## 9. Risks and mitigations

| Risk | Mitigation |
|------|------------|
| **Visibility / access drift** | Reconcile before cutover; read cutover uses `archive_item.visibility` for display; access tests for anonymous/family/staff |
| **Backlog semantics** | PR5f aligns backlog filter with `archive_item__metadata_status` or equivalent |
| **Document list / API / search filters** | PR5f moves filters to `archive_item__*` joins; regression tests on filter behavior |
| **Upload/create bridge** | PR5e aligns `create_ocr_document`; no upload API contract change |
| **Document detail vs archive detail** | OCR archive detail redirects to document detail — read cutover keeps both consistent |
| **Tests relying on Document fields** | Update assertions per PR; factories may set both until mirror deprecated |
| **Manual text behavior** | Already ArchiveItem SoT — reuse shared helpers; no Document involvement |
| **Future PHOTO compatibility** | ArchiveItem canonical model supports item types without Document |
| **Production data safety** | PR5b dry-run mandatory before apply; no destructive schema in initial cutover PRs |

---

## 10. Testing strategy

### Per-PR regression matrix

| PR | Focus |
|----|-------|
| PR5b | Reconciliation command dry-run/apply; mismatch detection; idempotency |
| PR5c | Display parity document vs archive surfaces; access unchanged; ArchiveItem shared-field display on OCR pages |
| PR5d | Write round-trip; both models in sync after OCR edit |
| PR5e | Upload create leaves both models matching |
| PR5f | Backlog inclusion rules; filter/search; admin readonly on shared fields |

### Standing test categories

- **Access tests** — `document_queryset_for_user`, `can_view_archive_item`, family/public/anonymous matrix
- **Read/write parity** — after any write, assert six fields equal on Document and ArchiveItem for OCR_DOCUMENT
- **Validation blocking** — cross-section OCR edit validation unchanged across cutover PRs
- **Admin readonly** — PR5f: POST to Document admin cannot change shared fields

---

## 11. Open questions

1. ~~**Visibility conflict policy after dry-run**~~ — **Resolved in PR5b:** dry-run reports visibility drift; default **`--apply`** skips visibility; **`--apply --include-visibility`** is explicit opt-in after review.
2. **When to drop Document shared columns** — After how many releases of ArchiveItem-canonical writes? PR5g requires explicit approval.
3. **Document list long-term** — Remain OCR operations console (Document-centric queryset) with ArchiveItem reads for shared display, or migrate to ArchiveItem-first list with OCR columns joined?
4. **Unified catalog metadata** — When to design **`ArchiveItem`**-level catalog for donor/tags/category across **`OCR_DOCUMENT`**, **`MANUAL_TEXT`**, **`PHOTO`**?
5. **`author_name` / `source_title`** — Add to OCR edit form in this PR series or defer to catalog design?

---

## 12. References

- Decision log: ArchiveItem foundation, manual text, OCR metadata UI chain (PR1–PR4), audit follow-up
- `docs/ai-context/document-date-precision.md` — date precision display semantics (apply to archival item dates post-cutover)
