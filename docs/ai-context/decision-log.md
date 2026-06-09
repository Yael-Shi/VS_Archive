# VS-Archive Decision Log

## ArchiveItem — central content entity foundation

**Decision:** Introduce **`ArchiveItem`** as the long-term central archival content entity. Existing and new OCR-backed **`Document`** rows link via **`Document.archive_item`** (`OneToOneField`, `on_delete=CASCADE`, `related_name="ocr_document"`).

**Initial `item_type` values:** `OCR_DOCUMENT`, `MANUAL_TEXT`, `PHOTO` (enum only for the latter two in this PR).

**Scope (this PR):** Model + migration + data backfill + `create_ocr_document` helper + upload create paths + minimal admin + focused tests + docs. **No** manual-text or photo-only creation flows. **No** removal of shared fields from **`Document`**. **No** list/detail/review UI cutover to **`ArchiveItem`**. **No** OCR/HTR, routing, visibility, or date-display behavior changes.

**Bridge semantics (temporary):** **`ArchiveItem`** shared fields are initialized from **`Document`** at **create** and **migration backfill** time only. There is **no** ongoing sync on edit. **`Document`** remains the runtime source of truth for existing list/detail/upload/review behavior until a later cutover PR. Field duplication on both models is a **migration bridge**, not the final architecture. **Do not assume** **`ArchiveItem`** copies stay current after **`Document`** edits during the bridge phase. Before any future cutover that makes **`ArchiveItem`** the runtime source of truth for list/detail/search/API, shared fields must be refreshed from current **`Document`** values or migrated through an **explicit sync strategy** (not implemented in this PR).

**Backfill:** Migration `0020_archiveitem_foundation` is **self-contained** (uses `apps.get_model` only; does not import runtime services). It creates **`ArchiveItem`** rows for documents missing **`archive_item`**, copying **`title`**, **`visibility`**, **`date_start`**, **`date_end`**, **`date_precision`**, **`metadata_status`** with **`item_type=OCR_DOCUMENT`** — no inference beyond stored document values. **`created_at`** / **`updated_at`** are set via **`QuerySet.update`** after create so **`auto_now_add`** / **`auto_now`** do not overwrite document timestamps.

**Create path:** Production upload APIs and tests use explicit **`create_ocr_document`**. **`Document.objects.create`** does **not** auto-create **`ArchiveItem`** (no manager override). **Django admin:** **`Document`** add is disabled. OCR-backed documents must be created via upload / **`create_ocr_document`**, not admin “Add document”. **`ArchiveItemAdmin`** is **view-only** during the foundation bridge: add/change/delete disabled; view via normal **`has_view_permission`** checks. Do not manually edit **`ArchiveItem`** shared fields in admin until **`ArchiveItem`** is runtime source of truth or a sync/cutover PR exists — editing would drift from **`Document`** with no ongoing sync.

**Delete behavior:** Legacy **`Document`** delete paths run inside **`transaction.atomic()`** so document-row removal and linked **`ArchiveItem`** cleanup commit or roll back together. At the ORM level, **`ArchiveItem.delete()`** is the canonical parent delete path (**`on_delete=CASCADE`** on **`Document.archive_item`** removes the linked OCR **`Document`** and its **`CASCADE`** children). **`ArchiveItem`** deletion through Django admin is **disabled** until a deliberate archive-item deletion policy/workflow is designed. Legacy **`Document`** delete paths (instance and **`QuerySet.delete`**) also remove the linked **`ArchiveItem`** so bulk document deletes do not leave orphan **`OCR_DOCUMENT`** rows.

**Deferred:** UI/API reads from **`ArchiveItem`** for OCR-backed documents; ongoing shared-field sync; deduplicating **`Document`** fields; **`PHOTO`** item flows; archive-item-level text results for OCR outputs; unified public **`ArchiveItem`** listing across item types.

## ArchiveItem — manual text (`MANUAL_TEXT`)

**Decision:** Implement the first non-OCR **`ArchiveItem`** content type as staff/admin-entered manual text. **`ArchiveItem`** is the **runtime source of truth** for **`MANUAL_TEXT`** items; **`ManualTextContent.body`** stores the typed content (not **`DocumentTextResult`**).

**Runtime source of truth (bridge phase):**

- **`OCR_DOCUMENT`:** **`Document`** remains runtime source of truth for OCR-specific fields and bridge upload/list/detail/review behavior. **`ArchiveItem.visibility`** is the access-control source of truth for viewing. Shared non-access fields are copied at create/backfill only — **no** ongoing sync. Do not assume **`ArchiveItem`** copies stay current after **`Document`** edits. **`Document.visibility`** remains a temporary compatibility field.
- **`MANUAL_TEXT`:** **`ArchiveItem`** + **`ManualTextContent`** are runtime source of truth. Before a future OCR cutover, refresh/sync shared fields from **`Document`** or run an explicit migration strategy.

**Model:** **`ManualTextContent`** — `OneToOneField` to **`ArchiveItem`** (`related_name="manual_text_content"`, `on_delete=CASCADE`), plus **`body`**, **`created_at`**, **`updated_at`**. Uses **`ArchiveItem.item_type=MANUAL_TEXT`**.

**Access (`ArchiveItem.visibility`):** **`public`** (everyone) and **`private`** (approved **`archive_family`** group + staff/admin). **`private`** means private family archive content, not staff-only. Centralized in **`documents/services/archive_item_access.py`**. **`ArchiveItem.visibility`** is the access-control source of truth for all item types. **`Document.visibility`** remains a temporary compatibility/bridge field; document list/detail access respects **`document.archive_item.visibility`**. Non-viewable items return **404**. Family invitation/account-management is deferred.

**Services:** **`create_manual_text_archive_item(...)`** and **`update_manual_text_archive_item(...)`** in **`documents/services/archive_items.py`**. Server-side validation in **`manual_text_validation.py`**. Default **`metadata_status=NEEDS_COMPLETION`**; staff choose **`NEEDS_COMPLETION`** or **`COMPLETED`** on create/edit. **Does not** create **`Document`**, **`DocumentSourceFile`**, or **`DocumentTextResult`**. **Does not** enqueue SQS or run OCR/HTR.

**UI routes (archive-oriented, not `/api/ui/...`):**

- **`/archive/`** — unified visible archive item list for current viewer
- **`/archive/<id>/`** — detail (**`MANUAL_TEXT`** in this PR; **`OCR_DOCUMENT`** redirects to existing document detail during bridge)
- **`/archive/manage/`** — staff/admin management list
- **`/archive/manage/new/manual-text/`** — staff/admin create
- **`/archive/manage/<id>/edit/`** — staff/admin edit (**`MANUAL_TEXT`** body + shared metadata; **`OCR_DOCUMENT`** shared metadata only — see **“OCR shared metadata edit UI (PR1)”** below)

Manual text body displayed with Django auto-escape + **`linebreaksbr`** (no **`safe`**).

**Admin:** **`ArchiveItemAdmin`** and **`ManualTextContentAdmin`** remain view-only. Django admin is **not** the primary create/edit flow.

**Future metadata (deferred):** people mentioned/shown, places, narrator/author/source, event context, relationships between archive items, richer date/source/confidence notes.

**Still deferred:** **`PHOTO`** items; full **`OCR_DOCUMENT`** cutover to **`ArchiveItem`** list/detail/search/API; automatic ongoing **`Document`** ↔ **`ArchiveItem`** sync outside explicit edit-time sync; storing manual text in **`DocumentTextResult`**; invitation/account-management for family users beyond Django Group membership.

## ArchiveItem — OCR shared metadata edit UI (PR1)

**Decision:** Add a staff/admin first-party UI to edit the six shared archival fields on existing **`OCR_DOCUMENT`** items: **`title`**, **`visibility`**, **`metadata_status`**, **`date_start`**, **`date_end`**, **`date_precision`**.

**Edit-time sync (bridge phase):** On successful OCR metadata edit, **`update_ocr_document_metadata`** saves shared fields on **`Document`** first, then mirrors them to the linked **`ArchiveItem`** via **`sync_archive_item_shared_fields_from_document`**. **`Document`** remains the OCR runtime source of truth during the bridge. **`ArchiveItem.visibility`** remains the access-control source of truth for viewing (document list/detail access reads **`document.archive_item.visibility`**).

**Scope (PR1):** Shared metadata edit routes/templates/services/tests/docs only. **Not** a full **`OCR_DOCUMENT`** cutover. **No** catalog fields (donor, collection, tags, etc.). **No** **`language`** / **`text_input_type`** editing. **No** OCR/HTR, upload, worker, or **`DocumentTextResult`** changes.

**Routes:** **`/archive/manage/<id>/edit/`** serves **`OCR_DOCUMENT`** items (redirect after save → existing document detail). Staff edit link also on document detail (**`עריכת מטא־דאטה`**) and manage list.

**Out of scope / still deferred:** Full **`ArchiveItem`** runtime cutover for OCR list/detail/search/API; automatic sync on non-edit paths (e.g. Django admin **`Document`** edits); **`PHOTO`** items.

## ArchiveItem — OCR catalog scalar metadata edit UI (PR2a)

**Decision:** Extend the existing staff/admin **`OCR_DOCUMENT`** edit UI at **`/archive/manage/<id>/edit/`** to edit catalog scalar metadata: **`donor`**, **`collection`**, **`original_location`**, **`notes`** (on **`DocumentMetadata`** / **`admin_meta`**) and **`category_event`** (on **`Document`**).

**Persistence:** **`update_ocr_document_catalog_metadata`** saves **`category_event`** on **`Document`** and upserts **`DocumentMetadata`** via **`update_or_create`**. **`Document`** remains OCR runtime source of truth. **No** **`ArchiveItem`** mirror for catalog fields. **No** OCR/HTR, upload, worker, routing, or **`DocumentTextResult`** changes.

**Scope (PR2a):** Catalog validation/parsing, service, OCR edit form/templates, staff detail display for **`category_event`**, tests, this log entry. **Tags** intentionally deferred to **PR2b**. **No** **`language`** / **`text_input_type`** editing. **Not** a full **`OCR_DOCUMENT`** cutover. **No** **`PHOTO`** items.

**Routes:** Same **`/archive/manage/<id>/edit/`** as PR1; redirect after save → document detail.

## ArchiveItem — OCR document tag edit UI (PR2b)

**Decision:** Extend the existing staff/admin **`OCR_DOCUMENT`** edit UI at **`/archive/manage/<id>/edit/`** with a comma-separated **`tags`** field. Save semantics are **replace-all**: the submitted tag set replaces **`Document.tags_m2m`**; empty input clears all document tags.

**Persistence:** **`update_ocr_document_tags`** uses **`Tag.objects.get_or_create(name=…)`** per normalized name, then **`document.tags_m2m.set(…)`**. **`Document`** remains OCR runtime source of truth. **No** **`ArchiveItem`** mirror. **No** deletion of unused **`Tag`** rows. Casing is preserved; duplicates in one submit are deduped after trim (first-seen order). Tag name max length **64** is validated on edit with Hebrew errors; tag validation errors block the combined OCR edit form save (shared + catalog + tags).

**Scope (PR2b):** Tag validation/parsing module, service, OCR edit form/templates, optional staff detail display for tags, upload attach helper refactor to shared list normalizer only (behavior unchanged), tests, this log entry. **No** upload API max-length validation. **No** **`language`** / **`text_input_type`** editing. **No** OCR/HTR, upload contract, worker, routing, or **`DocumentTextResult`** changes. **Not** a full **`OCR_DOCUMENT`** cutover. **No** **`PHOTO`** items.

**Routes:** Same **`/archive/manage/<id>/edit/`** as PR1/PR2a; redirect after save → document detail.

## ArchiveItem — metadata completion backlog edit links (PR3)

**Decision:** The staff metadata completion backlog (**השלמת פרטים**, **`/api/ui/admin/backlog/`**) uses the first-party **`OCR_DOCUMENT`** edit UI as the **primary** per-row action (**`עריכת מטא־דאטה`** → **`/archive/manage/<archive_item_id>/edit/`**). Django Admin document change remains available as a **secondary** technical path (**`עריכה טכנית (Django Admin)`**).

**Unchanged:** **`admin_backlog_page`** queryset, filters (**`only_missing_tags`**, **`only_missing_admin_meta`**), counts, pagination, auth, and inclusion rule (**`metadata_status=NEEDS_COMPLETION`** only). **No** auto-completion of **`metadata_status`** when catalog/tags are filled. **No** changes to בקרת תמלול review backlog, OCR edit form behavior, OCR/HTR processing, or **`ArchiveItem`** runtime cutover.

**Scope (PR3):** Backlog template action links, focused tests, this log entry.

## ArchiveItem — OCR staff action hierarchy on detail pages (PR4)

**Decision:** OCR **document detail** (`/api/ui/documents/<doc_id>/`) and **transcription review detail** (`/api/ui/admin/review/<doc_id>/`) staff toolbars follow the same first-party action hierarchy as the metadata completion backlog (PR3): **`עריכת מטא־דאטה`** is the emphasized first-party metadata path; **בקרת תמלול** / **תצוגת מסמך** remain secondary cross-workflow links; **Django Admin** is a secondary technical escape hatch labeled **`עריכה טכנית (Django Admin)`** (plain `btn`, not primary). Review detail adds the metadata edit link when **`archive_item_id`** exists; no redundant “open review” link on review detail. **`/archive/manage/`** OCR rows use **`עריכת מטא־דאטה`** (MANUAL_TEXT labels unchanged).

**Unchanged:** Metadata edit save behavior, review POST handlers, **`DocumentTextResult`** status/verification semantics, backlog queryset/filters/counts, permissions, OCR/HTR processing, upload API, worker/SQS, **`ArchiveItem`** runtime cutover. **No** delete action for **`OCR_DOCUMENT`**.

**Scope (PR4):** Detail/review-detail/manage-list template action links, focused tests, this log entry.

## ArchiveItem — OCR metadata edit audit follow-up (pre-PR5)

**Decision:** Small hardening after the OCR metadata UI chain audit: harmonize metadata backlog Django Admin label to **`עריכה טכנית (Django Admin)`** (matches PR4 detail/review wording); wrap combined shared/catalog/tags OCR metadata save in one **`transaction.atomic()`** block so DB failure cannot leave partial persistence; add focused regression tests for cross-section validation blocking and family GET **403** on **`/archive/manage/<id>/edit/`**.

**Unchanged:** Validation rules, field set, backlog queryset/filters/auth, review backlog, OCR/HTR processing, upload/worker behavior.

**Scope:** `backlog.html`, `_archive_manage_edit_ocr_document`, focused tests, this log entry.

## ArchiveItem — OCR_DOCUMENT shared-field source-of-truth cutover design (PR5a)

**Decision:** Approve the design for **`OCR_DOCUMENT`** shared-field source-of-truth cutover. **Target:** **`ArchiveItem`** is canonical for the six shared archival fields (**`title`**, **`visibility`**, **`metadata_status`**, **`date_start`**, **`date_end`**, **`date_precision`**). **`Document`** remains OCR/runtime/processing source of truth. Duplicated **`Document`** shared fields stay as temporary compatibility mirrors until optional later schema cleanup.

**Implementation:** Deferred to **PR5b+** (reconcile → read cutover → write flip → upload/admin/backlog alignment). **Not** implemented in PR5a.

**Docs:** `docs/ai-context/ocr-archiveitem-cutover.md`

