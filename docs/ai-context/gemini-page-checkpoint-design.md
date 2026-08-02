# Gemini OCR page checkpoint/resume

## Status

Approved and implemented by PR B in the Gemini OCR root-cause sequence:

1. PR A — safe response diagnostics and failure taxonomy.
2. **PR B — durable page checkpoint/resume (this document).**
3. PR C — Hebrew printed plain-text response contract (implemented on an
   uncommitted PR branch; focused/static and full regression validated, not
   yet merged; Hebrew printed uses the route-specific
   `gemini-hebrew-printed-prompt-v2` marker, so its JSON-era attempt
   identities are not reused, while all other routes retain
   `gemini-ocr-prompt-v1` and their existing identities — see the
   decision-log entry “Gemini Hebrew printed plain-text OCR response
   contract (PR C)”).
4. PR D — bounded per-page retry, backoff, and output-token escalation.
5. PR E — explicit mixed printed/handwritten routing and prompt contract.

This design changes persistence and resume behavior only. It does not authorize a
production retry or deployment by itself.

## Problem

Gemini previously accumulated every page transcript in an in-memory list.
`DocumentTextResult` was written only after every page succeeded. A failure on a
later page therefore discarded all earlier successful provider work, and a later
intentional OCR retry sent the whole document to Gemini again.

Memory and SQS payloads cannot be the checkpoint source of truth: both disappear
or may be delivered more than once. A `Document` id alone is also insufficient
because the source file, extraction output, route, prompt, model configuration,
or generation configuration may change between executions.

## Goals

- Persist every successful Gemini OCR page immediately.
- Reuse only a checkpoint whose complete source and execution identity matches.
- Send only missing or failed pages during a later intentional OCR execution.
- Fence duplicate, concurrent, expired, and late page workers.
- Assemble document text deterministically by 1-based page index.
- Report an exact durable list of non-succeeded pages when OCR is partial.
- Keep `DocumentTextResult` document-level and idempotent.
- Preserve PR A privacy guarantees.

## Non-goals

- Reclaiming or automatically replaying
  `ProcessDocumentRequest.RECOVERY_REQUIRED`.
- New attempt counts, backoff, output-token caps, continuation, or page splitting
  (PR D).
- Hebrew printed plain-text output (PR C).
- MIXED printed/handwritten routing (PR E).
- Translation checkpoints or any translation generation/retry change.
- Transkribus or Antigravity changes.
- UI display of missing page indices.
- Production deployment or document retry.

## Persistence model

### `GeminiOcrAttempt`

An attempt is a reusable OCR identity independent of
`ProcessDocumentRequest`. Its unique key is:

`(document, identity_fingerprint)`

It stores only fingerprints and safe configuration identity, not source bytes or
prompt text:

- source, route, prompt, and configuration fingerprints;
- prompt contract version;
- ordered model candidates;
- expected page count;
- lifecycle: `IN_PROGRESS`, `PARTIAL`, or `COMPLETED`;
- ordered `missing_page_indices`;
- completion timestamp.

The attempt belongs to the `Document`, so deletion and access control follow the
same document boundary as `DocumentTextResult`.

### `GeminiOcrPageCheckpoint`

The database enforces one row per `(attempt, page_index)`. A checkpoint contains:

- 1-based page index;
- page and source-content fingerprints;
- `RUNNING`, `SUCCEEDED`, or `FAILED`;
- lease token and expiry while `RUNNING`;
- actual Gemini model for a successful page;
- page text and review metadata for success;
- stable safe failure code/message for failure;
- start/completion timestamps.

Successful page text is intentionally stored in the database. It is document
content and must receive the same authorization treatment as
`DocumentTextResult`. It must never be copied into logs or operational failure
messages.

## Canonical identity

All fingerprints use SHA-256 over UTF-8 JSON serialized with:

- sorted object keys;
- compact separators;
- stable arrays for ordered data;
- no prompt or document content written to diagnostic fields.

### Source fingerprint

For every page, the canonical source payload includes:

