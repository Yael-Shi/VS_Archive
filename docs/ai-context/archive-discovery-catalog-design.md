# Unified Archive Discovery / Catalog Metadata Design

This document defines the **target direction** for public archive discovery and catalog metadata across **`ArchiveItem`** item types — categories, events, tags, search/discovery, visibility rules, and the role of **`DocumentMetadata`**.

**Status:** Design only (PR0). **Does not change runtime behavior.**

**Related docs:**

- `docs/ai-context/decision-log.md` — durable decisions and PR history
- `docs/ai-context/vs-archive-context.md` — broad project context
- `docs/ai-context/ocr-archiveitem-cutover.md` — shared-field cutover (separate from discovery metadata)

**Key code references (current behavior):**

- `documents/models.py` — **`ArchiveItem`**, **`Document`**, **`DocumentMetadata`**, **`Tag`**
- `documents/services/archive_item_access.py` — visibility access control for **`/archive/`**
- `documents/services/archive_items.py` — create/edit helpers for **`OCR_DOCUMENT`** and **`MANUAL_TEXT`**
- `documents/views.py` — **`/archive/`** list/detail, OCR metadata edit at **`/archive/manage/<id>/edit/`**

---

## 1. Purpose

VS-Archive is evolving from an OCR processing and staff review system into a **public and family archive discovery interface**. Users need to find items by topics, events, tags, source/author context, and dates — across item types, not only OCR-backed documents.

This design document records **target architecture and product decisions** before implementation. It answers:

- Where future **public/cross-item discovery metadata** should live
- What **`DocumentMetadata`** is and is **not** for
- How categories, events, and tags differ
- How visibility and privacy must constrain every future discovery surface

**This PR (PR0) is documentation only.** No models, migrations, templates, search, or clickable discovery links are implemented here.

---

## 2. Current state

### ArchiveItem is central

**`ArchiveItem`** is the long-term central archival content entity. The unified archive product surface is **`/archive/`** (list and detail direction), not separate mini-sites per item type.

**Current `item_type` values:**

| `item_type` | Backing model | Status |
|-------------|---------------|--------|
| **`OCR_DOCUMENT`** | **`Document`** (`OneToOneField` via **`Document.archive_item`**) | Implemented |
| **`MANUAL_TEXT`** | **`ManualTextContent`** (`OneToOneField` to **`ArchiveItem`**) | Implemented |
| **`PHOTO`** | **`PhotoContent`** (`OneToOneField` to **`ArchiveItem`**) | Implemented (V1 — see **`docs/ai-context/photo-archive-items.md`**) |

### ArchiveItem-owned shared archival fields

For all item types, **`ArchiveItem`** is canonical for these six shared archival fields:

- **`title`**
- **`visibility`**
- **`metadata_status`**
- **`date_start`**
- **`date_end`**
- **`date_precision`**

For **`OCR_DOCUMENT`**, **`Document`** retains **compatibility mirrors** of the six fields (updated from **`ArchiveItem`** on staff edit and create paths). See **`docs/ai-context/ocr-archiveitem-cutover.md`**.

### ArchiveItem-owned source metadata

**`ArchiveItem`** also has bibliographic/source display fields:

- **`author_name`**
- **`source_title`**

These are **public display metadata** when an item is visible to the viewer. Whether they become **clickable/filter links** is **not decided** in this design.

### Document remains OCR/runtime only

**`Document`** remains the source of truth for OCR-specific and processing fields: **`doc_type`**, **`language`**, **`text_input_type`**, upload/processing state, file storage, **`DocumentSourceFile`**, **`DocumentTextResult`**, and related runtime artifacts.

### OCR-side discovery/catalog fields (transitional)

These fields still exist on the **`OCR_DOCUMENT`** side only:

