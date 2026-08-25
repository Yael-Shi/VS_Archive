# PHOTO Archive Items — Design / Scope

Design and implementation scope for **`PHOTO`** archive items: private S3 storage, presigned display, browse-card thumbnails, and no OCR/HTR pipeline. The data model now allows **1..N** **`PhotoContent`** rows per PHOTO **`ArchiveItem`**. Staff can manage those rows. Public detail presents all renderable photos; browse cards still use the **first** photo.

**Status:** Design (PR1) through staff manage status clarity (PR6) are **implemented**. **Browse-card thumbnail generation**, **upload-time thumbnail persistence**, and **idempotent backfill commands** are **implemented**. The **multi-photo data model**, **staff multi-photo management (PR3)**, the **public multi-photo gallery (PR4)**, **search aggregation across PhotoContent / PhotoPerson (multi-photo PR5)**, **Person aliases schema + PHOTO search (PR6a)**, and **staff Person/alias management UI (PR6b)** are **implemented**. Public alias display, re-upload/retry after **`FAILED`**, and browse-card aggregation remain **not implemented**.

**Related docs:**

- `docs/ai-context/decision-log.md` — durable decisions and PR history (see end of log for current browse/thumbnail state)
- `docs/ai-context/vs-archive-context.md` — broad project context
- `docs/ai-context/archive-discovery-catalog-design.md` — ArchiveItem discovery metadata direction
- `docs/ai-context/unified-ocr-upload-flow.md` — completed OCR upload flow (separate from PHOTO)

**Key code references (current behavior):**

- `documents/models.py` — **`ArchiveItem`**, **`ManualTextContent`**, **`PhotoContent`**, **`Person`**, **`PersonAlias`**
- `documents/services/photo_content_management.py` — staff PHOTO child writes, Person rename, PersonAlias writes
- `documents/services/archive_item_access.py` — visibility and browse renderability
- `documents/services/archive_item_presentation.py` — **`ArchiveBrowseCard`**, text preview, type markers
- `documents/services/photo_archive_urls.py` — presigned browse thumbnails for PHOTO
- `documents/services/photo_gallery.py` — public detail gallery selection/presentation
- `documents/services/document_archive_urls.py` — presigned browse thumbnails for image OCR documents
- `documents/services/photo_thumbnail.py` — upload-time PHOTO thumbnail generation
- `documents/services/document_thumbnail.py` — upload-time OCR image-document thumbnail generation
- `documents/services/photo_upload.py` — PHOTO create/upload/finalize
- `documents/services/photo_presentation.py` — staff manage upload/renderability labels
- `documents/management/commands/backfill_photo_thumbnails.py` — operational PHOTO thumbnail backfill
- `documents/management/commands/backfill_document_thumbnails.py` — operational OCR image thumbnail backfill
- `documents/templates/documents/archive/partials/item_list_cards.html` — public browse cards
- `documents/s3.py` — presigned PUT/GET helpers, deterministic thumbnail key builders
- `documents/views.py` — **`/archive/`** list/detail, unified create at **`/archive/manage/new/`**, staff Person edit at **`/archive/manage/people/<id>/edit/`**

---

## Current authoritative state

This section describes **implemented runtime behavior**. Historical PR notes below may describe earlier deferred states; treat this section as source of truth for operations and UI.

### ArchiveItem and item types