- `page_index`;
- normalized page MIME type;
- source identity;
- SHA-256 of the actual downloaded source bytes;
- SHA-256 of the normalized/rendered page image bytes.

The attempt source fingerprint hashes the ordered page payloads. Consequently,
replacement of bytes, source-file reordering, a different source row/key, or a
change to rendered/normalized page bytes cannot reuse the old attempt.

For a PDF, pages share the original file identity/content hash while retaining
their distinct normalized-page hashes. For a multi-image document, each page
uses the matching ordered `DocumentSourceFile` identity and content hash.

### Route fingerprint

The route fingerprint includes:

- language hint;
- text input type;
- handwriting type;
- selected engine key;
- prompt variant.

Routing is still selected once by the worker. This feature does not add or
change routes.

### Prompt fingerprint and version

`prompt_fingerprint` is SHA-256 of the exact effective OCR prompt, including the
language-hint line. `prompt_contract_version` is an explicit semantic version
marker. Exact hashing prevents accidental stale reuse after textual changes;
the explicit version makes intentional contract changes auditable even when
their consequences are not obvious from a diff.

PR C or any later prompt/output-contract change must change the effective hash
and should bump the contract version when semantics change.

### Configuration fingerprint

The configuration fingerprint includes every currently relevant
output-affecting input:

- ordered model candidates;
- provider API/output mode;
- configured and effective temperature;
- top-k and top-p;
- maximum output tokens;
- minimum text length;
- double-pass and consistency settings.

The overall attempt identity hashes the source, route, prompt and configuration
fingerprints, prompt version, ordered candidates, and expected page count.

## Claim and lease fencing

Page claims use a short database transaction and `select_for_update`.

- Missing or `FAILED` page → issue a new token and set `RUNNING`.
- Unexpired `RUNNING` page → `BUSY`; do not call Gemini again.
- Expired `RUNNING` page → issue a new token and allow recovery.
- Matching `SUCCEEDED` page → `REUSE`; do not call Gemini.

Provider calls occur outside the database transaction. Success/failure
persistence is accepted only when the row is still `RUNNING` with the same
token. A late holder cannot overwrite a newer claim.

The page lease aligns with the existing 45-minute processing lease. It is a
fence for duplicate provider execution, not a new automatic request-recovery
policy.

## Execution and failure behavior

Gemini remains provider-specific inside `GeminiAdapter`/`gemini_engine`.
`run_worker.py` receives only provider-neutral page-incomplete or page-busy
errors.

The existing per-page provider behavior is retained:

- the current response-format/quota attempts inside `gemini_engine` are
  unchanged;
- Gemini model fallback remains quota-only;
- processing stops at the first page that remains unsuccessful.

Stopping at the first final failure preserves existing behavior and keeps PR B
separate from PR D. Earlier successes remain durable. Pages after the failure
are explicitly missing and are processed together with the failed page during a
later intentional request.

A failed page stores the stable PR A failure code when available. Unexpected
provider exceptions store only their exception class. Provider exception text,
finish-message text, OCR output, and prompts are forbidden from checkpoint
failure messages.

Provider execution and local checkpoint persistence are separate exception
domains. Database failures while creating an attempt, claiming a page, storing
success/failure, assembling, or reading missing indices raise the provider-
neutral `EnginePageCheckpointPersistenceRetryableError`. The worker returns
`OCR_PAGE_CHECKPOINT_PERSISTENCE_RETRYABLE`, does not acknowledge the SQS
message, and does not terminalize the request or document as `PARTIAL`. Only
safe stage/page metadata is exposed; the underlying database exception text is
not logged or persisted.

## Deterministic assembly

Assembly locks the attempt and page rows and requires exactly one successful
checkpoint for every index `1..expected_page_count`.

- Any missing/non-succeeded index → attempt `PARTIAL`, exact ordered
  `missing_page_indices`, and no document-level OCR success result.
- Complete sequence → join stripped page texts with the existing `"\n\n"`
  separator and mark the attempt `COMPLETED`.