**Out of scope (cutover series):** OCR/HTR, upload API, worker/SQS, **`DocumentTextResult`** semantics, catalog/tags migration, **`PHOTO`**, dropping **`Document`** shared columns.

## ArchiveItem — OCR_DOCUMENT shared-field reconciliation (PR5b)

**Decision:** Add pre-cutover reconciliation for **`OCR_DOCUMENT`** rows via management command **`reconcile_ocr_shared_fields`**. Default mode is **dry-run** (no writes). **`--apply`** copies shared fields from **`Document`** → linked **`ArchiveItem`** only; **never** mutates **`Document`**.

**Visibility policy:** **`ArchiveItem.visibility`** already controls access. Dry-run always detects and reports visibility drift. Default **`--apply`** reconciles the five non-visibility shared fields only. **`--apply --include-visibility`** is required to copy visibility (explicit opt-in; affects who can view items). **`--include-visibility`** without **`--apply`** raises **`CommandError`**.

**Scope (PR5b):** Service module **`ocr_shared_field_reconciliation.py`**, optional **`field_names`** on **`sync_archive_item_shared_fields_from_document`**, management command, focused tests, docs. **No** read-path or write-path cutover. **No** UI/API/worker/schema changes.

**Docs:** `docs/ai-context/ocr-archiveitem-cutover.md` (PR5b implementation note).

## ArchiveItem — OCR_DOCUMENT read-path display cutover (PR5c)

**Decision:** User-facing OCR document **display** reads the six shared archival fields from linked **`ArchiveItem`**, not **`Document`**. **`Document`** remains OCR/runtime/processing source of truth.

**Scope (PR5c):** Helper **`shared_archive_item_for_document`**; **`format_document_date`** docstring/type generalization; **`_serialize_doc`** and OCR list/detail/review/backlog **templates**; **`select_related("archive_item")`** on display querysets. **No** write-path, upload/create, reconciliation, Admin, or filter/search/backlog membership changes.

**Unchanged (deferred):** List/backlog/review **filters and search** still use **`Document`** shared fields (PR5f). Metadata backlog **inclusion** still **`Document.metadata_status=NEEDS_COMPLETION`**. OCR edit form GET seed still **`Document`** (PR5d). Access still **`ArchiveItem.visibility`**.

**Docs:** `docs/ai-context/ocr-archiveitem-cutover.md` (PR5c implementation note).

## ArchiveItem — OCR_DOCUMENT write-path flip (PR5d)

**Decision:** Staff OCR metadata edit at **`/archive/manage/<archive_item_id>/edit/`** writes the six shared archival fields to linked **`ArchiveItem`** first, then mirrors them onto **`Document`** via **`sync_document_shared_fields_from_archive_item`**. **`ArchiveItem`** is canonical for shared fields; duplicated **`Document`** shared columns are compatibility mirrors only. OCR edit form GET seed reads shared fields from **`ArchiveItem`**.

**Scope (PR5d):** **`update_ocr_document_metadata`** internal flip; **`sync_document_shared_fields_from_archive_item`**; OCR edit form seed in **`views.py`**. Catalog scalar metadata and tags remain **`Document`**-side. **`sync_archive_item_shared_fields_from_document`** unchanged (PR5b reconciliation command).

**Unchanged (deferred):** Upload/create (**`create_ocr_document`**, **`/api/uploads/create/`**) → PR5e. List/backlog/review **filters and search** and backlog **membership** → PR5f. Django Admin shared-field editability → PR5f.

**Docs:** `docs/ai-context/ocr-archiveitem-cutover.md` (PR5d implementation note).

## ArchiveItem — OCR_DOCUMENT upload/create alignment (PR5e)

**Decision:** **`create_ocr_document`** creates **`ArchiveItem`** as the canonical holder for the six shared archival fields at insert time. **`Document`** receives compatibility mirror values from the persisted **`ArchiveItem`** via **`archive_item_field_values_from_archive_item`** at **`Document.objects.create`** — no post-create **`sync_document_shared_fields_from_archive_item`** call (avoids an extra UPDATE). **`Document`** remains OCR/runtime source of truth for processing-specific fields.

**Scope (PR5e):** **`create_ocr_document`** refactor and private **`_split_ocr_document_create_kwargs`** in **`archive_items.py`**; focused create/upload parity tests; docs. **No** upload API request/response contract change. **No** **`views.py`** upload validation, S3 verification, presigned URLs, SQS enqueue, or multi-image behavior changes.

**Unchanged (deferred):** List/backlog/review **filters and search** and backlog **membership** → PR5f. Django Admin shared-field editability → PR5f.

**Docs:** `docs/ai-context/ocr-archiveitem-cutover.md` (PR5e implementation note).

## ArchiveItem — OCR_DOCUMENT filter/search/backlog/admin alignment (PR5f)

**Decision:** Shared archival **filters**, **search title arm**, and **metadata completion backlog membership** read from linked **`ArchiveItem`** (`archive_item__*` joins on **`Document`** querysets). **`Document`** shared columns remain compatibility mirrors only. **Runtime/processing filters** stay **`Document`**-based (`doc_type`, `upload_status`, `language`, `text_input_type`, `processing_state_user`, review **`DocumentTextResult`** filters, tags/admin_meta/catalog search arms).

**Scope (PR5f):** **`views._base_queryset`** (`metadata_status`, admin `visibility`, `q` title); **`admin_backlog_page`** membership (`archive_item__metadata_status=NEEDS_COMPLETION`); **`documents_in_review_backlog`** `q` title search; **`DocumentAdmin`** shared fields readonly with compatibility-mirror fieldset and first-party edit pointer; list display/filter/search use ArchiveItem-backed semantics. **No** migration, upload/create, reconciliation command, OCR/HTR, worker, or permissions changes.

**Django Admin:** **`DocumentAdmin`** shared fields (**`title`**, **`visibility`**, **`metadata_status`**, **`date_*`**) are **read-only** mirrors. **`ArchiveItemAdmin`** remains view-only. Staff edit canonical OCR metadata at **`/archive/manage/<archive_item_id>/edit/`**.

**Unchanged:** **`reconcile_ocr_shared_fields`** behavior (PR5b repair tool). Review backlog **membership** (pending **`DocumentTextResult`** only). **`only_missing_tags`** / **`only_missing_admin_meta`** sub-filters. Dropping **`Document`** shared columns → optional PR5g.

**Docs:** `docs/ai-context/ocr-archiveitem-cutover.md` (PR5f implementation note).

## Document date precision — schema foundation (PR 2)

**Decision:** Add **`Document.date_precision`** (`DatePrecision`: `EXACT_DAY`, `MONTH`, `YEAR`, `RANGE`, `UNKNOWN`) with Django default **`UNKNOWN`**. Migration adds the column with that default for **all** existing rows — **no** inference from `date_start`/`date_end` in this PR.

**Scope (PR 2):** Model + migration + minimal admin fieldset + focused tests + design-doc pointer update only. **No** upload/UI/display/filtering changes, **no** `date_display` / `date_note` / estimated-date fields, **no** backfill script beyond the migration default.

**Deferred:** Precision-aware save/display (PR 3–5 in `docs/ai-context/document-date-precision.md`); automated backfill rules (section 8 of that doc) require explicit approval before any data migration beyond `UNKNOWN`.

## Document date precision — list/detail display

**Decision:** Add **`format_document_date(document)`** for list/detail UI. **`UNKNOWN`** always displays **ללא תאריך** and **does not** show `date_start`/`date_end` even when populated (Option B). Other precisions format from bounds without exposing normalized first/last days for month/year.

**Scope:** Helper + template filter + `list.html` / `detail.html` only. Shipped **before** upload/edit precision UI (design-doc sequence PR 3). **No** model/migration/upload/API/filtering/backfill changes.

## Future design note — document date precision (design exploration; PR 1)

**Schema status updated by the PR 2 entry above.** Remaining bullets describe product direction, not current implementation.

- This entry records a **future design exploration only**. No behavior is implemented in this PR.
- Source input from QA: `Document.date_start` / `date_end` alone imply false precision for year-only, month-only, unknown, and range cases; list/detail currently render raw ISO dates.
- A dedicated design note was added at `docs/ai-context/document-date-precision.md`.
- **Core decision (documented, not coded):** keep normalized `date_start`/`date_end` for filtering/sorting; add `date_precision` (and display rules) so UI never shows expanded bounds as exact cataloger knowledge.
- This entry does **not** make final migration/backfill or UI decisions; those remain open for PR 2+ in the sequence in the design doc.

## Future design note — multi-image logical documents (not implemented)

- This entry records a **future design exploration only**. No behavior is implemented in this PR.
- Source input from QA: some logical archival documents consist of multiple **ordered** image files that should remain one logical document.
- A dedicated design note was added at `docs/ai-context/multi-image-documents-design.md`.
- This log entry does **not** make final schema or upload/API decisions; those remain open for later implementation PRs.

## Future design note — text-to-source hover/highlight in review (not implemented)

- This entry records a **future design exploration only**. No behavior is implemented in this PR.
- Source input from QA: reviewers had trouble locating the matching source line while editing/checking transcription text.
- A dedicated design note was added at `docs/ai-context/text-image-hover-design.md`.
- This entry does **not** make final schema, UI, API, model, or persistence decisions; those remain open for later implementation PRs.

## DocumentSourceFile — multi-image source identity foundation (PR1)

**Decision:** Add **`DocumentSourceFile`** as the ordered source-file identity layer for future multi-image documents. One row per source file per logical **`Document`**.

**Scope (PR1):** Django model + migration + model tests + this log entry only. **No** upload API changes, **no** worker/OCR/routing/adapter changes, **no** review/source-preview UI changes, **no** backfill of existing documents, **no** admin changes.

### Fields and constraints

- **`document`** FK (`related_name="source_files"`, CASCADE).
- **`order_index`** — zero-based internal ordering; UI may show **`order_index + 1`** in a later PR.
- **`file_s3_key`**, **`file_original_name`**, **`mime_type`**, **`size_bytes`**, **`created_at`**, **`updated_at`** — mirror single-file metadata shape on **`Document`**.
- **`unique(document, order_index)`**, **`unique(document, file_s3_key)`**, **`CheckConstraint(order_index >= 0)`**, **`Meta.ordering = ["order_index"]`**.
- V1 product direction is **multiple IMAGE source files only**; **MIME/image validation is deferred** to the upload flow (not enforced on the model in PR1).

### Coexistence with `Document.file_*`

- Existing **`Document.file_s3_key`**, **`file_original_name`**, **`mime_type`**, **`size_bytes`** remain unchanged and are still what upload/worker/UI use today.
- Long-term direction is **`DocumentSourceFile`** as canonical ordered source representation; cutover/dual-write is a **later** PR.
- **`DocumentTextResult`** stays **document-level**; no page-level text results in this foundation.

### Deferred (after PR2)

- Multi-file upload ingest, worker input from **`source_files`**, ordered review preview, geometry/hover, role/checksum/upload_status fields, backfill/migration of legacy single-file documents.

## DocumentSourceFile — single-file upload dual-write (PR2)

**Decision:** On successful single-file **`upload_complete`**, upsert exactly one **`DocumentSourceFile`** at **`order_index=0`** mirroring current **`Document.file_*`** metadata (`sync_primary_document_source_file` in `documents/services/source_files.py`).

**Scope (PR2):** Upload complete path + service helper + tests + this log entry only. **No** multi-file upload, **no** worker/OCR/routing/adapter changes, **no** review/source-preview UI changes, **no** backfill of existing documents, **no** API response shape change.

- **`Document.file_s3_key`**, **`file_original_name`**, **`mime_type`**, **`size_bytes`** remain written and are still what runtime paths use today.
- Idempotent **`update_or_create(document, order_index=0)`**; failed upload complete and missing-**`file_s3_key`** success paths do not create a source-file row.

## Multi-image upload — backend API contract (PR3)

**Decision:** Add a **backend-only** multi-image upload contract on the existing upload API. **No** worker, UI, OCR/HTR, or review/source-preview changes in this PR.

### Schema

- **`Document.expected_source_file_count`** — nullable; `null` for legacy/single-file; set to **N ≥ 2** at multi-image create.
- **`DocumentSourceFile.upload_status`** / **`upload_error`** — per-part upload lifecycle: **`PENDING`**, **`UPLOADED`**, **`FAILED`** (default **`PENDING`**).

### API (admin-only, same auth as existing upload endpoints)

- **`POST /api/uploads/create/`** — if **`files`** array is present → multi-image mode (2–30 **`image/*`** files only, server-assigned **`order_index`** from array order). Legacy single-file body/response unchanged when **`files`** is absent.
- **`POST /api/uploads/<doc_id>/parts/<order_index>/complete/`** — mark one planned source file **`UPLOADED`** or **`FAILED`**; part failure marks parent **`Document.upload_status=FAILED`**.
- **`POST /api/uploads/<doc_id>/finalize/`** — when all expected parts are **`UPLOADED`**, mirror **`order_index=0`** into **`Document.file_*`**, set **`Document.upload_status=UPLOADED`**, set **`Document.processing_state_user=PARTIAL`** (no **`ACTION_REQUIRED`** enum exists today), **do not enqueue SQS**. **[Superseded by PR4: the finalize success path now sets `PROCESSING` and enqueues `PROCESS_DOCUMENT`; see "Multi-image worker processing (PR4)" below.]**
- Legacy **`POST .../complete/`** — unchanged for single-file docs; returns **400** for multi-image documents.

### V1 failed-part policy (terminal)

- **Any failed multi-image part** marks the whole upload attempt **`Document.upload_status=FAILED`** — **terminal in V1**.
- While the document is **`FAILED`**, further **`success=true` part completion** and **finalize** return **400**; the failed **`DocumentSourceFile`** row stays **`FAILED`**, errors are not cleared, and the document is not moved back to **`UPLOADING`**, **`UPLOADED`**, **`PARTIAL`**, or **`PROCESSING`**.
- **Downside (accepted for V1):** admins cannot retry/replace only one failed part; they must **start a new multi-image upload**.
- **Deferred (future PR):** per-part retry/replacement before finalize — **not implemented** in PR3.

### S3 keys

- Multi-image parts: **`documents/{document_id}/source/{order_index}.{ext}`** (distinct, ordered).

### Deferred (follow-up PRs)

- Worker input from ordered **`source_files`** + SQS enqueue for multi-image documents.
- Multi-image upload UI (`upload.html`).
- Per-part upload retry/replacement after a failed part (see V1 failed-part policy above).
- Page-level text results, hover/highlight, routing changes.

## Multi-image worker processing (PR4)

**Decision:** Enable the worker to process finalized multi-image documents by building an ordered **`PageImage`** list from **`DocumentSourceFile`** rows, and enqueue multi-image documents on finalize. **No** UI, routing, adapter, or review/status semantic changes.

### Worker input selection (`run_worker._process_message`)

- **Legacy single-file documents are unchanged:** `Document.file_s3_key` → `get_object_bytes` → `extract_pages` → OCR/HTR. The legacy branch is selected whenever `is_multi_image_document(doc)` is false (i.e. `expected_source_file_count` is null or `< 2`).
- **Multi-image documents** (`expected_source_file_count >= 2`): the worker reads `source_files` ordered by `order_index`, downloads each from S3 (`get_object_bytes`), and builds one `PageImage` per source file via `source_file_bytes_to_page`. The resulting ordered list flows into the **existing** `select_ocr_route` + `transcribe_pages` path. Output stays one document-level `DocumentTextResult` set (combined text from all pages). **No page-level text results.**
- S3 I/O stays in the worker; `source_file_bytes_to_page` is a pure conversion (normalize bytes to PNG, assign page index).