- **`ArchiveItem`** is the source of truth for shared archival metadata (title, visibility, dates, metadata status, author/source display, discovery M2M, public note) across **`PHOTO`**, **`OCR_DOCUMENT`**, and **`MANUAL_TEXT`**.
- **`PHOTO`** — backed by **1..N** **`PhotoContent`** rows (`ForeignKey`, `related_name="photo_contents"`); no **`Document`**, worker, or **`DocumentTextResult`**. Staff manage all rows. Public detail presents every renderable `PhotoContent` on `/archive/<id>/`. Browse cards still use **`ArchiveItem.primary_photo_content`** (first by `position`, then `id`). Each photo may store its own `date_start` / `date_end` / `date_precision`; those dates are **not** aggregated onto **`ArchiveItem`** and are **not** used by structured year filters or FTS. Public `q` search aggregates descriptive text and identified Person names from **public-renderable** `PhotoContent` rows onto the one ArchiveItem index row.
- **`OCR_DOCUMENT`** — backed by **`Document`** (+ **`DocumentSourceFile`** where applicable); OCR/HTR via worker. Shared display metadata is read from **`ArchiveItem`**; OCR-specific fields remain on **`Document`**.
- **`MANUAL_TEXT`** — backed by **`ManualTextContent.body`**; no file upload.

### Public browse-card visual previews (`/archive/`, discovery browse pages)

Browse cards are server-rendered in **`item_list_cards.html`**. Each card has:

1. **Visual preview** — stored JPEG thumbnail **or** CSS type-marker fallback (mutually exclusive branches).
2. **Text preview** — truncated metadata/transcription paragraph (`card.preview_text`), separate from the visual preview.

| Item type | Visual when thumbnail available | Visual fallback | Presign key for browse |
|-----------|--------------------------------|-----------------|------------------------|
| **PHOTO** | `<img>` from the **primary** **`PhotoContent.thumbnail_file_key`** | CSS marker `--photo` | `photos/{id}/thumb_400.jpg` only |
| **OCR_DOCUMENT (IMAGE)** | `<img>` from **`Document.thumbnail_file_key`** | CSS marker `--ocr` | `documents/{id}/thumb_400.jpg` only |
| **OCR_DOCUMENT (PDF)** | Never (generation skipped) | CSS marker `--ocr` | N/A |
| **MANUAL_TEXT** | Never (no thumbnail enrichment) | CSS marker `--manual` | N/A |

**Rules (all item types):**

- Browse cards **never** presign **`PhotoContent.original_file_key`**, **`Document.file_s3_key`**, or any source-file key for list previews.
- When **`thumbnail_url`** is set, the template renders the image branch only — **no** type-marker icon is shown on the same card.
- Missing **`thumbnail_file_key`**, empty bucket config, or presign failure leaves **`thumbnail_url=None`** → CSS marker fallback.
- PDF OCR documents are excluded from document thumbnail presigning even if **`thumbnail_file_key`** is populated.

**Detail pages:**

- **PHOTO detail** (`/archive/<id>/`) — presigned GET for the **selected** photo’s **`original_file_key`** (full original), not the browse thumbnail. `?photo=<photo_content_id>` selects a renderable photo on this item; omitted/invalid/foreign values fall back to the first renderable photo. One renderable photo keeps the simple detail layout. Multiple renderable photos show Previous/Next, thumbnail selectors (stored thumbs or numbered fallback), and per-selected-photo metadata. Shared ArchiveItem metadata is shown once.
- **OCR detail** — `/archive/<id>/` redirects to the OCR document detail page.

### Thumbnail generation (forward path)

- **PHOTO:** after successful upload finalize (`upload_status=UPLOADED`), **`generate_and_persist_photo_thumbnail`** runs best-effort. Downloads the validated original from S3, applies EXIF-aware resize to max edge 400, uploads fixed JPEG key, persists **`width`**, **`height`**, and **`thumbnail_*`** on **`PhotoContent`**. Upload success is not rolled back on thumbnail failure.
- **OCR image documents:** after upload complete/finalize commits, **`schedule_document_thumbnail_after_upload`** runs **`generate_and_persist_document_thumbnail`** on commit. Uses the **first source page** (`DocumentSourceFile` at **`order_index=0`**). Persists **`first_page_width`**, **`first_page_height`**, and **`thumbnail_*`** on **`Document`**. **PDF** documents are excluded (**`should_generate_document_thumbnail`** returns false).
- Shared encoder: **`documents/services/image_thumbnail.py`** (`THUMBNAIL_MAX_EDGE=400`, JPEG output).
- Worker (`run_worker.py`) does **not** generate browse thumbnails.

