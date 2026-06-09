# PHOTO Archive Items — Design / Scope

Design and implementation scope for **`PHOTO`** archive items: one photo per **`ArchiveItem`**, private S3 storage, presigned display, and no OCR/HTR pipeline.

**Status:** Design (PR1) + **model foundation (PR2)** + **staff create/upload V1 (PR3)** + **public/archive display V1 (PR4)**. Edit/delete polish remains deferred (PR5).

**Related docs:**

- `docs/ai-context/decision-log.md` — durable decisions and PR history
- `docs/ai-context/vs-archive-context.md` — broad project context
- `docs/ai-context/archive-discovery-catalog-design.md` — ArchiveItem discovery metadata direction
- `docs/ai-context/unified-ocr-upload-flow.md` — completed OCR upload flow (separate from PHOTO)

**Key code references (current behavior):**

- `documents/models.py` — **`ArchiveItem`**, **`ManualTextContent`**, **`PhotoContent`**
- `documents/services/archive_item_access.py` — visibility access control for **`/archive/`**
- `documents/services/archive_items.py` — create/edit helpers for **`OCR_DOCUMENT`** and **`MANUAL_TEXT`**
- `documents/services/upload_validation.py` — MIME/extension validation (OCR upload today)
- `documents/s3.py` — presigned PUT/GET helpers, **`S3HeadObjectResult`**
- `documents/views.py` — **`/archive/`** list/detail, unified create at **`/archive/manage/new/`**

---

## 1. Product definition

### What is a PHOTO archive item?

A **`PHOTO`** archive item is a **single historical/family photograph** cataloged in the unified archive. It is a first-class **`ArchiveItem`** with **`item_type=PHOTO`**, visible in the normal archive list and detail flow, with shared archival metadata (title, dates, visibility, discovery fields) on **`ArchiveItem`** and the image bytes stored separately in private S3.

**V1 product rule:** exactly **one image file per PHOTO item**. No albums, no multi-page bundles, no OCR text extraction.

### How PHOTO differs from OCR_DOCUMENT

| Aspect | **`OCR_DOCUMENT`** | **`PHOTO`** |
|--------|-------------------|-------------|
| Backing model | **`Document`** (+ **`DocumentSourceFile`**, etc.) | **`PhotoContent`** (proposed; dedicated model) |
| Primary payload | Scanned document / PDF for text extraction | Photograph for viewing |
| Processing | S3 upload → SQS → worker → OCR/HTR → **`DocumentTextResult`** | Direct create/upload → store in S3 → display via presigned GET |
| Text results | **`DocumentTextResult`** rows, review/verification lifecycle | **None** — no OCR/HTR, no worker, no Gemini/Transkribus |
| Runtime source of truth | **`Document`** for OCR/processing; **`ArchiveItem`** for shared fields (cutover in progress) | **`ArchiveItem`** + **`PhotoContent`** from the start |
| Staff workflows | Upload, transcription review, metadata edit, processing state | Create/upload photo, edit shared metadata, view in archive |

**Non-negotiable:** PHOTO must **not** be routed through **`Document`**, the OCR upload pipeline, worker, SQS, or any OCR/HTR provider.

### How PHOTO differs from MANUAL_TEXT

| Aspect | **`MANUAL_TEXT`** | **`PHOTO`** |
|--------|-------------------|-------------|
| Backing model | **`ManualTextContent.body`** (typed text) | **`PhotoContent`** (image file metadata + S3 keys) |
| Primary payload | Plain text in DB | Binary image in private S3 |
| Display | Auto-escaped text + line breaks in templates | `<img>` (or equivalent) via presigned GET URL after permission check |
| Upload | Inline form POST (no S3 file upload) | Staff image upload to private S3 |
| Processing | None | None (no worker) |

Both types share the same **`ArchiveItem`** shell: title, visibility, dates, metadata status, author/source display fields, and (where implemented) discovery metadata (categories, events, tags).

---

