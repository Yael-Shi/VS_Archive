# PHOTO Archive Items — Design / Scope

Design and implementation scope for **`PHOTO`** archive items: one photo per **`ArchiveItem`**, private S3 storage, presigned display, browse-card thumbnails, and no OCR/HTR pipeline.

**Status:** Design (PR1) through staff manage status clarity (PR6) are **implemented**. **Browse-card thumbnail generation**, **upload-time thumbnail persistence**, and **idempotent backfill commands** are **implemented**. Re-upload/retry after **`FAILED`**, S3 delete on PHOTO delete, and multi-photo albums remain **not implemented**.

**Related docs:**

- `docs/ai-context/decision-log.md` — durable decisions and PR history (see end of log for current browse/thumbnail state)
- `docs/ai-context/vs-archive-context.md` — broad project context
- `docs/ai-context/archive-discovery-catalog-design.md` — ArchiveItem discovery metadata direction
- `docs/ai-context/unified-ocr-upload-flow.md` — completed OCR upload flow (separate from PHOTO)

**Key code references (current behavior):**

- `documents/models.py` — **`ArchiveItem`**, **`ManualTextContent`**, **`PhotoContent`**
- `documents/services/archive_item_access.py` — visibility and browse renderability
- `documents/services/archive_item_presentation.py` — **`ArchiveBrowseCard`**, text preview, type markers
- `documents/services/photo_archive_urls.py` — presigned browse thumbnails for PHOTO
- `documents/services/document_archive_urls.py` — presigned browse thumbnails for image OCR documents
- `documents/services/photo_thumbnail.py` — upload-time PHOTO thumbnail generation
- `documents/services/document_thumbnail.py` — upload-time OCR image-document thumbnail generation
- `documents/services/photo_upload.py` — PHOTO create/upload/finalize
- `documents/services/photo_presentation.py` — staff manage upload/renderability labels
- `documents/management/commands/backfill_photo_thumbnails.py` — operational PHOTO thumbnail backfill
- `documents/management/commands/backfill_document_thumbnails.py` — operational OCR image thumbnail backfill
- `documents/templates/documents/archive/partials/item_list_cards.html` — public browse cards
- `documents/s3.py` — presigned PUT/GET helpers, deterministic thumbnail key builders
- `documents/views.py` — **`/archive/`** list/detail, unified create at **`/archive/manage/new/`**

---

## Current authoritative state

This section describes **implemented runtime behavior**. Historical PR notes below may describe earlier deferred states; treat this section as source of truth for operations and UI.

### ArchiveItem and item types

- **`ArchiveItem`** is the source of truth for shared archival metadata (title, visibility, dates, metadata status, author/source display, discovery M2M, public note) across **`PHOTO`**, **`OCR_DOCUMENT`**, and **`MANUAL_TEXT`**.
- **`PHOTO`** — backed by **`PhotoContent`**; no **`Document`**, worker, or **`DocumentTextResult`**.
- **`OCR_DOCUMENT`** — backed by **`Document`** (+ **`DocumentSourceFile`** where applicable); OCR/HTR via worker. Shared display metadata is read from **`ArchiveItem`**; OCR-specific fields remain on **`Document`**.
- **`MANUAL_TEXT`** — backed by **`ManualTextContent.body`**; no file upload.

### Public browse-card visual previews (`/archive/`, discovery browse pages)

Browse cards are server-rendered in **`item_list_cards.html`**. Each card has:

1. **Visual preview** — stored JPEG thumbnail **or** CSS type-marker fallback (mutually exclusive branches).
2. **Text preview** — truncated metadata/transcription paragraph (`card.preview_text`), separate from the visual preview.

| Item type | Visual when thumbnail available | Visual fallback | Presign key for browse |
|-----------|--------------------------------|-----------------|------------------------|
| **PHOTO** | `<img>` from **`PhotoContent.thumbnail_file_key`** | CSS marker `--photo` | `photos/{id}/thumb_400.jpg` only |
| **OCR_DOCUMENT (IMAGE)** | `<img>` from **`Document.thumbnail_file_key`** | CSS marker `--ocr` | `documents/{id}/thumb_400.jpg` only |
| **OCR_DOCUMENT (PDF)** | Never (generation skipped) | CSS marker `--ocr` | N/A |
| **MANUAL_TEXT** | Never (no thumbnail enrichment) | CSS marker `--manual` | N/A |

**Rules (all item types):**

