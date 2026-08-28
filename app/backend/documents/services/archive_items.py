"""ArchiveItem helpers for OCR-backed documents and manual text items."""

from __future__ import annotations

from typing import Any

from django.db import transaction
from django.utils.text import slugify

from documents.services.video_validation import VIDEO_TIME_UNSET

VIDEO_DISCOVERY_PARTIAL_ERROR = (
    "category_names, event_names, and tag_names must be supplied together "
    "or all omitted"
)

ARCHIVE_ITEM_SHARED_FIELD_NAMES = (
    "title",
    "visibility",
    "date_start",
    "date_end",
    "date_precision",
    "metadata_status",
)


def _apply_legacy_author_name(archive_item: Any, author_name: str) -> None:
    """Dual-write OCR/MANUAL_TEXT/VIDEO author_name onto ordered Author relations."""
    from documents.services.archive_item_authors import apply_legacy_author_name

    apply_legacy_author_name(archive_item, author_name)


def archive_item_field_values_from_archive_item(archive_item: Any) -> dict[str, Any]:
    """Build shared archival field values from an ArchiveItem (no inference)."""
    return {
        name: getattr(archive_item, name) for name in ARCHIVE_ITEM_SHARED_FIELD_NAMES
    }


def shared_archive_item_for_document(document: Any):
    """Return the read source for OCR_DOCUMENT shared archival fields (display only).

    During the ArchiveItem cutover, user-facing OCR document surfaces read
    ``title``, ``visibility``, ``metadata_status``, and date fields from the
    linked ``ArchiveItem``. ``Document`` remains the OCR/runtime source of truth.
    """
    return document.archive_item


def _split_ocr_document_create_kwargs(
    document_kwargs: dict,
) -> tuple[dict, dict, dict]:
    """Split create kwargs into shared, ArchiveItem-only, and Document runtime fields.

    ``author_name`` and ``source_title`` are ArchiveItem bibliographic metadata only.
    They are removed here so they never reach ``Document.objects.create``.
    """
    runtime_kwargs = dict(document_kwargs)
    shared_kwargs = {}
    for name in ARCHIVE_ITEM_SHARED_FIELD_NAMES:
        if name in runtime_kwargs:
            shared_kwargs[name] = runtime_kwargs.pop(name)

    source_metadata_kwargs = {
        "author_name": runtime_kwargs.pop("author_name", ""),
        "source_title": runtime_kwargs.pop("source_title", ""),
        "public_note": runtime_kwargs.pop("public_note", ""),
    }

    return shared_kwargs, source_metadata_kwargs, runtime_kwargs


@transaction.atomic
def create_ocr_document(**document_kwargs: Any):
    """
    Create an OCR-backed Document with a linked ArchiveItem (item_type=OCR_DOCUMENT).

    ArchiveItem is canonical at create for the six shared archival fields.
    Document remains the OCR/runtime source of truth for processing-specific fields.
    """
    from documents.models import ArchiveItem, Document

    shared_kwargs, source_metadata_kwargs, runtime_kwargs = (
        _split_ocr_document_create_kwargs(document_kwargs)
    )

    pending_item = ArchiveItem(
        item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        **shared_kwargs,
    )
    archive_values = archive_item_field_values_from_archive_item(pending_item)
    archive_item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        **archive_values,
        author_name=source_metadata_kwargs["author_name"],
        source_title=source_metadata_kwargs["source_title"],
        public_note=source_metadata_kwargs["public_note"],
    )
    _apply_legacy_author_name(archive_item, source_metadata_kwargs["author_name"])
    document = Document.objects.create(
        archive_item=archive_item,
        **runtime_kwargs,
    )
    from documents.services.archive_search_index import sync_archive_item_search_index

    sync_archive_item_search_index(archive_item.pk)
    return document


@transaction.atomic
def create_manual_text_archive_item(
    *,
    title: str,
    body: str,
    visibility: str | None = None,
    date_start=None,
    date_end=None,
    date_precision: str | None = None,
    metadata_status: str | None = None,
    author_name: str = "",
    source_title: str = "",
    public_note: str = "",
):
    """
    Create a MANUAL_TEXT ArchiveItem with linked ManualTextContent.

    Does not create Document rows or enqueue processing.
    """
    from documents.models import ArchiveItem, ManualTextContent

    archive_item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        title=title,
        visibility=visibility or ArchiveItem.Visibility.PRIVATE,
        date_start=date_start,
        date_end=date_end,
        date_precision=date_precision or ArchiveItem.DatePrecision.UNKNOWN,
        metadata_status=metadata_status or ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
        author_name=author_name,
        source_title=source_title,
        public_note=public_note,
    )
    _apply_legacy_author_name(archive_item, author_name)
    ManualTextContent.objects.create(archive_item=archive_item, body=body)
    from documents.services.archive_search_index import sync_archive_item_search_index

    sync_archive_item_search_index(archive_item.pk)
    return archive_item


