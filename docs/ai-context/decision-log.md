# VS-Archive Decision Log

## Author → Person explicit link foundation (PR1)

**Decision / implemented:** Person and Author remain distinct identities.
`Author` may optionally reference at most one `Person` through an explicit
nullable FK (`Author.person`, `on_delete=SET_NULL`, related_name
`author_identities`). One Person may have multiple bibliographic Author
identities. Linkage is staff-explicit only and is never inferred from
names, including exact-name equality (review candidate only, never
identity evidence). Public People/Author catalog, detail, and navigation
unification is deferred to PR2.

**Current behavior:**

- Schema migration `0063_author_person_link` is additive and nullable.
  There is no name-based data backfill.
- Staff Author edit can show the linked Person, link by `Person.id`
  (staff Person picker options; ids are identity), or unlink. It does
  not create Person rows.
- Author merge: neither linked → unchanged; exactly one linked → keeper
  retains that Person; both linked to the same Person → allowed; both
  linked to different Persons → fail closed before writes
  (`AUTHOR_MERGE_PERSON_CONFLICT_ERROR`). Existing transactional lock
  order is unchanged; Person is copied onto the keeper before duplicate
  Author delete when needed.
- Person merge repoints Authors whose `person_id` is the duplicate onto
  the keeper (relation repointing, not inference). Preview/result expose
  affected Author-identity counts and the duplicate’s linked Authors.
  Duplicate Person delete remains last. Unlinked Authors stay unlinked.

**Deferred:** public unification of People/Author catalog and detail;
public navigation; automatic or name-based linking.

**Tests:** `documents/test_author_person_link.py`, plus Author/Person
merge cases in `documents/test_author_merge.py`,
`documents/test_author_merge_staff_ui.py`, and
`documents/test_person_merge.py`.

## Public structured-Author search and advanced Author filter

**Decision / implemented:** Public `/archive/` `q` discovery and the advanced
`author` filter cut over from **`ArchiveItem.author_name`** to structured
**`Author` / `ArchiveItemAuthor`** identity, with a narrow fail-closed legacy
fallback. This supersedes the deferred “global `q` / advanced author filter
cutover” notes on the public Author catalog, staff Author, and Author-merge
entries.

**Current behavior:**

- **`q` indexing and match-source attribution** share
  **`searchable_author_names_for_item`**: when any **`ArchiveItemAuthor`**
  row exists, ordered **`Author.name`** values (position, then id) are used
  and **`author_name` is ignored**, including stale/drifted strings. When an
  item has **zero** links, trimmed **`author_name`** remains the fallback.
  Weighting is unchanged (author text stays in **`metadata_text`**, weight B).
  Search-index build prefetches ordered **`author_links`**.
- Advanced GET **`author`** is **one positive `Author.id`**, not a name
  string. Malformed values are skipped. Repeatable params keep the first
  valid id. Filtering uses correlated **`Exists`** on
  **`ArchiveItemAuthor.author_id`** (no join fan-out).
- Legacy **`author_name`** membership is added **only** for items with zero
  structured links, **only** when the selected **`Author.name`** is
  **globally unique**, and **only** when that Author already has structured
  **`ArchiveItemAuthor`** membership in the **already-authorized queryset**
  passed to **`filter_archive_items_by_advanced_filters`**. The filter
  service does not take a user; authorization is that queryset. A manually
  supplied Author id with no structured membership there cannot enable
  leftover **`author_name`** matches (including a private-only Author id
  against a public zero-link item with the same name). Duplicate Author
  names never infer a leftover **`author_name`** onto one of those
  identities.
- Advanced Author choices are **`Author`** rows with at least one
  **`ArchiveItemAuthor`** link to the authorized browse queryset (order
  **`name`, `id`**). **`author_name`-only** items are not choice membership.
  The control stays single-select; option values are ids and labels are
  names. Duplicate names remain distinct options. Chips/removal,
  pagination, `q`, type, and other advanced filters preserve the Author id.
  Ordinary and `q`-only requests still skip choice-context queries.

**Rollout (required):** This cutover changes **`ArchiveItemSearchIndex.metadata_text`**
semantics. Existing index rows still contain pre-cutover author text until
rebuilt. After deploy, operators **must** run **`backfill_archive_search_index`**
before the new public ``q`` author contract is considered live. Write-path
sync of later edits is not a substitute for rebuilding the full table.
Verify after backfill: a structured item whose stale **`author_name`** token
differs from linked **`Author.name`** values must **not** match that stale
token, and **must** match the structured **`Author.name`** token(s). Then
**`backfill_archive_search_index --check-only`**. Do not treat ``q`` as cut
over until that verification passes.

**Unchanged:** Person filter/chips; public Author catalog/detail membership;
PHOTO Author staff UI; navigation; FTS ranking/weights besides the author
string source.

**Tests:** `documents/test_archive_search_author.py`,
`documents/test_archive_advanced_search_author.py`; updates in
`test_archive_advanced_search.py`, `test_archive_advanced_search_ui.py`,
`test_archive_advanced_search_person.py`,
`test_archive_item_author_public_display.py`, `test_author.py`,
`test_archive_item_author_staff_ux.py`. No migration.

## Text quality — SOURCE_TEXT → HEBREW_TEXT inheritance for Gemini translations

**Decision / implemented:** Successful Gemini Hebrew translations of
**non-Hebrew** OCR documents persist `HEBREW_TEXT.quality` as the inherited
same-engine `SOURCE_TEXT` persisted base quality. The writer is
`persist_hebrew_translation_result`; it calls
`capped_inherited_base_quality(source.quality)` with no candidate score.
There is no independent translation quality score.

**Current behavior:**

- Applies to successful initial OCR translation persist and successful
  Hebrew-translation retry (both already call the shared persist helper).
- Inheritance uses persisted `SOURCE_TEXT.quality` only (`UNKNOWN` / `LOW` /
  `MEDIUM` / `GOOD`). `verification_status` and effective/public quality are
  not used. `HUMAN_VERIFIED` / `NEEDS_CORRECTION` are never persisted.
- Missing same-engine SOURCE_TEXT fails closed: persist still succeeds,
  `based_on_source_revision` stays `None`, quality is `UNKNOWN`.
- Failed translation persist is unchanged (no quality inheritance).
- Hebrew-native `HEBREW_TEXT` (including Transkribus OCR mirror via
  `_save_htr_results`) is unchanged: it is not a translation and does not
  copy `HtrResult.quality`.
- No backfill/migration. Existing translation rows keep stored quality until
  a successful re-persist.

**Supersedes:** unwired-inheritance notes on Text quality PR1 / PR2 / PR3
(`capped_inherited_base_quality` remains unwired; translation `HEBREW_TEXT`
quality unchanged).

**Tests:** `documents/test_text_quality.py`
(`TextQualityTranslationInheritanceTests`); retry coverage in
`documents/test_hebrew_translation_retry.py`; Hebrew-native / Transkribus
unchanged in `documents/test_arabic_printed_text_quality.py`.

## Worker Hebrew translation runs outside the OCR persist transaction

**Decision / implemented:** On the initial non-Hebrew OCR success path,
`translate_text_to_hebrew_with_gemini` runs **after** Transkribus local
completion (if any) and **before** the Phase 3 `transaction.atomic()` that
persists `SOURCE_TEXT`, `HEBREW_TEXT`, processing-state rollup, and search-index
sync. The Gemini call is no longer nested inside that persist transaction.

**Unchanged:** Translation failure still persists `HEBREW_TEXT` as
`HEBREW_TRANSLATION_FAILED` and does not fail the OCR row. Index-sync failure
still rolls back SOURCE + translation together. Hebrew-translation retry
already called Gemini outside its persist TX. Routing and models are unchanged.
`persist_hebrew_translation_result` is extended separately by the later
SOURCE_TEXT → HEBREW_TEXT quality-inheritance decision above.

**Tests:** `RunWorkerBehaviorTests.test_non_hebrew_translation_gemini_call_sees_no_persisted_text_rows`
in `documents/tests.py`; existing
`test_worker_sync_failure_rolls_back_ocr_and_translation`.

## Arabic printed ambiguous Vision / provider-create fences (runbook)

**Decision / documented:** Fail-closed Vision and Antigravity create fences are
intentional. Operators must diagnose read-only and must not reset reservation
counters. Runbook:
`docs/ai-context/arabic-printed-ambiguous-fence-runbook.md`.

**Unchanged:** reclaim/reprocess eligibility, page budget, cancel recovery,
quality scoring.

## Public Author catalog and Author detail

**Decision / implemented:** Public Author browsing exists at
**`/archive/authors/`** (`archive-authors-index`) and
**`/archive/authors/<author_id>/`** (`archive-author-detail`). Membership is
**`ArchiveItemAuthor` only**. **`Author`** remains fully separate from
**`Person`**. This supersedes the deferred “public Author browsing / pages”
notes on the staff Author index and Author merge entries.

**Current behavior:**

- Catalog membership is Authors with at least one authorized +
  browse-renderable linked **`ArchiveItem`** via **`ArchiveItemAuthor`**.
  Authorization is **`archive_browse_queryset_for_user`**. Unlinked Authors,
  **`author_name`-only items**, Person links, and inaccessible /
  non-renderable-only links are omitted. Detail 404s in those cases.
- Index order is **`Author.name`, then `id`**. Pagination is fixed **48**.
  Optional GET **`q`** is a case-insensitive substring on **`Author.name`**
  only (no aliases, Person, or **`ArchiveItem.author_name`**). Duplicate
  names stay separate Author IDs and URLs.
- Each index row shows **`Author.name`** and a DISTINCT linked item count
  (Hebrew **`1 פריט` / `N פריטים`**). Counts are one page-restricted
  **`ArchiveItemAuthor`** aggregate (`Count(..., distinct=True)`), not
  per-row queries.
- Author detail H1 is **`Author.name`**, heading **פריטים קשורים**, then
  DISTINCT authorized+renderable linked ArchiveItems. Cards use the shared
  browse-card path (`build_archive_browse_cards` + thumbnail helpers) and
  **normal item detail URLs** (no Person-style photo deep-links). Order is
  **`-created_at`, `pk`**. Pagination is fixed **48**.
- PHOTO structured Author staff UI is unchanged and still out of scope.
  Existing **`ArchiveItemAuthor`** rows on renderable PHOTO items still
  count.

**Unchanged / still deferred:** public navigation links to these routes;
staff Author edit/merge behavior; PHOTO author UI; schema. Global **`q`**
and the advanced **`author`** filter now use structured Authors (see
**Public structured-Author search and advanced Author filter**).

**Public item Author presentation (cards / source metadata):** when an item
has one or more **`ArchiveItemAuthor`** rows, public cards and non-PHOTO
source metadata render those **`Author.name`** values in **`position`**
order as links to **`/archive/authors/<author_id>/`**. Duplicate names stay
distinct IDs/URLs. Structured links take precedence over
**`ArchiveItem.author_name`** (including stale/drifted or empty strings).
When there are **no** structured links, the existing trimmed
**`author_name`** text is shown unchanged (detail still applies the
placeholder-metadata filter). **`author_name` is never parsed or used to
infer Author links.** PHOTO **detail** still has no Author/source-metadata
surface. PHOTO **cards** already showed **`author_name`** and now use the
same structured-vs-fallback rule. Prefetch is ordered
**`author_links` + `select_related("author")`** on browse and public
detail querysets (no template queries).

**Tests:** `documents/test_archive_authors_public_index.py`,
`documents/test_archive_author_public_page.py`,
`documents/test_archive_item_author_public_display.py`; updated route
expectation in `documents/test_author_staff_index.py` and browse-card
author assertions in `documents/test_archive_item.py`. No migration.

## Checkpoint-backed Arabic printed banded PARTIAL documents remain reprocessable

**Decision / implemented:** Arabic printed banded OCR can terminate a document
as `PARTIAL` before any `DocumentTextResult(SOURCE_TEXT)` row exists. Staff OCR
reprocess eligibility now treats unfinished pages on the **worker-reusable**
`ArabicPrintedOcrAttempt` as durable resume evidence.

**Attempt selection:** Multiple attempts can exist for one document
(`uniq_ar_pr_ocr_attempt_identity` is `(document, identity_fingerprint)`). The
worker reuses `get_or_create(document, identity_fingerprint)` built from current
pages plus current route/prompt/banding constants. Staff eligibility does not
load S3/images; it selects the latest attempt (`-updated_at, -pk`) whose
`route_fingerprint` / `prompt_fingerprint` / `config_fingerprint` match those
current constants (Antigravity printed Arabic). Older source identities and
stale prompt/config contracts cannot open resume.

**Resumable on that attempt:** `PLANNING`, expired `RUNNING` (lease in the
past), or unfenced `FAILED` pages (durable Vision plan or no Vision call yet,
and no `ARABIC_PRINTED_PRIMARY_AMBIGUOUS` / `ARABIC_PRINTED_FALLBACK_AMBIGUOUS`
bands). Reprocess is `NORMAL_REENQUEUE`. SUCCEEDED pages stay `REUSE`.

**Not resumable:** no matching current-contract attempt; the selected attempt
has any actively leased `RUNNING` page (`lease_expires_at` in the future);
or every unfinished page on that attempt is fail-closed
(`ARABIC_PRINTED_VISION_AMBIGUOUS`, Vision reserved without a durable plan, or
an ambiguous band create). Live leases are treated as busy, matching
`claim_arabic_printed_page`.

**Unchanged:** Gemini checkpoint-only resume (`FAILED` `GeminiOcrPageCheckpoint`);
ordinary `SOURCE_TEXT` `FAILED/OCR_FAILED` reprocess; VERIFIED overwrite guard;
no DTR row is created merely to enable the button; checkpoints are not reset.
Eligibility does not require `ENABLE_ANTIGRAVITY_ARABIC_PRINTED` on the web
process: staff UI may assess while the worker still resumes banded checkpoints.

**Tests:** `documents/test_ocr_reprocess.py`
(`ArabicPrintedBandedPartialOcrReprocessTests`).

## Gemini Hebrew translation — short-chunk length floor is not truncation

**Decision / implemented:** Hebrew translation truncation detection must not
treat a completed short excerpt as output-token truncation merely because the
Hebrew result is shorter than `min_text_length` (default 20). Document 322
failed `HEBREW_TRANSLATION_FAILED` on chunk 1/16 with `source_length=13`,
`translation_length=5`, `finish_reason=STOP`, and `max_output_tokens=8192`.

**Current behavior:**

- `_is_translation_chunk_truncated` applies the `min_text_length` floor only
  when the source chunk is itself at least `min_text_length`.
- Sources under 1000 characters still skip the 20% ratio heuristic.
- `MAX_TOKENS` still retries, escalates the output cap, and may split.
- Empty provider output still fails as empty response.
- A translation shorter than `min_text_length` still sets `needs_review`.
- Chunking is unchanged: blank-line paragraphs pack up to 2200 characters; a
  short leading paragraph is flushed as its own chunk when the next block is
  oversized (`>2200`) or when a short first line is followed by an oversized
  line inside `_split_oversized_block`.

**Unchanged:** models, routing, OCR/banded OCR, production flags, and
length-based truncation for normal/large chunks.

**Tests:** `GeminiHebrewTranslationTests` in `documents/tests.py`.
## Reviewed PhotoPerson import v1

**Decision / implemented:** Staff/operator import of a reviewed JSON artifact
(`schema=photo-person-reviewed-import-v1`) into **`PhotoPerson`**, optional
**`PersonAlias`**, and explicit **`create_person`**. Default is dry-run.
**`--apply`** writes in one transaction. No public/staff Person UI. The
binding table is import infrastructure only (not searchable, not indexed,
not Author-related).

**Supersedes:** the previous “create_person re-apply blocker” note on this
page. Name-based re-apply remains forbidden; persistence is the binding.

**Current behavior:**

- Command: `import_reviewed_photo_people <artifact.json>` (dry-run);
  `--apply` persists.
- Service: `documents/services/photo_person_reviewed_import.py`.
- Ops: `create_person`, `add_alias`, `add_photo_person` (ADD-only).
- Photo bind: `archive_item_id` + `photo_content_id` +
  `expected_original_file_key`; parent must be PHOTO; photo must be
  **renderable** (`photo_is_archive_renderable`).
- Existing Person: `person_id` plus strip-exact (not casefold)
  `expected_canonical_name`.
- New Person: `local_person_ref` + `canonical_name`. First apply runs
  `find_existing_person_candidates`; any hit is ERROR (no force-create,
  no name reuse). Then `Person` + `ReviewedPersonImportBinding(operation_id)`.
- Re-apply of `create_person` resolves **only** via
  `ReviewedPersonImportBinding.operation_id` → `person_id`. Same-name
  unrelated Persons cannot satisfy it. Canonical stale mismatch fails
  closed. Missing/broken binding fails closed.
- Identical `PersonAlias` / `PhotoPerson` rows are NOOP.
- No `ArchiveItemPerson` writes or inference. `people_present` is not
  written. Document ids are rejected.
- Search-index: `sync_archive_item_search_indexes` only for ArchiveItems
  affected by an actual alias create (fan-out) or PhotoPerson create
  (owning item). Pure NOOP does not call sync. The binding itself is not
  indexed.
- Staff Person merge **repoints** duplicate bindings onto the keeper
  (`PROTECT` on Person) so merge is not blocked.

**Model:** `ReviewedPersonImportBinding`: unique `operation_id` (max 255),
FK `person` PROTECT, `created_at`. Migration
`0062_reviewed_person_import_binding`.

**Why not name-match:** `Person.name` is not unique; `(photo, name)` is not
unique; aliases are not globally unique.

**Deferred:** staff HTML review UI; REMOVE ops; AIP in this importer;
parsing `people_present`; exposing bindings in Person UI.

**Tests:** `documents/test_photo_person_reviewed_import.py`.

## Arabic printed banded OCR — stale CANCEL_PENDING recovery

**Decision / implemented:** A reclaimed Arabic printed band in
`CANCEL_PENDING` with a known interaction id and blank/unknown
`cancel_confirmed_status` is recovered with the existing provider GET
poll (`poll_arabic_printed_band_interaction` with `last_status=in_progress`).
It must not immediately fail closed, and it must not create a new
interaction merely to resolve that state.

**Current behavior:**

- Explicit `cancel_confirmed_status=completed` still uses
  `_recover_completed_cancel` (poll once; accepted transcription persists
  success; otherwise persist band failure).
- Explicit `cancel_confirmed_status=cancelled` still uses the existing
  fallback / Cloud Vision low-quality path.
- Blank/unknown confirmation with **no** interaction id, or with no
  remaining page budget before the recovery GET, still fail-closes
  (`ARABIC_PRINTED_BANDS_UNRESOLVED`); the band stays `CANCEL_PENDING`.
- Blank/unknown confirmation with a known id: one recovery GET.
  Completed + accepted transcription persists success without a new
  create. Confirmed cancelled then follows the normal fallback path when
  budget remains, or the existing low-quality path when it does not.
  Still in-progress, poll error/timeout, or other unsafe statuses remain
  fail-closed with no create and no low-quality selection.
- Page budget, band attempt timeout, request lease, and routing/flags are
  unchanged.

**Tests:** `documents/test_arabic_printed_banded_ocr.py` (document-322
checkpoint shape).

## Public `/archive/` Person advanced filter — unified AIP ∪ PhotoPerson

**Decision / implemented:** Repeatable public **`person=<person_id>`** matches
authorized+browse-renderable **`ArchiveItem`** rows related through
**`ArchiveItemPerson` OR identified `PhotoPerson` on renderable
`PhotoContent`**. Same user-facing membership as **`/archive/people/`** and
**`/archive/people/<id>/`**. This supersedes the ArchiveItemPerson-only
filter contract in **Public `/archive/` Person advanced filter**.

**Current behavior:**

- Outer list queryset remains **`archive_browse_queryset_for_user`**. The
  filter does not copy that queryset into inner subqueries.
- ORM: correlated **`Exists(ArchiveItemPerson)` OR `Exists(PhotoPerson)`**
  with **`person_id__in`** (OR within the Person group). Person still ANDs
  with author / category / event / tag / year. No join fan-out: dual AIP+PP
  and multiple matching photos of one item yield one ArchiveItem.
- PhotoPerson matches only identified people on
  **`filter_archive_renderable_photo_contents`** rows.
  **`people_present` is excluded.** AIP is not inferred from PhotoPerson.
- Picker membership is **`person_public_membership_q_for_item_pks`**
  (authorized item pks + renderable PhotoPerson). Canonical **`Person.name`**
  only; order **`name`, then `id`**. Aliases are not choices. Private /
  restricted / non-renderable links cannot leak.
- Active Person chips, global **`/archive/?q=`** indexing, People index,
  Person detail, and Person/Author models are unchanged.
- No schema change.

**Deferred:** alias-assisted picker search; photo-level “appears in this
photo” cards/filter (list results remain ArchiveItems).

**Tests:** `documents/test_archive_advanced_search_person.py`, plus updated
Person-page and Stage B PhotoPerson-only assertions. No migration.

## Public People catalog and unified Person detail

**Decision / implemented:** Public People catalog exists at
**`/archive/people/`** (`archive-people-index`). Public Person detail
**`/archive/people/<person_id>/`** (`archive-person-detail`) lists one
unified stream of authorized+renderable **`ArchiveItem`** rows reachable
through **`ArchiveItemPerson` OR `PhotoPerson`**. This supersedes the
deferred “Person catalog” note and the two-section Person page
(related items vs unpaged PhotoPerson appearance cards).

**Current behavior:**

- Index membership is the same accessibility contract as Person detail:
  at least one authorized+renderable AIP item or renderable PhotoPerson
  appearance (`archive_browse_queryset_for_user` +
  `filter_archive_renderable_photo_contents`). Unlinked people and
  non-renderable-only links are omitted (detail still 404s).
- Index order is **`Person.name`, then `id`**. Pagination is fixed **48**.
  Optional GET **`q`** matches canonical **`Person.name`** or
  **`PersonAlias.name`** with the same icontains/`Exists` predicate as
  the staff People index. Aliases are search-only and are not displayed.
  Duplicate canonical names are separate rows with distinct Person URLs.
- Each index row shows one count: **DISTINCT** authorized+renderable
  ArchiveItems via AIP ∪ PhotoPerson. Implementation: two page-restricted
  pair queries (ArchiveItemPerson and renderable PhotoPerson), then a
  Python set union of `(person_id, archive_item_id)`. Dual AIP+PP and
  multiple photos of one item count once. This is not a Django SQL
  `UNION` queryset. Staff index still shows two unfiltered relation
  counts.
- Person detail uses one heading (**פריטים קשורים**), one
  **`total_count`**, and one 48-item pagination stream ordered
  **`-created_at`, `pk`**. A matching renderable PhotoPerson deep-links
  to the earliest matching photo by **`(PhotoContent.position, id)`**
  (`/archive/<item_id>/?photo=<photo_id>`) and uses that photo’s
  thumbnail. AIP-only items use the normal ArchiveItem URL.
- Advanced **`person=`** filtering and the Person picker use the same
  AIP-or-renderable-PhotoPerson membership (see **Public `/archive/` Person
  advanced filter — unified AIP ∪ PhotoPerson**). Global **`/archive/?q=`**
  indexing is unchanged. Person and Author stay separate. No public alias
  display. No schema change.

**Helpers:** `documents/services/person_public.py`.

**Tests:** `documents/test_archive_people_public_index.py`,
`documents/test_archive_person_public_page.py`,
`documents/test_person_staff_ui.py`.

## One-time reviewed legacy comma-author cleanup

**Decision / implemented:** A **one-time**, fail-closed cleanup of four
reviewed legacy comma-containing bibliographic **`Author`** rows from the
2026-09-03 live audit. This is **not** a general comma-splitting policy.
The command does **not** discover or split arbitrary `author_name` values.

**Current behavior:**

- Management command **`cleanup_legacy_comma_authors`**. Default is dry-run
  (zero writes). **`--apply`** mutates inside one **`transaction.atomic`**.
- Explicit constants only. ArchiveItem **311** currently linked to aggregate
  **Author 69** (`חגי אשד, אביעזר גולן`) is rewritten to ordered
  **Authors `[29, 68]`** (`חגי אשד`, `אביעזר גולן`), which already exist
  and remain linked to items **121** and **310**. **`author_name`** is rebuilt
  with **`_joined_author_name`**. Search index for item **311** is refreshed
  in the same transaction.
- Unlinked aggregate Authors **4**, **6**, **61**, and (after unlink) **69**
  are deleted **only** if each has zero remaining **`ArchiveItemAuthor`**
  links. Items **13**, **29**, and **289** are already correctly split and
  are verified, not rewritten.
- Fail closed unless the live rows match the reviewed snapshot (ids, exact
  names, ordered links, orphan zero-link checks). Concurrent extra links
  abort with no writes. Index failure rolls back the entire cleanup.
- Repeat after a successful apply is **`already_complete`**: item 311 already
  has `[29, 68]`, matching **`author_name`**, items 13/29/289 still match,
  and Authors 4/6/61/69 are absent. No writes. Partial/unexpected states
  fail closed (not auto-repaired).
- Does **not** touch **Person**, rename/merge Authors, infer more comma
  cases, or add schema.

**Out of scope / deferred:** general comma-split; standalone Author delete
UI; public display/search cutover from **`author_name`**.

**Tests:** `documents/test_legacy_comma_author_cleanup.py`.

## Text quality PR3 — Arabic printed banded SOURCE_TEXT base quality

**Decision / implemented:** Automatic persisted `LOW` / `MEDIUM` / `GOOD` is
scored **only** for **new or reprocessed Arabic printed banded** OCR
**SOURCE_TEXT**. Other engines/routes remain `UNKNOWN` unless scored in a
later PR. Translation `HEBREW_TEXT` quality is unchanged;
`capped_inherited_base_quality` remains unwired.

**Scoring** (`documents.services.arabic_printed_text_quality.quality_from_banded_page_qualities`):
input is the completed banded `page_quality` list plus assembled source text.
The scorer does not query checkpoints.

- **UNKNOWN** — empty page evidence, any missing/blank/unknown `page_quality`,
  or missing/blank/whitespace-only assembled source text
- **GOOD** — every page is `UNASSISTED` and assembled text does not contain
  `[UNCLEAR]`
- **LOW** — at least half of pages are `CLOUD_VISION_LOW_QUALITY`, using
  integer-safe `2 * lq_count >= total_pages` (1/1 and 1/2 are LOW; 1/3 is
  MEDIUM; 2/3 and 2/4 are LOW)
- **MEDIUM** — every other valid completed case, including `ASSISTED`,
  `MIXED`, minority LQ, and all-`UNASSISTED` text containing `[UNCLEAR]`
  (`[UNCLEAR]` caps GOOD → MEDIUM). `ASSISTED` or `MIXED` alone never mean
  LOW. Do not use `status` / `READY` / `NEEDS_REVIEW` / engine name as
  quality proxies.

**Current behavior:**

- Completed banded Antigravity attaches `HtrResult.quality` from the scorer.
  Non-banded Antigravity leaves `quality` unset (`None`).
- Worker `_save_htr_results` writes `defaults["quality"]` only when
  `HtrResult.quality` is a persisted `DocumentTextResult.Quality` value.
  `None` omits the field: new rows default to `UNKNOWN`; existing rows are
  not overwritten by a non-scoring rerun.
- A successful **same-engine** banded rerun recomputes and persists base
  quality. Engine-marker identity / DTR uniqueness is unchanged.
- VERIFIED reprocess remains blocked; REJECTED may be reprocessed and can
  return to UNVERIFIED.
- Gemini, Transkribus, legacy/non-banded Antigravity, and other
  languages/routes are not scored. `HUMAN_VERIFIED` / `NEEDS_CORRECTION`
  remain presentation-only and are never written as base quality.

**Tests:** `documents/test_arabic_printed_text_quality.py`; adapter coverage
in `documents/test_antigravity_ocr.py`.

## Text quality PR2 — public detail quality indicator

**Decision / implemented:** Public OCR document detail shows **one** compact
quality row on the block the public UI presents as **תעתוק**
(`איכות התעתוק: [badge] [info]`). Public `MANUAL_TEXT` detail shows the same
shared indicator using the PR1 manual-text helper (`HUMAN_VERIFIED`).
Presentation uses `documents.services.text_quality_presentation`; templates
do not derive quality.

**Current behavior:**

- Exactly one OCR quality indicator per page, attached to the **displayed
  transcription**, using that row’s PR1 effective quality. Selection follows
  existing document-detail presentation: if the SOURCE panel is shown with
  text, that is transcription; if SOURCE is hidden and the HEBREW panel is
  the single shown text (Hebrew-language documents), that HEBREW block is
  transcription. Non-Hebrew documents normally use SOURCE_TEXT as
  transcription. The Hebrew **translation** panel never gets a separate badge.
- Tooltip title/intro/six levels/footer are shared. The sentence
  `בתרגום לעברית, האיכות תלויה גם באיכות התעתוק שעליו הוא מבוסס.` appears
  only when a Hebrew translation is actually displayed **in addition to**
  the transcription (non-Hebrew OCR with both texts). It is omitted on
  MANUAL_TEXT, Hebrew-language OCR (no translation panel), and
  transcription-only OCR. Explanatory only — `capped_inherited_base_quality`
  remains unwired.
- Staff review workspace is unchanged. Browse/search/homepage cards and
  quality filters are out of scope.

**Tests:** `documents/test_text_quality_public_ui.py`.

## Staff Author merge (explicit ids)

**Decision / implemented:** Controlled staff merge of one bibliographic
**`Author`** INTO another. This is merge by explicit **`Author.id`**, not
name matching. Logic lives in **`documents/services/author_merge.py`**.
**`Author`** remains separate from **`Person`**. There are no Author aliases.
An Author may explicitly reference at most one Person; that is not part of
name matching.

**Current behavior:**

- **`merge_author(keeper=..., duplicate=...)`** merges duplicate INTO keeper.
  Ids must exist and differ. Keeper **`Author.id`** and **`Author.name`** are
  unchanged. Duplicate is deleted only after affected **`ArchiveItemAuthor`**
  rows and rebuilt **`author_name`** strings are resolved.
- Mutated items are those linked to the **duplicate** at merge time.
  Keeper-only items are not rewritten and are not passed to search-index
  fan-out.
- Same-item collision (**`uniq_archive_item_author`**): if keeper is already
  linked, **drop the duplicate link and keep the keeper slot**. Do not use
  first-occurrence-wins. Example: `[duplicate, Bob, keeper]` becomes
  `[Bob, keeper]`. Surviving relative order is preserved. Positions are
  rebuilt contiguous **`0..n-1`**. If keeper is not linked, the duplicate
  slot is re-pointed to keeper (position preserved among remaining authors).
- Final **`author_name`** is rebuilt with **`_joined_author_name`** from the
  final ordered **`ArchiveItemAuthor`** rows (relations are source of truth;
  drifted strings are corrected). Every rebuilt string is prevalidated
  against 255 characters (**`AUTHOR_JOINED_TOO_LONG_ERROR`**) before any
  write. Duplicate **`Author.name`** is discarded; it does not become an
  alias.
- One **`transaction.atomic`**. Lock order matches Author writers
  (**`apply_staff_archive_item_authors`** / **`apply_legacy_author_name`** /
  **`rename_author`**): duplicate-linked **`ArchiveItem`** rows first
  (ascending pk, expanded until that set is stable), then those items'
  **`ArchiveItemAuthor`** rows, then **`Author`** rows (keeper, duplicate,
  and co-authors; ascending pk). Do **not** lock Author first (Person merge
  lock order would deadlock). After Author locks, duplicate-linked item ids
  are re-read; a newly linked item that is not already locked fails closed
  with **`AUTHOR_LINKS_CHANGED_RETRY_ERROR`**. Extra **`ArchiveItem`** rows
  are not locked while Author locks are held. **`IntegrityError`** is mapped
  to **`AUTHOR_MERGE_CONCURRENCY_ERROR`**.
- **`sync_archive_item_search_indexes`** runs inside the same transaction for
  the mutated duplicate-linked ids only (sorted). Index failure rolls back
  the entire merge. Search-index fan-out remains duplicate-linked items
  only; indexed author text now follows the structured-Author `q` contract
  (see **Public structured-Author search and advanced Author filter**).
- Explicit **`Author.person`**: if neither Author is linked, the keeper stays
  unlinked; if exactly one is linked, the keeper retains that Person; if
  both are linked to the same Person, merge is allowed; if they are linked
  to different Persons, merge fails closed before writes.
- Staff UI: from **`/archive/manage/authors/<keeper_id>/edit/`**, enter the
  duplicate id and GET **`/archive/manage/authors/<keeper_id>/merge/`**
  (**`archive-manage-author-merge`**). GET without **`duplicate_id`** shows
  the id field only (no error, no writes). GET with **`duplicate_id`** shows
  confirmation preview (ids, names, affected items, current vs planned
  order). Only POST with **`confirm_merge=1`** executes. Success returns to
  keeper edit. Access is **`@login_required`** + **`_require_admin_page`**.
  The staff Author index remains find/open only (no per-row merge buttons).

**Out of scope / deferred:** standalone Author delete; merge from the Author
index; name-based merge; PHOTO author UI; unique **`Author.name`**;
Django Admin Author tools. Public Author catalog/detail, public item
Author link presentation, and public **`q` / advanced Author filter**
cutover are implemented separately.

**Tests:** `documents/test_author_merge.py`,
`documents/test_author_merge_staff_ui.py`.

## Text quality PR1 — persisted base quality + effective public helper


**Decision / implemented:** `DocumentTextResult.quality` stores automatic/base
quality only: `UNKNOWN` / `LOW` / `MEDIUM` / `GOOD` (default `UNKNOWN`).
`HUMAN_VERIFIED` and `NEEDS_CORRECTION` are **not** persisted quality values.
Effective public quality is resolved by `documents.services.text_quality`:
`VERIFIED` → `HUMAN_VERIFIED`; `REJECTED` → `NEEDS_CORRECTION`; otherwise the
persisted base quality. `REJECTED` does not map to `LOW` and does not expose
the stored automatic quality publicly.

**Current behavior:**

- Migration `0061` backfills existing rows to `UNKNOWN`. Do not infer historical
  quality from `status`, `review_reasons`, engine, `NEEDS_REVIEW`, `READY`, or
  processing success.
- OCR staff text edit and verify/reject do not write `quality`. Verify still
  only changes `verification_status` (plus optional text save). Effective
  quality becomes `HUMAN_VERIFIED` automatically once `VERIFIED`, and
  `NEEDS_CORRECTION` once `REJECTED`.
- Worker `_save_htr_results` writes `quality` only when `HtrResult` supplies
  a persisted quality value (PR3: Arabic printed banded SOURCE_TEXT).
  Translation persist still does not set `quality` (model default
  `UNKNOWN`). Future `HEBREW_TEXT` scoring should set
  `persist_hebrew_translation_result` `defaults["quality"]` via
  `capped_inherited_base_quality(source.quality, candidate)` — not wired yet.
- Staff-created `MANUAL_TEXT` (`create_manual_text_archive_item` /
  `update_manual_text_archive_item`) is treated as `HUMAN_VERIFIED` in the
  helper. No quality/verification column on `ManualTextContent`. Django admin
  cannot add/change `ManualTextContent`. No automated/import writer exists.

**Deferred / PR2 (now implemented):** public detail UI heading
`איכות התעתוק` and Hebrew level labels in `PUBLIC_TEXT_QUALITY_LABELS`.
See **Text quality PR2** above.

**Tests:** `documents/test_text_quality.py`.

## Staff new-Person duplicate prevention

**Decision / implemented:** Staff “create new Person by name” workflows
look up existing **`Person.name`** and **`PersonAlias.name`** with trim plus
case-insensitive exact match. A match is a **warning / confirmation**, never
automatic reuse, merge, or alias create. Intentional same-name Person rows
remain allowed after an explicit per-token acknowledgement.

**Current behavior:**

- Shared layer: **`documents/services/person_duplicate_check.py`**.
  **`find_existing_person_candidates(name)`** /
  **`find_existing_person_candidates_for_names(names)`** batch canonical and
  alias lookup (one Person query for a comma-separated list). Candidates are
  distinct by **`Person.id`**, ordered by **`(name, id)`**. Matching a
  canonical name and an alias on the same Person yields one candidate.
- **`check_new_person_names`** parses tokens with the existing split/trim
  rules, then resolves candidates for **all** tokens before any create.
  **`create_identified_people_from_new_names`** is the only create helper;
  ArchiveItemPerson and PhotoPerson both use it.
- Normalization is **input trim + `casefold()` exact equality** only. No
  fuzzy, substring, transliteration, morphology, punctuation stripping, or
  automatic alias creation.
- If any token has candidates and is not force-approved: **zero** new Person
  rows and **zero** ArchiveItemPerson / PhotoPerson changes from that
  new-name field. Selected person-id fields are not persisted when the
  overall staff form fails (existing fail-closed transactions).
- Intentional create uses POST **`force_create_person`**: a list of SHA-256
  hex digests of the **exact trimmed UTF-8 token** (`person_new_name_token_key`).
  Acknowledgements bind to the token text, not array index. Changing the
  typed name (including case) invalidates a stale key. There is no global
  “ignore all warnings” checkbox.
- HTML forms stay on the same page, preserve submitted values, and show
  candidates (canonical name, aliases, id, link to
  **`/archive/manage/people/<id>/edit/`**) plus per-token
  “create a new Person anyway”. Staff may instead pick the existing Person
  in the picker and remove the new-name token. The typed text is not
  mutated; existing ids are not auto-selected.
- JSON PHOTO create / add-photo and OCR create return HTTP 400 with
  **`error_code=PERSON_NAME_CANDIDATES`**, **`error`**, and
  **`person_name_conflicts`**. JS renders the same warning and resubmits
  **`force_create_person`**. Parse/validation runs **before**
  ArchiveItem / PhotoContent / S3 plan writes.
- Unchanged: ArchiveItemPerson vs PhotoPerson stay separate; picker ids
  still link that exact Person; **`Person.name`** is not unique; Person
  merge remains the repair tool; search refresh, historical Person/Tag
  rules, and public display are unchanged.

**Why:** Accidental duplicate identities were easy because new-name fields
always inserted. Lookup-by-name without confirmation would silently attach
the wrong Person when two people share a display name or alias.

**Deferred:** fuzzy / morphological matching; auto-merge; auto-alias;
signed confirmation tokens.

**Tests:** `documents/test_person_duplicate_prevention.py`,
`documents/test_archive_item_person_staff_ui.py`,
`documents/test_photo_multi_manage.py`.

## Unified staff PHOTO edit cards

**Decision / implemented:** Staff PHOTO item edit
(`/archive/manage/<id>/edit/`) is the primary place to edit each
`PhotoContent` row. Per-photo metadata is no longer a separate-page-only
workflow. The standalone per-photo URL remains as a compatibility
fallback.

**Current behavior:**

- Shared ArchiveItem metadata stays in its own form at the top
  (title, visibility, item dates, discovery, ArchiveItemPerson).
- Each PhotoContent is a separate card in `(position, id)` order under
  **תמונות בפריט זה**, with thumbnail/status/filename, public **צפייה**
  (`/archive/<id>/?photo=<photo_id>`), move up/down, delete confirmation,
  and the existing per-photo fields.
- Each card is an independent `<form>` POSTing to
  `/archive/manage/<id>/photos/<photo_id>/edit/` with hidden
  `inline_photo_edit=1`. Saving one card updates only that PhotoContent /
  PhotoPerson. It does not write ArchiveItem shared metadata or another
  photo. No inference between ArchiveItemPerson and PhotoPerson.
- Successful inline save redirects to
  `/archive/manage/<id>/edit/#photo-<photo_id>`. Validation errors
  re-render the **item** edit page (HTTP 200) with submitted values and
  errors on that card only.
- GET/POST without `inline_photo_edit` on the standalone photo-edit URL
  still renders/saves that page and redirects back to itself on success.
- Date-widget POST `name`s stay unprefixed (`date_precision`,
  `date_start_*`, `date_end_*`). Photo cards prefix DOM ids via
  `date_widget_prefix=photo<id>` so multiple widgets can coexist with the
  unprefixed item-level widget. Other per-photo control ids use the same
  `photo<id>_` prefix; add-photo and the standalone editor stay
  unprefixed.
- Add-photo, reorder, and delete-one-photo endpoints are unchanged.

**Why:** Editing N photos required a round-trip per photo. Independent
server-rendered forms keep the existing parse/update path without a
modal/JS editor or mixed POST of item + photo fields.

**Deferred:** staff `?photo=` selector on the item edit URL; removing the
standalone photo-edit route; add-photo returning with a card hash.

**Tests:** `documents/test_photo_multi_manage.py`,
`documents/test_photo_manage_edit_delete.py`,
`documents/test_photo_manage_status_clarity.py`,
`documents/test_archive_item_person_staff_ui.py`.

## Public Person page PhotoPerson appearances

**Superseded:** This two-section Person page (ArchiveItemPerson related items
plus a separate unpaged PhotoPerson appearance list) is **not** current
behavior. Current public Person detail is one unified DISTINCT
**`ArchiveItem`** stream from **`ArchiveItemPerson` OR renderable
`PhotoPerson`**. See **Public People catalog and unified Person detail**.

**Decision / implemented (historical):** `/archive/people/<person_id>/` is
accessible when the current user can see **at least one**
authorized/renderable `ArchiveItemPerson` item **or** authorized/renderable
`PhotoPerson` appearance. The two relations stay semantically separate.
Neither is inferred from the other. Advanced `person=` filtering and public
`q` indexing are unchanged in this historical note; current `person=` is
documented under **Public `/archive/` Person advanced filter — unified AIP ∪
PhotoPerson**.

**Historical behavior:**

- 404 only when the Person id is missing, or the user has neither
  visible related items nor visible photo appearances. Same status for
  unauthorized/non-renderable-only cases; no private-count leak.
- Related items (`ArchiveItemPerson`) still use
  `archive_browse_queryset_for_user`, order `-created_at`, `pk`, and
  fixed 48-per-page pagination. `total_count` remains that queryset’s
  length. Section heading: **פריטים הקשורים לאדם**.
- Photo appearances (`PhotoPerson`) are a separate unpaged section on
  the same page (**תמונות שבהן האדם מופיע**). Queryset:
  `photo_person_appearances_queryset` in `photo_gallery.py`. Owning
  ArchiveItem must be in the same browse queryset (visibility + item-
  level PHOTO first-row eligibility). Appearance `PhotoContent` must
  pass `filter_archive_renderable_photo_contents` /
  `photo_is_archive_renderable`. Order: item `-created_at`, item id,
  photo `(position, id)`.
- Each appearance is its own card linking to
  `/archive/<item_id>/?photo=<photo_content_id>` via
  `public_photo_detail_url`. Multiple photos in one ArchiveItem stay
  separate cards. Item-level card title/meta are reused; thumbnail is
  the appearance photo when a thumbnail key exists.
- PhotoPerson-only identities no longer 404 when an appearance is
  visible. Biography still displays when the page is otherwise
  accessible. Aliases remain hidden. No public Person index.

**Historical pagination tradeoff:** Photo appearances were not combined into
the related-item paginator. They were listed in full on every related-item
page. A combined paginator was deferred to keep the two relations separate
and the page architecture small.

**Deferred at the time (partially superseded):** public alias display remains
deferred. Person catalog and combined related-item/appearance pagination are
implemented as the unified ArchiveItem stream (see **Public People catalog
and unified Person detail**). Photo-level appearance cards/filter on
`/archive/` remain deferred; list `person=` is unified AIP ∪ renderable
PhotoPerson (see **Public `/archive/` Person advanced filter — unified AIP ∪
PhotoPerson**).

**Tests:** `documents/test_archive_person_public_page.py`. No schema
migration.

## Staff Person identity merge (explicit ids)

**Decision / implemented:** Controlled staff merge of one `Person` INTO
another. This is identity merge by explicit `Person.id`, not name
**`Person` and `Author` remain completely separate identities.** Merge logic
lives in `documents/services/person_merge.py`, not in the view. Authors
explicitly linked to the duplicate Person are repointed to the keeper.

**Current behavior:**

- `merge_persons(keeper_id=..., duplicate_id=...)` merges duplicate INTO
  keeper. Ids must exist and differ. Keeper `Person.id` and canonical
  `Person.name` are unchanged. Duplicate is deleted only after dependents
  are handled. The whole merge is `transaction.atomic` and fail-closed.
- Person rows are locked with `select_for_update` in deterministic id
  order. Dependent alias, `ArchiveItemPerson`, `PhotoPerson`,
  `ArchiveItemPersonSuggestion`, `ReviewedPersonImportBinding`, and
  explicitly linked `Author` rows are locked before mutation.
  Fail-closed checks run before destructive writes.
- Frozen historical Person ids from
  `HISTORICAL_PERSON_NAME_TAG_RECORDS` may be the keeper. They must never
  be the duplicate/deleted Person. The frozen map is not modified.
- Biography: keep keeper if keeper nonempty and duplicate empty; copy
  duplicate onto keeper if keeper empty and duplicate nonempty; no change
  if both empty or equal; fail closed before writes if both nonempty and
  different. No concatenation or automatic choice.
- Aliases move to keeper with `(person, name)` dedupe. An incoming alias
  equal to keeper canonical name is skipped. Duplicate canonical name
  becomes a keeper alias when it differs from keeper canonical name and
  is not already an alias.
- `ArchiveItemPerson` and `PhotoPerson` are moved independently. Collision
  on the same item/photo keeps one keeper row and deletes the duplicate
  row. Neither relation is inferred from the other.
- Authors with `Author.person_id` equal to the duplicate are repointed to
  the keeper before duplicate delete. Unlinked Authors are unchanged.
  Exact-name equality between Author and Person is not used.
- `ArchiveItemPersonSuggestion.person` is PROTECT, so suggestions are
  repointed to keeper. Two PENDING suggestions for the same
  `(archive_item, action)` fail the entire merge. Reviewed
  APPROVED/REJECTED rows may coexist after repointing.
- Search: capture the pre-merge union of ArchiveItem ids reached through
  `ArchiveItemPerson` or `PhotoPerson` for either person; after duplicate
  deletion, `sync_archive_item_search_indexes` refreshes each id once.
- Staff UI: from `/archive/manage/people/<keeper_id>/edit/`, enter the
  duplicate id and GET `/archive/manage/people/<keeper_id>/merge/`. Only
  POST with `confirm_merge=1` executes. Success returns to keeper edit.
  Access is `@login_required` + `_require_admin_page`. Public pages do
  not expose merge. The staff people index remains find/open only (no
  per-row merge buttons).

**Deferred:** Person create/delete; merge from the people index; name-based
merge; Django Admin Person tools; changing frozen map ids.

**Tests:** `documents/test_person_merge.py`,
`documents/test_person_merge_staff_ui.py`.

## Staff Person index

**Decision / implemented:** Staff-only GET index at `/archive/manage/people/`
(`archive-manage-people`) for finding and opening existing `Person` rows.
This is not a public Person catalog, not Django Admin, and not a create /
delete surface. Merge is a separate explicit-id flow on the Person edit
page, not from this index.

**Current behavior:**

- Access matches other archive-manage pages: `@login_required` +
  `_require_admin_page`. Anonymous users redirect to login; other
  authenticated users get 403.
- Optional `q` is case-insensitive `icontains` on canonical `Person.name`
  and `PersonAlias.name`. Duplicate canonical names remain separate rows
  keyed by `Person.id`. Results order by `(name, id)`.
- Columns: Person id, canonical name, aliases, `ArchiveItemPerson` count,
  `PhotoPerson` count, edit link to existing `archive-manage-person-edit`.
  The two relation counts are independent annotations; neither relation is
  inferred from the other.
- Queryset uses alias prefetch plus `Count(..., distinct=True)` on each
  relation, and `Exists` for alias search, so alias/count queries do not
  grow per row.
- `/archive/manage/` links to the index. Public Person pages and public
  browse are unchanged.

**Deferred:** Person create/delete from this index; Django Admin for
Person; public Person catalog; pagination if the staff list becomes large.

**Tests:** `documents/test_person_staff_ui.py`.

## Public PhotoPerson name links (presentation only)

**Decision / implemented:** Public PHOTO detail **אנשים מזוהים:** names are
canonical-name links to the existing public Person page. This is
presentation only. `PhotoPerson` and `ArchiveItemPerson` stay separate
relations. Person-page authorization is unchanged.

**Current behavior:**

- Selected-photo `PhotoPerson` rows still use canonical `Person.name`,
  order `(person.name, person.pk)`, and `meaningful_metadata_value`
  filtering. Aliases are not read or displayed.
- Each remaining name is a link whose href is `person_public_page_url`
  (`/archive/people/<Person.id>/`). Duplicate canonical names stay
  distinct by `Person.id`.
- `identified_people_display_names()` remains a name-only compatibility
  helper. `PublicPhotoGallery` also exposes immutable
  `PublicIdentifiedPersonLink` rows (`name`, `href`).
- Layout stays comma-separated under **אנשים מזוהים:**. No ids, aliases,
  or extra label text.
- `build_public_photo_gallery()` still reads prefetched
  `photo_contents` / nested `people` and does not add a people query.
- PhotoPerson-only people still do not appear on ArchiveItemPerson
  cards, search, discovery, or advanced `person=` filters.
- A public Person page is accessible when the user can see at least
  one authorized/renderable `ArchiveItemPerson` item **or**
  authorized/renderable `PhotoPerson` appearance (see **Public Person
  page PhotoPerson appearances**). Following a PhotoPerson name link
  404s only when that Person has neither visible relation.

**Deferred:** public alias display; PhotoPerson appearance filter;
automatic `ArchiveItemPerson` from `PhotoPerson`; Person catalog/Admin.

**Tests:** `documents/test_photo_public_gallery.py`,
`documents/test_archive_person_public_presentation.py`.

## Arabic printed banded OCR — worker CDK execution flag ON

**Decision / implemented:** Worker CDK config (`app_stack.py`) sets
`ENABLE_ANTIGRAVITY_ARABIC_PRINTED_BANDED` to **`true`**. This is desired
source configuration, not a claim that live ECS already has the flag on.
It is effective in runtime after deployment; verify deployed runtime
separately. The env-validation / code default when the variable is unset
remains **false**. The flag is still not set on the web task. Routing,
adapters, Cloud Vision secret wiring, models, deadlines, and retry
behavior are unchanged by this rollout.

**Supersedes:** Phase 6A “do not enable it in deployment”; the Cloud Vision
worker-secret entry’s “flag still off / enabling remains deferred” status.

**Tests:** `documents/test_antigravity_ocr.py`
(`AntigravityBandedCdkWiringTests`).

## Arabic printed banded OCR — Cloud Vision worker secret (flag still off)

**Decision / implemented:** Worker ECS injects `GOOGLE_CLOUD_VISION_API_KEY`
from the existing Secrets Manager secret named exactly
`vs-archive/google-vision-key`, using the same
`Secret.from_secret_name_v2` + `ecs.Secret.from_secrets_manager` pattern as
`GEMINI_API_KEY`. The credential is injected into the worker container only;
the web container does not receive it. Web and worker share the existing ECS
execution role; that IAM boundary is intentional and matches Transkribus
secret injection.

**Does not:** enable `ENABLE_ANTIGRAVITY_ARABIC_PRINTED_BANDED` (at the time
of this wiring, code default and worker CDK remained **false**); change OCR
routing, adapters, or provider behavior.

**Supersedes:** the Phase 6A deferred item “Cloud Vision secret/CDK wiring”
and the earlier “do not reference a Cloud Vision secret yet” constraint.
Enabling the banded flag in production was deferred here and is **superseded**
by “worker CDK execution flag ON” above.

**Tests:** `documents/test_antigravity_ocr.py`
(`AntigravityBandedCdkWiringTests`).

## Arabic printed banded OCR — Phase 6A generic execution-lease deadline

**Decision / implemented:** Banded Antigravity uses one generic absolute
monotonic deadline derived from the actual `ProcessDocumentRequest`
execution lease. It does not use Antigravity’s 1200-second JSON poll
timeout as the document budget.

**Current behavior:**

- Claiming a request sets `lease_expires_at = now + EXECUTION_LEASE`
  (45 minutes) and copies that timestamp onto the in-process execution
  payload as `lease_expires_at`. The lease is not extended or reset for
  OCR.
- Immediately before `transcribe_pages`, `run_worker` converts remaining
  wall-clock seconds to `time.monotonic() + max(0, remaining_seconds)`.
  An expired lease therefore yields a deadline at the current monotonic
  time. Legacy payloads without a lease pass no deadline.
- `transcribe_pages` forwards `absolute_deadline_monotonic` as an optional
  kwargs field. Gemini ignores it. JSON Antigravity pops it and keeps
  `DEFAULT_TIMEOUT_SECONDS` (1200s). Banded Antigravity requires it and
  fails closed before coordinator/provider work if it is absent.
- The coordinator still applies its 60-second terminalization reserve,
  150-second page-start minimum, and 480-second per-page cap.
- Phase 5B identity is unchanged: oriented SHA/dimensions are prepared
  eagerly for all pages before claiming.

**Tests:** `documents/test_antigravity_ocr.py`,
`documents/test_process_document_request_worker.py`.

## Arabic printed banded OCR — Phase 6A safe production wiring (default OFF)

**Decision / implemented:** Wire the already-implemented Arabic Printed Banded
coordinator into `AntigravityAdapter` behind a new worker-only execution flag.
Do not enable it in deployment (historical Phase 6A constraint; **superseded**
by “worker CDK execution flag ON” above). Cloud Vision secret/CDK wiring is
implemented separately (injected into the worker container only); see “Cloud
Vision worker secret” above.

**Current behavior:**

- `ENABLE_ANTIGRAVITY_ARABIC_PRINTED` is unchanged (route activation for
  `ar` + `PRINTED`).
- `ENABLE_ANTIGRAVITY_ARABIC_PRINTED_BANDED` defaults to **false**. At Phase
  6A wiring time, worker CDK config set **`false`**. The flag is **not** on
  the web task. Worker CDK config now sets **`true`** (see “worker CDK
  execution flag ON”); that is desired source, effective in runtime after
  deployment — verify deployed runtime separately.
- When Arabic printed is routed to Antigravity and the banded flag is false,
  the existing whole-document JSON Antigravity path is unchanged.
- When both existing Arabic-Antigravity eligibility and the banded flag are
  true, `AntigravityAdapter.execute` calls
  `process_arabic_printed_banded_document`. No provider-specific logic was added
  to `run_worker.py`, `htr_engine.py`, `ocr_routing.py`, or the registry.
- `PageImage.page_index` is mapped from 1-based to contiguous 0-based
  coordinator indexes. Existing `source_identity` /
  `source_content_fingerprint` pass through unchanged. EXECUTE crops use Phase 3
  `prepare_arabic_printed_working_image`, not JSON-path
  `antigravity_outbound_image`.
- Antigravity Interactions still uses existing `GEMINI_API_KEY`.
  `GOOGLE_CLOUD_VISION_API_KEY` is required in worker env validation only when
  the banded flag is true. Worker CDK now injects that key from
  `vs-archive/google-vision-key`. The credential is injected into the worker
  container only; the web container does not receive it. The banded flag
  remains false, so the injected credential is unused in execution.
- `ArabicPrintedCheckpointBusyError` → `EnginePageCheckpointBusyError`.
  `ArabicPrintedCheckpointPersistenceRetryableError` →
  `EnginePageCheckpointPersistenceRetryableError`. Coordinator PARTIAL →
  `EnginePageIncompleteError` (no `DocumentTextResult`; checkpoints remain the
  resume source). Identity mismatch and stale-lease errors fail closed as
  `EnginePermanentError`.
- Completed `HtrResult.engine_name` is the banded runtime marker, never the
  Antigravity agent id. If completed pages disagree:
  `antigravity-banded:mixed:<24-hex-stable-digest>` (fits the 64-character
  `DocumentTextResult.engine` field).

**Deferred:** Enabling the banded flag in production; broadening beyond Arabic
printed Antigravity.

**Tests:** `documents/test_antigravity_ocr.py` (adapter wiring, env
validation, CDK source assertions).

## Global staff Author rename


**Decision / implemented:** Staff can rename one **`Author`** globally from
**`/archive/manage/authors/<author_id>/edit/`**
(**`archive-manage-author-edit`**). The rename applies to every linked
**`ArchiveItem`**; there is no per-item author name override. An exact name
collision with another **`Author`** is **rejected, never merged**.

**Current behavior:**

- Page is staff-only (**`@login_required`** + **`_require_admin_page`**), reached
  from the **"עריכת מחבר/ת"** link next to each already-linked author in
  `archive_item_authors_form_fields.html`. Reuses the Person-edit view shape:
  plain POST parsing (no Django Forms), single **`name`** field,
  **`messages.success`** + redirect on success, HTTP 200 re-render with
  **`form_errors`** on failure.
- GET shows an **affected-items preview**: every linked **`ArchiveItem`** with
  its current **`author_name`**, an **`archive-manage-edit`** link, and the
  count. The preview performs no writes and is re-rendered with the submitted
  value on validation error. The preview is advisory only; POST re-derives
  affected items under lock, so correctness does not depend on it.
- **`rename_author`** rejects before any write: blank/whitespace-only
  (**`AUTHOR_NAME_REQUIRED_ERROR`**), over 255 characters
  (**`AUTHOR_NAME_TOO_LONG_ERROR`**), and an exact post-strip name match on
  another **`Author`** (**`AUTHOR_NAME_COLLISION_ERROR`**). Collision is
  case-sensitive and exact, matching item-level exact-name reuse. Merging is
  **out of scope**: duplicate **`Author.name`** rows make item-level saves fail
  closed with **`AMBIGUOUS_AUTHOR_ERROR`**, so creating one is blocked.
  A **`Person`** with the same name is **not** a collision — Author stays
  separate from Person.
- One **`transaction.atomic`**. Lock order matches item-level author writers
  (**`apply_staff_archive_item_authors`** / **`apply_legacy_author_name`**):
  affected **`ArchiveItem`** rows first (ascending pk, expanded until the
  linked set is stable), then their **`ArchiveItemAuthor`** rows, then every
  **`Author`** whose name is used to rebuild **`author_name`** plus any
  exact-name collision candidate (ascending pk). After Author locks, the
  target Author's linked item ids are re-read. If a newly linked item is not
  already locked, the rename **fails closed** with
  **`AUTHOR_LINKS_CHANGED_RETRY_ERROR`** (staff reload and retry). Extra
  **`ArchiveItem`** rows are **not** locked while Author locks are held, so
  the item→Author order is not inverted. A concurrently removed link is
  omitted from the locked through rows and is not rebuilt. Co-author names
  are read only from those locked Author rows, so a concurrent rename cannot
  supply a stale joined string. **Prevalidate every rebuilt joined
  `author_name`** against 255 per item (**`AUTHOR_JOINED_TOO_LONG_ERROR`**)
  before any write — a longer name can push a multi-author item over the
  limit; rename; rebuild each affected **`author_name`** from that item's
  ordered **`ArchiveItemAuthor`** rows; then
  **`sync_archive_item_search_indexes(affected_ids)`** inside the same
  transaction. Index failure rolls back the rename and every rebuilt string.
  Renaming to the current name is a no-op (no writes, no index refresh)
  after the same locks.
- Because rebuild reads the ordered relations as source of truth, an
  **`author_name`** that had drifted from its links is corrected as a side
  effect on the items touched by that rename. **Intentional.**
- Rename does not change author **`position`** values, create/delete
  **`Author`** or **`ArchiveItemAuthor`** rows, or touch **`Person`** /
  **`PersonAlias`** / **`ArchiveItemPerson`** / **`PhotoPerson`**.
- No schema migration. **`Author.name`** stays non-unique. **`Author`** /
  **`ArchiveItemAuthor`** stay unregistered in Django admin. PHOTO create/edit
  still has no author UI and therefore no rename link. Public templates,
  advanced-filter code, and search-index code are unchanged — the rename is
  visible publicly only because **`author_name`** and the index rows are
  rebuilt.
- The joined-string rule is now shared: **`_joined_author_name`** is used by both
  **`apply_staff_archive_item_authors`** and **`rename_author`**.

**Out of scope / deferred:** Author delete; PHOTO author UI; unique
**`Author.name`** constraint; public **`author_name`** display/search/filter
cutover to Author relations; optimistic-concurrency token between preview
and save. Author merge, staff Author index, and public Author catalog/detail
are implemented separately.

**Tests:** `documents/test_author_name_edit.py`.

## Staff Author index

**Decision / implemented:** Staff can list every **`Author`** at
**`/archive/manage/authors/`** (**`archive-manage-authors`**). This is a
staff catalog/index only. **`Author`** remains separate from **`Person`**.

**Current behavior:**

- Page is staff-only (**`@login_required`** + **`_require_admin_page`**), same
  policy as **`archive_manage_author_edit_page`** and
  **`archive_manage_people_page`**. GET only. Reached from **ניהול מחברים**
  next to **ניהול אנשים** on **`/archive/manage/`**. Layout follows the staff
  People index (search toolbar, table, empty / no-match copy).
- Rows are every **`Author`**, ordered by **`(name, id)`**. Optional GET
  **`q`** is trimmed and matched with case-insensitive substring on
  **`Author.name`** only (no aliases, Person, fuzzy, or
  **`ArchiveItem.author_name`** lookup). Trimmed **`q`** is preserved in the
  field; a clear/reset control appears when **`q`** is nonempty.
- Each row shows **`Author.name`**, **`Author.id`**, annotated
  **`ArchiveItemAuthor`** count (**`Count("archive_item_links")`**), and
  **עריכה** to existing **`archive-manage-author-edit`**. Merge is reached
  from the edit page, not from this index.
- No standalone Author create, delete, aliases, Person integration, PHOTO
  author UI, or schema changes. Public Author catalog/detail is implemented
  separately (see **Public Author catalog and Author detail**).

**Out of scope / deferred:** standalone Author delete; PHOTO author support.
Author merge is implemented separately (see **Staff Author merge**). Public
Author browsing is implemented separately (see **Public Author catalog and
Author detail**).

**Tests:** `documents/test_author_staff_index.py`; route expectation in
`documents/test_author_name_edit.py`.

## Staff author exact-name reuse and explicit item unlink

**Decision / implemented:** Staff **`new_author_name`** tokens reuse a unique
exact **`Author.name`** match instead of rejecting it. Currently linked
authors are kept or unlinked with per-author checkboxes (no Ctrl-click
native multi-select). Unlink removes **`ArchiveItemAuthor`** rows only.

**Current behavior:**

- Each ordered **`new_author_name`** token: exactly one exact-name **`Author`**
  → reuse; none → create; more than one exact match → fail closed before any
  item / Author / link write (`AMBIGUOUS_AUTHOR_ERROR`). Token order is
  preserved. If that Author is already in submitted **`author_ids`** or repeats
  in the token list, the first occurrence is kept.
- Submitted **`author_ids`** are the kept/added existing Authors. Kept linked
  authors render as checked checkboxes in stored **`position`** order; the
  multi-select lists only authors that are not currently kept. A no-op save
  re-submits the checked ids. Omitting an id unlinks that Author from the
  item and does **not** delete the global **`Author`** row.
- One save may unlink an old Author and add/reuse another. Final order is
  kept ids (submitted order) then reused/created names (token order).
  Dual-write remains **`author_name = ", ".join(final ordered names)`**.
  Empty ids plus empty new names still clears links and **`author_name`**.
- Validation stays before writes; HTTP 200 re-renders submitted
  **`author_ids`** / **`new_author_name`**. Search-index refresh after the
  writer is unchanged. **`Author`** stays separate from **`Person`**. PHOTO /
  public UI / models / schema are unchanged.

**Supersedes:** the PR2 rule that typed exact existing names are rejected,
and the PR2 native multi-select ordering workaround for currently linked
authors.

**Tests:** `documents/test_archive_item_author_staff_ux.py`.

## Multi-author staff create/edit UX (PR2)

**Decision / implemented:** Staff create/edit for **OCR_DOCUMENT**,
**MANUAL_TEXT**, and **VIDEO** selects multiple existing **`Author`** rows and
can add comma-separated new names. This does **not** change PHOTO author UI,
public display, `q` indexing, snippets, advanced filters, models, or schema.

**Current behavior:**

- Staff POST/JSON fields are **`author_ids`** (submitted order) and
  **`new_author_name`** (ASCII commas). **`author_name`** is not a staff write
  field on these paths. Typed unique exact names are reused (see **Staff
  author exact-name reuse and explicit item unlink**); multiple exact matches
  fail closed.
- New tokens are trimmed, empty tokens dropped, order-deduped within input,
  max 255 per name. Commas-only input is invalid. Selected ids keep submitted
  order; reused/created names append in token order. Currently linked authors
  use keep/remove checkboxes; the add picker lists unselected authors by
  **`(name, id)`**.
- Writers dual-write **`author_name = ", ".join(ordered names)`**. The joined
  compatibility string must fit **`CharField(max_length=255)`**; over-length is
  rejected before writes. Empty ids plus empty new names clears links and
  **`author_name`**.
- Staff HTTP/JSON calls **`apply_staff_archive_item_authors`** inside the
  existing OCR / MANUAL_TEXT / VIDEO writers before search-index sync. They
  never pass the joined string to **`apply_legacy_author_name`**. The legacy
  **`author_name=`** service path is unchanged and still does not split commas.
- **`Author`** remains separate from **`Person`**. These paths never create or
  infer **`Person`**, **`ArchiveItemPerson`**, or **`PhotoPerson`**.

**Deferred:** PHOTO author UI; unique **`Author.name`**; independent
drag-reorder of selected authors; removing the legacy **`author_name=`**
writer path. Public display, ``q``, and advanced Author filter cutover from
**`author_name`** is implemented separately.

**Tests:** `documents/test_archive_item_author_staff_ux.py`;
`documents/test_author.py` (foundation + PHOTO isolation).

## Multi-author data foundation (PR1)

**Decision / implemented:** Add a bibliographic **`Author`** model and ordered
**`ArchiveItemAuthor`** links, plus a 1:1 exact-string backfill from
**`ArchiveItem.author_name`**. This PR is data foundation only. It does **not**
change public display, `q` indexing, snippets, advanced filters, PHOTO author
UI, templates, or URLs.

**Current behavior:**

- **`Author`** is a bibliographic name (`CharField(max_length=255)`), **not
  unique**, with **`created_at`** / **`updated_at`**. It is **not** a
  **`Person`**. Dual-write and backfill never infer, create, or link
  **`Person`**, **`ArchiveItemPerson`**, or **`PhotoPerson`**.
- Titles such as ד״ר / Dr. remain part of the stored **`Author.name`** for now.
  There is no title-stripping, spelling merge, or casefold.
- **`ArchiveItemAuthor`** is the through row for **`ArchiveItem.authors`**:
  **`archive_item`** FK CASCADE (`related_name=author_links`), **`author`** FK
  CASCADE (`related_name=archive_item_links`), **`position`**
  (`PositiveSmallIntegerField`), **`created_at`**. Unique
  **`(archive_item, author)`** and **`(archive_item, position)`**. Ordering is
  **`(archive_item, position, id)`**.
- **`ArchiveItem.author_name`** remains the compatibility field used by public
  display, search indexing, snippets, and the advanced author filter.
- Backfill (`0059_backfill_authors_from_author_name`) creates one **`Author`**
  per distinct exact non-empty stored **`author_name`**, and one
  **`ArchiveItemAuthor`** at **`position=0`** for each matching item. It does
  **not** split commas. Empty / whitespace-only values create no rows. Every
  item type with a stored value is included, including PHOTO. Reverse deletes
  **`Author`** / **`ArchiveItemAuthor`** rows; **`author_name`** stays intact.
- Legacy service **`author_name=`** still dual-writes one Author at
  **`position=0`** (`apply_legacy_author_name`). Staff HTTP/JSON create/edit
  uses the PR2 multi-author path instead of this string writer. Whenever the
  legacy path saves **`author_name`**, it keeps the exact normalized string
  and replaces that item's author relations with one exact-name **`Author`**.
  Empty clears relations. Exactly one matching **`Author`** is reused; when
  none exists, one is created; multiple exact matches fail closed with no
  partial item / string / relation writes. Comma input is not split.
- PHOTO create/edit still has no author UI and does **not** dual-write.
- Staff UI reuses a unique exact **`Author.name`** and fails closed on
  ambiguous duplicates (see **Staff author exact-name reuse and explicit item
  unlink**). This PR allows duplicate display names (same as **`Person.name`**).

**Migrations:** `0058_author_foundation` (schema) +
`0059_backfill_authors_from_author_name` (reversible data).

**Deferred (superseded in part by PR2 and later public Author work):**
~~staff Author UI / duplicate-create guard; comma-separated multi-author
input~~ → **Multi-author staff create/edit UX (PR2)**. ~~public
display/search/filter cutover from **`author_name`**~~ → public Author
pages, item presentation, ``q``, and advanced Author filter. Remaining:
PHOTO author UI.

**Tests:** `documents/test_author.py`.

## Prefixable archive date-entry widget

**Decision / implemented:** The shared staff date-entry widget can render two
independent instances on one page. This is a DOM/JS scoping change only. It
does not unify the PHOTO management page, change POST field names, or alter
current single-widget create/edit/upload behavior.

**Current behavior:**

- `archive_date_form_fields.html` accepts optional `date_widget_prefix`.
  Empty/omitted prefix keeps the existing unprefixed DOM ids
  (`date_precision`, `archiveDateEntry`, and the segmented input ids).
- A non-empty prefix is applied to every widget DOM `id`, matching `for`,
  and `aria-labelledby`. POST `name` values stay `date_precision`,
  `date_start_*`, and `date_end_*`.
- The precision select and segmented inputs are wrapped in
  `[data-archive-date-widget]`. `archive_date_entry.js` initializes every
  widget root and scopes lookups to that root instead of document-wide
  singleton ids.
- Existing `window.vsArchiveDateEntry.collectMeta(meta)` callers keep working
  on single-widget pages. A second argument may pass a widget root when more
  than one instance is present. Bare `collectMeta(meta)` does not guess among
  multiple prefixed widgets.
- Current create/edit/upload/add-photo templates do not pass a prefix.

**Why:** A later unified PHOTO page needs item-level and photo-level date
widgets on the same screen. Duplicate ids would make labels and JS attach to
the first widget only.

**Deferred:** ~~unified staff PHOTO page / actually placing two prefixed
widgets on a live page~~ → implemented in **Unified staff PHOTO edit
cards**. Remaining: public UI date widgets; staff `?photo=` selector.

**Tests:** `documents/test_archive_date_widget_prefix.py`; existing
`documents/test_archive_date_input.py` create/edit/upload markup tests.

## PHOTO add-photo identified people (PhotoPerson)

**Decision / implemented:** Staff add-photo can select existing people and
create comma-separated new people as **`PhotoPerson`** on the new
`PhotoContent` only. This reuses the #468 parser/create services. It does
not change PHOTO item create, shared metadata, redirects, or layout.

**Current behavior:**

- `/archive/manage/<id>/photos/add/` includes the same PhotoPerson
  controls as per-photo edit (`photo_person_form_fields.html`): existing
  `person_ids` picker and `new_person_name`.
- Add-photo JS sends `person_ids`, `new_person_name`, and when present
  `force_create_person` to `POST /api/photo-uploads/add/`. It does not send
  `archive_item_person_ids` / `new_archive_item_person_name`.
- Person input is validated before any `PhotoContent` or `Person` row is
  created (id format, existing ids, commas-only, per-token length,
  canonical/alias duplicate candidates). Invalid input is HTTP 400 with
  no new rows. Duplicate-name warnings use `error_code=PERSON_NAME_CANDIDATES`
  (see **Staff new-Person duplicate prevention**).
- Selected ids plus each parsed new name that is clear or force-approved
  become **`PhotoPerson` only** on the new photo. Exact in-input dedupe
  matches #468. `people_present` stays independent free text.
- PHOTO item create (`/api/photo-uploads/create/`) still writes
  **`ArchiveItemPerson` only**. Extra `person_ids` / `new_person_name` on
  that payload are ignored.
- Add-photo JS still redirects to the item management page.

**Why:** Staff often identify people while attaching another image. Waiting
until the per-photo editor forced a second round-trip.

**Deferred:** unified staff PHOTO page / selector; staff `?photo=` after
add; lookup/merge by name; parsing `people_present`.

**Tests:** `documents/test_photo_multi_manage.py` (`PhotoAddUploadTests`).

## PHOTO staff save stays on the same page

**Decision / implemented:** Successful staff PHOTO metadata saves remain on
the page that was submitted. This is a redirect-contract change only. It
does not unify the shared-metadata page with the per-photo editor, add
staff `?photo=` selection, or change add-photo / Person write paths.

**Current behavior:**

- Shared PHOTO metadata save (`POST /archive/manage/<id>/edit/`) redirects
  to the same item edit URL, not `/archive/manage/`.
- Per-photo save (`POST /archive/manage/<id>/photos/<photo_id>/edit/`)
  redirects to that same per-photo edit URL, not the item photo list.
- Validation errors still re-render the submitted form as HTTP 200 with
  no `Location` header.
- Item manage toolbar keeps the existing **חזרה לפריט** public link to
  `/archive/<id>/` (no extra **צפייה** button on that page). Per-photo
  edit keeps **חזרה לפריט** to the staff item-management page and adds a
  separate **צפייה** link to `/archive/<id>/?photo=<photo_id>`. Public
  gallery selection behavior is unchanged.
- Unchanged: PHOTO create redirect to the manage list; add-photo JS
  redirect to item edit; reorder/delete-one-photo redirects to item edit;
  whole-item delete redirect to the manage list; `people_present`; date
  widgets (prefixable, still unprefixed on current pages); public pages.
  Add-photo PhotoPerson is implemented separately
  (see **PHOTO add-photo identified people**).

**Why:** Leaving the item or the edited photo after every save forces extra
navigation. Stay-on-page matches the already-decided unified-management UX
without merging layouts yet.

**Deferred (superseded in part):** ~~unified staff PHOTO page~~ →
**Unified staff PHOTO edit cards**. Remaining: staff `?photo=` selector
on the item edit URL. Non-inline POST to the standalone photo-edit URL
still redirects to that same URL.

**Tests:** `documents/test_photo_manage_edit_delete.py`,
`documents/test_photo_multi_manage.py`.

## Comma-separated new Person names (staff create)

**Decision / implemented:** Staff **new Person** fields accept comma-separated
canonical names on the existing item-level and photo-level inputs. This is a
convenience for creating several new identities in one submit. It is not
identity merge, alias create, or lookup-by-name.

**Current behavior:**

- Fields: item-level **`new_archive_item_person_name`** (ArchiveItem create
  and edit, including PHOTO create) and photo-level **`new_person_name`**
  (per-photo edit and add-photo). Same parse/split contract.
- Split on ASCII comma, trim each token, drop empty tokens, then
  order-preserving dedupe **within that submitted string only**. Repeated
  tokens do not create extra Person rows. Dedupe is exact after trim
  (`Ada` and `ada` remain distinct).
- Each remaining token creates a new **`Person`** through
  **`create_identified_person`** **unless** an existing canonical or alias
  exact (case-insensitive) match requires per-token force-create (see
  **Staff new-Person duplicate prevention**). No `get_or_create`. No
  automatic reuse or merge. Duplicate display names remain allowed after
  explicit confirmation.
- Item input writes **`ArchiveItemPerson` only**. Photo input writes
  **`PhotoPerson` only**. Neither relation is inferred from the other.
- Per-token **`Person.name`** limit remains 255 characters. The HTML inputs
  no longer use `maxlength="255"` so a multi-name list is not capped at
  255 characters as a whole. A list longer than 255 is valid when each
  token is ≤ 255.
- Empty / whitespace-only input remains a no-op. Nonempty input that is
  only commas/whitespace is rejected with
  **`PERSON_NAMES_COMMAS_ONLY_ERROR`** and creates no Person / join rows.
- Hebrew field hints on both inputs describe comma-separated create.
- Unchanged: **`people_present`**, **`author_name`**, aliases, redirects,
  search, photo-page layout, picker ids, and C2 suggestion
  flows. Add-photo PhotoPerson is implemented separately (see **PHOTO
  add-photo identified people**).

**Why:** Staff often need to add several new people at once. Comma-separated
create on the existing fields avoids extra round-trips without weakening
the always-create-after-confirmation / no-merge identity contract.

**Deferred:** fuzzy lookup/merge by name; bulk alias create; parsing
`people_present`; public search. Add-photo identified people is
implemented (see **PHOTO add-photo identified people**). Exact
canonical/alias duplicate warnings are implemented (see **Staff
new-Person duplicate prevention**).

**Tests:** `documents/test_archive_item_person_staff_ui.py`,
`documents/test_photo_multi_manage.py`.

## Antigravity supported request contract

**Decision / implemented:** Antigravity Interactions create requests use the
live-validated request envelope. The general Gemini Interactions schema is
not the same as Antigravity-specific compatibility: fields that exist in
the broader schema can still be unknown parameters for this agent.

**Current behavior:**

- Create POST body top-level keys are exactly `agent`, `input`,
  `environment`, `background`, `tools`, and `agent_config`. Built by
  `build_antigravity_create_payload` in `antigravity_engine.py`.
- `agent` remains the configured/default agent ID
  (`ANTIGRAVITY_AGENT_ID` / `DEFAULT_ANTIGRAVITY_AGENT_ID`).
- `input` remains the existing multimodal OCR-contract prompt plus inline
  images.
- `background` remains the existing argument (production default `true`).
- `environment` is `{"type": "remote", "network": "disabled"}`.
- `tools` is the empty array `[]`: a live-validated empty tools
  configuration, not a universal provider guarantee that the agent
  cannot use tools. Unexpected-tool fail-closed validation remains
  defense in depth.
- `agent_config` is `{"type": "antigravity", "model": "gemini-3.7-flash"}`.
  The model is the requested/pinned code constant
  `ANTIGRAVITY_REQUESTED_MODEL`. There is no environment-variable override.
- The provider response does **not** echo an effective underlying model.
  Logs may record the requested/pinned model; they must not treat it as
  provider-confirmed.
- Create does **not** send `tool_choice`, `system_instruction`,
  `response_format`, `generation_config`, or `store`.
- Create POST remains a single attempt. There is no compatibility fallback
  that retries create with alternate fields.
- Application-level OCR output validation remains necessary and unchanged:
  Antigravity structured output is unsupported. Unexpected tool steps still
  fail closed. Prompt and response schema in `antigravity_ocr_contract.py`
  are unchanged.

**Live evidence (isolated probes, 2026-08-26):**

- Production create with `tool_choice: "none"` returned HTTP 400
  `Unknown parameter 'tool_choice'` before an interaction ID was created.
- P0: canonical request without `tool_choice` → HTTP 200, completed.
- P2: added `tools: []` → HTTP 200, completed, no tool steps, zero
  tool-use tokens.
- P3: added `agent_config: {"type": "antigravity", "model": "gemini-3.7-flash"}`
  → HTTP 200, completed. Response did not expose a machine-readable
  effective model.
- P4: `environment: {"type": "remote", "network": "disabled"}` → HTTP 200,
  completed.
- P1 (`system_instruction`) was intentionally not run; it is unnecessary.
- P5 made **no** provider call in the original probe matrix: the repository
  had no suitable printed-Arabic image fixture.

**Follow-up implementation (2026-08-26):** a small, synthetic,
non-sensitive printed-Arabic image and its ground-truth text are now
repository-owned under `scripts/dev/fixtures/`. The existing explicit
`antigravity-image` smoke mode can use the fixture for provider contract
validation.

**P5 live fixture validation (2026-08-27):** one explicit provider call used
the 34,730-byte repository PNG (SHA-256
`9f5cd9c6b79c82ab483837601aadbf6e3c070a0791782bf2120f64cfade0b493`)
with the supported Antigravity OCR envelope. The interaction completed,
produced non-empty OCR, and reproduced all three ground-truth lines exactly
after whitespace normalization (`missing_lines=[]`, `fixture_match=True`,
`smoke_rc=0`). The API key was read from
`vs-archive-dev/gemini_api_key` in AWS Secrets Manager without printing or
persisting it; no AWS or production state was modified. P5 is closed.

**Deferred:** Antigravity native structured outputs if/when the provider
supports them; `system_instruction`; `response_format`; create-POST
fallback/retry; environment-variable model override.

`tool_choice` must not be reconsidered unless Antigravity-specific
documentation explicitly supports it and a new isolated live probe
confirms it. It is not an ordinary deferred implementation.

**Supersedes:** the create-payload claim in **Antigravity inline-image OCR
output contract** that sent `"tool_choice": "none"` and omitted an empty
`tools` array. Output-validation rules in that entry remain current.

**Tests:** `documents/test_antigravity_ocr.py`.

## Antigravity inline-image OCR output contract

**Decision / implemented:** Antigravity provider status `completed` is
necessary but not sufficient for OCR success. Document 320 showed a
completed interaction that returned a generic assistant greeting instead
of transcription; that output was previously accepted, translated, and
persisted as successful OCR. Validation is now structural and fail-closed.

**Current behavior:**

- **Create payload (superseded):** the previous `"tool_choice": "none"`
  create field is **not** current supported behavior. Live production
  create with `tool_choice` returned HTTP 400 `Unknown parameter
  'tool_choice'` before an interaction ID was created. Current request
  contract: **Antigravity supported request contract**. The prompt still
  states the job is transcription of the supplied inline images only.
  Tools are not permitted. The payload still does **not** send
  `response_format` (Antigravity structured output is currently
  unsupported).
- Prompt-level JSON contract (`schema_version: 1`, one `pages` entry per
  inline image, outcomes `transcribed` / `blank` / `unavailable`) is
  validated with the standard library. No greeting denylist, no
  Arabic-character heuristic, no minimum-length guess.
- Only the last `model_output` step is parsed. Thoughts, function results,
  code results, and tool output are never treated as OCR. For a new OCR
  request, any tool-call or tool-result step (`function_call`,
  `function_result`, `code_execution_call`, `code_execution_result`, and
  the older `tool_call` / `tool_result` names) is a contract violation
  even if a later model message looks plausible.
- `AntigravityOutputValidationError` (subclass of `AntigravityError`)
  carries a machine-readable `reason` (`missing_model_output`,
  `unexpected_tool_use`, `invalid_json`, `invalid_contract`,
  `page_count_mismatch`, `page_index_mismatch`, `empty_transcription`,
  `input_unavailable`, `no_transcribed_text`). Messages and logs may
  include expected/actual counts and page indexes. They must not include
  provider text or document contents.
- Validation runs before a successful `AntigravityResult` is constructed.
  The existing adapter/worker path persists durable `SOURCE_TEXT` failure
  with `OCR_FAILED`. There is no translation, no successful source-text
  persistence, no transition to `READY`, no automatic second interaction,
  and no change to manual-retry eligibility.
- After validation, a single transcribed page is stored without a
  synthetic heading. Multiple pages use deterministic `עמוד N` sections.
  The legacy `[IMAGE N: filename]` format is not an accepted success
  contract. Historical stored rows are not rewritten; existing invalid
  production rows require an explicit operational retry or separate
  cleanup.

**Deferred:** Antigravity native structured outputs if/when the provider
supports them; automatic repair interactions; rewriting historical
invalid Document 320-style rows.

**Tests:** `documents/test_antigravity_ocr.py`.

## Public Person biography

**Decision / implemented:** Optional staff-authored plain-text
**`Person.biography`** is shown on the existing public Person page when
nonempty. It is not AI-generated, not indexed, and does not change the
Person-page authorization/404 contract.

**Current behavior:**

- Field: **`Person.biography`** (`TextField(blank=True, default="")`).
  Empty string, not NULL. No uniqueness, no DB max length, no backfill.
  Existing rows stay `""`. New Person creates (name-only picker) stay
  empty.
- Staff write: `update_person_biography` in
  `photo_content_management.py`. `None` and whitespace-only become `""`
  (clears the field). Leading/trailing whitespace is stripped; internal
  newlines are preserved. Unchanged values are a no-op. Saves
  `["biography", "updated_at"]` only. Does **not** refresh
  `ArchiveItemSearchIndex` and does not touch aliases, ArchiveItemPerson,
  PhotoPerson, or Tags.
- Staff UI: third POST form on
  `/archive/manage/people/<id>/edit/` (`action=update_biography`), after
  canonical name and before aliases. Label **תקציר**; button **עדכון תקציר**.
  Access is unchanged (`@login_required` + `_require_admin_page`).
- Public UI: `/archive/people/<id>/` shows meaningful nonempty biography
  directly after the `<h1>` name, unlabeled, with Django autoescape and
  **`linebreaksbr`**. Empty/whitespace/placeholder-only values are omitted
  (`meaningful_archive_metadata`). No `|safe`, markdown, or rich text.
  Aliases stay hidden. No public staff-edit link.
- Authorization/404: missing Person, unlinked Person, private-only
  (anonymous), and non-renderable-only links still **404** even when
  biography is nonempty. A visible authorized `PhotoPerson` appearance
  is sufficient to open the page (see **Public Person page PhotoPerson
  appearances**). Biography is display-only on an otherwise accessible
  Person page.
- Search: **`Person.biography` is not indexed.** `q` still uses
  `Person.name` / `PersonAlias.name` on ArchiveItem index rows only.

**Migration:** `0057_person_biography` (additive `AddField`; depends on
`0056_archive_item_person_suggestion`). No data migration.

**Deferred:** public alias display; PhotoPerson appearance
filter; Person catalog/Admin; search-index inclusion of biography; AI-
generated biography text; biography on cards, chips, item detail, or
other public surfaces. PhotoPerson name linking on public PHOTO detail
is implemented (see **Public PhotoPerson name links**).

**Tests:** `documents/test_person.py`,
`documents/test_person_staff_ui.py`,
`documents/test_archive_person_public_page.py`.

## Antigravity polling GET transient HTTP retry

**Decision / implemented:** After a successful Antigravity Interactions
create POST, unary polling GET `/interactions/{id}` retries the **same**
interaction ID on transient HTTP statuses. Create POST is not retried and
does not create a replacement interaction.

**Current behavior:**

- `_raise_for_api_error` raises `AntigravityHttpError` (subclass of
  `AntigravityError`) with a machine-readable `status_code`. The
  human-readable message remains `HTTP {status}: {message}`.
- Polling GET retries only `408`, `429`, `500`, `502`, `503`, and `504`.
  Non-retryable GET statuses such as `400` and `403` fail immediately.
- Retry delay is bounded exponential backoff with small jitter. Sleeps are
  capped so retries stay inside the existing **1200s** overall
  `in_progress` deadline. There is no independent max-attempt counter.
- Ordinary successful `in_progress` polling still uses the **5s** interval.
  `requests.Timeout` / `ReadTimeout` still retry the same interaction ID.
- Retry logs include `document_id`, sanitized `interaction_id`, HTTP
  status, retry count, selected delay, and elapsed time. They do not log
  response bodies, OCR text, raw bytes, Base64, or API keys.

**Deferred:** streaming; cancellation; per-page interactions; create-POST
retry; worker lease/persistence changes.

**Tests:** `documents/test_antigravity_ocr.py`.

## Historical person-Tag row deletion (D2b)

**Decision / implemented:** The 29 frozen historical person-name Tag *rows*
are deleted by an explicit management command. Identity remains the frozen
**(Tag.id, Person.id, exact Tag.name)** triples in
`documents/historical_person_tag_map.py`. The map is kept. Person,
`ArchiveItemPerson`, `PhotoPerson`, ordinary Tags, search indexes, and
Tag.id sequences are not written.

**Current behavior:**

- `delete_historical_person_tag_rows` is read-only dry-run by default.
  `--apply-rows` deletes only the 29 exact frozen Tag rows in one
  `transaction.atomic()`.
- Valid states: all 29 `(id, exact name)` rows present, or all 29 Tag ids
  absent. Partial presence, a frozen id with the wrong name, or a retired
  name on a non-mapped PK fail closed.
- Apply re-checks those invariants inside the same transaction, locks Tag
  rows, mapped through rows, and PENDING `ArchiveMetadataSuggestion` rows
  (`select_for_update`), and fail-closes unless Django `.delete()` returns
  exactly `29 / {documents.Tag: 29}`. Postconditions (no mapped Tag rows,
  no mapped through rows, no retired names on other PKs) are verified;
  mismatch rolls back.
- Refuse (no writes) while mapped `ArchiveItem.tags` or
  `Document.tags_m2m` through rows exist, while
  `pending_archive_metadata_suggestions_with_retired_tag_names()` is
  non-empty, or while any mapped Person.id is missing.
- Idempotent: after successful deletion, dry-run and `--apply-rows`
  report `state=all_absent`, `planned=0`, `deleted=0`.
- D1 `cleanup_historical_person_tags` and
  `reconcile_historical_person_tag_relations` treat all-29-absent Tags
  plus all 29 Person ids as `planned=0` success. Partial Tag absence and
  missing Person ids remain fail-closed. Neither command recreates Tag
  rows or invents `ArchiveItemPerson` from the map when Tags are gone.
- Stage B mapped-tag browse still 302s from the map. D2a retired names
  still block `get_or_create`. `TagAdmin` still refuses mapped-id delete.

**Deferred:** sequence reset (must not; frozen PK reuse would hit
ID-membership helpers); removing the frozen map; query-string `?tag=`
redirects (Option 1+); schema mapping table.

**Tests:** `documents/test_delete_historical_person_tag_rows.py` plus
coexistence coverage in `documents/test_cleanup_historical_person_tags.py`
and `documents/test_reconcile_historical_person_tag_relations.py`. No
schema or data migration.

## Antigravity Arabic OCR — original JPEG payload and longer unary polling (PR1)

**Decision / implemented:** Fix the two confirmed contributors to the
Document 321 Arabic Antigravity timeout: JPEG sources were expanded to PNG
before the Interactions request, and unary background polling used a 300s
overall deadline with 60s GET timeouts.

**Current behavior:**

- Shared ``PageImage.image_bytes`` / ``PageImage.mime_type`` remain
  **normalized PNG** for Gemini, Transkribus, checkpoints, fingerprints,
  PDF rendering, and ``source_content_fingerprint``. Those callers are
  unchanged.
- Image sources also retain optional ``original_image_bytes`` /
  ``original_mime_type``. PDF-rendered pages have no original encoded image.
- **Antigravity only:** if original JPEG is present (``image/jpeg`` or
  ``image/jpg``), the engine sends those exact bytes with canonical
  outbound MIME ``image/jpeg``. Otherwise it sends the existing normalized
  PNG. No re-encode, resize, downscale, grayscale, or arbitrary non-JPEG
  original formats.
- Observability describes **bytes actually sent** (outbound MIME, byte
  length, safe SHA-256 prefix). Logs do not include raw bytes, Base64, or
  document text.
- Production polling defaults: overall in_progress deadline **1200s**,
  unary GET timeout **120s**, poll interval unchanged (**5s**). Create
  timeout is unchanged (``max(120, 30 * page_count)``). Background create
  plus unary GET polling is unchanged. ``requests.Timeout`` /
  ``ReadTimeout`` still retry the same interaction ID. 1200s stays clearly
  below the worker **45-minute** processing lease.
- **Follow-up:** polling GET also retries transient HTTP statuses
  (`408`/`429`/`500`/`502`/`503`/`504`) for the same interaction; see
  **Antigravity polling GET transient HTTP retry**.
- This does **not** claim an Antigravity 20 MB request limit. Payload
  reduction is the JPEG-passthrough choice only.

**Deferred / follow-up (not this PR):** streaming Interactions; durable
recovery / cancellation / per-page interactions; recovery-state changes;
``include_input=false``; persistence or migrations; UI.

**Tests:** ``documents/test_antigravity_ocr.py``,
``documents/tests.py`` (``PageImageOriginalEncodingTests``), mixed-content
``PageImage`` field-set contract.

## Retired historical Person-Tag names (D2a)

**Decision / implemented:** The 29 frozen historical person-name Tag names
are an immutable runtime retired-name policy in
`documents/historical_person_tag_map.py`, stored alongside the frozen
**Tag.id → Person.id** identities. Identity remains **Tag.id → Person.id**.
`Person.name` is not identity. There are no automatic aliases and no
broad person-name denylist.

**Current behavior:**

- Runtime consumers must not import migration `0055`. The artifact is
  validated at import: 29 records; unique Tag ids, Person ids, and Tag
  names; names already trimmed.
- Matching runs only after the existing tag-name parse/trim
  (`normalize_tag_names_from_list` / suggestion parse). No casefold,
  fuzzy matching, or alias matching.
- A retired name stays blocked even when the original mapped Tag row no
  longer exists, so `get_or_create(name=…)` cannot resurrect it under a
  new PK. Mapped-ID blocks remain while Tag rows still exist.
- Shared helpers (`retired_historical_person_tag_name_errors`,
  `historical_person_tag_name_write_errors`) are used on audited Tag
  create/reuse paths: staff ArchiveItem metadata create/edit,
  `update_ocr_document_tags`, public `ArchiveMetadataSuggestion`
  submission, suggestion approval, Django Tag admin add/change, and
  Document admin `tags_m2m`. A mapped Tag row may be saved unchanged in
  Tag admin; it must not be renamed to any other name (including an
  ordinary name). Ordinary Tag names stay creatable and reusable.
- PENDING suggestions are not auto-rejected, mutated, or deleted.
  Submitting a retired name fails clearly. Approval rechecks the policy
  inside the existing `transaction.atomic()` and writes no partial
  metadata.
- `pending_archive_metadata_suggestions_with_retired_tag_names()` is a
  read-only inventory. D2b refuses while it is non-empty.

**Deferred (D2b, now implemented):** deleting the 29 mapped Tag rows.
See **Historical person-Tag row deletion (D2b)**.

**Tests:** `documents/test_historical_person_tag_retired_names.py` plus
updated map coverage in `documents/test_historical_person_tag_map.py`.
No schema or data migration.

## Public Person active-filter chip UX

**Decision / implemented:** Public `/archive/` active-filter summary renders
**one chip per selected `person=` id**, not one grouped Person chip.
Canonical **`Person.name`** is a GET link to **`archive-person-detail`**.
A separate visible **×** is a GET link that removes only that Person id.

**Current behavior:**

- Non-Person chips (q / author / category / event / tag / year) stay
  grouped whole-chip remove links. Category/event/tag still clear their
  entire id group in one click.
- Person remove uses `archive_advanced_filters_without_person` and
  `build_archive_public_list_query(..., advanced_open=True)`. The remove
  URL always includes **`advanced=1`**, including after the last Person,
  so the advanced panel stays open instead of returning to the unfiltered
  archive homepage.
- Remove preserves `q`, remaining Person ids, author / category / event /
  tag / year / year_to, `item_type`, and `per_page`. It drops `page`.
- The × control’s `aria-label` includes the Person canonical name
  (`הסרת {name}`). CSS uses logical properties, `:focus-visible`, and a
  2.75rem minimum tap target.
- Chip labels come from already-loaded `person_choices` (same conditional
  choice-context load as the picker). `person_public_page_url` is reverse
  only. No extra Person queries per selected id.

**Deferred:** public alias display on chips/picker; photo-level PhotoPerson
appearance cards on `/archive/` (list `person=` is unified AIP ∪ renderable
PhotoPerson; see **Public `/archive/` Person advanced filter — unified AIP ∪
PhotoPerson**); Person Admin. Public biography is
implemented (see **Public Person biography**). D2a
retired-name policy is implemented. D2b Tag-row deletion is implemented
(see **Historical person-Tag row deletion (D2b)**).

**Tests:** `documents/test_archive_advanced_search_person.py`. No schema
migration.

## Public Person page

**Superseded in part:** Public catalog is implemented (see **Public People
catalog and unified Person detail**). Person detail is now one unified
ArchiveItem stream; the separate PhotoPerson appearance section is removed
from the primary public Person page. Advanced `person=` uses AIP or
renderable PhotoPerson (see **Public `/archive/` Person advanced filter —
unified AIP ∪ PhotoPerson**).

**Decision / implemented:** Public Person detail exists at
**`/archive/people/<person_id>/`** (`archive-person-detail`). It lists
ArchiveItems generally related to that Person via **`ArchiveItemPerson`**
and, separately, photos where the Person appears via **`PhotoPerson`**.
Related items use the same authorized/renderable browse queryset as
`/archive/`. This started as Option A (canonical **`Person.name`** plus
related public cards). PhotoPerson appearances were added later (see
**Public Person page PhotoPerson appearances**). Optional staff-authored
**`Person.biography`** is documented under **Public Person biography**.

**Historical behavior (before unified stream; see **Public People catalog
and unified Person detail** for current behavior):**

- Route is registered before the `<int:item_id>/` catch-all. Missing
  Person ids **404**. Persons with **zero** authorized/renderable
  `ArchiveItemPerson` items **and** zero authorized/renderable
  `PhotoPerson` appearances **404** (same status; no private-count
  leak). Non-renderable PHOTO/OCR-only ArchiveItemPerson links 404.
  Pending/failed/empty-key PhotoContent does not make the page
  accessible.
- Related items: `archive_browse_queryset_for_user(request.user)` filtered
  by `ArchiveItemPerson.person_id`. Count is that queryset’s length.
  Family/restricted-capable users may see a higher authorized count than
  anonymous. Duplicate canonical names stay distinct by `Person.id`.
- Pagination is fixed **48** per page (`page=`) for the ArchiveItemPerson
  section only; invalid/out-of-range `page` follows
  `normalize_archive_public_list_page` (clamp). Photo appearances are a
  separate unpaged section on the same page. No `q`, type tabs, advanced
  filters, or `per_page` control. Related-item order is `-created_at`,
  `pk`.
- Page shows canonical name, optional nonempty staff-authored biography
  (unlabeled, autoescaped, `linebreaksbr`; see **Public Person
  biography**), related-item heading **פריטים הקשורים לאדם** with
  authorized related-item count and browse cards when that section is
  non-empty, photo-appearance heading **תמונות שבהן האדם מופיע** with
  appearance cards when that section is non-empty, page nav when related
  items span multiple pages, and **חזרה לארכיון**. Aliases are not
  displayed. No public staff-edit link.
- Item-level **אנשים קשורים** links on archive cards, homepage cards,
  archive detail, and OCR document detail go to the Person page
  (`person_public_page_url`). PhotoPerson **אנשים מזוהים:** names are
  canonical-name links to the same Person page (see **Public PhotoPerson
  name links**). PhotoPerson still does not create ArchiveItemPerson
  cards.
- Stage B mapped historical Tag browse
  `/archive/tags/<mapped_tag_id>/` now **302**s to the Person page
  (map-first). Following that URL still 404s when the Person has no
  authorized related items and no authorized photo appearances. Ordinary
  Tag browse is unchanged. Advanced `person=` list filtering uses AIP or
  renderable PhotoPerson (see **Public `/archive/` Person advanced filter —
  unified AIP ∪ PhotoPerson**). Active Person filter chips are implemented
  separately (see **Public Person active-filter chip UX**).

**Deferred:** public alias display; PhotoPerson appearance filter;
Person Admin. Combined related-item/appearance pagination is implemented
as one unified ArchiveItem stream (see **Public People catalog and
unified Person detail**).
Public biography/summary is implemented (see **Public Person
biography**).
D2a retired-name policy is implemented. D2b Tag-row deletion is
implemented (see **Historical person-Tag row deletion (D2b)**).

**Tests:** `documents/test_archive_person_public_page.py` plus updated
Stage A presentation, Stage B redirect, advanced-filter, and staff
route tests. The original page PR had no schema migration; biography
uses `0057_person_biography`.

## Historical person-Tag relation cleanup (D1)

**Decision / implemented:** Mapped historical person-name Tag *relations* are
removed by an explicit management command. Tag rows stay. Identity remains
**Tag.id → Person.id** from `documents/historical_person_tag_map.py`. Person,
`ArchiveItemPerson`, `PhotoPerson`, and ordinary Tags are not written.

**Current behavior:**

- `cleanup_historical_person_tags` is read-only dry-run by default.
  `--apply-relations` deletes mapped `ArchiveItem.tags` through rows and
  mapped `Document.tags_m2m` through rows in one `transaction.atomic()`,
  then `sync_archive_item_search_indexes` for ArchiveItems that lost a
  mapped Tag relation.
- Fail-closed before writes: map size must be 29; every mapped Person.id
  must exist; mapped Tag ids must be all present or all absent (partial
  Tag presence fails closed; all-absent is the D2b success state);
  every mapped `ArchiveItem.tags` relation must have its mapped
  `ArchiveItemPerson`; planned through rows must use mapped Tag ids only.
  Apply re-checks those invariants inside the same
  `transaction.atomic()`, locks planned through rows (`select_for_update`),
  and fail-closes if found through PKs or Django `.delete()` counts differ
  from the plan, or if any mapped through rows remain after each table's
  delete (unexpected PKs are not deleted or added to the plan).
  `Document.tags_m2m` leftovers do not require `ArchiveItemPerson`.
- Idempotent: after D1, dry-run and `--apply-relations` report `planned=0`
  and succeed. D1 never deletes Tag rows. After D2b, all 29 mapped Tag
  rows may already be absent; that is also `planned=0` success.
- `update_ocr_document_tags` and Django Admin `Document.tags_m2m` reject
  mapped Tag ids (choices omitted; form clean fail-closed). `TagAdmin`
  refuses delete of mapped Tag rows, including mixed bulk delete.
  Ordinary Tag delete is unchanged. No name denylist.

**Deferred (D2b, now implemented):** deleting the 29 mapped Tag rows.
See **Historical person-Tag row deletion (D2b)**. D2a retired-name
policy remains in force after those rows are gone. Direct
`?tag=<mapped_id>` stays Option 0 (matches nothing once relations are
gone).

**Tests:** `documents/test_cleanup_historical_person_tags.py`. No schema
or data migration.

## Public Tag browse/filter cutover (Person vs Tag, Stage B)

**Decision / implemented:** Mapped historical person-name Tag ids are no
longer public Tag browse/filter *choices*. `/archive/tags/<mapped_tag_id>/`
redirects (HTTP 302) to the public Person page
`/archive/people/<mapped_person_id>/`. Lookup is
`person_id_for_historical_person_name_tag(tag_id)` only (map-first, before
`Tag.objects.get`, so a future Tag-row delete still redirects). Ordinary
Tag browse, visibility, and authorized item querysets are unchanged.

**Current behavior:**

- `archive_tag_browse_page` resolves mapped Tag ids to Person ids, then
  `person_public_page_url`. Unmapped missing Tags remain 404.
  The Person page itself 404s when that Person has no authorized
  renderable `ArchiveItemPerson` items and no authorized renderable
  `PhotoPerson` appearances.
- `archive_advanced_filter_choice_context` excludes
  `historical_person_name_tag_ids()` from authorized public Tag choices
  only. Category/event/person choices are unchanged. Visibility scoping,
  `distinct()`, and name ordering remain.
- Direct legacy `?tag=` query strings are **not** rewritten, stripped,
  rejected, or merged. Mapped-only Tag filter, mixed mapped+ordinary Tag
  OR-within-tags, and Tag+Person AND-between-groups keep today’s
  semantics (Option 0). After D1 relation cleanup, crafted
  `?tag=<mapped_id>` matches nothing (unknown-id semantics). Leftover
  mapped Tag relations are removed by `cleanup_historical_person_tags
  --apply-relations`; Tag rows remain.

**Deferred:** D2a retired-name policy is implemented. D2b Tag-row deletion
is implemented (see **Historical person-Tag row deletion (D2b)**);
public alias display; PhotoPerson appearance filter; Person catalog;
query-string redirects (Option 1+). Public Person pages and Person
filter-chip UX are implemented (see **Public Person page** and
**Public Person active-filter chip UX**). PhotoPerson name linking on
public PHOTO detail is implemented (see **Public PhotoPerson name
links**). Search-index cleanup of mapped Tag names is part of D1 apply
(rebuild for affected ArchiveItems).

**Tests:** `documents/test_archive_person_tag_stage_b.py`.

## Public presentation cutover (Person vs Tag, Stage A)

**Decision / implemented:** Public archive cards and detail present
item-level `ArchiveItemPerson` (`ArchiveItem.people`) under **אנשים
קשורים**. The 29 frozen historical person-name Tag ids are hidden from
public card `קשור ל־` and detail `תגיות:`. Identity is `Person.id`.
Person links go to `/archive/people/<Person.id>/`. Ordinary Tags, events,
categories, current ordering, visibility, and existing URLs are
unchanged. PhotoPerson / **אנשים מזוהים:** stay separate.

**Current behavior:**

- Shared helpers in `documents/services/archive_item_presentation.py`
  filter public discovery Tags with `historical_person_name_tag_ids()`
  only; build Person links from `ArchiveItem.people` ordered by
  `(name, id)`; never match or dedupe by name; never read PhotoPerson.
- Cards: mapped historical Tags are excluded from `related_links`; a
  separate `person_links` row renders **אנשים קשורים**.
- Detail: `archive_detail_page` and `document_detail_page` pass the
  shared filtered Tags and Person links into
  `discovery_metadata.html`. **תגיות:** renders only when ordinary Tags
  remain. A people-only discovery block is allowed.
- `people` is prefetched on the card queryset, homepage cards, archive
  detail, and OCR document detail.

**Deferred:** Stage B path redirect and public Tag-choice hiding are
implemented (see **Public Tag browse/filter cutover (Person vs Tag,
Stage B)**). D1 mapped relation cleanup is implemented (see
**Historical person-Tag relation cleanup (D1)**). D2b Tag-row deletion
is implemented (see **Historical person-Tag row deletion (D2b)**).
Still deferred: staff/suggestion UI beyond current write-path blocks;
PhotoPerson changes; migrations.
Public Person pages and Person filter-chip UX are implemented (see
**Public Person page** and **Public Person active-filter chip UX**).

**Tests:** `documents/test_archive_person_public_presentation.py`.

## Block reuse of historical person-name Tags; post-deploy reconciliation

**Decision / implemented:** The 29 frozen historical person-name Tag ids
must not be reused. Runtime helpers
`historical_person_name_tag_ids()` and
`is_historical_person_name_tag(tag_id)` derive the blocked set only from
`documents/historical_person_tag_map.py`. Membership is **Tag.id only**.
Names remain display/provenance and are never lookup keys.

**Observed post-0055 production drift:** Migration 0055 created 124
`ArchiveItemPerson` rows matching Tag relations at backfill time.
Production later has **125** historical Tag relations and still **124**
`ArchiveItemPerson` rows. Concrete drift: Tag **29** is on items **194**
and **300**; mapped Person **19** is only on item **194**. Hiding person
Tags before reconciliation would drop the person association on item
**300**.

**Current behavior:**

- Staff discovery Tag selectors omit blocked Tag ids.
- Staff create/edit and upload discovery parse reject posted blocked Tag
  ids (and free-text names that resolve to an existing blocked Tag.id).
  Ordinary ArchiveItem edits preserve existing blocked Tag relations by
  merging them back before replace-all `tags.set`. Unrelated valid
  taxonomy in the same tampered POST is not applied (fail closed).
- Public metadata-suggestion POST rejects tampered `selected_tags`
  containing blocked Tag ids. Suggestion approval/application never adds
  or removes blocked Tag relations; a suggestion whose suggested tag
  names resolve to a blocked Tag.id fails closed and writes no taxonomy.
- Explicit management command
  `reconcile_historical_person_tag_relations` is read-only dry-run by
  default. `--apply` creates missing `ArchiveItemPerson` rows only,
  through `create_archive_item_person` (search-index refresh preserved).
  Create-only, atomic, idempotent. No Tag/relation deletion, no
  PhotoPerson, no name/`get_or_create(name=...)` identity. No data
  migration.

**Revised safe order:** block reuse → deploy → dry-run/apply
reconciliation → Stage A public presentation cutover (implemented) →
Stage B tag browse redirect / public Tag-choice hiding (implemented) →
D1 mapped relation cleanup (implemented).

**Deferred:** Stage A public cards/detail cutover is implemented (see
**Public presentation cutover (Person vs Tag, Stage A)**). Stage B path
redirect and public Tag-choice hiding are implemented (see **Public Tag
browse/filter cutover (Person vs Tag, Stage B)**). Direct legacy `?tag=`
semantics are intentionally unchanged (Option 0). D1 relation cleanup is
implemented (see **Historical person-Tag relation cleanup (D1)**). D2a
retired-name policy is implemented. D2b Tag-row deletion is implemented
(see **Historical person-Tag row deletion (D2b)**).

**Tests:** `documents/test_historical_person_tag_reuse.py`,
`documents/test_reconcile_historical_person_tag_relations.py`, plus map
helper coverage in `documents/test_historical_person_tag_map.py`. No
schema or data migration.

## Frozen historical person-name Tag.id → Person.id map

**Decision / implemented:** Versioned code artifact
`documents/historical_person_tag_map.py` freezes the production
**Tag.id → Person.id** pairs created by migration
`0055_backfill_person_from_person_name_tags`, plus the exact historical
Tag names used by D2a retired-name policy. Lookup is
`person_id_for_historical_person_name_tag(tag_id)`; unknown Tag ids
return `None`. Runtime consumers now include reuse blocking, the
post-deploy reconciliation command, Stage A public presentation,
Stage B mapped-tag browse redirects / public Tag-choice hiding, D1
relation cleanup, mapped-Tag admin/OCR write-path blocks, D2a
retired-name write-path blocks, and D2b Tag-row deletion. Mapped Tag
rows may be absent after D2b; this module stays the identity and
retired-name artifact.

**Evidence (production):** 0055 started from 0 Person rows and created
all 29 Persons sequentially in `APPROVED_PERSON_NAME_TAGS` order.
Person ids are **1–29** in that creation order. Person and
`ArchiveItemPerson` timestamps are one uninterrupted 0055 sequence.

- 26 pairs also have exact current `ArchiveItem.tags` ↔
  `ArchiveItemPerson` item-set correspondence.
- Tags **2** and **34** share item set `[264]`, so item-sets alone are
  not a bijection. Creation order proves **2→1** and **34→24**.
- Tag **29** later gained a leftover Tag relation to item **300**; its
  0055 Person remains **29→19**. Production now has 125 historical Tag
  relations vs 124 `ArchiveItemPerson` rows (Person **19** on item **194**
  only).

**Current behavior:** Frozen Tag names are retired-name policy only (D2a);
they are not Person lookup keys. `Person.name` is not unique and is
**not** identity. Do not look up Person by name. Do not join or break
ties on names. Do not use PhotoPerson. Do not import migration `0055`
at runtime.

**Deferred:** Stage A public cards/detail cutover is implemented (see
**Public presentation cutover (Person vs Tag, Stage A)**). Stage B path
redirect and public Tag-choice hiding are implemented (see **Public Tag
browse/filter cutover (Person vs Tag, Stage B)**). D1 relation cleanup is
implemented (see **Historical person-Tag relation cleanup (D1)**). D2a
retired-name policy is implemented (see **Retired historical Person-Tag
names (D2a)**). D2b Tag-row deletion is implemented (see
**Historical person-Tag row deletion (D2b)**). Still deferred for this
module: schema mapping table.
Direct legacy `?tag=` query application is intentionally unchanged.

**Tests:** `documents/test_historical_person_tag_map.py`. No schema
migration.

## ArchiveItemPerson suggestion UI (C2b)

**Decision / implemented:** C2 is complete only when this C2b UI lands
on top of C2a. The existing public metadata-suggestion page
(`/archive/<item_id>/metadata-suggestions/new/`) can submit Person
relationship deltas (ADD/REMOVE existing `Person.id` values). Staff have
a dedicated Person-suggestion backlog (`הצעות שיוך אנשים`), not an
overload of the taxonomy metadata-suggestion table. Identity remains
`Person.id`. There is no new-Person proposal field. PhotoPerson remains
a separate appearance relation. Historical person-name Tags are
unchanged on this form.

**Current behavior:**

- Public ADD picker universe = Persons linked via ArchiveItemPerson to
  at least one ArchiveItem the current user may view. PhotoPerson-only
  identities are excluded. Inaccessible names are not leaked.
  Unauthorized/invalid Person ids fail with the generic not-found error.
- Current people / REMOVE lists ArchiveItemPerson rows on this item
  only. Public submit never mutates ArchiveItemPerson, PhotoPerson,
  Person, aliases, or Tags.
- One POST may include taxonomy fields plus zero or more Person ADD and
  REMOVE actions. People-only is valid. At most one
  `ArchiveMetadataSuggestion` is created when taxonomy/note payload
  requires it; one `ArchiveItemPersonSuggestion` row per Person action.
  Independently reviewable. Honeypot still yields the thanks page with
  no rows of either type. Person-action validation failures roll back
  the whole submit grouping.
- Staff backlog is visibility-scoped via
  `archive_item_person_suggestions_queryset_for_user`. Pending first,
  then newest first. Approve/reject load through that queryset before
  calling C2a `approve_suggestion` / `reject_suggestion`. Unauthorized
  restricted items 404 with no service mutation. Stale ADD/REMOVE
  approval surfaces the Hebrew no-op messages; already-reviewed uses
  the existing C2a error. PRG back to the backlog.
- PHOTO form copy is item-level related-to, not photo appearance.
  Approve ADD/REMOVE writes ArchiveItemPerson only.

**Why:** C2a defined the delta contract; C2b is the public/staff HTML
for that contract. Public cards/detail cutover is Stage A (implemented
separately).

**Deferred:** Stage A public cards/detail cutover is implemented (see
**Public presentation cutover (Person vs Tag, Stage A)**). Stage B path
redirect and public Tag-choice hiding are implemented (see **Public Tag
browse/filter cutover (Person vs Tag, Stage B)**). Still deferred:
destructive cleanup; new-Person / alias / merge proposals. Person pages
and Person filter-chip UX are implemented (see **Public Person page**
and **Public Person active-filter chip UX**). Direct legacy `?tag=`
semantics are intentionally unchanged. Reuse of the 29 historical
person-name Tags is blocked separately (see **Block reuse of historical
person-name Tags; post-deploy reconciliation**).

**Tests:** `documents/test_archive_item_person_suggestion_ui.py` plus
restricted-visibility and visibility-metadata UI extensions. No new
schema migration.


## ArchiveItemPerson suggestions foundation (C2a)

**Decision / implemented:** C2 suggestions are **explicit relationship
deltas**, not a desired set of Person ids. One
**`ArchiveItemPersonSuggestion`** row is exactly one proposed action:
**ADD** Person X to ArchiveItem Y, or **REMOVE** Person X from
ArchiveItem Y. Identity is **`Person.id`**. Approval is idempotent:
stale ADD/REMOVE still becomes **APPROVED** as a no-op. New-Person /
alias / merge proposals are out of scope. C2a is the model + services
foundation; public/staff HTML is C2b.

**Current behavior:**

- Model fields follow `ArchiveMetadataSuggestion` lifecycle naming
  (`submitter_name` / `submitter_email` / `submitter_note` /
  `submitter_user`, `status` PENDING/APPROVED/REJECTED, `created_at`,
  `reviewed_at`, `reviewed_by`). Ordering is newest first.
- Global pending uniqueness: unique `(archive_item, person, action)`
  **WHERE status=PENDING**. Historical APPROVED/REJECTED rows do not
  block a later pending row. No STALE status.
- Submission (`submit_archive_item_person_suggestion`) creates one
  PENDING row only. It does not write ArchiveItemPerson, PhotoPerson,
  Person, PersonAlias, Tag, `Document.tags_m2m`, or the search index.
  Person must be in the caller-supplied authorized Person universe.
  ADD requires the person is not already linked; REMOVE requires a
  current ArchiveItemPerson link. Duplicate pending is a domain error.
  A racing `IntegrityError` on the pending unique constraint is
  translated to that same domain error.
- Review (`approve_suggestion` / `reject_suggestion`) uses
  `transaction.atomic` + `select_for_update`. Already-reviewed rows
  raise the Hebrew “already reviewed” error. Reject never mutates
  ArchiveItemPerson. Approve applies **one delta**:
  - ADD: `create_archive_item_person` if absent; otherwise APPROVED no-op
  - REMOVE: load the exact ArchiveItemPerson and
    `delete_archive_item_person` if present; otherwise APPROVED no-op
  Unrelated ArchiveItemPerson rows are untouched. Review never calls
  `set_archive_item_people` and never reconstructs a Person set.
  Example: live `[A, B]`, pending REMOVE B, staff later adds D →
  approve yields `[A, D]`.
- Search index: real ADD/REMOVE go through the existing people writers
  (they own q-index refresh). Stale no-op does not rebuild.
- PHOTO: ArchiveItemPerson remains item-level related-to. C2a never
  reads/writes/infers PhotoPerson. The same Person may keep a
  PhotoPerson row after item-level REMOVE. Person is not deleted.
- Authorization is layered: domain submit/apply services do not bake
  public request visibility into the apply path. C2b supplies the
  authorized Person universe on submit and authorizes the suggestion/item
  before mutate. Helper: `archive_item_person_suggestions_queryset_for_user`.
- Historical person-name Tags are unchanged.

**Why:** A desired-set replacement on approve would delete unrelated
people added after submit time. Person.name is not identity. Extending
`suggested_tags` would mix free-text taxonomy with Person ids.

**Deferred:** C2b public/staff HTML landed (see **ArchiveItemPerson
suggestion UI (C2b)**). C2 is complete only when C2b has landed. Still
deferred: new-Person / alias / merge proposals; public presentation of
ArchiveItemPerson; hide/remove person-name Tags; public Person page;
Tag-id → Person-id cleanup mapping; tag redirects; prevent reuse;
destructive cleanup.

**Tests:** `documents/test_archive_item_person_suggestions.py`.
Schema migration for `ArchiveItemPersonSuggestion` (after 0055).

## ArchiveItemPerson staff UI (C1)

**Decision / implemented:** Production staff ArchiveItem **create and edit**
can create and delete item-level **`ArchiveItemPerson`** links for every
item type (OCR_DOCUMENT / MANUAL_TEXT / VIDEO / PHOTO). This is the
missing write path for the identity model that public `q` and advanced
`person=<id>` already read.

**Current behavior:**

- Shared people section on the existing type-specific ArchiveItem
  **create** and **edit** forms. Staff Person index for find/open is
  `/archive/manage/people/` (not a public catalog and not a create/merge
  page). No user-submitted
  Person suggestions (C2).
- Create surfaces:
  - MANUAL_TEXT: `/archive/manage/new/?item_type=manual_text` and legacy
    `/archive/manage/new/manual-text/`
  - VIDEO: `/archive/manage/new/?item_type=video`
  - OCR_DOCUMENT: `/archive/manage/new/?item_type=ocr_document` (and the
    standalone upload page) via `/api/uploads/create/`
  - PHOTO: `/archive/manage/new/?item_type=photo` via `/api/photo-uploads/create/`
  Edit remains `/archive/manage/<id>/edit/`.
- PHOTO uses heading **אנשים קשורים לפריט** plus a staff hint that this
  is not photo appearance. Photo-level **`PhotoPerson`** remains on
  `/archive/manage/<item_id>/photos/<photo_id>/edit/` (**אנשים מזוהים
  בתמונה**). The two forms use distinct field names
  (`archive_item_person_ids` vs `person_ids`). PHOTO create writes
  **`ArchiveItemPerson` only**; it does not write **`PhotoPerson`**.
  Adding a photo to an existing item does not collect item-level people.
- Picker: option value is **`Person.id`**; visible label is canonical
  **`Person.name`**, plus aliases in `(name, id)` order when present.
  Aliases are not selectable entities. Person ids are not display labels.
  Persons ordered by `(name, id)` with alias prefetch (no N+1).
- New Person: canonical **`Person.name`** only via
  **`create_identified_person`** (trim; no aliases; no
  `get_or_create`). Exact canonical/alias matches warn and require
  per-token **`force_create_person`** (see **Staff new-Person duplicate
  prevention**). Duplicate names remain distinct identities when
  confirmed.
  **`new_archive_item_person_name`** may be comma-separated; each token
  that is clear or force-approved creates a new Person and
  **`ArchiveItemPerson`** only (see
  **Comma-separated new Person names**).
- Writes go through **`set_archive_item_people`** in
  `documents/services/archive_item_people.py` (diff current vs submitted
  ids, create/delete through rows, one in-transaction search-index
  refresh). Create and edit share the same parse helpers and people
  partial. Staff form save applies people first with
  `refresh_search_index=False`, then existing metadata/discovery updates
  refresh the same item so one submit does not rebuild once per
  add/remove. Create applies people inside the successful create
  transaction before the last existing item-level search sync (VIDEO
  uses one extra item sync after `create_video_archive_item` because
  that service already synced). Single-row
  **`create_archive_item_person`** / **`delete_archive_item_person`**
  remain. No signals, no `on_commit`. No per-Person rebuilds.
- Does not create/delete **`PhotoPerson`**, aliases, Tags, or
  **`Document.tags_m2m`**. Same person may exist in both
  ArchiveItemPerson and PhotoPerson; neither is inferred from the other.
- Permissions match existing archive-manage create/edit: anonymous → login
  redirect; authenticated non-admin → 403; staff → allowed. Invalid Person
  ids are rejected in Hebrew and preserve submitted people/new-name state
  on HTML create/edit; JSON OCR/PHOTO create returns 400 and does not
  create the item.

**Why:** Live Person / ArchiveItemPerson rows and public `q` / `person=`
filter already exist, but nothing in production staff UI wrote
ArchiveItemPerson. Historical person-name Tags cannot be hidden until
that write path exists. Create and edit are both required staff
workflows for the Person-vs-Tag migration.

**Deferred:** **C2 — user suggestion support for ArchiveItemPerson.**
C2a (model + submit/apply services) and C2b (public form + staff backlog
HTML) are implemented separately; see those entries. C1 itself does not
include user suggestions.
Also still deferred: public presentation of ArchiveItemPerson;
hide/remove person-name Tags; redirect old Tag browse URLs; prevent
person-Tag reuse; destructive Tag cleanup; public Person page; identity
merge.

**Tests:** `documents/test_archive_item_person_staff_ui.py`. No schema
migration.

## READY unverified OCR documents may be intentionally reprocessed

**Decision / implemented:** Staff/command OCR reprocess may include documents
whose `processing_state_user` is **`READY`**, provided every other existing
safety guard still passes.

**Current behavior:** `_processing_state_allows_ocr_reprocess` accepts
**`FAILED`**, recoverable **`PARTIAL`** (unchanged), and **`READY`**. READY
then continues through the existing validation chain: **`UPLOADED`**,
**`OCR_DOCUMENT`**, and the authoritative **`VERIFIED`**
`DocumentTextResult` guard. If any text result is **`VERIFIED`**, reprocess
is still blocked.

**Why:** `READY` means expected outputs are usable/displayable, not
human-verified ground truth. Automatic OCR can leave a document READY with
only `NEEDS_REVIEW` / `UNVERIFIED` rows. Blocking READY categorically
prevented an intentional staff rerun through the normal
`PROCESS_DOCUMENT` path.

**Unchanged:** FAILED eligibility; recoverable PARTIAL eligibility;
ineligible PARTIAL cases (including usable source text / translation-only
PARTIAL); non-`UPLOADED`; non-`OCR_DOCUMENT`; Transkribus retry
classification; worker / routing / Request semantics. Non-Transkribus
routes remain `NORMAL_REENQUEUE`.

**Not implied:** READY is not “always safe to overwrite.” VERIFIED remains
the overwrite protection.

**Tests:** `documents/test_ocr_reprocess.py`,
`documents/test_ocr_reprocess_ui.py`. No migration.

## Public `/archive/` Person advanced filter

**Superseded in part:** Filter membership is now ArchiveItemPerson **or**
renderable PhotoPerson (see **Public `/archive/` Person advanced filter —
unified AIP ∪ PhotoPerson**). Param shape, OR/AND composition, chips, and
choice-loading gates below remain current.

**Decision / implemented:** Public advanced search on `/archive/` gains a
repeatable **`person=<person_id>`** structured filter. It originally meant
**show ArchiveItems generally related to this Person** via
**`ArchiveItemPerson`** (`ArchiveItem.people`) only.

**Current behavior:**

- GET param: repeatable **`person`** (Person primary key). Same naming family
  as `category` / `event` / `tag`. Never `Person.name` or alias text as URL
  identity.
- Normalization matches other relation ids: positive integers; malformed /
  non-integer / `0` / negative values skipped; first-occurrence order
  preserved; duplicates dropped. Names and aliases are not reinterpreted as
  ids. Unknown-but-well-formed ids stay in the filter tuple and match nothing
  (same as unknown category/event/tag ids).
- ORM: correlated **`Exists`** on **`ArchiveItemPerson`** **or** renderable
  **`PhotoPerson`** (see **Public `/archive/` Person advanced filter —
  unified AIP ∪ PhotoPerson**). Inner subqueries do not copy the authorized
  browse queryset. **`people_present` is not consulted.** AIP is not inferred
  from PhotoPerson.
- OR within the Person group; AND with author / category / event / tag / year
  groups. Visibility remains `archive_browse_queryset_for_user` before filters.
- Applies to VIDEO / OCR_DOCUMENT / MANUAL_TEXT / PHOTO. Results remain
  ArchiveItems. No Person cards on the list. Public Person detail is
  `/archive/people/<id>/` (see **Public Person page**); this filter does
  not deep-link to it. Active Person chips link the name to that page
  without changing filter semantics.
- Choices: `Person.objects.filter(person_public_membership_q_for_item_pks(authorized pks)).order_by("name", "id")`
  when advanced choice context is needed (panel open or any advanced
  filter active). Canonical **`Person.name`** is the visible label; option
  value is the id. Aliases are not separate options and are not prefetched.
  PhotoPerson-only people appear when the parent item is authorized and the
  photo is renderable. Ordinary `/archive/` and q-only requests still skip all
  choice-context queries (now 5 when loaded: author + category + event + tag +
  person).
- Active chips: **one chip per selected Person id** (canonical
  `Person.name`). Name → Person page; × removes that id only via
  `archive_advanced_filters_without_person` and always keeps
  `advanced=1`. Other filter chips remain grouped. See **Public Person
  active-filter chip UX**.
- Historical person-name **Tags remain** and still power tag filter,
  `/archive/tags/<id>/`, tag chips, and tag `q` indexing. An item may match
  both a person Tag and the new Person filter during transition.

**Why:** Production has live `Person` / `ArchiveItemPerson` rows and `q`
already indexes those identities. Structured filtering was the remaining
public discovery gap. PhotoPerson stays the photo-appearance relation.

**Deferred:** public alias display; alias-assisted picker search (client
filter matches canonical option text only); photo-level PhotoPerson
appearance cards/filter on `/archive/`; D2b Tag-row deletion is implemented
(see **Historical person-Tag row deletion (D2b)**); legacy
`Document.tags_m2m` cleanup; identity merge/dedupe; fuzzy matching; AI
identification. Public Person browse/detail and Person filter-chip UX
are implemented (see **Public Person page** and **Public Person
active-filter chip UX**).

**Tests:** `documents/test_archive_advanced_search_person.py` plus updated
advanced-search backend/UI regressions. No schema migration.

## ArchiveItemPerson public q search

**Decision / implemented:** Public `/archive/?q=` indexes item-level
**`ArchiveItemPerson`** identities onto the owning
**`ArchiveItemSearchIndex.metadata_text`**. A Person linked through
`ArchiveItemPerson` is discoverable by canonical **`Person.name`** and
**`PersonAlias.name`**. This is **not** photo-appearance search.

**Current behavior:**

- Indexed sources are **`Person.name`** then **`PersonAlias.name`** for
  distinct Persons on `archive_item.people`, ordered like PhotoPerson
  identities: Persons by `(name, id)`; canonical names in that order; then
  aliases in that same person order, aliases by `(name, id)`.
- Applies to **every item type** (VIDEO / OCR_DOCUMENT / MANUAL_TEXT /
  PHOTO). It does **not** depend on PhotoContent renderability, does **not**
  attach to selected-photo metadata, and does **not** create `?photo=`
  deep-links or public Person pages.
- One ArchiveItem result. Outer first-occurrence segment dedupe drops
  repeated normalized fragments across tags, `ArchiveItemPerson`,
  PhotoPerson, and aliases.
- Visibility stays query-time (`archive_browse_queryset_for_user`). The
  builder does not special-case private/restricted items.
- Snippets: an `ArchiveItemPerson`-only hit with no matching ArchiveItem
  scalar/M2M field uses the existing generic **`נמצא בפרטי הפריט`** label
  (same as PhotoContent/PhotoPerson metadata-only hits). No Person-specific
  public UI.
- Historical person-name **Tags remain**. Tag `q` indexing,
  `/archive/tags/<id>/`, and advanced tag-id filters are unchanged.
- Refresh is the existing explicit in-transaction contract (no signals, no
  `on_commit`). `create_archive_item_person` /
  `delete_archive_item_person` rebuild that one item.
  `update_person_name` and alias create/edit/delete fan out through
  `archive_item_ids_for_person_search_refresh` (union of ArchiveItemPerson
  item links and PhotoPerson appearances; one rebuild per ArchiveItem even
  when both relations exist).
- Raw `ArchiveItemPerson.objects.create()` / `.delete()` are **not** hooked.
- **No schema migration.** Migration **`0055`** already created production
  `ArchiveItemPerson` rows **before** this index change, so deploy **must**
  run **`backfill_archive_search_index`**. Do not modify `0055`.

**Structured Person filter:** implemented in **Public `/archive/` Person
advanced filter — unified AIP ∪ PhotoPerson** (`person=<id>`, AIP or
renderable PhotoPerson). Public Person browse/detail is implemented (see
**Public Person page**).

**Why:** Production now has 29 Person rows and 124 ArchiveItemPerson links
copied from historical person-name Tags. Those identities were not in `q`
until this change. PhotoPerson remains the only “appears in this photo”
relation.

**Deferred:** eventual person-Tag removal
(ordinary Tags still power unmapped tag browse/filter; mapped Tag path
browse redirects to the Person page, while raw `?tag=` is intentionally preserved); legacy
`Document.tags_m2m` cleanup; staff PHOTO appearance review / PhotoPerson
backfill; identity cleanup/aliases where needed. Structured Person
**filter** is implemented separately (see **Public `/archive/` Person
advanced filter**). Public Person pages are implemented separately (see
**Public Person page**).

**Tests:** `documents/test_archive_search_archive_item_person.py` (plus
updated PhotoPerson/alias/backfill regressions).

## Historical person-name Tags → Person + ArchiveItemPerson backfill

**Decision / implemented:** One-time, fail-closed Django data migration
`0055_backfill_person_from_person_name_tags` copies 29 approved historical
person-name **Tags** into **`Person`** rows and item-level
**`ArchiveItemPerson`** links. A person-name Tag is an **ArchiveItem-level**
association, not proof that the person appears in a specific photo.

**Current behavior:**

- Frozen source identity is the **approved Tag id**, not `Person.name`.
  Canonical `Person.name` is the exact stored `Tag.name` string. Comparison
  is exact stored-string equality only (no strip, casefold, transliteration,
  fuzzy matching, or alias inference).
- Approved Tag ids: 2, 4, 5, 7, 8, 10, 11, 14, 15, 16, 19, 20, 23–39
  (29 Tags). `ArchiveItemPerson` is created only from current
  `ArchiveItem.tags` relations for those ids (audited production: 124
  links). Production at audit time had 0 Person / PersonAlias / PhotoPerson /
  ArchiveItemPerson rows and 0 reuse/ambiguous Person matches.
- If **none** of the 29 Tag ids exist (empty test DB / new environment),
  the migration is a no-op so `migrate` can succeed. If **any** of those
  ids exist, missing ids, Tag-name mismatches, or more than one
  already-linked same-name Person on currently tagged items fail
  **before writes**.
- Intended Person identity on retry is the unique Person already linked via
  `ArchiveItemPerson` to those tagged items with the exact Tag name. A
  same-name Person that is **not** linked through those items is a different
  identity and is not reused (`Person.name` is not unique; do not
  `get_or_create(name=...)`). Existing intended `(archive_item, person)`
  rows are left in place (`get_or_create` on that unique pair).
- **Not written:** `PhotoPerson`, `PersonAlias`, Tag rows, `ArchiveItem.tags`,
  `Document.tags_m2m`, search-index rebuilds. `people_present` is untouched.
  Private/restricted/public items all receive item-level links; visibility
  does not change the association.
- Old person-name Tags **remain**. Public `q` still indexes `ArchiveItem.tags`;
  `/archive/tags/<id>/` and advanced tag filters still use Tag ids.
  At the time of this backfill, `ArchiveItemPerson` was **not** public-search
  indexed and `backfill_archive_search_index` was **not** required merely
  because these rows were added. That search gap is superseded by
  **ArchiveItemPerson public q search**; deploy of that later change **does**
  require `backfill_archive_search_index` because `0055` predated the index
  contract.
- Reverse is **`RunPython.noop`**. There is no persistent Tag→Person mapping
  table, and `Person.name` is not unique, so reverse cannot safely delete
  Person rows that may have gained later PhotoPerson / alias / extra
  ArchiveItemPerson links. An irreversible data migration is safer than a
  destructive reverse. Django applies the migration once; the write phase is
  atomic so a failed attempt does not commit a partial set.

**Why a data migration, not a management command:** This is a one-time
historical copy with a frozen Tag-id mapping, matching existing `RunPython`
backfills (`0020`, `0014`, `0031`). Repeatable operational repair belongs in
commands (`backfill_archive_search_index`, thumbnail backfills). No permanent
mapping model was added.

**Deferred:** public Person browse/detail; eventual person-Tag removal (blocked on
search/browse still depending on Tag); legacy `Document.tags_m2m` cleanup (8
of these person Tags also exist there; out of scope here); staff PHOTO
appearance review / `PhotoPerson` creation (including cases such as PHOTO
ArchiveItem 224 / `people_present`); alias/identity cleanup. No Gemini→Tag
inference. Public `q` indexing of `ArchiveItemPerson` is implemented in
**ArchiveItemPerson public q search**. Structured Person **filter** is
implemented in **Public `/archive/` Person advanced filter**.

**Tests:** `documents/test_person_name_tag_backfill.py`.

## Web process must not import the Gemini SDK

**Decision / implemented:** Importing the normal Django URL/view stack must
not import `documents.services.gemini_engine` or `google.genai` merely
because Hebrew translation retry exists.

**Why:** After deploying `origin/main`
(`82c296ef607ca7d420efa15dd3dada400081c7ca`), public requests intermittently
returned HTTP 502. Gunicorn web workers timed out / were SIGKILL'd while
resolving `/api/ui/documents/<id>/`. The import chain was:

`documents.views` → `hebrew_translation_retry` → `gemini_engine` →
`google.genai` (plus a second eager path:
`hebrew_translation_retry` → `non_hebrew_hebrew_translation` →
`GeminiResult` from `gemini_engine`).

A separate views import also reached Gemini:

`views` → `transkribus_corrected_current_activation` →
`transkribus_corrected_current_sync` → `transkribus_engine` →
`htr_adapters.base.HtrResult` → `htr_adapters/__init__.py` → registry →
`GeminiAdapter` → `gemini_engine`. Package import of `HtrResult` must not
load the Gemini adapter.

Web ECS tasks are 256 CPU / 512 MiB with no swap. Loading the Gemini SDK
during ordinary page rendering is the architectural bug; raising memory or
Gunicorn timeout is not the first fix.

**Current behavior:**

- Staff UI eligibility (`is_hebrew_translation_retry_ui_eligible`),
  validation, and `enqueue_hebrew_translation_retry` remain importable from
  views without loading `gemini_engine`.
- `translate_text_to_hebrew_with_gemini` is a lazy wrapper in
  `hebrew_translation_retry` so the worker execution path still calls Gemini
  and existing tests can patch
  `documents.services.hebrew_translation_retry.translate_text_to_hebrew_with_gemini`.
- `non_hebrew_hebrew_translation` imports `GeminiResult` only under
  `TYPE_CHECKING`. Persistence remains duck-typed at runtime.
- Worker `run_worker.py` still eagerly imports `gemini_engine` for ordinary
  non-Hebrew OCR→Hebrew translation. The HTR adapter registry still eagerly
  imports `GeminiAdapter` → `gemini_engine` when OCR dispatch actually
  resolves an adapter. `htr_adapters/__init__.py` no longer imports the
  registry at package import time, so `from htr_adapters.base import
  HtrResult` (used by Transkribus web/staff sync) does not load Gemini.

**Deferred:** ECS web memory / Gunicorn `--timeout` / worker-count changes.
This import fix should be redeployed and observed in production before
concluding whether a resource change is still needed. The 502 incident is
not claimed solved until that validation.

**Tests:** `documents/test_web_gemini_import_isolation.py`.

## Person staff alias UI (PR6b)

**Decision / implemented:** Staff-only UX for managing one existing Person's
canonical name and aliases, plus alias-aware labels on the PHOTO per-photo
Person picker. A GET-only staff index at `/archive/manage/people/` finds
existing rows. No Django Admin registration, no public Person catalog, and
no public alias display.

**Current behavior:**

- Per-photo staff edit picker labels use canonical `Person.name` first, then
  aliases in `(name, id)` order: `Canonical Name (Alias 1, Alias 2)`. People
  with no aliases show the canonical name only. Option values remain Person
  ids. Person ids are not shown in the visible label. The picker queryset
  prefetches aliases so loading does not N+1.
- Selected identified Persons (the current form selection) get an explicit
  **עריכת אדם** link to the focused Person page. Unselected Persons in the
  picker do not. Native `<select>` options are not turned into links.
- Focused staff route: `/archive/manage/people/<person_id>/edit/`
  (`archive-manage-person-edit`). Alias edit/delete:
  `/archive/manage/people/<person_id>/aliases/<alias_id>/edit/` and
  `.../delete/`. Access matches other archive-manage pages:
  `@login_required` + `_require_admin_page` (staff/superuser). Anonymous users
  are redirected to login; other authenticated users get 403; missing Person
  or mismatched alias is 404. Staff can list existing people at
  `/archive/manage/people/` (`archive-manage-people`); that index is GET-only
  find/open, not create/merge/delete.
- The Person page edits canonical `Person.name` and can add/edit/delete
  aliases through the existing PR6a services (`update_person_name`,
  `create_person_alias`, `update_person_alias`, `delete_person_alias`). Views
  parse the request and show service errors; they do not duplicate
  validation. Success uses PRG + Django messages. Delete is POST-only after a
  confirmation page. Alias CRUD is global to the Person, not scoped to the
  current photo.
- `new_person_name` on photo edit (and now add-photo; see **PHOTO add-photo
  identified people**) remains name-only Person create with no automatic
  aliases. The field may be comma-separated; each token creates a new
  Person and **`PhotoPerson`** only (see **Comma-separated new Person
  names**). The Person edit link appears on the next photo-edit visit
  after that Person is selected.
- Canonical rename may equal an existing alias (PR6a). The UI does not
  delete or promote the matching alias; both can appear.
- Public PHOTO detail still shows canonical `Person.name` only. Search
  semantics are unchanged; index refresh still happens inside the PR6a
  write services.

**Migration:** none.

**Deferred:** Person Admin; identity
merge/deduplication; Tag → Person migration; public Person catalog; public
alias display; alias kind/type; language/script metadata; fuzzy matching; AI
identification. The staff Person index is in **Staff Person index**.

**Tests:** `documents/test_person_staff_ui.py`.

## Person aliases (PR6a)

**Decision / implemented:** Add `PersonAlias` as alternate lookup/search names
for an existing `Person`. `Person.name` remains the canonical display name.
Aliases do not replace that name, do not merge Person rows, and are not shown
on public PHOTO pages.

**Current behavior:**

- **`PersonAlias`** belongs to **`Person`** (`FK`, `related_name="aliases"`,
  `on_delete=CASCADE`). Fields: `name` (`CharField(max_length=255)`),
  `created_at`, `updated_at`. Ordering: `["name", "id"]`. Unique on
  **`(person, name)`** only — two identities may share the same alias string;
  the same Person may not store the exact same alias twice. Alias `name` is
  **not** globally unique. No backfill; existing Person rows with zero aliases
  remain valid.
- Stored alias text preserves case and Unicode exactly after
  **leading/trailing `.strip()` only**. No casefold, transliteration, Unicode
  normalization, fuzzy matching, AI, or interior-whitespace collapsing at
  write time.
- Write services in `photo_content_management.py`: `create_person_alias`,
  `update_person_alias`, `delete_person_alias`. They strip, reject empty,
  reject `>255`, and reject an alias that is exactly equal to that Person's
  canonical `name` after strip. Duplicate `(person, name)` is left to the DB
  uniqueness constraint and converted to `PhotoContentManagementError`.
  Services run in `transaction.atomic`, do not rewrite `Person.name`, and do
  not touch `PhotoPerson` / `ArchiveItemPerson` / Tags.
- Search: for each distinct Person attached to at least one
  **public-renderable** `PhotoContent` via `PhotoPerson`, canonical
  `Person.name` remains searchable, then all `PersonAlias.name` values for
  those Persons. Order: distinct Persons `(name, id)`; canonical names in that
  order; then aliases in that same person order, aliases by `(name, id)`.
  Outer segment dedupe still drops repeated fragments. `ArchiveItemPerson`
  identities were later added to the same `metadata_text` contract; see
  **ArchiveItemPerson public q search**. Result remains one ArchiveItem at
  `/archive/<id>/` (no Person result, no `?photo=` deep-link).
- Index refresh uses the existing explicit in-transaction contract: alias
  create/edit/delete fans out through
  `archive_item_ids_for_person_search_refresh` (distinct ArchiveItem ids via
  ArchiveItemPerson **and** PhotoPerson; one rebuild per item) then
  `sync_archive_item_search_indexes`. The original PR6a helper
  `archive_item_ids_for_person_photo_appearances` remains PhotoPerson-only.
  No Django signals and no `on_commit`. Search-index prefetch of aliases is
  confined to `archive_items_for_search_index_build`; public gallery/access
  prefetch stays canonical-only.
- `update_person_name` still renames only `Person.name` and rebuilds affected
  indexes (ArchiveItemPerson and PhotoPerson). It does **not** rewrite,
  delete, or promote aliases. If the
  new canonical name equals an existing alias, both rows may coexist; search
  dedupes the identical fragment. Alias-write validation still rejects
  creating/updating an alias to the current canonical name.
- There is **no production Person delete path**. Any future delete service
  must fan out search-index rebuilds **before** CASCADE removes `PhotoPerson`
  / `ArchiveItemPerson` links (and aliases).
- **`Person`** and **`PersonAlias`** remain unregistered in Django Admin.

**Migration:** `0054_personalias` — additive `CreateModel` + uniqueness
constraint; depends on `0053_photocontent_multi_photo_foundation`; no data
migration.

**Deferred (after PR6b):** public alias display; public Person pages; alias
kind/type; language/script metadata; Tag → Person migration; identity
merge/deduplication; Person catalog/Admin; fuzzy matching; AI identity
matching. Staff alias-management UI and alias-aware Person picker labels were
implemented in **Person staff alias UI (PR6b)**.

**Tests:** `documents/test_person_alias.py` (plus admin/public/search
regressions in `test_person.py`, `test_photo_public_gallery.py`,
`test_archive_search_photo_aggregation.py`).

## PHOTO search aggregation (PR5)

**Decision / implemented:** A PHOTO `ArchiveItem` is findable through descriptive
metadata on **public-renderable** `PhotoContent` rows, plus identified
`Person.name` values linked through `PhotoPerson` on those same rows. The
search result remains **one `ArchiveItem` row**. `PhotoContent` is never a
standalone hit.

**Current behavior:**

- Indexed PHOTO sources are appended to `ArchiveItemSearchIndex.metadata_text`
  (weight B, FTS + short-field `icontains`), after existing ArchiveItem
  discovery fields (author, source_title, categories, events, tags,
  `public_note`). PHOTO `body_text` and `hebrew_translation_text` stay empty.
- Per-photo text fields: `description`, `location`, `context`,
  `people_present`, `notes`. Empty/whitespace-only values are dropped with the
  existing `_normalize_segment` / `_join_segments` rules (no extra placeholder
  stripping).
- `people_present` stays free-text and is indexed separately from structured
  `Person.name`. The builder does not infer identities from `people_present`,
  create `ArchiveItemPerson` from `PhotoPerson`, or migrate person-name Tags.
- Identified names come only from `PhotoPerson → Person` on **renderable**
  photos. Distinct by Person id; order `(name, id)`. A Person attached to both
  a renderable and a non-renderable photo still appears once. A Person attached
  only to non-renderable photos for that item does not appear via PhotoPerson.
  Item-level `ArchiveItemPerson` search is a later, separate contract; see
  **ArchiveItemPerson public q search**. **Person aliases** on the
  renderable-PhotoPerson path are in **Person aliases (PR6a)**.
- Aggregation walks PhotoContent rows that pass `photo_is_archive_renderable`
  (same public-gallery helper: `UPLOADED` + non-empty `original_file_key`;
  used by `public_renderable_photo_contents`) in `(position, id)` order.
  `PENDING` / `FAILED` / empty-key rows do **not** contribute. Thumbnail
  presence is irrelevant. Repeated normalized fragments are kept once (first
  occurrence). Visibility stays query-time (`archive_browse_queryset_for_user`).
- Technical fields are not indexed: S3 keys, filenames, MIME, sizes, upload
  status/error, thumbnail keys, ids.
- **Per-photo dates are not indexed** (not as FTS text and not in `year` /
  `year_to` filters). ArchiveItem dates are also absent from FTS; structured
  year filters still use ArchiveItem `date_start` / `date_end` /
  `date_precision` only. Mixing photo-level dates into those filters would be
  ambiguous (umbrella vs component) and is deferred.
- Result URL remains `/archive/<id>/`. No matched-photo `?photo=` deep-link.
  Snippets stay item-level: photo-metadata hits use **`נמצא בפרטי הפריט`**
  when no more specific ArchiveItem metadata field matches; no per-photo
  excerpt UI.
- Refresh is the existing **in-transaction** `sync_archive_item_search_index`
  pattern (not Django signals, not `on_commit`). Child writers that change
  searchable PHOTO text call it once per logical ArchiveItem save:
  `update_photo_content_metadata`, `delete_one_photo_content`,
  `reorder_photo_contents` (order is part of the derived text),
  `create_additional_photo_upload_plan` (pending row may exist; builder omits
  it), and create-new-item via the existing discovery sync after `PhotoContent`
  is inserted. Successful `finalize_photo_upload` (`PENDING` → `UPLOADED`)
  rebuilds the owning index in the same transaction as the status change so
  newly renderable metadata becomes searchable; failed / retryable finalize
  does not. `update_person_name` fans out through
  `archive_item_ids_for_person_photo_appearances` (distinct ArchiveItem ids via
  PhotoPerson; one rebuild per item); the builder decides whether the renamed
  Person actually contributes. Raw `Person.save()` / `QuerySet.update()` are
  not hooked. Thumbnail generation runs after finalize commit and does not
  rebuild. There is no supported write path that transitions a renderable photo
  back to non-renderable.
- Full `backfill_archive_search_index` rebuilds PHOTO rows with these rules.
  No schema migration (`ArchiveItemSearchIndex` remains derived).

**Deferred:** Tag → Person migration; public Person pages;
automatic `ArchiveItemPerson` from `PhotoPerson`; matched-photo deep-linking;
browse-card alternate preview; structured year filters from photo dates; AI
identification. Person aliases are implemented in **Person aliases (PR6a)**.

**Tests:** `documents/test_archive_search_photo_aggregation.py` (plus updated
builder isolation in `test_archive_search_index.py`).

## PHOTO public multi-photo gallery (PR4)


**Decision / implemented:** Public PHOTO detail presents **all publicly
renderable** `PhotoContent` rows for one PHOTO `ArchiveItem`. Browse cards,
browse eligibility, search aggregation, Tag → Person
migration, and staff management are **unchanged**. Person aliases are
searchable (PR6a) but still not shown on this public gallery.

**Current behavior:**

- The PHOTO `ArchiveItem` remains the umbrella public record. Canonical URL
  stays `/archive/<id>/`.
- Photo selection uses `?photo=<photo_content_id>` on that same route.
  Missing `photo` shows the first renderable photo. Invalid, non-integer,
  non-renderable, or **foreign** (another item’s) ids fall back to the first
  renderable photo (**200**, not 404) and never load another item’s bytes.
- Renderable means `photo_is_archive_renderable`: `upload_status=UPLOADED`
  and a non-empty `original_file_key`. `PENDING` / `FAILED` / empty-key rows
  are omitted from the gallery. Item-level access still requires the **first**
  photo (`position`, then `id`) to be renderable; that gate is unchanged.
- **N = 1** renderable photo keeps the previous simple detail layout: no
  Previous/Next, no “1 מתוך 1”, metadata in the header, full original via
  presigned GET.
- **N > 1:** selected original is shown prominently; Previous/Next are real
  links; compact thumbnail selectors use stored `thumbnail_file_key` when
  present (numbered fallback otherwise); status is “2 מתוך 5” among
  **visible** photos. Per-photo metadata sits with the gallery; shared
  ArchiveItem metadata (title, umbrella dates, visibility, public_note,
  categories, events, tags) remains once in the header.
- Identified people are `PhotoPerson → Person` canonical names for the
  **selected** photo only, ordered by `(name, id)`, linked to
  `/archive/people/<Person.id>/` (see **Public PhotoPerson name links**).
  `people_present` stays separate free text. No `ArchiveItemPerson`
  derivation. Person ids are not shown as metadata text.
- Per-photo dates use `format_document_date` and are omitted when unknown.
- Main display presigns the selected **original**. Selectors presign
  thumbnails only. No raw private S3 URLs. Missing thumbnails are not
  generated on page load.
- Detail prefetch: ``get_viewable_archive_item()`` loads ``photo_contents``
  (ordered by ``position``, ``id``) and nested ``people`` in one queryset
  contract. The gallery builder reads that cache and does not start a second
  ORM prefetch. No per-photo/per-person N+1.

**Deferred (PR5+ at the time of PR4):** search aggregation across photos
(**implemented in PHOTO search aggregation (PR5)**); browse-card
aggregation / choosing a non-primary preview; Person alias **display** /
public Person pages (alias schema + PHOTO search are implemented in
**Person aliases (PR6a)**); Tag → Person migration; automatic
`ArchiveItemPerson` from `PhotoPerson`; re-upload/retry of `FAILED` photos;
AI identification.

**Tests:** `documents/test_photo_public_gallery.py` (plus existing PHOTO
display/browse regressions).

## PHOTO staff multi-photo management (PR3)

**Decision / implemented:** Staff can manage **1..N** `PhotoContent` rows under
one PHOTO `ArchiveItem`. This PR does **not** add a public gallery, search
aggregation across photos, Person aliases, tag-to-Person migration, or
automatic `ArchiveItemPerson` derivation.

**Current behavior:**

- `/archive/manage/<id>/edit/` for PHOTO now edits **shared ArchiveItem
  metadata only** (title, visibility, umbrella dates, metadata_status,
  public_note, categories, events, tags). Per-photo descriptive fields are no
  longer written from this form.
- The same page lists all `PhotoContent` rows ordered by `(position, id)`,
  with compact filename/thumbnail/status, Edit, Delete, and (when N>1)
  Up/Down reorder. One-photo items stay compact (no reorder controls).
- **Add photo:** `/archive/manage/<id>/photos/add/` reuses the existing
  presigned PUT + complete pipeline. `POST /api/photo-uploads/add/` creates a
  pending `PhotoContent` on the existing item, allocates
  `max(position)+1` under an `ArchiveItem` `select_for_update` lock (does not
  use the model default `position=1`), then keys S3 as
  `photos/{photo_content_id}/original.{ext}`. Staff can also send
  `person_ids` / `new_person_name` on that request (see **PHOTO add-photo
  identified people**). Finalize remains
  `POST /api/photo-uploads/<id>/complete/`. No Document/SQS.
- **Edit photo:** `/archive/manage/<id>/photos/<photo_id>/edit/` updates one
  `PhotoContent` (description, location, context, people_present, notes,
  per-photo dates with the same parsing/precision helpers as ArchiveItem
  dates, and `PhotoPerson` links). `people_present` stays independent free
  text. Person selection is existing `Person` rows plus a **minimal**
  name-only create on that form. Changing `PhotoPerson` does **not** create
  `ArchiveItemPerson`.
- **Reorder:** POST of a full `photo_ids` permutation. Transaction locks the
  item + photo rows, shifts positions through a high temporary range, then
  writes contiguous `1..N`. Foreign/missing/duplicate ids are rejected.
- **Delete one photo:** allowed only when at least two photos remain. Deletes
  that `PhotoContent` (cascading `PhotoPerson`), renumbers remaining rows
  contiguously, and schedules existing best-effort S3 cleanup for original +
  thumbnail `on_commit`. Deleting the last photo is rejected with a staff
  error; whole-item delete remains the way to remove a PHOTO item and still
  cleans **all** related S3 objects (PR2).
- Public browse/detail and search were **unchanged in PR3**: they still used
  `primary_photo_content` / first-photo browse eligibility. Public detail
  gallery is implemented in **PHOTO public multi-photo gallery (PR4)**;
  browse-card eligibility remains the first photo.

**Deferred (remaining after PR4):** search aggregation across photo rows
(**implemented in PHOTO search aggregation (PR5)**);
using non-primary photos for browse thumbnails; Person aliases / Person
administration beyond this minimal create; Tag → Person migration;
automatic ArchiveItemPerson from PhotoPerson; OCR/HTR or AI identification
on photos; drag-and-drop reorder; re-upload/retry after `FAILED`.

**Tests:** `documents/test_photo_multi_manage.py` (plus updated
`test_photo_manage_edit_delete.py` / `test_photo_manage_status_clarity.py`).

## PHOTO multi-photo data model (PR2)

**Decision / implemented:** Change `PhotoContent` from a 1:1 backing row to a
1:N component of a PHOTO `ArchiveItem`. This PR is schema/data-model only.
It does not add multi-photo upload UI, gallery/public presentation, staff
management UI, search aggregation, Person UI, or tag-to-person migration.

**Current behavior:**

- `ArchiveItem` remains the archival umbrella record. A PHOTO item may own
  **1..N** `PhotoContent` rows.
- `PhotoContent.archive_item` is a **`ForeignKey`** (`on_delete=CASCADE`,
  `related_name="photo_contents"`). The former OneToOne reverse
  `archive_item.photo_content` is **removed** (no compatibility property of
  that name). Transitional one-photo call sites use
  `ArchiveItem.primary_photo_content`, which returns the first row by
  `(position, id)` or `None`.
- `PhotoContent.position` is a 1-based integer. Existing rows are backfilled
  to **`position=1`**. New rows default to `1` for migration/create safety.
  Duplicate `(archive_item, position)` is rejected. Model ordering is
  `(archive_item, position, id)`. Upload sequencing for additional photos is
  **not** implemented here.
- Each `PhotoContent` has its own `date_start` / `date_end` /
  `date_precision`, reusing `ArchiveItem.DatePrecision` choices and the same
  stored-date validation (`date_end` must not precede `date_start`; invalid
  precision is rejected). Existing rows default to `UNKNOWN` with null
  bounds. This PR does **not** copy, aggregate, or overwrite `ArchiveItem`
  dates.
- Per-image descriptive fields stay on `PhotoContent` (`description`,
  `location`, `context`, `people_present`, `notes`, `PhotoPerson`).
  Item-level metadata stays on `ArchiveItem` (title, visibility,
  metadata_status, public_note, categories, events, tags, author_name,
  source_title).
- `PhotoPerson` still points at a specific `PhotoContent`.
  `ArchiveItemPerson` remains independent. No automatic sync.
- Existing one-photo presentation/upload/edit/delete paths keep working by
  using the primary (first) photo. Staff PHOTO delete schedules S3 cleanup
  for **every** related `PhotoContent` before cascade-deleting the
  `ArchiveItem`. Browse eligibility uses the **first** photo’s upload/key
  state, not “any photo uploaded.”

**Compatibility decision:** A `photo_content` property was **not** added.
It could not support `select_related("photo_content")` or
`photo_content__…` lookups, and would hide the 1:N relation. Call sites were
updated to `photo_contents` / `primary_photo_content`.

**Migration:** `0053_photocontent_multi_photo_foundation` — additive date
fields; `position` default 1 (backfills existing rows); OneToOne→FK;
unique `(archive_item, position)`; `position >= 1` check. No S3 key rewrite,
no tag/person backfill, no search-index rebuild.

**Deferred (later PRs):** Public gallery of N photos is implemented in
**PHOTO public multi-photo gallery (PR4)**. Search aggregation across photo
rows is implemented in **PHOTO search aggregation (PR5)**. Still deferred:
using non-primary photos for browse thumbnails. Staff add/edit/reorder/delete
of individual photos and per-image date forms are implemented in **PHOTO
staff multi-photo management (PR3)**.

**Tests:** `documents/test_photo_content.py` (plus existing PHOTO /
Person regression tests).

## Person identity foundation (data model only)

**Decision / implemented:** Add a minimal structured person-identity schema.
This PR is schema-only. It does not change search, advanced search, public UI,
`קשור ל־` presentation, tags, or `author_name`.

**Current behavior:**

- **`Person`** is one identified person in the archive. Fields: canonical
  display **`name`** (`CharField(max_length=255)`), **`created_at`**,
  **`updated_at`**. **`name` is not unique** — two identities may share a
  display name. No aliases, biography, dates, confidence, or roles.
  Aliases were added later in **Person aliases (PR6a)**; this PR remained
  schema-only for Person / join tables. Optional public **`biography`**
  was added later (see **Public Person biography**).
- **`ArchiveItemPerson`** is the explicit through row for
  **`ArchiveItem.people`**. Meaning: this person is generally related to this
  archival item. It does **not** mean the person appears in a photo or has a
  role. Unique on **`(archive_item, person)`**.
- **`PhotoPerson`** is the explicit through row for **`PhotoContent.people`**.
  Meaning: this identified person appears in this specific photo. Unique on
  **`(photo_content, person)`**. No identification confidence/status.
- The two relations are independent: a photo appearance does not imply an
  item-level relation, and vice versa.
- **`PhotoContent.people_present`** remains a free-text **`TextField`** for
  unidentified, partially identified, or uncertain descriptions. It is not
  migrated, parsed, or replaced.
- **`ArchiveItem.author_name`** and **`Tag`** are unchanged. Existing tags that
  contain person names are not migrated, deleted, classified, or rewritten.
- Join FKs use **`CASCADE`**. Through rows store **`created_at`** only (no
  mutable join payload yet).
- Existing **`ArchiveItemAdmin`** and **`PhotoContentAdmin`** explicitly
  **`exclude = ("people",)`** so the new M2M is not a field/widget on those
  pages. **`Person`**, **`ArchiveItemPerson`**, and **`PhotoPerson`** are not
  registered. This is admin-safety only, not Person management UI.

**Related names:**

- `ArchiveItem.people` / `Person.archive_items` (M2M through `ArchiveItemPerson`)
- `ArchiveItem.person_links` / `Person.archive_item_links` (through rows)
- `PhotoContent.people` / `Person.photo_contents` (M2M through `PhotoPerson`)
- `PhotoContent.person_links` / `Person.photo_links` (through rows)

**Unchanged / out of scope:** search index, advanced-search people filter,
public UI, Person management UI, Author model, tag-to-person classification.
Multi-photo was later implemented as a schema change in
**PHOTO multi-photo data model (PR2)** (`PhotoContent` is no longer OneToOne).

**Migration:** `0052_person_identity_foundation` — additive `Person`,
`ArchiveItemPerson`, `PhotoPerson` tables and uniqueness constraints; no
backfill.

**Tests:** `documents/test_person.py` (plus existing `test_photo_content.py`
regression).

## Public global nav/header archive search (PR3)

**Decision / implemented (this branch):** Add a compact global archive search form
to the shared public navigation (`partials/nav.html`). This completes the planned
PR1→PR3 public search chain.

**Current behavior:**

- Primary nav order: brand → `ארכיון` / `אודות` → compact archive search →
  flexible spacer → auth controls. Staff nav panel is unchanged and does not
  host a second search form.
- Form is HTML/CSS only (`method="get"`, `action` = `archive-list`, `name="q"`,
  `type="search"`, accessible visually-hidden label, normal submit button).
  No JavaScript, autocomplete suggestions, API, or new view/service.
- Header search is **q-only**. Submitting starts a **fresh** `/archive/?q=…`
  search and does **not** carry advanced filters, item type, pagination,
  `advanced=1`, or other current-page query state.
- The nav input stays **empty by default on all pages**, including `/archive/`
  when a `q` is active. The page’s main archive search field remains the
  authoritative display of the current query.
- Existing `/archive/` search semantics, ranking, advanced panel, and PR2 year
  validation / choice-context optimization are unchanged. No second search
  backend or duplicated normalize helper was introduced.

**Still deferred (separate future work, not part of this chain):** Hebrew
morphology, fuzzy OCR / pg_trgm, phrase search improvements, places authority,
Author model, `related` filter.

## Public `/archive/` advanced search UI (PR2)

**Decision / implemented (merged #410):** Add the public `/archive/` advanced-search
UX on top of the PR1 backend filter contract. Two mandatory closure items are
included: authoritative reverse/malformed year validation in the public UI, and
conditional loading of authorized advanced-filter choice context.
**PR3 completes the planned search chain with global nav/header q search**
(see entry above).

**Current behavior:**

- Default `/archive/` keeps page title `ארכיון`, a compact `q` field + `חיפוש`,
  visible item-type filter, and a `חיפוש מתקדם` link. The advanced panel is
  closed by default and is not a separate page.
- Panel open state uses GET `advanced=1` (UI-only; does not change filter
  semantics). Validation errors also force the panel open and preserve submitted
  year strings.
- Advanced fields: author (single authorized choice), multi category/event/tag
  from authorized choice context, year + optional year_to. No `קשור ל־` filter.
- Year validation is server-authoritative via
  `validate_archive_advanced_year_fields`. Reverse ranges and malformed years
  show Hebrew errors, force the advanced panel open, preserve submitted values,
  and suppress **all** result execution (`total_count=0`) until fixed — including
  when valid `q` / author / category / event / tag values were also submitted.
  They do **not** execute the PR1 defensive single-year fallback as a successful
  search. PR1 `normalize_archive_advanced_filters` fallback remains low-level
  safety only.
- After a valid filtered search, show result count, compact active-filter chips,
  `שינוי החיפוש המתקדם`, and `ניקוי הכול`. Compact `ניקוי החיפוש` clears `q`
  only while preserving advanced filters. Item-type / pagination / per-page
  continue to use `build_archive_public_list_query`.
- Choice-context optimization: `archive_advanced_filter_choice_context` runs only
  when the advanced panel is open or advanced filters are active. Ordinary
  `/archive/` and q-only requests skip the author/category/event/tag choice
  queries (regression-tested; before: 4 choice queries every request; after: 0
  on ordinary/q-only, 4 when panel/filters need them).

**Deferred (historical PR2 note):** ~~global navigation/header search~~ → **done
in PR3**. Still deferred: Hebrew morphology, fuzzy OCR / pg_trgm, phrase search,
places authority, Author model, `related` filter.

## Public `/archive/` advanced filters — backend contract (PR1)

**Decision / implemented (merged #408):** Add a structured
advanced-filter backend contract on the existing public `/archive/` list
pipeline. Collapsible advanced UI / chips / global header search were deferred
to later PRs; **PR2 implements the `/archive/` advanced UI + year validation +
choice-context loading optimization**; **PR3 implements global nav/header q
search and completes the planned PR1→PR3 chain** (see entries above).

**Current behavior:**

- Pipeline remains authoritative:
  authorized browse queryset → `item_type` → advanced filters → full-text `q`
  → count → paginate → browse cards/snippets.
- GET parameters: preserve `q`, `item_type`, `per_page`, `page`; add `author`
  (single exact `author_name`), repeatable `category` / `event` / `tag`
  (integer ids; OR within each group, AND across groups), optional `year` and
  `year_to` (inclusive calendar-year overlap on known archival dates).
- `year_to` without `year` is ignored by low-level normalize. Malformed `year`
  drops the date filter in normalize. Reverse ranges (`year_to < year`) fall
  back to a single-year window on `year` in normalize (same defensive fallback
  as malformed `year_to`; no silent swap). **Public UI validation (PR2) rejects
  reverse/malformed years before relying on that fallback for search.**
  `UNKNOWN` / missing date bounds never match while a date filter is active.
- No `related` filter (presentation-only concatenation of events+tags). No new
  Author model. Choice context for author/category/event/tag is derived only
  from the caller’s authorized archive universe.
- Query construction extends `build_archive_public_list_query` so advanced
  filters survive type links, pagination, and per-page forms.

**Deferred (historical PR1 note):** ~~global nav search (PR3)~~ → **done in
PR3**. Still deferred: Hebrew morphology, fuzzy OCR / pg_trgm, places authority.

## Public OCR detail — archive-search match ↔ transcription sync

**Decision / #398 merged/deployed; bidirectional click follow-up is this branch
(not merged/deployed yet):**
Follow-up to merged/deployed P1 search overlays + sticky nav. Previous / next
activates an archive-search match on the OCR document detail page with the
existing source-image active overlay behavior, and the displayed transcription
also scrolls to a server-authored target for that same match index when one
exists. A narrow follow-up on
`feature/search-match-bidirectional-click-sync` makes click activation
bidirectional (source overlay ↔ transcription match) with explicit one-sided
scroll intent.

**Current behavior (#398 deployed):**

- Transcription targets reuse the exact `ArchiveSearchGeometryMatch` enumerate
  indexes already used for `data-archive-search-match-index` on source-image
  overlays. The browser does **not** re-search the query string in displayed
  text (no `indexOf`, regex reconstruction, or fuzzy DOM matching).
- A transcription target is emitted only when the geometry match belongs to the
  exact `DocumentTextResult` selected by
  `resolve_displayed_transcription_result`. Matches on a non-displayed indexed
  surface (e.g. separate Hebrew translation while source transcription is shown)
  keep image navigation and omit transcription sync (fail closed).
- Invalid / out-of-range / overlapping canonical offsets fail closed for that
  match index only. Visible transcription text remains character-for-character
  identical to `DocumentTextResult.text`; Django autoescaping stays intact.
- Presentation builds a single segment list that can carry both
  `data-text-line-hover-id` and
  `data-archive-search-transcription-match-index` so hover markup and search
  anchors coexist without a second independent reconstruction of the same text.
- Active transcription highlight uses
  `.archive-search-transcription-match--active` (warm/amber), distinct from the
  blue-ish `.text-line-hover-source--active` hover state. Clearing/changing the
  active search match does not clear hover classes.
- Previous/next navigation uses `scrollSide: "preferTranscription"`: if a
  transcription target exists for the active match, scroll that target only;
  otherwise fall back to source-side scroll. Do **not** issue both scrolls in
  one activation.
- Initial page-load `setActiveMatch` preserves multi-page source scroll and does
  **not** prefer transcription scroll merely because a transcription target
  exists.
- The text panel is not a separate overflow container; desktop sticky overflow
  applies only to the source-image panel.

**Intended behavior (bidirectional click follow-up on this branch):**

- Source overlay click: activate the match and scroll **transcription only**
  when a transcription target exists; never also scroll the source page the
  user just clicked. Missing transcription target → highlight only (fail
  closed; no guessed page scroll).
- Transcription match click: activate the same canonical match index and scroll
  **source only** (prefer the matching overlay target inside the sticky/
  overflow source panel). Do not re-scroll the transcription. Missing source
  overlay → highlight only.
- A canonical match may span multiple transcription elements (same
  `data-archive-search-transcription-match-index`); clicking any span activates
  that match. Transcription match spans are minimally interactive
  (`role="button"`, `tabindex="0"`, Enter/Space) without changing visible text
  or nesting invalid interactive markup.
- Scroll intent is explicit via `scrollSide`
  (`none` / `transcription` / `source` / `preferTranscription`). No reciprocal
  programmatic clicks (avoids scroll loops).

**Deferred:** reverse image→text hover; PDF/Gemini geometry; search matching /
indexing changes.

**Tests:** `documents/test_archive_search_transcription_presentation.py` plus
existing hover and archive-search overlay/navigation suites.

## Public OCR detail — text-line hover + sticky archive-search match nav (P1)

**Decision / current behavior (P1 — merged/deployed):**
On the public OCR document detail page:

1. Hovering a displayed transcription line may highlight the corresponding
   trusted Transkribus source-image line geometry.
2. When archive-search match navigation is present
   (`.archive-search-match-nav`: previous / status / next), that existing
   control remains visible while scrolling via CSS `position: sticky`.

Sticky search-match navigation is presentation-only: no second nav component,
and no changes to archive-search matching, redirect, or geometry. Previous/next
↔ displayed-transcription sync (#398) is merged/deployed; see “archive-search
match ↔ transcription sync” above. Bidirectional click sync remains a separate
unmerged follow-up on this branch.

**Current behavior (as implemented in this change):**

- Hover mapping is built server-side from the exact
  `DocumentTextResult` selected by `resolve_displayed_transcription_result`
  (Hebrew: HEBREW_TEXT then SOURCE_TEXT; non-Hebrew: SOURCE_TEXT then
  HEBREW_TEXT). Do **not** infer the row by matching display text strings.
- Binding/snapshot trust is gated by `resolve_trusted_hover_binding` (shared
  with the text-range geometry service via `is_binding_trusted_for_hover`).
  Missing, stale, untrusted, or `hover_eligible=False` bindings disable **all**
  hover. There is no snapshot fallback and no client-side offset reconstruction.
- Canonical line offsets come from stored Transkribus `char_start` / `char_end`.
  After `resolve_trusted_hover_binding` succeeds, contributing lines are loaded
  once from that trusted snapshot (`select_related("page")`). Each line is
  validated independently through shared
  `text_range_geometry_from_snapshot_line` (no per-line
  `resolve_text_range_geometry` re-query). A line with invalid/unusable
  geometry stays plain/non-hoverable; other independently valid lines may
  remain hoverable. Geometry is never guessed or fabricated. Page dimensions
  are loaded from the trusted `binding.snapshot_id` only.
- Archive-search multi-line geometry resolution via
  `resolve_text_range_geometry` is unchanged: one invalid intersecting line
  still fails that whole search-match range closed.
- Other fail-closed cases: missing page dimensions / out-of-bounds bbox for a
  line, PDF / non-renderable source pages for a line, inability to reconstruct
  the visible text character-for-character, or no remaining hoverable overlays
  → no hover UI (or plain text for the affected line only, when binding trust
  still holds).
- Browser overlays reuse fail-closed page-relative percent conversion
  (`overlay_bbox_percent.page_bbox_to_percent`) and the same renderable-page
  contract as archive-search overlays (multi-image `display_number` /
  single IMAGE page 1 only).
- Hover DOM/CSS/JS uses a separate namespace from search matches
  (`data-text-line-hover-id`, `.text-line-hover-*`). Clearing hover must not
  remove `.archive-search-overlay-target--active`. Search and hover overlays
  may coexist.
- Displayed text remains character-equivalent to the existing reading-prose
  presentation, including separators/newlines. Django autoescaping stays
  intact; hover markup is not arbitrary JS-generated HTML.
- Sticky match nav reuses `.archive-search-match-nav` with
  `position: sticky; top: var(--space-4)` (same offset as other detail sticky
  panels; site nav is not fixed). Minimal `z-index` / `box-shadow` /
  existing `var(--card-muted)` background keep it readable over scrolling
  source content. Existing ≤480px column flex layout is preserved.

**Deferred:** reverse image→text hover; PDF/Gemini geometry; edit/review hover
surfaces; search matching changes.

**Tests:** `documents/test_text_line_hover_presentation.py` plus existing
archive-search overlay/geometry/navigation suites.

## VIDEO public YouTube — error 153 referrer fix + unified click-to-load facade

**Decision / implemented:** Narrow public VIDEO follow-up after PR3. Production YouTube embeds showed error 153 after click-to-load activation because the iframe used `referrerPolicy="no-referrer"`, which withheld the Referer/client identification YouTube requires. Also unify the pre-activation privacy placeholder and post-activation player into one media component.

**Current behavior:**

- Site-wide Django `SECURE_REFERRER_POLICY` remains `same-origin` (archive-item path is not sent cross-origin on ordinary navigations).
- Activated YouTube iframe sets `referrerPolicy="strict-origin-when-cross-origin"` so YouTube receives an origin-level Referer without sending the full item path.
- Do **not** use iframe `no-referrer`. Do **not** weaken the site-wide Referrer-Policy.
- External KAN/OTHER and YouTube fallback links keep `rel="noopener noreferrer"`.
- Approved embed host remains `https://www.youtube-nocookie.com` only; CSP `frame-src` unchanged; no autoplay; no fallback to `youtube.com/embed`.
- Click-to-load still creates the iframe only after explicit activation (initial HTML has no iframe / no third-party thumbnail).
- Unified `.video-embed-facade__media` holds both the local placeholder and the hidden player; activation hides the complete facade content and reveals the iframe in the same media area (no second player block below the placeholder).
- Layout: **16:9 by default** (`aspect-ratio: 16 / 9`) with **`min-height: 200px`** so title, explainer, and activation control remain usable. On sufficiently narrow content widths (e.g. ~320px), the box may become **slightly taller than 16:9**; the player fills the same container (no layout jump). No explicit 4:3 media-query override.
- After activation, focus moves to the iframe; the iframe stays in sequential Tab order (native focusability — do **not** set `tabindex="-1"`). The activation button is disabled only after successful iframe creation/insertion.
- Client-side embed validation (defense-in-depth; server still builds the URL): exact HTTPS + `www.youtube-nocookie.com` + `/embed/<11-char id>`; no userinfo/non-default port/hash; query allowlist only `playsinline` / `start` / `end` (no duplicates; canonical integer times; `playsinline` must be `1` when present; reject `autoplay` and any unknown key).

**Supersedes (PR3 referrer note):** PR3 documented iframe `referrerPolicy="no-referrer"`. That choice caused YouTube error 153 in production and is no longer current.

**Out of scope:** VIDEO models/migrations/parser/provider classification, management forms, search/visibility, KAN/OTHER presentation mode, metadata/thumbnails/uploads/captions/transcription/workers/queues, unrelated infrastructure.

**Tests:** `documents/test_video_public.py` (referrer contract, unified facade HTML/script contract, query allowlist, keyboard/focus contract, CSP/Referrer-Policy, security regressions).

## VIDEO ArchiveItems — PR3 public discovery, detail, click-to-load, security

**Decision / implemented:** Ship the public VIDEO experience on existing `/archive/` browse/detail routes, including YouTube click-to-load, KAN/OTHER external-link presentation, public type filter “סרטונים”, CSP `frame-src`, Referrer-Policy, privacy copy, and accessibility for the activation control.

**Public contract:**

- VIDEO appears in authorized browse/search/pagination via existing `archive_browse_queryset_for_user` / search-index metadata hooks from PR1.
- Public type filter choices are: הכל / מסמכים וטקסטים / תמונות / סרטונים. VIDEO is never merged into documents/texts.
- Browse cards use a local `--video` marker and provider label preview; no third-party thumbnails or network fetches at render time.
- Public detail reuses ArchiveItem metadata partials; media UI is provider-specific via `documents/services/video_presentation.py`.
- YouTube: no iframe / no YouTube request before explicit activation; local placeholder shows the ArchiveItem title (fallback “סרטון”) plus activation control; iframe `src` is built server-side as `https://www.youtube-nocookie.com/embed/<validated-id>` with integer start/end only; no autoplay; no-JS fallback link to normalized `source_url`.
- KAN / OTHER: external-link only (`target="_blank" rel="noopener noreferrer"`); never iframe / HLS / posters.
- Missing or inconsistent `VideoContent` fails closed (`Http404` / no unsafe link).

**Security headers (site-wide via Django settings/middleware):**

- `ContentSecurityPolicyMiddleware` + `SECURE_CSP = {"frame-src": ['self', https://www.youtube-nocookie.com]}` — smallest CSP change; does not set/open `default-src` / `script-src` / `object-src`.
- `SECURE_REFERRER_POLICY = "same-origin"` — preserves Django’s prior site-wide default. *(Historical PR3 note: iframes originally used `referrerPolicy="no-referrer"`. Superseded by the error-153 follow-up above: activated iframes now use `strict-origin-when-cross-origin`; site-wide policy stays `same-origin`.)*
- Public presentation re-parses `VideoContent.source_url` with `parse_video_url()` and requires stored provider/mode/id/canonical URL to match; explicit YouTube start/end may differ from URL-derived times when otherwise valid.

**Privacy copy:** About page section “פרטיות וסרטונים חיצוניים” plus on-detail YouTube activation explainer. No cookie-consent system. Copy states YouTube loads only after activation via `youtube-nocookie.com`; it does not claim that no information is sent after activation.

**Management list (current):** VIDEO titles link to public `archive-detail`. Edit and delete remain separate management actions. Public VIDEO detail is implemented.

**Out of scope:** uploads, thumbnails, provider metadata APIs, captions/transcription, OCR/HTR/workers/infra, analytics, autoplay, arbitrary embeds.

**Tests:** `documents/test_video_public.py`; manage-list regression updated in `documents/test_video_manage.py`.


## VIDEO ArchiveItems — PR2 management UI

**Decision / implemented:** Add staff management create/edit/delete for `ArchiveItem.ItemType.VIDEO` on existing `/archive/manage/` routes, reusing PR1 services (`create_video_archive_item` / `update_video_archive_item` / `parse_video_archive_item_form`).

**Management contract:**

- Unified create chooser slug `video` (“סרטון”) on `/archive/manage/new/`.
- Primary VIDEO input is `source_url`; provider, presentation mode, and provider video id remain server-derived and are not manually editable.
- Optional YouTube start/end accept seconds or friendly clock syntax (`1h2m3s`); blank keeps URL-derived times. Non-YouTube POSTs with times are rejected.
- Management UI shows a Hebrew presentation-mode explanation (`הסרטון יוצג כאן באתר` / `הצפייה תיפתח באתר המקורי`). Progressive-enhancement script toggles time fields/hint only; validation stays server-side.
- Restricted visibility choices and write authorization reuse `parse_visibility(user=)` exactly as MANUAL_TEXT / PHOTO / OCR.
- Delete allowlist includes VIDEO; cascading `VideoContent` delete follows the OneToOne CASCADE (no S3 cleanup).
- Successful create/edit redirects to the manage list.
- Delete uses `get_accessible_archive_item` (visibility-scoped via `filter_archive_items_for_user`); restricted VIDEO cannot be opened or deleted without `documents.view_restricted_archiveitem`.
- Management subtitle copy: “הוספה באמצעות קישור, ללא העלאת קובץ” (not “קישור חיצוני בלבד”, which contradicted YouTube embedded mode).

**Historical note (superseded by PR3):** While public VIDEO detail did not yet exist, PR2 temporarily linked manage-list VIDEO titles to `archive-manage-edit` to avoid a 404. That workaround is no longer current. **Current behavior:** manage-list VIDEO titles link to public `archive-detail`; edit/delete remain separate actions.

**Unchanged / out of scope for this PR:** public detail/cards/filters/search presentation, click-to-load embeds, CSP/Referrer-Policy, privacy copy, thumbnails, uploads, OCR/HTR/workers/infra. *(Public surfaces completed in PR3 entry above.)*

**Tests:** `documents/test_video_manage.py`; restricted visibility form coverage extended in `documents/test_restricted_visibility_forms.py`.

## VIDEO ArchiveItems — PR1 foundation (model, URL parser, services)

**Decision / implemented:** Add top-level `ArchiveItem.ItemType.VIDEO` (“סרטון”) backed by one-to-one `VideoContent`. V1 stores URL/provider presentation metadata only — no media bytes, S3 upload, transcription, captions, workers, or OCR/HTR coupling.

**Contract:**

- Shared catalog metadata stays on `ArchiveItem` (title, author_name, source_title, public_note, visibility, dates, metadata_status, discovery M2Ms).
- `VideoContent` holds `source_url`, `provider` (`YOUTUBE` | `KAN` | `OTHER`), `presentation_mode` (`EMBEDDED` | `EXTERNAL_LINK`), `provider_video_id`, optional YouTube-only `start_seconds` / `end_seconds`.
- Provider/mode are computed server-side by an isolated URL parser (`documents/services/video_url.py`) with no network I/O, redirects, or metadata fetches.
- YouTube is the only `EMBEDDED` provider; KAN and OTHER are always `EXTERNAL_LINK`. Spoofed hosts are not treated as YouTube/KAN.
- Changing `source_url` fully recomputes provider fields so stale YouTube IDs/times cannot remain after a KAN/OTHER transition.
- Create/update are atomic service writers that call `sync_archive_item_search_index` explicitly (no Django signals). Restricted visibility continues to use `parse_visibility` / `documents.view_restricted_archiveitem`.
- Search indexes ArchiveItem metadata only; `body_text` and `hebrew_translation_text` stay empty; `source_url` / `provider_video_id` are not indexed.
- Browse renderability fails closed for VIDEO without valid `VideoContent`; OCR/MANUAL_TEXT/PHOTO gates unchanged.
- Semantic validity: `VideoContent.clean()` re-parses `source_url` and requires provider/mode/id to match the normalized parse; YouTube IDs must match `^[A-Za-z0-9_-]{11}$`. Start/end may be explicit YouTube overrides. Shared contract lives in `video_url_contract.py` (no model import) to avoid circular imports.
- Obvious YouTube/KAN impersonation hosts are rejected (not stored as OTHER). Ordinary unknown HTTPS hosts remain OTHER.
- Hostname classification strips trailing DNS root dots so `www.youtube.com.` / `www.kan.org.il.` classify as YouTube/KAN, not OTHER.
- KAN port contract: `http` with no port or `:80`, and `https` with no port or `:443`, normalize to canonical `https://host/...` without a default port; non-default KAN ports are rejected. OTHER HTTPS still preserves non-default ports/path/query/fragment. Userinfo and IPv6 literals are rejected; YouTube rejects non-default ports.
- VIDEO create/update discovery follows `update_archive_item_discovery_metadata` replace-all: `category_names`, `event_names`, and `tag_names` must be supplied together or all omitted; partial calls raise before any write.
- Create/update perform exactly one search-index sync (via discovery helper when discovery is written, otherwise direct sync).

**Out of scope for this PR:** manage UI, public detail/cards/filters, embeds/click-to-load, CSP/Referrer-Policy, privacy copy, accessibility UI, thumbnails, uploads.

**Migration:** `0050_video_content` — additive `ItemType.VIDEO` choice + `VideoContent` table/constraints; no backfill or reclassification of existing rows. App rollback before reverse migration may leave `item_type=VIDEO` rows that older code treats as unknown (fail-closed on public detail). Reverse drops `VideoContent`; orphan `VIDEO` ArchiveItems would need manual cleanup if production data exists.

**Tests:** `documents/test_video_url.py`, `documents/test_video_content.py`, `documents/test_video_archive_items.py`.

## ArchiveItem visibility — manager-facing display metadata (UI)

**Decision / implemented:** Show each item’s visibility to managers as ordinary metadata throughout existing management and item interfaces. No authorization, queryset, validation, migration, or processing changes.

**Presentation contract:**

- Short read-only display labels (`visibility_display_label` / `archive_visibility_display_label`): `public` → `ציבורי`, `private` → `פרטי`, `restricted` → `רגיש`.
- Full form-choice labels (`visibility_choice_label` / `archive_visibility_choice_label` / `archive_visibility_ui_choices`): restricted remains `רגיש — למורשים בלבד`. Legacy aliases `visibility_label` and `archive_visibility_label` both remain full choice-label aliases (same semantics in Python and templates).
- Read-only templates must use `archive_visibility_display_label`; do not use the legacy filter for short display text.
- Visibility is rendered as ordinary text in existing tables, page leads, and metadata lines — not as badges, pills, chips, alerts, callouts, standalone visibility cards, or standalone visibility sections.
- Manager-only surfaces gain the short label; anonymous/family viewers do not gain new administrative visibility metadata.
- Django Admin already showed visibility as ordinary list/detail fields; left unchanged.
- Public archive browse cards (`/archive/` and category/event/tag browse; shared `item_list_cards.html`) show staff/document-admins only: `נראות: ציבורי` / `נראות: פרטי` / `נראות: רגיש` as ordinary text in the existing card meta line. Family and anonymous viewers do not receive this metadata. Restricted cards appear only when the existing access queryset already includes the item. Form-only `רגיש — למורשים בלבד` is not used on cards. Category/event/tag browse pass `is_admin` so the shared partial can gate this line.
- Staff-facing detail leads that already showed the short visibility value prefix it as `נראות: <short-label>` (archive PHOTO / MANUAL_TEXT / VIDEO detail, OCR document detail, review detail, transcription suggestion detail, Transkribus corrected-current attempts list/detail). Tables, queues, filters, and Django Admin are unchanged.

**Unchanged:** PR1–PR3 access controls, Admin scoping, write validation, data model, OCR/HTR/processing/infra.

**Tests:** `documents/test_visibility_metadata_ui.py`.

## ArchiveItem visibility — `restricted` (PR1 application authz)

**Decision:** Add a third `ArchiveItem.visibility` value, `restricted`, with access controlled only by the explicit Django permission `documents.view_restricted_archiveitem`.

**Contract (approved):**

- `ArchiveItem.visibility` remains the access-control source of truth.
- `public` behavior unchanged (everyone).
- `private` behavior unchanged: `archive_family` members and existing document-admins (`is_staff` or `is_superuser`) may view.
- `restricted` is visible only when `user.has_perm("documents.view_restricted_archiveitem")`.
- `is_staff` alone must **not** grant restricted access.
- Active superusers may view restricted through Django’s normal `has_perm` behavior.
- A non-staff family user with the explicit permission may view restricted.
- Unknown visibility values fail closed for everyone.
- Unauthorized direct object access returns **404**.
- Processing paths (OCR/HTR, Gemini, Transkribus, translation, indexing, sync execution, activation) remain unchanged and out of scope for this PR.

**Implemented in this PR:** model enum + custom permission migration; centralized helpers in `archive_item_access.py` / `document_access.py`; staff application surfaces (manage, backlog, review, suggestions, corrected/current sync) use authorized querysets / `get_viewable_*` so staff without the permission cannot discover restricted titles, counts, text, previews, or presigned URLs.

**Rollout rule (superseded):** The temporary prohibition on marking production items `restricted` applied until forms/UI/write validation landed. See PR3 below for the accurate production rollout condition (merge alone is not sufficient).

**Superseded deferral:** form/UI visibility choice and upload validator allowlists; Hebrew UI label → completed in the PR3 entry below. Django Admin queryset/autocomplete hardening → completed in the PR2 entry below.

## ArchiveItem visibility — `restricted` (PR2 Django Admin hardening)

**Decision / implemented:** Scope registered Django Admin surfaces that can expose ArchiveItem/Document content through the same centralized visibility helpers used by application authz (`filter_archive_items_for_user` / `archive_item_queryset_for_user` / `filter_documents_for_user` / `document_queryset_for_user`). Do **not** duplicate public/private/restricted rules inside Admin classes.

**Admin classes scoped:** `ArchiveItemAdmin`, `ManualTextContentAdmin`, `PhotoContentAdmin`, `DocumentAdmin`, `DocumentTextResultAdmin`, `CorrectionRequestAdmin`, `TranskribusRunAdmin`, plus `TranskribusRunInline` queryset scoping. FK choice querysets for `ArchiveItem` / `Document` relations are limited via a shared admin mixin (`formfield_for_foreignkey`), covering foreign-key widgets and any future autocomplete/raw-ID lookups that reuse Admin `get_queryset` / search.

**Behavior:**

- Staff without `documents.view_restricted_archiveitem` cannot discover restricted titles, transcription text, filenames, S3 keys, correction messages, or row existence through Admin changelists, search, list filters, or direct object URLs (Admin non-disclosing missing-object behavior).
- Staff with the permission and active superusers retain access.
- `public` / `private` Admin visibility unchanged (private remains available to document-admins / staff).
- Unknown visibility values remain fail-closed (hidden even from superusers).
- Existing Django model permissions are preserved; this PR only adds visibility scoping on top.

**Unchanged / out of scope:** application views already covered by PR1; upload visibility validators; forms/UI exposing `restricted` (see PR3); OCR/HTR/Gemini/Transkribus/translation/indexing/sync/activation; new migrations; registering additional Admin models that are not currently registered.

**Rollout rule:** Superseded by PR3 — see the PR3 rollout condition below (merge alone is not sufficient for production use).

**Tests:** `documents/test_restricted_visibility_admin.py`.

## ArchiveItem visibility — `restricted` (PR3 forms / UI / write validation)

**Decision / implemented:** Expose and validate `restricted` on staff create/edit/upload surfaces. Reuse the same permission that controls viewing:

- `documents.view_restricted_archiveitem`

That permission currently controls **both** viewing restricted items and assigning/changing `ArchiveItem.visibility` to/from `restricted`. A separate write-only permission is a possible future refinement and is **not** part of this PR.

**Contract:**

- Hebrew UI label: `רגיש — למורשים בלבד` via centralized `visibility_label` / `archive_visibility_ui_choices(user)`.
- Choice lists include `restricted` only when the requesting user has the permission (active superusers follow Django `has_perm`).
- Staff without the permission must not see the option and must not be able to submit `restricted` via crafted POST/JSON.
- Enum-valid alone is not sufficient; unauthorized `restricted` writes fail with the same `visibility is invalid` error as unknown values.
- Default visibility remains `private`.
- `public` / `private` behavior unchanged.

**Write paths covered:** manual text create/edit; OCR upload create JSON + OCR metadata edit; photo upload create JSON + photo metadata edit; shared archive metadata form parser; upload form / photo form / manage form choice rendering; documents list visibility filter choices.

**Rollout:** Production use of `restricted` is allowed only after **all** of the following:

- PR3 is merged;
- the resulting code is deployed to the target environment;
- migration `0049_archiveitem_restricted_visibility` is applied there;
- `documents.view_restricted_archiveitem` is granted only to intended authorized users;
- a short role-based smoke test confirms:
  - unauthorized staff do not see or submit restricted;
  - authorized staff can create/view/edit restricted;
  - anonymous/family behavior remains correct.

Merge of PR3 alone does **not** authorize production use.

**Unchanged / out of scope:** PR1 application authz; PR2 Django Admin scoping; new permissions/migrations; OCR/HTR/Gemini/Transkribus/translation/indexing/queues/sync/activation/infra/S3; reclassification of existing production items.

**Tests:** `documents/test_restricted_visibility_forms.py`.

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

**Decision:** OCR document-management staff surfaces share one cross-management nav partial (`documents/partials/staff_document_nav.html`) for the same OCR document: **document detail** (`/api/ui/documents/<doc_id>/`), **transcription review detail** (`/api/ui/admin/review/<doc_id>/`), and Transkribus corrected/current-sync list/detail. Links: **`עריכת מטא־דאטה`** (when **`archive_item_id`** exists), **`בקרת תעתוק`**, **`תצוגת מסמך`**, **`גרסאות תעתוק מ־Transkribus`** (only when the existing Transkribus corrected/current-sync UI eligibility applies), and **`עריכה טכנית (Django Admin)`**. The current section is omitted (no self-navigation). Buttons use dedicated smaller/distinct **`staff-document-nav` / `staff-document-nav__button`** styling (not ordinary **`btn-primary`**). The shared nav is **not** placed on the metadata-edit page. Transkribus attempt detail keeps local **`חזרה לגרסאות תעתוק`** separately. **`/archive/manage/`** OCR rows still use **`עריכת מטא־דאטה`** (MANUAL_TEXT labels unchanged).

**Unchanged:** Metadata edit save behavior, review POST handlers, **`DocumentTextResult`** status/verification semantics, backlog queryset/filters/counts, permissions, OCR/HTR processing, upload API, worker/SQS, Transkribus sync/enqueue/activation eligibility, **`ArchiveItem`** runtime cutover. **No** delete action for **`OCR_DOCUMENT`**.

**Scope (PR4):** Detail/review-detail/manage-list / Transkribus preview template action links, shared staff nav partial, focused tests, this log entry.

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

**Current state (supersedes “optional PR5g” above):** Migration **`0035_remove_document_date_end_and_more`** removed the six duplicated shared archival columns from **`Document`** (`title`, `visibility`, `metadata_status`, `date_start`, `date_end`, `date_precision`). **`ArchiveItem`** is now the **only** database home for those fields. **`Document`** retains OCR/runtime fields only. See **“ArchiveItem — Document shared-metadata mirror column removal (0035)”** below.

## Document date precision — schema foundation (PR 2)

**Decision:** Add **`Document.date_precision`** (`DatePrecision`: `EXACT_DAY`, `MONTH`, `YEAR`, `RANGE`, `UNKNOWN`) with Django default **`UNKNOWN`**. Migration adds the column with that default for **all** existing rows — **no** inference from `date_start`/`date_end` in this PR.

**Scope (PR 2):** Model + migration + minimal admin fieldset + focused tests + design-doc pointer update only. **No** upload/UI/display/filtering changes, **no** `date_display` / `date_note` / estimated-date fields, **no** backfill script beyond the migration default.

**Deferred:** Precision-aware save/display (PR 3–5 in `docs/ai-context/document-date-precision.md`); automated backfill rules (section 8 of that doc) require explicit approval before any data migration beyond `UNKNOWN`.

**Current state:** **`date_precision`** (and other shared date fields) now live on **`ArchiveItem`** only; removed from **`Document`** in migration **0035**.

## Archive item date precision — typed entry and partial ranges (2026-07-14)

**Decision:** Add precision-aware segmented date entry (year/month/day text fields with `inputmode="numeric"`) and two new range precisions while keeping existing **`RANGE`** as exact-day range semantics.

**New `DatePrecision` values (ArchiveItem only):**

| Value | Meaning | Normalized storage | Public display |
|-------|---------|-------------------|----------------|
| `RANGE` | Exact-day range (unchanged) | Exact start/end calendar days | `DD/MM/YYYY - DD/MM/YYYY` |
| `RANGE_MONTH` | Month+year range | Start → first day of start month; end → last day of end month | `MM/YYYY - MM/YYYY` |
| `RANGE_YEAR` | Year-only range | Start → Jan 1 of start year; end → Dec 31 of end year | `YYYY - YYYY` |

**Single-value normalized bounds:**

| Value | `date_start` | `date_end` |
|-------|--------------|------------|
| `EXACT_DAY` | exact day | same day |
| `MONTH` | first day of month | last day of month |
| `YEAR` | Jan 1 | Dec 31 |

**Backward compatibility:** Existing rows with `date_precision=RANGE` are **not** reinterpreted. No inference of month/year range precision from normalized boundary values. Legacy API/form `date_start`/`date_end` ISO fields map into components when segmented fields are absent.

**Form safety:** Shared partial `archive_date_form_fields.html` is the single source for precision select + segmented inputs. Server-rendered `hidden`/`disabled` state matches precision before JS runs; `archive_date_entry.js` updates the same state on change.

**Scope:** `ArchiveItem` only for typed entry and new range precisions. `Document.DatePrecision` remains the legacy five-value enum (date fields removed from Document in 0035). `ArchiveEvent.date_precision` reuses `ArchiveItem.DatePrecision.choices` at runtime but has **no** dedicated typed-entry UI; migration 0037 alters **ArchiveItem** only.

**Implementation:** `documents/services/archive_date_input.py` (parse/normalize/validate/repopulate); shared template `archive_date_form_fields.html`; `format_document_date()` extended for `RANGE_MONTH` / `RANGE_YEAR`. Migration **0037** updates `ArchiveItem.date_precision` choices.

**Deferred:** Date-based filtering/search overlap queries (unchanged).

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
- **Hebrew printed (`he` + `PRINTED`)** remains **Gemini** with **`prompt_variant=printed`**. Model selection is route-specific via **`GEMINI_HEBREW_PRINTED_MODEL`** (default **`gemini-3.1-flash-lite`**, single candidate; no automatic fallback to **`gemini-2.0-flash`** in this step). Resolved in **`ocr_routing.gemini_model_candidates`** and injected by **`htr_engine.transcribe_pages`** when **`worker_env`** is present.
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

- After successful OCR/HTR, worker persists **`SOURCE_TEXT`**, then calls Gemini Hebrew translation and persists **`HEBREW_TEXT`** (see **`docs/ocr-routing-reference.md`**). Translation failure persists a failed **`HEBREW_TEXT`** row (`HEBREW_TRANSLATION_FAILED`); manual retry via `hebrew_translation_retry`.
- `expected_outputs` expects **`SOURCE_TEXT` + `HEBREW_TEXT`**. Missing or failed **`HEBREW_TEXT`** → **`PARTIAL`**, not OCR failure.

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

### Non-Hebrew Hebrew translation — intentional `PARTIAL` (historical)

> **Superseded.** Automatic Gemini Hebrew translation for non-Hebrew documents is **implemented**. See **“Non-Hebrew `PARTIAL` (intentional)”** in **“Current state — OCR/HTR and Transkribus”** above and **`docs/ocr-routing-reference.md`**.

**Earlier behavior (before translation):** worker persisted **`SOURCE_TEXT` only**; documents stayed **`PARTIAL`** until **`HEBREW_TEXT`** existed — **not** an OCR failure.

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

**Product (V1, historical):** Staff/admin create one image; item appears in **`/archive/`** and **`/archive/<id>/`**; list uses placeholder/icon (not full original); detail shows original via presigned URL. Reuse existing **`ArchiveItem`** shared and discovery metadata fields — no large photo-specific metadata system in V1.

**Current state (supersedes list preview above):** Browse list uses stored **`thumb_400.jpg`** thumbnails when available; CSS markers when not. Detail still shows the full original. See **“Archive browse card thumbnails — current state”**.

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

**Public archive guard (PR3, historical):** Before PR4, **`/archive/`** list/detail/discovery browse excluded **`PHOTO`** until display shipped. Staff **`/archive/manage/`** still lists PHOTO items.

**Current state:** PR4+ renderability is enforced by **`filter_browse_renderable_archive_items`** inside **`archive_browse_queryset_for_user`** (uploaded PHOTO + uploaded OCR + other types). The historical **`exclude_deferred_archive_browse_item_types`** helper is **not** in the current codebase.

**Endpoints:** **`POST /api/photo-uploads/create/`**, **`POST /api/photo-uploads/<photo_content_id>/complete/`** (staff only). UI branch: **`/archive/manage/new/?item_type=photo`**.

**S3 keys:** **`photos/{photo_content_id}/original.{ext}`** (canonical ext from validated MIME). Private bucket only; no presigned GET in PR3.

**Finalize idempotency:** Repeat complete on **`upload_status=UPLOADED`** returns current state without S3 re-verify or field overwrite.

**Deferred:** Re-upload/retry after **`upload_status=FAILED`** (not implemented in PR3).

**Scope (PR3):** Service + API + unified create UI branch + discovery metadata on create + focused tests + minimal doc updates. **No** **`Document`**, worker, SQS, archive list/detail rendering, thumbnails, dimensions, edit/delete, visibility changes.

**Deferred (PR4+):** Archive list/detail PHOTO display (presigned GET), edit/delete polish, thumbnail generation — per `docs/ai-context/photo-archive-items.md`.

**Docs:** `docs/ai-context/photo-archive-items.md`

## ArchiveItem — PHOTO public/archive display V1 (PR4)

**Decision:** Show uploaded **`PHOTO`** items on **`/archive/`** list and **`/archive/<id>/`** detail. Reuse existing **`ArchiveItem.visibility`** access rules exactly — no new visibility tier or photo-specific permission layer.

**Browse vs access:** **`archive_item_queryset_for_user`** answers visibility/access only. **`archive_browse_queryset_for_user`** adds PHOTO renderability: linked **`PhotoContent`**, **`upload_status=UPLOADED`**, non-empty **`original_file_key`**. **`PENDING`** / **`FAILED`** PHOTO items return **404** on detail and are omitted from list/discovery browse. Staff **`/archive/manage/`** unchanged (all PHOTO rows regardless of upload status).

**List (V1, historical):** Type label + modest placeholder text; **no** presigned GET per row.

**Detail (V1):** After **`get_viewable_archive_item`**, generate presigned GET via existing **`create_presigned_get`** for **`PhotoContent.original_file_key`** when **`UPLOADS_BUCKET_NAME`** is configured; otherwise safe unavailable message. S3 objects remain private.

**Scope (PR4):** Access/browse queryset eligibility, list/detail templates, presigned GET on detail only, focused tests, minimal doc updates. **No** thumbnails, dimensions, edit/delete, S3 cleanup, OCR/**`Document`**, worker.

**Deferred (PR5+, historical):** Edit/delete polish, thumbnail generation — per `docs/ai-context/photo-archive-items.md`.

**Current state (supersedes PR4 list preview above):** Public browse cards now presign **`PhotoContent.thumbnail_file_key`** when present (never **`original_file_key`**). Missing thumbnail or presign failure uses the CSS **`--photo`** type marker. Detail still presigns the full original. See **“Archive browse card thumbnails — current state”** and `docs/ai-context/photo-archive-items.md` (**Current authoritative state**).

**Docs:** `docs/ai-context/photo-archive-items.md`

## ArchiveItem — PHOTO staff metadata edit + delete V1 (PR5)

**Decision:** Add first-party staff/admin PHOTO management polish on existing archive manage routes. Reuse **`parse_archive_metadata_form`**, **`update_archive_item_discovery_metadata`**, and shared metadata form fields — **no** separate PHOTO metadata path.

**Edit:** **`/archive/manage/<id>/edit/`** for **`item_type=PHOTO`** updates **`ArchiveItem`** shared fields + discovery M2M only via **`update_photo_archive_item_metadata`**. **Does not** change **`PhotoContent.original_file_key`**, upload status, or image bytes. **No** re-upload UI. Redirect after save → **`/archive/manage/`** (not public detail — **`PENDING`**/**`FAILED`** PHOTO may be non-renderable on **`/archive/<id>/`**).

**Delete:** **`/archive/manage/<id>/delete/`** for **`PHOTO`** and **`MANUAL_TEXT`** only (OCR unchanged — still **404**). GET shows confirmation; POST deletes **`ArchiveItem`** (cascades **`PhotoContent`**) and redirects to **`/archive/manage/`**. Staff/admin only.

**S3 cleanup:** **Deferred.** PR5 deletes DB rows only. No existing safe project-wide S3 delete-object helper; orphaned private photo keys are a known follow-up (operational cleanup runbook/job).

**Scope (PR5):** Service, edit/delete views/templates, manage list + detail staff actions, focused tests, minimal doc updates. **No** re-upload/retry after **`FAILED`**, thumbnails, dimensions, captions, OCR/**`Document`**, worker/SQS, visibility changes.

**Deferred:** S3 object delete on PHOTO delete, re-upload/retry, thumbnail generation — per `docs/ai-context/photo-archive-items.md`.

**Docs:** `docs/ai-context/photo-archive-items.md`

## ArchiveItem — PHOTO staff manage status clarity (PR6)

**Decision:** Improve staff **`/archive/manage/`** clarity for PHOTO upload and public-archive renderability without changing behavior.

**Manage list:** PHOTO rows show Hebrew **`PhotoContent.upload_status`** label and a separate archive-renderability signal. Renderable when **`upload_status=UPLOADED`** and **`original_file_key`** is non-empty (same upload/key checks as **`filter_browse_renderable_archive_items`**; **`filter_browse_renderable_photo_items`** is a backward-compatible alias). Visibility is shown in its own column. Non-PHOTO rows show **—** in those columns.

**Edit/delete copy:** PHOTO edit page states metadata-only / no file replacement / public archive after successful upload. PHOTO delete confirmation states DB-row delete and deferred S3 cleanup.

**Helpers:** **`documents/services/photo_presentation.py`** — **`photo_upload_status_label`**, **`photo_is_archive_renderable`**, and related staff labels.

**Scope (PR6):** Templates, presentation helpers, focused tests, minimal doc updates. **No** thumbnails, presigned GET in manage list, S3 delete, re-upload, model/migration changes.

**Docs:** `docs/ai-context/photo-archive-items.md`

## OCR/HTR — Hebrew printed Gemini model config

### Decision

- **`language=he`** + **`text_input_type=PRINTED`** continues to route to **Gemini** with **`prompt_variant=printed`** (unchanged **`OCR_ROUTES`** entry).
- **`GEMINI_HEBREW_PRINTED_MODEL`** (default **`gemini-3.1-flash-lite`**) on **`WorkerEnvConfig.gemini_hebrew_printed_model`** selects the Gemini runtime model for that pair only.
- **`ocr_routing.gemini_model_candidates`** resolves model candidates by route + language + text input type. **`htr_engine.transcribe_pages`** passes **`model_candidates`** into **`GeminiAdapter`** when **`worker_env`** is present. **`GeminiAdapter`** stays generic.
- Hebrew printed uses a **single** model candidate by default (**no** automatic **`gemini-2.0-flash`** fallback in this PR). Other Gemini routes keep **`("gemini-2.0-flash", "gemini-1.5-flash")`**.
- **No** changes to Hebrew handwritten / Transkribus routing, prompts, **`DocumentTextResult`** schema, or processing-state rollup.

### Deferred

- Full explicit **`(language, text_input_type)` → model/prompt** matrix on **`OcrRouteConfig`**; additional per-route env overrides beyond Hebrew printed.

## ArchiveItem — Document shared-metadata mirror column removal (0035)

**Decision:** Remove the six duplicated shared archival columns from **`Document`** now that **`ArchiveItem`** is canonical for reads, writes, filters, and upload/create.

**Migration:** **`0035_remove_document_date_end_and_more`** drops from **`Document`**: **`title`**, **`visibility`**, **`metadata_status`**, **`date_start`**, **`date_end`**, **`date_precision`**.

**Current behavior:**

- **`ArchiveItem`** is the **only** ORM storage for those six fields across all item types.
- **`Document`** retains OCR/runtime fields (`doc_type`, `language`, `text_input_type`, upload/processing state, file keys, thumbnail fields, catalog/tags side fields, etc.).
- Display and staff edit paths read/write shared metadata via **`archive_item`** (PR5c–PR5f cutover series).
- **`sync_document_shared_fields_from_archive_item`** and related mirror-write helpers are **removed** with the columns.

**Historical note:** PR5a–PR5f described temporary compatibility mirrors on **`Document`**. Migration 0035 completes the optional “PR5g” schema cleanup referenced in PR5f.

## Archive browse card thumbnails — current state

**Decision:** Public archive browse cards (`/archive/`, category/event/tag discovery browse) render **stored JPEG thumbnails** when available; otherwise **CSS type-marker fallbacks**. Thumbnail and marker template branches are **mutually exclusive**.

### Visual preview by item type

| Item type | Thumbnail source | Browse presign key | Fallback marker |
|-----------|------------------|--------------------|-----------------|
| **PHOTO** | **`PhotoContent.thumbnail_file_key`** | `photos/{photo_content_id}/thumb_400.jpg` | `--photo` |
| **OCR_DOCUMENT (IMAGE)** | **`Document.thumbnail_file_key`** | `documents/{document_id}/thumb_400.jpg` | `--ocr` |
| **OCR_DOCUMENT (PDF)** | None (presign skipped) | N/A | `--ocr` |
| **MANUAL_TEXT** | None | N/A | `--manual` |

### Rules

- Browse cards **never** presign **`PhotoContent.original_file_key`**, **`Document.file_s3_key`**, or source-file keys for list previews.
- **`apply_photo_thumbnail_urls_to_browse_cards`** and **`apply_document_thumbnail_urls_to_browse_cards`** attach presigned GET URLs to **`ArchiveBrowseCard.thumbnail_url`** in **`_archive_browse_cards_for_items`**.
- Image OCR presigning requires **`doc_type=IMAGE`**; PDFs always use the marker even if **`thumbnail_file_key`** is set.
- Text preview (`card.preview_text`) is separate from the visual thumbnail/marker.
- Template: **`documents/archive/partials/item_list_cards.html`**. No browse-card JavaScript.

**Docs:** `docs/ai-context/photo-archive-items.md` (**Current authoritative state**)

## PHOTO and OCR image browse thumbnails — generation

**Decision:** Generate fixed-edge JPEG browse thumbnails at upload time (best-effort; does not fail the upload). PDF OCR documents are excluded.

### PHOTO

- **When:** After successful **`PhotoContent`** upload finalize (`upload_status=UPLOADED`).
- **Service:** **`generate_and_persist_photo_thumbnail`** in **`documents/services/photo_thumbnail.py`**.
- **Input:** Validated **`original_file_key`** from S3.
- **Output key:** **`photos/{photo_content_id}/thumb_400.jpg`** via **`build_photo_thumbnail_s3_key`**.
- **Persisted:** **`width`**, **`height`**, **`thumbnail_file_key`**, **`thumbnail_mime_type`**, **`thumbnail_size_bytes`** on **`PhotoContent`**.

### OCR image documents

- **When:** After upload complete/finalize transaction commits (**`schedule_document_thumbnail_after_upload`**).
- **Service:** **`generate_and_persist_document_thumbnail`** in **`documents/services/document_thumbnail.py`**.
- **Input:** First source page — **`DocumentSourceFile`** at **`order_index=0`** with non-empty **`file_s3_key`**.
- **Output key:** **`documents/{document_id}/thumb_400.jpg`** via **`build_document_thumbnail_s3_key`**.
- **Persisted:** **`first_page_width`**, **`first_page_height`**, **`thumbnail_*`** on **`Document`**.
- **PDF:** **`should_generate_document_thumbnail`** returns false.

### Shared encoder

- **`documents/services/image_thumbnail.py`** — max edge 400, EXIF-aware transpose, JPEG output.
- Worker (`run_worker.py`) does **not** generate browse thumbnails.

### Schema

- PHOTO thumbnail columns: migration **`0025_photocontent`**.
- Document thumbnail columns: migration **`0036_document_thumbnail_fields`** (`thumbnail_*`, **`first_page_width`**, **`first_page_height`**).

## Thumbnail backfill — operational commands

**Decision:** Provide supported, **idempotent** management commands for repair and catch-up thumbnail generation. These are **operational tooling**, not temporary one-off scripts. Completed production backfills do **not** make the commands obsolete.

| Command | Service module | Eligibility summary |
|---------|----------------|---------------------|
| **`backfill_photo_thumbnails`** | **`photo_thumbnail_backfill.py`** | `UPLOADED` + non-empty **`original_file_key`** + empty **`thumbnail_file_key`** |
| **`backfill_document_thumbnails`** | **`document_thumbnail_backfill.py`** | `doc_type=IMAGE` + `UPLOADED` + empty **`thumbnail_file_key`** + valid primary source at `order_index=0` |

**Modes:** Default is **dry-run** (report only). **`--commit`** generates and persists thumbnails (S3 + DB). Both support **`--limit`**, **`--json`**, and single-id filters (`--photo-id`, `--document-id`). Re-runs skip rows that already have thumbnails.

**Delegation:** Both commands call the same **`generate_and_persist_*_thumbnail`** functions used by upload-time generation.

## Document S3 orphan cleanup — `cleanup_document_s3_orphans`

**Decision:** Provide an operational command to audit and optionally delete unreferenced S3 objects under **`documents/`**.

**Protected references (database-backed, not filename exemptions):**

- **`Document.file_s3_key`**
- **`Document.thumbnail_file_key`**
- **`DocumentSourceFile.file_s3_key`**
- **`TranskribusSnapshotPage.page_xml_s3_key`** when the stored key exactly equals the deterministic key for that row’s `(document_id, snapshot_id, page_index)`:
  - **`READY`**: always
  - **`PENDING_UPLOAD`**: only while snapshot `created_at` is within **`TRANSKRIBUS_SNAPSHOT_PENDING_ORPHAN_PROTECTION_HOURS` (24)**
  - **`FAILED`**: never
  (see “Transkribus transcript snapshot storage”)

**Behavior:**

- Default is **dry-run**. **`--commit`** deletes listed orphan candidates (with age/limit filters).
- Referenced thumbnail keys are **never** deleted while the DB row still points at them.
- Unreferenced thumbnail derivatives under **`documents/`** remain valid orphan candidates.
- Snapshot PAGE XML for **`FAILED`** attempts, **stale `PENDING_UPLOAD`** (>24h), and mismatched keys is intentionally **not** treated as referenced, so residual objects remain age-eligible orphan candidates.
- Objects under **`photos/`** are **outside** this command’s scope (use **`cleanup_photo_s3_orphans`** for PHOTO objects).

**Service:** **`documents/services/document_s3_orphan_cleanup.py`**

## OCR upload UI — gallery-first incremental flow (current)

**Decision:** The first-party admin upload UI (`documents/templates/documents/upload/`) uses a **gallery-first, incremental** image workflow. This supersedes the historical PR6 multi-select batch flow (2–30 files selected once) for the current product UI.

### Current behavior

- **Mobile:** Gallery-first — primary control «הוספת עמוד מהגלריה»; users photograph pages in the device camera app, return to the site, and add images from the gallery. Each image uploads immediately.
- **Direct in-browser camera capture removed** — the file input has no `capture` attribute (removed because it conflicted with multi-select; incremental gallery flow replaced it).
- **Image documents:** 1–35 pages per document (`MULTI_IMAGE_MAX_FILES=35` in **`source_files.py`** and upload script). Pages upload **incrementally** via **`POST /api/uploads/create/`** with **`incremental: true`**, then per-part complete endpoints; finalize when ready.
- **PDF:** Separate single-file path — not mixed with gallery images.
- **EXIF orientation:** Server-side normalization for supported uploaded images via **`normalize_uploaded_image_exif_in_s3`** (`documents/services/exif_orientation.py`) on upload completion paths.

### Historical note

- **Multi-image upload — admin UI (PR6)** documented 2–30 files selected in one batch with immediate multi-part upload. The current UI uses incremental page-by-page upload with a **35**-page cap.
- **Multi-image upload — backend API contract (PR3)** batch `files[]` create mode remains in the API for compatibility; the current first-party UI primarily uses the incremental draft flow.

**Docs:** `docs/ai-context/unified-ocr-upload-flow.md` (API history); upload templates under `documents/templates/documents/upload/`.

## Transkribus transcript snapshot storage (PAGE XML persistence)

**Decision:** Persist already-fetched / already-selected Transkribus PAGE XML as an immutable `TranskribusTranscriptSnapshot` with normalized page/line rows in PostgreSQL and raw PAGE XML objects in S3. This layer does **not** fetch Transkribus metadata, select transcripts, activate bindings, or update `DocumentTextResult`.

**Service:** `documents/services/transkribus_snapshot_storage.py` → `store_transkribus_transcript_snapshot(...)`.

**Parser:** Reuses pure `parse_document_pages_for_snapshot` / `PARSER_VERSION` from `transkribus_snapshot_parser.py`. Storage must not reimplement geometry or text ordering.

**S3 key contract (deterministic, no user filenames):**

`documents/{document_id}/transkribus/snapshots/{snapshot_id}/pages/{page_index}.page.xml`

Content-Type: `application/xml` via `put_object_bytes`.

**Lifecycle (no PostgreSQL↔S3 cross-system atomicity):**

1. Validate inputs; parse all pages and fingerprints before persistent writes.
2. If a `READY` snapshot already exists for `(document, parser_version, raw_xml_fingerprint)`, return `REUSED_EXISTING` (no upload, no duplicate rows).
3. Otherwise create `PENDING_UPLOAD` snapshot + page/line rows with final S3 keys in a **short** DB transaction (no network I/O while holding the transaction).
4. Upload every PAGE XML object outside the DB transaction.
5. Only after all uploads succeed, transition to `READY`.
6. On upload failure: never `READY`; best-effort mark `FAILED` (DB update failures are attached as secondary `state_update_errors` and must not replace the primary upload error); best-effort delete objects uploaded in this attempt (caller-owned accumulator retains successful keys even when a later page upload raises); preserve the primary upload error (cleanup failures reported separately).
7. Concurrent identical finalization: recheck for an existing identical `READY` before finalizing; on race loss, clean up this attempt’s S3 objects, best-effort mark the losing `PENDING_UPLOAD` row `FAILED`, and return `REUSED_CONCURRENT_WINNER`. Do not overwrite either snapshot’s immutable content.

**Idempotency rules:**

- Same provider `tsId` with different PAGE XML → new snapshot (provider identity is observational, not unique).
- Dedup key is `(document, parser_version, raw_xml_fingerprint)` for `READY` only (`uniq_tr_snap_ready_raw_xml`).
- Canonical-text hash alone must **not** deduplicate (geometry may change with identical text).
- A previously stored identical raw snapshot may be reused even if not currently active.
- `FAILED` attempts do not block later retries.

**Orphan cleanup integration (`cleanup_document_s3_orphans`):**

- Protected references additionally include `TranskribusSnapshotPage.page_xml_s3_key` when the stored key **exactly equals** the deterministic key built from `(snapshot.document_id, snapshot_id, page_index)`:
  - **`READY`**: always protected under that exact-identity rule.
  - **`PENDING_UPLOAD`**: protected only while the snapshot `created_at` is newer than **`TRANSKRIBUS_SNAPSHOT_PENDING_ORPHAN_PROTECTION_HOURS` (24)**. Stale PENDING keys are **not** protected and may become orphan candidates under the command’s existing object-age safeguards. Stale PENDING **DB status is not changed** by orphan cleanup.
  - **`FAILED`**: never protected.
- A syntactically valid key that belongs to another document/snapshot/page is **not** protected.
- Classic `DOCUMENT_S3_REFERENCE_FIELDS` (Document source/thumbnail + DocumentSourceFile) are unchanged.
- Dry-run-by-default and `--commit` behavior preserved. `photos/` remains outside this command’s prefix scope.

**Document deletion:** Follow existing OCR document convention — ORM `CASCADE` removes snapshot/page/line rows; S3 objects are **not** deleted synchronously. After cascade, snapshot PAGE XML keys are no longer referenced and become age-eligible orphan candidates (same pattern as document source/thumbnail objects). Immediate after-commit S3 delete (photo path) is intentionally **not** added for OCR documents in this PR.

**Out of scope (deferred at storage PR time; see “Transkribus automatic snapshot integration” below for what shipped later):** corrected-current fetch/selection, search/hover UI, backfill, stale-PENDING status automation.

**Known limitation:** PostgreSQL and S3 are not a single atomic unit. Crash between successful uploads and `READY` transition can leave `PENDING_UPLOAD` rows with objects in S3. Those keys are orphan-protected only for the first **24 hours** after snapshot `created_at`; after that window they may be cleaned as orphans (subject to the command’s object-age threshold) even while the DB row remains `PENDING_UPLOAD`. Ops / retry paths must still account for stuck PENDING rows; this PR does not add a stale-PENDING management command.

## Transkribus automatic snapshot integration (worker binding)

**Decision:** On successful Transkribus recognition, persist an `AUTOMATIC_HTR` READY snapshot from the **exact** PAGE XML selected by production `pick_transcript` / `ordered_transcript_selections` (jobId+modelId, then job-only, then model-only — unchanged), then atomically write `DocumentTextResult` rows + `TranskribusTextResultBinding` and only then `mark_succeeded`.

**Run→snapshot association:** `TranskribusTranscriptSnapshot.transkribus_run` remains **origin/provenance** of first creation. Storage may reuse an identical READY snapshot by `(document, parser_version, raw_xml_fingerprint)`. Every automatic consuming run records durable use via **`TranskribusRunAutomaticSnapshot`** (`OneToOne` run → FK snapshot, plus `mapping_trusted` and `review_reasons`). Multiple runs may share one immutable READY snapshot. Association enforces same-document + READY + `AUTOMATIC_HTR`. The run→snapshot link is **immutable**: create if missing; same-snapshot retry is idempotent (safe `mapping_trusted` upgrade / empty `review_reasons` fill only); reassignment to a different snapshot raises. Resume, local completion, idempotency, and SUCCEEDED checks resolve the snapshot through this association — **not** origin FK alone.

**Lifecycle:**

1. After `recognition_job_id` is durable, keep `TranskribusRun` at `RECOGNITION_STARTED` through snapshot storage and association.
2. Store snapshot via `store_transkribus_transcript_snapshot` (S3 outside DB transactions). Reuse identical READY snapshots by existing fingerprint idempotency; **do not** mutate READY fields (including `hover_eligible`) on reuse.
3. Worker calls `complete_transkribus_local_success` with lock order: **Document → TranskribusRun → TranskribusRunAutomaticSnapshot → snapshot → DTR rows**; write/update SOURCE_TEXT (+ Hebrew HEBREW_TEXT mirror); bind `SNAPSHOT_SOURCE` / `HEBREW_MIRROR`; require hash == `canonical_text_sha256`; roll up processing state; `mark_succeeded` — all in one transaction.
4. Transient snapshot/S3 failure **or** transient re-fetch during resume (durable `recognition_job_id`) → `TranskribusLocalPersistenceRetryableError`: do **not** mark run FAILED, do **not** persist OCR failure DTR, worker returns `False` (no SQS ack).
5. Binding/DB failure rolls back the local-success transaction; SQS not ack’d; resume from durable recognition / associated READY snapshot without `start_pylaia_recognition`.

**Hover / mapping trust:** Upload associations set `mapping_trusted=True`. EXISTING_SERVER associations set `mapping_trusted=False`. New EXISTING_SERVER snapshots may be created with `hover_eligible=False`; reused READY snapshots keep their original hover eligibility. End-to-end hover eligibility must not be claimed for an untrusted EXISTING_SERVER association (binding-time / association-level check preferred).

**Review reasons:** Engine reasons such as `EMPTY_TRANSCRIPT_PAGE` are stored on `TranskribusRunAutomaticSnapshot.review_reasons` and reconstructed from snapshot page stats when needed. READY-snapshot resume must not drop them.

**Resume (before upload / new recognition):**

- Upload mode: use `find_blocking_upload_run`; resume `RECOGNITION_STARTED` with durable ids, or `SUCCEEDED` when an association exists (idempotent duplicate delivery when bindings are structurally complete, including human-edited-after-bind). Historical `SUCCEEDED` without association is **not** interrupted new-pipeline work.
- EXISTING_SERVER: resume only `RECOGNITION_STARTED` or **demonstrably incomplete** `SUCCEEDED` (association present, bindings not structurally complete). Fully completed EXISTING_SERVER runs are **not** selected — SQS carries only `document_id` (no attempt id), so duplicate delivery vs a new requested processing cannot be distinguished; treating completed runs as no-ops would make every future EXISTING_SERVER request a no-op. **Limitation:** fully completed EXISTING_SERVER duplicate idempotency is deferred until request identity exists.
- Associated READY snapshot → reuse canonical text + durable review reasons (no recognition restart). Else re-fetch finished job via `complete_pylaia_transcription_after_job` only.

**Page index convention:** Production `PageImage.page_index` and snapshot `page_index` are **1-based**. Trusted `page_index_to_page_nr` keys must be dense ``0..N-1`` (converted ``+1`` at the snapshot boundary) or dense ``1..N`` (preserved). Gaps, mixed bases, and duplicate keys after integer coercion are rejected. Do not mass-rewrite unrelated fixtures.

**Bindings inspection:** Distinguish never-completed (missing binding, or binding for a different snapshot) from completed-with-later-human-edit (current DTR text/revision drifts from otherwise-valid original binding metadata) from corrupt original metadata (`bound_text_sha256` ≠ snapshot canonical hash, `bound_source_revision < 1`, role mismatch, or Hebrew SOURCE/HEBREW bound revisions disagree). Duplicate delivery must not overwrite human edits. Corrupt binding metadata is never an idempotent completed no-op.

**Revisions (automatic only):** create at `source_revision=1`; byte-identical text does not bump; changed text increments SOURCE and sets Hebrew `based_on_source_revision`; rows stay `NEEDS_REVIEW` / `UNVERIFIED` (no verified-edit services).

**Still out of scope:** corrected/current sync orchestration, staff attempt/activation UI, search/hover UI, backfill, stale-PENDING recovery, SQS/DLQ redesign, non-Transkribus engines, request-identity idempotency for EXISTING_SERVER.

## Transkribus corrected-current transcript selection (v1)

**Decision:** Staff corrected-current sync (future PRs) must select Transkribus PAGE XML using a **separate** pure selector in `documents/services/transkribus_corrected_current_selection.py`. It must **not** call or extend automatic HTR `pick_transcript` / `ordered_transcript_selections`.

**Verified production findings (read-only transcript-version audits, Documents 247, 249, 280):**

- On **every audited page** across Documents **247**, **249**, and **280**, the provider exposed **exactly one** transcript in `tsList`. **No multi-transcript page was observed** in those production audits.
- All three documents therefore **support the v1 sole-transcript selection rule** in production as audited.
- Document **249** showed that the **sole** transcript’s metadata did **not** match stored recognition `jobId`/`modelId` (automatic `pick_transcript` / geometry-audit failure class). That proves a **metadata mismatch**, not human editing by itself.
- Automatic job/model selection remains **insufficient** for corrected-current import when the sole provider transcript lacks or does not match the stored recognition job/model metadata, as observed for Document **249**; v1 corrected-current selection intentionally **does not** use job/model matching.
- Remote transcript **`IN_PROGRESS`** may appear on otherwise selectable rows; it is a **warning**, not a selection failure.

**Current behavior (v1 selector rule):**

- Per page: select the sole transcript when `len(tsList) == 1` and `tsId` is non-empty.
- **No** job/model match, **no** `ORIGINAL_HTR` / `NON_MATCHING_VERSION` classification, **no** timestamp / `tsId` / list-order / `isCurrent` / `isLatest` fallbacks.
- Per page: refuse with precise errors when `len == 0`, `len > 1`, or the single row lacks `tsId`. Refusal when `len > 1` is a **conservative v1 safety rule** (provider shape and unit-test fixtures); it was **not** triggered by the 247/249/280 production audits above.
- Per document: if **any** page refuses, the whole document selection is refused (orchestration must not partially sync). Empty `pages` input is invalid and raises **`ValueError`** (caller must supply mapped pages).
- `IN_PROGRESS` (case/space normalized) sets a fixed `in_progress_warning` on the selection; it does not refuse.

**Deferred (not this PR):** HTTP fetch, snapshot storage, staff UI, activation, hover, backfill.

## Transkribus corrected-current sync provenance (schema)

**Decision:** Persist staff corrected/current sync as **`TranskribusCorrectedCurrentSyncAttempt`** plus per-page **`TranskribusCorrectedCurrentSyncPage`** rows (migration **0041**). Schema only — no orchestration, HTTP/S3, activation, or UI in this PR.

**Attempt contract:**

- Required **`document`**, trusted **`UPLOAD_CREATED`** **`transkribus_run`** (same document, non-empty **`page_index_to_page_nr`**), and **`status`** (`STARTED` | `COMPLETED` | `REFUSED` | `FAILED`). Multiple attempts per document are allowed. **`initiated_by`** is nullable with **`SET_NULL`** (required when the future creation service starts an attempt; historical rows survive user deletion), aligned with snapshot audit actor fields.
- **`COMPLETED`** links a **`resolved_snapshot`** that must be **`READY`** on the same document and records **`storage_outcome`** (`CREATED` | `REUSED_EXISTING` | `REUSED_CONCURRENT_WINNER`) aligned with snapshot storage semantics (including reuse of an existing **`AUTOMATIC_HTR`** READY row without mutating **`source_kind`**). DB check constraints enforce declared **`status`** / non-null **`storage_outcome`** values only.
- **`REFUSED`** / **`FAILED`** terminal shapes are enforced with DB check constraints; **`FAILED`** requires non-null, non-empty **`failure_code`**. Cross-row lifecycle transitions and terminal immutability belong in a future service (not model save hooks beyond run/snapshot integrity checks).
- **`transkribus_run`** and **`resolved_snapshot`** use **`RESTRICT`** so provenance blocks deleting runs or snapshots still referenced independently; **`document`** CASCADE removes attempts (and pages), and existing **`TranskribusRun`** / **`TranskribusTranscriptSnapshot`** CASCADE from **`Document`** removes runs and snapshots when the document is deleted.

**Page contract:**

- Per attempt: 1-based **`page_index`** / **`page_nr`**, outcome **`SELECTED`** (requires **`transcript_ts_id`**; optional remote status + **`in_progress_warning`**) or **`REFUSED`** (requires bounded selection error fields). **`selection_error_code`** on REFUSED rows must be one of **`ZERO_TRANSCRIPTS`**, **`MULTIPLE_TRANSCRIPTS`**, or **`MISSING_TS_ID`** (same vocabulary as **`transkribus_corrected_current_selection`**). DB constraints enforce declared **`outcome`** and non-empty **`selection_error_code`** values. Mutual exclusion enforced in the DB.

**Activation (future):** Staff activation must reference an **explicit `COMPLETED` attempt id**, verify page **`transcript_ts_id`** values against the attempt’s **`resolved_snapshot`** pages, and must **never** infer “latest” attempt. No URLs, tokens, raw XML, provider user metadata, or generic JSON on these tables.

**Still out of scope:** staff UI, explicit activation against `DocumentTextResult` / bindings, search/hover UI, queues/commands, backfill.

## Transkribus corrected-current sync orchestration (service)

**Decision:** Staff corrected/current import is orchestrated in `documents/services/transkribus_corrected_current_sync.py` via `run_corrected_current_transkribus_sync(...)`. Transport (Trp login, metadata GET, transcript XML GET) and snapshot S3 uploads run **outside** `transaction.atomic()`; DB writes use short transactions with `select_for_update` on the sync attempt for terminal transitions.

**Flow:**

1. Resolve **`Document`**, trusted **`UPLOAD_CREATED`** run (`resolve_audit_transkribus_run`), and dense **`normalize_page_index_to_page_nr`** mapping.
2. Create a new **`STARTED`** `TranskribusCorrectedCurrentSyncAttempt` (required **`initiated_by`** at service entry).
3. Fetch pages metadata once; build **`CorrectedCurrentPageInput`** rows; call **`select_corrected_current_transcripts_for_document`**.
4. **Refused:** persist REFUSED page rows + terminal **`REFUSED`** (no PAGE XML fetch, no snapshot storage).
5. **Selected:** persist SELECTED page rows while **`STARTED`**; fetch selected transcript XML; **`snapshot_pages_from_upload_mapping`** → **`store_transkribus_transcript_snapshot`** with **`source_kind=CORRECTED_CURRENT_SYNC`** and **`hover_eligible=None`** (parser-derived geometry eligibility — orchestration does not force hover); verify READY snapshot page_index/page_nr/tsId parity with attempt pages; terminal **`COMPLETED`** with exact **`SnapshotStorageOutcome`** value.
6. **Failure after attempt creation:** best-effort **`STARTED` → `FAILED`** with fixed public **`failure_code`** / **`failure_message`** (no raw provider URLs, tokens, or external exception text in DB or raised messages). Raw external exception **messages and tracebacks are not logged or chained** into **`CorrectedCurrentSyncError`**; server-side logs record **`failure_code`**, **`attempt_id`**, and external **exception class name** only. Retain SELECTED page rows where applicable; raise **`CorrectedCurrentSyncError`** with safe text (**`raise … from None`**).

**Terminal rules:** Only **`STARTED` → `COMPLETED` | `REFUSED` | `FAILED`**. Idempotent retry when status and payload already match; never overwrite a different terminal outcome (conflicts raise **`CorrectedCurrentSyncTerminalConflictError`**).

**Run resolution:** Occurs before attempt creation; **`RUN_RESOLUTION_FAILED`** uses **`attempt_id=None`** (no persisted failed attempt).

**Explicit non-goals (unchanged):** automatic **`pick_transcript`**, **`TranskribusRunAutomaticSnapshot`**, **`DocumentTextResult`**, bindings, **`processing_state_user`**, activation UI.

## Transkribus corrected-current sync execution surface (v1 command)

**Decision:** The v1 manual execution surface for `run_corrected_current_transkribus_sync(...)` is a **worker-environment** Django management command: **`sync_transkribus_corrected_current`** (`--document-id`, `--initiated-by-user-id`). It reads Transkribus session + bearer credentials from the worker env (same pattern as existing Transkribus audit commands), requires an **active staff** initiating user, and prints only safe attempt/status/snapshot/outcome/`failure_code` fields.

**Deferred:** a **dedicated SQS message type** on the **existing** worker/queue until a staff enqueue UI exists. That message must remain **separate from `PROCESS_DOCUMENT`** (not nested as an `operation`). A **separate queue/worker** is **not** justified currently (worker already has Transkribus credentials and hosts longer recognition work). **PR1 (schema only)** adds the **`TranskribusCorrectedCurrentSyncRequest`** model and constant **`SYNC_TRANSKRIBUS_CORRECTED_CURRENT`**; sender/worker/UI remain deferred — see **“Transkribus corrected/current sync queue foundation (PR1)”** below.

**Operational follow-ups (not this PR):** duplicate/concurrent manual invocations (each run creates a new attempt by design); stale **`STARTED`** recovery after process kill; staff UI / activation / search / hover.

## Transkribus corrected-current sync staff preview (read-only)

**Decision:** Ship a **read-only** staff application surface for corrected/current sync attempt history and text preview (not Django admin; not OCR review mutation actions). URLs are document-scoped under `/api/ui/admin/documents/<id>/transkribus-corrected-current-sync/` (+ `/<attempt_id>/`). Access uses the existing staff-page gate (`login_required` + `_require_admin_page` / `is_document_admin`). Staff may inspect private documents; mismatched document/attempt pairs return **404**.

**Comparison baseline:** Preview diffs snapshot `canonical_text` against the latest **displayable SOURCE_TEXT** only via `resolve_displayable_source_text_result` (SUCCEEDED then NEEDS_REVIEW; **never** falls back to HEBREW_TEXT). Diff uses `render_transcription_diff_html(source_text, snapshot.canonical_text)`. When no displayable SOURCE_TEXT exists, show an explicit empty state and skip the diff.

**Staff UI presentation (read-only polish):** List/detail copy is Hebrew-first for staff without exposing internal model names in primary content. Technical identifiers (`source_kind`, `storage_status`, `geometry_capability`, `hover_eligible`, raw enums, `tsId`, `page_index`/`page_nr`, failure/selection codes, DocumentTextResult ids) live in a collapsed **`פרטים טכניים`** `<details>` block. Comparison remains SOURCE_TEXT-only in the backend even though the UI no longer names `SOURCE_TEXT` / `snapshot` in primary headings.

**Non-goals for the preview surface itself:** running sync from the web; bindings; translation; search/hover; SQS/worker/command changes; selector/orchestration/storage; models/migrations; stale STARTED detection/recovery; global nav backlog. Explicit activation is a separate POST surface (see activation UI PR2 below), not part of the read-only list.

## Transkribus corrected-current activation (service PR1)

**Decision:** Explicit staff activation of a **COMPLETED** corrected/current sync attempt into canonical `DocumentTextResult` is implemented in `documents/services/transkribus_corrected_current_activation.py` via `activate_corrected_current_sync_attempt(...)`. Sync remains provenance-only; this service is the first writer that binds corrected/current snapshot text into displayed/searchable rows.

**Required inputs (no inference):** `document_id`, `attempt_id`, `source_text_result_id` (exact preview SOURCE_TEXT row), `activated_by` (active document-admin via `is_document_admin`; missing/anonymous/inactive/non-admin → `ACTOR_UNAUTHORIZED` before any mutation, including before `ALREADY_ACTIVE`), `expected_source_revision`, `expected_source_sha256`. Never select “latest” attempt or resolve target from `run.engine_runtime`. Persisted actor is always written to binding `bound_by` and any activation-created `DocumentTextResultEdit.editor`.

**Eligibility:** Attempt belongs to document and is **COMPLETED**; `resolved_snapshot` same document and **READY**; SELECTED attempt pages match snapshot pages on `page_index` / `page_nr` / `transcript_ts_id` (reuses sync `_verify_snapshot_matches_attempt_pages`); target is SOURCE_TEXT on the document; expected revision/SHA match the locked row. Reused **AUTOMATIC_HTR** READY snapshots are allowed when attempt provenance matches. Empty/whitespace-only canonical text is rejected; stored `canonical_text_sha256` must equal `sha256(canonical_text)` before write. Persist `canonical_text` byte-for-byte. `hover_eligible` is **not** required. Do **not** touch `TranskribusRunAutomaticSnapshot`.

**Hard blocks (no override in PR1):** target SOURCE `VERIFIED`; paired Hebrew mirror `VERIFIED` (Hebrew docs); existing binding with trustworthy original metadata whose current text/revision drifted; SOURCE `DocumentTextResultEdit` history without a trustworthy binding that matches the current text/revision baseline (`HUMAN_EDITED_BLOCKED`). Trustworthy prior binding requires same-document READY snapshot, non-empty `canonical_text_sha256` equal to `sha256(canonical_text)`, `bound_text_sha256` equal to that verified hash, valid role, and `bound_source_revision >= 1`. Malformed/untrustworthy bindings (including non-READY or broken canonical integrity) plus edit history also block.

**Write semantics:** Preserve existing non-VERIFIED `status` / `verification_status` (activation does not verify). Hebrew: when SOURCE bytes change, mirror SOURCE↔HEBREW text and revision link and create one SOURCE `DocumentTextResultEdit`; when SOURCE already equals canonical but Hebrew text/`based_on_source_revision` needs repair, update Hebrew only (no SOURCE revision bump, no edit row). Create `SNAPSHOT_SOURCE` + `HEBREW_MIRROR` bindings. Non-Hebrew: SOURCE only; do not change/delete/translate/enqueue HEBREW (stale via `based_on_source_revision` mismatch). Binding helper accepts optional `bound_by`. Result fields: `source_text_changed` (SOURCE bytes changed) and `hebrew_mirror_updated` (Hebrew text and/or `based_on` updated). Outcomes: `ALREADY_ACTIVE` when SOURCE (and required Hebrew) are already fresh for this snapshot — checked **before** preview-token validation so the same original request tokens replay cleanly; otherwise stale preview tokens still block before any write. `APPLIED` covers SOURCE text apply, Hebrew mirror-only repair, and binding-only repair. Binding failures raise `BINDING_FAILED` without chaining provider/local exception text (`from None`); the surrounding atomic transaction rolls back SOURCE/Hebrew writes, audit rows, and any partial bindings.

**Transaction:** One short `atomic()`; lock order Document → Attempt → Snapshot → DTR rows (pk order) → bindings; no HTTP/S3/Gemini/SQS. Does **not** update `Document.processing_state_user` in this PR.

**Still out of scope for the service PR:** staff activation UI/routes (see PR2); translation enqueue; search/hover; processing-state rollup; selector/sync/storage/automatic-completion/worker changes.

## Transkribus corrected-current activation UI (PR2)

**Decision:** Staff activate a **COMPLETED** corrected/current attempt from the existing attempt **detail** page only, via a dedicated POST route that calls `activate_corrected_current_sync_attempt(...)`. GET detail remains read-only. The attempts **list** has no activation controls. Activation does **not** run Transkribus sync, S3, Gemini, or SQS.

**Route / auth:** `POST /api/ui/admin/documents/<doc_id>/transkribus-corrected-current-sync/<attempt_id>/activate/` (`corrected-current-sync-attempt-activate`). Same staff gate as preview (`login_required` + `_require_admin_page` / `is_document_admin`). CSRF required; non-POST rejected. Document/attempt ownership lookup runs immediately after the admin gate (before confirmation or baseline parsing); nonexistent attempt or document/attempt mismatch → **404** with no queued message and no service call (parity with GET detail). POST/Redirect/GET back to the detail page on success and handled rejection.

**Exact preview baseline:** The form submits the exact SOURCE_TEXT baseline shown in preview: `source_text_result_id`, `expected_source_revision`, `expected_source_sha256` (from `compute_sha256_hex(source_row.text)`). The view does not infer latest attempt/engine/result. Missing confirmation checkbox does **not** call the service. Missing/invalid baseline fields redirect with a safe Hebrew message without calling the service when values cannot be parsed. The service remains authoritative under locks.

**Form availability (GET):** Render the activation form only when the attempt is **COMPLETED**, `resolved_snapshot` is **READY**, and a concrete displayable SOURCE_TEXT baseline exists. If COMPLETED+READY but no baseline, show a short non-technical explanation and no button. Do **not** claim client-side eligibility for VERIFIED/human-edit cases — the service decides at POST time.

**UI copy:** Hebrew-first staff wording. Restrained warning that activation replaces the currently displayed transcription with the Transkribus corrected/current text, does not mark text as human-verified, and blocks verified/protected human-edited text. Explicit confirmation checkbox required. Action label: **`החלפת התעתוק המוצג בגרסת Transkribus`**. Primary UI does not expose enums, hashes, revisions, result IDs, engine names, or error codes (collapsed technical details unchanged).

**Messages:** Map stable `CorrectedCurrentActivationErrorCode` values to concise Hebrew staff messages (stale preview; VERIFIED / human-edited block; unauthorized; attempt/snapshot no longer eligible; binding/internal safe failure). Never display raw exception text, provider details, hashes, IDs, or traceback. Success distinguishes three **APPLIED** shapes — SOURCE text changed; Hebrew mirror updated without SOURCE text change; binding-only repair with no displayed text change — plus **ALREADY_ACTIVE** idempotent no-op. After redirect, detail re-renders the current baseline/diff.

**Still out of scope:** activation service semantics changes; models/migrations; selector/sync/storage/parser; automatic completion; OCR routing/worker/upload; search/hover; public archive pages; navigation; processing-state rollup; translation enqueue.

## Transkribus corrected/current sync queue foundation (PR1 — schema/contract only)

**Decision:** Staff-triggered corrected/current sync from the website will use **Design B**: a durable **`TranskribusCorrectedCurrentSyncRequest`** row plus a top-level SQS message on the **existing** worker queue (not a `PROCESS_DOCUMENT` **`operation`**). PR1 ships **schema, migration, model tests, message-type constant, and docs only** — no enqueue sender, worker branch, web UI, service correlation parameters, visibility changes, or recovery commands.

**Delivery guarantee (planned):** **At-most-once provider orchestration per Request**, not exactly-once. SQS is at-least-once; lease-token fencing plus atomic Request↔Attempt correlation before provider I/O prevent duplicate Transkribus/S3 work for the same Request. Terminal Request statuses are **immutable** (no automatic rewrite from `RECOVERY_REQUIRED` to `FAILED` and back).

**Request model (migration 0044):** FK **`document`** (CASCADE), **`initiated_by`** (`SET_NULL`), **`status`** (`QUEUED` | `RUNNING` | `RECOVERY_REQUIRED` | `COMPLETED` | `REFUSED` | `FAILED` | `ENQUEUE_FAILED`), nullable unique **`attempt`** OneToOne (`RESTRICT` — preserves provenance; delete the request or document before deleting a referenced attempt), **`lease_token`** / **`lease_expires_at`**, safe **`failure_code`** / **`failure_message`**, timestamps including **`last_enqueued_at`**. DB partial unique: **at most one active Request per document** for `QUEUED`, `RUNNING`, `RECOVERY_REQUIRED`, and `ENQUEUE_FAILED`. Lifecycle check constraints enforce queue shapes, terminal rows without active lease fields, and **`FAILED`** requiring non-empty **`failure_code`**.

**`RECOVERY_REQUIRED` (non-terminal):** entered when **`RUNNING`** has a linked **`STARTED`** Attempt whose execution exceeded the recovery threshold. Retains **`lease_token`** as fencing identity for a potentially late original worker, but **`lease_expires_at` must be null** (no active lease expiry). Never re-enters provider orchestration. A later terminal linked Attempt may move the Request to **`COMPLETED`** / **`REFUSED`** / **`FAILED`**. Otherwise a future explicit staff POST may abandon/retry (later PR). **`RECOVERY_REQUIRED`** counts toward the one-active-request rule.

**SQS message type constant (PR1 only):** **`SYNC_TRANSKRIBUS_CORRECTED_CURRENT`** in `documents/services/sqs.py`. Future payload carries **`request_id`** only (no credentials). Not nested under **`PROCESS_DOCUMENT`**.

**Unchanged in PR1:** `run_corrected_current_transkribus_sync(...)` and **`sync_transkribus_corrected_current`** management command (no `SyncRequest` parameter yet). Activation remains manual and separate. Web enqueue remains **disabled**; nothing is operationally enabled by PR1.

**Planned later PRs (not PR1):** ~~worker claim/fencing + service correlation + visibility extension~~ → **PR2 implemented**; ~~enqueue service~~ → **PR3 implemented** (see enqueue entry below); staff UI POST + feature gate; ops reconcile/requeue command.

**Planned v1 timing constants (docs only until worker PR):** execution lease **45 minutes**, one-shot SQS visibility extension **45 minutes**, linked **`STARTED`** recovery threshold **60 minutes** — conservative for up to ~30-page documents; tune after production measurements.

## Transkribus corrected/current sync worker (PR2 — claim/fencing/correlation)

**Decision / implemented:** Worker execution for **`SYNC_TRANSKRIBUS_CORRECTED_CURRENT`** with lease fencing and atomic Request↔Attempt correlation. Delivery guarantee: **at-most-once provider orchestration per Request**, with **idempotent terminal Request reconciliation** (not exactly-once SQS delivery).

**Service correlation:** `run_corrected_current_transkribus_sync(..., sync_request_id=None, lease_token=None)`. Management-command path omits both (unchanged). Worker path passes both. Before any Transkribus HTTP or S3 I/O, one short transaction locks the Request, requires **`RUNNING`** + matching **`lease_token`** + no linked Attempt, creates **`STARTED`** Attempt, and links it. Stale/fenced workers raise **`CorrectedCurrentSyncFencedOutError`** with no Attempt, no provider I/O, and no Request mutation.

**Worker handler:** `handle_sync_transkribus_corrected_current` in `documents/services/transkribus_corrected_current_sync_worker.py`, dispatched from `run_worker._process_message` **before** **`PROCESS_DOCUMENT`**. Payload validates **`request_id`** (int); unknown top-level types still ack-discard as before.

**Claim / reclaim / defer:**

| Observation | Action |
|-------------|--------|
| Terminal Request | No-op ack |
| Linked terminal Attempt | Reconcile Request to matching terminal status; ack; no provider I/O |
| Linked **`STARTED`**, age &lt; 60m | Never rerun; defer (no ack); visibility **2 minutes** |
| Linked **`STARTED`**, age ≥ 60m | Request → **`RECOVERY_REQUIRED`** (keep **`lease_token`**, clear **`lease_expires_at`**); ack; no provider rerun |
| `attempt_id` null, **`RUNNING`**, lease fresh | Defer (no ack); visibility **2 minutes** |
| `QUEUED` / `ENQUEUE_FAILED`, or stale **`RUNNING`** with null Attempt | Claim/reclaim: rotate **`lease_token`**, lease **45 minutes**; one-shot SQS visibility **45 minutes**; run service once |

**Late legitimate worker:** After **`RECOVERY_REQUIRED`**, the original worker that still holds the fencing token may terminalize the Request from its terminal Attempt (Attempt remains source of truth on any observe).

**Timing constants (implemented):** execution lease **45m**; SQS visibility after claim **45m**; competing/in-progress defer **2m**; linked **`STARTED`** recovery threshold **60m**.

**Unchanged / still out of scope for PR2:** ~~enqueue sender~~ → **PR3**; IAM/CDK; web view/route/button; feature gate; automatic activation; search; transcript selection/storage; DTR/bindings/processing-state; ops reconcile command; management command behavior when correlation args omitted.

## Transkribus corrected/current sync enqueue (PR3 — service only)

**Decision / implemented:** `enqueue_transkribus_corrected_current_sync(*, document_id, initiated_by)` in `documents/services/transkribus_corrected_current_sync_enqueue.py`, plus `send_sync_transkribus_corrected_current_message(request_id)` in `documents/services/sqs.py`. Staff UI POST was deferred at PR3 time and is now implemented (see staff enqueue UI entry). Feature gate, IAM/CDK, recovery/requeue command, and worker changes remain out of scope for the enqueue-service PR.

**Document-lock send-right:** Under a short `Document` `select_for_update` transaction, only (1) the caller that **creates** a new **`QUEUED`** Request, or (2) the caller that atomically transitions **`ENQUEUE_FAILED → QUEUED`**, receives local send right. Existing **`QUEUED`**, **`RUNNING`**, and **`RECOVERY_REQUIRED`** never resend. `SendMessage` runs **after commit**, never inside a DB atomic block, and **not** via `transaction.on_commit` as an outbox.

**Post-send CAS finalization** (never regresses worker-owned / terminal state; never writes lease or `attempt`):

- Success: `UPDATE last_enqueued_at` only where `status=QUEUED` ∧ `lease_token IS NULL` ∧ `attempt_id IS NULL`.
- Failure / unknown: `UPDATE status=ENQUEUE_FAILED` (+ safe `failure_code` / `failure_message`) under the same predicate.
- If PR2 already claimed or terminalized the Request, finalization updates **0** rows; the service reloads and returns the **observed** status (`ALREADY_RUNNING`, `ALREADY_TERMINAL`, etc.) with `message_sent` reflecting the send attempt (`True` on accepted send, `False` / `None` on definite / ambiguous failure). Observed terminal must never be mapped to `ALREADY_QUEUED`.

**Failure classification:** Catch only expected botocore / SQS configuration send failures (`ClientError`, `BotoCoreError`, `SqsConfigurationError` from `documents/services/sqs.py`; `SqsConfigurationError` subclasses `RuntimeError` for `_required_env` backward compatibility, but enqueue catches the dedicated subclass only). Ordinary `RuntimeError` and other programming exceptions must propagate — do not convert them into `ENQUEUE_FAILED`. `ENQUEUE_SEND_FAILED` only for definite reject codes / missing config. Timeouts / connection / unclear errors → `ENQUEUE_OUTCOME_UNKNOWN` (ambiguous). Delivered-but-marked-`ENQUEUE_FAILED` remains claimable by PR2.

**Known limitation:** crash after DB commit but before `SendMessage` can leave a stranded **`QUEUED`** Request; peers will not resend. Repair is deferred to a later recovery/requeue command.

**Validation boundary:** service requires Document existence + persisted `initiated_by`. Authz / CSRF / admin gate belong at the staff POST (see staff enqueue UI entry).

**Still deferred (after PR3 service):** ~~staff UI POST~~ → **implemented** (fetch/sync enqueue only); feature gate; ops recovery/requeue for stranded `QUEUED`; automatic activation; search / DTR / bindings / processing-state changes.

## Archive full-text search — architecture (docs-only)

**Decision:** Future public `/archive/?q=` full-text search will use a **denormalized one-to-one search-index row per `ArchiveItem`**, queried only after the existing browse authorization/renderability filter. Detailed design: **`docs/ai-context/archive-full-text-search-design.md`**. This entry is **docs-only**; no application code, migrations, tests, settings, or dependencies change here.

**Current behavior at architecture-docs time (historical; superseded by PR1–PR3 implementation entries):**

- Public search applied **`archive_browse_queryset_for_user`**, then type filtering, then **`icontains`** over **`title`**, **`author_name`**, **`source_title`**, and linked **`categories`**, **`events`**, and **`tags`** names.
- It did **not** search **`ManualTextContent.body`** or displayed **`DocumentTextResult`** text.
- Database was **PostgreSQL 16** without search-index FTS yet.
- **Live behavior now:** see PR1–PR3 implementation entries (`ArchiveItemSearchIndex` + PR3 `/archive/?q=` cutover).

**Target searchable content:** title, author, source title, categories, events, tags, **`public_note`**, **`ManualTextContent.body`**, and the OCR transcription selected by existing display helpers (`get_displayed_transcription_text` / `resolve_displayed_transcription_result` — not every `DocumentTextResult` row).

**Explicit exclusions:** **`DocumentMetadata`**, technical/provider data, Transkribus snapshots, PAGE XML, bindings/geometry, and other private implementation metadata. Initial scope also excludes detailed **`PhotoContent`** descriptive fields and **date** search. Misleading date/place help text is corrected only in the UI/snippet PR.

**Policy decisions:**

1. Denormalized **1:1 search-index row per `ArchiveItem`**.
2. OCR body text follows **current display selection**; **`REJECTED`** remains searchable when it is still displayable. Changing display/REJECTED policy is a separate decision.
3. Preserve **one result per `ArchiveItem`**, existing type filters, pagination, and public/family/private visibility.
4. PostgreSQL FTS with config **`simple`** for language-independent body tokenization/ranking; **not** sufficient alone for Hebrew substring/prefix (e.g. `מרזוק` vs `ומרזוק`). Preserve substring behavior on short discovery fields; evaluate **`pg_trgm`** or measured Hebrew normalization **before** trigram-indexing full OCR bodies.
5. **No** locator / hover payload fields yet. Search-to-line/page mapping waits for hover integration.
6. **Visibility stays query-time** and is **never** denormalized into the search index. Auth/renderability run **before** matching, ranking, counts, and snippets. Search must work **without** Transkribus snapshots, bindings, or geometry.

**Implementation sequence (future code PRs):**

| PR | Focus |
|----|--------|
| **PR1** | Search-index model; pure builder (value object only) + persistence (materializes row/`search_vector`); migration; GIN; idempotent backfill — **no** public search behavior change, **no** broad write-path hooks |
| **PR2a** | Explicit sync for discovery/manual/taxonomy writers + `--check-only` drift verification |
| **PR2b-1** | Explicit sync for human-controlled displayed OCR/`DocumentTextResult` mutation paths |
| **PR2b-2** | Explicit sync for automated worker/translation displayed OCR/`DocumentTextResult` mutation paths |
| **PR3** | Backend search cutover (auth, ranking, Hebrew behavior, query-plan tests) — **no** snippet UI — **implemented** (see PR3 entry) |
| **PR4** | Safe Hebrew snippets, match-source presentation, help-text correction — **implemented** (see PR4 entry) |
| **Later** | Optional search-result → line/page mapping when hover is implemented — **deferred** |

**Hard rollout rule:** **PR1 migrate/backfill → PR2a sync → PR2b-1 sync → PR2b-2 sync → full backfill again while all sync hooks are active → drift verification → PR3 cutover.** Satisfied before PR3 implementation.

**Docs:** `docs/ai-context/archive-full-text-search-design.md`

## Archive full-text search — PR1 search-index foundation (implemented)

**Decision / implemented:** PR1 foundation from `docs/ai-context/archive-full-text-search-design.md` is in code.

**Introduced:**

- Model **`ArchiveItemSearchIndex`** (`documents.models`): OneToOne → **`ArchiveItem`** (`related_name="search_index"`, **`CASCADE`**); plain fields **`title_text`** (weight A), **`metadata_text`** (weight B: author, source_title, categories/events/tags names, public_note), **`body_text`** (weight C); **`SearchVectorField`** **`search_vector`** (nullable until persistence); GIN index **`archive_item_search_vector_gin`**; migration **`0042_archive_item_search_index`**.
- **`django.contrib.postgres`** added to **`INSTALLED_APPS`** (required for **`SearchVectorField`** / **`GinIndex`**).
- Pure builder + persistence in **`documents/services/archive_search_index.py`**: **`ArchiveItemSearchContent`** value object; **`build_archive_item_search_content`** (no DB writes / no vector); **`persist_archive_item_search_content`** upserts row and materializes weighted **`simple`** **`search_vector`**; queryset helper **`archive_items_for_search_index_build`** documents prefetch expectations. OCR body uses **`get_displayed_transcription_text`**.
- Management command **`backfill_archive_search_index`** (`--archive-item-id`, `--batch-size`): idempotent rebuild of the search-index table only.

**Unchanged (intentional):** public **`/archive/?q=`** still uses **`filter_archive_items_by_search_query`** (`icontains` metadata only). **No** write-path sync hooks in PR1. **No** snippets, ranking cutover, `pg_trgm`, locators, photo-detail/date search, or Transkribus geometry dependency.

**Tests:** `documents/test_archive_search_index.py`.

## Archive full-text search — PR2a discovery/manual/taxonomy sync (implemented)

**Decision / implemented:** PR2 write-path synchronization is split. **PR2a** covers ArchiveItem discovery/manual/taxonomy index sync and drift verification only. Displayed OCR mutation sync is further split into **PR2b-1** (human-controlled) and **PR2b-2** (automated).

**Introduced:**

- Id-based sync API in **`documents/services/archive_search_index.py`**: **`sync_archive_item_search_index(archive_item_id)`** reloads via **`archive_items_for_search_index_build`**, locks only the **`ArchiveItem`** row, then rebuilds/persists. Returns **`None`** only when the ArchiveItem is missing (delete race); other errors propagate so surrounding source transactions roll back. **`sync_archive_item_search_indexes`** fans out for taxonomy renames. **No signals.**
- Explicit hooks (same transaction as source writes): **`create_ocr_document`**, **`create_manual_text_archive_item`**, **`update_manual_text_archive_item`**, **`update_photo_archive_item_metadata`**, **`update_ocr_document_metadata`**, **`update_archive_item_discovery_metadata`** (also covers photo create empty/non-empty discovery), **`archive_metadata_suggestion_review.approve_suggestion`**, **`apply_archive_discovery_metadata_backfill`** when links are added, and Tag/ArchiveCategory/ArchiveEvent admin **`save_model`** name-rename fan-out.
- **`backfill_archive_search_index --check-only`**: read-only coverage/content/null-vector/extra-row drift verification; prints counts and archive item ids only; exits non-zero on drift; no writes.

**Deferred (superseded by PR2b-1 / PR2b-2 split):** displayed OCR/`DocumentTextResult` mutation hooks were deferred from PR2a; see the PR2b-1 and PR2b-2 entries below.

**Unchanged (intentional):** public **`/archive/?q=`** remains **`icontains`**. No schema migration. No snippets/ranking/`pg_trgm`. Deletes continue to rely on **`ArchiveItemSearchIndex` CASCADE**.

**Hard rollout rule (updated):** public FTS cutover (PR3) remains blocked until **PR2a, PR2b-1, and PR2b-2** are deployed, a **full backfill is rerun while all sync hooks are active**, and **`--check-only` drift verification passes**.

**Tests:** `documents/test_archive_search_index_sync.py` (plus existing PR1 suite).

## Archive full-text search — PR2b-1 human-controlled displayed-text sync (implemented)

**Decision / implemented:** PR2b is split. **PR2b-1** covers human-controlled displayed-text mutation sync only. **PR2b-2** covers automated worker/translation mutations (see following entry).

**Introduced:**

- Same-transaction id-based **`sync_archive_item_search_index(archive_item_id)`** hooks after successful human-controlled displayed-text mutations:
  - **`edit_pending_text_result`** / **`edit_verified_text_result`** (after canonical edit/mirror/revision logic; pending no-op early return does not sync)
  - transcription **`approve_suggestion`** (after displayed text + Hebrew mirror / revision updates and suggestion status save)
  - **`activate_corrected_current_sync_attempt`** only when **`source_text_changed`** or **`hebrew_mirror_updated`** is true (after final displayed text + bindings)
- Index failure propagates and rolls back the surrounding source transaction (text edits, suggestion approval, activation text/bindings). **No signals. No `on_commit`. No schema migration.**

**Explicitly not hooked in PR2b-1:** verification-only verify/reject; transcription suggestion rejection; activation **`ALREADY_ACTIVE`** and binding-only repair; preview/history GET; snapshot fetch/storage; geometry/binding-only helpers; worker OCR/HTR, translation persist/retry, Transkribus local completion (PR2b-2); public **`/archive/?q=`**.

**Unchanged (intentional):** public **`/archive/?q=`** remains **`icontains`**. Hebrew/non-Hebrew displayed body continues to follow **`get_displayed_transcription_text`**. PR2a discovery/manual/taxonomy sync remains intact.

**Hard rollout rule (updated):** public FTS cutover (PR3) remains blocked until **PR2a, PR2b-1, and PR2b-2** are deployed, a **full backfill is rerun while all sync hooks are active**, and **`--check-only` drift verification passes**.

**Tests:** `documents/test_archive_search_index_sync_ocr_body.py` (plus existing PR1/PR2a suites).

## Archive full-text search — PR2b-2 automated displayed-text sync (implemented)

**Decision / implemented:** **PR2b-2** covers automated displayed-text mutation sync. Hooks are at **parent transaction boundaries** only (one sync per logical automated operation). Shared **`persist_hebrew_translation_result`** is intentionally **not** hooked (worker Phase 3 already syncs after translation persist). The Gemini translation **call** runs outside that Phase 3 transaction; SOURCE + HEBREW persist + sync remain in one TX.

**Introduced:**

- Same-transaction id-based **`sync_archive_item_search_index(archive_item_id)`** after final automated display state:
  - Worker Phase 3 atomic in **`run_worker._process_message`** — after `_save_htr_results` / `_save_ocr_failure` and processing-state save (covers Gemini OCR success, nested non-Hebrew translation persist, and OCR failure demotion). Transkribus automatic snapshot path is **not** double-synced here (it returns via local completion).
  - **`complete_transkribus_local_success`** write path only — after DTR/bindings/run success; **skips** early no-overwrite exit when bindings are structurally complete and run is already SUCCEEDED.
  - **`run_hebrew_translation_retry`** persist atomic only — after HEBREW persist + processing-state save; claim TX / abort / duplicate terminal no-op paths do not sync.
- Index failure propagates and rolls back the surrounding source transaction. Worker Phase 3 / local-completion failures continue to prevent SQS ack via existing exception/`False` behavior; translation-retry persist failures remain **`return False`** (no ack) after rollback. **No signals. No `on_commit`. No schema migration.**

**Lock order (documented):**
- Phase 3: **Document** → **ArchiveItem** (inside sync)
- Local completion: **Document → TranskribusRun → RunAutomaticSnapshot → Snapshot → DTRs** → **ArchiveItem** (inside sync)
- Translation retry persist: **Document** → **ArchiveItem** (inside sync)

**Explicitly not hooked in PR2b-2:** `persist_hebrew_translation_result` helper; claim-only / abort translation-retry paths; local-completion early no-overwrite; multi-image validation FAILED without DTR; snapshot PAGE XML storage; bindings/geometry-only helpers; verification-only verify/reject; PR2b-1 human paths (already hooked); public **`/archive/?q=`**.

**Unchanged (intentional):** public **`/archive/?q=`** remains **`icontains`**. Non-Hebrew missing/failed translation still yields intentional **`PARTIAL`**. Worker retry/ack policy unchanged except index failure cannot leave text and index inconsistent.

**Hard rollout rule (historical for this entry):** public FTS cutover (PR3) remained blocked until **PR2a, PR2b-1, and PR2b-2** were deployed, a **full backfill was rerun while all sync hooks are active**, and **`--check-only` drift verification** passed. **PR3 is now implemented** — see the following entry.

**Tests:** `documents/test_archive_search_index_sync_automated.py` (plus existing PR1/PR2a/PR2b-1 suites).

## Archive full-text search — PR3 backend search cutover (implemented)

**Decision / implemented:** Public `/archive/?q=` now searches **`ArchiveItemSearchIndex`** after **`archive_browse_queryset_for_user`**. Production gate satisfied before cutover: PR1–PR2b-2 deployed; final backfill 216/216; `--check-only` drift verification clean.

**Query semantics:**

- Display/URL `q`: trim only. Normalization outcome via **`resolve_archive_list_search_terms`**: blank/whitespace → **`no_search`** (browse); overlong or nonblank punctuation-only → **`no_matches`** (empty results, not full archive); otherwise **`search`** terms after collapsing whitespace and splitting on ordinary punctuation **and underscore** (`[\W_]+`). PostgreSQL/`SearchQuery` `config="simple"` + `search_type="plain"`.
- Multi-term **AND** (cross-source allowed: terms may hit different of title/metadata/body/**hebrew_translation_text**). No OR fallback; no phrase/minus/paren/web syntax.
- Per-term match is **decomposed**: authorized PK `UNION` of FTS (`search_vector @@`) ∪ `title_text` `icontains` ∪ `metadata_text` `icontains`, then AND across terms via `pk__in`. This keeps the FTS arm independently usable with **`archive_item_search_vector_gin`** (a combined `@@ OR ILIKE OR ILIKE` WHERE can force seq scan). **No** `body_text` / `hebrew_translation_text` substring.
- Ranking: `SearchRank` on A/B/C vector + title substring boost `1.0` + metadata substring boost `0.4` (each boost once if any term hits that short field); tie-break `-created_at`, then `pk`. Empty `q`: unchanged chronological `-created_at`.
- Safety: trimmed `q` longer than **200** chars → empty results (display string preserved). Missing index row → no match, no crash, no GET rebuild.
- Auth/filters/pagination/UI unchanged aside from backend matching/ranking. One `ArchiveItem` per hit. `REJECTED` displayable OCR remains searchable per display helpers.

**Explicitly deferred (at PR3 time; PR4 now implemented):** PR4 snippets/highlights/match-source/help-text — see following entry. Still deferred: hover/page/line locators; `pg_trgm`; write-path/sync/schema changes; staff document-list FTS.

**Tests:** `documents/test_archive_full_text_search.py` (plus updated PR1/PR2 public-search regressions and archive list search tests).

**Docs:** `docs/ai-context/archive-full-text-search-design.md` (PR3 marked implemented).

## Archive full-text search — PR4 snippets, match-source, help text (implemented)

**Decision / implemented:** Public `/archive/?q=` search results show safe contextual snippets and accurate Hebrew match-source labels for the authorized page slice only. PR3 matching/ranking/normalization/auth/filters/pagination are unchanged.

**Presentation:**

- No effective `q`: ordinary beginning-of-text card preview unchanged.
- With `q`: at most one contextual body snippet per card (~160–220 chars, word-aware window maximizing distinct query terms, earliest tie-break, ellipses only when omitted). Body whole-token match → replace preview; OCR label **`נמצא בתעתוק`**, ManualText **`נמצא בטקסט`**. Prefer body snippet when body and title/metadata both match.
- Title-only: no fabricated body excerpt (title already visible).
- Metadata/public_note/discovery-only: keep ordinary preview; specific label when one source is reliable, else **`נמצא בפרטי הפריט`**. Never claim a field without a normalized term hit.
- Highlighting: autoescaped `ArchiveSearchSnippetSegment` text + template `<mark>` only (no `mark_safe` / raw HTML). Unicode-aware case-insensitive whole-token highlights inside the selected snippet.
- Help text + placeholder corrected to live fields (no date/place claims).

**Performance / auth:** Snippets built only after browse auth and pagination; one bounded `ArchiveItemSearchIndex` load for page ids; no N+1; unauthorized private content never appears in snippets, labels, counts, or HTML.

**Files:** `documents/services/archive_search_snippets.py`; `ArchiveBrowseCard` search fields; `archive_list_page` wiring; archive list/card templates; CSS; tests in `documents/test_archive_full_text_search.py`.

**Explicitly deferred:** hover/page/line mapping; jump-to-match; `pg_trgm`/morphology/fuzzy; search backend/ranking changes beyond translation-field coverage.

**Docs:** `docs/ai-context/archive-full-text-search-design.md` (PR4 marked implemented).

## Archive full-text search — post-PR4 Hebrew translation coverage (implemented)

**Decision / implemented:** After PR1–PR4, non-Hebrew documents were findable by displayed source transcription but **not** by text that appears only in the displayed Hebrew translation, because `ArchiveItemSearchIndex` stored a single `body_text`. This correction adds a separate indexed field and keeps source/translation contracts distinct.

**Introduced:**

- Model field **`ArchiveItemSearchIndex.hebrew_translation_text`** (weight **C** with `body_text`); migration **`0043_archiveitemsearchindex_hebrew_translation_text`** (schema only; no data migration).
- Value object field on **`ArchiveItemSearchContent`**; builder uses **`get_displayed_hebrew_translation_text`** / **`resolve_displayed_hebrew_translation_result`** in **`text_presentation.py`**.
- Selection contract for the translation field:
  - non-Hebrew OCR only;
  - displayable HEBREW_TEXT via the same `_latest_displayable` rules as `get_text_presentation_for_document`;
  - requires a displayable SOURCE (so HEBREW used as transcription fallback is not duplicated into both fields);
  - **includes** revision-stale translations (public detail still shows them; `is_hebrew_translation_stale` is review-detail only);
  - Hebrew-language documents leave the field empty (no mirror duplication);
  - ManualText / photos leave the field empty.
- Persistence rematerializes `search_vector` as title A + metadata B + `body_text` C + `hebrew_translation_text` C. Existing GIN index unchanged; no `pg_trgm`.
- Snippets: translation-only matches use label **`נמצא בתרגום`**; source keeps **`נמצא בתעתוק`**; ManualText keeps **`נמצא בטקסט`**. When both long-text fields match, pick the window with more distinct query terms; ties prefer source/body.
- Backfill `--check-only` compares `hebrew_translation_text` (counts/IDs only; no source/translation text in logs).

**Sync/write-path audit:** No new hooks. Existing parent-boundary `sync_archive_item_search_index` calls already rebuild the full value object after worker OCR + nested translation, translation retry, human pending/verified HEBREW_TEXT edits, suggestion approval, and corrected-current activation when source/hebrew text flags change. Source edits that leave HEBREW revision-stale still rebuild the index with the translation text the public detail continues to show. **`persist_hebrew_translation_result`** remains unhooked (would double-sync on the worker path).

**Unchanged (intentional):** ranking weights A/B/C and boosts; auth; UNION/GIN strategy; max query length; punctuation normalization; filters; pagination; no-query ordering; photo content; translation generation / OCR routing / processing-state semantics.

**Rollout:** 1) deploy + migrate `0043`; 2) full `backfill_archive_search_index` while all PR2 sync hooks are active; 3) full `--check-only` drift verification.

**Tests:** extended `test_archive_search_index.py`, `test_archive_full_text_search.py`, `test_archive_search_index_sync.py`, `test_archive_search_index_sync_ocr_body.py`, `test_archive_search_index_sync_automated.py`.

**Still deferred:** hover/page/line mapping; `pg_trgm`/fuzzy/morphology/Hebrew prefix expansion.

## PROCESS_DOCUMENT durable Request enqueue foundation (service only)

**Decision / implemented:** `enqueue_process_document_request(...)` in `documents/services/process_document_request_enqueue.py` creates, coalesces, and enqueues durable `ProcessDocumentRequest` rows. `send_process_document_request_message(request_id)` sends the request-aware payload `{"type": "PROCESS_DOCUMENT", "request_id": <positive int>}`. This PR is service-only: existing upload-finalize, OCR-reprocess, and Hebrew-translation-retry callers continue using their legacy enqueue paths and are wired in later, separately reviewed PRs.

**Transaction boundary:** The public enqueue service must be called outside every database atomic block. It rejects an outer transaction before validation or database writes. Under one short transaction it locks the `Document`, validates the canonical operation/origin/retry/source-run shape, and determines send right. SQS `SendMessage` always runs after that transaction commits.

**Document-lock send right and coalescing:** Only the caller that creates a new `QUEUED` Request, or atomically changes a matching `ENQUEUE_FAILED` Request back to `QUEUED`, may send. A matching `QUEUED`, `RUNNING`, or `RECOVERY_REQUIRED` Request is returned without resending. A differing active payload returns `ACTIVE_REQUEST_CONFLICT`; the existing Request is never repurposed. Terminal history does not block a new Request.

**Payload and provenance:** OCR supports `UPLOAD_FINALIZE` and `OCR_REPROCESS`; Hebrew translation supports `HEBREW_TRANSLATION_RETRY`. `transkribus_recognition_only` requires a positive source `TranskribusRun` id belonging to the same Document. System-initiated work may use `initiated_by=None`; any supplied actor must be a persisted `User`. Retrying with no actor preserves existing actor provenance.

**Post-send fencing:** Success and failure finalization use compare-and-set updates restricted to an unclaimed `QUEUED` Request with no lease token. They never overwrite worker-owned `RUNNING`, `RECOVERY_REQUIRED`, or terminal state. If the worker claims or terminalizes during `SendMessage`, the service reloads and reports the observed state; `PARTIAL` remains an acknowledged terminal outcome.

**Send failures:** Only expected botocore or SQS configuration failures are classified. Definite rejection or missing configuration records `ENQUEUE_FAILED` / `ENQUEUE_SEND_FAILED` with a safe message and `message_sent=False`. Ambiguous transport outcomes record `ENQUEUE_FAILED` / `ENQUEUE_OUTCOME_UNKNOWN` with `message_sent=None`. Programming exceptions propagate instead of being misclassified.

**Known limitation:** A process crash after the Request transaction commits but before `SendMessage`, or an unexpected programming exception at send time, can leave a stranded `QUEUED` Request. Matching peers deliberately do not resend it. Explicit recovery/requeue tooling remains a later task.

**Still deferred:** wiring upload finalize, OCR reprocess, and Hebrew translation retry to this service; removing the legacy document-id payload producers; stranded-`QUEUED` reconciliation/requeue tooling; and any caller-specific UI or operational behavior changes.

## PROCESS_DOCUMENT upload-finalize caller cutover

**Decision / implemented:** Successful single-file `upload_complete` and multi-image `upload_finalize` now call `enqueue_uploaded_document_processing(...)` after the upload database transaction. The adapter submits `operation=OCR`, `origin=UPLOAD_FINALIZE`, `ocr_retry_mode=normal_reenqueue`, `source_transkribus_run_id=None`, with the authenticated staff user as `initiated_by`. The views no longer send the legacy document-id payload directly.

**Idempotence:** Upload completion retries still consult the durable adapter. A matching active Request coalesces without another send. A matching terminal `UPLOAD_FINALIZE` Request returns `ALREADY_TERMINAL` and does not create new OCR work; later intentional OCR retry must use `origin=OCR_REPROCESS`.

**Document-state fencing:** New, retried, or already-queued work sets `Document.processing_state_user=PROCESSING` and clears `upload_error` only while the associated Request is still `QUEUED`. Queue failure sets the safe `FAILED` state only while the Request is still `ENQUEUE_FAILED`. Worker-owned running or terminal state is never overwritten.

**HTTP boundary:** Expected queue, validation, conflict, and recovery outcomes use typed `UploadProcessEnqueueError` values and safe response text. Raw SQS exception details are not returned. Unexpected programming exceptions propagate.

**Infrastructure:** The shared ECS task role receives `queue.grant_send_messages(...)` in addition to consume permissions. Any deployment must preserve the explicit live/new image tag and pass a narrowly reviewed CDK diff.

**Still deferred:** OCR reprocess and Hebrew translation retry remain on the legacy document-id payload pending separate caller cutovers. Recovery/requeue tooling for stranded pre-send `QUEUED` Requests also remains deferred.

## PROCESS_DOCUMENT OCR-reprocess caller cutover

**Decision / implemented:** Intentional staff or command-driven OCR reprocess now
uses a durable `ProcessDocumentRequest` with `operation=OCR`,
`origin=OCR_REPROCESS`, the retry mode selected by the existing route-aware
assessment, and `source_transkribus_run_id` only for
`transkribus_recognition_only`. The staff actor is stored as `initiated_by`;
management-command execution remains system initiated.

**Transaction and state boundary:** Assessment remains read-only. The generic
enqueue service owns the short Document-lock transaction and sends the
request-id message only after commit. The OCR-reprocess adapter marks the
Document `PROCESSING` and clears `upload_error` only while the associated
Request is still `QUEUED`; it marks a safe queue failure only while the Request
is still `ENQUEUE_FAILED`. Worker-owned `RUNNING` and terminal state always
wins.

**Outcomes and errors:** Matching active Requests coalesce; a conflicting active
payload and `RECOVERY_REQUIRED` are explicit safe conflicts. Definite and
ambiguous queue failures expose no SQS details. Unexpected programming
exceptions propagate. Terminal OCR-reprocess history does not block a later
intentional retry.

**Compatibility and deferrals:** The request-aware worker execution contract is
unchanged. The legacy document-id sender remains temporarily for Hebrew
translation retry. Stranded-`QUEUED` recovery/requeue, async Transkribus resume,
Gemini page persistence, and deployment remain separate work.

## PROCESS_DOCUMENT Hebrew-translation-retry caller cutover

**Decision / implemented:** Staff-triggered retry of a failed or missing Hebrew
translation now creates or coalesces a durable `ProcessDocumentRequest` with
`operation=HEBREW_TRANSLATION`, `origin=HEBREW_TRANSLATION_RETRY`, no OCR retry
mode, and no source Transkribus run. The authenticated staff user is stored as
`initiated_by`; SQS receives only the Request-aware `request_id` payload.

**Eligibility and idempotence:** Existing translation-only eligibility and
overwrite protection remain authoritative before a new Request or an
`ENQUEUE_FAILED` resend. Matching `QUEUED` or worker-owned `RUNNING` work
coalesces without a second send. Matching `RECOVERY_REQUIRED` and differing
active work are safe typed conflicts. Terminal translation-retry history does
not block a later intentional retry.

**State and error boundary:** Enqueue does not mutate
`Document.processing_state_user` or `upload_error`; the translation worker owns
the transition to `PROCESSING` and its terminal state. Definite and ambiguous
queue failures are persisted on the Request and exposed through safe typed
messages without raw SQS details. Unexpected programming exceptions propagate.

**Compatibility and deferrals:** No worker execution, model, migration, queue,
or infrastructure contract changes in this cutover. The legacy document-id
sender and worker payload reader remain temporarily for in-flight messages and
mixed-version deployment safety. Remove them only after the durable chain is
deployed and verified end to end. Stranded pre-send `QUEUED` recovery/requeue,
async Transkribus resume, Gemini page persistence, and deployment remain
separate work.

## PROCESS_DOCUMENT stranded enqueue recovery/requeue

**Decision / implemented:** Add the dry-run-by-default management command
`recover_process_document_requests` and the fenced recovery service in
`process_document_request_recovery.py`. The command repairs delivery of an
already-approved durable Request; it does not create new processing intent or
repurpose its payload. Before reserving a send it revalidates the current
caller-specific safety contract. Operator usage is documented in
`docs/ai-context/process-document-request-recovery.md`.

**Eligibility:** A Request is recoverable only when it is older than the
operator-selected cooldown (default **15 minutes**) and is either:

- `QUEUED` with `last_enqueued_at IS NULL` (no successful send finalization);
- `ENQUEUE_FAILED`, including `ENQUEUE_OUTCOME_UNKNOWN`.

`QUEUED` rows with a recorded successful enqueue, `RUNNING`,
`RECOVERY_REQUIRED`, and every terminal status are excluded. Duplicate
request-id messages remain execution-safe because the worker locks the Request
and grants only one current lease.

**Current-intent reassessment:** Recovery must not bypass safety changes that
occurred after the original enqueue attempt. `OCR_REPROCESS` reruns the
route-aware assessment and requires the selected retry mode/source run to match
the stored Request exactly. `HEBREW_TRANSLATION_RETRY` reruns source,
processing-state, and overwrite protection. `UPLOAD_FINALIZE` requires an
uploaded OCR Document with no verified text and no usable existing
`SOURCE_TEXT`. Failed reassessment or payload drift is reported as an
ineligible skip, without reservation or SQS.

**Operator safety:** Unscoped execution is report-only. `--apply` requires
repeated `--request-id`, repeated `--document-id`, or explicit
`--all-eligible`. `--limit` defaults to 100 and is capped at 1000.
`--older-than-minutes` is explicit, may override the 15-minute default, and
must remain at least one minute so the reservation has a real exclusion
window.
Missing explicit Request ids and invalid limits/ages fail before mutation.

**Reservation and SQS boundary:** Recovery reassesses under
`select_for_update`. An eligible `ENQUEUE_FAILED` row returns to canonical
`QUEUED` shape; every eligible row receives a new `updated_at` cooldown
reservation. The transaction then commits before `SendMessage`. A crash in the
new pre-send window leaves the row recoverable again after the cooldown. The
shared `send_reserved_process_document_request` path owns the existing
definite/ambiguous failure classification and post-send compare-and-set
fencing, so recovery cannot overwrite worker-owned running or terminal state.
Unexpected programming exceptions still propagate.

**Document state:** Successful queued OCR recovery marks the Document
`PROCESSING` and clears `upload_error` only while the same Request remains
`QUEUED`. A persisted queue failure marks a safe OCR failure only while that
Request remains `ENQUEUE_FAILED`. Hebrew-translation recovery does not mutate
Document state; its worker remains the owner of claim and completion.

**Scope / deferrals:** No model, migration, worker execution, SQS payload, IAM,
or infrastructure changes. `RECOVERY_REQUIRED` execution recovery is
deliberately not replayed by this command. Legacy document-id compatibility,
deployment/E2E, async Transkribus resume, and Gemini page persistence remain
separate work.

## Gemini OCR response safety and failure taxonomy

**Decision / implemented:** Gemini OCR response handling classifies provider
metadata before parsing or accepting text. Response failures use stable codes
for empty output, `MAX_TOKENS`, safety, recitation, language, SPII, other
blocking reasons, missing candidates, JSON parse/schema failures, and provider
API errors.

**Privacy boundary:** Logs, exception messages, and persisted worker failure
details may include only operational metadata: model, page, attempt,
finish/block reason, candidate count, output lengths, token counts, configured
output cap, and exception class. They must not include the OCR response,
prompt, provider finish-message text, or other document content. This
finish-message and provider-exception redaction also applies to the shared
Gemini Hebrew translation logging path.

**Temporary JSON contract:** Until Hebrew printed OCR moves to plain text, the
JSON parser requires a non-empty string `text`, a real boolean `has_unclear`,
and a non-negative integer `unclear_count`. Missing keys, empty text, and
wrongly typed values fail explicitly instead of becoming a successful empty or
partial transcription.

**Unchanged / deferred:** No routing, model selection, persistence schema,
attempt count, backoff, output-token cap, Transkribus, Antigravity, translation
generation, response-text extraction, retry behavior, or processing-state
semantics change. Page-level checkpoint/resume, Hebrew printed plain-text
output, bounded per-page recovery, and mixed printed/handwritten routing
remain separate PRs.

## Gemini OCR durable page checkpoint/resume

**Decision / implemented:** Gemini OCR now uses a durable
`GeminiOcrAttempt` identity plus one fenced `GeminiOcrPageCheckpoint` per
1-based page. A successful page is saved immediately. A later intentional OCR
execution with the same complete identity reuses successful pages and calls
Gemini only for failed or missing pages. `DocumentTextResult` remains
document-level and is persisted only after deterministic complete assembly.

**Identity:** Attempt identity hashes actual downloaded source bytes, ordered
source identity, normalized page bytes, language/input/handwriting route,
engine/prompt variant, exact effective prompt plus explicit contract version,
ordered model candidates, and output-affecting generation configuration.
Source replacement/reordering, extraction output, prompt, model candidates, or
configuration changes therefore cannot silently reuse stale page text.

**Fencing and crash behavior:** Page claims use a 45-minute token/lease and
database row locking. Unexpired work is not executed twice; expired work may be
reclaimed; late tokens cannot overwrite a newer claim. Complete page
checkpoints remain reusable if the worker crashes before document-level
persistence.

**Partial outcome:** A final page failure stops the current execution, preserves
earlier successes, marks the attempt/document/request `PARTIAL`, and reports
`GEMINI_PAGES_INCOMPLETE` with a bounded ordered `missing_pages=...` message.
It does not create a misleading whole-document failed `DocumentTextResult`.
Missing-page UI is deferred; durable indices are available on the attempt.

**Local persistence boundary:** Provider execution and checkpoint persistence
are separate exception domains. A database failure while creating, claiming,
persisting, assembling, or reading checkpoint state returns a dedicated
`OCR_PAGE_CHECKPOINT_PERSISTENCE_RETRYABLE` outcome. It leaves the SQS message
and request lease in place and does not misclassify the failure as provider
`API_ERROR`, terminalize as `PARTIAL`, or persist provider/document content.

**Runtime model identity:** Each page stores its actual Gemini model. Uniform
assembly keeps the concrete model name. Multi-model assembly uses
`gemini-mixed:` plus 48 SHA-256 hex characters over the ordered page/model
mapping, with full provenance retained on page rows. This preserves the
existing Gemini-only candidate fallback without reprocessing successful pages.

**Request boundary:** Attempts are intentionally independent of
`ProcessDocumentRequest` so a later intentional request can reuse them.
`RECOVERY_REQUIRED` is not reclaimed or replayed by this PR; existing request
lease/fencing remains authoritative. Request-level recovery is separate work.

**Rationale and rejected alternatives:** Binding page state only to a Request
would prevent cross-request resume; page-level `DocumentTextResult` rows would
break document display/edit/search/verification semantics; memory/SQS state is
not durable; replaying `RECOVERY_REQUIRED` would contradict the existing
provider-safety fence; reprocessing every page after model fallback would
defeat the cost/reliability goal; excluding Arabic would leave an existing
multi-candidate Gemini route unsafe; UI would broaden this concurrency/schema
PR. Full reasoning is recorded in
`docs/ai-context/gemini-page-checkpoint-design.md`.

**Unchanged / deferred:** No routing, translation, Transkribus, Antigravity,
attempt-count, backoff, token-cap, continuation, splitting, Hebrew printed
plain-text, or MIXED document behavior changes. Those remain in their approved
separate PRs.

**Validation audit / inherited test repairs:** The initial 2,255-test broad run
reported ten failures. Nine unchanged worker fixtures supplied `object()` or
`SimpleNamespace` instead of the now-required complete `PageImage`; they were
updated to use the production page value object. Weakening the production
checkpoint/source-identity contract for partial test doubles was rejected.
The affected three modules then passed 75/75 tests.

The tenth failure, the archive browse/search regression test, reproduced alone
on a clean worktree at the exact base commit
`cb10864de0f9180051e4463bd717047bdc17f4a2`. Its setup mutated
`item.categories` directly after index creation and therefore bypassed the
documented explicit `update_archive_item_discovery_metadata` search-index hook.
The fixture now uses that service; the isolated test and `test_archive_item`
passed 1/1 and 324/324. No model signal or search production change was added:
that would contradict the established same-transaction explicit-hook and
no-signals architecture.

The following full-suite rerun exposed an inherited planner-sensitive GIN test.
The query still contained the independent FTS `UNION` arm, but PostgreSQL could
prefer the selective one-to-one B-tree index and apply `search_vector @@` as a
filter for the tiny fixture. The unchanged test passed on the clean base and in
five isolated current-worktree runs. Its existing `enable_seqscan=off` plan
probe now also sets `enable_indexscan=off`, leaving bitmap scans enabled so the
test deterministically demonstrates the intended bitmap/GIN path. Removing the
GIN assertion, changing production search for a planner-only choice, and
inflating/analyzing fixtures were rejected. The stabilized check passed five
consecutive runs and the full 45-test FTS module.

Final staged review found one canonicalization gap: attempt identity stripped
model-candidate whitespace while adapter execution did not. The adapter now
normalizes once before identity and execution and rejects candidates that
become empty. Identity-only normalization was rejected because it could map
different provider input to the same attempt; hashing surrounding whitespace
was rejected because it is not part of a valid model identifier.

**Typing evidence:** Scoped PR B Pyright/mypy is clean. Full-tree checks still
report 41 Pyright and 12 mypy errors in unchanged Transkribus test files; these
are inherited baseline debt and are not represented as passing PR B
validation. Before the final staged-review correction, the full Django suite
passed all 2,255 tests with exit code 0. After the model-candidate
canonicalization correction, 29 focused checkpoint/adapter tests and all final
scoped/static checks passed.

The post-correction Django regression covered all 2,257 tests exactly once in
two complementary segments because the original run was interrupted: the
first 751 tests completed without a test failure through
`ArchiveFamilyAccessManagePageTests.test_invalid_post_action_returns_400_and_changes_nothing`,
and the runner-reconstructed remaining 1,506 test IDs then passed in 1,737.182
seconds with exit code 0. This is complete split-run regression coverage, not a
claim that one uninterrupted command reported `Ran 2257 tests`.

Subsequent staged review found and corrected the local checkpoint-persistence
exception-boundary gap described above. After that correction, the affected
checkpoint, adapter, worker, and request-worker selection passed all 74 tests
in 5.187 seconds. Ruff formatting/lint, Django system and migration checks,
scoped Pyright/mypy, and the staged-diff check all passed; all eight recorded
exit codes were zero. The 2,257-test split-run evidence above predates this
last correction, so no new post-correction full-suite run is claimed.

## Gemini Hebrew printed plain-text OCR response contract (PR C)

**Status:** Implemented and validated on the uncommitted PR C branch
`feat/gemini-hebrew-printed-plain-text-pr-c`. Not merged.

**Decision / implemented:** Hebrew printed OCR (`prompt_variant=printed` with
the canonical language hint `he`) uses the plain-text transcription contract
instead of the previous single-JSON-object contract. This supersedes the
“Temporary JSON contract” note in the PR A entry above for Hebrew printed
only. The new `_HEBREW_PRINTED_PROMPT` keeps the archival guardrails of the
JSON-era Hebrew printed prompt (verbatim typos and unusual Hebrew forms, no
vowel marks, no silent omissions, URL/header/footer preservation, attention
to short Hebrew words and personal details, decorative-UI exclusion) and
requires transcription text only: no JSON, markdown, code fences, comments,
explanations, labels, or introductory text. Execution uses the existing safe
plain-text path already used by Latin printed/handwritten and general Hebrew
handwritten: `v1beta`, effective temperature 0, thinking budget 0, and
`_plain_text_response_to_page_data`. `has_unclear`, `unclear_count`, and
`needs_review` are derived from the established `[?]` / `[UNCLEAR]`
uncertainty markers in the returned transcription; the prompt instructs the
model to use exactly those markers (the JSON-era `[מילה?]` example form is no
longer requested).

**Root cause modeled:** The production document 293 failure class — the
observed provider response began as a JSON object but ended inside its long
`"text"` string, producing `Unterminated string`. Because the entire page
depended on that one complete JSON string, mid-string truncation made all
otherwise returned OCR text unusable. The regression suite models this
precisely: it starts from valid serialized JSON built with
`json.dumps(..., ensure_ascii=False)`, deliberately truncates that serialized
response inside the text value, shows the old JSON parser fails on it, and
shows the same full synthetic transcription is accepted verbatim through the
plain-text path without JSON parsing. No production data was used and no live
Gemini call was made.

**Scope guard:** Only the canonical `he` hint selects the plain-text Hebrew
printed contract. Other non-Latin hints (e.g. `ar`), non-canonical spellings
(`hebrew`, `iw`), and missing hints are **not** treated as Hebrew and keep the
JSON contract via `_PRINTED_TEXT_PROMPT` (currently Arabic printed on the
Gemini route). Handwritten variants, Hebrew translation, Transkribus,
Antigravity, routing, and worker/queue behavior are unchanged.

**Contract version and checkpoint identity:** The contract version is
route-specific. Hebrew printed alone uses the new
`GEMINI_HEBREW_PRINTED_PROMPT_CONTRACT_VERSION` =
`gemini-hebrew-printed-prompt-v2` marker (within the existing 64-character
field) because the output contract for that route changed semantically. All
other routes continue to return the unchanged default
`GEMINI_OCR_PROMPT_CONTRACT_VERSION` = `gemini-ocr-prompt-v1`, so their
existing checkpoint identities remain valid and reusable. For Hebrew printed,
attempt identity hashes both the changed effective prompt fingerprint and the
new route-specific version, so previously completed Hebrew JSON-era
checkpoints cannot be reused under the plain-text contract. Prompt selection,
output-mode selection, and version selection share one predicate
(`_is_hebrew_printed_plain_text_contract`) so they cannot drift apart.

**Failure behavior:** Unchanged PR A taxonomy. Empty Hebrew printed output
fails typed as `EMPTY_RESPONSE` (retryable once within the existing attempt
policy); finish/block failures (`SAFETY`, `RECITATION`, `MAX_TOKENS`, etc.)
remain typed and content-safe; the existing single `MAX_TOKENS` escalation
retry on the plain-text path applies. PR B checkpoints, fencing,
deterministic assembly, mixed-model provenance, and
`OCR_PAGE_CHECKPOINT_PERSISTENCE_RETRYABLE` behavior are preserved.

**Privacy boundary:** Unchanged. Logs, exceptions, and persisted failure
metadata carry only operational metadata (model, page, attempt, finish/block
reason, lengths, token counts, output cap, exception class). Prompts, raw
provider responses, document text, and provider exception text are not logged
or persisted outside the authorized checkpoint/result text fields.

**Rejected alternative — JSON repair:** Keeping the JSON contract and adding
repair heuristics (closing an unterminated string, bracket completion,
lenient parsers, or forcing a provider response schema) was rejected. A
repaired truncated response cannot recover the missing tail of the page, so
repair would silently accept an incomplete transcription as complete archival
text — worse than an explicit typed failure. Plain text removes the
all-or-nothing single-JSON-string dependency instead of patching it.

**Validation evidence:** Focused PR C validation: 63 tests passed in 2.114
seconds. Ruff format/check on the four scoped Python files passed. Django
system check passed. Migration check reported no changes. Scoped Pyright: 0
errors and 0 warnings. Scoped mypy: no issues in four source files.
`git diff --check` passed. All eight focused/static exit codes were 0. Full
`documents` regression: 2,267 tests passed in 1,934.989 seconds,
`TEST_EXIT_CODE=0`.

**Validation still required:** Live Hebrew printed OCR fidelity/quality
validation on real (non-production-reference) documents, deployment through
the existing worker-first runbook, and any production document retry remain
outstanding and are not claimed here.

**Unchanged / deferred:** No routing changes. No new retry counts, backoff,
token escalation policy, hard caps, continuation, or page splitting — that is
PR D. No MIXED printed/handwritten behavior — that is PR E. No translation,
Transkribus, Antigravity, worker-state, queue, UI, migration, deployment, or
production-document changes.

## Gemini bounded per-page OCR recovery (PR D)

**Status:** Implemented and validated on branch
`feat/gemini-bounded-page-recovery-pr-d`; **merged to `main`**
(“Add bounded Gemini OCR page recovery”, #369).

**Scope:** `transcribe_pages_with_gemini` per-page loop only. At most **three
provider calls per page per model candidate**. Ordered model-candidate fallback in
`GeminiAdapter` is unchanged and quota-only; each candidate may receive its own
three-call budget after quota exhaustion on the prior candidate.

**Classify before parse:** Finish/block/candidate/empty failures are classified
before JSON or plain-text parsing. Classified response failures never reach
`_parse_page_json_strict` or `_plain_text_response_to_page_data`.

**Retryable (within budget):**

- `EMPTY_RESPONSE` — backoff **1s** then **2s** before attempts 2 and 3.
- `JSON_PARSE` — immediate retry, no token escalation, no JSON repair.
- `MAX_TOKENS` — immediate retry with deterministic cap ladder (no sleep).
- Transient quota/rate-limit errors (not `LIMIT: 0`) — existing classification,
  counts toward the same three-call budget.

**Permanent (no retry):** PR A codes including `SAFETY`, `RECITATION`,
`LANGUAGE`, `SPII`, blocked/prohibited content, `JSON_SCHEMA`, `NO_CANDIDATES`,
`OTHER`, etc.

**Token-cap ladder:** `None` or cap below 8192 → 8192; else double; clamp to
`GEMINI_MAX_OUTPUT_TOKENS_HARD_CAP` (default **32768**, max configured **65536**,
must be ≥ `GEMINI_OCR_MAX_OUTPUT_TOKENS`). If cap cannot increase, fail typed
`MAX_TOKENS` without repeating an identical call. No continuation and no page
splitting.

**Configuration:** Optional validated env `GEMINI_MAX_OUTPUT_TOKENS_HARD_CAP`
via `validate_required_env` / `WorkerEnvConfig.gemini_max_output_tokens_hard_cap`.
Rejects booleans, non-integers, non-positive values, values below the
configured OCR initial cap, and values above 65536. No migration.

**Checkpoint identity:** Configuration fingerprint now includes
`retry_policy_version` (`gemini-ocr-page-retry-v1`),
`max_provider_calls_per_page` (`3`), and `max_output_tokens_hard_cap`. These
fields are part of the config fingerprint independently of the prompt-contract
version, so this is an intentional identity change for **every Gemini OCR
route** — including PR C Hebrew printed. Hebrew printed remains on
`gemini-hebrew-printed-prompt-v2`; PR D adds a new config-identity boundary on
top of that route-specific prompt version.
Source/route/prompt/page/fencing/lease/persistence/assembly unchanged. No
page-level `DocumentTextResult` rows.

**2026-08-03 initial output-cap follow-up:** The original
`GEMINI_MAX_OUTPUT_TOKENS=2048` value entered the project as an early
page-by-page OCR heuristic. No corpus measurement, Gemini model limit, cost
calculation, or live-document validation was recorded for that value. Later
configuration centralization and PR D preserved it for compatibility rather
than revalidating it.

Live French-handwritten document 272 provided direct evidence that
the initial cap was too low for this corpus: page 1 consumed exactly 2,048
candidate tokens, produced 4,862 characters, and finished with `MAX_TOKENS`.
The following call at 8,192 returned zero output with `RECITATION`. The second
result does not prove that the larger cap caused `RECITATION`, because it was a
separate generation.

A separate `GEMINI_OCR_MAX_OUTPUT_TOKENS` worker setting is therefore
introduced with default **4096**. The existing shared
`GEMINI_MAX_OUTPUT_TOKENS` remains **2048** for Hebrew translation and no
longer controls worker OCR. The existing PR D retry algorithm remains
unchanged, producing the bounded sequence **4096 → 8192 → 16384**.
Temperature, prompt, model, routing, three-call budget, hard cap, parsing,
failure classifications, and Hebrew translation behavior remain unchanged;
`RECITATION` remains permanent and is not retried. This is not a retry-policy
change, so `gemini-ocr-page-retry-v1` remains current. `max_output_tokens` is
already included directly in the Gemini configuration fingerprint, so the new
OCR value creates a new checkpoint identity without a policy-version bump,
migration, or mutation of historical rows.

## Bounded RECITATION model fallback for French/English handwriting

**Status:** Merged as PR #374 at `9b51f45`, built in worker image
`20260804045900`, deployed worker-only, and live-validated on document 273.
The candidate chain remains current for English handwriting but is superseded
for French handwriting by the 2026-08-04 follow-up below.

**Live evidence:** Raising the worker OCR initial cap from 2048 to 4096 was
successfully deployed and produced a new configuration/attempt identity, but it
did not solve French-handwritten document 273. Before that deployment, request
11 produced `MAX_TOKENS` at 2048 and then `RECITATION` with zero output at
8192. After deployment, request 12 used the new 4096 cap and failed immediately
on page 1 with `RECITATION`, zero output, and model `gemini-2.5-flash`.
French-handwritten document 272 had shown the same broad pattern under the old
cap: `MAX_TOKENS` at 2048 followed by zero-output `RECITATION` at 8192. These
runs show that the 4096 separation was activated correctly but that output-cap
size alone is insufficient. They do not prove that a larger cap causes
`RECITATION`.

**PR #374 decision (historical for French; still current for English):**
English/French handwritten Gemini OCR initially received the ordered model
chain `gemini-2.5-flash` → `gemini-3.1-flash-lite`. In the durable
checkpoint-backed worker path only, `RECITATION` from the active model may
advance once to the next configured model when provider-call budget remains.
`RECITATION` is still terminal for the model that returned it; the same model
is not called again merely because it returned `RECITATION`.

**Bounded call policy:** English/French handwriting shares one global maximum
of **three provider calls per page across the candidate chain**. If the first
model returns `RECITATION` on call 1 at 4096, the fallback model receives at
most the two remaining calls, starting at 4096. If the first model reaches
8192 before returning `RECITATION` on call 2, the fallback receives one call at
8192. The current output cap is carried into the fallback; it is not reset
downward. A second `RECITATION` is persisted immediately, without spending an
otherwise-unused call on the same fallback model.

**Scope boundaries:** No prompt, temperature, top-k, top-p, output mode,
parsing, hard cap, page splitting, continuation, translation, or provider
change. `SAFETY`, `LANGUAGE`, `SPII`, `JSON_SCHEMA`, prohibited/blocked
content, and other permanent classifications do not advance to another model.
Legacy direct adapter calls without document/checkpoint identity do not gain
the new `RECITATION` fallback. Outside English/French handwriting, the
pre-existing quota-only candidate fallback retains a fresh bounded three-call
budget for the next candidate. Inside the scoped route, quota and `RECITATION`
share the same global three-call ceiling.

**Identity consequence:** The ordered candidate list changes the configuration
and overall attempt identity for English/French handwritten routes. The retry
policy marker advances from `gemini-ocr-page-retry-v1` to
`gemini-ocr-page-retry-v2`; because that marker is part of the shared Gemini
configuration fingerprint, it intentionally creates a new configuration
identity for every Gemini OCR route. No migration or historical-row mutation
is required. Existing attempts/checkpoints remain immutable and are not
silently reused under the new policy.

**Validation evidence:** Ruff format check reported all six scoped Python
files already formatted; Ruff lint passed. Django system check passed and
`makemigrations --check --dry-run` reported no changes. Scoped Pyright reported
0 errors and 0 warnings; scoped mypy reported no issues in four production
files. The initial focused Django run executed 57 tests; the expanded Gemini,
checkpoint, worker, routing, and prompt-contract run executed 108 tests. Both
completed `OK`. The full `documents` regression then ran 2,330 tests in
1,777.543 seconds and completed `OK` with `FULL_TEST_STATUS=0`.
`git diff --check` passed.

**Validation still required:** Final staged review, merge, worker-first
deployment, and one intentional live retry of document 273. A successful
provider result still requires human fidelity review in the site before
document 272 is retried.

**PR B/C preservation:** Database-persistence retryable boundary (PR B) and
route-specific Hebrew printed prompt version (PR C) unchanged. Successful
checkpoint pages are not re-executed; only failed/missing pages run again.

**Privacy:** Logs and persisted failures remain operational metadata only — never
prompt, response text, document text, or provider exception text.

**Rejected alternatives:** Infinite retry, JSON repair, continuation prompts,
page splitting, cross-layer retry loops, routing/prompt/translation/worker/DLQ
changes.

**Reference failure classes (synthetic tests only):** Documents 271 and 277
modeled as transient `EMPTY_RESPONSE`; 289 and 291 as `MAX_TOKENS` truncation.
Document 293 remains PR C (Hebrew printed plain-text contract).

**Validation evidence:** Focused PR D validation: the initial focused run
executed **79** tests. One test failed only because its expected final-attempt
output-length metadata still described the former second-attempt fixture; the
test fixture/assertions were corrected **without production-code changes**; the
corrected test passed in isolation; the new quota traceback-privacy test also
passed in isolation. Because the test-only correction followed the initial run,
the focused evidence is complementary rather than one uninterrupted 79/79 OK run.
Static validation: Ruff formatting was applied to
the two affected test files; the final format check reported all **10** scoped
files formatted. Ruff lint passed. Django system check passed. Migration check
reported no changes. Scoped Pyright: 0 errors and 0 warnings. Scoped mypy: no
issues in **7** files. `git diff --check` passed. Full `documents` regression:
one uninterrupted suite ran **2,286** tests in **1,886.264** seconds; result
**OK**; `TEST_EXIT_CODE=0`.

**Validation still required:** Live Gemini provider/fidelity validation;
deployment through the existing worker-first runbook; production-document
retries.

**Unchanged / deferred:** OCR routing, prompts/output contracts (except identity
hash inputs for retry policy), translation, Transkribus, Antigravity, worker/SQS
ack/DLQ, UI, models/migrations, deployment, production documents, MIXED
printed/handwritten (PR E).

## Explicit MIXED printed/handwritten Gemini OCR route (PR E)

**Status:** Implemented and focused/static validated on branch
`feat/gemini-mixed-content-pr-e`. **Not yet merged.**

**Decision:** `MIXED` is an explicit **manual document-level**
`Document.text_input_type` choice alongside `HANDWRITTEN` and `PRINTED`. It
does not classify individual pages, and no automatic printed/handwritten
content detection or per-page route selection was introduced. Every page of a
MIXED document uses **one** mixed printed/handwritten Gemini prompt contract;
the contract covers pages that are entirely printed, entirely handwritten,
mixed within the same page, or a printed form filled in by hand. Both
meaningful printed and handwritten content must be transcribed.

**Routing:** Static `OCR_ROUTES` gains explicit
`(he|en|fr|ar, MIXED) → GEMINI / prompt_variant=mixed` entries.
`select_ocr_route` continues to reject unknown `text_input_type` values with
`ValueError` — nothing silently routes as MIXED. Hebrew MIXED does not pass
through the Transkribus Hebrew-handwritten gate; Arabic MIXED is not routed to
Antigravity. Model candidates for MIXED resolve to
`DEFAULT_GEMINI_MODEL_CANDIDATES` via the existing `gemini_model_candidates`
fallthrough (no model-selection change).

**Prompt/output contract:** The approved mixed prompt is stored verbatim as
`_MIXED_CONTENT_PROMPT` in `gemini_engine.py` and is a **closed product
contract** — wording, punctuation, examples, ordering, uncertainty markers
(`[?]` / `[UNCLEAR]`), and backticks are preserved exactly and pinned by a
SHA-256 assertion in `test_gemini_mixed_content.py`. Output is **raw plain
text** (`v1beta`, effective temperature 0, thinking budget 0) for every
language hint; the mixed route never uses JSON parsing. Uncertainty metadata
derives from the `[?]` / `[UNCLEAR]` markers through the shared plain-text
page path.

**Contract identity:** New route-specific version
`GEMINI_MIXED_PROMPT_CONTRACT_VERSION` = `gemini-mixed-content-prompt-v1`,
selected through the `_is_mixed_content_contract` predicate (same
single-predicate pattern as PR C Hebrew printed, so prompt, output mode, and
version cannot drift apart). The mixed prompt fingerprint and contract version
participate in the Gemini attempt identity exactly like the other
route-specific contracts, so checkpoints created under any other prompt
contract are **not** reused for MIXED, and identical MIXED inputs keep a
stable identity. The general `gemini-ocr-prompt-v1` and Hebrew printed
`gemini-hebrew-printed-prompt-v2` identities are not reused and not changed.

**Schema/migration:** `MIXED` was added to the application-level
`Document.TextInputType` and `DocumentTextResult.OcrPromptVariant` choices.
Migration `0048_document_mixed_text_input_type_and_prompt_variant` was
generated; it contains two model-state `AlterField` operations for
`Document.text_input_type` and `DocumentTextResult.prompt_variant`. Both
columns remain unconstrained `CharField`s; the migration updates
application-level choices only and makes no effective database
column/schema change (no type, default, nullability, index, or constraint
change).

**PR B–D preservation:** Durable page checkpoints/resume, successful-page
reuse under identical attempt identity, bounded three-call recovery, retry
classifications, deterministic backoff and output-token hard-cap ladder,
privacy/safe-error boundaries, and quota-only ordered model-candidate fallback
are unchanged; MIXED flows through the same adapter and engine paths.

**No per-page persistence:** No page classification field was added to
`PageImage`, `Document`, or `GeminiOcrPageCheckpoint`; no mixed-specific
database persistence exists.

**Rejected alternatives:** Automatic printed/handwritten classification,
per-page routing or per-page prompt contracts, reusing the printed or
handwritten prompts for mixed pages, heuristic fallback that interprets
unknown values as MIXED.

**Validation evidence:** The final focused suite ran **82** tests in
**5.251** seconds; result **OK**; `FOCUSED_TEST_EXIT=0`. Ruff formatted one of
the two corrected Python files; the subsequent Ruff format check reported both
scoped files already formatted. Ruff lint passed for both scoped files. Django
system check passed. `makemigrations --check --dry-run`: no changes. Pyright: 0
errors and 0 warnings. mypy: no issues in **6** source files.
`git diff --check` passed. `migrate --plan` succeeded and included migrations
**0045–0048**, with **0048** containing the two expected `AlterField`
operations. Full `documents` regression completed successfully on 2026-08-03:
**2,309** tests ran in **1,870.289** seconds; result **OK**;
`FULL_TEST_EXIT_CODE=0`. Django system check identified no issues. Full log:
`/tmp/vs_archive_gemini_pr_e_full_documents_regression_2026-08-03.txt`
(SHA-256
`d03f7f7f1a3ad25841d1eb46849128d307c792608c7172eeb5a5957463b653b6`).
Tracebacks, ERROR logs, and warnings emitted during the run belong to
intentional negative-path tests and did not represent test failures.

**Validation still required:** Live Gemini provider/fidelity validation,
deployment, and any production-document retries remain outstanding. **PR E is
not merged.**

**Unchanged / deferred:** Translation behavior, Hebrew printed prompt/contract,
general Hebrew handwritten prompt/contract, Transkribus, Antigravity,
worker/SQS ack/lease/DLQ/durable requests, Gemini retry policy/budget/backoff/
hard-cap configuration, provider models and candidate ordering, unrelated UI.

## General Hebrew handwritten prompt anti-runaway experiment (PR #371)

**Status:** Rejected after live validation. PR #371 was merged as `cb69ec0`,
built as image `20260803122739`, and deployed, but the runtime was rolled back
to image `202608031223` after the experiment failed on document 291. This code
rollback restores the source contract used by `202608031223`; merge and a new
deployment are still required to remove the source/runtime drift.

**Evidence before PR #371:**

* Document 289, `hebrew_general_handwritten`, page 1 made three bounded
  provider calls and still returned `MAX_TOKENS`; the final call recorded
  `raw_output_length=30163`, `candidates_token_count=16384`, and
  `max_output_tokens=16384` for a relatively sparse page.
* Document 291 under `gemini-ocr-prompt-v1` created DB attempt 3. Page 1
  succeeded and was durably saved as checkpoint 4 with 961 characters; page 2
  failed as checkpoint 5 with `MAX_TOKENS` after three calls and
  `raw_output_length=30369`. The attempt remained `PARTIAL`, missing page 2.

These observations show provider runaway repetition or hallucinated
continuation. PR D's bounded recovery and PR B's page persistence behaved as
designed, but neither accepted or repaired the runaway output.

**Rejected experiment:** PR #371 changed only
`_HEBREW_GENERAL_HANDWRITTEN_PROMPT`. It added instructions against image
description, invented text from non-text regions, repetition, continuation,
and padding; it also introduced the route-specific version
`gemini-hebrew-general-handwritten-prompt-v2`. The change passed 43 focused
tests, Ruff, Django and migration checks, Pyright, mypy, `git diff --check`,
and the full 2,318-test `documents` regression. Those results established
local correctness, not provider fidelity.

**Live rejection evidence:** Request 8 on document 291 used DB attempt 8 and
the v2 prompt contract. Its page 1 did not reuse the earlier v1 checkpoint,
which proves the checkpoint identity change worked. Instead, page 1 itself
failed as checkpoint 15 after three calls with `MAX_TOKENS`,
`raw_output_length=23991`, and `candidates_token_count=16384`; page 2 did not
run. The request ended `PARTIAL` with `missing_pages=1,2`, and no new
`DocumentTextResult` was created. A page that had succeeded under v1 therefore
failed under v2, so prompt-only hardening was rejected as a production
solution.

**Rollback decision:** Restore the exact pre-experiment prompt (SHA-256
`c576010ed8127d4cd1c65c01d09d5641396dce2fed49374730de60cc342cf863`)
and the shared `gemini-ocr-prompt-v1` contract for
`hebrew_general_handwritten`. Remove the v2 constant and selection path. Keep
the route on Gemini with plain-text `v1beta` output and effective temperature
0. Keep Hebrew printed on `gemini-hebrew-printed-prompt-v2`, MIXED on
`gemini-mixed-content-prompt-v1`, and keep PR D's configuration fingerprint,
retry budget, classification, backoff, token ladder, and hard cap unchanged.

The rejected v2 prompt SHA-256
`ce9aef4f083d6493db5e344b5f8405676bd75f019741bd7aaaa52785a3126988`
and contract name remain in regression tests and this incident record only.
Existing v2 attempt/checkpoint rows remain immutable history; current v1
identity will not reuse them. Request 8, attempts 3/8, and checkpoints 4/5/15
must not be deleted or rewritten.

**Root cause remains open:** This rollback does not solve Gemini runaway and
does not authorize partial salvage, silent acceptance, new prompt experiments,
or additional production retries. A deterministic design must separately
evaluate safe repetition metrics, post-response rejection versus streaming
early stop, caps/models/preprocessing, and explicit salvage policy. Documents
289 and 291 are incident evidence and must not be retried meanwhile.

**Unchanged:** PRs #366–#370; migrations 0047/0048; routing; model candidates
and ordering; page persistence/resume; retry count and classifications;
backoff, token budgets, and hard cap; output parsing; worker/SQS lifecycle;
Hebrew printed and MIXED prompts; Transkribus; Antigravity; incident rows.

## French handwritten best-available full-page Gemini 3.6 route

**Status:** Implemented and focused/static validated on branch
`fix/gemini-french-handwritten-3-6-flash`; not yet merged or deployed.

**Product constraint and acceptance policy:** The archive owner does not read
French and cannot perform a line-by-line French fidelity review. For this
material, an imperfect best-available transcription is preferable to leaving
the document without OCR. The result is therefore preserved as source-language
`SOURCE_TEXT`, while the existing automatic OCR lifecycle continues to mark it
`NEEDS_REVIEW` and `UNVERIFIED`. `READY` means displayable, not
human-verified. Hebrew translation remains a later, separate result and cannot
repair source-transcription errors.

**Production evidence after PR #374:** Document 273 request 13 used the merged
two-model chain. Page 1 received zero-output `RECITATION` from
`gemini-2.5-flash` and then succeeded through `gemini-3.1-flash-lite` with 669
characters, but visual review showed substantial omissions and uncertain or
incorrect readings. Page 2 exhausted all three calls on
`gemini-2.5-flash` with `MAX_TOKENS`: output grew from 7,297 to 15,151 to
30,316 characters while the page contained only about twenty handwritten
lines. The fallback model received no remaining call budget, the attempt
remained partial, and pages 3–4 were not started.

**Model probes:** Direct non-persistent probes were run against pages 1 and 2.
`gemini-3.6-flash` with `thinking_level=minimal`, model-default decoding, and a
4096 output cap returned `STOP` for both full pages (938 and 660 characters).
`gemini-2.5-pro` required a minimum thinking budget and returned shorter,
less reliable text with conspicuous unsupported readings. It was rejected.
`gemini-3.6-flash` was the best available tested model, although its output is
not claimed to be verbatim or human-verified.

**Segment experiment:** Page 1 was also split into three overlapping horizontal
bands. All three calls returned `STOP`, but the combined output introduced
overlap duplication and contradictory readings, including `1943` versus
`1953`, and still omitted lower-page material. Automatic stitching was rejected
because it would convert local uncertainty into duplicated or contradictory
canonical text. Production remains full-page only.

**Decision:** French handwritten OCR now uses one direct full-page candidate:
`gemini-3.6-flash`. Its generation profile uses minimal thinking and omits
explicit temperature, top-k, and top-p so the model uses its decoding defaults.
The legacy `thinking_budget=0` plain-text profile remains unchanged for other
models. English handwritten OCR keeps the PR #374 chain
`gemini-2.5-flash` → `gemini-3.1-flash-lite` and its bounded
`RECITATION` model switch. No segmented fallback is introduced.

**Checkpoint identity:** The French ordered candidate list changes from
`gemini-2.5-flash` → `gemini-3.1-flash-lite` to the single
`gemini-3.6-flash` candidate. Because candidates are part of the configuration
and overall attempt fingerprints, French handwritten retries receive a new
identity and cannot reuse the earlier page-1 checkpoint. The shared
`gemini-ocr-page-retry-v2` marker and three-call ceiling remain unchanged.
Existing attempts and checkpoints stay immutable; no migration is required.

**Unchanged:** Latin handwritten prompt text and prompt-contract version,
4096 initial OCR cap, hard-cap ladder, failure taxonomy, parsing, page
checkpoint persistence, document assembly, translation behavior, English and
Hebrew routing, Transkribus, Antigravity, worker/SQS behavior, database schema,
and UI.

**Validation:** Ruff format/check passed. Django system and migration checks
passed. Scoped Pyright and mypy passed after using the SDK-typed
`types.ThinkingLevel.MINIMAL` enum. The expanded French-model,
RECITATION, bounded-recovery, checkpoint, and routing suite ran 60 tests and
completed `OK`. The full `documents` regression ran **2,333 tests** in
**1,947.639 seconds** and completed `OK`, with `FULL_TEST_STATUS=0`.
Pre-test and post-test `git diff --check` both passed.

**Validation still required:** Final staged review, merge, worker-only image
build/deploy, and one intentional reprocessing of document 273. If the new
attempt completes, the best available French source text may be accepted for
display as `NEEDS_REVIEW` / `UNVERIFIED`; the owner is not expected to certify
French fidelity before document 272 is retried.

## Hebrew general handwritten cost-aware Gemini 3.6 fallback

**Status:** Implemented and under validation on branch
`fix/hebrew-general-handwritten-gemini-36`; not yet merged or deployed.

**Failure evidence:** Documents 289, 291, and 306 are
`hebrew_general_handwritten` documents with valid source pages. Their earlier
Gemini 2.5 Flash attempts failed through runaway output rather than upload,
queue, routing, or checkpoint failure. Examples include document 289 page 1
ending with `MAX_TOKENS` after 30,163 raw characters, document 291 page 2 after
30,369 raw characters, and document 306 page 1 after 26,247 raw characters.
The rejected Hebrew prompt experiment also caused document 291 page 1 to fail
after 23,991 raw characters. Increasing the 2.5 output cap therefore produced
more unsupported continuation rather than a usable transcription.

**Prompt decision:** The rejected prompt experiment remains rolled back. The
general Hebrew handwritten prompt retains `gemini-ocr-prompt-v1` and SHA-256
`c576010ed8127d4cd1c65c01d09d5641396dce2fed49374730de60cc342cf863`.
This follow-up changes model selection and bounded fallback, not the
transcription instructions.

**Non-persistent provider evidence:** Full-page Gemini 3.6 Flash probes used
the restored production prompt, `thinking_level=MINIMAL`, model-default
decoding, a 4096 output cap, and one provider call per page. All eight pages
completed with `STOP`: document 289 pages 1–5 returned 708, 454, 395, 295, and
258 characters; document 291 pages 1–2 returned 1,535 and 1,276 characters;
document 306 page 1 returned 1,321 characters. The results are imperfect but
bounded, coherent, and materially preferable to having no transcription.

**Cost-aware decision:** Hebrew general handwritten OCR uses the ordered chain
`gemini-2.5-flash` → `gemini-3.6-flash`. The primary 2.5 model receives one
4096-token provider call. A successful 2.5 result is persisted immediately and
3.6 is not called. Primary `MAX_TOKENS` or `RECITATION` advances immediately to
3.6 instead of spending calls on the 8192/16384 2.5 runaway ladder. Gemini 3.6
receives at most the two remaining calls within the existing global maximum of
three calls per page. Existing quota candidate fallback remains bounded by that
same shared budget. Other permanent response classifications do not advance.
There is no segmented path.

**Scope:** This applies only to checkpoint-backed OCR on the explicit Hebrew
GENERAL handwritten route. Hebrew VS handwriting remains on Transkribus.
Hebrew printed, mixed-content, French, English, Arabic, Antigravity,
translation, and all other routes remain unchanged.

**Checkpoint identity:** The ordered candidates change from the default
`gemini-2.5-flash` → `gemini-3.1-flash-lite` chain to the Hebrew-specific
`gemini-2.5-flash` → `gemini-3.6-flash` chain. Because candidates are part of
the configuration and overall attempt fingerprints, later Hebrew general
handwritten attempts receive a new identity. The restored prompt fingerprint,
prompt version, and shared `gemini-ocr-page-retry-v2` marker remain unchanged.
Historical attempts and checkpoints remain immutable; no migration is required.

**Validation required:** Re-run formatting, lint, Django checks, typing,
expanded Gemini/checkpoint/routing tests, the full `documents` regression,
staged review, merge, worker-only deployment, and intentional production
validation.

## Checkpoint-backed Gemini PARTIAL documents remain reprocessable

**Status:** Implemented and under validation on branch
`fix/partial-gemini-checkpoint-reprocess`; not yet merged or deployed.

Gemini page-checkpoint failures can terminate a document as `PARTIAL` before a
failed `DocumentTextResult(SOURCE_TEXT)` row exists. OCR reprocess eligibility
now accepts a failed `GeminiOcrPageCheckpoint` as durable source-OCR failure
evidence when the existing safeguards also hold: the document routes to Gemini,
has no usable source text, has no VERIFIED text result, is uploaded, and belongs
to an OCR archive item.

A generic `PARTIAL` document remains ineligible without explicit failed source
OCR evidence. Active-request coalescing and recovery behavior remain owned by
the durable `ProcessDocumentRequest` enqueue service.

Production evidence motivating this change is document 306: its sole historical
Gemini attempt was `PARTIAL`, page 1 checkpoint was `FAILED/MAX_TOKENS`, and no
`DocumentTextResult` was created. This caused both the detail-page action and
the backend assessment to reject an otherwise recoverable OCR failure.

## Gemini OCR attempt lifecycle / provider-call observability clarification

**Decision:** Preserve the existing `GeminiOcrAttempt` lifecycle from durable
page checkpoints. `PARTIAL` is a resumable, non-terminal attempt state: an
identical future execution may reuse successful page checkpoints, reclaim only
failed/missing pages, and move the same attempt back to `IN_PROGRESS`.
`completed_at` therefore remains `NULL` for `IN_PROGRESS` and `PARTIAL` and is
set only after complete deterministic assembly reaches `COMPLETED`. No schema,
migration, or historical-row rewrite is required.

**Observability clarification:** `GeminiResponseMetadata.attempt` is retained
for internal compatibility, but its value is the global 1-based Gemini provider
call ordinal for the page, not a `GeminiOcrAttempt` database id. New safe
diagnostics label it `provider_call_ordinal`. Retry logs also expose the local
`candidate_call=N/M` separately so a fallback-model call cannot be mistaken for
a database attempt id or for an `N-of-M` global call count.

**Unchanged:** provider-call budget, fallback policy, response classification,
checkpoint identity/reuse, persistence semantics, processing-state behavior,
and historical incident evidence.

## Transkribus binding freshness is the hover/geometry trust gate

**Decision:** Hover geometry and any future jump-to-match / text↔image alignment
must be exposed only through a **current, trustworthy**
`TranskribusTextResultBinding`. Document ownership of a
`TranskribusTranscriptSnapshot` alone is **never** sufficient.

**Current behavior / primitive:** Read-only fail-closed assessment lives in
`documents/services/transkribus_binding_freshness.py`
(`assess_binding_freshness`, `is_binding_structurally_fresh`,
`is_binding_trusted_for_hover`). It reuses the same trust invariants already
enforced by corrected/current activation (ready snapshot, verified canonical
SHA, role matched to `DocumentTextResult.result_type`, bound hash equals
verified canonical hash, current text SHA equals bound hash, revision
alignment for SOURCE/`source_revision` and HEBREW/`based_on_source_revision`,
`bound_source_revision >= 1`). Unsupported result types fail closed.

**Structural freshness vs hover trust:** A binding may be structurally fresh
(baseline trustworthy for activation / edit-history proofs) while
`snapshot.hover_eligible` is False. Hover trust requires structural freshness
**and** `hover_eligible=True`. Activation continues to allow
`hover_eligible=False` snapshots.

**Non-goals for this PR:** UI, hover JavaScript, search integration,
jump-to-match endpoints, image overlays, schema/migrations. Local completion
keeps its distinct never-bound / human-edited-after-bind / corrupt semantics
and only reuses equivalent lower-level predicates.

## Transkribus trusted text-range geometry resolver

**Decision:** Text-to-image geometry for Hover / Jump-to-match is resolved only
from a current `DocumentTextResult` through its trusted
`TranskribusTextResultBinding`. The resolver never falls back to another
snapshot owned by the same document.

Text ranges use Python half-open coordinates: `[start, end)`.

A stored Transkribus line intersects a requested range only when:

- `line.char_start < end`
- `line.char_end > start`

This preserves the snapshot parser's existing canonical-text offset contract.

**Separators:** Canonical line separators (`\n`) and page separators (`\n\n`)
have no geometry of their own. A separator-only range resolves to no geometry.
A range spanning text on both sides of a separator may resolve to multiple
lines and, when applicable, multiple pages.

**Fail-closed geometry:** Resolution requires the binding to be trusted for
hover. Every intersecting contributing line must also contain safe stored
polygon and bounding-box geometry. If any intersecting line cannot be safely
resolved, the entire request returns no geometry rather than a partial or
potentially misleading highlight.

Results are deterministic and ordered by snapshot `page_index`, then
`order_index`.

**Non-goals for this PR:** search integration, matching/query generation,
public endpoints, HTML/JavaScript hover behavior, scrolling/jump UI, polygon
merging, or schema changes.

## Archive search matches map to geometry only through exact displayed-text ranges

**Decision:** Hover / Jump-to-match must not derive image geometry directly
from PostgreSQL full-text-search positions or from normalized
`ArchiveItemSearchIndex.body_text`.

Public archive search may match through PostgreSQL FTS even when no exact,
safe substring range can be reconstructed in the displayed
`DocumentTextResult`. Search results remain valid in that case, but no
hover/jump target is exposed.

`archive_search_match_ranges.py` therefore:

- reuses the public archive query tokenization contract;
- resolves the exact `DocumentTextResult` rows used by the displayed
  transcription and, where applicable, displayed Hebrew translation;
- searches the original `DocumentTextResult.text`, not the stripped search
  index body;
- emits only exact case-insensitive literal `[start, end)` ranges whose
  offsets are safe in the original string;
- may return multiple occurrences for a term;
- preserves deterministic order: query-term order, occurrence order, primary
  transcription before separate translation;
- never changes whether an `ArchiveItem` itself is a valid search result.

Geometry projection is a second fail-closed step. Each exact text match is
passed to the trusted Transkribus text-range geometry resolver. Matches without
a current trusted binding, with stale provenance, with hover-ineligible
snapshots, or otherwise without safe geometry are omitted.

**Non-goals for this PR:** changing public search ranking/filtering, generating
search snippets, choosing a single current/next occurrence, templates,
JavaScript hover behavior, scrolling, image overlays, or schema changes.

## Transkribus corrected/current staff sync enqueue UI (fetch/sync only)

**Decision / implemented:** Authorized staff may request a fresh corrected/current Transkribus sync from the existing attempts list page via POST `corrected_current_sync_enqueue` → `enqueue_transkribus_corrected_current_sync(...)`. This is enqueue-only: it does **not** activate or replace the displayed transcription; preview/diff/explicit activation remain unchanged.

**Boundary:** `login_required` + `@require_POST` + `_require_admin_page` + `get_viewable_document` + CSRF. Eligibility reuses `_is_transkribus_corrected_current_sync_ui_eligible` (fail-closed on `TranskribusPageXmlGeometryError`). Hiding the button is not authorization.

**UX:** Form label **משיכת תעתוק עדכני מ־Transkribus** on `corrected_current_sync_attempts.html`; PRG redirect back to the attempts list with Hebrew Django messages mapped from enqueue outcomes (`CREATED_AND_ENQUEUED`/`REENQUEUED`, `ALREADY_QUEUED`, `ALREADY_RUNNING`, `BLOCKED_RECOVERY_REQUIRED`, `ENQUEUE_FAILED`, `ENQUEUE_OUTCOME_UNKNOWN`, `ALREADY_TERMINAL`). No polling/JS added.

**Still deferred:** feature gate; stranded-`QUEUED` recovery/requeue; automatic activation.

## Transkribus paragraph presentation metadata (PR1 — persistence and proof only)

**Decision / implemented:** v1 paragraph grouping is Transkribus-only presentation metadata. The normal workflow is: correct transcription in Transkribus, pull/activate the corrected snapshot in VS-Archive, then staff save paragraph boundaries in VS-Archive (staff UI is **not** in this PR).

**Current behavior:**

- `TranskribusParagraphMapping` is owned by one `TranskribusTranscriptSnapshot` (one mapping maximum per snapshot). Stored `document` must match `snapshot.document`. `copied_from` is the source of the **current** saved paragraph division, not permanent ancestry. An explicit future adoption write sets it; an ordinary/manual resave through `save_paragraph_mapping` without `copied_from` clears it. Create without `copied_from` remains valid (null). There is no adoption UI in this PR.
- `save_paragraph_mapping` may omit `actor` on create. On resave, a provided `actor` updates `updated_by`; `actor=None` preserves the existing editor.
- `TranskribusParagraphBreak` means: paragraph break **after** this contributing source line. Identity is the snapshot-line PK, not `provider_region_id`, provider paragraph ids, char offsets, or hover ids such as `p1-o3`.
- Mapping row + zero breaks = staff explicitly saved one flowing paragraph. No mapping row = grouping has never been saved for that snapshot.
- Page boundary and paragraph boundary are independent: a paragraph may cross a page when there is no break after that page's final contributing line.
- Services in `transkribus_paragraph_mapping.py` retrieve contributing lines, validate breaks, save/replace a mapping transactionally, and assess currentness. Re-saving replaces that snapshot's break rows only.
- Public/current applicability uses the existing Transkribus baseline: displayed `DocumentTextResult`, a `TranskribusTextResultBinding`, `is_binding_structurally_fresh(...)`, and `binding.snapshot_id == mapping.snapshot_id`. `hover_eligible=False` does **not** by itself make a mapping non-current.
- Local `DocumentTextResult` text/revision drift makes the mapping non-current (structural freshness fails) but the mapping row is kept. A new snapshot/rebinding makes an old mapping non-current because mappings belong to the snapshot on which they were authored.
- Cross-snapshot transfer proof (`transkribus_paragraph_correspondence.py`) is strict and fail-closed. It allows differing line text. It requires same document, both READY, same parser version, matching ordered `page_index` structure, matching per-page contributing-line counts, complete unique `(page_index, provider_line_id)` identities, and identical ordered identity sequences. `(page_index, provider_line_id)` is the identity because PAGE XML TextLine ids are page-scoped and `provider_line_id` is not unique at snapshot level.
- Historical suggestion discovery is read-only: older snapshots that already have mappings, newest-to-oldest, filtered through that proof. Suggestions may come from more than the immediately previous snapshot. Discovery never writes, adopts, or overwrites. A current mapping on the target does not suppress suggestions.

**Must not change (this PR and later paragraph work unless explicitly approved):** `DocumentTextResult.text`, `TranskribusTranscriptSnapshot.canonical_text`, `TranskribusSnapshotLine.text`, source geometry, canonical char offsets, hover IDs, search offsets.

**Not implemented (later PRs, do not treat as done):** automatic adoption; Gemini/general-provider paragraph mappings; AI/fuzzy matching; dehyphenation; same-snapshot local typo remapper. Staff paragraph editor/status is implemented in the PR3 entry below. Public paragraph rendering is implemented in the PR2 entry below. Historical suggestion/adoption is implemented in the PR4 entry below.

## Transkribus paragraph public rendering (PR2 — presentation overlay only)

**Decision / implemented:** Public v1 paragraph presentation is Transkribus-only. An applicable **current** mapping activates human-paragraph markup on the public document detail transcription. Paragraph grouping is an explicit presentation overlay: it wraps existing hover/search/plain fragments. It does **not** create a replacement display string, mutate `DocumentTextResult.text`, mutate snapshot canonical text, or recompute search offsets from browser-visible text.

**Current behavior:**

- Applicability reuses PR1 `assess_paragraph_mapping_currentness`. No mapping, stale mapping, other-snapshot mapping, structurally stale binding, or locally drifted displayed text → `enabled=False` and the **legacy** renderer is used unchanged.
- A current mapping with **zero break rows** is not fallback: the contributing transcription is one flowing paragraph, including across source lines and pages.
- Paragraph breaks after contributing source lines produce visual/semantic `<p>` separation. A source-page boundary does not automatically create a paragraph; a paragraph may contain lines from more than one page.
- Canonical characters, including `\n` and `\n\n`, remain in the DOM in document order. Concatenating text-bearing DOM content reconstructs the canonical transcription character-for-character. Newlines are not replaced with spaces in the text data.
- Visual flow inside a paragraph comes from CSS: the paragraph container uses normal whitespace so canonical separators collapse visually; source-line spans keep `pre-wrap` so intra-line spaces are not destroyed.
- Source-line hover IDs, overlay bbox calculation, and `pointer-events: none` overlays are unchanged. Search match indexes, click-sync attributes, previous/next navigation, and sticky source behavior keep the existing identities/order.
- Gemini, other non-Transkribus OCR, manual text, photo, and video public paths are unchanged. No new public paragraph endpoint.

**Not implemented (later PRs):** automatic inheritance; paragraph inference; Gemini/general-provider paragraph support; transcription editing; dehyphenation; canonical-text rewriting. Staff paragraph editor/status is implemented in the PR3 entry below. Historical suggestion/adoption is implemented in the PR4 entry below.

**Explicit non-goals for correspondence:** canonical-text equality, char offsets, fuzzy text, geometry epsilon, `provider_region_id`, provider page identity alone, raw XML equality, or matching line counts alone.

## Transkribus paragraph staff editor and status (PR3 — editor UI only)

**Decision / implemented:** v1 staff paragraph editing is Transkribus-only and is not a transcription editor. Workflow remains: correct text in Transkribus, pull/activate in VS-Archive, then define paragraph boundaries in VS-Archive. The editor operates on original Transkribus source-line structure (not flowed prose). Every source line is not a paragraph; absence of a mapping means grouping was never saved; a saved mapping with zero breaks is an explicit one-paragraph save.

**Current behavior:**

- Route: `ui/admin/documents/<id>/transkribus-paragraphs/` (`transkribus-paragraphs`). Belongs to the existing Transkribus versions/management family. No new top-level staff nav item. Review-detail is unchanged (text review only).
- Authorization reuses Transkribus versions: `login_required` + `_require_admin_page` + `get_viewable_document` + CSRF. Restricted items still require `documents.view_restricted_archiveitem`.
- Editor layout reuses document-detail two-column source/text CSS. Source lines stay in page/order sequence. Page transitions are labeled and are **not** preselected as paragraph breaks. A paragraph may cross pages. There is no break control after the final contributing line. Source text is not editable.
- Save delegates to PR1 `save_paragraph_mapping(...)` (ordinary/manual save, no `copied_from`). POST/PRG. Staff actor is `request.user`. Zero selected breaks is a valid explicit save. Canonical text, snapshot lines, geometry, offsets, and bindings are not mutated.
- Freshness: POST carries `expected_document_id`, `expected_text_result_id`, and `expected_snapshot_id`. If the displayed result, bound snapshot, or structural freshness no longer matches, the write is refused with a staff-facing Hebrew message that does not expose raw IDs.
- Status (current displayed/bound snapshot only): never saved; saved one paragraph; saved N paragraphs. An old-snapshot mapping alone does not count as the current division; versions/detail may note that a historical mapping exists, without adopt/copy controls. Historical suggestion/adoption UI is implemented in the PR4 entry below.
- Status locations: Transkribus versions card “תעתוק Transkribus המוצג כעת”; compact staff-only note on document detail. Entry point: “עריכת חלוקת פסקאות”.
- Existing source-line hover JS is reused in the editor when hover overlays already exist; hover is not required for editing.
- No live public preview in this PR.

**Not implemented (later PRs, do not treat as done):** overwrite confirmation for historical suggestions; AI paragraph inference; Gemini/general-provider paragraph support; transcription editing; dehyphenation.

## Transkribus paragraph historical suggestion/adoption (PR4 — explicit create-only)

**Decision / implemented:** A historical Transkribus paragraph mapping may be **offered** as a staff suggestion. The manager decides whether to adopt it. Nothing is copied automatically. Adoption creates a **new** mapping for the current displayed/bound snapshot. The historical mapping never becomes current merely because correspondence exists.

**Current behavior:**

- Eligibility reuses PR1 `prove_contributing_line_correspondence` / `discover_transferable_historical_mappings`. Fail-closed `(page_index, provider_line_id)` identity proof. No fuzzy matching, canonical-text comparison, geometry, AI, or inferred paragraph boundaries.
- GET discovery runs only on the paragraph editor, and only when the current bound snapshot is structurally fresh and has **no** mapping. Versions and document detail keep the cheap existence note and do **not** run correspondence discovery or show adopt actions.
- Multiple eligible historical mappings are all shown, newest-first (discovery order). None is auto-selected. Staff must choose explicitly. Labels use snapshot `created_at` (`d.m.Y H:i`, seconds if needed to disambiguate) plus paragraph count (`פסקה אחת` / `N פסקאות`). Zero-break historical mappings are labeled as one saved paragraph. Raw snapshot/mapping/line/binding IDs, hashes, and provider IDs are hidden form tokens only.
- Dedicated POST: `ui/admin/documents/<id>/transkribus-paragraphs/adopt/` (`transkribus-paragraphs-adopt`). Not multiplexed into the ordinary editor save (a PR3 POST with zero `break_after` remains an explicit one-paragraph manual save).
- POST revalidates authorization, expected document/displayed result/target snapshot, structural freshness, selected source mapping identity (same document, still historical, still the chosen mapping/snapshot), absence of a target mapping, and correspondence. Then remaps source break PKs onto target contributing lines.
- `adopt_historical_paragraph_mapping` is create-only: `transaction.atomic()` + `select_for_update` on the target snapshot/mapping, then `save_paragraph_mapping(..., copied_from=source, create_only=True)`. OneToOne uniqueness / `IntegrityError` is treated as refuse-overwrite. Source mapping is not mutated. Ordinary PR1 save remains create-or-replace; `create_only` defaults false.
- If a current target mapping already exists, including a GET→POST race: refuse, leave it unchanged, no overwrite confirmation, no silent conversion to a manual resave. Manual edit remains the PR3 editor.
- Successful adopt: PRG to the editor, Hebrew success message, status reflects the new current mapping, editor remains available. `copied_from` records the adopted source. A later ordinary resave clears `copied_from` (existing PR1 semantics).
- Authorization matches PR3: `login_required` + `_require_admin_page` + `get_viewable_document` + CSRF. Restricted POST 404s before the adoption service.

**Must not change:** `DocumentTextResult.text`, snapshot canonical text, snapshot lines, geometry, char offsets, hover IDs, search offsets, bindings, public currentness/rendering, ordinary manual-save behavior.

**Explicit non-goals:** automatic inheritance/adoption; fuzzy matching; AI paragraph inference; Gemini/general-provider paragraph mappings; transcription editing; dehyphenation; canonical-text rewriting; overwrite-via-adoption / overwrite confirmation.
