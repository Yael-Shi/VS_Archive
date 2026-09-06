from __future__ import annotations

import uuid

from django.conf import settings
from django.contrib.postgres.indexes import GinIndex
from django.contrib.postgres.search import SearchVectorField
from django.core.exceptions import ValidationError
from django.db import models, transaction


class ArchiveItem(models.Model):
    """Central archival content entity; OCR-backed documents link via Document.archive_item."""

    class ItemType(models.TextChoices):
        OCR_DOCUMENT = "OCR_DOCUMENT", "OCR document"
        MANUAL_TEXT = "MANUAL_TEXT", "Manual text"
        PHOTO = "PHOTO", "Photo"
        VIDEO = "VIDEO", "Video"

    class Visibility(models.TextChoices):
        PRIVATE = "private", "Private"
        PUBLIC = "public", "Public"
        RESTRICTED = "restricted", "Restricted"

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
    people: models.ManyToManyField[Person, ArchiveItemPerson] = models.ManyToManyField(
        "Person",
        through="ArchiveItemPerson",
        blank=True,
        related_name="archive_items",
    )
    authors: models.ManyToManyField[Author, ArchiveItemAuthor] = models.ManyToManyField(
        "Author",
        through="ArchiveItemAuthor",
        blank=True,
        related_name="archive_items",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        permissions = [
            (
                "view_restricted_archiveitem",
                "Can view restricted archive items",
            ),
        ]

    def __str__(self) -> str:
        return f"{self.title} ({self.item_type})"

    @property
    def primary_photo_content(self) -> PhotoContent | None:
        """First PhotoContent by ``(position, id)`` for browse/preview call sites.

        This is not a compatibility alias for the former OneToOne reverse
        relation ``photo_content``. Public detail may present all renderable
        photos; browse cards and item eligibility still use this first row.
        """
        cached = getattr(self, "_prefetched_objects_cache", None)
        if cached is not None and "photo_contents" in cached:
            photos = list(cached["photo_contents"])
            if not photos:
                return None
            return min(photos, key=lambda photo: (photo.position, photo.pk))
        return self.photo_contents.order_by("position", "id").first()


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


class ArchiveItemSearchIndex(models.Model):
    """Denormalized full-text search document for one ArchiveItem (PR1 foundation)."""

    archive_item = models.OneToOneField(
        ArchiveItem,
        on_delete=models.CASCADE,
        related_name="search_index",
    )
    # Weight A — title alone (not duplicated into metadata_text).
    title_text = models.TextField(blank=True, default="")
    # Weight B — author, source_title, categories, events, tags, public_note,
    # ArchiveItemPerson canonical names and aliases (all item types), plus
    # public-renderable PHOTO PhotoContent descriptive fields and PhotoPerson
    # names/aliases (same gallery renderability contract).
    metadata_text = models.TextField(blank=True, default="")
    # Weight C — ManualText body or displayed OCR transcription (source/original).
    body_text = models.TextField(blank=True, default="")
    # Weight C — displayed Hebrew translation for non-Hebrew OCR only (never
    # concatenated into body_text; empty for Hebrew docs / ManualText / photos).
    hebrew_translation_text = models.TextField(blank=True, default="")
    search_vector = SearchVectorField(null=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        indexes = [
            GinIndex(
                fields=["search_vector"],
                name="archive_item_search_vector_gin",
            ),
        ]

    def __str__(self) -> str:
        return f"ArchiveItemSearchIndex(archive_item_id={self.archive_item_id})"


class PhotoContent(models.Model):
    """One image/component belonging to a PHOTO archive item (not OCR/Document-backed)."""

    class UploadStatus(models.TextChoices):
        PENDING = "PENDING", "Pending"
        UPLOADED = "UPLOADED", "Uploaded"
        FAILED = "FAILED", "Failed"

    archive_item = models.ForeignKey(
        ArchiveItem,
        on_delete=models.CASCADE,
        related_name="photo_contents",
    )
    position = models.PositiveIntegerField(default=1)
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
    date_start = models.DateField(null=True, blank=True)
    date_end = models.DateField(null=True, blank=True)
    date_precision = models.CharField(
        max_length=16,
        choices=ArchiveItem.DatePrecision.choices,
        default=ArchiveItem.DatePrecision.UNKNOWN,
    )
    description = models.TextField(blank=True, default="")
    location = models.TextField(blank=True, default="")
    context = models.TextField(blank=True, default="")
    people_present = models.TextField(blank=True, default="")
    notes = models.TextField(blank=True, default="")
    people: models.ManyToManyField[Person, PhotoPerson] = models.ManyToManyField(
        "Person",
        through="PhotoPerson",
        blank=True,
        related_name="photo_contents",
    )
    width = models.PositiveIntegerField(null=True, blank=True)
    height = models.PositiveIntegerField(null=True, blank=True)
    thumbnail_file_key = models.CharField(max_length=1024, blank=True, default="")
    thumbnail_mime_type = models.CharField(max_length=128, blank=True, default="")
    thumbnail_size_bytes = models.PositiveBigIntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["archive_item", "position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["archive_item", "position"],
                name="uniq_photocontent_archive_item_position",
            ),
            models.CheckConstraint(
                condition=models.Q(position__gte=1),
                name="photocontent_position_gte_1",
            ),
        ]

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
        if self.position is not None and self.position < 1:
            raise ValidationError({"position": "position must be at least 1."})
        from documents.services.archive_item_validation import (
            validate_stored_archive_date_fields,
        )

        validate_stored_archive_date_fields(
            date_start=self.date_start,
            date_end=self.date_end,
            date_precision=self.date_precision,
        )

    def __str__(self) -> str:
        return (
            f"PhotoContent(archive_item_id={self.archive_item_id}, "
            f"position={self.position})"
        )


class Person(models.Model):
    """One identified person in the archive (canonical display name)."""

    name = models.CharField(max_length=255)
    biography = models.TextField(blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name"]

    def __str__(self) -> str:
        return self.name


class PersonAlias(models.Model):
    """Alternate lookup/search name for one Person. Does not replace Person.name."""

    person = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name="aliases",
    )
    name = models.CharField(max_length=255)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["person", "name"],
                name="uniq_person_alias_person_name",
            ),
        ]

    def __str__(self) -> str:
        return self.name


