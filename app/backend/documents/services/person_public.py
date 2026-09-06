"""Public Person catalog and unified Person-detail ArchiveItem relations.

Directory membership is AIP, renderable PhotoPerson, or an explicitly linked
Author with public ArchiveItemAuthor membership. Name equality is not identity.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, replace

from django.db.models import Exists, OuterRef, Q, QuerySet, Subquery

from documents.models import (
    ArchiveItem,
    ArchiveItemAuthor,
    ArchiveItemPerson,
    Author,
    Person,
    PhotoContent,
    PhotoPerson,
)
from documents.services.archive_item_access import archive_browse_queryset_for_user
from documents.services.author_public import author_public_membership_q
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
    """Person rows with at least one authorized+renderable AIP or PhotoPerson item.

    Used by the advanced Person filter picker. Does not include linked-Author
    membership; that belongs to the unified public People directory/detail.
    """
    return person_public_membership_q_for_item_pks(authorized_browse_item_pks(user))


def person_linked_author_membership_q_for_item_pks(item_pks: QuerySet) -> Q:
    """Person rows with an explicit ``Author.person`` link to public AIA items.

    Uses the FK only. Does not infer identity from Author/Person names.
    """
    return Exists(
        ArchiveItemAuthor.objects.filter(
            author__person_id=OuterRef("pk"),
            archive_item_id__in=item_pks,
        )
    )


def person_unified_public_membership_q(user) -> Q:
    """Directory/detail membership: AIP, renderable PhotoPerson, or linked Author AIA."""
    item_pks = authorized_browse_item_pks(user)
    return person_public_membership_q_for_item_pks(
        item_pks
    ) | person_linked_author_membership_q_for_item_pks(item_pks)


def _person_linked_author_name_icontains_q(user, search_query: str) -> Q | None:
    """Match linked ``Author.name`` only when that Author has public AIA membership.

    Uses ``author_public_membership_q`` (authorized + browse-renderable
    ``ArchiveItemAuthor``). A private-only linked Author name is not searchable.
    Does not infer identity from names.
    """
    q = (search_query or "").strip()
    if not q:
        return None
    return Exists(
        Author.objects.filter(
            person_id=OuterRef("pk"),
            name__icontains=q,
        ).filter(author_public_membership_q(user))
    )


def public_people_queryset(user, *, search_query: str = "") -> QuerySet[Person]:
    """Public People-directory Person identities: unified membership and search.

    Search is canonical name, alias, or an explicitly linked ``Author.name``
    that itself has authorized+browse-renderable ``ArchiveItemAuthor``
    membership. Name equality with an unlinked Author is not identity.
    """
    people = Person.objects.filter(person_unified_public_membership_q(user)).order_by(
        "name", "id"
    )
    search_q = person_canonical_or_alias_icontains_q(search_query)
    linked_author_q = _person_linked_author_name_icontains_q(user, search_query)
    if search_q is not None and linked_author_q is not None:
        people = people.filter(search_q | linked_author_q)
    return people


def public_person_archive_items_queryset(user, person_id: int) -> QuerySet[ArchiveItem]:
    """Distinct authorized+renderable ArchiveItems for one Person.

    Membership is AIP, renderable PhotoPerson, or ArchiveItemAuthor on an
    Author explicitly linked with ``Author.person_id``. Annotates
    ``person_first_matching_photo_id``: earliest matching renderable
    ``PhotoContent`` by ``(position, id)``, or NULL when there is no matching
    PhotoPerson (including authored-only items). Authored overlap does not
    drop that PhotoPerson deep-link. Outer queryset is ``ArchiveItem``, so
    overlap does not duplicate rows. Order is ``-created_at, pk``.
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
    aia_exists = Exists(
        ArchiveItemAuthor.objects.filter(
            author__person_id=person_id,
            archive_item_id=OuterRef("pk"),
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
        .filter(aip_exists | pp_exists | aia_exists)
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

    UNION DISTINCT of ``(person_id, archive_item_id)`` from ArchiveItemPerson,
    renderable PhotoPerson, and ArchiveItemAuthor via explicit
    ``Author.person_id``. Applied in Python over restricted pair queries so
    empty sides cannot drop the others. Multiple photos, dual AIP+PP, and
    authored overlap count once.
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
    aia_pairs = ArchiveItemAuthor.objects.filter(
        author__person_id__in=page_ids,
        archive_item_id__in=authorized_pks,
    ).values_list("author__person_id", "archive_item_id")
    distinct_pairs.update(
        (int(person_id), int(item_id))
        for person_id, item_id in aia_pairs
        if person_id is not None
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


def matching_photo_ids_for_selected_persons(
    item_ids: Iterable[int],
    person_ids: Sequence[int],
) -> dict[int, int]:
    """Map page ArchiveItem id → presentation PhotoContent id for ``person=``.

    One batched ``PhotoPerson`` query over the given item ids, selected Person
    ids, and renderable ``PhotoContent`` only. Does not infer ArchiveItemPerson,
    does not read ``people_present``, and does not change filter membership.

    ``|S| == 1``: earliest renderable photo containing that Person, by
    ``(position, id)``.
    ``|S| >= 2``: earliest renderable photo whose PhotoPerson set among the
    selected ids is a superset of S; omit the item when no such photo exists.
    """
    selected = tuple(
        person_id for person_id in (int(value) for value in person_ids) if person_id >= 1
    )
    page_ids = [
        item_id for item_id in (int(value) for value in item_ids) if item_id >= 1
    ]
    if not selected or not page_ids:
        return {}

    selected_set = set(selected)
    rows = PhotoPerson.objects.filter(
        person_id__in=selected,
        photo_content__archive_item_id__in=page_ids,
        photo_content__in=renderable_photo_contents_queryset(),
    ).values_list(
        "photo_content__archive_item_id",
        "person_id",
        "photo_content_id",
        "photo_content__position",
    )

    photos: dict[tuple[int, int], tuple[int, set[int]]] = {}
    for item_id, person_id, photo_id, position in rows:
        key = (int(item_id), int(photo_id))
        if key not in photos:
            photos[key] = (int(position), set())
        photos[key][1].add(int(person_id))

    require_all = len(selected_set) > 1
    candidates_by_item: dict[int, list[tuple[int, int]]] = {}
    for (item_id, photo_id), (position, persons) in photos.items():
        if require_all:
            if not selected_set <= persons:
                continue
        elif selected_set.isdisjoint(persons):
            continue
        candidates_by_item.setdefault(item_id, []).append((position, photo_id))

    chosen: dict[int, int] = {}
    for item_id, candidates in candidates_by_item.items():
        candidates.sort()
        chosen[item_id] = candidates[0][1]
    return chosen


def person_public_item_detail_url(
    *,
    archive_item_id: int,
    first_matching_photo_id: int | None,
) -> str:
    """Deep-link to the earliest matching renderable photo when one exists."""
    if first_matching_photo_id is not None:
        return public_photo_detail_url(archive_item_id, first_matching_photo_id)
    return public_photo_detail_url(archive_item_id)


def apply_matching_photo_presentation_to_cards(
    cards: Sequence[ArchiveBrowseCard],
    photo_id_by_item_id: Mapping[int, int],
    *,
    bucket: str,
    expires_in: int = 3600,
) -> list[ArchiveBrowseCard]:
    """Override card href and thumbnail from a page-slice photo-id map.

    Used by Person detail and advanced ``person=`` archive-list presentation.
    Matching PhotoContent rows are loaded in one query. A missing thumbnail
    key yields ``thumbnail_url=None`` even when the primary photo had a thumb
    (same as Person detail).
    """
    if not photo_id_by_item_id:
        return list(cards)

    photo_ids = [
        photo_id for photo_id in photo_id_by_item_id.values() if int(photo_id) >= 1
    ]
    photos_by_id = {
        photo.pk: photo for photo in PhotoContent.objects.filter(pk__in=photo_ids)
    }
    built: list[ArchiveBrowseCard] = []
    for card in cards:
        raw_photo_id = photo_id_by_item_id.get(card.item.pk)
        if raw_photo_id is None:
            built.append(card)
            continue
        photo_id = int(raw_photo_id)
        if photo_id < 1:
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
                    archive_item_id=card.item.pk,
                    first_matching_photo_id=photo_id,
                ),
                thumbnail_url=thumbnail_url,
            )
        )
    return built


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
    photo_id_by_item_id: dict[int, int] = {}
    for item in items:
        photo_id = _first_matching_photo_id(item)
        if photo_id is not None:
            photo_id_by_item_id[item.pk] = photo_id
    return apply_matching_photo_presentation_to_cards(
        cards,
        photo_id_by_item_id,
        bucket=bucket,
        expires_in=expires_in,
    )
