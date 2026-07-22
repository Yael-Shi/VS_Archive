from __future__ import annotations

import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models, transaction


class ArchiveItem(models.Model):
    """Central archival content entity; OCR-backed documents link via Document.archive_item."""

    class ItemType(models.TextChoices):
        OCR_DOCUMENT = "OCR_DOCUMENT", "OCR document"
        MANUAL_TEXT = "MANUAL_TEXT", "Manual text"
        PHOTO = "PHOTO", "Photo"

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private"
        PUBLIC = "public", "Public"

    class MetadataStatus(models.TextChoices):
        NEEDS_COMPLETION = "NEEDS_COMPLETION", "Needs completion"
        COMPLETED = "COMPLETED", "Completed"

    class DatePrecision(models.TextChoices):
        EXACT_DAY = "EXACT_DAY", "Exact day"
        MONTH = "MONTH", "Month"
        YEAR = "YEAR", "Year"
        RANGE = "RANGE", "Exact day range"
        RANGE_MONTH = "RANGE_MONTH", "Month range"
        RANGE_YEAR = "RANGE_YEAR", "Year range"
        UNKNOWN = "UNKNOWN", "Unknown"

    title = models.CharField(max_length=255)
    author_name = models.CharField(max_length=255, blank=True, default="")
    source_title = models.CharField(max_length=255, blank=True, default="")
    public_note = models.TextField(blank=True, default="")
    item_type = models.CharField(max_length=32, choices=ItemType.choices)
    visibility = models.CharField(
        max_length=16,
        choices=Visibility.choices,
        default=Visibility.PRIVATE,
    )
    date_start = models.DateField(null=True, blank=True)
    date_end = models.DateField(null=True, blank=True)
    date_precision = models.CharField(
        max_length=16,
        choices=DatePrecision.choices,
        default=DatePrecision.UNKNOWN,
    )
    metadata_status = models.CharField(
        max_length=32,
        choices=MetadataStatus.choices,
        default=MetadataStatus.NEEDS_COMPLETION,
    )
    categories: models.ManyToManyField[ArchiveCategory, ArchiveCategory] = (
        models.ManyToManyField(
            "ArchiveCategory",
            blank=True,
            related_name="archive_items",
        )
    )
    events: models.ManyToManyField[ArchiveEvent, ArchiveEvent] = models.ManyToManyField(
        "ArchiveEvent",
        blank=True,
        related_name="archive_items",
    )
    tags: models.ManyToManyField[Tag, Tag] = models.ManyToManyField(
        "Tag",
        blank=True,
        related_name="archive_items",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"{self.title} ({self.item_type})"


class ArchiveCategory(models.Model):
    """Cross-item archival topic for public discovery (linked via ArchiveItem M2M)."""

    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True)
    description = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ArchiveEvent(models.Model):
    """Family/historical occasion for public discovery (linked via ArchiveItem M2M)."""

    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(max_length=255, unique=True)
    description = models.TextField(blank=True, default="")
    date_start = models.DateField(null=True, blank=True)
    date_end = models.DateField(null=True, blank=True)
    date_precision = models.CharField(
        max_length=16,
        choices=ArchiveItem.DatePrecision.choices,
        default=ArchiveItem.DatePrecision.UNKNOWN,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class ManualTextContent(models.Model):
    """First-party typed text for MANUAL_TEXT archive items (not OCR output)."""

    archive_item = models.OneToOneField(
        ArchiveItem,
        on_delete=models.CASCADE,
        related_name="manual_text_content",
    )
    body = models.TextField()
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return f"ManualTextContent(archive_item_id={self.archive_item_id})"


class PhotoContent(models.Model):
    """Image file metadata for PHOTO archive items (not OCR/Document-backed)."""

    class UploadStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        UPLOADED = "UPLOADED", "Uploaded"
        FAILED = "FAILED", "Failed"

    archive_item = models.OneToOneField(
        ArchiveItem,
        on_delete=models.CASCADE,
        related_name="photo_content",
    )
    original_file_key = models.CharField(max_length=1024)
    original_filename = models.CharField(max_length=512)
    original_mime_type = models.CharField(max_length=128)
    original_size_bytes = models.PositiveBigIntegerField(default=0)
    upload_status = models.CharField(
        max_length=16,
        choices=UploadStatus.choices,
        default=UploadStatus.PENDING,
    )
    upload_error = models.CharField(max_length=512, blank=True, default="")
    description = models.TextField(blank=True, default="")
    location = models.TextField(blank=True, default="")
    context = models.TextField(blank=True, default="")
    people_present = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    thumbnail_file_key = models.CharField(max_length=1024, blank=True, default="")
    thumbnail_mime_type = models.CharField(max_length=128, blank=True, default="")
    thumbnail_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self) -> None:
        super().clean()
        if (
            self.archive_item_id
            and self.archive_item.item_type != ArchiveItem.ItemType.PHOTO
        ):
            raise ValidationError(
                {
                    "archive_item": (
                        "PhotoContent requires ArchiveItem with item_type=PHOTO."
                    )
                }
            )

    def __str__(self) -> str:
        return f"PhotoContent(archive_item_id={self.archive_item_id})"


class DocumentQuerySet(models.QuerySet):
    def delete(self) -> tuple[int, dict[str, int]]:
        with transaction.atomic():
            archive_item_ids = list(
                self.exclude(archive_item_id__isnull=True)
                .values_list("archive_item_id", flat=True)
                .distinct()
            )
            deleted_count, deleted_map = super().delete()
            if archive_item_ids:
                ArchiveItem.objects.filter(pk__in=archive_item_ids).delete()
            return deleted_count, deleted_map