| Field / model | Location | Role today |
|---------------|----------|------------|
| **`category_event`** | **`Document`** | Single transitional field mixing category and event semantics |
| **`tags_m2m`** | **`Document`** → **`Tag`** | Transitional OCR-side tags |
| **`donor`** | **`DocumentMetadata`** | Staff/admin metadata |
| **`collection`** | **`DocumentMetadata`** | Staff/admin metadata |
| **`original_location`** | **`DocumentMetadata`** | Staff/admin metadata |
| **`notes`** | **`DocumentMetadata`** | Staff/admin metadata; public vs internal split **undecided** |

Staff edit **`OCR_DOCUMENT`** catalog scalar metadata and tags at **`/archive/manage/<id>/edit/`**; persistence remains on **`Document`** / **`DocumentMetadata`** with **no** **`ArchiveItem`** mirror.

**`MANUAL_TEXT`** items have **no** equivalent category/event/tag/catalog fields yet.

---

## 3. Problem statement

The archive is becoming a **discovery interface**, not only an OCR processing UI. Users need to search and browse by:

- broad **topics** (categories)
- specific **family/historical occasions** (events)
- flexible **labels** (tags)
- **source/author** context
- **dates**

**Current gaps:**

1. **`category_event`** is a **legacy transitional field** that mixes category and event in one string on **`Document`**.
2. **`Document.tags_m2m`** exists only on **`OCR_DOCUMENT`**; **`MANUAL_TEXT`** and future **`PHOTO`** items have no shared tag model.
3. **`DocumentMetadata`** is **OCR-side** and was introduced for staff/admin operational metadata — it must **not** become the unified public discovery model as the archive grows cross-item.
4. **`author_name`** / **`source_title`** live on **`ArchiveItem`** but are not yet part of a unified discovery taxonomy.
5. **Public discovery** must preserve the same **visibility/access policy** as **`/archive/`** — no leakage of private items, hidden counts, or internal metadata.

Building long-term public discovery on **`DocumentMetadata`** or OCR-only fields would block **`MANUAL_TEXT`**, **`PHOTO`**, and unified archive search.

---

## 4. Definitions

### Category

A **broad archival/content topic**, not merely a document type or MIME category.

Examples:

- רופאת משפחה
- יהדות מצרים
- הפרשה
- עליה ממצרים

### Event

A **specific family/historical occasion** or concrete event — often narrower and more particular than a category.

Examples:

- a particular wedding
- a particular bar mitzvah
- a specific family/historical event

### Tag

A **flexible/free-form label** for search and discovery.

Examples:

- people names
- place names
- family names
- event/category names during transition
- other useful search/discovery terms

Tags may overlap with categories or events during migration; the long-term model should still treat these concepts separately.

### Source metadata

Bibliographic/publication context such as **`author_name`** and **`source_title`** on **`ArchiveItem`**. Describes who wrote or published the source material, not OCR engine or processing identity.

### Internal/admin metadata

Metadata useful for **staff/admin** workflows but **not necessarily public** — e.g. donor provenance, internal collection notes, physical original location. Distinct from public discovery metadata even when stored in the same edit UI during transition.

### Document type / item type (explicitly separate from category)

**`ArchiveItem.item_type`** (`OCR_DOCUMENT`, `MANUAL_TEXT`, `PHOTO`) and OCR **`Document.doc_type`** describe **what kind of archive item** this is technically — not its thematic category.

Example: an **`OCR_DOCUMENT`** letter and a **`MANUAL_TEXT`** typed memoir can share the category **יהדות מצרים**; item type does not replace category.

---

## 5. Decisions made now

