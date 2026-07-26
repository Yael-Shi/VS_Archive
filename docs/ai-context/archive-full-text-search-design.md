# Archive full-text search — design

**Status:** Architecture contract. **PR1**, **PR2a**, **PR2b-1**, **PR2b-2**, **PR3**, and **PR4** are implemented in application code; later hover/page-line mapping remains deferred.

**Companion decision-log entries:** `docs/ai-context/decision-log.md` — “Archive full-text search — architecture (docs-only)”, “PR1 search-index foundation (implemented)”, “PR2a discovery/manual/taxonomy sync (implemented)”, “PR2b-1 human-controlled displayed-text sync (implemented)”, “PR2b-2 automated displayed-text sync (implemented)”, “PR3 backend search cutover (implemented)”, “PR4 snippets/match-source/help text (implemented)”.

This document is the detailed contract for the implementation PR sequence. It records verified current behavior, target decisions, risks, and per-PR scope. Do not treat unimplemented later hover-mapping behavior as live.

---

## 1. Current behavior (verified)

### Public `/archive/?q=` pipeline (PR3 + PR4 live)

Entry: `archive_list_page` (`documents/views.py`) via `documents/archive_urls.py` (`archive-list`).

Order of operations:

1. `normalize_archive_public_list_type_filter(item_type)`
2. `normalize_archive_list_search_query(q)` (trim for form/URL display)
3. Base queryset: `archive_browse_queryset_for_user(request.user)` then select-related and `.order_by("-created_at")`
4. `filter_archive_items_by_public_list_type`
5. `filter_archive_items_by_search_query` — `resolve_archive_list_search_terms` then queries **`ArchiveItemSearchIndex`** via per-term FTS/substring **UNION** candidates (GIN-usable FTS arm) AND’d across terms; blank `q` leaves chronological browse; punctuation-only/overlong → empty; search reorders by relevance then `-created_at` then `pk`
6. `count()` → page / `per_page` → slice → browse cards
7. With effective `q`, PR4 attaches at most one safe contextual snippet + match-source label per card for that authorized page slice only (`apply_archive_search_match_presentation_to_cards`)

Helpers live in `documents/services/archive_item_presentation.py` and `documents/services/archive_search_snippets.py`. Index builder/persistence live in `documents/services/archive_search_index.py`. Access helpers live in `documents/services/archive_item_access.py`.

**Note:** Discovery PR5 decision-log text refers to `archive_item_queryset_for_user`. The live list path uses **`archive_browse_queryset_for_user`** (visibility **and** renderability). Auth/renderability remains the choke point **before** matching, ranking, counts, and pagination.

### Fields searched (PR3)

Via denormalized `ArchiveItemSearchIndex` (content from the PR1 builder contract):

| Index field | Weight / match |
|-------------|----------------|
| `title_text` | A — FTS and short-field `icontains` |
| `metadata_text` | B — author, source_title, categories/events/tags names, `public_note` — FTS and short-field `icontains` |
| `body_text` | C — ManualText body or displayed OCR transcription — **FTS only** (no body substring) |

### Still not searched

- Detailed `PhotoContent` fields (`description`, `location`, `context`, `people_present`, `notes`)
- Dates (`date_start` / `date_end` / `date_precision`)
- `DocumentMetadata` and legacy OCR discovery fields on `Document`
- Non-displayed `DocumentTextResult` rows (only display-helper text is indexed)

List help copy and placeholder (PR4) describe live searchable fields only (title, author, categories, events, tags, text words) — not date/place.

### Database / FTS posture (PR3)

- App DB engine: PostgreSQL (`vs_archive.settings.DATABASES`); infra image `postgres:16-alpine`.
- `django.contrib.postgres` enabled; model GIN index `archive_item_search_vector_gin` on `ArchiveItemSearchIndex.search_vector`.
- Public search uses Django `SearchQuery` / `SearchRank` (`config="simple"`, `search_type="plain"`) — no handcrafted tsquery interpolation.
- **`pg_trgm` not adopted** for PR3; short-field substring uses `icontains` on stored `title_text` / `metadata_text`.