### Thumbnail backfill (operational / repair)

Supported management commands (not temporary one-off scripts):

| Command | Targets | Default mode |
|---------|---------|--------------|
| **`backfill_photo_thumbnails`** | **`PhotoContent`**: `UPLOADED`, non-empty **`original_file_key`**, empty **`thumbnail_file_key`** | Dry-run; **`--commit`** writes S3 + DB |
| **`backfill_document_thumbnails`** | **`Document`**: `doc_type=IMAGE`, `UPLOADED`, empty **`thumbnail_file_key`**, valid primary source at `order_index=0` | Dry-run; **`--commit`** writes S3 + DB |

Both commands support **`--limit`**, **`--json`**, and single-id filters (`--photo-id`, `--document-id`). Re-runs are **idempotent** (rows with existing thumbnails are skipped). Completed production backfills do **not** obsolete these commands.

### S3 key convention (implemented)

```
photos/{photo_content_id}/original.{ext}     # validated MIME extension
photos/{photo_content_id}/thumb_400.jpg      # fixed JPEG browse thumbnail

documents/{document_id}/thumb_400.jpg        # fixed JPEG browse thumbnail (image OCR only)
```

Thumbnail keys are **always** `thumb_400.jpg` (not `thumb_400.{ext}`).

### S3 orphan cleanup (document scope only)

- **`cleanup_document_s3_orphans`** scans under **`documents/`** and protects keys referenced in the database from:
  - **`Document.file_s3_key`**
  - **`Document.thumbnail_file_key`**
  - **`DocumentSourceFile.file_s3_key`**
- Protection is by **DB reference**, not filename pattern. Unreferenced thumbnail derivatives under **`documents/`** remain valid orphan-delete candidates.
- Objects under **`photos/`** are **outside** this command’s scope.

### Browse eligibility

**`archive_browse_queryset_for_user`** applies visibility via **`archive_item_queryset_for_user`**, then **`filter_browse_renderable_archive_items`**:

- **PHOTO** — the **first** **`PhotoContent`** (by `position`, then `id`) has **`upload_status=UPLOADED`** and a non-empty **`original_file_key`**. Extra photos are ignored for **browse-card eligibility**; public detail may still show additional renderable photos after the item is accessible.
- **OCR_DOCUMENT** — **`ocr_document.upload_status=UPLOADED`**
- **MANUAL_TEXT** — no upload gate

If the **first** photo is **`PENDING`** / **`FAILED`** / empty-key, the item is omitted from public browse and **`/archive/<id>/`** returns **404**. Additional non-renderable rows on an otherwise accessible item are omitted from the public gallery only. Staff **`/archive/manage/`** lists all PHOTO items regardless of upload status.

### Public search (multi-photo PR5)

`ArchiveItemSearchIndex` remains one row per ArchiveItem. PHOTO `q` search concatenates, in `(position, id)` order, each **public-renderable** PhotoContent's `description` / `location` / `context` / `people_present` / `notes`, then distinct `PhotoPerson` `Person.name` values from those rows ordered by `(name, id)`, then `PersonAlias.name` values for those same Persons (same person order, aliases by `(name, id)`), into `metadata_text`. Renderable means the same public-gallery contract (`photo_is_archive_renderable` / `public_renderable_photo_contents`): `UPLOADED` and a non-empty `original_file_key`. Pending, failed, and empty-key photos are omitted; thumbnail presence does not matter. Access is still applied at query time. Result URL is `/archive/<id>/` with no matched `?photo=` deep-link. Per-photo dates are not searchable and do not affect `year` / `year_to`. Child-row writes refresh the owning index in the same transaction as the staff service (`update_photo_content_metadata`, add/delete/reorder, `update_person_name`, `create_person_alias` / `update_person_alias` / `delete_person_alias`). Successful upload finalize (`PENDING` → `UPLOADED`) also refreshes the owning index so that photo's metadata becomes searchable; failed finalize leaves it absent. Aliases are searchable but not shown on public PHOTO detail.