### `PageImage.page_index` convention (Decision)

- Multi-image `page_index` is **1-based and contiguous**: `page_index = order_index + 1`. Source mapping is `page_index - 1 == order_index`.
- **Rationale:** preserves the existing 1-based `PageImage` convention (legacy single image uses `page_index=1`) and Transkribus `pageNr` semantics (descriptor/pages-query derive `pageNr` from `page_index`; `pageNr=0` would be invalid). The product-facing `order_index` remains zero-based.

### Pre-OCR validation and failure behavior (`get_ordered_source_files_for_processing`)

- Before any OCR/HTR dispatch, multi-image input is validated: `expected_source_file_count >= 2`; a `DocumentSourceFile` exists for every `order_index` in `0..N-1` (contiguous); no extra/out-of-range rows (`order_index < 0` or `>= N`); each row is `upload_status=UPLOADED`; each row has a non-empty `file_s3_key`; each row is `image/*` (images only in V1).
- **On validation failure** (`MultiImageSourceFilesError`): set **`Document.processing_state_user=FAILED`**, log a clear error, do **not** call OCR/HTR adapters, and do **not** create `DocumentTextResult` rows. These input-integrity failures are intentionally distinct from OCR/HTR pipeline failures (`_save_ocr_failure`), so no misleading text-result rows are written.

### SQS enqueue on finalize (`upload_finalize`)

- **Current behavior (supersedes PR3 for the success path):** on successful multi-image finalize, the document is set to **`processing_state_user=PROCESSING`** and a `PROCESS_DOCUMENT` message is enqueued (mirroring single-file `upload_complete`). PR3 previously left finalized multi-image documents at `PARTIAL` with no enqueue.
- **Double-enqueue guard:** finalize only transitions state and enqueues when the document is not already `UPLOADED`. An idempotent finalize retry enqueues **once**. Failed-upload documents remain terminal `FAILED` and never enqueue.
- **Enqueue failure** sets `Document.processing_state_user=FAILED`, records `upload_error`, and returns HTTP 500 (same shape as `upload_complete`).
- Single-file `upload_complete` enqueue behavior is unchanged.

### Routing / adapters (unchanged)

- `select_ocr_route`, `transcribe_pages`, and both adapters already accept a multi-page `PageImage` list and combine output; no changes were needed. **No** Transkribus routing broadening, **no** Gemini fallback for Hebrew handwritten (still fails fast when `ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN=false`).

### Processing-state rollup (unchanged)

- `_update_processing_state` / `expected_result_types_for_document` are unchanged: Hebrew multi-image success → **`READY`**; non-Hebrew multi-image (only `SOURCE_TEXT` persisted, `HEBREW_TEXT` missing) → **`PARTIAL`** (intentional current policy); failures → `FAILED`/`PARTIAL` per existing semantics.

### Scope / stop lines (PR4)

- **Out of scope:** upload UI, source-preview/review UI, hover/highlight, page-level `DocumentTextResult`, PDF/mixed files in multi-image (images only), per-part retry/replace, retry/backoff/DLQ redesign, schema/migration changes.

## Multi-image source preview — read-only UI (PR5)

**Decision:** Add **read-only** ordered source-image preview to the document detail page and the transcription review detail page for multi-image documents. **No** upload UI, worker, OCR/HTR, routing, review/verification semantic, `DocumentTextResult`, hover/highlight, or schema changes.

### Behavior

- New helper **`build_source_preview(document, bucket, expires_in=3600)`** in `documents/services/source_files.py` returns a `SourcePreview(items, non_uploaded_count)`.
- **`source_files` is used only for multi-image documents** (`is_multi_image_document(doc)` / `expected_source_file_count >= 2`). For non-multi-image documents the helper returns empty and both views keep the **existing `content_url` single-file preview unchanged**.
- **Legacy single-file documents are not switched** to the `source_files` rendering path even though PR2 dual-writes a `DocumentSourceFile(order_index=0)`. This prevents duplicate previews of the same first file via both `Document.file_s3_key` and `source_files[0]`.
- Preview items are ordered by `order_index` (model `Meta.ordering`); UI shows 1-based **`display_number = order_index + 1`** with a muted `עמוד N — original_name` header.
- **Only `upload_status=UPLOADED`** source files get a presigned GET / `<img>`. Any PENDING/FAILED/non-uploaded rows are counted (`non_uploaded_count`) and surfaced as **one muted note**; broken placeholders are not rendered per missing file.
- Presigned GET is generated **per item and guarded**: a failure for one file yields `url=None` (muted per-item placeholder) and does not 500 the page.

### Views

- `document_detail_page` and `review_detail_page` prefetch `source_files`, call the helper, and pass `source_preview_items` + `source_preview_unavailable_count` to the templates. The legacy `content_url` is only generated for non-multi-image documents.

### Scope / stop lines (PR5)

- **Out of scope:** upload UI / file-input changes, worker/OCR/HTR/adapter/routing changes, review/verification semantic changes, `DocumentTextResult` / page-level text changes, hover/highlight, migrations, AWS/CDK/settings, public API changes, PDF preview changes (PDFs stay on the legacy path), broad visual redesign, tabs/JS/anchors.

## Multi-image upload — admin UI (PR6)

**Decision:** Make the existing **admin** upload page (`documents/templates/documents/upload.html`) able to create multi-image documents through the already-existing PR3/PR4 backend API. **Template + inline JS only.** **No** backend, worker, OCR/HTR, routing, review/verification semantic, `DocumentTextResult`, source-preview, schema, or public API contract changes.

### Behavior

- The single file input now has the **`multiple`** attribute. Client-side branching is by selection count:
  - **1 file** → unchanged **legacy single-file flow** (`create` → presigned `PUT` → `complete/`); image **or** PDF, exactly as before. No confirmation prompt.
  - **2–30 files** → **multi-image flow** (`create` with `files[]` → `PUT` each part → `parts/<order_index>/complete/` each → `finalize/`).
- **Selection order is the document page order.** The selected files are listed (multi-image case only) as 1-based `עמוד N: <original_name>`. **No reorder controls and no drag/drop.**
- Multi-image is **image-only**: 2+ selections are validated client-side — any non-`image/*` file or **>30** files is rejected **before** any API call (mirrors the backend rules; backend remains the enforcer). No PDF+image mixed selection, no multi-PDF.
- For a 2+ selection the page forces **`doc_type=IMAGE`** (multi-image create requires/assumes `IMAGE`), so the user does not separately pick `doc_type`.
- Shared metadata (`title`, `date_*`, `language`, `text_input_type`, `category_event`, `visibility`, `tags`, `admin_meta`) applies to the **whole `Document`**; per-file payload is only `original_name`, `mime_type`, `size_bytes` inside `files[]`.
- Progress reuses the existing `#msg` status area: "יוצרת מסמך…", "מעלה עמוד K מתוך N…", "מאשרת עמוד K מתוך N…", "מסיימת מסמך…", success → redirect to the document detail page.
- **Failed-part behavior (terminal in V1):** on an S3 `PUT` failure (or network error) the UI best-effort calls `parts/<order_index>/complete/` with `{ success:false, error }` so the backend marks the upload `FAILED`, then **stops** and shows a Hebrew message that the multi-image upload failed and must be **restarted**. **No per-part retry/replace.**
- The `capture` attribute is no longer set on the file input (it conflicts with multi-select); `accept` mode handling by `doc_type` is otherwise unchanged.

### Tests

- `UploadPageTemplateTests` in `documents/tests.py` (template-render only): file input renders `multiple`; multi-image explanatory Hebrew copy present; key metadata fields still rendered; JS references `/parts/` and `/finalize/`; upload page remains admin-only (403 for non-admin). Existing backend multi-image API tests (`UploadApiTests`) are unchanged and not duplicated.

### Scope / stop lines (PR6)

- **Out of scope:** worker / OCR/HTR / adapter / routing changes, review/verification semantic changes, `DocumentTextResult` / page-level text results, hover/highlight, source-preview changes, migrations, AWS/CDK/settings, public API contract changes, PDF+image mixed upload, multi-PDF, per-part retry/replace, drag-drop/reordering, broad visual redesign. `views.py` `upload_page` context was already sufficient and was not changed.

## Current state — OCR/HTR and Transkribus (read this first)

**Last aligned:** production-gated Hebrew handwritten Transkribus routing + OCR/HTR review lifecycle behavior.

### Routing (implemented)

- Static **`OCR_ROUTES`** remains **Gemini-only** for the valid pairs that still use Gemini; `language=he` + `HANDWRITTEN` is handled explicitly in `select_ocr_route`, not by a Gemini table entry.
- **Hebrew handwritten requires Transkribus.** In `select_ocr_route`, `language=he` + `HANDWRITTEN` returns **`engine_key=TRANSKRIBUS`** only when **`ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN=true`**.
- If **`ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN=false`** (default), Hebrew handwritten routing fails fast with a clear configuration error. It does **not** fall back to Gemini.
- All other valid pairs → **Gemini** from `OCR_ROUTES`.
- **No** Gemini→Transkribus fallback. **No** Transkribus→Gemini fallback. **No** hybrid OCR routing. **`ENABLE_HYBRID_HTR`** only gates credential validation in env loading, not engine selection.
- This routing change does **not** change the OCR review lifecycle: automatic worker success still persists **`DocumentTextResult.status=NEEDS_REVIEW`** and **`verification_status=UNVERIFIED`**.

### Transkribus (implemented, gated)

- **Real** Legacy TrpServer / PyLaia: upload ingest, existing-server-document dev mode, adapter + `transkribus_engine.py`, registry, `OcrEngineKey.TRANSKRIBUS`.
- **Not** broad production-default. Current approved routing scope is **Hebrew handwritten only**, behind **`ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN`**. That flag means the route is operationally enabled in the environment; it is **not** a fallback selector.
- Existing **`TRANSKRIBUS_DEV_UPLOAD_MODE`**, **`TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT`**, **`TRANSKRIBUS_FORCE_REPROCESS`**, and **`TRANSKRIBUS_RECOGNITION_ONLY_RETRY`** remain execution / recovery controls. They are **not** the route-selection flag.
- CDK now persists the worker-side Transkribus runtime wiring using AWS-managed config references: worker non-secret Transkribus values are read from SSM parameter names derived from project/env, secret values stay in Secrets Manager, and local dev uses `.env.local` / `.env.template` placeholders instead of committed real values.
- Upload mode creates a **new** Trp document per run; **`TranskribusRun`** rows are written on Transkribus adapter paths (PR2 wiring). Duplicate prevention and cleanup remain deferred.

### AWS deploy hardening (CDK + runtime parity)

- CDK deploy hardening now preserves the intentional worker Gemini OCR/runtime tuning env values in the worker task definition to avoid silent behavior drift to application defaults.
- Added a dedicated safe deploy runbook: `docs/ai-context/deploy-aws-cdk.md`.
- Transkribus runtime wiring remains worker-only via SSM (non-secret flags/config) and Secrets Manager (credentials/token).
- No OCR routing behavior changed, no adapter behavior changed, and no worker orchestration contract changed.

### Admin UI — dual backlog workflows (PR1 list/detail, PR2 verify/reject)

Two separate staff workflows; a document may appear in both.

| Hebrew label | URL | Purpose |
|---|---|---|
| **השלמת פרטים** | `/api/ui/admin/backlog/` | Catalog/metadata completion (`metadata_status=NEEDS_COMPLETION`, tags, admin meta). Existing `admin_backlog_page` behavior unchanged; visible label renamed from generic “Backlog מנהלים”. |
| **בקרת תמלול** | `/api/ui/admin/review/` | OCR/HTR human review queue. Detail: `/api/ui/admin/review/<doc_id>/`. |

**בקרת תמלול backlog query** — document included when it has at least one `DocumentTextResult` with:

- `status=NEEDS_REVIEW`
- `verification_status` in `UNVERIFIED`, `REJECTED`
- non-empty `text`

Does **not** include legacy `SUCCEEDED` + `UNVERIFIED` rows. Does **not** reuse the metadata backlog queryset.

**PR1 scope:** staff-only list + detail; full text and row metadata (`engine_key`, `review_reasons`, `TranskribusRun` summary on detail).

**PR2 scope (manual verify/reject):**

- Staff-only POST from review detail: `POST /api/ui/admin/review/text-results/<result_id>/verify/` and `.../reject/`.
- Actions apply to **one `DocumentTextResult` transcription result** at a time (e.g. `SOURCE_TEXT` and `HEBREW_TEXT` on the same document are verified independently).
- Eligibility: `is_review_pending_text_result(row)` — `NEEDS_REVIEW`, `verification_status` in `UNVERIFIED` / `REJECTED`, non-empty stripped `text`. Ineligible POST returns **400** (no silent redirect).
- **Verify** → `verification_status=VERIFIED`; **reject** → `verification_status=REJECTED`.
- Does **not** change `DocumentTextResult.status` (including leaving **`NEEDS_REVIEW`** after verification), `text`, `review_reasons`, or **`Document.processing_state_user`**.
- Redirect to `/api/ui/admin/review/<document_id>/` after success. A document leaves the backlog when **no** pending rows remain (e.g. one of two results verified → document stays with `pending_count=1`; rejected rows remain pending).

**PR3 scope (staff-only text edit):**

- Staff-only POST: `POST /api/ui/admin/review/text-results/<result_id>/text/` from בקרת תמלול detail (`name=text` form field).
- Edits **one `DocumentTextResult` row** at a time (not the whole document, not visual/text lines).
- Overwrites `DocumentTextResult.text` in place; **no** separate raw OCR/HTR output version history. Original source image/document remains canonical.
- Eligibility: `is_review_editable_text_result(row)` — currently same as pending review (`NEEDS_REVIEW`, `UNVERIFIED` / `REJECTED`, non-empty stripped `text`). Ineligible POST returns **400**; invalid id **404**.
- **VERIFIED**, **FAILED**, **SUCCEEDED** (including legacy), and whitespace-only existing text are **not** editable in PR3.
- Does **not** verify: `verification_status` unchanged (`UNVERIFIED` stays unverified; `REJECTED` stays rejected). Reviewer uses PR2 **אשר תמלול** separately.
- Does **not** change `DocumentTextResult.status`, `review_reasons`, or **`Document.processing_state_user`**. Saves `text` + `updated_at` only.
- Submitted text must be non-empty after strip; multiline body preserved (whole text not stripped before save).
- **Deferred:** hover/coordinate UI, audit fields/tables, explicit “reopen verified for editing” workflow.

**PR4 scope (review detail workspace layout):**

- בקרת תמלול detail (`/api/ui/admin/review/<doc_id>/`) — side-by-side workspace: **מסמך מקור** (sticky source preview on desktop) and **בדיקת תמלול** (per-result cards with separate **אימות תמלול** and **עריכת טקסט** zones).
- Technical metadata, admin meta, and **ריצת Transkribus אחרונה** demoted into native `<details>` blocks; verify/reject actions stay visible outside collapsed sections.
- **No** workflow semantic changes (verify/reject/edit POST contracts unchanged). **No** coordinate/hover highlighting — that remains future work and requires persisted geometry (PAGE XML / ALTO / line polygons).

### OCR review lifecycle (implemented)

**Automatic success** (`run_worker._save_htr_results`):