class Document(models.Model):
    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private"
        PUBLIC = "public", "Public"

    class UploadStatus(models.TextChoices):
        UPLOADING = "UPLOADING", "Uploading"
        UPLOADED = "UPLOADED", "Uploaded"
        FAILED = "FAILED", "Failed"

    class DocType(models.TextChoices):
        PDF = "PDF", "PDF"
        IMAGE = "IMAGE", "Image"

    class MetadataStatus(models.TextChoices):
        NEEDS_COMPLETION = "NEEDS_COMPLETION", "Needs completion"
        COMPLETED = "COMPLETED", "Completed"

    class Language(models.TextChoices):
        HEBREW = "he", "Hebrew"
        ENGLISH = "en", "English"
        FRENCH = "fr", "French"
        ARABIC = "ar", "Arabic"

    class TextInputType(models.TextChoices):
        HANDWRITTEN = "HANDWRITTEN", "Handwritten"
        PRINTED = "PRINTED", "Printed"

    class HandwritingType(models.TextChoices):
        VS = "VS", "VS handwriting"
        GENERAL = "GENERAL", "General Hebrew handwriting"

    class DatePrecision(models.TextChoices):
        EXACT_DAY = "EXACT_DAY", "Exact day"
        MONTH = "MONTH", "Month"
        YEAR = "YEAR", "Year"
        RANGE = "RANGE", "Range"
        UNKNOWN = "UNKNOWN", "Unknown"

    # Required for V1
    doc_type = models.CharField(max_length=16, choices=DocType.choices)

    language = models.CharField(
        max_length=8,
        choices=Language.choices,
        null=True,
        blank=True,
    )
    text_input_type = models.CharField(
        max_length=16,
        choices=TextInputType.choices,
    )
    handwriting_type = models.CharField(
        max_length=16,
        choices=HandwritingType.choices,
        default=HandwritingType.VS,
    )

    category_event = models.CharField(max_length=255, null=True, blank=True)

    tags_m2m: models.ManyToManyField[Tag, Tag] = models.ManyToManyField(
        "Tag", blank=True, related_name="documents"
    )

    upload_status = models.CharField(
        max_length=16,
        choices=UploadStatus.choices,
        default=UploadStatus.UPLOADING,
    )

    class ProcessingState(models.TextChoices):
        PROCESSING = "PROCESSING", "Processing"
        READY = "READY", "Ready"
        PARTIAL = "PARTIAL", "Partial"
        FAILED = "FAILED", "Failed"

    processing_state_user = models.CharField(
        max_length=32,
        choices=ProcessingState.choices,
        default=ProcessingState.PROCESSING,
    )

    # S3 file data
    file_s3_key = models.CharField(max_length=1024, blank=True, default="")
    file_original_name = models.CharField(max_length=512, blank=True, default="")
    mime_type = models.CharField(max_length=128, blank=True, default="")
    size_bytes = models.BigIntegerField(null=True, blank=True)
    upload_error = models.TextField(null=True, blank=True)

    thumbnail_file_key = models.CharField(max_length=1024, blank=True, default="")
    thumbnail_mime_type = models.CharField(max_length=128, blank=True, default="")
    thumbnail_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    first_page_width = models.PositiveIntegerField(null=True, blank=True)
    first_page_height = models.PositiveIntegerField(null=True, blank=True)

    expected_source_file_count = models.IntegerField(
        null=True,
        blank=True,
        help_text=(
            "Planned multi-image source file count when set at create; "
            "null for legacy single-file documents."
        ),
    )

    archive_item = models.OneToOneField(
        ArchiveItem,
        on_delete=models.CASCADE,
        related_name="ocr_document",
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = DocumentQuerySet.as_manager()

    def __str__(self) -> str:
        if self.archive_item_id:
            title = (self.archive_item.title or "").strip()
            if title:
                return title
        if not self._state.adding:
            return f"Document {self.pk}"
        return "Document"

    def delete(
        self,
        using: str | None = None,
        keep_parents: bool = False,
    ) -> tuple[int, dict[str, int]]:
        with transaction.atomic():
            archive_item_id = self.archive_item_id
            deleted = super().delete(using=using, keep_parents=keep_parents)
            if archive_item_id:
                ArchiveItem.objects.filter(pk=archive_item_id).delete()
            return deleted


class DocumentTextResult(models.Model):
    class OcrEngineKey(models.TextChoices):
        GEMINI = "GEMINI", "Gemini"
        TRANSKRIBUS = "TRANSKRIBUS", "Transkribus"
        ANTIGRAVITY = "ANTIGRAVITY", "Antigravity"

    class OcrPromptVariant(models.TextChoices):
        HANDWRITTEN = "handwritten", "Handwritten"
        HEBREW_GENERAL_HANDWRITTEN = (
            "hebrew_general_handwritten",
            "General Hebrew handwriting",
        )
        PRINTED = "printed", "Printed"
        HEBREW_TRANSLATION = "hebrew_translation", "Hebrew translation"

    class ResultType(models.TextChoices):
        SOURCE_TEXT = "SOURCE_TEXT", "Source text"
        HEBREW_TEXT = "HEBREW_TEXT", "Hebrew text"

    class Status(models.TextChoices):
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"
        NEEDS_REVIEW = "NEEDS_REVIEW", "Needs review"

    class VerificationStatus(models.TextChoices):
        UNVERIFIED = "UNVERIFIED", "Unverified"
        VERIFIED = "VERIFIED", "Verified"
        REJECTED = "REJECTED", "Rejected"

    document = models.ForeignKey(
        Document, on_delete=models.CASCADE, related_name="text_results"
    )

    result_type = models.CharField(max_length=32, choices=ResultType.choices)
    engine = models.CharField(max_length=64, default="engine_v1")

    engine_key = models.CharField(max_length=32, choices=OcrEngineKey.choices)
    prompt_variant = models.CharField(max_length=32, choices=OcrPromptVariant.choices)

    status = models.CharField(max_length=32, choices=Status.choices)
    verification_status = models.CharField(
        max_length=32,
        choices=VerificationStatus.choices,
        default=VerificationStatus.UNVERIFIED,
    )

    text = models.TextField(null=True, blank=True)

    # Monotonic revision for SOURCE_TEXT; paired HEBREW_TEXT tracks via based_on_source_revision.
    source_revision = models.PositiveIntegerField(default=1)
    based_on_source_revision = models.PositiveIntegerField(null=True, blank=True)

    error_code = models.CharField(max_length=64, null=True, blank=True)
    error_details = models.TextField(null=True, blank=True)

    # Why this result was marked as NEEDS_REVIEW.
    # Stored as a JSON string (e.g. ["HAS_UNCLEAR","CONSISTENCY_MISMATCH"]) or plain text.
    review_reasons = models.TextField(blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["document", "result_type", "engine"]),
            models.Index(fields=["status", "verification_status"]),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "result_type", "engine"],
                name="uniq_document_resulttype_engine",
            )
        ]