**`ArchiveItemPerson` is not a photo appearance.** Item-level person links are indexed as ArchiveItem metadata (canonical name + aliases, all item types) and do not select a photo, generate `?photo=`, or depend on photo renderability. `PhotoPerson` remains the only “this person appears in this photo” relation. Historical person-name Tags still power tag browse/filter.

**Staff write path (C1):** ArchiveItem-level people are managed on ArchiveItem **create and edit** for all types, including PHOTO create (`/archive/manage/new/?item_type=photo`) and the PHOTO shared-metadata edit page (`/archive/manage/<id>/edit/`) under **אנשים קשורים לפריט**. PHOTO create writes **`ArchiveItemPerson`** (related to the archival item) and does **not** write **`PhotoPerson`**. Photo-level identified people remain **אנשים מזוהים בתמונה** on the per-photo edit page. Neither relation is inferred from the other. **C2a** stores user-submitted ArchiveItemPerson ADD/REMOVE deltas (`ArchiveItemPersonSuggestion`) and applies them through `create_archive_item_person` / `delete_archive_item_person` only; it never infers PhotoPerson. **C2b** is the public metadata-suggestion Person delta UI plus a dedicated staff Person-suggestion backlog; it does not infer or mutate PhotoPerson. Public cards/detail now show ArchiveItemPerson under **אנשים קשורים** and hide the 29 frozen historical person-name Tag ids from public discovery; PhotoPerson **אנשים מזוהים:** stays separate. Stage B tag browse/filter hiding remains deferred.

---

## 1. Product definition

### What is a PHOTO archive item?

A **`PHOTO`** archive item is a historical/family photograph (or a set of related photograph components) cataloged in the unified archive. It is a first-class **`ArchiveItem`** with **`item_type=PHOTO`**, visible in the normal archive list and detail flow, with shared archival metadata (title, dates, visibility, discovery fields) on **`ArchiveItem`** and image bytes stored separately in private S3.

**Current product/UI rule:** staff can attach **multiple image files** to one PHOTO item. Public detail presents **all renderable photos** as a server-rendered gallery on the ArchiveItem URL. Browse cards still use **the first image** (`primary_photo_content`). Public `q` search aggregates text from **public-renderable photos** onto that one ArchiveItem. No OCR text extraction.

### How PHOTO differs from OCR_DOCUMENT

| Aspect | **`OCR_DOCUMENT`** | **`PHOTO`** |
|--------|-------------------|-------------|
| Backing model | **`Document`** (+ **`DocumentSourceFile`**, etc.) | **`PhotoContent`** (dedicated model) |
| Primary payload | Scanned document / PDF for text extraction | Photograph for viewing |
| Processing | S3 upload → SQS → worker → OCR/HTR → **`DocumentTextResult`** | Direct create/upload → store in S3 → display via presigned GET |
| Text results | **`DocumentTextResult`** rows, review/verification lifecycle | **None** — no OCR/HTR, no worker, no Gemini/Transkribus |
| Runtime source of truth | **`ArchiveItem`** for shared metadata; **`Document`** for OCR/processing | **`ArchiveItem`** + **`PhotoContent`** |
| Staff workflows | Upload, transcription review, metadata edit, processing state | Create/upload photo (including item-level **ArchiveItemPerson**), edit shared metadata (including item-level **ArchiveItemPerson**), manage individual photos / PhotoPerson |

**Non-negotiable:** PHOTO must **not** be routed through **`Document`**, the OCR upload pipeline, worker, SQS, or any OCR/HTR provider.

### How PHOTO differs from MANUAL_TEXT