- Browse cards **never** presign **`PhotoContent.original_file_key`**, **`Document.file_s3_key`**, or any source-file key for list previews.
- When **`thumbnail_url`** is set, the template renders the image branch only — **no** type-marker icon is shown on the same card.
- Missing **`thumbnail_file_key`**, empty bucket config, or presign failure leaves **`thumbnail_url=None`** → CSS marker fallback.
- PDF OCR documents are excluded from document thumbnail presigning even if **`thumbnail_file_key`** is populated.

**Detail pages (unchanged from PR4 intent):**

- **PHOTO detail** (`/archive/<id>/`) — presigned GET for **`original_file_key`** (full original), not the browse thumbnail.
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

- **PHOTO** — linked **`PhotoContent`**, **`upload_status=UPLOADED`**, non-empty **`original_file_key`**
- **OCR_DOCUMENT** — **`ocr_document.upload_status=UPLOADED`**
- **MANUAL_TEXT** — no upload gate

**`PENDING`** / **`FAILED`** PHOTO rows are omitted from public browse and return **404** on detail. Staff **`/archive/manage/`** lists all PHOTO rows regardless of upload status.

---

## 1. Product definition

### What is a PHOTO archive item?

A **`PHOTO`** archive item is a **single historical/family photograph** cataloged in the unified archive. It is a first-class **`ArchiveItem`** with **`item_type=PHOTO`**, visible in the normal archive list and detail flow, with shared archival metadata (title, dates, visibility, discovery fields) on **`ArchiveItem`** and the image bytes stored separately in private S3.

**V1 product rule:** exactly **one image file per PHOTO item**. No albums, no multi-page bundles, no OCR text extraction.

### How PHOTO differs from OCR_DOCUMENT

| Aspect | **`OCR_DOCUMENT`** | **`PHOTO`** |
|--------|-------------------|-------------|
| Backing model | **`Document`** (+ **`DocumentSourceFile`**, etc.) | **`PhotoContent`** (dedicated model) |
| Primary payload | Scanned document / PDF for text extraction | Photograph for viewing |
| Processing | S3 upload → SQS → worker → OCR/HTR → **`DocumentTextResult`** | Direct create/upload → store in S3 → display via presigned GET |
| Text results | **`DocumentTextResult`** rows, review/verification lifecycle | **None** — no OCR/HTR, no worker, no Gemini/Transkribus |
| Runtime source of truth | **`ArchiveItem`** for shared metadata; **`Document`** for OCR/processing | **`ArchiveItem`** + **`PhotoContent`** |
| Staff workflows | Upload, transcription review, metadata edit, processing state | Create/upload photo, edit shared metadata, view in archive |

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
    └── OneToOne ── PhotoContent
                      └── S3 keys + file metadata + thumbnail metadata