- **`DocumentTextResult.status=NEEDS_REVIEW`** for all successful automatic OCR/HTR persistence (Gemini, Transkribus, any worker success path).
- **`verification_status=UNVERIFIED`** on that path.
- **`NEEDS_REVIEW`** = usable/displayable text requiring human review before ground-truth use; **not** a technical failure (distinct from **`FAILED`**).
- **`FAILED`** remains for pipeline/dispatch/routing failures.

**Human ground truth:** **`verification_status=VERIFIED`** is the human-approved layer. **`SUCCEEDED`** remains a valid enum for future trusted/manual paths; **current automatic worker persistence normally uses `NEEDS_REVIEW`**.

**Review reasons (worker):**

- **`AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW`** — policy-level on every automatic success.
- **`NEEDS_REVIEW_FLAG`** — only when **`HtrResult.needs_review=True`** (not the generic policy reason).
- **`MIN_TEXT_LENGTH`**, **`HAS_UNCLEAR`**, engine-provided reasons — content/engine signals; deduplicated with order preserved.

**`Document.processing_state_user` rollup:**

- **Usable/displayable** row: **`status`** in **`SUCCEEDED`** or **`NEEDS_REVIEW`**, non-empty stripped **`text`**.
- **`READY`** = all expected outputs exist and are usable — **not** human-verified. Valid: parent **`READY`** with child rows **`NEEDS_REVIEW`** + **`UNVERIFIED`**.
- **`PARTIAL`** = missing, incomplete, failed, or unusable expected outputs — **not** merely review pending. **`NEEDS_REVIEW` alone does not force `PARTIAL`** when expected usable rows exist.
- **`FAILED`** when all expected rows exist and all are **`FAILED`**.

### Hebrew `DocumentTextResult` types (current accepted behavior)

- **Worker** (`_save_htr_results`): Hebrew documents persist **both** `SOURCE_TEXT` and `HEBREW_TEXT` (same text/status per run; status normally **`NEEDS_REVIEW`** on automatic success).
- **Rollup** (`expected_outputs`): Hebrew **`READY`** requires **`HEBREW_TEXT` only** (usable per rollup rules); `SOURCE_TEXT` is not part of that expectation list.
- **Open decision (future):** whether to keep both rows long-term — document only; no behavior change in unrelated work.

### Non-Hebrew `PARTIAL` (intentional)

- Worker persists **`SOURCE_TEXT` only**; `expected_outputs` expects **`SOURCE_TEXT` + `HEBREW_TEXT`**. Missing translation → **`PARTIAL`**, not OCR failure. Do not fix opportunistically.

### Blockers before broader Transkribus use

**Schema + wiring (PR1–PR2, done):** **`TranskribusRun`** records one Transkribus processing attempt per VS-Archive document. **`TranskribusAdapter`** creates/updates rows through **`transkribus_run_persistence`**. **`run_worker`** passes generic **`document_id`** into **`transcribe_pages`**; no provider branches in the worker.

Cleanup/retention now has a **V1 dry-run reporting command** (`report_transkribus_cleanup`) for local operator visibility only. **Remote deletion, row deletion, and automation remain deferred.** Do **not** broaden Transkribus routing beyond **Hebrew handwritten** until those broader decisions are made or explicitly deferred.

### Near-term PR sequence

1. ~~Trp identity schema + persistence wiring~~ → **done (PR1 + PR2)**.
2. ~~Duplicate upload guard (dev upload mode)~~ → **done (PR3)**.
3. ~~Recognition-only retry V1 (dev/staging recovery)~~ → **done** (see section below).
4. ~~Cleanup / retention V1 dry-run reporting~~ → **done**.
5. Remote deletion / automation later, only after API behavior is verified and explicitly approved.
6. Broader production routing only if explicitly approved.

---

## TranskribusRun — remote identity schema (PR1)

**Decision:** Persist Transkribus processing attempts in a dedicated **`TranskribusRun`** model (one row per attempt), not as nullable fields on **`Document`**.

**Scope (PR1):** Django model + migration + read-only admin + model tests only. **No** adapter, engine, worker, or routing changes.

### Fields (summary)

- **`document`** FK (CASCADE); multiple runs per document allowed.
- **`status`:** `STARTED` | `UPLOADED` | `RECOGNITION_STARTED` | `SUCCEEDED` | `FAILED` — **Trp attempt lifecycle only**.
- **`mode`:** `UPLOAD_CREATED` | `EXISTING_SERVER`.
- **`collection_id`**, **`model_id`** — required snapshot at attempt start.
- **`remote_doc_id`** — nullable; unknown at upload **`STARTED`** until ingest completes; may remain null on ingest failure.
- **`pages_query`** — `CharField(max_length=512)`; nullable until known.
- **`page_index_to_page_nr`** — nullable JSON (upload mode).
- **`upload_id`**, **`ingest_job_id`**, **`recognition_job_id`** — nullable (upload fields N/A for existing-server mode).
- **`engine_runtime`** — nullable; correlates with **`DocumentTextResult.engine`** on terminal success (PR2).
- **`error_code`**, **`error_details`** — nullable; Trp attempt failure only.

### Status layer separation (do not conflate)

**`TranskribusRun.status`** does **not** replace or drive:

- **`Document.processing_state_user`**
- **`DocumentTextResult.status`** (e.g. automatic success → **`NEEDS_REVIEW`**)
- **`DocumentTextResult.verification_status`**

**`TranskribusRun.SUCCEEDED`** is **not** human verification. A successful Trp run may still yield **`NEEDS_REVIEW`** + **`UNVERIFIED`** text and a parent document **`READY`** when rollup expectations are met.

Do **not** store Trp remote ids or job ids in **`review_reasons`**.

### Persistence wiring (PR2)

- **`transkribus_run_persistence.py`:** ORM-only helpers (`start_run`, `mark_uploaded`, `mark_recognition_started`, `mark_succeeded`, `mark_failed`).
- **`TranskribusAdapter`:** stepwise engine calls + lifecycle updates; requires **`document_id`** kwarg from **`transcribe_pages`**.
- **`transkribus_engine`:** **`PylaiaTranscriptionOutcome`** + **`complete_pylaia_transcription_after_job`**; **`pylaia_transcribe_document_with_session`** return type is the dataclass (tuple preserved on **`transcribe_existing_server_document`** / **`upload_then_transcribe_page_images_with_pylaia`** wrappers).
- **`TranskribusRun.status=SUCCEEDED`** is Trp attempt success only — does not change **`DocumentTextResult.status=NEEDS_REVIEW`**, **`verification_status`**, or rollup.
- **`dev_transkribus_transcribe`** unchanged (no **`document_id`** → no **`TranskribusRun`** rows from CLI unless added later).

### Deferred (post-PR2)

- Product/admin reprocess workflow, general re-run on **`TranskribusRun.SUCCEEDED`**, cleanup/retention.
- Production **`OCR_ROUTES`** expansion.

---

## Transkribus — recognition-only retry V1 (recovery)

**Decision:** Dev/staging **recovery-only** path to re-run PyLaia on an existing Trp **`remote_doc_id`** when upload-created ingest already succeeded but recognition/transcript did not complete cleanly. **Not** general reprocess and **not** for re-OCR after a successful Trp attempt.

### Env flag (dev/staging only)

- **`TRANSKRIBUS_RECOGNITION_ONLY_RETRY`** (default **false**) → **`WorkerEnvConfig.transkribus_recognition_only_retry`**
- When **true** and a reusable source run exists → recognition-only (no **`/uploads`** ingest).
- When **true** but no reusable source run → unchanged PR3 behavior (block or first upload).
- **Not** a product/admin reprocess mechanism.

### Precedence vs force upload

- **`TRANSKRIBUS_FORCE_REPROCESS=true`** → full upload, **new** Trp document (may orphan prior docs). **Wins** if both flags are set.
- Recognition-only → **no** upload, reuse existing **`remote_doc_id`**.

### Reusable source run (`find_reusable_upload_run`)

All must match:

- **`mode=UPLOAD_CREATED`**, same **`document_id`**, **`collection_id`**, **`model_id`** (stripped)
- non-empty **`remote_doc_id`** and **`pages_query`** (after strip)
- **`status`** in **`FAILED`**, **`UPLOADED`**, **`RECOGNITION_STARTED`** only

**Excluded from V1:**

- **`SUCCEEDED`** (successful Trp attempt — use force reprocess for a new doc if needed)
- **`STARTED`** without remote identity; **`FAILED`** without **`remote_doc_id`**; missing **`pages_query`**

Returns most recent qualifying row (`-created_at`, `-id`).

### Guards (fail fast, `EnginePermanentError`, no HTTP)

- **`DocumentTextResult.verification_status=VERIFIED`** for the document → block (no force override in V1).
- If source has **`page_index_to_page_nr`**: current **`PageImage[]`** count must equal mapping entry count (does not prove byte equality).

### Behavior

- **`TranskribusAdapter._execute_dev_upload`**: if force → full upload; elif recognition-only + reusable source → **`_execute_dev_recognition_only`**; else PR3 guard + full upload.
- Creates a **new** **`TranskribusRun`** attempt row; copies upload metadata from source; **does not mutate** source row.
- Skips **`run_trp_upload_page_images_through_ingest`**; PyLaia + transcript via existing engine helpers.
- Worker persistence unchanged: automatic success → **`NEEDS_REVIEW`** + **`UNVERIFIED`**.

### Deferred (post–recognition-only V1)

- Re-run on **`SUCCEEDED`** source runs, staleness TTL, content-hash / file-changed detection, cleanup automation, product reprocess API.

---

## Transkribus — transient PyLaia workdir failure retry (recognition-only, in-adapter)

**Decision:** Treat the transient PyLaia decode-node failure whose TrpServer job `description` matches **"Could not create workdir at: /tmp/HTR/PyLaia/"** as **retryable** instead of a terminal OCR failure. QA observed it on two Hebrew handwritten documents (`remote_doc_id` 16537736, 16539496); the same `remote_doc_id`/`pages`/model recovered on manual re-run, and both Bearer and session-cookie auth succeeded, so the root cause is transient Transkribus/PyLaia decode-node infrastructure, **not** auth mode.

**Current behavior:**

- `transkribus_engine.is_retryable_pylaia_workdir_failure(description)` classifies the signature. In `poll_job_until_done`, a terminal job failure matching it raises **`TranskribusRetryableError`** (→ `EngineRetryableError`); all other job failures stay **`TranskribusPermanentError`** (→ `EnginePermanentError`).
- `transkribus_engine.run_recognition_with_workdir_retry(...)` runs recognition with a **bounded** retry of **only** this signature: each attempt issues a **new** PyLaia recognition job against the **same** server document (`remote_doc_id`/`pages_query`) on the same logged-in session — **no new `/uploads` ingest**, so it cannot create duplicate Trp documents. Other retryable errors (timeouts, 5xx, polling timeout) and all permanent errors are re-raised on first occurrence (behavior unchanged).
- All three `TranskribusAdapter` recognition paths (existing-server, recognition-only, dev-upload-created) route through this helper. Retry budget reuses the existing generic worker config: `MAX_RETRIES` (attempts) and `RETRY_DELAY_SECONDS_1`/`RETRY_DELAY_SECONDS_2` (delays); **no new env vars**.
- The retry is consumed **inside** a single adapter `execute()` (one `TranskribusRun`, latest `recognition_job_id` persisted). The worker is **unchanged and provider-agnostic**: it persists a terminal **`FAILED`** `DocumentTextResult` only **after** the budget is exhausted (existing failure path, `engine="ocr-dispatch"`, `engine_key=TRANSKRIBUS`). **No** Gemini fallback.
- Safe diagnostics on the workdir failure log only coarse, non-sensitive job fields (job id, state, `moduleUrl`, `clientId`, first line of `description`, `remote_doc_id`, `pages_query`, attempt counter). No tokens, cookies, image bytes, OCR text, presigned URLs, request bodies, or secrets.

**Out of scope / deferred:** broad worker-level retry, SQS/visibility/backoff/DLQ redesign (still deferred), fetch-existing-PAGE-XML-only recovery, admin reprocess action, and changing session-cookie vs Bearer auth (both verified working).

### Runtime envelope vs SQS visibility (Known limitation)

- **Worst-case runtime (current defaults `MAX_RETRIES=2`, `RETRY_DELAY_SECONDS_1=60`):** the bounded recognition retry worst-case is **~31 min** (attempt 1 poll ≤ `POLL_MAX_WAIT_SEC`=900s + one `RETRY_DELAY_SECONDS_1`=60s sleep + attempt 2 poll ≤ 900s). Whole-document worst-case, including the upload ingest poll (≤ 900s), is **~46 min dominant**, and potentially higher with per-call HTTP timeouts (`DEFAULT_HTTP_TIMEOUT_SEC`=60s for login, create, each PUT, each metadata/transcript fetch).
- **At `MAX_RETRIES=2`, `RETRY_DELAY_SECONDS_2` is currently unused** — two attempts means exactly one inter-attempt sleep (`RETRY_DELAY_SECONDS_1`); `RETRY_DELAY_SECONDS_2` would only apply to a third attempt.
- **Effective SQS visibility timeout is currently 300s** because `run_worker._receive_one` passes `VisibilityTimeout=300` on `receive_message`, which overrides the queue default of 10 minutes (`data_stack.py` `visibility_timeout=Duration.minutes(10)`).
- **The visibility overrun pre-dates this PR:** a single Transkribus poll can wait up to `POLL_MAX_WAIT_SEC`=900s, already ~3× the 300s visibility window, independent of any retry.
- **This PR widens the existing window but does not introduce a new concurrent-processing hazard under current infrastructure**, because the worker service is capped at **`max_capacity=1`** (`app_stack.py`). With a single consumer there is no concurrent re-delivery; re-processing only happens sequentially after a worker task dies (spot reclaim / deploy / nightly stop), and that path is guarded by the PR3 duplicate-upload guard.
- **Before increasing worker `max_capacity` above 1**, add a visibility heartbeat (`ChangeMessageVisibility`) and/or a DLQ / `maxReceiveCount` design first. At >1 worker the visibility overrun becomes a genuine concurrent-duplicate-processing hazard (true with or without this PR). This remains part of the deferred retry/visibility/DLQ redesign.

---

## Transkribus — duplicate upload guard (PR3)

**Decision:** In dev **upload-created** mode, block a second Trp upload for the same VS-Archive document unless an explicit dev env override is set. **Guard only** — no reuse of existing `remote_doc_id`, no cleanup, no routing changes.

### Match key

A prior run blocks a new upload when all match:

- same **`document_id`**
- **`mode=UPLOAD_CREATED`**
- same **`collection_id`** and **`model_id`** (stripped, same normalization as `start_run`)

Do **not** use `pages_query`, page mapping, S3 key, or content hash in PR3.

### Blocking statuses

Block when any matching prior run has:

- `STARTED`, `UPLOADED`, `RECOGNITION_STARTED`, or `SUCCEEDED`
- `FAILED` with non-empty **`remote_doc_id`**

Do **not** block `FAILED` with **`remote_doc_id`** null or blank (upload may be retried).

**`STARTED` blocks** even if the row may be stale after a worker crash. **No** TTL/staleness logic in PR3 — ops may use **`TRANSKRIBUS_FORCE_REPROCESS`** or manual DB fix for stuck `STARTED` rows.

### Behavior