| Aspect | **`MANUAL_TEXT`** | **`PHOTO`** |
|--------|-------------------|-------------|
| Backing model | **`ManualTextContent.body`** (typed text) | **`PhotoContent`** (image file metadata + S3 keys) |
| Primary payload | Plain text in DB | Binary image in private S3 |
| Display | Auto-escaped text + line breaks in templates | `<img>` via presigned GET URL after permission check |
| Upload | Inline form POST (no S3 file upload) | Staff image upload to private S3 |
| Processing | None | None (no worker) |

Both types share the same **`ArchiveItem`** shell: title, visibility, dates, metadata status, author/source display fields, and discovery metadata (categories, events, tags).

---

## 2. Recommended model

**Decision:** **`ArchiveItem`** remains the user-facing archival item. **`PHOTO`** is backed by a dedicated **`PhotoContent`** model — **not** **`Document`**.

```
ArchiveItem (item_type=PHOTO)
    └── ForeignKey 1..N ── PhotoContent  (related_name="photo_contents")
                             └── S3 keys + file metadata + thumbnail metadata
                             └── per-image dates + descriptive fields
```

**Rationale:**

- **`Document`** carries OCR-specific fields (`language`, `text_input_type`, processing state, **`DocumentTextResult`**, worker semantics) that do not apply to photos.
- **`ManualTextContent`** remains OneToOne. PHOTO now allows multiple image components under one umbrella item.
- Keeps PHOTO implementation isolated from OCR bridge/cutover work and from accidental worker enqueue.

**Runtime source of truth (PHOTO):**

- **`ArchiveItem`** — shared archival and discovery metadata, visibility, umbrella dates.
- **`PhotoContent`** — one image/component: storage identity, file metadata, browse thumbnail metadata, per-image dates, descriptive fields.

---

## 3. PhotoContent fields

**Status:** Model foundation through staff manage clarity, browse thumbnail generation, the **1:N multi-photo schema**, **staff multi-photo management**, and the **public multi-photo gallery** are **implemented**.

| Field | Type | Notes |
|-------|------|-------|
| **`archive_item`** | **`ForeignKey(ArchiveItem)`** | `related_name="photo_contents"`, `on_delete=CASCADE` |
| **`position`** | **`PositiveIntegerField`**, default `1` | Stable order within one item; unique with `archive_item`; existing rows backfilled to `1` |
| **`original_file_key`** | **`CharField`** | Private S3 object key for the uploaded original |
| **`original_filename`** | **`CharField`** | Client/original filename for display/audit |
| **`original_mime_type`** | **`CharField`** | Validated MIME type (e.g. `image/jpeg`) |
| **`original_size_bytes`** | **`PositiveBigIntegerField`** | Size from S3 HeadObject **`ContentLength`** after finalize |
| **`upload_status`** | **`CharField`** | **`PENDING`** / **`UPLOADED`** / **`FAILED`** |
| **`upload_error`** | **`CharField`** | Safe error text when **`upload_status=FAILED`** |
| **`date_start`** / **`date_end`** / **`date_precision`** | same types as **`ArchiveItem`** | Per-image archival date; not aggregated onto the umbrella item |
| **`width`** | **`PositiveIntegerField`, nullable** | Populated on successful thumbnail generation (transposed dimensions) |
| **`height`** | **`PositiveIntegerField`, nullable** | Populated on successful thumbnail generation |
| **`thumbnail_file_key`** | **`CharField`, nullable** | Deterministic S3 key `photos/{id}/thumb_400.jpg` when generation succeeds |
| **`thumbnail_mime_type`** | **`CharField`, nullable** | `image/jpeg` when thumbnail exists |
| **`thumbnail_size_bytes`** | **`PositiveBigIntegerField`, nullable** | JPEG byte size when thumbnail exists |
| **`created_at`** | **`DateTimeField(auto_now_add=True)`** | |
| **`updated_at`** | **`DateTimeField(auto_now=True)`** | |