### Displayed OCR text (reuse contract)

Canonical displayed transcription selection is already implemented in `documents/services/text_presentation.py`:

- `_latest_displayable` — non-empty text; prefer latest `SUCCEEDED`, else latest `NEEDS_REVIEW`; excludes `FAILED` / empty
- `resolve_displayed_transcription_result` / `get_displayed_transcription_text` — Hebrew docs: HEBREW then SOURCE; non-Hebrew: SOURCE then HEBREW

Browse OCR preview already calls `get_displayed_transcription_text` (`_ocr_document_preview` in `archive_item_presentation.py`).

**Search body for OCR items must follow these helpers**, not index every `DocumentTextResult` row (multi-engine uniqueness is `(document, result_type, engine)`).

`verification_status=REJECTED` is **not** filtered out of display selection today. If a REJECTED row remains displayable under those rules, it is shown. Search will follow that same rule until a separate display-policy decision changes it.

### Manual text

`ManualTextContent` (`OneToOne` → `ArchiveItem`, field `body`) is the typed body for `MANUAL_TEXT` items. It is displayed on detail/browse preview today and is **in scope** for future search indexing.

### Authorization (verified)

| Viewer | ArchiveItem visibility |
|--------|-------------------------|
| Staff / document admin | All |
| `archive_family` group | `public` + `private` |
| Anonymous / other authenticated | `public` only |

Browse renderability: PHOTO requires uploaded key; OCR_DOCUMENT requires `Document.upload_status=UPLOADED`; MANUAL_TEXT unchanged.

Hard rule for all discovery/search work (also in `archive-discovery-catalog-design.md`): no unauthorized leak of private **existence**, **counts**, titles, ranks, or snippets.

### Search independence from Transkribus geometry

Public and family search must work with **no** Transkribus transcript snapshot, binding, PAGE XML, or hover geometry. Hover/highlight is a later enhancement (`text-image-hover-design.md`) and is **not** a search dependency.

---

## 2. Target decisions

1. **Denormalized index:** one search-index row per `ArchiveItem` (1:1). Working name in this doc: `ArchiveItemSearchIndex` (final model name may vary in PR1).
2. **Indexed content:** title, author_name, source_title, category/event/tag **names**, `public_note`, `ManualTextContent.body`, and the OCR transcription returned by existing display helpers.
3. **Never indexed:** `DocumentMetadata`, technical/provider identifiers, Transkribus snapshots / PAGE XML / bindings / geometry, engine keys, review-internal fields, or other private implementation metadata.
4. **Initial non-goals for content:** detailed `PhotoContent` descriptive fields; date overlap/search. Help-text correction for date/place claims lands in PR4 only.
5. **REJECTED:** follow current display behavior — displayable ⇒ searchable. Changing that is a separate decision.
6. **Result model:** one `ArchiveItem` per hit; keep existing public type filters (`documents_and_texts` / `photo` / all), pagination (`page` / `per_page`), and visibility/family/private behavior.
7. **FTS:** PostgreSQL full-text search with text-search configuration **`simple`** for language-independent body tokenization and ranking. Do **not** treat `simple` FTS as sufficient Hebrew substring/prefix handling.
8. **Short-field substrings:** preserve substring behavior for short discovery fields (title/author/source_title and discovery names as needed). Evaluate `pg_trgm` **or** a measured Hebrew-normalization strategy **before** applying trigrams to full OCR bodies.
9. **No locator fields yet:** do not add search-result → line/page/hover payloads in PR1–PR4. Mapping is deferred until hover integration.
10. **Visibility is query-time only:** never store “who can see this” in the search index. Always filter with `archive_browse_queryset_for_user` (or equivalent visibility + renderability) **before** matching, ranking, counts, and snippets.

---

## 3. Recommended index shape

Logical fields (exact Django field names left to PR1):