@transaction.atomic
def update_manual_text_archive_item(
    archive_item,
    *,
    title: str,
    body: str,
    visibility: str,
    date_start=None,
    date_end=None,
    date_precision: str,
    metadata_status: str,
    author_name: str = "",
    source_title: str = "",
    public_note: str = "",
):
    """
    Update a MANUAL_TEXT ArchiveItem and its ManualTextContent body.

    Does not create Document rows or enqueue processing.
    """
    from documents.models import ArchiveItem

    if archive_item.item_type != ArchiveItem.ItemType.MANUAL_TEXT:
        raise ValueError("archive item is not MANUAL_TEXT")

    archive_item.title = title
    archive_item.visibility = visibility
    archive_item.date_start = date_start
    archive_item.date_end = date_end
    archive_item.date_precision = date_precision
    archive_item.metadata_status = metadata_status
    archive_item.author_name = author_name
    archive_item.source_title = source_title
    archive_item.public_note = public_note
    archive_item.save()
    _apply_legacy_author_name(archive_item, author_name)

    content = archive_item.manual_text_content
    content.body = body
    content.save(update_fields=["body", "updated_at"])
    from documents.services.archive_search_index import sync_archive_item_search_index

    sync_archive_item_search_index(archive_item.pk)
    return archive_item


def _video_discovery_names_or_none(
    *,
    category_names: list[str] | None,
    event_names: list[str] | None,
    tag_names: list[str] | None,
) -> dict[str, list[str]] | None:
    """Return discovery names when all three are supplied, else ``None``.

    Matches the replace-all contract of ``update_archive_item_discovery_metadata``:
    callers must supply categories, events, and tags together, or omit all three
    to leave discovery unchanged. Partial updates are rejected before any write.
    """
    provided = (
        category_names is not None,
        event_names is not None,
        tag_names is not None,
    )
    if not any(provided):
        return None
    if not all(provided):
        raise ValueError(VIDEO_DISCOVERY_PARTIAL_ERROR)
    return {
        "category_names": list(category_names or []),
        "event_names": list(event_names or []),
        "tag_names": list(tag_names or []),
    }


@transaction.atomic
def create_video_archive_item(
    *,
    title: str,
    source_url: str,
    visibility: str | None = None,
    date_start=None,
    date_end=None,
    date_precision: str | None = None,
    metadata_status: str | None = None,
    author_name: str = "",
    source_title: str = "",
    public_note: str = "",
    start_seconds=VIDEO_TIME_UNSET,
    end_seconds=VIDEO_TIME_UNSET,
    category_names: list[str] | None = None,
    event_names: list[str] | None = None,
    tag_names: list[str] | None = None,
    user=None,
):
    """
    Create a VIDEO ArchiveItem with linked VideoContent.

    Provider fields are derived server-side from ``source_url``. Does not create
    Document rows, enqueue processing, or store media bytes.

    Discovery metadata follows ``update_archive_item_discovery_metadata``: supply
    ``category_names``, ``event_names``, and ``tag_names`` together, or omit all
    three.
    """
    from documents.models import ArchiveItem, VideoContent
    from documents.services.video_validation import (
        parse_video_content_from_source_url,
        validate_video_archive_item_metadata,
    )

    discovery = _video_discovery_names_or_none(
        category_names=category_names,
        event_names=event_names,
        tag_names=tag_names,
    )
    resolved_visibility = visibility or ArchiveItem.Visibility.PRIVATE
    resolved_date_precision = date_precision or ArchiveItem.DatePrecision.UNKNOWN
    resolved_metadata_status = (
        metadata_status or ArchiveItem.MetadataStatus.NEEDS_COMPLETION
    )
    validate_video_archive_item_metadata(
        title=title,
        visibility=resolved_visibility,
        metadata_status=resolved_metadata_status,
        date_precision=resolved_date_precision,
        date_start=date_start,
        date_end=date_end,
        author_name=author_name,
        source_title=source_title,
        user=user,
    )
    video_fields = parse_video_content_from_source_url(
        source_url,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )

    archive_item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.VIDEO,
        title=title,
        visibility=resolved_visibility,
        date_start=date_start,
        date_end=date_end,
        date_precision=resolved_date_precision,
        metadata_status=resolved_metadata_status,
        author_name=author_name,
        source_title=source_title,
        public_note=public_note,
    )
    _apply_legacy_author_name(archive_item, author_name)
    content = VideoContent(
        archive_item=archive_item,
        **video_fields,
    )
    content.full_clean()
    content.save()

    from documents.services.archive_search_index import sync_archive_item_search_index

    if discovery is not None:
        update_archive_item_discovery_metadata(archive_item, **discovery)
    else:
        sync_archive_item_search_index(archive_item.pk)
    return archive_item