- **`find_blocking_upload_run`** in **`transkribus_run_persistence.py`** returns the most recent blocking row (any blocking row via ordered query — not “latest row only”).
- **`TranskribusAdapter._execute_dev_upload`** calls it after upload config validation and **before** `start_run` / **`requests.Session`** / login / HTTP.
- On block: **`logger.warning`** with `document_id`, `blocking_run_id`, `blocking_run_status`, `blocking_remote_doc_id`, `collection_id`, `model_id`; raise **`EnginePermanentError`** with actionable text; **no** new **`TranskribusRun`** row.
- **`EXISTING_SERVER`** mode is **not** guarded.

### Force override (dev/staging only)

- **`TRANSKRIBUS_FORCE_REPROCESS`** (default **false**) on **`WorkerEnvConfig`** as **`transkribus_force_reprocess`**.
- When **true**, bypasses the guard and allows another Trp upload (may create **duplicate/orphan** Trp documents). **Not** a product/admin reprocess mechanism — no Document fields, admin actions, API params, or worker kwargs in PR3.

### Status layers (unchanged)

Blocked upload does not change OCR review lifecycle or rollup rules. Worker may still persist **`DocumentTextResult.FAILED`** from the adapter error. **`TranskribusRun.status`** remains Trp attempt lifecycle only.

### Deferred (post-PR3)

- Cleanup/retention automation, Trp delete API
- Staleness TTL for in-progress rows
- Product reprocess policy (file changed, admin action) beyond recognition-only V1 guards

---

## Transkribus — cleanup / retention V1 (dry-run reporting only)

**Decision:** implement a **local-only dry-run reporting command** first; do **not** delete remote Trp documents, do **not** delete `TranskribusRun` rows, and do **not** add cleanup logic to `run_worker.py`.

### Implemented in V1

- **`report_transkribus_cleanup`** reads local DB state only and reports:
  - per-`remote_doc_id` retention / review buckets
  - per-`TranskribusRun` buckets
  - stale in-progress rows based on a reporting threshold (default **24h**)
- **No** Transkribus HTTP calls, **no** delete endpoint usage, **no** local mutations.

### Current retention policy

- **Never auto-delete** from this V1 command.
- **Retain** any `remote_doc_id` referenced by:
  - **`EXISTING_SERVER`** runs
  - any document with **`DocumentTextResult.verification_status=VERIFIED`**
  - any run that remains reusable for **recognition-only retry V1**
  - the newest successful/useful remote doc in a `(document_id, collection_id, model_id)` lineage
- **Review only** buckets are operator hints for manual investigation. They are **not** deletion approvals.

### Explicit non-scope

- Remote Trp deletion
- Local `TranskribusRun` deletion / pruning
- Worker-triggered cleanup
- Admin cleanup actions
- Schema / migration changes for cleanup state

### Why this is conservative

- `remote_doc_id` is now persisted on **`TranskribusRun`**, which is enough for reporting and lineage analysis.
- Recognition-only retry can reuse an older remote Trp document, so older rows may still represent a remote document that is **in use**.
- `transkribus_engine.py` still has **no verified delete endpoint wrapper** in code, so remote deletion remains intentionally deferred.

---

## OCR/HTR routing by language and text input type

> **Historical / partially superseded.** Core routing **decisions** below remain valid. Facts that are **obsolete** are marked inline; for **current** Transkribus and execution-layer state, see **“Current state — OCR/HTR and Transkribus”** above and Transkribus PR sections below.

### Decision
OCR/HTR processing will be routed by two explicit document metadata fields:

- `Document.language`
- `Document.text_input_type`

`text_input_type` is required.

Allowed values:
- `HANDWRITTEN`
- `PRINTED`

There is no `UNKNOWN` value.

### Existing documents
Existing documents should be migrated/defaulted to:

- `HANDWRITTEN`

Reason:
The current existing document set is known to be handwritten, so this is not treated as an unknown/default guess.

### Upload behavior
Upload must require selecting `text_input_type`.

The upload form should not allow submission without choosing whether the document is handwritten or printed.

Validation strategy:
- Enforce `text_input_type` at upload/create time.
- Also validate defensively during processing/routing.
- If metadata needed for routing is missing or invalid at processing time, persist an explicit failure (`OCR_ROUTING_INVALID`) and do not silently fallback to `GEMINI/handwritten`.

### Admin editing
Admins must be able to edit `text_input_type` in the existing admin/metadata edit flow.

Do not create a separate new UI unless no relevant edit flow exists.

### Language routing
`Document.language` already uses fixed enum values:

- `he`
- `en`
- `fr`
- `ar`

Routing should use explicit entries per language and text input type.

Do not create broad language groups such as `LATIN`.

Even if English and French currently route to the same engine/prompt, they should remain separate routing table entries so they can diverge later with a one-line change.

### Routing architecture
Routing logic must live in a dedicated routing module, for example:

- `documents/services/ocr_routing.py`

The worker must not contain language-specific or text-input-specific routing logic.

The selector should return configuration only, such as:

- `engine_key`
- `prompt_variant`

The selector should not call the engine directly.

### Execution layer (OCR/HTR engines)
After routing, execution is layered as follows:

1. **Routing** (`documents/services/ocr_routing.py`): `select_ocr_route` returns `OcrRouteConfig` (`engine_key`, `prompt_variant`) only. It does not call providers.

2. **Dispatcher** (`documents/services/htr_engine.py`): `transcribe_pages` resolves the adapter by `engine_key` and calls `adapter.execute(...)`. An optional `route` argument allows the caller (worker) to pass the same route used for persistence so routing is not re-derived inside the dispatcher.

3. **Adapter registry** (`documents/services/htr_adapters/registry.py`): static map from `engine_key` to adapter implementation. Unknown keys raise `UnsupportedEngineError`.

4. **Provider adapters** (e.g. `documents/services/htr_adapters/gemini_adapter.py`, `transkribus_adapter.py`): own provider-specific execution. **Superseded:** “Transkribus is not implemented yet” — Transkribus is implemented behind dev/staging gates; static `OCR_ROUTES` remain Gemini-only unless changed.

### Route metadata vs OCR result payload
`engine_key` and `prompt_variant` are **routing metadata**. They are selected by the routing layer and carried through the worker for persistence on `DocumentTextResult`. They are **not** part of the minimal OCR result payload (`HtrResult`: text, review flags, runtime engine name, etc.).

### DocumentTextResult fields
- **`engine`**: continues to mean the runtime processing identity used for uniqueness and processing-state rollups (e.g. concrete Gemini model id, or failure-path markers such as `ocr-dispatch` / `unsupported:<key>`). Do not repurpose this field for provider routing keys.

- **`engine_key` / `prompt_variant`**: stored on `DocumentTextResult` for auditability and reproducibility. Values come from the **selected route** on success (worker-held `OcrRouteConfig`) and from route re-selection or explicit unresolved markers on failure paths.