| Logical field | Role |
|---------------|------|
| `archive_item` | OneToOne PK/FK |
| `metadata_text` | Concatenated short discovery strings used for indexing/debug |
| `body_text` | Manual body or displayed OCR transcription (plain text) |
| `search_vector` | Weighted PostgreSQL `tsvector` (`simple`): title/metadata high weight, body lower — **materialized on the row by persistence**, not by the pure builder |
| Optional match flags | Booleans or small enum bits for later PR4 match-source UI (title / tags / body) — only if cheap in PR1; otherwise derive at query time in PR3/PR4 |
| `updated_at` | Sync/backfill freshness |

### Pure builder vs persistence

- **Pure builder:** Deterministically selects and normalizes source-of-truth text/segments (discovery fields, `public_note`, ManualText body, displayed OCR transcription via existing helpers) and returns a **plain value object**. It performs **no** database writes and does **not** update `search_vector`.
- **Persistence layer:** Materializes/updates `ArchiveItemSearchIndex` from that value object, including writing plain text columns and computing/storing the weighted PostgreSQL `search_vector`.

Backfill (PR1) and write-path sync (PR2) both: build value object → persist row. Do not describe the pure builder as directly performing database vector updates.

**Weighting intent:** title and short discovery fields outrank long body hits; tie-break with existing `-created_at` when ranks are equal or when `q` is empty (empty `q` continues to mean “no search filter,” chronological browse).

**Auth:** index rows may exist for private items; queries must still start from the browse queryset so unauthorized users never match, count, rank, or snippet those rows.

---

## 4. Hebrew and short-field matching

### Limitation (known)

Under plain `simple` FTS, tokens are split on whitespace/punctuation without Hebrew morphology or clitic stripping.

Example: searching `מרזוק` may **not** match indexed `ומרזוק` (leading vav) or other prefixed forms. This is an accepted limitation of `simple` FTS, not a bug to “fix” by silently indexing every substring of OCR bodies.

### Strategy

- **Body (long text):** `simple` FTS + ranking; optional prefix operator on the last query token where appropriate; document Hebrew clitic/prefix gaps in UI only if product asks (not required in PR3).
- **Short discovery fields (PR3 choice):** substring via `icontains` on stored index `title_text` / `metadata_text` (comparable to pre-cutover short-field behavior). **`pg_trgm` not adopted** in PR3.
- **Do not** default to trigram GIN on full OCR `body_text` without measurement (index size, write amplification, query plans).

---

## 5. Authorization, leakage, and snippets

### Query pipeline (PR3 + PR4 live)

1. `archive_browse_queryset_for_user(user)`
2. Type filter
3. Search against the denormalized index **joined/filtered to that queryset only**
4. Rank + stable tie-break (`-created_at`, `pk`)
5. `count` / paginate
6. Build snippets / match-source labels **only** for the authorized page slice (one bounded index load; no N+1; no unauthorized rows)

### Leakage tests (required by cutover)

- Anonymous: unique private title/body must not appear in results, HTML, or counts.
- Family: can find authorized private items.
- Staff: can find private items.
- Snippets must not include text from non-visible items.
- `DocumentMetadata` terms must not match on the public archive path.

### HTML escaping (PR4 live)

OCR/manual text may contain `<`, `&`, quotes, or accidental markup. Snippet rendering uses plain `ArchiveSearchSnippetSegment` values in an autoescaped template: segment text is escaped by Django; only template-authored `<mark>` wrappers are markup. Do not use `mark_safe`, `|safe`, or raw body interpolation.

---

## 6. Sync, drift, and deployment

### Drift risk

The index is a derived cache. It can drift if:

- ManualText or displayed OCR text changes without rebuild
- Discovery metadata (title/tags/etc.) changes without rebuild
- Display selection would pick a different `DocumentTextResult` after OCR write/activation/edit, but the index still holds the old body

**Requirement:** idempotent rebuild/backfill command that can recompute any item from source of truth (ArchiveItem + ManualTextContent / display helpers) and be re-run safely. A PR1-era backfill is **not** assumed to remain fully current across the gap until PR2 sync is deployed.

### Write-path sync (PR2a + PR2b-1 + PR2b-2, not PR1)

PR1 ships pure builder + persistence helper + backfill only (no broad hooks). Between PR1 backfill and full PR2 sync going live, source writes can drift the index.

