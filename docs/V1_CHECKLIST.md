# V1 Manual Checklist

## Upload (Desktop)
- [ ] Open /api/ui/upload/
- [ ] Upload PDF with only: file + title + doc_type=PDF
- [ ] Verify redirect to /api/ui/documents/<id>/ and PDF displays inline
- [ ] Upload IMAGE with only: file + title + doc_type=IMAGE
- [ ] Verify redirect and image displays inline

## Upload (Mobile)
- [ ] Open /api/ui/upload/ from mobile
- [ ] Select doc_type=IMAGE -> verify camera/gallery option appears (best-effort)
- [ ] Upload an image -> verify inline display

## Documents list
- [ ] Open /api/ui/documents/
- [ ] Verify newly uploaded docs appear
- [ ] Search by title (q) returns expected results
- [ ] Filter by doc_type works
- [ ] Filter by metadata_status works

## Admin backlog
- [ ] As admin, open /api/ui/admin/backlog/
- [ ] Verify newly uploaded docs appear (NEEDS_COMPLETION)
- [ ] Click edit -> opens Django admin
- [ ] Change metadata_status=COMPLETED
- [ ] Refresh backlog -> document is gone
- [ ] Verify document is still editable in admin after COMPLETED

## Logs / Observability
- [ ] Trigger list + detail + backlog
- [ ] Verify logs appear in CloudWatch (or local console) for:
      documents_list_api / documents_list_page / document_detail_page / admin_backlog_page