## 2. Recommended model

**Decision:** **`ArchiveItem`** remains the user-facing archival item. **`PHOTO`** is backed by a dedicated **`PhotoContent`** model — **not** **`Document`**.

```
ArchiveItem (item_type=PHOTO)
    └── OneToOne ── PhotoContent
                      └── S3 keys + file metadata
```

**Rationale:**

- **`Document`** carries OCR-specific fields (`language`, `text_input_type`, processing state, **`DocumentTextResult`**, worker semantics) that do not apply to photos.
- **`ManualTextContent`** established the pattern: non-OCR item types get a small dedicated backing model linked **`OneToOne`** to **`ArchiveItem`**.
- Keeps PHOTO implementation isolated from OCR bridge/cutover work and from accidental worker enqueue.

**Runtime source of truth (PHOTO):**

- **`ArchiveItem`** — shared archival and discovery metadata, visibility.
- **`PhotoContent`** — image storage identity and file metadata.

---

## 3. PhotoContent fields

**Status:** Model + migration implemented in **PR2** (foundation only). Upload, S3, and display remain deferred.

| Field | Type | Notes |
|-------|------|-------|
| **`archive_item`** | **`OneToOneField(ArchiveItem)`** | `related_name="photo_content"`, `on_delete=CASCADE` |
| **`original_file_key`** | **`CharField`** | Private S3 object key for the uploaded original |
| **`original_filename`** | **`CharField`** | Client/original filename for display/audit |
| **`original_mime_type`** | **`CharField`** | Validated MIME type (e.g. `image/jpeg`) |
| **`original_size_bytes`** | **`PositiveBigIntegerField`** | Size from S3 HeadObject **`ContentLength`** after finalize |
| **`upload_status`** | **`CharField`** | **`PENDING`** / **`UPLOADED`** / **`FAILED`** (PR3) |
| **`upload_error`** | **`CharField`** | Safe error text when **`upload_status=FAILED`** |
| **`width`** | **`PositiveIntegerField`, nullable** | Optional; may be populated at upload or deferred |
| **`height`** | **`PositiveIntegerField`, nullable** | Optional; may be populated at upload or deferred |
| **`thumbnail_file_key`** | **`CharField`, nullable** | **Future** — not populated in V1 |
| **`thumbnail_mime_type`** | **`CharField`, nullable** | **Future** |
| **`thumbnail_size_bytes`** | **`PositiveBigIntegerField`, nullable** | **Future** |
| **`created_at`** | **`DateTimeField(auto_now_add=True)`** | |
| **`updated_at`** | **`DateTimeField(auto_now=True)`** | |

**Thumbnail foundation:** Thumbnail columns may be added in PR2 as nullable fields even though V1 does not generate thumbnails. Alternatively, thumbnail columns may be deferred to a later migration — **but** the S3 key convention and model design must not block adding them later. Do not store thumbnails inline in the DB.

**Validation invariant:** **`ArchiveItem.item_type`** must be **`PHOTO`** when a **`PhotoContent`** row exists. **`PhotoContent.clean()`** enforces this on **`full_clean()`**; create/update services in later PRs should enforce it at write time (same pattern as **`ManualTextContent`** + **`MANUAL_TEXT`**).

---

## 4. S3 / storage strategy

### Privacy

- Original images are stored as **private S3 objects** (same bucket pattern as existing document uploads; no public ACL / no public bucket policy for photo keys).
- Display uses **presigned GET URLs** generated only after **`archive_item_access`** permission checks.

### Key convention

Use a dedicated prefix keyed by **`PhotoContent`** primary key (not **`Document`** id):

```
photos/{photo_content_id}/original.{ext}
photos/{photo_content_id}/thumb_400.{ext}   # reserved for future thumbnails
```

**Notes:**

