"""Public Person catalog and unified Person-detail ArchiveItem relations."""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace

from django.db.models import Exists, OuterRef, Q, QuerySet, Subquery

from documents.models import (
    ArchiveItem,
    ArchiveItemPerson,
    Person,
    PhotoContent,
    PhotoPerson,
)
from documents.services.archive_item_access import archive_browse_queryset_for_user
from documents.services.archive_item_presentation import (
    ArchiveBrowseCard,
    build_archive_browse_cards,
    person_public_page_url,
)
from documents.services.document_archive_urls import (
    apply_document_thumbnail_urls_to_browse_cards,
)
from documents.services.person_search import person_canonical_or_alias_icontains_q
from documents.services.photo_archive_urls import (
    apply_photo_thumbnail_urls_to_browse_cards,
    presign_photo_thumbnail_url,
)
from documents.services.photo_gallery import public_photo_detail_url
from documents.services.photo_presentation import (
    filter_archive_renderable_photo_contents,
)

PERSON_FIRST_MATCHING_PHOTO_ANNOTATION = "person_first_matching_photo_id"


@dataclass(frozen=True, slots=True)
class PublicPersonIndexRow:
    """One public People-index row (no Person id; aliases are not included)."""

    name: str
    href: str
    item_count: int


def renderable_photo_contents_queryset() -> QuerySet[PhotoContent]:
    """PhotoContent rows that pass the public gallery renderability contract."""
    return filter_archive_renderable_photo_contents(PhotoContent.objects.all())


def authorized_browse_item_pks(user) -> QuerySet:
    """Authorized + browse-renderable ArchiveItem primary keys for ``user``."""
    return archive_browse_queryset_for_user(user).order_by().values("pk")


def person_public_membership_q_for_item_pks(item_pks: QuerySet) -> Q:
    """Person rows with AIP or renderable PhotoPerson on the given ArchiveItem pks.

    ``item_pks`` must already be authorized + browse-renderable. Does not
    infer ArchiveItemPerson from PhotoPerson. ``people_present`` is ignored.
    """
    renderable_photos = renderable_photo_contents_queryset()
    aip_exists = Exists(
        ArchiveItemPerson.objects.filter(
            person_id=OuterRef("pk"),
            archive_item_id__in=item_pks,
        )
    )
    pp_exists = Exists(
        PhotoPerson.objects.filter(
            person_id=OuterRef("pk"),
            photo_content__in=renderable_photos,
            photo_content__archive_item_id__in=item_pks,
        )
    )
    return aip_exists | pp_exists


def person_public_membership_q(user) -> Q:
    """Person rows with at least one authorized+renderable AIP or PhotoPerson item."""
    return person_public_membership_q_for_item_pks(authorized_browse_item_pks(user))


def public_people_queryset(user, *, search_query: str = "") -> QuerySet[Person]:
    """Public People index queryset: membership, optional name/alias q, name then id."""
    people = Person.objects.filter(person_public_membership_q(user)).order_by(
        "name", "id"
    )
    search_q = person_canonical_or_alias_icontains_q(search_query)
    if search_q is not None:
        people = people.filter(search_q)
    return people


def public_person_archive_items_queryset(user, person_id: int) -> QuerySet[ArchiveItem]:
    """Distinct authorized+renderable ArchiveItems for one Person (AIP or PhotoPerson).

    Annotates ``person_first_matching_photo_id``: earliest matching renderable
    ``PhotoContent`` by ``(position, id)``, or NULL when the item is AIP-only.
    Outer queryset is ``ArchiveItem``, so overlap and multiple photos do not
    duplicate rows. Order is ``-created_at, pk``.
    """
    renderable_photos = renderable_photo_contents_queryset()
    aip_exists = Exists(
        ArchiveItemPerson.objects.filter(
            person_id=person_id,
            archive_item_id=OuterRef("pk"),
        )
    )
    pp_exists = Exists(
        PhotoPerson.objects.filter(
            person_id=person_id,
            photo_content__archive_item_id=OuterRef("pk"),
            photo_content__in=renderable_photos,
        )
    )
    first_photo_id = Subquery(
        PhotoPerson.objects.filter(
            person_id=person_id,
            photo_content__archive_item_id=OuterRef("pk"),
            photo_content__in=renderable_photos,
        )
        .order_by("photo_content__position", "photo_content_id")
        .values("photo_content_id")[:1]
    )
    return (
        archive_browse_queryset_for_user(user)
        .filter(aip_exists | pp_exists)
        .annotate(
            **{PERSON_FIRST_MATCHING_PHOTO_ANNOTATION: first_photo_id},
        )
        .order_by("-created_at", "pk")
    )


