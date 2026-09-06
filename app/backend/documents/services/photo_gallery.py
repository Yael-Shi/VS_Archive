"""Public PHOTO detail gallery: renderable photos, selection, and presentation."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from urllib.parse import urlencode

from django.db.models import Prefetch, QuerySet
from django.urls import reverse

from documents.models import ArchiveItem, PhotoContent, PhotoPerson
from documents.services.archive_item_presentation import (
    ArchiveBrowseCard,
    archive_item_author_links_prefetch,
    build_archive_browse_card,
    person_public_page_url,
)
from documents.services.archive_metadata_validation import meaningful_metadata_value
from documents.services.document_date import NO_DATE_LABEL, format_document_date
from documents.services.photo_archive_urls import presign_photo_thumbnail_url
from documents.services.photo_presentation import (
    filter_archive_renderable_photo_contents,
    photo_is_archive_renderable,
)

PUBLIC_PHOTO_QUERY_PARAM = "photo"


@dataclass(frozen=True, slots=True)
class PublicPhotoGalleryItem:
    """One publicly selectable photo in an ArchiveItem gallery."""

    photo: PhotoContent
    display_index: int
    thumbnail_url: str | None
    selection_url: str
    is_selected: bool
    selector_label: str
    alt_text: str


@dataclass(frozen=True, slots=True)
class PublicIdentifiedPersonLink:
    """Canonical PhotoPerson display name plus public Person page href."""

    name: str
    href: str


@dataclass(frozen=True, slots=True)
class PublicPhotoGallery:
    """Server-rendered public presentation for one PHOTO ArchiveItem."""

    selected: PhotoContent
    selected_index: int
    photo_count: int
    show_gallery: bool
    previous_url: str | None
    next_url: str | None
    status_label: str
    selected_alt_text: str
    identified_people: list[PublicIdentifiedPersonLink]
    identified_people_names: list[str]
    photo_date_label: str
    selector_items: list[PublicPhotoGalleryItem]


def parse_public_photo_selector(raw_value: str | None) -> int | None:
    """Parse ``?photo=`` as a PhotoContent id. Invalid values yield ``None``."""
    if raw_value is None:
        return None
    text = str(raw_value).strip()
    if not text:
        return None
    try:
        photo_id = int(text)
    except (TypeError, ValueError):
        return None
    if photo_id < 1:
        return None
    return photo_id


def public_photo_detail_url(archive_item_id: int, photo_id: int | None = None) -> str:
    """Canonical ArchiveItem detail URL, optionally selecting one photo."""
    url = reverse("archive-detail", kwargs={"item_id": archive_item_id})
    if photo_id is None:
        return url
    return f"{url}?{urlencode({PUBLIC_PHOTO_QUERY_PARAM: photo_id})}"


def public_renderable_photo_contents(archive_item: ArchiveItem) -> list[PhotoContent]:
    """Uploaded PhotoContent rows for public display, in ``(position, id)`` order."""
    photos = list(archive_item.photo_contents.all())
    photos.sort(key=lambda photo: (photo.position, photo.pk))
    return [photo for photo in photos if photo_is_archive_renderable(photo)]


def identified_people_links(
    photo_content: PhotoContent,
) -> list[PublicIdentifiedPersonLink]:
    """Stable public PhotoPerson links for this photo only.

    Identity is ``Person.id``. Duplicate canonical names stay distinct.
    Aliases are not read. Empty/placeholder names are omitted.
    """
    people = list(photo_content.people.all())
    people.sort(key=lambda person: (person.name, person.pk))
    links: list[PublicIdentifiedPersonLink] = []
    for person in people:
        name = meaningful_metadata_value(person.name)
        if name:
            links.append(
                PublicIdentifiedPersonLink(
                    name=name,
                    href=person_public_page_url(person.pk),
                )
            )
    return links


def identified_people_display_names(photo_content: PhotoContent) -> list[str]:
    """Stable public names for people identified on this photo only."""
    return [link.name for link in identified_people_links(photo_content)]


def public_photo_date_label(photo_content: PhotoContent) -> str:
    """Per-photo date using archive date formatting; empty when unknown/unuseful."""
    label = format_document_date(photo_content)
    if not label or label == NO_DATE_LABEL:
        return ""
    return label


def public_photo_alt_text(
    photo_content: PhotoContent,
    *,
    item_title: str,
    display_index: int,
    total: int,
) -> str:
    """Image alt from description or title; never includes internal ids."""
    description = meaningful_metadata_value(photo_content.description)
    if description:
        return description
    title = (item_title or "").strip()
    if total > 1:
        status = f"תמונה {display_index} מתוך {total}"
        if title:
            return f"{title} — {status}"
        return status
    return title or "תמונה"


def _select_public_gallery_photo(
    photos: list[PhotoContent],
    selected_photo_id: int | None,
) -> PhotoContent:
    if selected_photo_id is not None:
        for photo in photos:
            if photo.pk == selected_photo_id:
                return photo
    return photos[0]


def build_public_photo_gallery(
    archive_item: ArchiveItem,
    *,
    selected_photo_param: str | None,
    bucket: str,
    expires_in: int = 3600,
) -> PublicPhotoGallery | None:
    """Build the public PHOTO detail gallery, or ``None`` when nothing is renderable.

    Invalid, foreign, or non-renderable ``?photo=`` values fall back to the first
    renderable photo. Selection never loads photos from another ArchiveItem.
    """
    if archive_item.item_type != ArchiveItem.ItemType.PHOTO:
        return None

    photos = public_renderable_photo_contents(archive_item)
    if not photos:
        return None

    selected = _select_public_gallery_photo(
        photos,
        parse_public_photo_selector(selected_photo_param),
    )
    selected_index = next(
        index for index, photo in enumerate(photos, start=1) if photo.pk == selected.pk
    )
    total = len(photos)
    show_gallery = total > 1
    previous_url = None
    next_url = None
    if show_gallery and selected_index > 1:
        previous_url = public_photo_detail_url(
            archive_item.pk,
            photos[selected_index - 2].pk,
        )
    if show_gallery and selected_index < total:
        next_url = public_photo_detail_url(
            archive_item.pk,
            photos[selected_index].pk,
        )

    selector_items: list[PublicPhotoGalleryItem] = []
    if show_gallery:
        for display_index, photo in enumerate(photos, start=1):
            selector_label = f"תמונה {display_index} מתוך {total}"
            if photo.pk == selected.pk:
                selector_label = f"{selector_label}, מוצגת כעת"
            selector_items.append(
                PublicPhotoGalleryItem(
                    photo=photo,
                    display_index=display_index,
                    thumbnail_url=presign_photo_thumbnail_url(
                        photo,
                        bucket=bucket,
                        expires_in=expires_in,
                    ),
                    selection_url=public_photo_detail_url(archive_item.pk, photo.pk),
                    is_selected=photo.pk == selected.pk,
                    selector_label=selector_label,
                    alt_text=public_photo_alt_text(
                        photo,
                        item_title=archive_item.title,
                        display_index=display_index,
                        total=total,
                    ),
                )
            )

    identified_people = identified_people_links(selected)
    return PublicPhotoGallery(
        selected=selected,
        selected_index=selected_index,
        photo_count=total,
        show_gallery=show_gallery,
        previous_url=previous_url,
        next_url=next_url,
        status_label=f"{selected_index} מתוך {total}",
        selected_alt_text=public_photo_alt_text(
            selected,
            item_title=archive_item.title,
            display_index=selected_index,
            total=total,
        ),
        identified_people=identified_people,
        identified_people_names=[link.name for link in identified_people],
        photo_date_label=public_photo_date_label(selected),
        selector_items=selector_items,
    )


def photo_person_appearances_queryset(
    *,
    person_id: int,
    authorized_archive_items: QuerySet[ArchiveItem],
) -> QuerySet[PhotoPerson]:
    """Authorized, publicly renderable PhotoPerson appearances for one Person.

    Does not read or create ArchiveItemPerson. Owning ArchiveItems must already
    be in ``authorized_archive_items`` (visibility plus item-level browse
    renderability). Each appearance PhotoContent must pass
    ``filter_archive_renderable_photo_contents`` (same contract as
    ``photo_is_archive_renderable``). Order is owning item ``-created_at``,
    item id, then photo ``(position, id)``.
    """
    renderable_photos = filter_archive_renderable_photo_contents(
        PhotoContent.objects.all()
    )
    return (
        PhotoPerson.objects.filter(
            person_id=person_id,
            photo_content__in=renderable_photos,
            photo_content__archive_item_id__in=authorized_archive_items.values("pk"),
        )
        .select_related(
            "photo_content",
            "photo_content__archive_item",
            "photo_content__archive_item__manual_text_content",
            "photo_content__archive_item__ocr_document",
            "photo_content__archive_item__video_content",
        )
        .prefetch_related(
            Prefetch(
                "photo_content__archive_item__photo_contents",
                queryset=PhotoContent.objects.order_by("position", "id"),
            ),
            "photo_content__archive_item__categories",
            "photo_content__archive_item__events",
            "photo_content__archive_item__tags",
            "photo_content__archive_item__people",
            archive_item_author_links_prefetch(
                lookup="photo_content__archive_item__author_links"
            ),
        )
        .order_by(
            "-photo_content__archive_item__created_at",
            "photo_content__archive_item_id",
            "photo_content__position",
            "photo_content_id",
        )
    )


def build_photo_person_appearance_cards(
    appearances: Sequence[PhotoPerson],
    *,
    bucket: str,
    expires_in: int = 3600,
) -> list[ArchiveBrowseCard]:
    """Browse-style cards for PhotoPerson appearances, linking ``?photo=``.

    Reuses ArchiveItem card title/meta. Thumbnail comes from the appearance
    PhotoContent when a thumbnail key exists, not from the item primary photo.
    """
    cards: list[ArchiveBrowseCard] = []
    for appearance in appearances:
        photo = appearance.photo_content
        item = photo.archive_item
        card = build_archive_browse_card(item)
        card = replace(
            card,
            detail_url=public_photo_detail_url(item.pk, photo.pk),
            thumbnail_url=presign_photo_thumbnail_url(
                photo,
                bucket=bucket,
                expires_in=expires_in,
            ),
        )
        cards.append(card)
    return cards
