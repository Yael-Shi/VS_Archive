# Document Date Precision Design (Future)

This note defines how VS-Archive should represent, display, and eventually filter **partial or imprecise** document dates. It is **documentation only** — no model, migration, UI, or API changes in this PR.

## 1. Problem statement

Real archive documents often do not have a single exact calendar day. Catalogers may know only:

- a full exact date
- month + year
- year only
- a date range
- an approximate date (future)
- no date at all

**Current model (`Document`):**

- `date_start` — `DateField`, nullable, blank allowed
- `date_end` — `DateField`, nullable, blank allowed

Both are optional metadata (“often unknown at upload time” per model comments).

**Current UI/API behavior (verified):**

- Upload/admin create accepts optional `date_start` / `date_end` as `YYYY-MM-DD` (`_parse_date_optional` in `documents/views.py`).
- Upload template labels: **תאריך התחלה** / **תאריך סיום** with HTML `type="date"` (`upload.html`).
- List and detail templates render `{{ date_start }} - {{ date_end }}` when either is set (`list.html`, `detail.html`).

**Product/modeling gap:**

Treating only `date_start` / `date_end` without a **precision** field implies false exactness. Examples:

- Year **1948** must not display as `1948-01-01` – `1948-12-31` or as if the archivist chose January 1 and December 31.
- **May 1948** must not display as a specific day unless that day is actually known.
- **Unknown date** must remain valid and distinguishable from “missing by accident.”
- **Ranges** (e.g. 1947–1949) must remain supported without forcing exact start/end days in the UI.

This is not a quick UI label fix; semantics must be defined before model and filtering changes.

## 2. Core principle

**Separate two concerns:**

| Concern | Role |
|--------|------|
| **Normalized date range** (`date_start`, `date_end`) | Machine use: filtering, sorting, search overlap, timeline queries. Values are **inclusive bounds** for the known period, expanded to day granularity only for indexing — not as a claim of exact knowledge. |
| **Date precision / display** (`date_precision` or `date_kind`, plus display label) | Human use: what the cataloger actually knew and what the UI shows. Must not be derived solely from normalized bounds without precision metadata. |

**Rule:** Never show normalized bounds as if they were the archivist’s exact input. Display must follow precision-aware rules (section 5).

## 3. Recommended conceptual fields (later implementation)

**Do not implement in this PR.** Intended direction for a follow-up schema PR:

| Field | Purpose |
|-------|---------|
| `date_start` | Normalized **lower** bound (`DateField`, nullable). |
| `date_end` | Normalized **upper** bound (`DateField`, nullable). |
| `date_precision` (or `date_kind`) | Enum: what level of calendar knowledge was recorded (V1 set in section 4). |
| `date_display` | Optional stored display string; may be computed from precision + bounds if stable. |
| `date_is_estimated` | Optional later: marks approximate/uncertain dating. |
| `date_note` | Optional later: free-text cataloger note (source, “circa”, citation). |

**Naming note:** Prefer one enum name in implementation (`date_precision` vs `date_kind`) — pick at migration time; this doc uses `date_precision` as the primary label.

Keep `date_start` / `date_end`; do not replace them with free-text-only dates (section 9).

## 4. Supported V1 date precision types

Small practical set for first implementation:

| Value | Meaning |
|-------|---------|
| `EXACT_DAY` | Full calendar date known (single day or same start/end day). |
| `MONTH` | Month + year known; day not known. |
| `YEAR` | Year only known; month/day not known. |
| `RANGE` | Interval between two partial or exact dates (may span years). |
| `UNKNOWN` | No date information; explicit “no date”. |

**Optional / future types (document only — not V1):**

- `APPROXIMATE` — circa / estimated (may pair with `date_is_estimated`)
- `DECADE` — e.g. 1940s
- `BEFORE` / `AFTER` — open-ended bounds
- `HEBREW_DATE` — Hebrew calendar display with normalized Gregorian bounds for search
- `SEASON` — e.g. spring 1948
- `DATE_SOURCE` / confidence metadata — provenance of the date (separate from precision if needed)

## 5. Semantics examples (normalized + display)

Normalization rules apply when the cataloger saves metadata. **Display** uses precision, not raw ISO range formatting for imprecise types.

### Exact day

| | |
|--|--|
| **Input/display** | 12 May 1948 |
| **Normalized** | `start=1948-05-12`, `end=1948-05-12` |
| **Precision** | `EXACT_DAY` |
| **Display** | 12 May 1948 (locale-appropriate formatting) |

### Month

| | |
|--|--|
| **Input/display** | May 1948 |
| **Normalized** | `start=1948-05-01`, `end=1948-05-31` |
| **Precision** | `MONTH` |
| **Display** | May 1948 — **not** 01/05/1948–31/05/1948 |

### Year

| | |
|--|--|
| **Input/display** | 1948 |
| **Normalized** | `start=1948-01-01`, `end=1948-12-31` |
| **Precision** | `YEAR` |
| **Display** | 1948 — **not** 01/01/1948–31/12/1948 |

### Range

| | |
|--|--|
| **Input/display** | 1947–1949 |
| **Normalized** | `start=1947-01-01`, `end=1949-12-31` (when ends are year-only; tighten if sub-precision known on each side) |
| **Precision** | `RANGE` |
| **Display** | 1947–1949 (preserve cataloger phrasing where possible) |