PR2 is intentionally split:

- **PR2a** — explicit sync for ArchiveItem discovery scalars/M2M, ManualText body, photo/OCR shared metadata, taxonomy name renames, plus read-only **`--check-only`** drift verification on `backfill_archive_search_index`.
- **PR2b-1** — explicit sync for **human-controlled** displayed OCR/HTR mutation paths: staff pending/verified text edits, transcription suggestion approval, corrected-current activation when `source_text_changed` or `hebrew_mirror_updated`.
- **PR2b-2** — explicit sync for **automated** displayed OCR/HTR mutation paths: worker OCR/HTR persistence, Transkribus local completion, translation persist/retry, and other automated `DocumentTextResult` writers that can change displayed `body_text`.

After **PR2a, PR2b-1, and PR2b-2** sync are **deployed and active**, operators **must** run the **full idempotent backfill again** (while sync is live), then perform **drift verification**, before any PR3 cutover. Do not treat the original PR1 backfill as sufficient for cutover. Do not cut over while only PR2a and/or PR2b-1 are live.

### Deployment order (hard rule)

1. PR1: migrate schema + GIN → full idempotent backfill.
2. PR2a: deploy discovery/manual/taxonomy sync + drift verification command mode; confirm active.
3. PR2b-1: deploy human-controlled displayed-OCR sync hooks; confirm active.
4. PR2b-2: deploy automated worker/translation displayed-OCR sync hooks; confirm active.
5. While **all** sync hooks are active: run the **full idempotent backfill again**.
6. Run/perform **drift verification** (`--check-only`).
7. Only then cut over public search behavior (PR3).
8. Ship snippet UI (PR4) — **implemented**.

Required order (short form): **PR1 migrate/backfill → PR2a sync → PR2b-1 sync → PR2b-2 sync → full backfill again while all sync is active → drift verification → PR3 cutover.**

**Do not** switch `/archive/?q=` to the index while any required backfill is partial, while PR2a / PR2b-1 / PR2b-2 sync is missing, or before post-PR2 drift verification passes. A feature flag around PR3 cutover is allowed if it makes rollback safer.

### Rollback

- PR3: revert to `icontains` filter (index table can remain).
- PR4: hide snippet fields; keep backend FTS.
- PR1/PR2: dropping the index table is possible but disruptive; prefer leaving the table unused over rushed public cutover.

---

## 7. Implementation PR sequence

### PR1 — Search index foundation (no public behavior change)

| | |
|--|--|
| **Scope** | Model 1:1 with `ArchiveItem`; **pure builder** that selects/normalizes source text into a plain value object (no DB writes); **persistence** that materializes/updates `ArchiveItemSearchIndex` including weighted `search_vector`; migration + GIN; enable `django.contrib.postgres` if required by the implementation; idempotent management command backfill (builder → persist); **no** change to `filter_archive_items_by_search_query`; **no** broad write-path hooks |
| **Likely files** | `documents/models.py`; new `documents/services/archive_search_index.py` (or similar); management command under `documents/management/commands/`; migration(s); `vs_archive/settings.py` only if adding `django.contrib.postgres`; focused tests; decision-log touch if needed |
| **Migrations** | Create search-index table; GIN on `search_vector`; no public-search behavior migration |
| **Tests** | Pure builder value object: OCR body equals `get_displayed_transcription_text`; ManualText uses `body`; FAILED/empty excluded; multi-engine picks displayable row; REJECTED still selected when displayable; metadata segments include decided fields and exclude `DocumentMetadata`; persistence writes row + `search_vector` from the value object; backfill idempotent |
| **Rollout** | Migrate → full backfill to completion on each environment. Public search remains `icontains`. This backfill alone is **not** the final pre-cutover rebuild (see PR2). |
| **Rollback** | Stop using the command; table can remain empty/unused. |
| **Non-goals** | Public FTS cutover; write-path sync hooks; snippets; `pg_trgm` on OCR bodies; locator/hover fields; photo descriptive fields; date search |

### PR2a — Discovery / ManualText / taxonomy sync + drift verification