@transaction.atomic
def update_video_archive_item(
    archive_item,
    *,
    title: str,
    source_url: str,
    visibility: str,
    date_start=None,
    date_end=None,
    date_precision: str,
    metadata_status: str,
    author_name: str = "",
    source_title: str = "",
    public_note: str = "",
    start_seconds=VIDEO_TIME_UNSET,
    end_seconds=VIDEO_TIME_UNSET,
    category_names: list[str] | None = None,
    event_names: list[str] | None = None,
    tag_names: list[str] | None = None,
    user=None,
):
    """
    Update a VIDEO ArchiveItem and recompute VideoContent from ``source_url``.

    Changing ``source_url`` fully recomputes provider, presentation mode,
    provider_video_id, and start/end seconds so stale YouTube values cannot remain.

    Discovery metadata follows ``update_archive_item_discovery_metadata``: supply
    ``category_names``, ``event_names``, and ``tag_names`` together, or omit all
    three to preserve existing discovery relations.
    """
    from documents.models import ArchiveItem
    from documents.services.video_validation import (
        parse_video_content_from_source_url,
        validate_video_archive_item_metadata,
    )

    if archive_item.item_type != ArchiveItem.ItemType.VIDEO:
        raise ValueError("archive item is not VIDEO")

    discovery = _video_discovery_names_or_none(
        category_names=category_names,
        event_names=event_names,
        tag_names=tag_names,
    )
    validate_video_archive_item_metadata(
        title=title,
        visibility=visibility,
        metadata_status=metadata_status,
        date_precision=date_precision,
        date_start=date_start,
        date_end=date_end,
        author_name=author_name,
        source_title=source_title,
        user=user,
    )
    video_fields = parse_video_content_from_source_url(
        source_url,
        start_seconds=start_seconds,
        end_seconds=end_seconds,
    )

    archive_item.title = title
    archive_item.visibility = visibility
    archive_item.date_start = date_start
    archive_item.date_end = date_end
    archive_item.date_precision = date_precision
    archive_item.metadata_status = metadata_status
    archive_item.author_name = author_name
    archive_item.source_title = source_title
    archive_item.public_note = public_note
    archive_item.save()
    _apply_legacy_author_name(archive_item, author_name)

    content = archive_item.video_content
    content.source_url = video_fields["source_url"]
    content.provider = video_fields["provider"]
    content.presentation_mode = video_fields["presentation_mode"]
    content.provider_video_id = video_fields["provider_video_id"]
    content.start_seconds = video_fields["start_seconds"]
    content.end_seconds = video_fields["end_seconds"]
    content.full_clean()
    content.save(
        update_fields=[
            "source_url",
            "provider",
            "presentation_mode",
            "provider_video_id",
            "start_seconds",
            "end_seconds",
            "updated_at",
        ]
    )

    from documents.services.archive_search_index import sync_archive_item_search_index

    if discovery is not None:
        update_archive_item_discovery_metadata(archive_item, **discovery)
    else:
        sync_archive_item_search_index(archive_item.pk)
    return archive_item


@transaction.atomic
def update_photo_archive_item_metadata(
    archive_item,
    *,
    title: str,
    visibility: str,
    date_start=None,
    date_end=None,
    date_precision: str,
    metadata_status: str,
    public_note: str = "",
):
    """
    Update shared ArchiveItem metadata for a PHOTO item.

    Does not modify PhotoContent rows, file fields, Person relations, create
    Document rows, or enqueue processing. Per-photo fields are edited through
    ``update_photo_content_metadata``.
    """
    from documents.models import ArchiveItem

    if archive_item.item_type != ArchiveItem.ItemType.PHOTO:
        raise ValueError("archive item is not PHOTO")

    archive_item.title = title
    archive_item.visibility = visibility
    archive_item.date_start = date_start
    archive_item.date_end = date_end
    archive_item.date_precision = date_precision
    archive_item.metadata_status = metadata_status
    archive_item.public_note = public_note
    archive_item.save(
        update_fields=[
            *ARCHIVE_ITEM_SHARED_FIELD_NAMES,
            "public_note",
            "updated_at",
        ]
    )
    from documents.services.archive_search_index import sync_archive_item_search_index

    sync_archive_item_search_index(archive_item.pk)
    return archive_item