**Range with mixed precision (future detail):** e.g. May 1947 – 1949 may normalize to `1947-05-01` … `1949-12-31` with display `May 1947–1949` and precision `RANGE`; exact rules for partial endpoints belong in the UI PR.

### Unknown

| | |
|--|--|
| **Input/display** | unknown / no date |
| **Normalized** | `start=null`, `end=null` |
| **Precision** | `UNKNOWN` |
| **Display** | “No date” / Hebrew equivalent in UI |

**Invariant:** For `UNKNOWN`, both bounds stay null. Do not use sentinel dates.

## 6. UI implications (future)

**Upload / admin edit**

- Replace “date range” framing (**תאריך התחלה** / **תאריך סיום** as two independent day pickers) with **document date** workflow: choose **date type/precision** first, then collect only the fields required for that type.
- Do not force users to invent a day or month for year-only or month-only knowledge.
- Allow explicit **no date** (`UNKNOWN`) without treating it as incomplete metadata by default.

**List / detail / review**

- Show precision-aware labels (section 5), not raw `date_start - date_end` for imprecise types.
- Keep Hebrew UI strings consistent with existing admin templates.

**Metadata completion backlog (`metadata_status=NEEDS_COMPLETION`)**

- Today: `Document.MetadataStatus` — `NEEDS_COMPLETION` vs `COMPLETED` (`documents/models.py`).
- Future: backlog rules should treat **date precision** intentionally — a document with `UNKNOWN` date may still be catalog-complete; do not assume every document needs filled `date_start`/`date_end` day fields.

**Out of scope for date PRs:** OCR/HTR review UI, processing-state rollup, Transkribus, upload S3/worker paths — unless explicitly scoped.

## 7. Filtering and search implications (future)

- **Filter/sort/search** may use normalized `date_start` / `date_end` (overlap, before/after, year bucket).
- **Result lists and detail headers** must use precision-aware display, not expanded bounds.
- **Unknown:** filterable as “no date” (`precision=UNKNOWN` and null bounds).
- **Year/month queries:** e.g. “documents in 1948” should match `YEAR`/`MONTH`/`EXACT_DAY`/`RANGE` rows whose normalized interval overlaps 1948 — not only rows where the user happened to type exact days.

**Stop line until semantics ship:** Do not change filtering behavior in production until precision is stored and backfill rules are agreed (section 8).

## 8. Migration and backfill implications (future)

Existing rows have only `date_start` / `date_end`. **Do not blindly reinterpret** them after adding `date_precision`.

**Proposed default inference (for migration script design only — requires explicit approval):**

| Existing data pattern | Likely default precision | Caveat |
|----------------------|--------------------------|--------|
| both null | `UNKNOWN` | Safe default. |
| `date_start == date_end` (both set) | `EXACT_DAY` | May be wrong if user entered same artificial day for a year-only intent. |
| both set and differ | `RANGE` | May be wrong if range was entered as two exact days but meant year span. |

**Process requirements:**

- Review sample of production/staging data before automated backfill.
- Prefer manual correction for ambiguous rows over silent wrong precision.
- **Do not** backfill manually in ad hoc SQL without a written data decision.
- Document any one-off fixes in ops notes or a follow-up decision-log entry.

Historical note: early migration `0001_initial` required dates; `0002` made them nullable — legacy rows may exist with placeholder dates; treat backfill as **data archaeology**, not pure logic.

## 9. Stop lines

Explicit boundaries for all work until follow-up PRs are approved:

- **Do not** remove `date_start` / `date_end` — they remain the normalized range for search.
- **Do not** replace normalized dates with **free-text-only** storage (loses filtering); free text belongs in optional `date_note` / display helpers only.
- **Do not** infer `EXACT_DAY` for year-only or month-only input at save time without user-selected precision.
- **Do not** change filtering/sorting semantics until precision is persisted and display rules exist.
- **Do not** mix this work with OCR/HTR, upload completion, worker, routing, `DocumentTextResult`, `processing_state_user`, or infrastructure changes in the same PR unless explicitly requested.

## 10. Proposed implementation sequence

| PR | Scope |
|----|--------|
| **PR 1** | This design doc + decision-log pointer only. |
| **PR 2** | Model migration: `date_precision` (+ optional `date_display` / note fields if approved). Data migration plan documented; backfill executed only per section 8 decision. |
| **PR 3** | Admin/upload/edit UI: precision selector, conditional inputs, validation. |
| **PR 4** | List/detail (and public if applicable) display using precision-aware labels. |
| **PR 5** | Filtering/search: overlap queries, “no date”, year/month natural filters. |
| **PR 6** | Optional: `APPROXIMATE`, `DECADE`, `BEFORE`/`AFTER`, `HEBREW_DATE`, `SEASON`, source/confidence metadata. |

## Related docs

- `docs/ai-context/decision-log.md` — durable pointer entry for this design
- `app/backend/documents/models.py` — current `date_start` / `date_end` fields
- `docs/ai-context/vs-archive-context.md` — high-level project context (no date precision yet)

## Open questions (for PR 2 planning)

- Single-day upload today: if only `date_start` is set, is `date_end` implied equal or left null? Define validation when precision exists.
- Should `metadata_status=COMPLETED` require a date at all, or only require precision when date is catalog-relevant?
- Public vs admin display locale rules for Hebrew/English dates.