def public_people_item_counts_for_person_ids(
    user,
    person_ids: Iterable[int],
) -> dict[int, int]:
    """DISTINCT authorized+renderable ArchiveItem counts for a page of Person ids.

    UNION DISTINCT of ``(person_id, archive_item_id)`` from ArchiveItemPerson and
    renderable PhotoPerson, applied in Python over two restricted pair
    queries so empty AIP or PP sides cannot drop the other. Multiple photos
    of one item and dual AIP+PP links count once. Does not add AIP count to
    PhotoPerson count.
    """
    page_ids = [int(person_id) for person_id in person_ids]
    if not page_ids:
        return {}

    authorized_pks = authorized_browse_item_pks(user)
    renderable_photos = renderable_photo_contents_queryset()
    aip_pairs = ArchiveItemPerson.objects.filter(
        person_id__in=page_ids,
        archive_item_id__in=authorized_pks,
    ).values_list("person_id", "archive_item_id")
    pp_pairs = PhotoPerson.objects.filter(
        person_id__in=page_ids,
        photo_content__in=renderable_photos,
        photo_content__archive_item_id__in=authorized_pks,
    ).values_list("person_id", "photo_content__archive_item_id")
    distinct_pairs = {
        (int(person_id), int(item_id)) for person_id, item_id in aip_pairs
    }
    distinct_pairs.update(
        (int(person_id), int(item_id)) for person_id, item_id in pp_pairs
    )
    counts: dict[int, int] = {}
    for person_id, _item_id in distinct_pairs:
        counts[person_id] = counts.get(person_id, 0) + 1
    return counts


def build_public_people_index_rows(
    user,
    people: Sequence[Person],
) -> list[PublicPersonIndexRow]:
    """Attach DISTINCT public item counts to a page of Person rows."""
    counts = public_people_item_counts_for_person_ids(
        user, [person.pk for person in people]
    )
    return [
        PublicPersonIndexRow(
            name=person.name,
            href=person_public_page_url(person.pk),
            item_count=counts.get(person.pk, 0),
        )
        for person in people
    ]


def _first_matching_photo_id(archive_item: ArchiveItem) -> int | None:
    raw = getattr(archive_item, PERSON_FIRST_MATCHING_PHOTO_ANNOTATION, None)
    if raw is None:
        return None
    photo_id = int(raw)
    if photo_id < 1:
        return None
    return photo_id


def person_public_item_detail_url(
    *,
    archive_item_id: int,
    first_matching_photo_id: int | None,
) -> str:
    """Deep-link to the earliest matching renderable photo when one exists."""
    if first_matching_photo_id is not None:
        return public_photo_detail_url(archive_item_id, first_matching_photo_id)
    return public_photo_detail_url(archive_item_id)


def build_person_public_item_cards(
    items: Sequence[ArchiveItem],
    *,
    bucket: str,
    expires_in: int = 3600,
) -> list[ArchiveBrowseCard]:
    """Browse cards for the unified Person-detail item list.

    PhotoPerson hits replace ``detail_url`` and thumbnail with the earliest
    matching renderable photo. AIP-only items keep the normal item URL and
    primary-photo thumbnail.
    """
    cards = build_archive_browse_cards(items)
    cards = apply_photo_thumbnail_urls_to_browse_cards(
        cards, bucket=bucket, expires_in=expires_in
    )
    cards = apply_document_thumbnail_urls_to_browse_cards(
        cards, bucket=bucket, expires_in=expires_in
    )
    photo_ids = [
        photo_id
        for photo_id in (_first_matching_photo_id(item) for item in items)
        if photo_id is not None
    ]
    photos_by_id = {
        photo.pk: photo for photo in PhotoContent.objects.filter(pk__in=photo_ids)
    }
    built: list[ArchiveBrowseCard] = []
    for item, card in zip(items, cards, strict=True):
        photo_id = _first_matching_photo_id(item)
        if photo_id is None:
            built.append(card)
            continue
        photo = photos_by_id.get(photo_id)
        thumbnail_url = None
        if photo is not None:
            thumbnail_url = presign_photo_thumbnail_url(
                photo, bucket=bucket, expires_in=expires_in
            )
        built.append(
            replace(
                card,
                detail_url=person_public_item_detail_url(
                    archive_item_id=item.pk,
                    first_matching_photo_id=photo_id,
                ),
                thumbnail_url=thumbnail_url,
            )
        )
    return built