class ArchiveItemPerson(models.Model):
    """Person generally related to an archival item (not a photo appearance or role)."""

    archive_item = models.ForeignKey(
        ArchiveItem,
        on_delete=models.CASCADE,
        related_name="person_links",
    )
    person = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name="archive_item_links",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["archive_item", "person"],
                name="uniq_archive_item_person",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"ArchiveItemPerson(archive_item_id={self.archive_item_id}, "
            f"person_id={self.person_id})"
        )


class Author(models.Model):
    """Bibliographic author name. Not a Person and never inferred from Person."""

    name = models.CharField(max_length=255)
    person = models.ForeignKey(
        Person,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="author_identities",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["name", "id"]

    def __str__(self) -> str:
        return self.name


class ArchiveItemAuthor(models.Model):
    """Ordered author link for an archival item (not a Person relation)."""

    archive_item = models.ForeignKey(
        ArchiveItem,
        on_delete=models.CASCADE,
        related_name="author_links",
    )
    author = models.ForeignKey(
        Author,
        on_delete=models.CASCADE,
        related_name="archive_item_links",
    )
    position = models.PositiveSmallIntegerField()
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["archive_item", "position", "id"]
        constraints = [
            models.UniqueConstraint(
                fields=["archive_item", "author"],
                name="uniq_archive_item_author",
            ),
            models.UniqueConstraint(
                fields=["archive_item", "position"],
                name="uniq_archive_item_author_position",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"ArchiveItemAuthor(archive_item_id={self.archive_item_id}, "
            f"author_id={self.author_id}, position={self.position})"
        )


class PhotoPerson(models.Model):
    """Identified person who appears in a specific photo."""

    photo_content = models.ForeignKey(
        PhotoContent,
        on_delete=models.CASCADE,
        related_name="person_links",
    )
    person = models.ForeignKey(
        Person,
        on_delete=models.CASCADE,
        related_name="photo_links",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["photo_content", "person"],
                name="uniq_photo_person",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"PhotoPerson(photo_content_id={self.photo_content_id}, "
            f"person_id={self.person_id})"
        )


class ReviewedPersonImportBinding(models.Model):
    """Maps a reviewed create_person operation_id to the Person it created.

    Internal import infrastructure only. Not searchable. Not a public or
    staff Person UI surface. Does not relate to Author.
    """

    operation_id = models.CharField(max_length=255, unique=True)
    person = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        related_name="reviewed_import_bindings",
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return (
            f"ReviewedPersonImportBinding(operation_id={self.operation_id!r}, "
            f"person_id={self.person_id})"
        )


class VideoContent(models.Model):
    """External video reference for VIDEO archive items (URL metadata only; no media bytes)."""

    class Provider(models.TextChoices):
        YOUTUBE = "YOUTUBE", "YouTube"
        KAN = "KAN", "Kan"
        OTHER = "OTHER", "Other"

    class PresentationMode(models.TextChoices):
        EMBEDDED = "EMBEDDED", "Embedded"
        EXTERNAL_LINK = "EXTERNAL_LINK", "External link"

    archive_item = models.OneToOneField(
        ArchiveItem,
        on_delete=models.CASCADE,
        related_name="video_content",
    )
    source_url = models.CharField(max_length=2048)
    provider = models.CharField(max_length=16, choices=Provider.choices)
    presentation_mode = models.CharField(
        max_length=16,
        choices=PresentationMode.choices,
    )
    provider_video_id = models.CharField(max_length=32, blank=True, default="")
    start_seconds = models.IntegerField(null=True, blank=True)
    end_seconds = models.IntegerField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(provider__in=["YOUTUBE", "KAN", "OTHER"]),
                name="video_content_provider_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(presentation_mode__in=["EMBEDDED", "EXTERNAL_LINK"]),
                name="video_content_presentation_mode_valid",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        provider="YOUTUBE",
                        presentation_mode="EMBEDDED",
                    )
                    & models.Q(provider_video_id__regex=r"^[A-Za-z0-9_-]{11}$")
                )
                | models.Q(
                    provider="KAN",
                    presentation_mode="EXTERNAL_LINK",
                    provider_video_id="",
                )
                | models.Q(
                    provider="OTHER",
                    presentation_mode="EXTERNAL_LINK",
                    provider_video_id="",
                ),
                name="video_content_provider_mode_id_shape",
            ),
            models.CheckConstraint(
                condition=models.Q(start_seconds__isnull=True)
                | models.Q(start_seconds__gte=0),
                name="video_content_start_seconds_non_negative",
            ),
            models.CheckConstraint(
                condition=models.Q(end_seconds__isnull=True)
                | models.Q(end_seconds__gt=0),
                name="video_content_end_seconds_positive",
            ),
            models.CheckConstraint(
                condition=models.Q(end_seconds__isnull=True)
                | (models.Q(start_seconds__isnull=True) & models.Q(end_seconds__gt=0))
                | models.Q(end_seconds__gt=models.F("start_seconds")),
                name="video_content_end_after_start",
            ),
            models.CheckConstraint(
                condition=models.Q(provider="YOUTUBE")
                | models.Q(
                    start_seconds__isnull=True,
                    end_seconds__isnull=True,
                ),
                name="video_content_times_youtube_only",
            ),
            models.CheckConstraint(
                condition=~models.Q(source_url=""),
                name="video_content_source_url_nonempty",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        # Lazy import avoids circular imports with the URL parser/validation layer.
        from documents.services.video_validation import validate_video_content_instance

        validate_video_content_instance(self)

    def __str__(self) -> str:
        return f"VideoContent(archive_item_id={self.archive_item_id})"


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
        MIXED = "MIXED", "Mixed printed and handwritten"

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
        MIXED = "mixed", "Mixed printed and handwritten"
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

    class Quality(models.TextChoices):
        """Persisted automatic/base quality: UNKNOWN/LOW/MEDIUM/GOOD.

        HUMAN_VERIFIED and NEEDS_CORRECTION are presentation-only and are
        not persisted.
        """

        UNKNOWN = "UNKNOWN", "Unknown"
        LOW = "LOW", "Low"
        MEDIUM = "MEDIUM", "Medium"
        GOOD = "GOOD", "Good"

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
    quality = models.CharField(
        max_length=32,
        choices=Quality.choices,
        default=Quality.UNKNOWN,
        help_text=(
            "Automatic/base public quality (UNKNOWN/LOW/MEDIUM/GOOD). "
            "HUMAN_VERIFIED and NEEDS_CORRECTION are presentation-only and "
            "are not persisted."
        ),
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
            ),
            models.CheckConstraint(
                condition=models.Q(
                    quality__in=["UNKNOWN", "LOW", "MEDIUM", "GOOD"]
                ),
                name="dtr_quality_persisted_values",
            ),
        ]