- **`{ext}`** should come from validated upload MIME/extension mapping (e.g. `jpg`, `png`, `webp`), not from unvalidated client filenames alone.
- Thumbnail key path is **defined now** but **unused in V1** — avoids ad-hoc key migrations when thumbnail generation lands.
- If width/height are extracted at upload time (Pillow or similar), they are stored on **`PhotoContent`**; extraction failure should not block V1 upload if size/MIME verification succeeds (dimensions remain nullable).

### S3 object deletion on PHOTO delete

**Recommendation (V1):** On staff/admin **ArchiveItem** delete for **`PHOTO`**, attempt **best-effort S3 deletion** of **`original_file_key`** (and any future thumbnail keys) **after** DB row removal succeeds, inside the same request/service flow, with failures logged but not blocking DB delete.

**Rationale:** Orphan private objects are low risk (not public) but accumulate cost and clutter; best-effort delete matches staff expectation that “delete item” removes the photo. Strict two-phase “DB only until batch cleanup” is acceptable only if operational concerns arise — document in PR5 if deferred.

**Open alternative (explicit decision point):** Defer all S3 deletion to a later cleanup job (like Transkribus retention reporting). If chosen, staff delete must still remove **`ArchiveItem`** / **`PhotoContent`** rows and presigned URLs must stop working because keys are no longer served.

**PR5** should finalize delete UX (staff manage delete link, confirmation copy) and lock the S3 deletion policy if not implemented in PR3.

---

## 5. Image display / access

### Visibility / access control

**PHOTO does not introduce a new visibility level and does not redefine access control.** PHOTO reuses the existing **`ArchiveItem.visibility`** behavior exactly — the enum has **only two values** (`public`, `private`). There is **no** `FAMILY` visibility level and **no** three-tier PUBLIC/FAMILY/PRIVATE model.

Current behavior (unchanged for PHOTO; centralized in **`documents/services/archive_item_access.py`**):

| Viewer | Can view |
|--------|----------|
| Staff/admin | All **`ArchiveItem`** rows |
| Anonymous | **`public`** only |
| Authenticated **`archive_family`** group members | **`public`** + **`private`** |
| Everyone else | **`public`** only |

- **`public`** — visible to everyone (including anonymous) in **`/archive/`** and detail.
- **`private`** — visible to authenticated **`archive_family`** group members **and** staff/admin. **`private`** means private **family** archive content — **not** staff-only.

Non-viewable items return **404** through the existing helpers (same as **`MANUAL_TEXT`** and other item types today). PHOTO must not add photo-specific permission checks beyond these shared helpers.

### Presigned GET flow

1. Viewer requests **`/archive/<archive_item_id>/`** (or a dedicated image URL endpoint if added in PR4).
2. View loads **`ArchiveItem`** + **`PhotoContent`** with **`select_related`**.
3. **`can_view_archive_item(request.user, item)`** (or equivalent) runs **before** any S3 URL is built.
4. On success, backend calls existing **`create_presigned_get(bucket, key, expires_in=…)`** for **`original_file_key`**.
5. Template renders the image using the presigned URL. URL TTL should be short enough for page view (e.g. 1 hour, consistent with existing document presigned GET usage).

**Public items, private objects:** An item may be **`visibility=public`** at the **`ArchiveItem`** level while the S3 object remains **private**. Public archive browsing still uses presigned GET URLs issued by the backend after the item is determined to be viewable — do **not** make S3 objects public for “public” items.

**List page (V1):** Do **not** presign and load full original images for every row in **`/archive/`** — see §6.

---

## 6. V1 UI behavior

### Create / manage (staff/admin)

- Staff create **one PHOTO** via a **PHOTO-specific** flow (recommended: new branch on **`/archive/manage/new/?item_type=photo`** in PR3 — mirror unified create pattern, not OCR upload API).
- **`/archive/manage/`** lists PHOTO items alongside other types (existing manage list extended in later PRs).
- Edit shared metadata at **`/archive/manage/<id>/edit/`** (PHOTO branch in PR5 or alongside display PRs) — reuse **`ArchiveItem`** fields; do not invent a large photo-specific metadata schema in V1.