**Validation invariant:** **`ArchiveItem.item_type`** must be **`PHOTO`** when a **`PhotoContent`** row exists. **`PhotoContent.clean()`** enforces this on **`full_clean()`**, plus stored date-bound checks aligned with **`ArchiveItem`**.

---

## 4. S3 / storage strategy

### Privacy

- Original images and thumbnails are stored as **private S3 objects** (same bucket pattern as document uploads; no public ACL).
- Display uses **presigned GET URLs** generated only after **`archive_item_access`** permission checks.

### Key convention

Use a dedicated prefix keyed by **`PhotoContent`** primary key:

```
photos/{photo_content_id}/original.{ext}
photos/{photo_content_id}/thumb_400.jpg
```

**Notes:**

- **`{ext}`** on originals comes from validated upload MIME/extension mapping (e.g. `jpg`, `png`, `webp`).
- Browse thumbnails are **always** fixed JPEG at **`thumb_400.jpg`**.
- Thumbnail generation failure does not block upload finalize; browse cards fall back to the CSS photo marker.

### S3 object deletion on PHOTO delete

**Current behavior:** Staff whole-item delete and single-photo delete schedule best-effort S3 cleanup (`on_commit`) for each affected **`original_file_key`** and **`thumbnail_file_key`**. AWS failures are logged and do not fail the completed request. Orphaned keys can still be cleaned by operational commands.

---

## 5. Image display / access

### Visibility / access control

**PHOTO does not introduce a new visibility level.** Reuse existing **`ArchiveItem.visibility`** (`public` / `private` only). Helpers in **`documents/services/archive_item_access.py`**.

| Viewer | Can view |
|--------|----------|
| Staff/admin | All **`ArchiveItem`** rows |
| Anonymous | **`public`** only |
| Authenticated **`archive_family`** group members | **`public`** + **`private`** |
| Everyone else | **`public`** only |

Non-viewable items return **404**.

### Presigned GET flow (detail)

1. Viewer requests **`/archive/<archive_item_id>/`** (optional **`?photo=<photo_content_id>`**).
2. View loads **`ArchiveItem`** + **`PhotoContent`** via **`get_viewable_archive_item()`**, which prefetches ``photo_contents`` and each photo’s identified ``people``.
3. Access check runs **before** any S3 URL is built. Item-level PHOTO access still requires the first photo to be renderable.
4. On success, backend calls **`create_presigned_get`** for the **selected** photo’s **`original_file_key`** (full original). Gallery selectors may also presign stored thumbnail keys.
5. Template renders the selected image. TTL is 1 hour (`PRESIGNED_GET_EXPIRY_SECONDS=3600`), consistent with other presigned GET usage.

**Public items, private objects:** S3 objects stay private; public browsing uses backend-issued presigned GET after access checks.

### List / browse cards

- When **`thumbnail_file_key`** is set and presign succeeds, browse cards show a **presigned thumbnail** (`apply_photo_thumbnail_urls_to_browse_cards`).
- Otherwise cards show the **CSS photo type marker** — not the full original.
- Browse list **never** presigns **`original_file_key`**.

---

## 6. V1 UI behavior

### Create / manage (staff/admin)

- Staff create **one PHOTO** via **`/archive/manage/new/?item_type=photo`** (first `PhotoContent` at `position=1`).
- **`/archive/manage/`** lists PHOTO items with upload-status and archive-renderability badges (primary photo).
- Edit shared metadata at **`/archive/manage/<id>/edit/`**, which also lists all photos. Add/edit/reorder/delete individual photos from nested `/photos/` routes. Shared metadata is not duplicated on the per-photo form. A PHOTO item must keep at least one `PhotoContent`.
- Per-photo file replace is not implemented.
- Per-photo identified-person picker labels include aliases for staff
  (`Canonical (Alias 1, Alias 2)`). Selected people link to
  **`/archive/manage/people/<person_id>/edit/`** for canonical rename and
  alias add/edit/delete. Aliases belong to the Person globally. There is no
  Person catalog. Public pages still show canonical names only.