| | |
|--|--|
| **Scope** | Id-based `sync_archive_item_search_index` API; hooks on ArchiveItem discovery scalars/M2M, ManualText, photo/OCR shared metadata, metadata-suggestion approve, discovery backfill additive links, taxonomy admin renames; `backfill_archive_search_index --check-only` |
| **Likely files** | `archive_search_index.py`; `archive_items.py`; photo upload (via discovery hook); metadata suggestion review; discovery backfill; taxonomy admins; backfill command; tests; decision-log |
| **Migrations** | None expected |
| **Tests** | Fresh-reload vs stale prefetch; same-TX rollback on sync failure; each PR2a hooked writer; taxonomy fan-out; CASCADE delete; check-only pass/fail/no-write; public `icontains` unchanged |
| **Rollout** | Deploy after PR1. Still **no** public FTS cutover. PR3 remains blocked until PR2b-1 and PR2b-2 are also active and post-sync full backfill + drift verification pass. |
| **Rollback** | Remove PR2a hooks; rely on periodic rebuild until fixed. |
| **Non-goals** | Displayed OCR mutation hooks (PR2b-1/PR2b-2); changing `/archive/?q=`; snippets; hover locators |

### PR2b-1 — Human-controlled displayed OCR text mutation sync

| | |
|--|--|
| **Scope** | Explicit sync after human-controlled writers that change displayed transcription body: `edit_pending_text_result`, `edit_verified_text_result`, transcription `approve_suggestion`, corrected-current activation when `source_text_changed` or `hebrew_mirror_updated` |
| **Likely files** | `verified_text_result_edit.py`; `transcription_suggestion_review.py`; `transkribus_corrected_current_activation.py`; `archive_search_index` call sites; tests; design/decision-log |
| **Migrations** | None expected |
| **Tests** | Pending/verified edits update `body_text`; Hebrew mirror follows display selector; suggestion approve updates body; reject skips sync; activation syncs only on source/hebrew text flags; binding-only and `ALREADY_ACTIVE` skip; same-TX rollback; public `icontains` unchanged; PR2a intact |
| **Rollout** | Deploy after PR2a. Still **no** public FTS cutover. PR3 remains blocked until PR2b-2 is also active and post-sync full backfill + drift verification pass. |
| **Rollback** | Remove PR2b-1 hooks; do not cut over PR3 while any required sync slice is rolled back. |
| **Non-goals** | Worker/translation/local-completion hooks (PR2b-2); changing `/archive/?q=`; snippet UI; hover locators; verify/reject-only paths; snapshot/preview/geometry/binding-only paths |

### PR2b-2 — Automated worker/translation displayed OCR text mutation sync

| | |
|--|--|
| **Scope** | Explicit sync at parent automated write boundaries that can change displayed transcription body: worker Phase 3 OCR/HTR success/failure (+ nested non-Hebrew translation), Transkribus local completion write path, Hebrew translation-retry persist TX |
| **Likely files** | `run_worker.py` Phase 3; `transkribus_local_completion.py`; `hebrew_translation_retry.py`; tests; design/decision-log |
| **Migrations** | None expected |
| **Tests** | Worker success (EN SOURCE / HE Hebrew selector); OCR failure demotion rebuild; local completion write vs early no-overwrite skip; translation retry single sync / duplicate no-resync; same-TX rollback; public `icontains` unchanged; PR2a/PR2b-1 intact |
| **Rollout** | Deploy after PR2b-1. Then **full backfill again while PR2a+PR2b-1+PR2b-2 sync are active**, then **drift verification**. Still **no** public FTS cutover. |
| **Rollback** | Remove PR2b-2 hooks; do not cut over PR3 while any required sync slice is rolled back. |
| **Non-goals** | Changing `/archive/?q=` behavior; snippet UI; hover locators; redoing PR2b-1 human hooks; hooking shared `persist_hebrew_translation_result` (would double-sync on worker path) |

### PR3 — Backend search cutover (no snippet UI) — **implemented**