### Public / family archive surfaces

| Surface | V1 behavior |
|---------|-------------|
| **`/archive/`** | PHOTO rows appear with title, dates, type indicator; **placeholder/icon** for preview — **not** full original image |
| **`/archive/<id>/`** | Detail page shows **original image** via presigned GET after permission check |
| Visibility | Same rules as other item types |

### Reuse ArchiveItem fields (V1)

Use existing shared fields only:

- **`title`**, **`visibility`**, **`metadata_status`**
- **`date_start`**, **`date_end`**, **`date_precision`**
- **`author_name`**, **`source_title`** where relevant
- **`categories`**, **`events`**, **`tags`** (ArchiveItem-level discovery metadata — align with unified OCR upload direction; no separate photo taxonomy)

**Out of scope for V1 UI:** captions as rich text, EXIF-driven auto-metadata, face boxes, comments, transformations, public self-service upload.

---

## 7. Upload approach

### Options considered

| Option | Summary | Verdict |
|--------|---------|---------|
| **Reuse OCR upload API** (`/api/uploads/create/`, `create_ocr_document`, multipart flow) | Would create **`Document`**, **`DocumentSourceFile`**, enqueue worker | **Reject** — violates PHOTO non-OCR decision; spreads PHOTO conditionals through OCR code |
| **PHOTO-specific upload/create flow** | Dedicated endpoints or archive manage POST + presigned PUT; creates **`ArchiveItem`** + **`PhotoContent`** only | **Recommended** |

### Recommended direction

- **Do not** push PHOTO through **`Document`**, OCR upload pipeline, or worker enqueue.
- **Do** implement a **PHOTO-specific** create/upload path that reuses safe shared utilities:
  - **`upload_validation`** (or a thin photo-specific wrapper) — image MIME/extension allowlist
  - **`create_presigned_put`** / **`create_presigned_get`** from **`documents/s3.py`**
  - **`S3HeadObjectResult`** / HeadObject verification — confirm object exists and **`ContentType`** matches expected image type after client PUT
  - **`create_*` / `update_*` patterns** in **`archive_items.py`** — transactional **`ArchiveItem`** + **`PhotoContent`** creation
  - **`parse_archive_item_discovery_metadata_form`** / **`update_archive_item_discovery_metadata`** — if discovery fields are on the create form (consistent with OCR PR3)
- **Avoid** duplicating security logic poorly; **avoid** branching OCR upload views/services with many **`if photo`** paths.

**Implemented flow (PR3):**

1. Staff open **`/archive/manage/new/?item_type=photo`** and submit metadata + one image file.
2. **`POST /api/photo-uploads/create/`** validates MIME/extension, creates **`ArchiveItem`** (`PHOTO`) + **`PhotoContent`** with **`upload_status=PENDING`**, applies discovery metadata, returns presigned PUT for **`photos/{photo_content_id}/original.{ext}`**.
3. Browser uploads to private S3 via presigned PUT.
4. **`POST /api/photo-uploads/<photo_content_id>/complete/`** runs HeadObject, verifies S3 **`ContentType`** matches expected MIME, persists **`original_size_bytes`** from S3 **`ContentLength`**, sets **`upload_status=UPLOADED`**. Validation/verification/client failures set **`upload_status=FAILED`** + **`upload_error`**. Retryable AWS failures return **502** and leave **`PENDING`**.

**PR3 create-order decision:** Create **`ArchiveItem`** + **`PhotoContent`** **before** client S3 upload (same pattern as OCR **`Document`** `UPLOADING` + predetermined key). Upload lifecycle is explicit via **`upload_status`** / **`upload_error`** — not inferred from size. **Do not** route through **`/api/uploads/*`** or **`create_ocr_document`**.