class GeminiOcrAttempt(models.Model):
    """Durable, reusable identity for one Gemini OCR input/configuration."""

    class Status(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        PARTIAL = "PARTIAL", "Partial"
        COMPLETED = "COMPLETED", "Completed"

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="gemini_ocr_attempts",
    )
    identity_fingerprint = models.CharField(max_length=64)
    source_fingerprint = models.CharField(max_length=64)
    route_fingerprint = models.CharField(max_length=64)
    prompt_fingerprint = models.CharField(max_length=64)
    config_fingerprint = models.CharField(max_length=64)
    prompt_contract_version = models.CharField(max_length=64)
    model_candidates = models.JSONField(default=list)
    expected_page_count = models.PositiveIntegerField()
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.IN_PROGRESS,
    )
    missing_page_indices = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["document", "-created_at"],
                name="gem_ocr_attempt_doc_idx",
            ),
            models.Index(
                fields=["document", "status"],
                name="gem_ocr_attempt_status_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "identity_fingerprint"],
                name="uniq_gem_ocr_attempt_identity",
            ),
            models.CheckConstraint(
                condition=models.Q(expected_page_count__gte=1),
                name="gem_ocr_attempt_page_count_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["IN_PROGRESS", "PARTIAL", "COMPLETED"]),
                name="gem_ocr_attempt_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="COMPLETED")
                    | (
                        models.Q(completed_at__isnull=False)
                        & models.Q(missing_page_indices=[])
                    )
                ),
                name="gem_ocr_attempt_completed_shape",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="COMPLETED") | models.Q(completed_at__isnull=True)
                ),
                name="gem_ocr_attempt_noncompleted_shape",
            ),
        ]


class GeminiOcrPageCheckpoint(models.Model):
    """Durable fenced Gemini OCR result for one 1-based source page."""

    class Status(models.TextChoices):
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    attempt = models.ForeignKey(
        GeminiOcrAttempt,
        on_delete=models.CASCADE,
        related_name="page_checkpoints",
    )
    page_index = models.PositiveIntegerField()
    page_fingerprint = models.CharField(max_length=64)
    source_content_fingerprint = models.CharField(max_length=64)
    status = models.CharField(max_length=32, choices=Status.choices)
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    actual_model = models.CharField(max_length=64, blank=True, default="")
    text = models.TextField(null=True, blank=True)
    needs_review = models.BooleanField(default=False)
    review_reasons = models.JSONField(default=list)
    failure_code = models.CharField(max_length=64, blank=True, default="")
    failure_message = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField()
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["page_index"]
        indexes = [
            models.Index(
                fields=["attempt", "status"],
                name="gem_ocr_page_status_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["attempt", "page_index"],
                name="uniq_gem_ocr_attempt_page",
            ),
            models.CheckConstraint(
                condition=models.Q(page_index__gte=1),
                name="gem_ocr_page_index_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["RUNNING", "SUCCEEDED", "FAILED"]),
                name="gem_ocr_page_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="RUNNING")
                    | (
                        models.Q(lease_token__isnull=False)
                        & models.Q(lease_expires_at__isnull=False)
                        & models.Q(completed_at__isnull=True)
                        & models.Q(actual_model="")
                        & models.Q(text__isnull=True)
                        & models.Q(failure_code="")
                        & models.Q(failure_message="")
                    )
                ),
                name="gem_ocr_page_running_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="SUCCEEDED")
                    | (
                        models.Q(lease_token__isnull=True)
                        & models.Q(lease_expires_at__isnull=True)
                        & models.Q(completed_at__isnull=False)
                        & ~models.Q(actual_model="")
                        & models.Q(text__isnull=False)
                        & ~models.Q(text="")
                        & models.Q(failure_code="")
                        & models.Q(failure_message="")
                    )
                ),
                name="gem_ocr_page_succeeded_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="FAILED")
                    | (
                        models.Q(lease_token__isnull=True)
                        & models.Q(lease_expires_at__isnull=True)
                        & models.Q(completed_at__isnull=False)
                        & models.Q(actual_model="")
                        & models.Q(text__isnull=True)
                        & ~models.Q(failure_code="")
                    )
                ),
                name="gem_ocr_page_failed_shape",
            ),
        ]


class ArabicPrintedOcrAttempt(models.Model):
    """Durable identity for one printed-Arabic banded OCR input/configuration."""

    class Status(models.TextChoices):
        IN_PROGRESS = "IN_PROGRESS", "In progress"
        PARTIAL = "PARTIAL", "Partial"
        COMPLETED = "COMPLETED", "Completed"

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="arabic_printed_ocr_attempts",
    )
    identity_fingerprint = models.CharField(max_length=64)
    source_fingerprint = models.CharField(max_length=64)
    route_fingerprint = models.CharField(max_length=64)
    prompt_fingerprint = models.CharField(max_length=64)
    config_fingerprint = models.CharField(max_length=64)
    prompt_contract_version = models.CharField(max_length=64)
    expected_page_count = models.PositiveIntegerField()
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.IN_PROGRESS,
    )
    missing_page_indices = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        indexes = [
            models.Index(
                fields=["document", "-created_at"],
                name="ar_pr_ocr_attempt_doc_idx",
            ),
            models.Index(
                fields=["document", "status"],
                name="ar_pr_ocr_attempt_status_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["document", "identity_fingerprint"],
                name="uniq_ar_pr_ocr_attempt_identity",
            ),
            models.CheckConstraint(
                condition=models.Q(expected_page_count__gte=1),
                name="ar_pr_ocr_attempt_page_count_gte_1",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["IN_PROGRESS", "PARTIAL", "COMPLETED"]),
                name="ar_pr_ocr_attempt_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="COMPLETED")
                    | (
                        models.Q(completed_at__isnull=False)
                        & models.Q(missing_page_indices=[])
                    )
                ),
                name="ar_pr_ocr_attempt_completed_shape",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="COMPLETED") | models.Q(completed_at__isnull=True)
                ),
                name="ar_pr_ocr_attempt_noncompleted_shape",
            ),
        ]