| | |
|--|--|
| **Scope** | Replace `filter_archive_items_by_search_query` to query `ArchiveItemSearchIndex` under the browse-authorized queryset; cross-source AND multi-term semantics; weighted ranking + short-field substring boosts + `-created_at`/`pk` tie-break; preserve type filters, pagination, one-row-per-item; short-field substring via `icontains` on `title_text`/`metadata_text` (no `pg_trgm`); missing-index rows never match; safety max length on `q` |
| **Files** | `archive_item_presentation.py`; `test_archive_full_text_search.py`; PR1/PR2 “public search unchanged” regressions updated; design + decision-log |
| **Migrations** | None (`pg_trgm` not adopted) |
| **Tests** | Auth leak (existence/count/rank/pagination); title/metadata/`public_note`/ManualText/OCR body; REJECTED displayable OCR; title > metadata > body ranking; tie-break; AND + cross-source AND; normalization outcomes (blank vs punctuation-only vs underscore); short-field substring vs no body substring; empty `q`; type filter; one row per item; missing index; public-search EXPLAIN proves UNION + GIN on FTS arm (`enable_seqscan=off` for tiny fixtures) |
| **Rollout** | After PR2a + PR2b-1 + PR2b-2 sync, full backfill, and `--check-only` drift verification (production gate passed for this cutover). |
| **Rollback** | Revert `filter_archive_items_by_search_query` to pre-cutover `icontains`; index table remains. |
| **Non-goals** | Snippet/highlight UI; match-source chips; help-text rewrite (PR4); locator fields; photo field search; date search; staff `_base_queryset` FTS |

#### PR3 final query semantics

1. **Display `q`:** trim only (`normalize_archive_list_search_query`); preserved in form/URL.
2. **Internal terms (`resolve_archive_list_search_terms` → `ArchiveListSearchTerms`):**
   - `no_search`: blank/whitespace-only → no search filter (browse).
   - `no_matches`: overlong, or nonblank punctuation-only (e.g. `... !!!`) → empty result set (not full archive).
   - `search`: collapse whitespace; split on ordinary punctuation **and underscore** (`[\W_]+`); drop empty terms; `config="simple"` at query time.
3. **Max length:** `ARCHIVE_LIST_SEARCH_QUERY_MAX_LENGTH = 200` on the trimmed display string. Overlong → `no_matches`; display string unchanged.
4. **Multi-term:** AND. Every term must match; terms may hit different sources/fields; order/adjacency do not matter. No OR fallback; no phrase/minus/paren syntax.
5. **Per-term match (decomposed for GIN):** authorized PK candidates are `FTS ∪ title_text icontains ∪ metadata_text icontains` via Django `QuerySet.union`, then AND’d across terms with successive `pk__in`. A single `@@ OR ILIKE OR ILIKE` WHERE can force a seq scan and prevent meaningful `archive_item_search_vector_gin` participation; UNION keeps the FTS `@@` SELECT independently indexable. No `body_text` substring/prefix arm.
6. **Ranking:** `SearchRank(search_vector, AND of terms)` + title substring boost `1.0` (weight A) + metadata substring boost `0.4` (weight B); each short-field boost applies once if any term hits that field (not summed per term); order `-relevance`, `-created_at`, `pk`.
7. **Missing index:** no match, no crash, no GET-time rebuild.
8. **Hebrew/`simple`:** body clitic/prefix gaps remain accepted limitations; short-field substring covers partials only on title/metadata.
9. **Remaining tradeoff:** short-field `icontains` branches are still unindexed scans of the denormalized short text columns; they run as separate UNION arms rather than poisoning the FTS index path.

### PR4 — Snippets, match sources, help text — **implemented**

