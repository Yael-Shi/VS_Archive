from __future__ import annotations

from documents.models import Document, DocumentSourceFile


def sync_primary_document_source_file(document: Document) -> DocumentSourceFile:
    """
    Upsert the primary (order_index=0) source file row from Document file metadata.

    Raises ``ValueError`` when ``document.file_s3_key`` is missing. ``upload_complete``
    rejects that case before calling this helper on the success path.
    """
    if not document.file_s3_key:
        raise ValueError("document.file_s3_key is required to sync DocumentSourceFile")

    source, _created = DocumentSourceFile.objects.update_or_create(
        document=document,
        order_index=0,
        defaults={
            "file_s3_key": document.file_s3_key,
            "file_original_name": document.file_original_name,
            "mime_type": document.mime_type,
            "size_bytes": document.size_bytes,
        },
    )
    return source