### DocumentTextResult.OcrEngineKey schema limitation
**Superseded:** `OcrEngineKey` now includes **`TRANSKRIBUS`** (migration landed in Transkribus PR #1). Historical note: first engine addition required enum + migration.

### Deferred: `UNRESOLVED` routing-failure markers vs TextChoices
When routing metadata cannot be resolved (`OCR_ROUTING_INVALID`), failed rows persist `engine_key` and `prompt_variant` as the literal string **`UNRESOLVED`** so the outcome is explicit and avoids misleading `GEMINI` / `handwritten` fallbacks.

That sentinel is **not** listed on `DocumentTextResult.OcrEngineKey` or `OcrPromptVariant` today. Django persists it without running model validation in the normal `save()` path; forms or admin that assume only declared choices may need care.

**Future work (separate PR):** add first-class choice values, use nullable fields, or otherwise align the schema with the sentinel—out of scope for the pre-Transkribus cleanup PR.

### Gemini prompt selection
Gemini should receive a `prompt_variant` key, not arbitrary prompt text.

Gemini should choose the actual prompt internally based on that key.

The Gemini JSON output contract must remain unchanged.

Gemini-specific tuning (temperature, model candidates, quota fallback, etc.) belongs in `GeminiAdapter` and/or `gemini_engine.py`, not in generic worker retry loops.

### SQS / adapter errors (current policy)
For now:

- Adapter and processing errors are persisted as document OCR failures (`DocumentTextResult` failed rows, appropriate `error_code` where defined).
- SQS messages are **acked** after handling (including failures): there is no automatic re-drive or DLQ split based on error class.

Engine-specific retry, backoff, visibility-timeout tuning, and DLQ policy are **deferred** and not part of the current worker contract.

### Reprocessing behavior
Changing `Document.text_input_type` after a document has already been processed must not automatically trigger OCR/HTR reprocessing in this PR.

Future work:
Design an explicit reprocessing workflow.

### Non-Hebrew Hebrew translation — intentional `PARTIAL` (current behavior)
**Current behavior (accepted):**

- `run_worker._save_htr_results` persists **`SOURCE_TEXT` only** for non-Hebrew documents.
- `expected_outputs.expected_result_types_for_document` expects **`SOURCE_TEXT` + `HEBREW_TEXT`**.
- Until real Hebrew translation exists, documents stay **`PARTIAL`** — **not** an OCR failure.

Do not fix this opportunistically unless explicitly requested (translation feature).

## Transkribus integration — PR #1 (skeleton / stable connection point)

> **Historical.** PR #1 landed enum + registry + fail-fast adapter. Later PRs added live TrpServer integration, dev upload mode, and env-gated `select_ocr_route`. See **“Current state — OCR/HTR and Transkribus”** at the top of this file.

### Decision (historical)

The first Transkribus PR establishes only the **plumbing** so a second engine can exist in the same architecture as Gemini, **without** changing production routing or calling Transkribus.

### Behavior after PR #1 only (superseded)

- `DocumentTextResult.OcrEngineKey` includes **`TRANSKRIBUS`** (with migration updating the field choices).
- `TranskribusAdapter` registered with `engine_key = "TRANSKRIBUS"`.
- At PR #1: adapter raised **`EnginePermanentError`** (“not implemented yet”). **Superseded:** adapter now runs real HTTP when dev env gates are on.
- At PR #1: `OCR_ROUTES` all **GEMINI**. **Still true** for static table; dev routing override added later (env-gated, not static table entries).

### Deferred (still in force unless explicitly requested)

- Static production `OCR_ROUTES` entries for Transkribus (separate approval).
- Hybrid or fallback between engines.

## Transkribus PR #2 — Legacy TrpServer PyLaia (existing server document only)

### What this is not

- **Not** a full Transkribus integration and **not** the complete VS-Archive path: user upload → Transkribus document creation → recognition → `DocumentTextResult`.
- **`OCR_ROUTES` unchanged**; production OCR remains **GEMINI** until a later routing PR.

### Scope

- **Legacy TrpServer only** (account has no Metagrapho / processing v2 under current plan).
- **Session login:** `POST https://transkribus.eu/TrpServer/rest/auth/login` with form fields `user` and `pw` (URL-encoded). PyLaia recognition **POST** uses this session (Bearer alone insufficient for recognition in verified testing).
- **PyLaia start:** `POST /pylaia/{colId}/{modelId}/recognition` with UI-aligned query parameters; **response body is plain-text job id** (not JSON).
- **Polling:** `GET /jobs/{jobId}` until success/failure/timeout rules in code.
- **Polling constants** in `transkribus_engine.py` (`POLL_INTERVAL_SEC`, `POLL_MAX_WAIT_SEC`, default HTTP timeouts) are **dev/demo defaults**. Before **production** Transkribus is selected via **`OCR_ROUTES`**, revisit **SQS visibility timeout vs. worker polling / job duration** (and any related queue behavior) so messages are not released mid-poll or held too long. PR #2 does not change `OCR_ROUTES` or `run_worker.py`.
- **Page metadata:** `GET /collections/{colId}/{docId}/pages?pages=…` returns a **JSON array** of page objects; transcript choice uses **`jobId` / `modelId`** when present in `tsList.transcripts`, not `transcripts[0]` blindly.
- **Transcript:** `GET` transcript URL; **Bearer** (`TRANSKRIBUS_API_TOKEN`) for `files.transkribus.eu` in verified testing.
- **PAGE XML** (`PcGts` namespace) → plain text via `TextLine` / `TextEquiv` / `Unicode`; lines joined with `\n`, pages with `\n\n`.

### Safeguards

- **`TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT`** (default false): if false, **`TranskribusAdapter` raises `EnginePermanentError` before any HTTP** (no silent no-op).
- **Dev-only document id:** `TRANSKRIBUS_DEV_EXISTING_DOCUMENT_ID` — not a VS-Archive document id; names the pre-existing TrpServer doc for dev/demo.
- **Dev-only pages query:** `TRANSKRIBUS_DEV_EXISTING_PAGES` supplies the `pages=` query value. **Do not** assume `PageImage.page_index` matches TrpServer page numbers in PR #2; `PageImage[]` is **validation-only** (non-empty).
- **Config** for collection/model: `TRANSKRIBUS_COLLECTION_ID`, `TRANSKRIBUS_MODEL_ID`. **No** hard-coded account ids in code.

### Explicit non-scope (PR #2)

- **No** upload / create-document on Transkribus (**PR #3** must implement upload + page mapping from VS-Archive bytes/`PageImage[]`).
- **No** `run_worker.py` changes.
- **No** `/recognition/atr` or **`htrCITlab`** for this flow (UI used `/pylaia/.../recognition`; `htrCITlab` tied to deprecated HTR+).

### Credentials

- Do **not** log username, password, session cookies, or tokens.

## Transkribus PR #3 — Legacy `/uploads` ingest (engine-only; no adapter wiring yet)

### Decision

- **Engine layer only:** helpers, parsers, job polling rules, and **`run_trp_upload_page_images_through_ingest`** in `documents/services/transkribus_engine.py` for the Legacy **`POST /uploads` → `PUT /uploads/{uploadId}` (multipart `img`) → ingest `jobId` → `GET /jobs/{jobId}`** flow documented in the [Transkribus REST upload article](https://www.transkribus.org/blog/transkribus/docu/rest-api/upload) and **`/uploads` resources in** `https://transkribus.eu/TrpServer/rest/application.wadl` (and `?detail=true`).
- **Verified on the real Transkribus account (authenticated trace):** `POST /uploads?collId=…` returns top-level **`uploadId`**; **`PUT …/{uploadId}`** with **img-only** multipart succeeds for PNG pages; the final ingest job is **`GET /jobs/{jobId}`** with **`state=FINISHED`**, **`success=true`**, **`type=Create Document`**, **`jobImpl=UploadImportJob`**, and top-level **`docId`**; **`GET /collections/{collId}/{docId}/pages?pages=…`** returns page metadata including **`pageNr`**, **`imgFileName`** matching the synthetic upload name, and **`tsList.transcripts`** (e.g. status NEW with PAGE XML URL).
- **PR #3 does not wire the adapter** (`TranskribusAdapter` unchanged).
- **PR #3 does not add env flags** for Transkribus upload (no new `WorkerEnvConfig` / `validate_required_env` fields for this flow).
- **No** `OCR_ROUTES`, **`run_worker.py`**, **`HtrResult`**, **`DocumentTextResult.engine`** semantics, or **DB schema** for Transkribus document ids in PR #3.
- **PR #3 does not run recognition** after upload; PR #2 PyLaia / transcript flow remains separate (no duplication inside the upload orchestrator).
- **`docId`:** **verified** as **top-level** on successful **UploadImportJob** for this account; parser remains **narrow** (top-level `docId` only).
- **Job polling (shared with PyLaia and other TrpServer jobs):** terminal **success** requires **`success is True`** and either a completed **`state`** (`FINISHED`, `DONE`, or `COMPLETED`) or a **missing/blank** `state` (legacy payloads). Terminal **failure** uses explicit states **`FAILED` / `ERROR` / `CANCELLED` / `CANCELED`**, or completed `state` **without** success. **`success=false` with `CREATED`** (e.g. queue description) is **non-terminal**—keep polling. Do **not** treat **`success=false` alone** as failure while the job is still queued or running. **`nrOfErrors` > 0** is terminal failure **only** when `state` is **not** in the non-terminal set (`''`, `CREATED`, `RUNNING`, `WAITING`, `QUEUED`); while still in those states, polling continues (covers in-progress noise without aborting early).
- **Production intent (upload path):** one VS-Archive **document upload / processing run** that uses Transkribus upload should create **one new** Transkribus document inside the configured **`TRANSKRIBUS_COLLECTION_ID`** via **`POST /uploads`** (new `docId`). Do **not** append pages to a fixed, pre-existing Transkribus `docId` as part of that design—that remains the separate **existing-server-document** dev path only.
- **No** cleanup/retention in PR #3. **Once adapter/worker use upload in production**, dev uploads may **accumulate** and retries may create **duplicate** server-side documents until an explicit retention/dedup policy is designed.

### Implemented in code (PR #3)

- **`run_trp_upload_page_images_through_ingest`:** descriptor → `POST /uploads` → ordered `PUT` of each PNG → last non-empty **`jobId`** → **`poll_job_until_done`** → **`parse_doc_id_from_successful_trp_job`** → **`fetch_pages_metadata`** → **`strict_map_page_index_to_trp_page_nr`** → **`TrpUploadOutcome`** (includes `pages_query` for follow-on calls).

### Deferred

- **Adapter / worker** calling upload orchestration then PR #2 recognition in one product path (and any new env flags for that) remains a **follow-up PR**.
- **Retention/dedup** for Transkribus-side documents.

## Transkribus PR #4 — Engine-only upload + PyLaia composition (`transkribus_engine.py`)

### Decision

- **`pylaia_transcribe_document_with_session`:** behavior-preserving extraction of the PyLaia start → poll → pages → transcripts → PAGE XML text path (formerly inline in **`transcribe_existing_server_document`**). **`transcribe_existing_server_document`** now only creates a session, logs in, and calls this helper—no duplicate recognition logic.
- **`upload_then_transcribe_page_images_with_pylaia`:** engine-only composition: session/login → **`run_trp_upload_page_images_through_ingest`** (new Trp **`docId`** + **`pages_query`**) → same **`pylaia_transcribe_document_with_session`** → returns **`HtrResult`** with the same **`engine_name`** pattern as **`TranskribusAdapter`** (`transkribus-pylaia:{model_id}`). **No** adapter wiring, **no** new env flags, **no** `OCR_ROUTES` / **`run_worker.py`** / **`env_validation`** / **models** / **migrations** changes in this PR.
- **`HtrResult` import:** `HtrResult` lives in `documents/services/htr_adapters/base.py` as the **shared HTR/OCR result payload** (also the return type of **`htr_engine.transcribe_pages`**). Importing it in **`transkribus_engine`** is a dependency on that **minimal dataclass module**, not on **`TranskribusAdapter`** or adapter wiring. This PR’s upload+recognition orchestration **constructs `HtrResult` at the engine boundary** for convenience; the adapter’s **`execute`** path still builds **`HtrResult`** from **`transcribe_existing_server_document`**’s tuple as before—**`needs_review = bool(review_reasons)`** matches **`TranskribusAdapter`** exactly for consistency.
- **Timeouts (behavior-preserving for PR #2 path):** **`transcribe_existing_server_document`** passes **`timeout_sec=DEFAULT_HTTP_TIMEOUT_SEC`** (60) into **`pylaia_transcribe_document_with_session`**, which forwards it to **`start_pylaia_recognition`**, **`poll_job_until_done`** (per-`get_job` request), **`fetch_pages_metadata`**, and **`fetch_transcript_xml`**. That matches the prior implicit default (**60s**) on each of those calls; **`login_trp_server`** is still invoked **without** an explicit timeout on the existing-document entrypoint (unchanged from pre-refactor). **`upload_then_transcribe_page_images_with_pylaia`** passes its **`timeout_sec`** through login, upload ingest, and the shared PyLaia helper for one consistent knob on that path only.
- **Production intent unchanged:** one VS-Archive document flow using this path should correspond to **one new** Transkribus document in **`TRANSKRIBUS_COLLECTION_ID`** (via upload), not appending pages to a fixed pre-existing **`docId`**.
- **Still engine-only:** no **adapter** wiring, no **env flags**, no **`OCR_ROUTES`** or **`run_worker.py`** edits, **no production routing**; **adapter** integration of **`upload_then_transcribe_page_images_with_pylaia`** remains **deferred**.

### Deferred

- **Retention/dedup** for Transkribus-side documents created by upload.

## Transkribus PR #5 — `TranskribusAdapter` dev upload mode (adapter wiring only)

### Decision

- **`TRANSKRIBUS_DEV_UPLOAD_MODE`** (default false): when true, **`TranskribusAdapter.execute`** calls **`upload_then_transcribe_page_images_with_pylaia`** with the **`PageImage[]`** from the caller (upload → new Trp **`docId`** in **`TRANSKRIBUS_COLLECTION_ID`** → PyLaia → **`HtrResult`**). Still **not** production routing: **`OCR_ROUTES`** and **`run_worker.py`** are unchanged; no new **`DocumentTextResult`** semantics.
- **`TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT`** path is **unchanged** (same env vars, same **`transcribe_existing_server_document`** call).
- The two flags are **mutually exclusive**. If both are true, **`TranskribusAdapter`** raises **`EnginePermanentError`** before any HTTP or engine call.
- **Default:** if **neither** flag is true, **`TranskribusAdapter`** still fails fast before HTTP (message names both env toggles).
- **Upload dev mode** requires **`TRANSKRIBUS_USERNAME`**, **`TRANSKRIBUS_PASSWORD`**, **`TRANSKRIBUS_API_TOKEN`**, **`TRANSKRIBUS_COLLECTION_ID`**, **`TRANSKRIBUS_MODEL_ID`** only; it does **not** require **`TRANSKRIBUS_DEV_EXISTING_DOCUMENT_ID`** or **`TRANSKRIBUS_DEV_EXISTING_PAGES`** (those remain for the separate existing-document dev path).
- **Retention / duplicate Transkribus documents** on retries and **production route selection** for Transkribus remain **deferred**.

## Transkribus — adapter dev upload smoke verification & PyLaia auth (recorded findings)

This section records **local smoke-test results against real Legacy TrpServer**. It does **not** mean Transkribus is production-enabled: **`OCR_ROUTES`** still do not select Transkribus for production documents, and **`run_worker.py`** was not changed for this path.

### End-to-end adapter dev upload mode (verified)

With **`TRANSKRIBUS_DEV_UPLOAD_MODE=true`** and a **`TranskribusAdapter.execute([PageImage], …)`** call against a real account, the following chain was observed to complete successfully:

**`PageImage[]` → `TranskribusAdapter` → upload new document into `TRANSKRIBUS_COLLECTION_ID` → `UploadImportJob` yields top-level `docId` → `GET …/pages` metadata (`pageNr`) → PyLaia recognition start → poll → transcript XML fetch → PAGE XML → plain text → `HtrResult`.**

Smoke output (technical check only): **`engine_name`** `transkribus-pylaia:564149`, **`needs_review`** false, **`review_reasons`** empty, **`text_length`** 6, short/low-quality text preview from a **synthetic** PNG. That outcome **validates wiring** (adapter → upload → recognition → transcript → parse → `HtrResult`); it is **not** archival OCR quality validation. A later smoke run on a **real local Hebrew image** likewise confirmed **technical** end-to-end execution only (same scope as below).

### PyLaia `POST /pylaia/{colId}/{modelId}/recognition` — media type and auth

**Working request shape on real Legacy TrpServer:**

- **Session:** logged-in **`login_trp_server`** session cookie (same flow as upload/create).
- **Headers:** `Accept: application/json, text/plain, */*` only.
- **No** `Authorization` header on this POST.
- **No** `Content-Type` and **no** request body (`json` / `data` omitted).

**Diagnostics (same account):**

- Sending **`Content-Type`** / a **body** on PyLaia start produced **HTTP 415**.
- Sending **`Authorization: Bearer`** (with or without an existing session cookie) produced **HTTP 401**.
- **Session cookie only** (no Bearer on this POST) returned **HTTP 200** and a **plain-text job id** in the response body.

**Auth split (unchanged intent in code):**

- **Upload / create-document / PyLaia start / Trp `GET` jobs & pages:** Legacy **session** after **`login_trp_server`**.
- **Transcript XML fetch** (`files.transkribus.eu` / transcript URLs): **Bearer** token, as already implemented in **`fetch_transcript_xml`**.

### Deferred validation: smoke scope vs integration fidelity

The successful adapter **dev upload-mode** smoke test(s) validate **technical connectivity and end-to-end execution only**. They do **not** yet validate **transcript fidelity** between what Transkribus shows in the **UI / native output** and VS-Archive’s **parsed `HtrResult`**. We have **not** yet checked whether the adapter/parser preserves **all lines**, **page order**, **transcript selection** (e.g. which `tsList` entry), **encoding**, or **exact text** as produced by Transkribus. This should be revisited **before production routing** or **before relying on Transkribus outputs at scale**.

This deferred work is **separate from OCR/HTR model quality**: we are **not** claiming to have evaluated **word-level correctness** of the Transkribus PyLaia model in the smoke step. The open gap is **integration fidelity**—whether VS-Archive **receives, parses, and (when persisted) reflects** what Transkribus actually produced for the chosen transcript path.

### Still deferred / not implied by the smoke test

- **No** production **`OCR_ROUTES`** entry selecting Transkribus; **no** production “Transkribus is the default engine” claim.
- **No** **`run_worker.py`** changes for this integration step.
- **No** cleanup / retention policy for Transkribus-side documents created by dev upload or retries.
- **No** DB persistence of Transkribus **`docId`** on the VS-Archive `Document` row.
- **No** quality evaluation on real archival handwriting; **no** layout / line-polygon policy decision for production documents beyond what the current PyLaia path already does.

## Transkribus PR — dev-only dispatch smoke command (`dev_transkribus_transcribe`)

### Decision

- Add **`documents/management/commands/dev_transkribus_transcribe.py`**: a **dev/staging-only** Django management command that reads a **local** image or PDF path, runs **`extract_pages`** (same helper as the worker), builds an **explicit** **`OcrRouteConfig`** with **`engine_key=TRANSKRIBUS`** and a **`prompt_variant`** from CLI (default **handwritten**), loads **`WorkerEnvConfig`** via **`validate_required_env()`**, and calls **`htr_engine.transcribe_pages(..., route=…, worker_env=cfg)`** so the normal **dispatcher + registry + adapter** path runs **without** going through **`select_ocr_route`** or **`OCR_ROUTES`**.
- **Safety guard:** the command refuses to run unless **`--confirm-create-transkribus-doc`** is passed, with a clear message that it **creates a real Transkribus document** and **does not clean up**.
- **Env:** requires **`TRANSKRIBUS_DEV_UPLOAD_MODE=true`** and the same **upload-mode** credential/collection/model vars as **`TranskribusAdapter`** dev upload mode (**username/password**, **API token**, **collection id**, **model id**). **`GEMINI_API_KEY`** and other worker vars still load via **`validate_required_env`** (unchanged global worker env contract).
- **Automated tests** mock **`transcribe_pages`** / env loading at the command boundary; **no live Transkribus** calls in CI.

### Explicit non-scope

- **No** changes to **`OCR_ROUTES`**, **`run_worker.py`**, **`htr_engine.py`** behavior, or production routing.
- **No** Gemini→Transkribus fallback, hybrid routing, **DB schema**, or persistence of Transkribus **`docId`**.
- **No** cleanup/retention for server-side documents created when the command is run manually against real TrpServer.

## Transkribus — production-gated Hebrew handwritten OCR routing (`select_ocr_route`)

### Decision

- **`documents/services/ocr_routing.py`** treats **`language=he`** + **`text_input_type=HANDWRITTEN`** as **Transkribus-only**. That pair never selects Gemini.
- **`ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN=true`** → `select_ocr_route` returns **`engine_key=TRANSKRIBUS`** with the handwritten prompt variant for that pair.
- **`ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN=false`** (default) → `select_ocr_route` raises **`ValueError`** with a clear message that Hebrew handwritten documents require Transkribus and the route flag is disabled. This is a **routing/configuration failure**, not a Gemini fallback case.
- The new flag means the Hebrew handwritten Transkribus route is **operationally enabled in this environment**. It is **not** a “Gemini vs Transkribus” switch and it does **not** repurpose existing dev/staging upload/recovery flags.
- **`TRANSKRIBUS_DEV_OCR_ROUTE`** was the older dev-only route-selection flag. It is now **obsolete** for `select_ocr_route` and should not be used as a second selector for Hebrew handwritten routing.
- Existing **`TRANSKRIBUS_DEV_UPLOAD_MODE`**, **`TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT`**, **`TRANSKRIBUS_FORCE_REPROCESS`**, and **`TRANSKRIBUS_RECOGNITION_ONLY_RETRY`** remain adapter/execution controls only.
- All other valid `(language, text_input_type)` pairs continue to return the normal Gemini route from **`OCR_ROUTES`**.
- **No** Gemini→Transkribus fallback, **no** Transkribus→Gemini fallback, **no** hybrid routing, **no** **`run_worker.py`** edits, **no** **`HtrResult`** changes, and **no** models/migrations for this step.

This routing gate still does not add cleanup/retention for Transkribus-side documents; retries may create duplicate Trp documents and cleanup remains deferred.

### Operational note

If **`ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN=true`** and Transkribus execution later fails (credentials missing, upload failure, recognition failure, timeout, transcript fetch failure, or other adapter/engine error), the worker persists the failure through the existing lifecycle. It does **not** retry by switching to Gemini.

## Transkribus — manual dev/staging SQS worker smoke (verified)

### Record (manual confirmation)

A **full real** dev/staging path was exercised end-to-end (no mocks): **SQS** → **`run_worker`** → **S3** download → **`extract_pages`** → **`select_ocr_route`** (env-gated **TRANSKRIBUS**) → **`transcribe_pages`** → **TranskribusAdapter** dev **upload** mode → **Legacy TrpServer** → **`DocumentTextResult`** persistence.

**Document (example run):**

- **`id=9`**
- **`title`:** `ניסיון נוסף מהסלולרי`
- **`upload_status=UPLOADED`**
- **`language=he`**
- **`text_input_type=HANDWRITTEN`**
- **`mime_type=image/jpeg`**
- **`file_s3_key=documents/9/original.jpeg`**

**Environment:**

- **Historical route flag:** **`TRANSKRIBUS_DEV_OCR_ROUTE=true`**
- **Current equivalent route flag:** **`ENABLE_TRANSKRIBUS_HEBREW_HANDWRITTEN=true`**
- **`TRANSKRIBUS_DEV_UPLOAD_MODE=true`**
- **`TRANSKRIBUS_USE_EXISTING_SERVER_DOCUMENT`** unset / **false**
- **Dev** SQS queue and **dev** S3 object (object existed at **`file_s3_key`**)

**SQS queue attributes:**

- **Before worker:** **`ApproximateNumberOfMessages=1`**, **`ApproximateNumberOfMessagesNotVisible=0`**
- **Worker command:** `poetry run python manage.py run_worker --once --max-messages 1 --wait-seconds 20`
- **After worker:** **`ApproximateNumberOfMessages=0`**, **`ApproximateNumberOfMessagesNotVisible=0`**

**Persistence:**

- **`Document.processing_state_user=READY`**
- **`DocumentTextResult`** rows for **`SOURCE_TEXT`** and **`HEBREW_TEXT`**
- For **both** rows: **`engine=transkribus-pylaia:564149`**, **`engine_key=TRANSKRIBUS`**, **`prompt_variant=handwritten`**, **`status=SUCCEEDED`**, **`verification_status=UNVERIFIED`**, **`error_code` None**, **`text` length 1205**

  > **Historical smoke snapshot.** Observed **`status=SUCCEEDED`** at the time of this run. **Current worker policy** persists automatic OCR/HTR success as **`NEEDS_REVIEW`** (see “Current state — OCR review lifecycle” above).

### Meaning

This run **validates wiring and operability** of the **dev/staging** worker pipeline with **real** SQS, S3, DB, and **Legacy TrpServer** for the gated **Hebrew handwritten** route. It is **not** a claim about production readiness, default routing, or OCR quality.

### Limitations (unchanged policy)

- Still **dev/staging gated** only (**env flags**); **no** production-default Transkribus routing.
- **`OCR_ROUTES`** static table remains **Gemini-only**; production behavior with flags **off** is unchanged.
- **No** cleanup/retention for Transkribus-side documents; retries may create **duplicate** Trp documents.
- **No** Transkribus **`docId`** persisted on the VS-Archive **`Document`**.
- **No** transcript **quality** or **fidelity** validation is implied by this smoke.
- **No** Gemini→Transkribus **hybrid** or **fallback** is implemented.

## Transkribus — operational safety (dev/staging routing)

This section records **operational risks and semantics** for env-gated **Transkribus** routing and **upload** mode. It does **not** change product behavior; it informs **broader dev/staging use** and future schema/tooling work.

### Server-side documents and VS-Archive state

1. **Every** Transkribus **upload-mode** run that reaches Legacy TrpServer **creates a new server-side Transkribus document** (new **`docId`** on the Trp side for that upload path).

2. VS-Archive currently persists the Transkribus **`docId`** as **`TranskribusRun.remote_doc_id`**. It is **not** persisted on the parent **`Document`** row.

3. **Reprocessing** the same VS-Archive **`Document`** (e.g. another **`PROCESS_DOCUMENT`** message for the same **`document_id`**) can therefore create **additional** Transkribus documents—**duplicates on Trp** are possible even when VS-Archive still represents “one” archive document.

### How VS-Archive rows interact with Trp duplicates

4. **`DocumentTextResult`** persistence uses **`update_or_create`** keyed by **`(document, result_type, engine)`** (see model **`UniqueConstraint`** / worker **`_save_htr_results`**). **`engine`** is the **runtime** identity (e.g. **`transkribus-pylaia:{model_id}`**). So:
   - If a **second** run produces the **same** **`engine`** string, the **same** result row(s) may be **updated in place** (new text, same key)—while Trp may still have received a **new** upload document on the second run.
   - If **`engine`** differs between runs (e.g. different model id in **`engine_name`**), **additional** rows can appear for the same **`result_type`** under different **`engine`** values.

### Cleanup and retention

5. **Cleanup / retention V1** is a **dry-run local reporting command** only. It classifies local rows / remote-doc lineages for operator review but **does not delete** remote Trp documents and **does not delete** local rows.

### Where not to stash `docId` (until an approved schema PR)

6. Do **not** store Transkribus **`docId`** in **`DocumentTextResult.error_details`** or **`review_reasons`**—those fields have **failure** / **review-reason** semantics and are a poor fit for external identifiers.

   Current persistence is **`TranskribusRun.remote_doc_id`**. If future product requirements need a different home, prefer **either**:
   - **Explicit nullable field(s) on `Document`** when the product truly needs one current linked Trp doc, **or**
   - **A dedicated link / history model** beyond `TranskribusRun` if cleanup state needs richer lifecycle tracking,

   **Both require a separate, explicitly approved schema / migration PR** (out of scope for doc-only updates).

### Status and verification semantics

7. **`verification_status=UNVERIFIED`** means **human verification** in VS-Archive has **not** been completed. A successful dev smoke **does not** imply **OCR quality**, **transcript fidelity**, or agreement with the Transkribus UI.

   **Current worker policy:** automatic OCR/HTR success persists **`status=NEEDS_REVIEW`** (not a technical failure). **`SUCCEEDED`** remains valid in the schema; older smoke notes that record **`SUCCEEDED`** are **historical snapshots** of behavior at run time.

### Decisions still required before broader use

8. Before expanding dev/staging volume or moving toward production routing, we still need explicit decisions on:
   - Whether **`TranskribusRun.remote_doc_id`** remains sufficient for audit / dedupe / cleanup, or whether richer cleanup state is needed later.
   - **Whether** to **allow reprocessing** the same **`Document`** through Transkribus upload mode (and under what guards, e.g. **`VERIFIED`** results, file changed, explicit admin action).
   - **Whether** to add **destructive cleanup tooling** (e.g. Transkribus delete API calls) once API behavior is verified.
   - **Automatic OCR review lifecycle:** implemented in worker — see **“OCR review lifecycle (implemented)”** in Current state above (worker-wide **`NEEDS_REVIEW`**, not Transkribus-only).

**Remote deletion / reprocess / cleanup automation** remain undecided and unimplemented.

## Upload JSON endpoints — CSRF protection (session auth)

**Decision:** Admin upload JSON endpoints (`POST /api/uploads/create/`, `complete/`, `parts/<order_index>/complete/`, `finalize/`) use **`@login_required`**, **`_require_admin` (staff)**, and **Django CSRF middleware** — not `@csrf_exempt`.

**Current behavior:**

- These endpoints are **browser-session admin flows** only. The upload page renders its own `{% csrf_token %}` hidden input; client JS sends `X-CSRFToken` from that input first, with cookie fallback.
- **`@csrf_exempt` was removed** from all four upload JSON views; no compensating exemption remains.
- Auth requirements are unchanged: unauthenticated → login redirect; non-staff → 403.

**Out of scope at CSRF PR time (partially addressed June 2026):** upload-completion MIME validation and S3 HeadObject metadata verification are documented in **“Upload hardening — June 2026 follow-ups”** below. Still deferred: file size validation, S3 CORS tightening, presigned URL policy, rate limits, API-token auth for non-browser clients, deeper content sniffing, and broader upload validation beyond those completion paths.

## Upload hardening — June 2026 follow-ups

**Record (June 2026):** Additional upload-hardening and test-reliability follow-ups after upload completion S3 HeadObject verification work.

### Completed

- Removed unused **`BotoCoreError`** import from **`documents/s3.py`** — no behavior change.
- **`UploadApiCsrfTests`** now mocks **`documents.views.head_s3_object`** (upload S3 metadata verification) instead of depending on local AWS credentials or live S3. Keeps CSRF tests focused on CSRF/auth behavior. No production change.
- Legacy single-file **`upload_complete`**: validates non-empty payload **`file_mime`** via centralized upload metadata validation **before** S3 verification and **before** persisting MIME metadata. Invalid MIME/extension mismatch → **400**; document not marked uploaded; processing not enqueued. No migrations.
- Extended upload S3 verification from existence-only HeadObject checks to uploaded-object metadata verification:
  - Private helper **`_verify_uploaded_s3_object_metadata`** in **`documents/views.py`** (renamed to reflect metadata checks, not only existence).
  - **`upload_complete`** and **`upload_part_complete`** compare S3 HeadObject **`ContentType`** to expected MIME before marking complete.
  - Missing expected MIME, missing S3 **`ContentType`**, or mismatch → **400**.
  - Missing S3 object → **400**.
  - AWS/client HeadObject failure → **502**.
  - Failed verification does not mark upload/part uploaded and does not enqueue processing.
  - MIME normalization for comparison: **`image/jpg`** → **`image/jpeg`**, **`image/pjpeg`** → **`image/jpeg`**. **`application/octet-stream`** is not accepted as a substitute for a specific expected MIME.

### Out of scope (these PRs)

- OCR/HTR, Transkribus, Gemini, worker, routing, **`DocumentTextResult`** / processing-state semantics, frontend, S3 CORS tightening, infrastructure, migrations.
- File size validation, presigned URL policy, rate limits, API-token auth for non-browser clients, and deeper content sniffing remain deferred.

### Rationale

Closes consistency gaps in the upload completion path: the backend no longer relies only on client-reported metadata and object existence; it also verifies the uploaded object's stored S3 **`ContentType`** before accepting completion.

## Unified Archive Discovery / Catalog Metadata — target design (PR0)

**Decision (June 2026):** Add a design document for **Unified Archive Discovery / Catalog Metadata** before implementation. **Key architectural decision:** **`DocumentMetadata`** will **not** become the unified public discovery/catalog metadata model.

**Target direction:**

- Future **cross-item** public discovery metadata (categories, events, tags, and related browse/search dimensions) should be **`ArchiveItem`**-level or **linked to `ArchiveItem`**.
- **Categories**, **events**, and **tags** are intended as **`ArchiveItem`**-level **many-to-many** relationships from the foundation PR (PR1), not single-value or one-to-one links.
- **`DocumentMetadata`** remains **`OCR_DOCUMENT`**-side **internal/admin** metadata for now and must not anchor new public discovery features.
- **`Document.category_event`** and **`Document.tags_m2m`** are **transitional OCR-side** fields until **`ArchiveItem`**-level discovery metadata is implemented and backfilled.
- **`donor`**, **`collection`**, and **`original_location`** are **private/internal** for now; **`notes`** public vs internal split remains **open**.
- **`author_name`** / **`source_title`** on **`ArchiveItem`** are public display metadata; clickable filter/browse links are **not decided** now.
- Every future discovery/search/filter page must use the same access policy as **`/archive/`** (no leakage of hidden items, counts, or internal metadata).

**Docs:** `docs/ai-context/archive-discovery-catalog-design.md`

**Scope (PR0):** Design doc + this log entry only. **No** models, migrations, templates, search, clickable discovery links, **`category_event`** split, tag migration, **`DocumentMetadata`** implementation changes, **`PHOTO`**, upload/OCR/worker/routing/review changes.

## Unified Archive Discovery / Catalog Metadata — model foundation (PR1)

**Decision:** Add **`ArchiveItem`**-level discovery metadata models and many-to-many links: **`ArchiveCategory`**, **`ArchiveEvent`**, and **`ArchiveItem.tags`** (reusing existing **`Tag`**). **`ArchiveEvent.date_precision`** reuses **`ArchiveItem.DatePrecision`** choices.

**Scope (PR1):** Models + migration + minimal admin + focused tests + this log entry only. **No** edit UI, public display, search, clickable browse pages, backfill/reconciliation, or changes to **`Document.category_event`**, **`Document.tags_m2m`**, or **`DocumentMetadata`**.

**Deferred:** PR2 edit UI; PR3 public display; PR4 backfill from legacy OCR fields; PR5+ search and browse pages.

**Docs:** `docs/ai-context/archive-discovery-catalog-design.md`

## Unified Archive Discovery / Catalog Metadata — edit UI (PR2)

**Decision:** Add first-party staff/admin edit UI for **`ArchiveItem`**-level discovery metadata on **`/archive/manage/<id>/edit/`** for **`MANUAL_TEXT`** and **`OCR_DOCUMENT`**.

**UI:** Comma-separated Hebrew-labeled fields — **קטגוריות**, **אירועים**, **תגיות** — in a user-facing discovery section on the existing archive metadata edit page.

**Persistence:** Replace-all saves on **`ArchiveItem.categories`**, **`ArchiveItem.events`**, and **`ArchiveItem.tags`**. Existing categories/events/tags are reused by exact normalized name; new **`ArchiveCategory`** / **`ArchiveEvent`** rows get generated slugs with numeric suffixes (**`-2`**, **`-3`**, …) on slug collisions. **`ArchiveCategory.name`** and **`ArchiveEvent.name`** are **`unique=True`** (migration **0024**) so exact-name reuse matches DB integrity.

**OCR form field naming:** ArchiveItem discovery tags POST as **`discovery_tags`** on **`OCR_DOCUMENT`** edit to avoid collision with legacy **`tags`** editing for **`Document.tags_m2m`**. **`MANUAL_TEXT`** edit uses **`tags`** for ArchiveItem-level tags.

**Unchanged / transitional:** **`Document.category_event`**, **`Document.tags_m2m`**, and **`DocumentMetadata`** — no writes from this PR; remain OCR-side transitional fields.

**Scope (PR2):** Service helpers, validation, edit templates, migration **0024**, focused tests, this log entry. **No** public display, search, clickable category/event/tag pages, or backfill/reconciliation from legacy OCR fields. **No** **`PHOTO`** items.

**Deferred:** PR3 public display; PR4 backfill from legacy OCR fields; PR5+ search and browse pages.

**Docs:** `docs/ai-context/archive-discovery-catalog-design.md`

## Unified Archive Discovery / Catalog Metadata — public display (PR3)

**Decision:** Display **`ArchiveItem`**-level discovery metadata publicly on archive/document detail pages.

**Displayed fields:** **`ArchiveItem.categories`**, **`ArchiveItem.events`**, **`ArchiveItem.tags`**.

**Surfaces:** **`MANUAL_TEXT`** archive detail (**`/archive/<id>/`**) and **`OCR_DOCUMENT`** document detail (**`/api/ui/documents/<id>/`**).

**UI:** Hebrew labels **קטגוריות**, **אירועים**, **תגיות** near title/date/source metadata. Empty labels/sections are hidden.

**OCR transitional rule:** **`OCR_DOCUMENT`** detail prefers **`ArchiveItem`**-level discovery metadata. Legacy **`Document.category_event`** and **`Document.tags_m2m`** are shown only as transitional fallback when **`ArchiveItem`** discovery metadata is empty.

**Access:** **`DocumentMetadata`** remains staff/admin-only and is not exposed to anonymous/public viewers. **No** access-control changes; existing visibility rules still determine who can view the item.

**Helper:** **`archive_item_has_discovery_metadata`** in **`documents/services/archive_item_presentation.py`**. Only trusts prefetch cache when **`categories`**, **`events`**, and **`tags`** are all prefetched; otherwise falls back to DB **`exists()`** checks.

**Scope (PR3):** Reusable discovery-metadata template partial, detail views/templates, prefetch on detail querysets, presentation helper, focused tests, this log entry. **No** search, clickable category/event/tag pages, or backfill.

**Deferred:** PR5+ search and browse pages.

**Docs:** `docs/ai-context/archive-discovery-catalog-design.md`

## Unified Archive Discovery / Catalog Metadata — legacy backfill command (PR4)

**Decision:** Add management command **`backfill_archive_discovery_metadata`** to report and optionally backfill **`ArchiveItem`**-level discovery metadata from legacy OCR-side fields.

**User decision (existing data):** For the small number of existing documents currently in the site, any non-blank **`Document.category_event`** value is treated as an **`ArchiveCategory`** name (not an **`ArchiveEvent`**) during this backfill.

**Behavior:** Default **dry-run** (no writes). **`--apply`** links legacy **`Document.tags_m2m`** tags onto **`Document.archive_item.tags`** (reusing existing **`Tag`** rows; add-only, no duplicates) and maps non-blank **`Document.category_event`** to **`ArchiveItem.categories`** via exact-name **`ArchiveCategory`** get/create. **No** **`ArchiveEvent`** rows are created. **No** legacy OCR fields are deleted or cleared; **`DocumentMetadata`** is unchanged.

**Scope (PR4):** Service module, management command, focused tests, this log entry. **No** public/edit UI, search, clickable browse pages, automatic migration, or model changes.

**Deferred:** PR5+ search and browse pages; legacy field cleanup (PR7).

**Docs:** `docs/ai-context/archive-discovery-catalog-design.md`

## Unified Archive Discovery / Catalog Metadata — public archive search (PR5)

**Decision:** Add basic query search on **`/archive/`** via **`?q=`** over **`ArchiveItem`**-level public discovery metadata: **`title`**, **`author_name`**, **`source_title`**, and linked **`categories`**, **`events`**, and **`tags`** names (case-insensitive **`icontains`**; M2M joins use **`distinct()`**).

**Access:** Search applies only after the existing **`archive_item_queryset_for_user`** visibility filter. Anonymous users cannot discover private/family-only items through search; staff retain full list visibility. **`DocumentMetadata`** and other OCR-side internal fields are **not** searched or exposed.

**Scope (PR5):** List view search filter, Hebrew search UI on archive list template, presentation helpers, focused tests, this log entry. **No** full-text engine, OCR text search, clickable category/event/tag browse pages, or model/migration changes.

**Deferred:** PR6 clickable category/event/tag browse pages; legacy field cleanup (PR7).

**Docs:** `docs/ai-context/archive-discovery-catalog-design.md`

## Unified Archive Discovery / Catalog Metadata — clickable browse pages (PR6)

**Decision:** Add public browse pages for **`ArchiveItem`**-level discovery metadata at ID-based URLs:

- **`/archive/categories/<int:category_id>/`**
- **`/archive/events/<int:event_id>/`**
- **`/archive/tags/<int:tag_id>/`**

Category/event/tag names on archive detail pages link to these browse pages. Browse pages list matching archive items after the existing **`archive_item_queryset_for_user`** visibility filter (not raw M2M reverse querysets). Missing category/event/tag ids return **404**. When a taxonomy row exists but the viewer has no visible linked items, the browse page shows an empty state (**`אין פריטים להצגה.`**) without revealing private item titles.

**URL policy (PR6):** Browse URLs are **ID-based** only. **`ArchiveCategory.slug`** and **`ArchiveEvent.slug`** are **not** used for public browse routes in this PR. Human-controlled public slugs (including Hebrew transliteration such as **יהדות מצרים** → **yahadut-mitzraim**) are **deferred** to a future dedicated task because automatic/generated transliteration can be wrong or misleading.

**Access:** Visibility rules unchanged. Anonymous users see only public linked items; staff see all linked items. **`DocumentMetadata`** and legacy OCR **`Document.tags_m2m`** are **not** exposed on browse pages (tag browse filters **`ArchiveItem.tags`** only).

**Scope (PR6):** Browse views/URLs/templates, clickable discovery-metadata partial, shared archive item list table partial, focused tests, this log entry. **No** model/migration changes, slug fields, transliteration, editable slug UI, search-index changes, or legacy OCR field cleanup.

**Deferred:** PR7 legacy OCR discovery field cleanup; human-controlled public slugs for browse URLs.

**Docs:** `docs/ai-context/archive-discovery-catalog-design.md`

## Unified Archive Discovery / Catalog Metadata — remove public legacy OCR fallback display (PR7a)

**Decision:** Remove the public-facing transitional fallback that displayed legacy **`Document.category_event`** and **`Document.tags_m2m`** on **`OCR_DOCUMENT`** detail when **`ArchiveItem`** discovery metadata was empty.

**Public display/search/browse:** Public users now see **`ArchiveItem`**-level **`categories`**, **`events`**, and **`tags`** only (via the existing discovery-metadata partial on detail pages, **`/archive/?q=`** search, and ID-based browse pages). When **`ArchiveItem`** discovery metadata is empty, public detail pages show no categories/events/tags rather than falling back to legacy OCR fields.

**Legacy fields retained:** **`Document.category_event`** and **`Document.tags_m2m`** remain in the model and database as legacy/internal/transitional OCR-side fields. **No** data was deleted. **No** migrations were added. Staff/admin OCR edit UI and **`DocumentMetadata`** behavior are unchanged.

**Scope (PR7a):** OCR document detail view/template cleanup, focused tests, this log entry. **No** model/migration changes, backfill command changes, search/index changes, browse URL changes, or legacy data deletion.

**Deferred:** Full schema cleanup/removal of **`Document.category_event`** and **`Document.tags_m2m`** (PR7 follow-up).

**Docs:** `docs/ai-context/archive-discovery-catalog-design.md`

## Manual text UX — create discovery metadata and safe URL linkify (QA follow-up)

**Decision:** Extend manual text creation to support **`ArchiveItem`**-level discovery metadata in one flow, and safely linkify **`http://`** / **`https://`** URLs in manual text body on the public detail page.

**Manual text create:** **`/archive/manage/new/manual-text/`** and the manual-text branch of **`/archive/manage/new/`** now include the same categories/events/tags fields as manual text edit. Create POST reuses **`parse_archive_item_discovery_metadata_form`** and **`update_archive_item_discovery_metadata`**; submitted discovery values are preserved when validation fails. **No** legacy OCR **`Document.category_event`** / **`Document.tags_m2m`** fields.

**Manual text body display:** Plain text remains stored in **`ManualTextContent.body`**. Detail rendering uses **`manual_text_body_display`** (escape → line breaks → safe http/https linkify with **`target="_blank"`** / **`rel="noopener noreferrer"`**). **No** rich text editing, **no** HTML storage, **no** arbitrary HTML rendering.

**Scope:** Views/templates/services/templatetags, focused tests, this log entry. **No** models, migrations, OCR/worker/status changes, or OCR text formatting changes.

**Deferred:** Full rich text formatting/editor design.

## Unified OCR upload flow — design / audit (PR0)

**Decision:** Add a design/audit note for integrating OCR PDF/image upload into the unified archive create-item experience at **`/archive/manage/new/`**, without changing upload behavior in this PR.

**Superseded by:** PR1–PR3 implementation entries below. PR0 described pre-implementation state (bridge card, inline script in `upload.html`).

**Scope (PR0):** Documentation only — `docs/ai-context/unified-ocr-upload-flow.md`, this log entry.

**Docs:** `docs/ai-context/unified-ocr-upload-flow.md`

## Unified OCR upload flow — reusable upload partials (PR1)

**Decision:** Extract the existing OCR upload page into reusable template partials without changing runtime upload behavior.

**Current behavior:** `documents/templates/documents/upload/_upload_form.html` holds the upload form and admin-metadata column; `documents/templates/documents/upload/_upload_script.html` is the **single source of truth** for presigned S3 upload JavaScript. `documents/upload.html` remains the `/api/ui/upload/` shell and includes both partials.

**Scope (PR1):** Template extraction + comments + existing upload page tests. **No** endpoint, JS logic, redirect, upload API, S3, worker, or OCR/HTR changes.

## Unified OCR upload flow — embed in unified create page (PR2)

**Decision:** Embed the OCR upload UI inline on **`/archive/manage/new/?item_type=ocr_document`** using the same upload partials and the same `/api/uploads/*` client flow.

**Current behavior:** Unified OCR branch includes `_upload_form.html` and `_upload_script.html`. Shared `views._upload_form_context()` supplies template context to both `upload_page` and the unified OCR branch. **`/api/ui/upload/`** remains available as a fallback/secondary page (also using the same partials). Manual text create on the unified page is unchanged.

**Scope (PR2):** `manage_new.html`, `views._upload_form_context`, unified create tests. **No** duplicate presigned JS, **no** upload API/JS/S3/worker/OCR behavior changes.

## Unified OCR upload flow — ArchiveItem discovery metadata on OCR create (PR3)

**Decision:** Align first-party OCR upload discovery metadata with **`ArchiveItem`**-level categories/events/tags for **newly uploaded** OCR documents only.

**Current behavior:** Upload form uses `discovery_metadata_form_fields.html` (`categories`, `events`, `discovery_tags`). Upload JS sends those fields in create-upload JSON. `create_upload` parses them via `parse_archive_item_discovery_metadata_form` and persists to the linked `ArchiveItem` through `update_archive_item_discovery_metadata`. First-party UI no longer sends legacy `category_event` or `Document.tags_m2m` tags; legacy JSON in create payload is tolerated/ignored and not written to `Document`. Applies to single-file and multi-image create flows.

**Forward-only:** No backfill, no migrations, no schema cleanup, no modification of existing documents. `Document.category_event` and `Document.tags_m2m` remain for old/transitional data.

**Scope (PR3):** Upload form/script, `create_upload` discovery parsing/persistence, focused tests. **No** worker, routing, S3, or upload completion semantics changes.

**Deferred:** Post-upload redirect to archive detail; `/api/ui/upload/` retirement/redirect decision; legacy schema cleanup; **`PHOTO`**; rich text.

## ArchiveItem — PHOTO design / scope (PR1)

**Decision:** Approve V1 design for **`PHOTO`** archive items before implementation. **`PHOTO`** is one photo per **`ArchiveItem`**, backed by a dedicated **`PhotoContent`** model (**not** **`Document`**), with private S3 storage and presigned GET display after **`ArchiveItem.visibility`** checks. **No** OCR/HTR, worker, SQS, **`DocumentTextResult`**, Gemini, or Transkribus.

**Product (V1):** Staff/admin create one image; item appears in **`/archive/`** and **`/archive/<id>/`**; list uses placeholder/icon (not full original); detail shows original via presigned URL. Reuse existing **`ArchiveItem`** shared and discovery metadata fields — no large photo-specific metadata system in V1.

**Access:** PHOTO does **not** introduce a new visibility level or redefine access control. Reuse existing **`ArchiveItem.visibility`** exactly (**`public`** / **`private`** only — no **`FAMILY`** tier). **`public`:** everyone; **`private`:** authenticated **`archive_family`** + staff/admin (**not** staff-only). Helpers in **`archive_item_access.py`**; non-viewable → **404**.

**Model (proposed):** **`PhotoContent`** **`OneToOne`** to **`ArchiveItem`** with **`original_*`** S3/file fields and nullable **`thumbnail_*`** foundation fields (thumbnail generation deferred). S3 keys under **`photos/{photo_content_id}/original.{ext}`** with reserved **`thumb_400.{ext}`** path.

**Upload (recommended):** PHOTO-specific create/upload flow — **do not** reuse OCR **`/api/uploads/*`** → **`create_ocr_document`** pipeline. Reuse shared validation, presigned S3 helpers, HeadObject verification, and **`ArchiveItem`** services where appropriate.

**S3 delete (recommended):** Best-effort delete of private objects on staff PHOTO delete; finalize policy in PR5 if not implemented earlier.

**Implementation:** Deferred to **PR2+** per `docs/ai-context/photo-archive-items.md`. **Not** implemented in PR1.

**Docs:** `docs/ai-context/photo-archive-items.md`

**Out of scope (V1):** Multi-photo albums, thumbnail generation, image transforms, OCR-on-photo, face/people tagging, comments, public upload, rich text, legacy **`Document`** cleanup.

## ArchiveItem — PHOTO model foundation (PR2)

**Decision:** Add **`PhotoContent`** as the dedicated backing model for **`PHOTO`** archive items. **`ArchiveItem`** + **`PhotoContent`** are the runtime source of truth for PHOTO items (same pattern as **`MANUAL_TEXT`** + **`ManualTextContent`**). **No** **`Document`**, OCR/HTR, worker, SQS, upload, presigned URLs, or archive display in this PR.

**Model:** **`PhotoContent`** — `OneToOneField` to **`ArchiveItem`** (`related_name="photo_content"`, `on_delete=CASCADE`); **`original_*`** file metadata fields; nullable/blank **`width`**, **`height`**, and **`thumbnail_*`** foundation fields (thumbnail generation deferred). **`PhotoContent.clean()`** rejects non-**`PHOTO`** **`ArchiveItem`** links on **`full_clean()`**.

**Admin:** **`PhotoContentAdmin`** is view-only (add/change/delete disabled), matching **`ManualTextContentAdmin`**.

**Scope (PR2):** Model, migration **0025**, admin registration, focused model/admin tests, minimal doc updates. **No** data backfill. **No** create/upload service, S3, templates, or visibility changes.

**Deferred (PR3+):** Staff create/upload, presigned PUT/GET, archive list/detail PHOTO rendering, thumbnail generation, S3 delete on item delete — per `docs/ai-context/photo-archive-items.md`.

**Docs:** `docs/ai-context/photo-archive-items.md` (PR2 status note)

## ArchiveItem — PHOTO staff create/upload V1 (PR3)

**Decision:** Staff/admin create one **`PHOTO`** item via a **PHOTO-specific** flow — **not** OCR **`/api/uploads/*`** or **`create_ocr_document`**. Reuse shared image MIME/extension validation, presigned S3 PUT, and HeadObject **`ContentType`** verification.

**Create-order:** Create **`ArchiveItem`** + **`PhotoContent`** **before** client S3 upload (mirrors OCR **`Document`** `UPLOADING` + predetermined key). Explicit upload state on **`PhotoContent`**: **`upload_status`** (`PENDING` / `UPLOADED` / `FAILED`) + **`upload_error`**. Create sets **`PENDING`**; successful finalize after HeadObject sets **`UPLOADED`**; client/validation/verification failures set **`FAILED`** with safe **`upload_error`**. Retryable AWS HeadObject failures return **502** and leave **`PENDING`** (retry-safe).

**Size source of truth:** Persist **`original_size_bytes`** from S3 HeadObject **`ContentLength`** only — not client **`file_size`**.

**Public archive guard (PR3):** **`/archive/`** list/detail/discovery browse exclude **`PHOTO`** via **`archive_browse_queryset_for_user`** / **`exclude_deferred_archive_browse_item_types`** until PR4 display. Staff **`/archive/manage/`** still lists PHOTO items.

**Endpoints:** **`POST /api/photo-uploads/create/`**, **`POST /api/photo-uploads/<photo_content_id>/complete/`** (staff only). UI branch: **`/archive/manage/new/?item_type=photo`**.

**S3 keys:** **`photos/{photo_content_id}/original.{ext}`** (canonical ext from validated MIME). Private bucket only; no presigned GET in PR3.

**Finalize idempotency:** Repeat complete on **`upload_status=UPLOADED`** returns current state without S3 re-verify or field overwrite.

**Deferred:** Re-upload/retry after **`upload_status=FAILED`** (not implemented in PR3).

**Scope (PR3):** Service + API + unified create UI branch + discovery metadata on create + focused tests + minimal doc updates. **No** **`Document`**, worker, SQS, archive list/detail rendering, thumbnails, dimensions, edit/delete, visibility changes.

**Deferred (PR4+):** Archive list/detail PHOTO display (presigned GET), edit/delete polish, thumbnail generation — per `docs/ai-context/photo-archive-items.md`.

**Docs:** `docs/ai-context/photo-archive-items.md`