| | |
|--|--|
| **Scope** | Safe contextual snippets for the authorized page slice; autoescaped segment + `<mark>` highlighting; match-source Hebrew labels; correct misleading date/place search help text / placeholder |
| **Files** | `archive_search_snippets.py`; `ArchiveBrowseCard` fields; `archive_list_page` wiring; `list.html` / `item_list_cards.html`; CSS; `test_archive_full_text_search.py`; design + decision-log |
| **Migrations** | None |
| **Tests** | OCR/Manual body labels + contextual snippet; no-q preview unchanged; title-only no fabricated body excerpt; metadata/discovery labels; multi-term nearby/far; ellipses/deterministic window; XSS escape; private leakage; page-slice only; no N+1; type filter/pagination; PR3 order unchanged; help text |
| **Rollout** | After PR3 stable. |
| **Rollback** | Hide snippet UI fields; keep FTS backend. |
| **Non-goals** | Hover overlay; line/page geometry; Transkribus binding requirements; search backend/ranking changes |

#### PR4 presentation semantics

1. **No effective `q`:** cards keep ordinary beginning-of-text `preview_text` with no match-source row.
2. **One snippet max** per card when body whole-tokens match query terms. Target ~190 chars (band 160–220); CSS line-clamp still 3. Window maximizes distinct query terms within max length; tie-break earliest match. Whole-word edges where practical; leading/trailing `…` only when omitted. No page/line locators.
3. **Body vs other sources:** useful body match → replace preview with snippet; labels **`נמצא בתעתוק`** (OCR) / **`נמצא בטקסט`** (ManualText). Prefer body snippet even when title/metadata also match (ranking unchanged).
4. **Title-only:** no unrelated body excerpt; title remains the visible explanation (no match-source chip required).
5. **Metadata/public_note/discovery-only:** keep ordinary preview; show a specific Hebrew label when exactly one metadata source matches, else **`נמצא בפרטי הפריט`**. Never claim a field unless it contains a normalized query term (`icontains`-aligned for short fields; whole-token for body).
6. **Highlighting:** Unicode-aware case-insensitive whole-token marks inside the selected snippet only; does not change match membership. No Hebrew morphology / fuzzy / body substring.
7. **Performance/auth:** snippets after auth + pagination; one `ArchiveItemSearchIndex` load for the page ids; no per-result queries; never load all matching bodies before pagination.

### Later — Search ↔ hover mapping (optional)

When hover/highlight is implemented, optionally attach search-result → page/line mapping. Requires a separate design aligned with `text-image-hover-design.md`. **Out of scope** for PR1–PR4. Must remain optional: search continues to work with no snapshot/binding/geometry.

---

## 8. Explicit cross-cutting non-goals

- Treating Transkribus snapshots, bindings, or PAGE XML as required for search
- Indexing `DocumentMetadata` for the public archive path
- Denormalizing visibility into the search index
- Changing `REJECTED` / display-selection policy inside search PRs
- Date search and detailed photo-content field search in the initial sequence
- Staff document-list / review-backlog FTS cutover in the same PRs
- SQS/retry/DLQ redesign
- Fake or approximate hover highlighting

---

## 9. Self-check against audit

| Audit fact | This design |
|------------|-------------|
| Browse QS then type then index search (was `icontains` metadata) | §1 current behavior (PR3) |
| ManualText/OCR body searchable via index | §1 PR3; builder/display helpers |
| Postgres 16; GIN on `search_vector`; no `pg_trgm` in PR3 | §1 |
| Use display helpers, not all DTR rows | §1 + §2 |
| No snapshot/binding dependency | §1 + §8 |
| Auth before match/rank/count/snippet | §2 #10, §5 |
| Denormalized 1:1 index | §2 #1, §3 |
| `public_note` in; photo details/dates out initially | §2 #2–4 |
| REJECTED follows display | §2 #5 |
| `simple` FTS + Hebrew substring caveat | §2 #7–8, §4 |
| No locators in initial PRs | §2 #9, Later |
| Pure builder → value object; persistence materializes `search_vector` | §3 |
| PR1 migrate/backfill → PR2a → PR2b-1 → PR2b-2 → full backfill again → drift check → PR3 → PR4 | §6–§7 |
| Cutover only after post-PR2a+PR2b-1+PR2b-2 backfill + drift verification | §6 |

PR3 cutover and PR4 snippet/help-text presentation match the verified audit and the product decisions recorded in the PR3/PR4 decision-log entries. Hover/page-line mapping remains deferred.