### Public / family archive surfaces

| Surface | Current behavior |
|---------|------------------|
| **`/archive/`** and discovery browse | PHOTO rows show title, dates, text preview, and **stored thumbnail** of the **primary** photo when available; **CSS photo marker** when not |
| **`/archive/<id>/`** | Detail shows **all renderable photos**; selected **full original** via presigned GET after permission check (`?photo=` selects) |
| Visibility | Same rules as other item types |

### Reuse ArchiveItem fields

Use existing shared fields: **`title`**, **`visibility`**, **`metadata_status`**, dates, **`author_name`**, **`source_title`**, **`categories`**, **`events`**, **`tags`**.

**Out of scope:** captions as rich text, EXIF-driven auto-metadata, face boxes, comments, transformations, public self-service upload.

---

## 7. Upload approach

### Recommended direction (implemented in PR3)

- **PHOTO-specific** create/upload path — **not** OCR **`/api/uploads/*`**.
- Reuses shared image validation, presigned S3 PUT, HeadObject verification, and **`ArchiveItem`** services.

**Implemented flow (new item):**

1. Staff open **`/archive/manage/new/?item_type=photo`** and submit metadata + one image file.
2. **`POST /api/photo-uploads/create/`** creates **`ArchiveItem`** (`PHOTO`) + **`PhotoContent`** (`PENDING`, `position=1`), returns presigned PUT for **`photos/{photo_content_id}/original.{ext}`**.
3. Browser uploads to private S3 via presigned PUT.
4. **`POST /api/photo-uploads/<photo_content_id>/complete/`** runs HeadObject, sets **`UPLOADED`** or **`FAILED`**, then best-effort **`generate_and_persist_photo_thumbnail`**.

**Implemented flow (add photo to existing item):**

1. Staff open **`/archive/manage/<id>/photos/add/`**.
2. **`POST /api/photo-uploads/add/`** locks the **`ArchiveItem`**, allocates **`max(position)+1`**, creates pending **`PhotoContent`**, returns the same style of presigned PUT.
3. Browser PUT + **`/complete/`** are the existing finalize path.

**Create-order:** **`PhotoContent`** is created **before** client S3 upload. **`upload_status`** / **`upload_error`** are explicit.

**Re-upload/retry after `FAILED`:** not implemented.

### OCR upload behavior (cross-reference)

Where staff upload scanned documents (separate from PHOTO), the current first-party upload UI (`documents/templates/documents/upload/`) implements:

- **Mobile gallery-first** — primary action «הוספת עמוד מהגלריה»; users photograph pages in the device camera app, then add from gallery.
- **Direct in-browser camera capture removed** — no `capture` attribute on the file input.
- **Image documents:** 1–35 pages per document; pages upload **incrementally** (each selection uploads immediately via incremental create + part endpoints).
- **PDF:** separate single-file path (not mixed with gallery images).
- **EXIF orientation:** server-side normalization for supported uploaded images via **`normalize_uploaded_image_exif_in_s3`** before processing/thumbnail generation.

See **`docs/ai-context/decision-log.md`** for OCR upload API history and current incremental-flow entry.

---

## 8. Explicit out of scope

- Browse-card aggregation / choosing a non-primary preview image
- Image transformations beyond thumbnail resize (watermark, CDN, etc.)
- OCR/HTR on photos
- Worker / SQS processing for PHOTO
- Face recognition, AI identification, comments
- Public alias display; Person catalog/Admin
- Eventual person-Tag removal (Tags still power public tag browse/filter); automatic ArchiveItemPerson from PhotoPerson; staff PHOTO appearance review / PhotoPerson backfill from `people_present`; public Person filter/browse UX; Stage B hide mapped historical person Tags from tag browse/filter; Tag-id → Person-id cleanup mapping; redirects; destructive cleanup; no new-Person proposals
- Public Person pages
- Public (unauthenticated) upload
- Rich text captions
- Re-upload/retry after **`upload_status=FAILED`**
- PDF browse thumbnails for OCR documents