class DocumentTextResultEdit(models.Model):
    class EditType(models.TextChoices):
        SOURCE_TEXT = "SOURCE_TEXT", "Source text"
        HEBREW_TEXT = "HEBREW_TEXT", "Hebrew text"

    text_result = models.ForeignKey(
        DocumentTextResult,
        on_delete=models.CASCADE,
        related_name="edits",
    )
    editor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="document_text_result_edits",
    )
    edited_at = models.DateTimeField(auto_now_add=True)
    old_text = models.TextField()
    new_text = models.TextField()
    edit_type = models.CharField(max_length=32, choices=EditType.choices)

    class Meta:
        ordering = ["-edited_at"]

    def __str__(self) -> str:
        return (
            f"DocumentTextResultEdit(id={self.id}, result_id={self.text_result_id}, "
            f"edit_type={self.edit_type})"
        )


class ProcessingMetric(models.Model):
    """
    Metrics/time measurements for processing pipeline steps.

    Design goals:
    - Multiple metrics per Document
    - Multiple attempts per step
    - Supports duration tracking (started_at/finished_at) + numeric values
    - Easy to query "last 24h" / "per stage" / "per engine"
    """

    class Stage(models.TextChoices):
        UPLOAD = "UPLOAD", "Upload"
        PAGE_EXTRACTION = "PAGE_EXTRACTION", "Page extraction"
        OCR_HTR = "OCR_HTR", "OCR/HTR"
        TRANSLATION = "TRANSLATION", "Translation"
        POSTPROCESS = "POSTPROCESS", "Postprocess"
        PIPELINE = "PIPELINE", "Pipeline total"

    class Status(models.TextChoices):
        STARTED = "STARTED", "Started"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    class Unit(models.TextChoices):
        MS = "ms", "Milliseconds"
        SECONDS = "s", "Seconds"
        BYTES = "bytes", "Bytes"
        PAGES = "pages", "Pages"
        CHARS = "chars", "Characters"
        TOKENS = "tokens", "Tokens"
        COUNT = "count", "Count"

    document = models.ForeignKey(
        "Document",
        on_delete=models.CASCADE,
        related_name="processing_metrics",
    )

    stage = models.CharField(max_length=32, choices=Stage.choices)
    name = models.CharField(
        max_length=64,
        help_text="Metric name within stage, e.g. duration, pages, chars, api_calls, etc.",
    )

    engine = models.CharField(max_length=64, blank=True, default="")

    attempt_id = models.UUIDField(default=uuid.uuid4, editable=False)

    unit = models.CharField(max_length=16, choices=Unit.choices, blank=True, default="")
    value_int = models.BigIntegerField(null=True, blank=True)
    value_float = models.FloatField(null=True, blank=True)

    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.STARTED,
    )

    error_code = models.CharField(max_length=64, null=True, blank=True)
    error_details = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=["document", "stage", "name"]),
            models.Index(fields=["stage", "name", "created_at"]),
            models.Index(fields=["status", "created_at"]),
            models.Index(fields=["engine", "created_at"]),
        ]

    def __str__(self) -> str:
        return (
            f"ProcessingMetric(doc={self.document_id}, stage={self.stage}, "
            f"name={self.name}, status={self.status})"
        )


class TranscriptionEditSuggestion(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="transcription_edit_suggestions",
    )
    current_text_snapshot = models.TextField()
    suggested_text = models.TextField()
    submitter_name = models.CharField(max_length=255)
    submitter_email = models.EmailField(blank=True, default="")
    submitter_note = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_transcription_edit_suggestions",
    )
    submitter_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_transcription_edit_suggestions",
    )
    approved_text = models.TextField(null=True, blank=True)
    applied_text_result = models.ForeignKey(
        DocumentTextResult,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="applied_transcription_edit_suggestions",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return (
            f"TranscriptionEditSuggestion(id={self.id}, document_id={self.document_id}, "
            f"status={self.status})"
        )


class ArchiveMetadataSuggestion(models.Model):
    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    archive_item = models.ForeignKey(
        ArchiveItem,
        on_delete=models.CASCADE,
        related_name="metadata_suggestions",
    )
    suggested_categories = models.TextField(blank=True, default="")
    suggested_events = models.TextField(blank=True, default="")
    suggested_tags = models.TextField(blank=True, default="")
    submitter_name = models.CharField(max_length=255)
    submitter_email = models.EmailField(blank=True, default="")
    submitter_note = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=16,
        choices=Status.choices,
        default=Status.PENDING,
    )
    created_at = models.DateTimeField(auto_now_add=True)
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_archive_metadata_suggestions",
    )
    submitter_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_archive_metadata_suggestions",
    )

    class Meta:
        ordering = ["-created_at"]

    def __str__(self) -> str:
        return (
            f"ArchiveMetadataSuggestion(id={self.id}, archive_item_id={self.archive_item_id}, "
            f"status={self.status})"
        )