- **`ArchiveItem`** remains the **central archival entity** for cross-item public discovery direction.
- **Public/cross-item discovery metadata** should be **`ArchiveItem`**-level or **linked to `ArchiveItem`** — not **`Document`**-only and not **`DocumentMetadata`**-centric.
- **`DocumentMetadata`** will **not** become the unified public discovery/catalog metadata model.
- **`donor`** is **private/internal** for now.
- **`collection`** is **private/internal** for now, even though it **may** become public later.
- **`original_location`** is **private/internal/admin** metadata for now.
- **`notes`** remain an **open question** — do not decide yet whether notes are public, internal, or split.
- **`category_event`** is a **current transitional legacy field** on **`Document`** that mixes category and event.
- **`Document.tags_m2m`** is a **transitional OCR-side** tags implementation.
- Future **categories**, **events**, and **tags** should be **`ArchiveItem`**-level or linked to **`ArchiveItem`**.
- **`ArchiveItem` ↔ `ArchiveCategory`** must be **many-to-many** from the **model foundation PR (PR1)** — one item may have multiple categories; one category may contain many items.
- **`ArchiveItem` ↔ `ArchiveEvent`** must be **many-to-many** from **PR1** — one item may link to zero, one, or multiple events; one event may contain many items.
- **`ArchiveItem` ↔ tags** must be **many-to-many** from **PR1** — one item may have multiple tags; one tag may label many items.
- **`author_name`** / **`source_title`** are **public display metadata** when the item is visible; **clickability** (filter/browse links) is **not decided** now.

---

## 6. Target architecture direction

**Not implemented in PR0.** Possible future model direction:

### Categories — `ArchiveCategory`

- Curated or staff-managed category entities (name, optional slug, optional description).
- **Many-to-many** with **`ArchiveItem`** from **PR1**: one **`ArchiveItem`** may have **multiple** categories; one **`ArchiveCategory`** may contain **many** **`ArchiveItem`** rows.
- Replaces long-term use of **`Document.category_event`** for category semantics.

### Events — `ArchiveEvent`

- Normalized or semi-normalized event entities (name, optional date range, optional description).
- **Many-to-many** with **`ArchiveItem`** from **PR1**: one **`ArchiveItem`** may link to **zero, one, or multiple** events; one **`ArchiveEvent`** may contain **many** **`ArchiveItem`** rows.
- Replaces long-term use of **`Document.category_event`** for event semantics.

### Tags — `ArchiveItem`-level

- **Many-to-many** with **`ArchiveItem`** from **PR1**: one **`ArchiveItem`** may have **multiple** tags; one tag may label **many** **`ArchiveItem`** rows.
- **Model naming** (not cardinality) remains open for PR1:
  - **Reuse** existing **`Tag`** with an **`ArchiveItem`** M2M, or
  - Introduce **`ArchiveTag`** / **`ArchiveItemTag`** if OCR **`Document.tags_m2m`** semantics or lifecycle do not fit.

Tags remain **flexible labels**; categories and events remain **structured discovery dimensions**.

### Document stays OCR/runtime only

**`Document`** continues to own OCR, upload, processing, and review artifacts. It does **not** become the long-term home for unified public discovery metadata.

### DocumentMetadata stays internal/admin OCR metadata

**`DocumentMetadata`** remains **`OCR_DOCUMENT`**-side staff/admin metadata unless a future cross-item internal model is introduced (see section 7 and PR9).

### Search and discovery surfaces (future)

Future public search, filter chips, and browse pages should query **`ArchiveItem`** (and linked discovery entities) through the same access helpers as **`/archive/`**, not raw **`Document`** or **`DocumentMetadata`** tables exposed to anonymous users.

---

## 7. DocumentMetadata policy

**`DocumentMetadata` is not the future unified catalog/discovery model.**

| Aspect | Policy |
|--------|--------|
| **Role** | **`OCR_DOCUMENT`**-side **internal/admin/operational** metadata |
| **Staff workflows** | May continue to support existing staff edit and backlog flows |
| **New public discovery** | **Must not** be built on **`DocumentMetadata`** fields |
| **Cross-item internal metadata** | If **`MANUAL_TEXT`** or **`PHOTO`** later need comparable internal fields, consider a separate **`ArchiveItemInternalMetadata`** (or per-type internal tables) in a **future design** — do not stretch **`DocumentMetadata`** to cover all item types |

**Field-level policy (now):**

| Field | Visibility (now) |
|-------|------------------|
| **`donor`** | Private/internal |
| **`collection`** | Private/internal (may become public later — open question) |
| **`original_location`** | Private/internal/admin |
| **`notes`** | Open — public, internal, or split undecided |