class ArabicPrintedOcrPageCheckpoint(models.Model):
    """Fenced printed-Arabic OCR result for one zero-based source page."""

    class Status(models.TextChoices):
        PLANNING = "PLANNING", "Planning"
        RUNNING = "RUNNING", "Running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    class PageQuality(models.TextChoices):
        UNASSISTED = "UNASSISTED", "Unassisted"
        ASSISTED = "ASSISTED", "Assisted"
        MIXED = "MIXED", "Mixed"
        CLOUD_VISION_LOW_QUALITY = (
            "CLOUD_VISION_LOW_QUALITY",
            "Cloud Vision low quality",
        )

    attempt = models.ForeignKey(
        ArabicPrintedOcrAttempt,
        on_delete=models.CASCADE,
        related_name="page_checkpoints",
    )
    page_index = models.IntegerField()
    page_fingerprint = models.CharField(max_length=64)
    source_content_fingerprint = models.CharField(max_length=64)
    oriented_image_sha256 = models.CharField(max_length=64)
    oriented_image_width = models.IntegerField()
    oriented_image_height = models.IntegerField()
    cloud_vision_response_sha256 = models.CharField(max_length=64, blank=True, default="")
    cloud_vision_call_count = models.PositiveSmallIntegerField(default=0)
    banding_contract_fingerprint = models.CharField(max_length=64)
    banding_strategy = models.CharField(max_length=64)
    band_count = models.PositiveSmallIntegerField(default=0)
    max_band_height_ratio = models.DecimalField(
        max_digits=4,
        decimal_places=3,
        default="0.350",
    )
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.PLANNING,
    )
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    assembled_text = models.TextField(null=True, blank=True)
    page_quality = models.CharField(
        max_length=32,
        choices=PageQuality.choices,
        blank=True,
        default="",
    )
    runtime_engine_marker = models.CharField(max_length=64, blank=True, default="")
    antigravity_create_count = models.PositiveSmallIntegerField(default=0)
    failure_code = models.CharField(max_length=64, blank=True, default="")
    failure_message = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["page_index"]
        indexes = [
            models.Index(
                fields=["attempt", "status"],
                name="ar_pr_ocr_page_status_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["attempt", "page_index"],
                name="uniq_ar_pr_ocr_attempt_page",
            ),
            models.CheckConstraint(
                condition=models.Q(page_index__gte=0),
                name="ar_pr_ocr_page_index_gte_0",
            ),
            models.CheckConstraint(
                condition=models.Q(oriented_image_width__gte=1)
                & models.Q(oriented_image_height__gte=1),
                name="ar_pr_ocr_page_oriented_dims",
            ),
            models.CheckConstraint(
                condition=models.Q(cloud_vision_call_count__lte=1),
                name="ar_pr_ocr_page_vision_calls",
            ),
            models.CheckConstraint(
                condition=models.Q(antigravity_create_count__lte=12),
                name="ar_pr_ocr_page_ag_creates",
            ),
            models.CheckConstraint(
                condition=models.Q(band_count__lte=6),
                name="ar_pr_ocr_page_band_count_lte_6",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=["PLANNING", "RUNNING", "SUCCEEDED", "FAILED"]
                ),
                name="ar_pr_ocr_page_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="PLANNING")
                    | (
                        models.Q(lease_token__isnull=True)
                        & models.Q(lease_expires_at__isnull=True)
                        & models.Q(completed_at__isnull=True)
                        & models.Q(assembled_text__isnull=True)
                        & models.Q(page_quality="")
                        & models.Q(runtime_engine_marker="")
                        & models.Q(failure_code="")
                        & models.Q(failure_message="")
                    )
                ),
                name="ar_pr_ocr_page_planning_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="RUNNING")
                    | (
                        models.Q(lease_token__isnull=False)
                        & models.Q(lease_expires_at__isnull=False)
                        & models.Q(completed_at__isnull=True)
                        & models.Q(assembled_text__isnull=True)
                        & models.Q(page_quality="")
                        & models.Q(runtime_engine_marker="")
                        & models.Q(failure_code="")
                        & models.Q(failure_message="")
                    )
                ),
                name="ar_pr_ocr_page_running_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="SUCCEEDED")
                    | (
                        models.Q(lease_token__isnull=True)
                        & models.Q(lease_expires_at__isnull=True)
                        & models.Q(completed_at__isnull=False)
                        & models.Q(assembled_text__isnull=False)
                        & ~models.Q(assembled_text="")
                        & models.Q(
                            page_quality__in=[
                                "UNASSISTED",
                                "ASSISTED",
                                "MIXED",
                                "CLOUD_VISION_LOW_QUALITY",
                            ]
                        )
                        & ~models.Q(runtime_engine_marker="")
                        & models.Q(failure_code="")
                        & models.Q(failure_message="")
                        & models.Q(band_count__gte=1)
                    )
                ),
                name="ar_pr_ocr_page_succeeded_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="FAILED")
                    | (
                        models.Q(lease_token__isnull=True)
                        & models.Q(lease_expires_at__isnull=True)
                        & models.Q(completed_at__isnull=False)
                        & models.Q(assembled_text__isnull=True)
                        & models.Q(page_quality="")
                        & models.Q(runtime_engine_marker="")
                        & ~models.Q(failure_code="")
                    )
                ),
                name="ar_pr_ocr_page_failed_shape",
            ),
        ]


