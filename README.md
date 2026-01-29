# VS-Archive

## Project status
**Current version:** V1 (closed)

V1 provides a minimal, end-to-end vertical slice:
- Browser upload (Desktop + Mobile)
- Inline document viewing (PDF / Image)
- Document list with basic filtering
- Admin backlog for metadata completion
- Server-rendered UI behind authentication

Advanced processing (OCR / translation / entity extraction) is planned for V2+.

## V1 Scope
- Minimal UI (server-rendered) behind login
- Upload via browser using: create → presigned PUT → complete
- Required fields on upload: title, doc_type
- Inline viewing via presigned GET URLs
- Admin backlog for documents needing metadata completion
- Admins can always edit metadata (even after completion)
- Correction requests managed via Django admin

## Key URLs
- Upload UI: /api/ui/upload/
- Documents list: /api/ui/documents/
- Document detail: /api/ui/documents/<id>/
- Admin backlog: /api/ui/admin/backlog/
- Django admin: /admin/

## Known limitations
- OCR / HTR / translation are not implemented in V1 (planned for V2)
- Mobile camera capture is best-effort (device/browser dependent)

## Documentation
- V1 manual checklist: docs/V1_CHECKLIST.md
