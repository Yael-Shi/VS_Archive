# VS-Archive

Django/AWS digital archive for historical and family archive materials.

## Current status

- **Archive items** — OCR documents, manual text items, and PHOTO items (unified archive at `/archive/`)
- **OCR/HTR** — implemented for configured routes (Gemini default; Transkribus for Hebrew handwritten when enabled)
- **Hebrew translation** — non-Hebrew OCR output can be translated to Hebrew via Gemini
- **Staff workflows** — upload, processing, transcription review, metadata edit, and archive browse/manage
- **Quality baseline** — Ruff, mypy, Pyright, Django `check`, and migration checks are currently clean

Not every planned capability is complete. See the documentation map for scope, deferred work, and operational detail.

## Documentation

**[`docs/README.md`](docs/README.md)** — current documentation map (OCR/HTR routing, processing semantics, deploy/ops, quality baseline).

## Key URLs

- Archive browse: `/archive/`
- Archive manage: `/archive/manage/`
- Staff create (unified): `/archive/manage/new/`
- Upload UI (OCR fallback): `/api/ui/upload/`
- Documents list: `/api/ui/documents/`
- Django admin: `/admin/`

## Historical — original V1 slice (closed)

The first vertical slice delivered browser upload, inline viewing, document list, admin backlog, and server-rendered UI behind authentication. The bullets below describe that original scope, not the full current system:

- Minimal UI (server-rendered) behind login
- Upload via browser: create → presigned PUT → complete
- Required fields on upload: title, doc_type
- Inline viewing via presigned GET URLs
- Admin backlog for documents needing metadata completion
- Manual QA checklist: `docs/V1_CHECKLIST.md`
