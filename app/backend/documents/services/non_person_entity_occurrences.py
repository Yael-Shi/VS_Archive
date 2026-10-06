"""Shared non-person occurrence text primitives and read-side validity.

Workbook preflight, dry-run, and apply call these helpers. Request-time
readers must use this module and must not import the workbook classifier.

Validity answers whether a stored pin still matches the current authoritative
displayed text. It does not decide whether a user may see the archive item,
and it does not write.
"""

from __future__ import annotations

import hashlib
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

from django.core.exceptions import ObjectDoesNotExist

from documents.models import ArchiveItem, ArchiveItemEntityOccurrence, NonPersonEntity
from documents.services.text_presentation import resolve_displayed_transcription_result

SURFACE_V1 = "surface-v1"

_WS = re.compile(r"\s")
_BIDI_MARKS = dict.fromkeys(
    map(
        ord,
        "\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069",
    ),
    None,
)


@dataclass(frozen=True)
class SurfaceOccurrence:
    """One match of a single normalized surface.

    ``ordinal`` is 1-based among non-overlapping matches of that surface only.
    It is not a position among other surfaces. ``start`` and ``end`` are
    indexes into the original source text (``end`` exclusive), present only
    when a safe original slice was recovered.
    """

    ordinal: int
    matched_text: str
    start: int
    end: int


@dataclass(frozen=True)
class SurfaceLocation:
    """Occurrences of one normalized surface in authoritative text.

    ``occurrences`` is None when the count is known but a safe original
    ``matched_text`` slice could not be recovered.
    """

    count: int
    occurrences: tuple[SurfaceOccurrence, ...] | None


@dataclass(frozen=True)
class AuthoritativeTextContext:
    """Displayed text and SHA-256 for one archive item, computed once.

    ``texts[kind] is None`` when that kind has no authoritative text.
    ``digests[kind] is None`` in that same case. A digest is never stored
    on the occurrence row.
    """

    archive_item_id: int
    texts: Mapping[str, str | None]
    digests: Mapping[str, str | None]


@dataclass(frozen=True)
class ValidEntityLink:
    """One NonPersonEntity reached by at least one currently valid occurrence.

    This is not a public chip and not a stored link. Order is the first
    valid occurrence by primary key. Public chip order must use character
    offsets from ``locate_surface_occurrences``, not ``occurrence_ordinal``.
    """

    entity: NonPersonEntity

    @property
    def entity_id(self) -> int:
        return self.entity.pk


def normalize_surface_v1(surface: str) -> str:
    """Normalize one surface with the occurrence surface-v1 contract.

    NFC, strip bidi marks, casefold, trim, and collapse internal whitespace.
    Punctuation and Hebrew prefixes are kept. There is no final-letter folding.
    """

    text = unicodedata.normalize("NFC", surface)
    text = text.translate(_BIDI_MARKS)
    text = text.casefold().strip()
    return re.sub(r"\s+", " ", text)


def source_text_sha256(text: str) -> str:
    """SHA-256 of the exact UTF-8 text before surface normalization."""

    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def locate_surface_occurrences(source_text: str, surface: str) -> SurfaceLocation:
    """Find non-overlapping left-to-right surface-v1 matches.

    The count uses ``normalize_surface_v1``. Original slices and character
    offsets are returned only when each slice normalizes back to the same
    surface. Repeated matches stay distinct ordinals. They are not collapsed.
    """

    normalized_surface = normalize_surface_v1(surface)
    normalized_source = normalize_surface_v1(source_text)
    starts = _find_nonoverlapping(normalized_source, normalized_surface)
    if not normalized_surface:
        return SurfaceLocation(count=0, occurrences=())
    mapped = _normalize_with_spans(source_text)
    if mapped is None or mapped[0] != normalized_source:
        return SurfaceLocation(count=len(starts), occurrences=None)
    normalized, spans = mapped
    found: list[SurfaceOccurrence] = []
    for ordinal, start in enumerate(starts, start=1):
        end = start + len(normalized_surface)
        original_start = spans[start][0]
        original_end = spans[end - 1][1]
        matched = source_text[original_start:original_end]
        if normalize_surface_v1(matched) != normalized_surface:
            return SurfaceLocation(count=len(starts), occurrences=None)
        if normalized[start:end] != normalized_surface:
            return SurfaceLocation(count=len(starts), occurrences=None)
        found.append(
            SurfaceOccurrence(
                ordinal=ordinal,
                matched_text=matched,
                start=original_start,
                end=original_end,
            )
        )
    return SurfaceLocation(count=len(starts), occurrences=tuple(found))