class CorrectionRequest(models.Model):
    class Status(models.TextChoices):
        OPEN = "OPEN", "Open"
        IN_PROGRESS = "IN_PROGRESS", "In Progress"
        CLOSED = "CLOSED", "Closed"

    class Scope(models.TextChoices):
        DATA = "DATA", "Data"
        METADATA = "METADATA", "Metadata"

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="corrections",
    )

    status = models.CharField(
        max_length=16, choices=Status.choices, default=Status.OPEN
    )
    scope = models.CharField(max_length=16, choices=Scope.choices)

    field_path = models.CharField(max_length=512, null=True, blank=True)
    message = models.TextField()

    requester_name = models.CharField(max_length=255, null=True, blank=True)
    requester_email = models.EmailField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)


class Tag(models.Model):
    name = models.CharField(max_length=64, unique=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["name"]),
        ]

    def __str__(self) -> str:
        return self.name


class DocumentMetadata(models.Model):
    document = models.OneToOneField(
        Document, on_delete=models.CASCADE, related_name="admin_meta"
    )

    notes = models.TextField(blank=True, default="")
    donor = models.CharField(max_length=255, blank=True, default="")
    collection = models.CharField(max_length=255, blank=True, default="")
    original_location = models.CharField(max_length=255, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["donor"]),
            models.Index(fields=["collection"]),
            models.Index(fields=["original_location"]),
        ]

    def __str__(self) -> str:
        return f"DocumentMetadata(document_id={self.document_id})"


class TranskribusRun(models.Model):
    """
    One Transkribus processing attempt for a VS-Archive Document.

    Tracks external TrpServer identity and job ids for ops/debugging.
    ``TranskribusRun.status`` is the Trp attempt lifecycle only — it does not
    replace ``Document.processing_state_user``, ``DocumentTextResult.status``,
    or ``DocumentTextResult.verification_status``.
    """

    class Mode(models.TextChoices):
        UPLOAD_CREATED = "UPLOAD_CREATED", "Upload created"
        EXISTING_SERVER = "EXISTING_SERVER", "Existing server document"

    class Status(models.TextChoices):
        STARTED = "STARTED", "Started"
        UPLOADED = "UPLOADED", "Uploaded"
        RECOGNITION_STARTED = "RECOGNITION_STARTED", "Recognition started"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="transkribus_runs",
    )

    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.STARTED,
    )

    mode = models.CharField(max_length=32, choices=Mode.choices)

    collection_id = models.CharField(max_length=64)
    model_id = models.CharField(max_length=64)

    remote_doc_id = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="TrpServer docId when known.",
    )

    pages_query = models.CharField(
        max_length=512,
        null=True,
        blank=True,
        help_text="Trp pages= query used for recognition/metadata.",
    )

    page_index_to_page_nr = models.JSONField(
        null=True,
        blank=True,
        default=None,
        help_text="VS-Archive page_index → Trp pageNr (upload mode).",
    )

    upload_id = models.BigIntegerField(null=True, blank=True)
    ingest_job_id = models.CharField(max_length=128, null=True, blank=True)
    recognition_job_id = models.CharField(max_length=128, null=True, blank=True)

    engine_runtime = models.CharField(
        max_length=255,
        null=True,
        blank=True,
        help_text="Runtime engine string, e.g. transkribus-pylaia:{model_id}.",
    )

    error_code = models.CharField(max_length=64, null=True, blank=True)
    error_details = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["document", "-created_at"], name="tr_run_doc_created_idx"
            ),
            models.Index(fields=["document", "status"], name="tr_run_doc_status_idx"),
            models.Index(fields=["remote_doc_id"], name="tr_run_remote_doc_idx"),
            models.Index(
                fields=["status", "created_at"], name="tr_run_status_created_idx"
            ),
        ]

    def __str__(self) -> str:
        doc_part = self.remote_doc_id or "pending"
        return (
            f"TranskribusRun(doc={self.document_id}, status={self.status}, "
            f"remote_doc_id={doc_part})"
        )


class DocumentSourceFile(models.Model):
    """
    One ordered source file belonging to a logical Document.

    V1 product scope (future PRs): multiple IMAGE source files per document only.
    ``order_index`` is zero-based; UI may display ``order_index + 1`` later.
    """

    class UploadStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        UPLOADED = "UPLOADED", "Uploaded"
        FAILED = "FAILED", "Failed"

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="source_files",
    )

    order_index = models.IntegerField(
        help_text="Zero-based position among this document's source files.",
    )

    file_s3_key = models.CharField(max_length=1024)
    file_original_name = models.CharField(max_length=512, blank=True, default="")
    mime_type = models.CharField(max_length=128, blank=True, default="")
    size_bytes = models.BigIntegerField(null=True, blank=True)

    upload_status = models.CharField(
        max_length=16,
        choices=UploadStatus.choices,
        default=UploadStatus.PENDING,
    )
    upload_error = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["order_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "order_index"],
                name="uniq_document_sourcefile_order",
            ),
            models.UniqueConstraint(
                fields=["document", "file_s3_key"],
                name="uniq_document_sourcefile_s3_key",
            ),
            models.CheckConstraint(
                condition=models.Q(order_index__gte=0),
                name="dsf_order_index_gte_0",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"DocumentSourceFile(doc={self.document_id}, "
            f"order_index={self.order_index})"
        )


