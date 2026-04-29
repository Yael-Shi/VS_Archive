# VS-Archive AI Context

VS-Archive is a Django backend project for managing historical family documents.

Main domain:
- Documents may be uploaded as IMAGE or PDF.
- Documents have metadata.
- OCR/HTR extracts source text.
- Translation may create Hebrew text.
- Text results are stored separately from the document.
- Admin review/verification matters.

Current implementation notes:
- Do not assume Transkribus is implemented.
- Gemini may currently be the only implemented OCR/HTR engine.
- Engine selection should be introduced carefully as a routing layer, not as a full OCR rewrite.

Desired next feature:
Select processing engine based on:
1. source language
2. whether the document is handwritten or printed

Important design preference:
- Add small isolated selector logic first.
- Do not change database schema unless proven necessary.
- Do not add real external integrations yet.