The worker then uses its existing document lock and
`DocumentTextResult.update_or_create` contract. Existing database uniqueness
prevents duplicate document rows for the same document/result type/runtime
engine. A crash after checkpoint assembly but before document-result
persistence is recoverable: the next intentional execution reuses every page,
assembles again idempotently, and performs local persistence without another
Gemini call.

## Mixed runtime models

The attempt identity records the configured ordered model candidates. Each page
records the actual model that produced its text.

- All pages use one model → `DocumentTextResult.engine` is that concrete model.
- Multiple models → runtime identity is
  `gemini-mixed:<48 hexadecimal SHA-256 characters>`.

The hash is computed from canonical ordered `(page_index, model)` data. The
48-character prefix provides 192 bits while keeping the full marker within the
existing 64-character database field. Complete page-to-model provenance remains
available on the page checkpoints.

This is not a new provider fallback. It makes the already-approved Gemini
candidate fallback truthful at page granularity.

## `PARTIAL` semantics

A final page failure does not create a misleading whole-document failed
`DocumentTextResult`. Instead:

- the attempt is `PARTIAL`;
- `missing_page_indices` stores every failed or unattempted page;
- `Document.processing_state_user` becomes `PARTIAL`;
- request-aware processing terminalizes as `PARTIAL`;
- failure code is `GEMINI_PAGES_INCOMPLETE`;
- failure message is bounded safe metadata such as `missing_pages=2,3`.

The current UI does not display this list. A later UI PR may read it from the
attempt; PR B deliberately does not expand view/template scope.

## Relationship to `ProcessDocumentRequest`

The attempt is deliberately not owned by one request. A terminal request cannot
be the reusable OCR identity because a later intentional OCR-reprocess request
must reuse already-successful pages.

This PR does not change request fencing:

- a fresh duplicate delivery is deferred by the request lease;
- an expired request becomes `RECOVERY_REQUIRED`;
- `RECOVERY_REQUIRED` is not automatically reclaimed or replayed;
- a late retained request holder may still terminalize under its existing token.

Operational resolution of `RECOVERY_REQUIRED` remains separate work. After that
request-level state permits a new intentional OCR request, the new request can
reuse the matching page attempt.

## Alternatives considered

### Tie checkpoints only to `ProcessDocumentRequest` — rejected

A later intentional request would have a different id and could not reuse the
successful pages. Copying rows between requests would create another stale-data
and idempotency problem. Source/config identity is the correct reuse boundary.

### Automatically replay `RECOVERY_REQUIRED` — rejected

The current request design intentionally fences expired work because there is
no generic safe provider-attempt reconciliation contract. Changing that policy
inside PR B would contradict the existing request decision and combine two
independent recovery projects.

### Page-level `DocumentTextResult` rows — rejected

`DocumentTextResult` is the display/verification result for a whole document
and has uniqueness, editing, revision, search, and translation semantics at that
level. Reusing it for internal page work would leak incomplete results into
existing read paths and force an unrelated UI/search/revision redesign.

### Store checkpoints only in memory or SQS — rejected

Memory is lost on crash. SQS is at-least-once delivery, not durable relational
state, and payload-carried text would enlarge the privacy and stale-reuse
surface.

### Reprocess all successful pages after Gemini model fallback — rejected

It preserves a single model name but defeats the primary cost/reliability goal:
a quota failure on one page would resend every completed page. Per-page actual
model provenance is more truthful and avoids duplicate provider work.

### Exclude Arabic multi-model routes — rejected

Arabic is an existing Gemini route with an approved candidate chain. A
Gemini-wide persistence contract that silently omitted it would be incomplete
and would retain the original data-loss behavior for that route.

### Add missing-page UI in PR B — rejected

Durable state is required now; UI is not needed for checkpoint correctness.
Including views/templates would widen the migration/concurrency PR and require a
separate read-path/access-control audit. The schema intentionally supports that
later UI.

### Log raw provider or document content — rejected

Historical documents can contain sensitive personal information. PR A already
established that logs and persisted failure metadata contain operational
metadata only. Page persistence does not weaken that boundary.