class TranskribusTranscriptSnapshot(models.Model):
    """
    Immutable Transkribus PAGE-XML transcript snapshot for a Document.

    Snapshots are history owned by the document. Active geometry association to
    displayed text is via ``TranskribusTextResultBinding``, not this row alone.

    Cross-document ``transkribus_run`` mismatches are rejected in ``save()``.
    Do not use ``bulk_create`` for this model (bypasses that check).
    """

    class SourceKind(models.TextChoices):
        AUTOMATIC_HTR = "AUTOMATIC_HTR", "Automatic HTR"
        CORRECTED_CURRENT_SYNC = "CORRECTED_CURRENT_SYNC", "Corrected-current sync"

    class StorageStatus(models.TextChoices):
        PENDING_UPLOAD = "PENDING_UPLOAD", "Pending upload"
        READY = "READY", "Ready"
        FAILED = "FAILED", "Failed"

    class GeometryCapability(models.TextChoices):
        VERIFIED = "VERIFIED", "Verified"
        PARTIAL = "PARTIAL", "Partial"
        NOT_AVAILABLE = "NOT_AVAILABLE", "Not available"
        INDETERMINATE = "INDETERMINATE", "Indeterminate"

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="transkribus_transcript_snapshots",
    )
    transkribus_run = models.ForeignKey(
        TranskribusRun,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transcript_snapshots",
    )

    source_kind = models.CharField(max_length=32, choices=SourceKind.choices)
    remote_doc_id = models.CharField(max_length=64, blank=True, default="")
    collection_id = models.CharField(max_length=64, blank=True, default="")
    model_id = models.CharField(max_length=64, blank=True, default="")
    recognition_job_id = models.CharField(max_length=128, blank=True, default="")

    parser_version = models.CharField(max_length=64)

    # Fingerprints are nullable while storage_status is PENDING_UPLOAD / FAILED.
    provider_identity_fingerprint = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="SHA-256 of ordered page_index:tsId lines. Not unique.",
    )
    raw_xml_fingerprint = models.CharField(
        max_length=64,
        null=True,
        blank=True,
        help_text="SHA-256 of ordered per-page PAGE XML SHA-256 digests.",
    )

    canonical_text = models.TextField(blank=True, default="")
    canonical_text_sha256 = models.CharField(max_length=64, blank=True, default="")

    geometry_capability = models.CharField(
        max_length=32,
        choices=GeometryCapability.choices,
        default=GeometryCapability.INDETERMINATE,
    )
    hover_eligible = models.BooleanField(default=False)

    storage_status = models.CharField(
        max_length=32,
        choices=StorageStatus.choices,
        default=StorageStatus.PENDING_UPLOAD,
    )

    # Observed provider transcript status metadata only — never verification.
    remote_status_summary = models.JSONField(null=True, blank=True, default=None)

    created_at = models.DateTimeField(auto_now_add=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transkribus_transcript_snapshots_created",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["document", "-created_at"],
                name="tr_snap_doc_created_idx",
            ),
            models.Index(
                fields=["document", "storage_status"],
                name="tr_snap_doc_storage_idx",
            ),
            models.Index(
                fields=["raw_xml_fingerprint"],
                name="tr_snap_raw_xml_fp_idx",
            ),
        ]
        constraints = [
            # Same raw XML may be reparsed under a future parser_version.
            # Pending rows may omit fingerprints; only READY+complete fingerprints dedupe.
            models.UniqueConstraint(
                fields=["document", "parser_version", "raw_xml_fingerprint"],
                condition=models.Q(storage_status="READY")
                & models.Q(raw_xml_fingerprint__isnull=False)
                & ~models.Q(raw_xml_fingerprint=""),
                name="uniq_tr_snap_ready_raw_xml",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(storage_status="READY")
                    | (
                        models.Q(provider_identity_fingerprint__isnull=False)
                        & ~models.Q(provider_identity_fingerprint="")
                        & models.Q(raw_xml_fingerprint__isnull=False)
                        & ~models.Q(raw_xml_fingerprint="")
                        & ~models.Q(canonical_text_sha256="")
                    )
                ),
                name="tr_snap_ready_requires_fingerprints",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        self._validate_transkribus_run_document()

    def _validate_transkribus_run_document(self) -> None:
        if not self.transkribus_run_id or not self.document_id:
            return
        transkribus_run = self.transkribus_run
        if transkribus_run is None:
            raise ValidationError(
                {
                    "transkribus_run": (
                        "TranskribusTranscriptSnapshot requires a valid "
                        "TranskribusRun when transkribus_run_id is set."
                    )
                }
            )
        run_document_id = transkribus_run.document_id
        if run_document_id != self.document_id:
            raise ValidationError(
                {
                    "transkribus_run": (
                        "TranskribusTranscriptSnapshot requires transkribus_run "
                        "to belong to the same document."
                    )
                }
            )

    def save(self, *args, **kwargs):
        # bulk_create bypasses save(); do not use it for this model.
        self._validate_transkribus_run_document()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return (
            f"TranskribusTranscriptSnapshot(id={self.pk}, doc={self.document_id}, "
            f"status={self.storage_status})"
        )


class TranskribusSnapshotPage(models.Model):
    """One PAGE XML page within an immutable Transkribus transcript snapshot."""

    class GeometryCapability(models.TextChoices):
        VERIFIED = "VERIFIED", "Verified"
        PARTIAL = "PARTIAL", "Partial"
        NOT_AVAILABLE = "NOT_AVAILABLE", "Not available"
        INDETERMINATE = "INDETERMINATE", "Indeterminate"

    snapshot = models.ForeignKey(
        TranskribusTranscriptSnapshot,
        on_delete=models.CASCADE,
        related_name="pages",
    )

    page_index = models.PositiveIntegerField(
        help_text="1-based local page index (matches PageImage.page_index).",
    )
    page_nr = models.PositiveIntegerField(
        help_text="Transkribus pageNr.",
    )
    transcript_ts_id = models.CharField(max_length=64)
    provider_page_id = models.BigIntegerField(null=True, blank=True)

    image_width = models.PositiveIntegerField(null=True, blank=True)
    image_height = models.PositiveIntegerField(null=True, blank=True)
    image_filename = models.CharField(max_length=512, blank=True, default="")
    page_namespace = models.CharField(max_length=255, blank=True, default="")

    remote_transcript_status = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Observed provider transcript status; not verification.",
    )

    page_xml_s3_key = models.CharField(
        max_length=1024,
        blank=True,
        default="",
        help_text="Future S3 key for raw PAGE XML; unused in schema PR.",
    )
    page_xml_sha256 = models.CharField(max_length=64, blank=True, default="")

    text_region_count = models.PositiveIntegerField(default=0)
    text_line_count = models.PositiveIntegerField(default=0)
    lines_with_non_empty_text = models.PositiveIntegerField(default=0)
    duplicate_line_ids = models.PositiveIntegerField(default=0)
    reading_order_present = models.BooleanField(default=False)
    reading_order_resolved = models.BooleanField(default=False)
    lines_xml_order_differs_from_reading_order = models.PositiveIntegerField(default=0)

    page_geometry_capability = models.CharField(
        max_length=32,
        choices=GeometryCapability.choices,
        default=GeometryCapability.INDETERMINATE,
    )

    class Meta:
        ordering = ["page_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["snapshot", "page_index"],
                name="uniq_tr_snap_page_index",
            ),
            models.UniqueConstraint(
                fields=["snapshot", "page_nr"],
                name="uniq_tr_snap_page_nr",
            ),
            models.CheckConstraint(
                condition=models.Q(page_index__gte=1),
                name="tr_snap_page_index_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(page_nr__gte=1),
                name="tr_snap_page_nr_gte_1",
            ),
            models.CheckConstraint(
                condition=~models.Q(transcript_ts_id=""),
                name="tr_snap_page_ts_id_nonempty",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(image_width__isnull=True) | models.Q(image_width__gte=1)
                ),
                name="tr_snap_page_image_width_positive",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(image_height__isnull=True) | models.Q(image_height__gte=1)
                ),
                name="tr_snap_page_image_height_positive",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"TranskribusSnapshotPage(snapshot={self.snapshot_id}, "
            f"page_index={self.page_index})"
        )