@transaction.atomic
def update_ocr_document_metadata(
    document,
    *,
    title: str,
    visibility: str,
    date_start=None,
    date_end=None,
    date_precision: str,
    metadata_status: str,
    author_name: str = "",
    source_title: str = "",
    public_note: str = "",
):
    """
    Update shared archival metadata on an OCR-backed Document.

    ArchiveItem is canonical for the six shared archival fields. Document remains
    OCR/runtime source of truth for processing-specific fields.
    """
    from documents.models import ArchiveItem

    if document.archive_item.item_type != ArchiveItem.ItemType.OCR_DOCUMENT:
        raise ValueError("document is not linked to an OCR_DOCUMENT archive item")

    archive_item = document.archive_item
    archive_item.title = title
    archive_item.visibility = visibility
    archive_item.date_start = date_start
    archive_item.date_end = date_end
    archive_item.date_precision = date_precision
    archive_item.metadata_status = metadata_status
    archive_item.author_name = author_name
    archive_item.source_title = source_title
    archive_item.public_note = public_note
    archive_item.save(
        update_fields=[
            *ARCHIVE_ITEM_SHARED_FIELD_NAMES,
            "author_name",
            "source_title",
            "public_note",
            "updated_at",
        ]
    )
    _apply_legacy_author_name(archive_item, author_name)
    from documents.services.archive_search_index import sync_archive_item_search_index

    sync_archive_item_search_index(archive_item.pk)
    return document


_CATEGORY_EVENT_UNCHANGED = object()


@transaction.atomic
def update_ocr_document_catalog_metadata(
    document,
    *,
    donor: str,
    collection: str,
    original_location: str,
    notes: str,
    category_event: str | None | object = _CATEGORY_EVENT_UNCHANGED,
):
    """
    Update OCR catalog scalar metadata on Document and DocumentMetadata.

    Document remains OCR runtime source of truth. Does not sync ArchiveItem.
    """
    from documents.models import ArchiveItem, DocumentMetadata

    if document.archive_item.item_type != ArchiveItem.ItemType.OCR_DOCUMENT:
        raise ValueError("document is not linked to an OCR_DOCUMENT archive item")

    update_fields = ["updated_at"]
    if category_event is not _CATEGORY_EVENT_UNCHANGED:
        document.category_event = category_event
        update_fields.insert(0, "category_event")
    document.save(update_fields=update_fields)

    DocumentMetadata.objects.update_or_create(
        document=document,
        defaults={
            "donor": donor,
            "collection": collection,
            "original_location": original_location,
            "notes": notes,
        },
    )
    return document


@transaction.atomic
def update_ocr_document_tags(
    document,
    *,
    tag_names: list[str],
):
    """
    Replace all tags on an OCR-backed Document.

    Document remains OCR runtime source of truth. Does not sync ArchiveItem.
    Unused Tag rows are left in the database. Frozen historical person-name
    Tag ids cannot be reused; retired historical Tag names cannot be
    recreated. Both fail closed before any relation write.
    """
    from documents.models import ArchiveItem
    from documents.services.archive_discovery_metadata_validation import (
        HISTORICAL_PERSON_TAG_REUSE_ERROR,
        historical_person_tag_name_write_errors,
        historical_person_tag_reuse_errors,
    )

    if document.archive_item.item_type != ArchiveItem.ItemType.OCR_DOCUMENT:
        raise ValueError("document is not linked to an OCR_DOCUMENT archive item")

    reuse_errors = historical_person_tag_name_write_errors(tag_names)
    if reuse_errors:
        raise ValueError(reuse_errors[0])

    tag_objs = []
    for name in tag_names:
        tag_obj, _ = _get_or_create_tag_by_name(name)
        tag_objs.append(tag_obj)
    reuse_errors = historical_person_tag_reuse_errors(tag.pk for tag in tag_objs)
    if reuse_errors:
        raise ValueError(HISTORICAL_PERSON_TAG_REUSE_ERROR)
    document.tags_m2m.set(tag_objs)
    return document


_SLUG_MAX_LENGTH = 255
_SLUG_FALLBACK_BASE = "item"


def _slug_base_from_name(name: str) -> str:
    slug = slugify(name, allow_unicode=True)
    if not slug:
        slug = _SLUG_FALLBACK_BASE
    return slug[:_SLUG_MAX_LENGTH]