```

**Rationale:**

- **`Document`** carries OCR-specific fields (`language`, `text_input_type`, processing state, **`DocumentTextResult`**, worker semantics) that do not apply to photos.
- **`ManualTextContent`** established the pattern: non-OCR item types get a small dedicated backing model linked **`OneToOne`** to **`ArchiveItem`**.
- Keeps PHOTO implementation isolated from OCR bridge/cutover work and from accidental worker enqueue.

**Runtime source of truth (PHOTO):**

- **`ArchiveItem`** — shared archival and discovery metadata, visibility.
- **`PhotoContent`** — image storage identity, file metadata, and browse thumbnail metadata.

---

## 3. PhotoContent fields

**Status:** Model foundation (PR2) through staff manage clarity (PR6) and browse thumbnail generation are **implemented**.

| Field | Type | Notes |
|-------|------|-------|
| **`archive_item`** | **`OneToOneField(ArchiveItem)`** | `related_name="photo_content"`, `on_delete=CASCADE` |
| **`original_file_key`** | **`CharField`** | Private S3 object key for the uploaded original |
| **`original_filename`** | **`CharField`** | Client/original filename for display/audit |
| **`original_mime_type`** | **`CharField`** | Validated MIME type (e.g. `image/jpeg`) |
| **`original_size_bytes`** | **`PositiveBigIntegerField`** | Size from S3 HeadObject **`ContentLength`** after finalize |
| **`upload_status`** | **`CharField`** | **`PENDING`** / **`UPLOADED`** / **`FAILED`** |
| **`upload_error`** | **`CharField`** | Safe error text when **`upload_status=FAILED`** |
| **`width`** | **`PositiveIntegerField`, nullable** | Populated on successful thumbnail generation (transposed dimensions) |
| **`height`** | **`PositiveIntegerField`, nullable** | Populated on successful thumbnail generation |
| **`thumbnail_file_key`** | **`CharField`, nullable** | Deterministic S3 key `photos/{id}/thumb_400.jpg` when generation succeeds |
| **`thumbnail_mime_type`** | **`CharField`, nullable** | `image/jpeg` when thumbnail exists |
| **`thumbnail_size_bytes`** | **`PositiveBigIntegerField`, nullable** | JPEG byte size when thumbnail exists |
| **`created_at`** | **`DateTimeField(auto_now_add=True)`** | |
| **`updated_at`** | **`DateTimeField(auto_now=True)`** | |

**Validation invariant:** **`ArchiveItem.item_type`** must be **`PHOTO`** when a **`PhotoContent`** row exists. **`PhotoContent.clean()`** enforces this on **`full_clean()`**.

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

**Current behavior (PR5):** Staff delete removes **`ArchiveItem`** + **`PhotoContent`** DB rows only. **S3 object cleanup is not implemented** on delete — neither **`original_file_key`** nor **`thumbnail_file_key`** objects are deleted in the delete request. Orphaned private keys require separate operational cleanup.

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

1. Viewer requests **`/archive/<archive_item_id>/`**.
2. View loads **`ArchiveItem`** + **`PhotoContent`** with **`select_related`**.
3. Access check runs **before** any S3 URL is built.
4. On success, backend calls **`create_presigned_get`** for **`original_file_key`** (full original).
5. Template renders the image. TTL is 1 hour (`PRESIGNED_GET_EXPIRY_SECONDS=3600`), consistent with other presigned GET usage.

**Public items, private objects:** S3 objects stay private; public browsing uses backend-issued presigned GET after access checks.

### List / browse cards

- When **`thumbnail_file_key`** is set and presign succeeds, browse cards show a **presigned thumbnail** (`apply_photo_thumbnail_urls_to_browse_cards`).
- Otherwise cards show the **CSS photo type marker** — not the full original.
- Browse list **never** presigns **`original_file_key`**.

---

## 6. V1 UI behavior

### Create / manage (staff/admin)

- Staff create **one PHOTO** via **`/archive/manage/new/?item_type=photo`**.
- **`/archive/manage/`** lists PHOTO items with upload-status and archive-renderability badges.
- Edit shared metadata at **`/archive/manage/<id>/edit/`** — metadata only; no file replace.

### Public / family archive surfaces

| Surface | Current behavior |
|---------|------------------|
| **`/archive/`** and discovery browse | PHOTO rows show title, dates, text preview, and **stored thumbnail** when available; **CSS photo marker** when not |
| **`/archive/<id>/`** | Detail shows **full original** via presigned GET after permission check |
| Visibility | Same rules as other item types |

### Reuse ArchiveItem fields

Use existing shared fields: **`title`**, **`visibility`**, **`metadata_status`**, dates, **`author_name`**, **`source_title`**, **`categories`**, **`events`**, **`tags`**.

**Out of scope:** captions as rich text, EXIF-driven auto-metadata, face boxes, comments, transformations, public self-service upload.

---

## 7. Upload approach

### Recommended direction (implemented in PR3)

- **PHOTO-specific** create/upload path — **not** OCR **`/api/uploads/*`**.
- Reuses shared image validation, presigned S3 PUT, HeadObject verification, and **`ArchiveItem`** services.

**Implemented flow:**

1. Staff open **`/archive/manage/new/?item_type=photo`** and submit metadata + one image file.
2. **`POST /api/photo-uploads/create/`** creates **`ArchiveItem`** (`PHOTO`) + **`PhotoContent`** (`PENDING`), returns presigned PUT for **`photos/{photo_content_id}/original.{ext}`**.
3. Browser uploads to private S3 via presigned PUT.
4. **`POST /api/photo-uploads/<photo_content_id>/complete/`** runs HeadObject, sets **`UPLOADED`** or **`FAILED`**, then best-effort **`generate_and_persist_photo_thumbnail`**.

**Create-order:** **`ArchiveItem`** + **`PhotoContent`** created **before** client S3 upload. **`upload_status`** / **`upload_error`** are explicit.

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

- Multi-photo albums / galleries per item
- Image transformations beyond thumbnail resize (watermark, CDN, etc.)
- OCR/HTR on photos
- Worker / SQS processing for PHOTO
- Face recognition, people tagging, comments
- Public (unauthenticated) upload
- Rich text captions
- S3 delete on PHOTO staff delete
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
| **`PHOTO`** | **`PhotoContent`** | None | Browse card: photo thumbnail or `--photo` marker; detail shows full original |