## Deployment and migration gate

PR B adds a database migration. Deployment must use the existing
migration-aware worker-first runbook:

1. build explicit web and worker image tags;
2. keep worker desired count at zero;
3. run the migration task and require exit code zero;
4. start the worker only after the schema is live;
5. complete web cutover and health checks.

Do not retry production reference documents until the relevant PR chain is
merged, deployed, and verified.

## Required validation

- crash before/after page persistence;
- later request sends only failed/missing pages;
- duplicate and concurrent claims;
- expired claim reclaim and stale-token rejection;
- source replacement/reordering;
- prompt/config/model-candidate identity changes;
- ordered assembly;
- uniform and mixed model identities;
- exact missing-page reporting;
- no whole-document failed row for a page-incomplete attempt;
- no sensitive content in diagnostics;
- no translation, Transkribus, Antigravity, routing, or request-fencing
  regression.

## Validation audit and inherited test repairs

The first broad PR B run executed 2,255 tests and reported ten failures. The
failures were audited individually instead of being treated as one production
defect.

Nine failures came from unchanged worker tests that mocked extracted pages with
`object()` or `SimpleNamespace(page_index=1)`. PR B legitimately needs the
complete immutable `PageImage` dataclass so the worker can attach source
identity and content fingerprints with `dataclasses.replace`. Those fixtures
now construct real `PageImage` values. Weakening the production contract to
accept partial structural stubs was rejected because it would hide invalid
callers and undermine the checkpoint identity boundary. After the fixture
repair, the three affected regression modules passed all 75 tests.

The remaining failure was
`ArchiveItemDiscoveryBrowseTests.test_existing_archive_search_still_works_after_browse_pages`.
It failed identically when run alone from a clean worktree at the exact PR B
base, `cb10864de0f9180051e4463bd717047bdc17f4a2`, proving that PR B did not
introduce it. The test directly called `item.categories.add(...)` after the
search index had been created. That bypassed the documented explicit
`update_archive_item_discovery_metadata` write hook, so the denormalized index
remained stale. The fixture now uses that service. Adding a model signal or
changing search production code was rejected because the search architecture
explicitly requires same-transaction service hooks and explicitly has no
signals. The isolated test and all 324 tests in `test_archive_item` then passed.

The next full-suite rerun exposed an inherited query-plan test that sometimes
preferred the selective one-to-one B-tree index and applied `search_vector @@`
as a filter instead of naming the GIN index. The real query still retained the
separate FTS `UNION` arm, and the unchanged test passed on the clean base and in
five isolated current-worktree repetitions. The test already disabled
sequential scans to demonstrate indexability; it now also disables regular
index scans while leaving bitmap scans enabled, so the plan check
deterministically demonstrates the intended bitmap/GIN path. Removing the GIN
assertion would weaken the original regression guard; changing the production
query would respond to planner choice rather than a functional or structural
regression; inflating fixtures and running `ANALYZE` would be slower and would
mutate shared planner statistics. The stabilized test passed five consecutive
runs and all 45 tests in `test_archive_full_text_search`.

Final staged review found that attempt identity stripped surrounding whitespace
from model candidates while the adapter executed the unstripped values. That
could give different provider input the same canonical identity. The adapter
now normalizes candidates once before both identity construction and execution,
and rejects any candidate that becomes empty. Keeping normalization only in the
identity was rejected because it would leave identity and execution unequal;
hashing unnormalized values was rejected because surrounding whitespace is not
part of a valid model identifier and would weaken canonicalization.

Scoped PR B Pyright and mypy validation is clean. Full-tree typing also exposed
41 Pyright errors and 12 mypy errors in unchanged Transkribus test files. They
are inherited baseline debt, not PR B regressions, and this PR must not claim
that full-tree typing is clean. Before the final staged-review correction, the
full Django suite passed all 2,255 tests with exit code 0. After the model-
candidate canonicalization correction, 29 focused checkpoint/adapter tests and
all final scoped/static checks passed.

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