def _unique_slug_for_model(model, name: str) -> str:
    base = _slug_base_from_name(name)
    slug = base
    counter = 2
    while model.objects.filter(slug=slug).exists():
        suffix = f"-{counter}"
        max_base_len = _SLUG_MAX_LENGTH - len(suffix)
        slug = base[:max_base_len] + suffix
        counter += 1
    return slug


def get_or_create_archive_category_by_name(name: str):
    """Return an ArchiveCategory by exact name, creating one with a unique slug if needed."""
    from documents.models import ArchiveCategory

    try:
        return ArchiveCategory.objects.get(name=name), False
    except ArchiveCategory.DoesNotExist:
        slug = _unique_slug_for_model(ArchiveCategory, name)
        return ArchiveCategory.objects.create(name=name, slug=slug), True


def _get_or_create_archive_event_by_name(name: str):
    from documents.models import ArchiveEvent

    try:
        return ArchiveEvent.objects.get(name=name), False
    except ArchiveEvent.DoesNotExist:
        slug = _unique_slug_for_model(ArchiveEvent, name)
        return ArchiveEvent.objects.create(name=name, slug=slug), True


def _get_or_create_tag_by_name(name: str):
    from documents.models import Tag
    from documents.services.archive_discovery_metadata_validation import (
        HISTORICAL_PERSON_TAG_REUSE_ERROR,
        retired_historical_person_tag_name_errors,
    )

    if retired_historical_person_tag_name_errors([name]):
        raise ValueError(HISTORICAL_PERSON_TAG_REUSE_ERROR)
    return Tag.objects.get_or_create(name=name)


def discovery_metadata_form_data_from_item(archive_item) -> dict:
    """Build discovery metadata form values from an ArchiveItem (selected IDs, empty new-text)."""
    from documents.historical_person_tag_map import historical_person_name_tag_ids
    from documents.services.archive_discovery_metadata_validation import (
        empty_discovery_metadata_form_fields,
    )

    return {
        **empty_discovery_metadata_form_fields(),
        "selected_category_ids": list(
            archive_item.categories.order_by("name").values_list("id", flat=True)
        ),
        "selected_event_ids": list(
            archive_item.events.order_by("name").values_list("id", flat=True)
        ),
        "selected_tag_ids": list(
            archive_item.tags.exclude(pk__in=historical_person_name_tag_ids())
            .order_by("name")
            .values_list("id", flat=True)
        ),
    }


@transaction.atomic
def update_archive_item_discovery_metadata(
    archive_item,
    *,
    category_names: list[str],
    event_names: list[str],
    tag_names: list[str],
):
    """Replace ArchiveItem discovery categories, events, and tags (replace-all per relation).

    Historical person-name Tags are not part of the desired-set contract.
    Existing blocked Tag relations are merged back before ``tags.set`` so
    ordinary edits cannot drop them. New blocked Tag ids and retired
    historical Tag names fail closed before any relation write.
    """
    from documents.historical_person_tag_map import (
        historical_person_name_tag_ids,
        is_historical_person_name_tag,
    )
    from documents.services.archive_discovery_metadata_validation import (
        HISTORICAL_PERSON_TAG_REUSE_ERROR,
        historical_person_tag_name_write_errors,
    )

    reuse_errors = historical_person_tag_name_write_errors(tag_names)
    if reuse_errors:
        raise ValueError(reuse_errors[0])

    category_objs = []
    for name in category_names:
        category_obj, _ = get_or_create_archive_category_by_name(name)
        category_objs.append(category_obj)

    event_objs = []
    for name in event_names:
        event_obj, _ = _get_or_create_archive_event_by_name(name)
        event_objs.append(event_obj)

    tag_objs = []
    for name in tag_names:
        tag_obj, _ = _get_or_create_tag_by_name(name)
        tag_objs.append(tag_obj)

    existing_blocked = list(
        archive_item.tags.filter(pk__in=historical_person_name_tag_ids())
    )
    existing_blocked_ids = {tag.pk for tag in existing_blocked}
    if any(
        is_historical_person_name_tag(tag.pk) and tag.pk not in existing_blocked_ids
        for tag in tag_objs
    ):
        raise ValueError(HISTORICAL_PERSON_TAG_REUSE_ERROR)

    allowed_tags = [
        tag for tag in tag_objs if not is_historical_person_name_tag(tag.pk)
    ]
    tag_objs = allowed_tags + existing_blocked

    archive_item.categories.set(category_objs)
    archive_item.events.set(event_objs)
    archive_item.tags.set(tag_objs)
    from documents.services.archive_search_index import sync_archive_item_search_index

    sync_archive_item_search_index(archive_item.pk)
    return archive_item
