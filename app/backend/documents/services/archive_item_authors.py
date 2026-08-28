"""Ordered ArchiveItemAuthor relations and legacy author_name dual-write.

Author is bibliographic, not Person. Callers that persist ``author_name`` on
OCR / MANUAL_TEXT / VIDEO writers must use ``apply_legacy_author_name`` in the
same transaction. PHOTO writers do not dual-write. Comma input is not split.
"""

from __future__ import annotations

from collections.abc import Sequence

from django.db import transaction

from documents.models import ArchiveItem, ArchiveItemAuthor, Author

AMBIGUOUS_AUTHOR_ERROR = (
    "multiple authors exist with this exact name; resolve duplicates before saving"
)
DUPLICATE_AUTHOR_IN_ORDER_ERROR = "duplicate author in ordered author list"


class ArchiveItemAuthorError(Exception):
    """Fail-closed author relation write error."""

    def __init__(self, message: str):
        super().__init__(message)
        self.message = message


def ordered_author_links(archive_item: ArchiveItem) -> list[ArchiveItemAuthor]:
    """Return this item's author links in position order."""
    return list(
        archive_item.author_links.select_related("author").order_by("position", "id")
    )


def ordered_authors(archive_item: ArchiveItem) -> list[Author]:
    """Return this item's authors in position order."""
    return [link.author for link in ordered_author_links(archive_item)]


def _resolve_unique_author_by_exact_name(name: str) -> Author:
    matches = list(Author.objects.select_for_update().filter(name=name).order_by("id"))
    if len(matches) > 1:
        raise ArchiveItemAuthorError(AMBIGUOUS_AUTHOR_ERROR)
    if matches:
        return matches[0]
    return Author.objects.create(name=name)


def _replace_author_links(
    archive_item: ArchiveItem,
    authors: Sequence[Author],
) -> list[ArchiveItemAuthor]:
    unique_ids = [author.pk for author in authors]
    if len(unique_ids) != len(set(unique_ids)):
        raise ArchiveItemAuthorError(DUPLICATE_AUTHOR_IN_ORDER_ERROR)

    ArchiveItemAuthor.objects.filter(archive_item=archive_item).delete()
    links: list[ArchiveItemAuthor] = []
    for position, author in enumerate(authors):
        links.append(
            ArchiveItemAuthor.objects.create(
                archive_item=archive_item,
                author=author,
                position=position,
            )
        )
    return links


@transaction.atomic
def replace_archive_item_authors(
    archive_item: ArchiveItem,
    authors: Sequence[Author],
) -> list[ArchiveItemAuthor]:
    """Replace author links with ``authors`` in order (position 0..n-1).

    Does not write ``author_name``, create Person rows, or split names.
    Duplicate authors in ``authors`` fail closed before any relation write.
    """
    locked_item = (
        ArchiveItem.objects.select_for_update().filter(pk=archive_item.pk).first()
    )
    if locked_item is None:
        raise ArchiveItem.DoesNotExist
    return _replace_author_links(locked_item, authors)


@transaction.atomic
def apply_legacy_author_name(
    archive_item: ArchiveItem,
    author_name: str,
) -> list[ArchiveItemAuthor]:
    """Atomically persist ``author_name`` and dual-write one Author at position 0.

    Empty/whitespace-only clears ``author_name`` and all author relations.
    Exact name match reuses the single Author, creates one if none exists, and
    fails closed if multiple exact matching Authors exist — before any item,
    string, or relation write. Does not split commas, strip titles, merge
    spelling variants, or create Person / ArchiveItemPerson / PhotoPerson rows.
    """
    locked_item = (
        ArchiveItem.objects.select_for_update().filter(pk=archive_item.pk).first()
    )
    if locked_item is None:
        raise ArchiveItem.DoesNotExist

    exact_name = (author_name or "").strip()
    resolved: Author | None = None
    if exact_name:
        resolved = _resolve_unique_author_by_exact_name(exact_name)

    locked_item.author_name = exact_name
    locked_item.save(update_fields=["author_name", "updated_at"])
    archive_item.author_name = exact_name

    links = _replace_author_links(
        locked_item,
        [resolved] if resolved is not None else [],
    )
    return links
