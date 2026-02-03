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

    # Required for V1
    title = models.CharField(max_length=255)
    doc_type = models.CharField(max_length=16, choices=DocType.choices)

    # Optional metadata (often unknown at upload time)
    date_start = models.DateField(null=True, blank=True)
    date_end = models.DateField(null=True, blank=True)

    language = models.CharField(max_length=16, null=True, blank=True)  # he/en/ar/fr
    category_event = models.CharField(max_length=255, null=True, blank=True)

    tags_m2m = models.ManyToManyField("Tag", blank=True, related_name="documents")

    # Metadata completion status
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
        ACTION_REQUIRED = "ACTION_REQUIRED", "Action required"
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

    status = models.CharField(max_length=32, choices=Status.choices)
    verification_status = models.CharField(
        max_length=32,
        choices=VerificationStatus.choices,
        default=VerificationStatus.UNVERIFIED,
    )

    text = models.TextField(null=True, blank=True)

    error_code = models.CharField(max_length=64, null=True, blank=True)
    error_details = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            models.Index(fields=["document", "result_type", "engine"]),
            models.Index(fields=["status", "verification_status"]),
        ]


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

    # Optional: points to a specific field path (e.g., "title", "metadata.source", etc.)
    field_path = models.CharField(max_length=512, null=True, blank=True)

    message = models.TextField()

    # option for public
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
