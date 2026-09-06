"""Public Person catalog and unified Person-detail ArchiveItem relations.

Directory membership is AIP, renderable PhotoPerson, or an explicitly linked
Author with public ArchiveItemAuthor membership. Name equality is not identity.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
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