def item_supports_occurrence_text_kind(item: ArchiveItem, text_kind: str) -> bool:
    """True when this item type is the source for that occurrence text kind."""

    kinds = ArchiveItemEntityOccurrence.TextKind
    if text_kind == kinds.MANUAL_TEXT:
        return item.item_type == ArchiveItem.ItemType.MANUAL_TEXT
    if text_kind == kinds.OCR_TRANSCRIPTION:
        return item.item_type == ArchiveItem.ItemType.OCR_DOCUMENT
    return False


def authoritative_displayed_text(item: ArchiveItem, text_kind: str) -> str | None:
    """Displayed source text for one occurrence text kind. Read-only.

    ``MANUAL_TEXT`` is ``ManualTextContent.body``. ``OCR_TRANSCRIPTION`` is
    ``resolve_displayed_transcription_result`` text: Hebrew documents prefer
    displayable ``HEBREW_TEXT``, then ``SOURCE_TEXT``; other languages prefer
    ``SOURCE_TEXT``, then ``HEBREW_TEXT``. Missing text is ``None``, not ``""``.
    """

    kinds = ArchiveItemEntityOccurrence.TextKind
    if text_kind == kinds.MANUAL_TEXT:
        try:
            return item.manual_text_content.body
        except ObjectDoesNotExist:
            return None
    if text_kind != kinds.OCR_TRANSCRIPTION:
        return None
    try:
        document = item.ocr_document
    except ObjectDoesNotExist:
        return None
    result = resolve_displayed_transcription_result(document)
    if result is None or result.text is None:
        return None
    return result.text


def authoritative_text_context_for_item(item: ArchiveItem) -> AuthoritativeTextContext:
    """Load both text kinds and hash each available string once."""

    kinds = (
        ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
        ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
    )
    texts: dict[str, str | None] = {}
    digests: dict[str, str | None] = {}
    for kind in kinds:
        text = authoritative_displayed_text(item, kind)
        texts[kind] = text
        digests[kind] = None if text is None else source_text_sha256(text)
    return AuthoritativeTextContext(
        archive_item_id=item.pk,
        texts=MappingProxyType(texts),
        digests=MappingProxyType(digests),
    )


def occurrence_is_currently_valid(
    occurrence: ArchiveItemEntityOccurrence,
    *,
    context: AuthoritativeTextContext | None = None,
) -> bool:
    """True only when the pin still matches current authoritative text.

    Fail closed. Does not write, does not rewrite ``source_text_sha256``, and
    does not apply archive-item authorization. Pass ``context`` from
    ``authoritative_text_context_for_item`` to reuse one hash per text kind.
    A context for a different item is not valid.
    """

    if occurrence.resolution_status != (
        ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED
    ):
        return False
    if occurrence.entity_id is None:
        return False
    if occurrence.normalization_version != SURFACE_V1:
        return False
    if occurrence.archive_item_id is None:
        return False
    item = occurrence.archive_item
    if not item_supports_occurrence_text_kind(item, occurrence.text_kind):
        return False
    if context is None:
        context = authoritative_text_context_for_item(item)
    elif context.archive_item_id != occurrence.archive_item_id:
        return False
    digest = context.digests.get(occurrence.text_kind)
    if digest is None:
        return False
    return digest == occurrence.source_text_sha256


