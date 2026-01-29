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

    tags = models.JSONField(default=list)      # list[str]
    metadata = models.JSONField(default=dict)  # flexible metadata

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

    # S3 file data
    file_s3_key = models.CharField(max_length=1024, blank=True, default="")
    file_original_name = models.CharField(max_length=512, blank=True, default="")
    mime_type = models.CharField(max_length=128, blank=True, default="")
    size_bytes = models.BigIntegerField(null=True, blank=True)
    upload_error = models.TextField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return self.title


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

    status = models.CharField(max_length=16, choices=Status.choices, default=Status.OPEN)
    scope = models.CharField(max_length=16, choices=Scope.choices)

    # אופציונלי: מצביע לשדה (title / metadata.source / etc)
    field_path = models.CharField(max_length=512, null=True, blank=True)

    message = models.TextField()

    # אופציונלי לציבור
    requester_name = models.CharField(max_length=255, null=True, blank=True)
    requester_email = models.EmailField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