**PR4 browse eligibility:** **`archive_browse_queryset_for_user`** includes **`PHOTO`** only when linked **`PhotoContent.upload_status == UPLOADED`** and **`original_file_key`** is non-empty (visibility/access unchanged). **`PENDING`** / **`FAILED`** PHOTO rows stay hidden from **`/archive/`** list/detail/discovery browse. Staff **`/archive/manage/`** still lists all PHOTO items regardless of upload status.

**PR4 detail display:** After **`get_viewable_archive_item`** permission/eligibility checks, detail generates presigned GET for **`original_file_key`** only (not in list V1). Missing bucket config fails safely on detail (no broken URL).

---

## 8. Explicit V1 out of scope

- Multi-photo albums / galleries per item
- Thumbnail generation (fields/key paths may exist; no worker/job in V1)
- Image transformations (resize, rotate, watermark, CDN)
- OCR/HTR on photos
- Worker / SQS processing
- Face recognition, people tagging, comments
- Public (unauthenticated) upload
- Rich text captions or body content
- Legacy **`Document`** schema cleanup
- Using full-size originals as list “thumbnails” if performance becomes heavy
- Reusing OCR transcription review or **`DocumentTextResult`** semantics

---

## 9. Suggested PR sequence

| PR | Scope |
|----|--------|
| **PR1** (this doc) | Design/scope documentation only |
| **PR2** | **`PhotoContent`** model + migration + admin (view-only) + focused model tests; **no** upload UI |
| **PR3** | Staff create/upload V1 — one photo, private S3, MIME/extension/ContentType validation, **`ArchiveItem`** + discovery metadata on create (**implemented**) |
| **PR4** | **`/archive/`** list + **`/archive/<id>/`** detail for PHOTO — visibility checks, presigned GET on detail, list placeholder/icon |
| **PR5** | Edit/delete polish — shared metadata edit, staff delete, S3 deletion policy if not done in PR3 |
| **Later** | Thumbnail generation (`thumb_400`), gallery polish, multi-photo albums, captions, advanced metadata |

**Parallel constraints:** Do not mix PHOTO PRs with rich-text work, OCR cutover PRs, or legacy **`Document`** field removal.

---

## 10. Risks / non-negotiables

| Rule | Why |
|------|-----|
| **Do not route PHOTO to OCR worker** | Prevents spurious **`DocumentTextResult`**, cost, and wrong product semantics |
| **Do not use `Document` as PHOTO backing model** | Avoids OCR bridge confusion and accidental processing |
| **Do not expose private S3 objects publicly** | S3 objects stay private; access is gated by existing **`ArchiveItem.visibility`** + presigned GET |
| **Do not introduce PHOTO-specific visibility levels or access rules** | Reuse **`archive_item_access.py`** only; no `FAMILY` tier, no staff-only **`private`** semantics |
| **Do not load full originals in archive list as fake thumbnails** | Performance and bandwidth; list uses icon/placeholder in V1 |
| **Do not skip MIME/extension/ContentType validation** | Prevents malicious or mistaken uploads |
| **Do not mix PHOTO with rich text or legacy schema cleanup** | Keeps PRs reviewable and reduces regression risk |

**Known follow-ups (non-blockers for V1):**

- Thumbnail worker or on-upload generation
- Whether **`/archive/manage/new/`** post-create redirect goes to **`/archive/<id>/`** (align with future OCR redirect decision in unified upload doc)
- Dimension extraction library choice (Pillow) vs deferred null dimensions

---

## Appendix: alignment with existing item types

| `item_type` | Backing | Processing | Archive list/detail |
|-------------|---------|------------|---------------------|
| **`OCR_DOCUMENT`** | **`Document`** | Worker + OCR/HTR | Bridge: OCR detail redirect; archive metadata on **`ArchiveItem`** |
| **`MANUAL_TEXT`** | **`ManualTextContent`** | None | **`/archive/`** + **`/archive/<id>/`** |
| **`PHOTO`** (planned) | **`PhotoContent`** | None | **`/archive/`** + **`/archive/<id>/`** from V1 |