class ArabicPrintedOcrBandCheckpoint(models.Model):
    """Normalized band plan and selected transcription for one page band."""

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        PRIMARY_RUNNING = "PRIMARY_RUNNING", "Primary running"
        CANCEL_PENDING = "CANCEL_PENDING", "Cancel pending"
        FALLBACK_RUNNING = "FALLBACK_RUNNING", "Fallback running"
        SUCCEEDED = "SUCCEEDED", "Succeeded"
        FAILED = "FAILED", "Failed"

    class SelectedResult(models.TextChoices):
        UNASSISTED = "UNASSISTED", "Unassisted"
        ASSISTED_FALLBACK = "ASSISTED_FALLBACK", "Assisted fallback"
        CLOUD_VISION_LOW_QUALITY = (
            "CLOUD_VISION_LOW_QUALITY",
            "Cloud Vision low quality",
        )

    page_checkpoint = models.ForeignKey(
        ArabicPrintedOcrPageCheckpoint,
        on_delete=models.CASCADE,
        related_name="band_checkpoints",
    )
    band_index = models.IntegerField()
    rect_x = models.IntegerField()
    rect_y = models.IntegerField()
    rect_width = models.IntegerField()
    rect_height = models.IntegerField()
    crop_mime = models.CharField(max_length=64)
    crop_byte_length = models.PositiveIntegerField()
    crop_sha256 = models.CharField(max_length=64)
    vision_draft_text = models.TextField()
    vision_draft_byte_length = models.PositiveIntegerField()
    vision_draft_sha256 = models.CharField(max_length=64)
    selected_result = models.CharField(
        max_length=32,
        choices=SelectedResult.choices,
        blank=True,
        default="",
    )
    status = models.CharField(
        max_length=32,
        choices=Status.choices,
        default=Status.PENDING,
    )
    primary_interaction_id = models.CharField(max_length=128, blank=True, default="")
    primary_provider_status = models.CharField(max_length=64, blank=True, default="")
    primary_latency_ms = models.PositiveIntegerField(null=True, blank=True)
    primary_failure_type = models.CharField(max_length=64, blank=True, default="")
    primary_safe_diagnostics = models.CharField(max_length=512, blank=True, default="")
    fallback_interaction_id = models.CharField(max_length=128, blank=True, default="")
    fallback_provider_status = models.CharField(max_length=64, blank=True, default="")
    fallback_latency_ms = models.PositiveIntegerField(null=True, blank=True)
    fallback_failure_type = models.CharField(max_length=64, blank=True, default="")
    fallback_safe_diagnostics = models.CharField(max_length=512, blank=True, default="")
    cancel_attempted = models.BooleanField(default=False)
    cancel_attempted_at = models.DateTimeField(null=True, blank=True)
    cancel_http_status = models.IntegerField(null=True, blank=True)
    cancel_confirmed_status = models.CharField(max_length=64, blank=True, default="")
    cancel_safe_diagnostics = models.CharField(max_length=512, blank=True, default="")
    prior_attempts = models.JSONField(default=list)
    transcription_text = models.TextField(null=True, blank=True)
    transcription_byte_length = models.PositiveIntegerField(null=True, blank=True)
    transcription_sha256 = models.CharField(max_length=64, blank=True, default="")
    create_call_count = models.PositiveSmallIntegerField(default=0)
    failure_code = models.CharField(max_length=64, blank=True, default="")
    failure_message = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    completed_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["band_index"]
        indexes = [
            models.Index(
                fields=["page_checkpoint", "status"],
                name="ar_pr_ocr_band_status_idx",
            ),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["page_checkpoint", "band_index"],
                name="uniq_ar_pr_ocr_page_band",
            ),
            models.CheckConstraint(
                condition=models.Q(band_index__gte=0),
                name="ar_pr_ocr_band_index_gte_0",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(rect_x__gte=0)
                    & models.Q(rect_y__gte=0)
                    & models.Q(rect_width__gte=1)
                    & models.Q(rect_height__gte=1)
                ),
                name="ar_pr_ocr_band_rect_valid",
            ),
            models.CheckConstraint(
                condition=models.Q(create_call_count__lte=2),
                name="ar_pr_ocr_band_create_count",
            ),
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "PENDING",
                        "PRIMARY_RUNNING",
                        "CANCEL_PENDING",
                        "FALLBACK_RUNNING",
                        "SUCCEEDED",
                        "FAILED",
                    ]
                ),
                name="ar_pr_ocr_band_status_valid",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="SUCCEEDED")
                    | (
                        models.Q(
                            selected_result__in=[
                                "UNASSISTED",
                                "ASSISTED_FALLBACK",
                                "CLOUD_VISION_LOW_QUALITY",
                            ]
                        )
                        & models.Q(transcription_text__isnull=False)
                        & ~models.Q(transcription_text="")
                        & models.Q(transcription_byte_length__isnull=False)
                        & ~models.Q(transcription_sha256="")
                        & models.Q(completed_at__isnull=False)
                        & models.Q(failure_code="")
                    )
                ),
                name="ar_pr_ocr_band_succeeded_shape",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(status="SUCCEEDED")
                    | (
                        models.Q(selected_result="")
                        & models.Q(transcription_text__isnull=True)
                        & models.Q(transcription_sha256="")
                        & models.Q(transcription_byte_length__isnull=True)
                    )
                ),
                name="ar_pr_ocr_band_nonsuccess_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="FAILED")
                    | (
                        models.Q(completed_at__isnull=False)
                        & ~models.Q(failure_code="")
                    )
                ),
                name="ar_pr_ocr_band_failed_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="CANCEL_PENDING")
                    | (
                        models.Q(cancel_attempted=True)
                        & models.Q(cancel_attempted_at__isnull=False)
                    )
                ),
                name="ar_pr_ocr_band_cancel_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="PRIMARY_RUNNING")
                    | models.Q(create_call_count=1)
                ),
                name="ar_pr_ocr_band_primary_count",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="FALLBACK_RUNNING")
                    | models.Q(create_call_count=2)
                ),
                name="ar_pr_ocr_band_fallback_count",
            ),
            models.CheckConstraint(
                condition=(
                    ~(
                        models.Q(status="SUCCEEDED")
                        & models.Q(selected_result="UNASSISTED")
                    )
                    | models.Q(create_call_count=1)
                ),
                name="ar_pr_ocr_band_unassisted_count",
            ),
            models.CheckConstraint(
                condition=(
                    ~(
                        models.Q(status="SUCCEEDED")
                        & models.Q(selected_result="ASSISTED_FALLBACK")
                    )
                    | models.Q(create_call_count=2)
                ),
                name="ar_pr_ocr_band_assisted_count",
            ),
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