class TranskribusSnapshotLine(models.Model):
    """One TextLine within a snapshot page (immutable geometry + text slice)."""

    page = models.ForeignKey(
        TranskribusSnapshotPage,
        on_delete=models.CASCADE,
        related_name="lines",
    )

    order_index = models.PositiveIntegerField(
        help_text="Zero-based order among retained lines in XML document order.",
    )
    provider_region_id = models.CharField(max_length=128, blank=True, default="")
    provider_line_id = models.CharField(max_length=128, blank=True, default="")

    text = models.TextField(blank=True, default="")
    contributes_to_canonical = models.BooleanField(default=True)

    char_start = models.PositiveIntegerField()
    char_end = models.PositiveIntegerField()

    # Coordinates as JSON arrays of [x, y] pairs (float-compatible numbers).
    polygon_points = models.JSONField(null=True, blank=True, default=None)
    baseline_points = models.JSONField(null=True, blank=True, default=None)

    bbox_min_x = models.FloatField(null=True, blank=True)
    bbox_min_y = models.FloatField(null=True, blank=True)
    bbox_max_x = models.FloatField(null=True, blank=True)
    bbox_max_y = models.FloatField(null=True, blank=True)

    coords_valid = models.BooleanField(default=False)
    baseline_valid = models.BooleanField(default=False)
    has_meaningful_geometry = models.BooleanField(default=False)

    class Meta:
        ordering = ["order_index"]
        constraints = [
            models.UniqueConstraint(
                fields=["page", "order_index"],
                name="uniq_tr_snap_line_order",
            ),
            models.CheckConstraint(
                condition=models.Q(char_end__gte=models.F("char_start")),
                name="tr_snap_line_char_end_gte_start",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"TranskribusSnapshotLine(page={self.page_id}, "
            f"order_index={self.order_index})"
        )


class TranskribusTextResultBinding(models.Model):
    """
    Explicit active binding from a DocumentTextResult to a transcript snapshot.

    Geometry/hover must follow this binding (and freshness checks in later PRs),
    not document-level snapshot ownership alone.

    Cross-document mismatches are rejected in ``save()``. Do not use
    ``bulk_create`` for this model (bypasses that check).
    """

    class BindingRole(models.TextChoices):
        SNAPSHOT_SOURCE = "SNAPSHOT_SOURCE", "Snapshot source text"
        HEBREW_MIRROR = "HEBREW_MIRROR", "Hebrew mirror of snapshot text"

    text_result = models.OneToOneField(
        DocumentTextResult,
        on_delete=models.CASCADE,
        related_name="transkribus_snapshot_binding",
    )
    snapshot = models.ForeignKey(
        TranskribusTranscriptSnapshot,
        on_delete=models.CASCADE,
        related_name="text_result_bindings",
    )
    binding_role = models.CharField(
        max_length=32,
        choices=BindingRole.choices,
        default=BindingRole.SNAPSHOT_SOURCE,
    )
    bound_text_sha256 = models.CharField(max_length=64)
    bound_source_revision = models.PositiveIntegerField()

    bound_at = models.DateTimeField(auto_now_add=True)
    bound_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transkribus_text_result_bindings",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["snapshot"],
                name="tr_bind_snapshot_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        self._validate_same_document()

    def _validate_same_document(self) -> None:
        if not self.text_result_id or not self.snapshot_id:
            return
        text_document_id = self.text_result.document_id
        snapshot_document_id = self.snapshot.document_id
        if text_document_id != snapshot_document_id:
            raise ValidationError(
                {
                    "snapshot": (
                        "TranskribusTextResultBinding requires the snapshot and "
                        "text result to belong to the same document."
                    )
                }
            )

    def save(self, *args, **kwargs):
        # bulk_create bypasses save(); do not use it for this model.
        self._validate_same_document()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return (
            f"TranskribusTextResultBinding(text_result={self.text_result_id}, "
            f"snapshot={self.snapshot_id})"
        )


class TranskribusRunAutomaticSnapshot(models.Model):
    """
    Durable association from a TranskribusRun to the READY AUTOMATIC_HTR snapshot
    used for that run's local completion.

    ``TranskribusTranscriptSnapshot.transkribus_run`` remains origin/provenance of
    first creation. Storage may reuse an identical READY snapshot across runs;
    each consuming run records its own association here.

    ``mapping_trusted`` is run-level (upload map vs EXISTING_SERVER traversal) and
    must not mutate immutable snapshot hover_eligible.
    """

    run = models.OneToOneField(
        TranskribusRun,
        on_delete=models.CASCADE,
        related_name="automatic_snapshot_association",
    )
    snapshot = models.ForeignKey(
        TranskribusTranscriptSnapshot,
        on_delete=models.CASCADE,
        related_name="run_associations",
    )
    mapping_trusted = models.BooleanField(
        default=False,
        help_text=(
            "True when page_index↔pageNr came from a trusted upload mapping. "
            "False for EXISTING_SERVER traversal-only indexes."
        ),
    )
    # Engine outcome review reasons (e.g. EMPTY_TRANSCRIPT_PAGE) for resume.
    review_reasons = models.JSONField(default=list, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def clean(self) -> None:
        super().clean()
        self._validate_same_document()
        self._validate_ready_automatic()

    def _validate_same_document(self) -> None:
        if not self.run_id or not self.snapshot_id:
            return
        if self.run.document_id != self.snapshot.document_id:
            raise ValidationError(
                {
                    "snapshot": (
                        "TranskribusRunAutomaticSnapshot requires the run and "
                        "snapshot to belong to the same document."
                    )
                }
            )

    def _validate_ready_automatic(self) -> None:
        if not self.snapshot_id:
            return
        if (
            self.snapshot.storage_status
            != TranskribusTranscriptSnapshot.StorageStatus.READY
        ):
            raise ValidationError(
                {
                    "snapshot": (
                        "TranskribusRunAutomaticSnapshot requires a READY snapshot."
                    )
                }
            )
        if (
            self.snapshot.source_kind
            != TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR
        ):
            raise ValidationError(
                {
                    "snapshot": (
                        "TranskribusRunAutomaticSnapshot requires AUTOMATIC_HTR "
                        "source_kind."
                    )
                }
            )

    def save(self, *args, **kwargs):
        # bulk_create bypasses save(); do not use it for this model.
        self._validate_same_document()
        self._validate_ready_automatic()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return (
            f"TranskribusRunAutomaticSnapshot(run={self.run_id}, "
            f"snapshot={self.snapshot_id})"
        )


class TranskribusCorrectedCurrentSyncAttempt(models.Model):
    """
    Staff-initiated corrected/current Transkribus sync provenance for a Document.

    Records whether a sync completed, was refused at selection, or failed before
    a READY snapshot link. Future activation must reference an explicit COMPLETED
    attempt id (never infer latest). Does not perform HTTP, storage, or activation.
    """

    class Status(models.TextChoices):
        STARTED = "STARTED", "Started"
        COMPLETED = "COMPLETED", "Completed"
        REFUSED = "REFUSED", "Refused"
        FAILED = "FAILED", "Failed"

    class StorageOutcome(models.TextChoices):
        CREATED = "CREATED", "Created"
        REUSED_EXISTING = "REUSED_EXISTING", "Reused existing"
        REUSED_CONCURRENT_WINNER = (
            "REUSED_CONCURRENT_WINNER",
            "Reused concurrent winner",
        )

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="transkribus_corrected_current_sync_attempts",
    )
    transkribus_run = models.ForeignKey(
        TranskribusRun,
        on_delete=models.RESTRICT,
        related_name="corrected_current_sync_attempts",
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transkribus_corrected_current_sync_attempts",
    )

    status = models.CharField(max_length=16, choices=Status.choices)

    resolved_snapshot = models.ForeignKey(
        TranskribusTranscriptSnapshot,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="corrected_current_sync_attempts",
    )
    storage_outcome = models.CharField(
        max_length=32,
        choices=StorageOutcome.choices,
        null=True,
        blank=True,
    )

    failure_code = models.CharField(max_length=64, null=True, blank=True)
    failure_message = models.CharField(max_length=512, null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["document", "-created_at"],
                name="tr_cc_sync_doc_created_idx",
            ),
            models.Index(
                fields=["document", "status"],
                name="tr_cc_sync_doc_status_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "STARTED",
                        "COMPLETED",
                        "REFUSED",
                        "FAILED",
                    ]
                ),
                name="tr_cc_sync_status_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(storage_outcome__isnull=True)
                | models.Q(
                    storage_outcome__in=[
                        "CREATED",
                        "REUSED_EXISTING",
                        "REUSED_CONCURRENT_WINNER",
                    ]
                ),
                name="tr_cc_sync_storage_outcome_valid",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="STARTED")
                    | (
                        models.Q(completed_at__isnull=True)
                        & models.Q(resolved_snapshot__isnull=True)
                        & models.Q(storage_outcome__isnull=True)
                        & models.Q(failure_code__isnull=True)
                        & models.Q(failure_message__isnull=True)
                    )
                ),
                name="tr_cc_sync_started_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="COMPLETED")
                    | (
                        models.Q(completed_at__isnull=False)
                        & models.Q(resolved_snapshot__isnull=False)
                        & models.Q(storage_outcome__isnull=False)
                        & models.Q(failure_code__isnull=True)
                        & models.Q(failure_message__isnull=True)
                    )
                ),
                name="tr_cc_sync_completed_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="REFUSED")
                    | (
                        models.Q(completed_at__isnull=False)
                        & models.Q(resolved_snapshot__isnull=True)
                        & models.Q(storage_outcome__isnull=True)
                        & models.Q(failure_code__isnull=True)
                        & models.Q(failure_message__isnull=True)
                    )
                ),
                name="tr_cc_sync_refused_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="FAILED")
                    | (
                        models.Q(completed_at__isnull=False)
                        & models.Q(failure_code__isnull=False)
                        & ~models.Q(failure_code="")
                        & models.Q(resolved_snapshot__isnull=True)
                        & models.Q(storage_outcome__isnull=True)
                    )
                ),
                name="tr_cc_sync_failed_shape",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        self._validate_transkribus_run()
        self._validate_resolved_snapshot()

    def _validate_transkribus_run(self) -> None:
        if not self.transkribus_run_id or not self.document_id:
            return
        run = self.transkribus_run
        if run.document_id != self.document_id:
            raise ValidationError(
                {
                    "transkribus_run": (
                        "TranskribusCorrectedCurrentSyncAttempt requires "
                        "transkribus_run to belong to the same document."
                    )
                }
            )
        if run.mode != TranskribusRun.Mode.UPLOAD_CREATED:
            raise ValidationError(
                {
                    "transkribus_run": (
                        "Corrected-current sync requires an UPLOAD_CREATED "
                        "TranskribusRun with trusted page_index_to_page_nr."
                    )
                }
            )
        mapping = run.page_index_to_page_nr
        if not isinstance(mapping, dict) or not mapping:
            raise ValidationError(
                {
                    "transkribus_run": (
                        "Corrected-current sync requires a non-empty "
                        "page_index_to_page_nr mapping on the TranskribusRun."
                    )
                }
            )

    def _validate_resolved_snapshot(self) -> None:
        if not self.resolved_snapshot_id:
            return
        snapshot = self.resolved_snapshot
        if snapshot is None:
            return
        if snapshot.document_id != self.document_id:
            raise ValidationError(
                {
                    "resolved_snapshot": (
                        "Resolved snapshot must belong to the same document "
                        "as the sync attempt."
                    )
                }
            )
        if snapshot.storage_status != TranskribusTranscriptSnapshot.StorageStatus.READY:
            raise ValidationError(
                {
                    "resolved_snapshot": (
                        "Resolved snapshot must have storage_status=READY."
                    )
                }
            )

    def save(self, *args, **kwargs):
        self._validate_transkribus_run()
        self._validate_resolved_snapshot()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return (
            f"TranskribusCorrectedCurrentSyncAttempt(id={self.pk}, "
            f"doc={self.document_id}, status={self.status})"
        )


