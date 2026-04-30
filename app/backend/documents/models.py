import uuid
from django.db import models


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

    # Required for V1
    title = models.CharField(max_length=255)
    doc_type = models.CharField(max_length=16, choices=DocType.choices)

    # Optional metadata (often unknown at upload time)
    date_start = models.DateField(null=True, blank=True)
    date_end = models.DateField(null=True, blank=True)

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

    category_event = models.CharField(max_length=255, null=True, blank=True)

    tags_m2m = models.ManyToManyField("Tag", blank=True, related_name="documents")

    metadata_status = models.CharField(
        max_length=32,
        choices=MetadataStatus.choices,
        default=MetadataStatus.NEEDS_COMPLETION,
    )

    visibility = models.CharField(
        max_length=16,
        choices=Visibility.choices,
        default=Visibility.PRIVATE,
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

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self) -> str:
        return str(self.title)


class DocumentTextResult(models.Model):
    class OcrEngineKey(models.TextChoices):
        GEMINI = "GEMINI", "Gemini"

    class OcrPromptVariant(models.TextChoices):
        HANDWRITTEN = "handwritten", "Handwritten"
        PRINTED = "printed", "Printed"

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

    error_code = models.CharField(max_length=64, null=True, blank=True)
    error_details = models.TextField(null=True, blank=True)

    # NEW: why this result was marked as NEEDS_REVIEW.
    # Store as a JSON string (e.g. ["HAS_UNCLEAR","CONSISTENCY_MISMATCH"]) or plain text.
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