class ArchiveItemPersonSuggestion(models.Model):
    """One proposed ADD or REMOVE of a Person relationship on an ArchiveItem.

    Identity is Person.id. This is an explicit relationship delta, not a
    desired set of Person ids. New-Person / alias / merge proposals are out
    of scope. Approval must not reconstruct a Person set or write PhotoPerson.
    """

    class Action(models.TextChoices):
        ADD = "ADD", "Add"
        REMOVE = "REMOVE", "Remove"

    class Status(models.TextChoices):
        PENDING = "PENDING", "Pending"
        APPROVED = "APPROVED", "Approved"
        REJECTED = "REJECTED", "Rejected"

    archive_item = models.ForeignKey(
        ArchiveItem,
        on_delete=models.CASCADE,
        related_name="person_suggestions",
    )
    person = models.ForeignKey(
        Person,
        on_delete=models.PROTECT,
        related_name="archive_item_person_suggestions",
    )
    action = models.CharField(max_length=16, choices=Action.choices)
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
        related_name="reviewed_archive_item_person_suggestions",
    )
    submitter_user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="submitted_archive_item_person_suggestions",
    )

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(
                fields=["archive_item", "person", "action"],
                condition=models.Q(status="PENDING"),
                name="uniq_pending_archive_item_person_suggestion",
            ),
        ]

    def __str__(self) -> str:
        return (
            f"ArchiveItemPersonSuggestion(id={self.id}, "
            f"archive_item_id={self.archive_item_id}, person_id={self.person_id}, "
            f"action={self.action}, status={self.status})"
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

    Geometry/hover must follow this binding after binding-freshness trust checks
    (see ``documents.services.transkribus_binding_freshness``), not
    document-level snapshot ownership alone.

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


class TranskribusParagraphMapping(models.Model):
    """
    Staff-authored paragraph presentation metadata for one Transkribus snapshot.

    Presentation only: never stores transcription text, char offsets, hover IDs,
    or geometry. A row with zero break rows is an explicit one-paragraph save.
    Absence of this row means paragraph grouping has never been saved.

    Cross-document snapshot mismatches are rejected in ``save()``. Do not use
    ``bulk_create`` for this model (bypasses that check).
    """

    snapshot = models.OneToOneField(
        TranskribusTranscriptSnapshot,
        on_delete=models.CASCADE,
        related_name="paragraph_mapping",
    )
    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="transkribus_paragraph_mappings",
    )
    copied_from = models.ForeignKey(
        "self",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="copied_to_mappings",
        help_text="Optional provenance when a manager later adopts a historical mapping.",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transkribus_paragraph_mappings_created",
    )
    updated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transkribus_paragraph_mappings_updated",
    )

    class Meta:
        indexes = [
            models.Index(
                fields=["document", "-updated_at"],
                name="tr_para_map_doc_upd_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        self._ensure_document_from_snapshot()
        self._validate_document_matches_snapshot()
        self._validate_copied_from()

    def _ensure_document_from_snapshot(self) -> None:
        if self.document_id or not self.snapshot_id:
            return
        self.document_id = self.snapshot.document_id

    def _validate_document_matches_snapshot(self) -> None:
        if not self.snapshot_id:
            return
        snapshot_document_id = self.snapshot.document_id
        if not self.document_id:
            self.document_id = snapshot_document_id
            return
        if self.document_id != snapshot_document_id:
            raise ValidationError(
                {
                    "document": (
                        "TranskribusParagraphMapping requires document to match "
                        "the snapshot's document."
                    )
                }
            )

    def _validate_copied_from(self) -> None:
        if not self.copied_from_id:
            return
        source = self.copied_from
        if source is None:
            raise ValidationError(
                {
                    "copied_from": (
                        "TranskribusParagraphMapping requires a valid mapping "
                        "when copied_from_id is set."
                    )
                }
            )
        if self.pk is not None and source.pk == self.pk:
            raise ValidationError(
                {"copied_from": "A paragraph mapping cannot copy from itself."}
            )
        if self.snapshot_id and source.snapshot_id == self.snapshot_id:
            raise ValidationError(
                {
                    "copied_from": (
                        "copied_from must refer to a mapping on a different snapshot."
                    )
                }
            )
        if self.document_id and source.document_id != self.document_id:
            raise ValidationError(
                {
                    "copied_from": (
                        "copied_from must belong to the same document as this mapping."
                    )
                }
            )

    def save(self, *args, **kwargs):
        # bulk_create bypasses save(); do not use it for this model.
        self._ensure_document_from_snapshot()
        self._validate_document_matches_snapshot()
        self._validate_copied_from()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"TranskribusParagraphMapping(id={self.pk}, snapshot={self.snapshot_id})"


class TranskribusParagraphBreak(models.Model):
    """
    Paragraph break after one contributing source line of the mapping's snapshot.

    Each row means: start a new paragraph after this contributing line.
    Page boundaries are independent of paragraph boundaries.

    Do not persist provider IDs, char offsets, or presentation hover IDs.
    Do not use ``bulk_create`` for this model (bypasses ``save()`` checks).
    """

    mapping = models.ForeignKey(
        TranskribusParagraphMapping,
        on_delete=models.CASCADE,
        related_name="breaks",
    )
    after_line = models.ForeignKey(
        TranskribusSnapshotLine,
        on_delete=models.CASCADE,
        related_name="paragraph_breaks_after",
    )

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["mapping", "after_line"],
                name="uniq_tr_para_break_after_line",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        self._validate_after_line()

    def _validate_after_line(self) -> None:
        if not self.mapping_id or not self.after_line_id:
            return
        mapping = self.mapping
        after_line = self.after_line
        if after_line.page.snapshot_id != mapping.snapshot_id:
            raise ValidationError(
                {
                    "after_line": (
                        "Paragraph break after_line must belong to the mapping's "
                        "snapshot."
                    )
                }
            )
        if not after_line.contributes_to_canonical:
            raise ValidationError(
                {
                    "after_line": (
                        "Paragraph break after_line must be a contributing source line."
                    )
                }
            )
        final_line_id = (
            TranskribusSnapshotLine.objects.filter(
                page__snapshot_id=mapping.snapshot_id,
                contributes_to_canonical=True,
            )
            .order_by("page__page_index", "order_index")
            .values_list("pk", flat=True)
            .last()
        )
        if final_line_id is not None and after_line.pk == final_line_id:
            raise ValidationError(
                {
                    "after_line": (
                        "A paragraph break after the final contributing source "
                        "line is not meaningful."
                    )
                }
            )

    def save(self, *args, **kwargs):
        # bulk_create bypasses save(); do not use it for this model.
        self._validate_after_line()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return (
            f"TranskribusParagraphBreak(mapping={self.mapping_id}, "
            f"after_line={self.after_line_id})"
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


class ProcessDocumentRequest(models.Model):
    """Durable lifecycle and payload contract for PROCESS_DOCUMENT."""

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        RECOVERY_REQUIRED = "RECOVERY_REQUIRED", "Recovery required"
        COMPLETED = "COMPLETED", "Completed"
        PARTIAL = "PARTIAL", "Partial"
        FAILED = "FAILED", "Failed"
        ENQUEUE_FAILED = "ENQUEUE_FAILED", "Enqueue failed"

    class Operation(models.TextChoices):
        OCR = "OCR", "OCR"
        HEBREW_TRANSLATION = "HEBREW_TRANSLATION", "Hebrew translation"

    class Origin(models.TextChoices):
        UPLOAD_FINALIZE = "UPLOAD_FINALIZE", "Upload finalize"
        OCR_REPROCESS = "OCR_REPROCESS", "OCR reprocess"
        HEBREW_TRANSLATION_RETRY = (
            "HEBREW_TRANSLATION_RETRY",
            "Hebrew translation retry",
        )

    class OcrRetryMode(models.TextChoices):
        NORMAL_REENQUEUE = "normal_reenqueue", "Normal re-enqueue"
        TRANSKRIBUS_RECOGNITION_ONLY = (
            "transkribus_recognition_only",
            "Transkribus recognition only",
        )

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="process_document_requests",
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="initiated_process_document_requests",
    )
    source_transkribus_run = models.ForeignKey(
        TranskribusRun,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="process_document_requests",
    )

    status = models.CharField(max_length=32, choices=Status.choices)
    operation = models.CharField(max_length=32, choices=Operation.choices)
    origin = models.CharField(max_length=32, choices=Origin.choices)
    ocr_retry_mode = models.CharField(
        max_length=48,
        choices=OcrRetryMode.choices,
        blank=True,
        default="",
    )

    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    failure_code = models.CharField(max_length=64, blank=True, default="")
    failure_message = models.CharField(max_length=512, blank=True, default="")

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_enqueued_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["document", "-created_at"],
                name="proc_req_doc_created_idx",
            ),
            models.Index(
                fields=["document", "status"],
                name="proc_req_doc_status_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "QUEUED",
                        "RUNNING",
                        "RECOVERY_REQUIRED",
                        "COMPLETED",
                        "PARTIAL",
                        "FAILED",
                        "ENQUEUE_FAILED",
                    ]
                ),
                name="proc_req_status_valid",
            ),
            models.UniqueConstraint(
                fields=["document"],
                condition=models.Q(
                    status__in=[
                        "QUEUED",
                        "RUNNING",
                        "RECOVERY_REQUIRED",
                        "ENQUEUE_FAILED",
                    ]
                ),
                name="uniq_process_req_active_doc",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=["QUEUED", "ENQUEUE_FAILED"])
                    | (
                        models.Q(lease_token__isnull=True)
                        & models.Q(lease_expires_at__isnull=True)
                        & models.Q(started_at__isnull=True)
                        & models.Q(completed_at__isnull=True)
                    )
                ),
                name="proc_req_queued_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="RUNNING")
                    | (
                        models.Q(lease_token__isnull=False)
                        & models.Q(lease_expires_at__isnull=False)
                        & models.Q(started_at__isnull=False)
                        & models.Q(completed_at__isnull=True)
                    )
                ),
                name="proc_req_running_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="RECOVERY_REQUIRED")
                    | (
                        models.Q(lease_token__isnull=False)
                        & models.Q(lease_expires_at__isnull=True)
                        & models.Q(started_at__isnull=False)
                        & models.Q(completed_at__isnull=True)
                    )
                ),
                name="proc_req_recovery_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=["COMPLETED", "PARTIAL", "FAILED"])
                    | (
                        models.Q(lease_token__isnull=True)
                        & models.Q(lease_expires_at__isnull=True)
                        & models.Q(completed_at__isnull=False)
                    )
                ),
                name="proc_req_terminal_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="COMPLETED")
                    | (models.Q(failure_code="") & models.Q(failure_message=""))
                ),
                name="proc_req_completed_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=["FAILED", "ENQUEUE_FAILED"])
                    | ~models.Q(failure_code="")
                ),
                name="proc_req_failure_shape",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        operation="OCR",
                        ocr_retry_mode__in=[
                            "normal_reenqueue",
                            "transkribus_recognition_only",
                        ],
                    )
                    | models.Q(
                        operation="HEBREW_TRANSLATION",
                        ocr_retry_mode="",
                        source_transkribus_run__isnull=True,
                    )
                ),
                name="proc_req_operation_payload",
            ),
            models.CheckConstraint(
                condition=(
                    (
                        models.Q(
                            ocr_retry_mode="transkribus_recognition_only",
                            source_transkribus_run__isnull=False,
                        )
                    )
                    | (
                        ~models.Q(ocr_retry_mode="transkribus_recognition_only")
                        & models.Q(source_transkribus_run__isnull=True)
                    )
                ),
                name="proc_req_source_run_shape",
            ),
            models.CheckConstraint(
                condition=(
                    models.Q(
                        origin="UPLOAD_FINALIZE",
                        operation="OCR",
                        ocr_retry_mode="normal_reenqueue",
                    )
                    | models.Q(
                        origin="OCR_REPROCESS",
                        operation="OCR",
                    )
                    | models.Q(
                        origin="HEBREW_TRANSLATION_RETRY",
                        operation="HEBREW_TRANSLATION",
                    )
                ),
                name="proc_req_origin_operation",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        source_transkribus_run = self.source_transkribus_run
        if (
            source_transkribus_run is not None
            and self.document_id
            and source_transkribus_run.document_id != self.document_id
        ):
            raise ValidationError(
                {
                    "source_transkribus_run": (
                        "ProcessDocumentRequest requires source_transkribus_run "
                        "to belong to the same document."
                    )
                }
            )

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is None or (
            {
                "document",
                "document_id",
                "source_transkribus_run",
                "source_transkribus_run_id",
            }
            & set(update_fields)
        ):
            self.clean()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return (
            f"ProcessDocumentRequest(id={self.pk}, "
            f"doc={self.document_id}, status={self.status})"
        )


class TranskribusCorrectedCurrentSyncRequest(models.Model):
    """
    Durable staff corrected/current sync queue request for one Document.

    Records enqueue/execution lifecycle and lease fencing for worker dispatch.
    Provider HTTP/S3 orchestration is performed by the worker via
    ``run_corrected_current_transkribus_sync`` under lease fencing — not here.
    """

    class Status(models.TextChoices):
        QUEUED = "QUEUED", "Queued"
        RUNNING = "RUNNING", "Running"
        RECOVERY_REQUIRED = "RECOVERY_REQUIRED", "Recovery required"
        COMPLETED = "COMPLETED", "Completed"
        REFUSED = "REFUSED", "Refused"
        FAILED = "FAILED", "Failed"
        ENQUEUE_FAILED = "ENQUEUE_FAILED", "Enqueue failed"

    document = models.ForeignKey(
        Document,
        on_delete=models.CASCADE,
        related_name="transkribus_corrected_current_sync_requests",
    )
    initiated_by = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="transkribus_corrected_current_sync_requests",
    )
    status = models.CharField(max_length=32, choices=Status.choices)
    attempt = models.OneToOneField(
        TranskribusCorrectedCurrentSyncAttempt,
        on_delete=models.RESTRICT,
        null=True,
        blank=True,
        related_name="corrected_current_sync_request",
        help_text=(
            "Linked sync attempt once worker correlation succeeds. RESTRICT "
            "preserves request provenance; delete the request (or its document) "
            "before deleting a referenced attempt."
        ),
    )
    lease_token = models.UUIDField(null=True, blank=True)
    lease_expires_at = models.DateTimeField(null=True, blank=True)
    failure_code = models.CharField(max_length=64, blank=True, default="")
    failure_message = models.CharField(max_length=512, blank=True, default="")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)
    started_at = models.DateTimeField(null=True, blank=True)
    completed_at = models.DateTimeField(null=True, blank=True)
    last_enqueued_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [
            models.Index(
                fields=["document", "-created_at"],
                name="tr_cc_sync_req_doc_created_idx",
            ),
            models.Index(
                fields=["document", "status"],
                name="tr_cc_sync_req_doc_status_idx",
            ),
        ]
        constraints = [
            models.CheckConstraint(
                condition=models.Q(
                    status__in=[
                        "QUEUED",
                        "RUNNING",
                        "RECOVERY_REQUIRED",
                        "COMPLETED",
                        "REFUSED",
                        "FAILED",
                        "ENQUEUE_FAILED",
                    ]
                ),
                name="tr_cc_sync_req_status_valid",
            ),
            models.UniqueConstraint(
                fields=["document"],
                condition=models.Q(
                    status__in=[
                        "QUEUED",
                        "RUNNING",
                        "RECOVERY_REQUIRED",
                        "ENQUEUE_FAILED",
                    ]
                ),
                name="uniq_tr_cc_sync_req_active_doc",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=["QUEUED", "ENQUEUE_FAILED"])
                    | (
                        models.Q(lease_token__isnull=True)
                        & models.Q(lease_expires_at__isnull=True)
                        & models.Q(started_at__isnull=True)
                        & models.Q(completed_at__isnull=True)
                        & models.Q(attempt__isnull=True)
                    )
                ),
                name="tr_cc_sync_req_queued_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="RUNNING")
                    | (
                        models.Q(lease_token__isnull=False)
                        & models.Q(lease_expires_at__isnull=False)
                        & models.Q(started_at__isnull=False)
                        & models.Q(completed_at__isnull=True)
                    )
                ),
                name="tr_cc_sync_req_running_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="RECOVERY_REQUIRED")
                    | (
                        models.Q(attempt__isnull=False)
                        & models.Q(lease_token__isnull=False)
                        & models.Q(lease_expires_at__isnull=True)
                        & models.Q(started_at__isnull=False)
                        & models.Q(completed_at__isnull=True)
                    )
                ),
                name="tr_cc_sync_req_recovery_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=["COMPLETED", "REFUSED"])
                    | (
                        models.Q(attempt__isnull=False)
                        & models.Q(completed_at__isnull=False)
                    )
                ),
                name="tr_cc_sync_req_success_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status="FAILED")
                    | (
                        models.Q(completed_at__isnull=False)
                        & ~models.Q(failure_code="")
                    )
                ),
                name="tr_cc_sync_req_failed_shape",
            ),
            models.CheckConstraint(
                condition=(
                    ~models.Q(status__in=["COMPLETED", "REFUSED", "FAILED"])
                    | (
                        models.Q(lease_token__isnull=True)
                        & models.Q(lease_expires_at__isnull=True)
                    )
                ),
                name="tr_cc_sync_req_terminal_no_lease",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        self._validate_attempt_document()

    def _validate_attempt_document(self) -> None:
        if not self.attempt_id or not self.document_id:
            return
        attempt = self.attempt
        if attempt is None:
            return
        if attempt.document_id != self.document_id:
            raise ValidationError(
                {
                    "attempt": (
                        "TranskribusCorrectedCurrentSyncRequest requires "
                        "attempt to belong to the same document."
                    )
                }
            )

    def save(self, *args, **kwargs):
        update_fields = kwargs.get("update_fields")
        if update_fields is None or (
            {"attempt", "document", "attempt_id", "document_id"} & set(update_fields)
        ):
            self._validate_attempt_document()
        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return (
            f"TranskribusCorrectedCurrentSyncRequest(id={self.pk}, "
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
