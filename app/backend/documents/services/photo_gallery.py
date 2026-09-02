"""Public PHOTO detail gallery: renderable photos, selection, and presentation."""

from __future__ import annotations

from dataclasses import dataclass
from urllib.parse import urlencode

from django.urls import reverse

from documents.models import ArchiveItem, PhotoContent
from documents.services.archive_item_presentation import person_public_page_url
from documents.services.archive_metadata_validation import meaningful_metadata_value
from documents.services.document_date import NO_DATE_LABEL, format_document_date
from documents.services.photo_archive_urls import presign_photo_thumbnail_url
from documents.services.photo_presentation import photo_is_archive_renderable

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