class TranskribusCorrectedCurrentSyncPage(models.Model):
    """Per-page corrected/current selection or refusal for one sync attempt."""

    class Outcome(models.TextChoices):
        SELECTED = "SELECTED", "Selected"
        REFUSED = "REFUSED", "Refused"

    class SelectionErrorCode(models.TextChoices):
        ZERO_TRANSCRIPTS = "ZERO_TRANSCRIPTS", "Zero transcripts"
        MULTIPLE_TRANSCRIPTS = "MULTIPLE_TRANSCRIPTS", "Multiple transcripts"
        MISSING_TS_ID = "MISSING_TS_ID", "Missing tsId"

    attempt = models.ForeignKey(
        TranskribusCorrectedCurrentSyncAttempt,
        on_delete=models.CASCADE,
        related_name="pages",
    )

    page_index = models.PositiveIntegerField(
        help_text="1-based local page index (matches PageImage.page_index).",
    )
    page_nr = models.PositiveIntegerField(
        help_text="Transkribus pageNr.",
    )

    outcome = models.CharField(max_length=16, choices=Outcome.choices)

    transcript_ts_id = models.CharField(max_length=64, blank=True, default="")
    remote_transcript_status = models.CharField(
        max_length=64,
        blank=True,
        default="",
        help_text="Observed provider transcript status; not verification.",
    )
    in_progress_warning = models.BooleanField(default=False)

    selection_error_code = models.CharField(
        max_length=32,
        choices=SelectionErrorCode.choices,
        blank=True,
        default="",
    )
    selection_error_message = models.CharField(max_length=512, blank=True, default="")

    class Meta:
        ordering = ["page_index"]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(outcome__in=["SELECTED", "REFUSED"]),
                name="tr_cc_sync_page_outcome_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(selection_error_code="")
                | models.Q(
                    selection_error_code__in=[
                        "ZERO_TRANSCRIPTS",
                        "MULTIPLE_TRANSCRIPTS",
                        "MISSING_TS_ID",
                    ]
                ),
                name="tr_cc_sync_selection_error_code_valid",
            ),
            models.UniqueConstraint(
                fields=["attempt", "page_index"],
                name="uniq_tr_cc_sync_page_index",
            ),
            models.UniqueConstraint(
                fields=["attempt", "page_nr"],
                name="uniq_tr_cc_sync_page_nr",
            ),
            models.CheckConstraint(
                condition=models.Q(page_index__gte=1),
                name="tr_cc_sync_page_index_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(page_nr__gte=1),
                name="tr_cc_sync_page_nr_gte_1",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(outcome="SELECTED")
                    | (
                        ~models.Q(transcript_ts_id="")
                        & models.Q(selection_error_code="")
                        & models.Q(selection_error_message="")
                    )
                ),
                name="tr_cc_sync_page_selected_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(outcome="REFUSED")
                    | (
                        models.Q(transcript_ts_id="")
                        & ~models.Q(selection_error_code="")
                        & ~models.Q(selection_error_message="")
                    )
                ),
                name="tr_cc_sync_page_refused_shape",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"TranskribusCorrectedCurrentSyncPage(attempt={self.attempt_id}, "
            f"page_index={self.page_index}, outcome={self.outcome})"
        )