def valid_occurrences_for_item(
    item: ArchiveItem,
) -> tuple[ArchiveItemEntityOccurrence, ...]:
    """Currently valid pins for one item, in primary-key order.

    Stale and unresolved rows are omitted. Separate occurrence rows are kept.
    This does not dedupe by entity.
    """

    context = authoritative_text_context_for_item(item)
    rows = item.entity_occurrences.select_related("archive_item", "entity").order_by(
        "id"
    )
    return tuple(
        row for row in rows if occurrence_is_currently_valid(row, context=context)
    )


def deduped_valid_entity_links_for_item(
    item: ArchiveItem,
) -> tuple[ValidEntityLink, ...]:
    """One link per entity that has a currently valid occurrence on this item.

    An entity with only stale or unresolved pins is absent. This is not public
    chip order.
    """

    seen: set[int] = set()
    links: list[ValidEntityLink] = []
    for occurrence in valid_occurrences_for_item(item):
        entity = occurrence.entity
        if entity is None or entity.pk in seen:
            continue
        seen.add(entity.pk)
        links.append(ValidEntityLink(entity=entity))
    return tuple(links)


def _find_nonoverlapping(text: str, surface: str) -> list[int]:
    if surface == "":
        return []
    starts: list[int] = []
    cursor = 0
    while True:
        found = text.find(surface, cursor)
        if found < 0:
            return starts
        starts.append(found)
        cursor = found + len(surface)


def _normalize_with_spans(
    text: str,
) -> tuple[str, list[tuple[int, int]]] | None:
    nfc = _nfc_with_spans(text)
    if nfc is None:
        return None
    without_bidi = _drop_bidi(*nfc)
    folded = _casefold_with_spans(*without_bidi)
    if folded is None:
        return None
    stripped = _strip_spans(*folded)
    return _collapse_whitespace(*stripped)


def _nfc_with_spans(
    text: str,
) -> tuple[str, list[tuple[int, int]]] | None:
    """Map NFC characters only when the source is already NFC.

    One NFC pass decides the fast path. Already-NFC text uses one original
    span per character. Text that still changes under NFC returns no map, so
    later code cannot invent offsets. Occurrence counting stays on
    ``normalize_surface_v1``.
    """

    normalized = unicodedata.normalize("NFC", text)
    if normalized != text:
        return None
    return normalized, [(index, index + 1) for index in range(len(text))]


def _drop_bidi(
    text: str,
    spans: list[tuple[int, int]],
) -> tuple[str, list[tuple[int, int]]]:
    kept = [
        (character, span)
        for character, span in zip(text, spans, strict=True)
        if ord(character) not in _BIDI_MARKS
    ]
    if not kept:
        return "", []
    characters, kept_spans = zip(*kept, strict=True)
    return "".join(characters), list(kept_spans)


def _casefold_with_spans(
    text: str,
    spans: list[tuple[int, int]],
) -> tuple[str, list[tuple[int, int]]] | None:
    characters: list[str] = []
    folded_spans: list[tuple[int, int]] = []
    for character, span in zip(text, spans, strict=True):
        folded = character.casefold()
        if folded == "":
            continue
        characters.extend(folded)
        folded_spans.extend([span] * len(folded))
    folded_text = "".join(characters)
    if folded_text != text.casefold():
        return None
    return folded_text, folded_spans


def _strip_spans(
    text: str,
    spans: list[tuple[int, int]],
) -> tuple[str, list[tuple[int, int]]]:
    start = 0
    end = len(text)
    while start < end and _WS.fullmatch(text[start]):
        start += 1
    while end > start and _WS.fullmatch(text[end - 1]):
        end -= 1
    return text[start:end], spans[start:end]


def _collapse_whitespace(
    text: str,
    spans: list[tuple[int, int]],
) -> tuple[str, list[tuple[int, int]]]:
    characters: list[str] = []
    collapsed: list[tuple[int, int]] = []
    index = 0
    while index < len(text):
        if _WS.fullmatch(text[index]):
            end = index + 1
            while end < len(text) and _WS.fullmatch(text[end]):
                end += 1
            characters.append(" ")
            collapsed.append((spans[index][0], spans[end - 1][1]))
            index = end
            continue
        characters.append(text[index])
        collapsed.append(spans[index])
        index += 1
    return "".join(characters), collapsed