**Do not** expose **`donor`**, **`collection`**, **`original_location`**, or undecided **`notes`** on anonymous public discovery endpoints or pages without an explicit future decision and access review.

---

## 8. Visibility and privacy rules

Every future discovery/search/filter/category/event/tag page must use the **same viewer access policy** as **`/archive/`** (via **`archive_item_access`** helpers and equivalent query filtering).

| Viewer | Access |
|--------|--------|
| **Anonymous** | **Public** items only |
| **Approved/family** (`archive_family` group) | Items they are **authorized** to see (public + permitted private family content) |
| **Staff/admin** | **All** items |

**Hard rules:**

- **No discovery endpoint** may leak **private/family-only metadata**, **hidden item counts**, or **hidden item existence** to unauthorized viewers.
- Filter/count APIs must not reveal “N private items matched your query” to anonymous users.
- **Internal/admin fields** (**`DocumentMetadata`**, private collection/donor, etc.) must not appear on public discovery surfaces unless explicitly approved and access-filtered.
- Non-viewable items should behave like **`/archive/`** today: **404** or equivalent non-disclosure, not “exists but forbidden” discovery hints.

---

## 9. Proposed future PR chain

Phased implementation plan. **PR0 is this design doc only.**

| PR | Scope |
|----|--------|
| **PR0** | This design document + decision-log entry |
| **PR1** | Model foundation for **`ArchiveItem`**-level categories/events/tags |
| **PR2** | First-party edit UI for **`ArchiveItem`** discovery metadata |
| **PR3** | Public display of **`ArchiveItem`**-level discovery metadata |
| **PR4** | Backfill/reconciliation/report from legacy **`Document.category_event`** and **`Document.tags_m2m`** |
| **PR5** | Public archive search |
| **PR6** | Clickable category/event/tag browse pages |
| **PR7** | Deprecate legacy OCR-side discovery fields |
| **PR8** | **`DocumentMetadata`** policy hardening (access, admin vs public boundaries) |
| **PR9** | Optional **`ArchiveItemInternalMetadata`** if internal metadata is needed across all item types |
| **PR10** | **`PHOTO`** readiness/design checkpoint |

**Explicitly out of this chain (unless separately approved):** PR5g shared-field schema cleanup from **`ocr-archiveitem-cutover.md`**; OCR/HTR, upload, worker, routing, **`DocumentTextResult`** review semantics.

---

## 10. Open questions

- Should **`collection`** ever become **public** discovery metadata?
- Should **`notes`** split into **`public_notes`** and **`internal_notes`**?
- Should **categories** be **curated only** or **staff-creatable inline** at edit time?
- Should **tags** be **free-form** or **curated** (or hybrid)?
- Should **events** be **normalized entities** with dates/descriptions, or lighter-weight labels?
- Should **`author_name`** / **`source_title`** ever become **clickable** filter/browse links?
- Should existing **`Tag`** be **reused** for **`ArchiveItem`** tags, or should **`ArchiveTag`** be introduced?
- How should **Hebrew/English slugs** work for category/event/tag browse URLs?
- How to **map legacy `category_event` values** safely during backfill (split heuristics vs manual review)?

---

## 11. Non-goals

**PR0 and this design doc do not include:**

- Any **implementation** (Python, templates, migrations)
- **PR5g** — dropping duplicated **`Document`** shared columns
- **`PHOTO`** item implementation
- **Upload**, S3, worker, OCR/HTR, Gemini, Transkribus, routing changes
- **`DocumentTextResult`** review/status/verification changes
- **Permissions** redesign beyond documenting visibility rules for future discovery
- **Search** implementation
- **Clickable** tags/categories/events
- **Splitting `category_event`** in code
- **Multiple categories** in code
- **Moving** tags/**`category_event`** to **`ArchiveItem`** in code
- **Changing `DocumentMetadata`** implementation