---

## 9. PR sequence (historical)

| PR | Scope | Status |
|----|--------|--------|
| **PR1** | Design/scope documentation | Done |
| **PR2** | **`PhotoContent`** model + migration + admin | Done |
| **PR3** | Staff create/upload V1 | Done |
| **PR4** | Public archive list/detail for PHOTO | Done (browse visual preview extended later — see **Current authoritative state**) |
| **PR5** | Staff metadata edit/delete | Done |
| **PR6** | Staff manage status clarity | Done |
| **Post-PR6** | Browse-card thumbnails + upload-time generation + backfill commands | **Implemented** — see decision log |
| **Multi-photo PR2** | 1:N `PhotoContent` FK, `position`, per-image dates | **Implemented** |
| **Multi-photo PR3** | Staff add/edit/reorder/delete photos + Person selection | **Implemented** |
| **Multi-photo PR4** | Public gallery / per-selected-photo metadata / identified Person names | **Implemented** |
| **Multi-photo PR5** | Search aggregation across public-renderable PhotoContent text + PhotoPerson names; child-row and successful-finalize index refresh | **Implemented** |
| **PR6a** | `PersonAlias` schema + alias write services + PHOTO search integration (no staff alias UI, no public alias display) | **Implemented** |
| **PR6b** | Staff Person edit page + alias CRUD UI + alias-aware picker labels + selected-person edit links (no catalog, no public alias display) | **Implemented** |
| **C1 ArchiveItemPerson staff UI** | Item-level people manager on ArchiveItem **create and edit** for all types; PHOTO heading distinguished from PhotoPerson; PHOTO create does not write PhotoPerson; public cards/detail cutover is Stage A | **Implemented** |
| **C2a ArchiveItemPerson suggestions foundation** | `ArchiveItemPersonSuggestion` ADD/REMOVE deltas; Person.id identity; pending unique constraint; submit/apply services; stale approve = APPROVED no-op; no PhotoPerson inference; no public/staff HTML | **Implemented** |
| **C2b ArchiveItemPerson suggestion UI** | Public metadata-suggestion page submits Person ADD/REMOVE deltas; dedicated staff backlog + approve/reject; Person.id identity; no new-Person proposals; PhotoPerson remains separate; historical person Tags unchanged on that form; Stage A public cards/detail cutover implemented separately | **Implemented** |

---

## 10. Risks / non-negotiables

| Rule | Why |
|------|-----|
| **Do not route PHOTO to OCR worker** | Prevents spurious **`DocumentTextResult`**, cost, and wrong product semantics |
| **Do not use `Document` as PHOTO backing model** | Avoids OCR bridge confusion and accidental processing |
| **Do not expose private S3 objects publicly** | Access gated by **`ArchiveItem.visibility`** + presigned GET |
| **Do not presign originals for browse list previews** | Performance and bandwidth; use stored thumbnails or CSS markers |
| **Do not skip MIME/extension/ContentType validation** | Prevents malicious or mistaken uploads |

---

## Appendix: alignment with existing item types

| `item_type` | Backing | Processing | Archive list/detail |
|-------------|---------|------------|---------------------|
| **`OCR_DOCUMENT`** | **`Document`** | Worker + OCR/HTR | Browse card: image thumbnail or `--ocr` marker; detail via OCR document page |
| **`MANUAL_TEXT`** | **`ManualTextContent`** | None | Browse card: `--manual` marker; **`/archive/<id>/`** body text |
| **`PHOTO`** | **`PhotoContent`** | None | Browse card: primary photo thumbnail or `--photo` marker; detail shows all renderable originals |
