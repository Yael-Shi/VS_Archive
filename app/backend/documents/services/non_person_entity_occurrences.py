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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from functools import lru_cache
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
class _PreparedNormalizedSource:
    """One authoritative body after a single surface-v1 span normalization.

    ``normalized_text`` is ``normalize_surface_v1(original_text)``.
    ``character_spans`` is the per-code-point original cover from
    ``_normalize_with_spans`` when that map's text equals ``normalized_text``.
    ``None`` means the span map is missing or disagrees, which is the same
    fail-closed state as a per-call locate: counts may still be computed,
    and original slices are not.
    """

    original_text: str
    normalized_text: str
    character_spans: tuple[tuple[int, int], ...] | None


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

    The count uses ``normalize_surface_v1`` and non-overlapping ``str.find``.
    Original slices are recovered from the span map. Canonical NFC reordering
    and composition may move or merge code points; the recovered slice is the
    bounding original cover of that match. Offsets are returned only when the
    slice normalizes back to the same surface. A cover that cannot be proved
    returns the count with ``occurrences is None``. Repeated matches stay
    distinct ordinals. They are not collapsed.

    An empty normalized surface returns no occurrences and does not build the
    source span map. Any other call prepares that source once, then searches.
    """

    if normalize_surface_v1(surface) == "":
        return SurfaceLocation(count=0, occurrences=())
    return _locate_prepared_surface(_prepare_normalized_source(source_text), surface)


def _prepare_normalized_source(source_text: str) -> _PreparedNormalizedSource:
    """Normalize one source body and keep its span map when the map agrees."""

    normalized_text = normalize_surface_v1(source_text)
    mapped = _normalize_with_spans(source_text)
    if mapped is None or mapped[0] != normalized_text:
        spans = None
    else:
        spans = tuple(mapped[1])
    return _PreparedNormalizedSource(
        original_text=source_text,
        normalized_text=normalized_text,
        character_spans=spans,
    )


def _locate_prepared_surface(
    prepared: _PreparedNormalizedSource,
    surface: str,
) -> SurfaceLocation:
    """Locate one surface in an already prepared source.

    Ordinals follow non-overlapping ``str.find`` order. A failed original
    cover drops every slice for this surface and keeps the count.
    """

    normalized_surface = normalize_surface_v1(surface)
    starts = _find_nonoverlapping(prepared.normalized_text, normalized_surface)
    if not normalized_surface:
        return SurfaceLocation(count=0, occurrences=())
    spans = prepared.character_spans
    if spans is None:
        return SurfaceLocation(count=len(starts), occurrences=None)
    source_text = prepared.original_text
    normalized = prepared.normalized_text
    found: list[SurfaceOccurrence] = []
    for ordinal, start in enumerate(starts, start=1):
        end = start + len(normalized_surface)
        if end > len(spans):
            return SurfaceLocation(count=len(starts), occurrences=None)
        original_start, original_end = _cover_original_span(spans, start, end)
        if (
            original_start < 0
            or original_end > len(source_text)
            or original_start >= original_end
        ):
            return SurfaceLocation(count=len(starts), occurrences=None)
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


def _cover_original_span(
    spans: Sequence[tuple[int, int]],
    start: int,
    end: int,
) -> tuple[int, int]:
    """Bounding original slice of ``spans[start:end]``.

    Already-NFC spans are monotonic, so this is the first span's start and
    the last span's end. Canonical reordering can place a later source index
    earlier in NFC order, so the cover is the minimum start and maximum end.
    """

    original_start, original_end = spans[start]
    for span_start, span_end in spans[start + 1 : end]:
        if span_start < original_start:
            original_start = span_start
        if span_end > original_end:
            original_end = span_end
    return original_start, original_end


def _nfc_with_spans(
    text: str,
) -> tuple[str, list[tuple[int, int]]] | None:
    """Map each NFC code point to the original span that produced it.

    Already-NFC text uses one original index per code point. Other text is
    canonically decomposed, stably reordered by combining class, and
    composed. Pair composition uses ``unicodedata.normalize``, so exclusions
    and Hangul follow Unicode NFC. Each output code point keeps the bounding
    original span of the code points that produced it.

    The reconstructed string must equal ``unicodedata.normalize("NFC", text)``.
    Anything else returns no map. This pass is linear in the source length
    aside from sorting each combining run, which is ``O(k log k)`` in that
    run's length. It does not search the original string for matches.
    """

    normalized = unicodedata.normalize("NFC", text)
    if normalized == text:
        return normalized, [(index, index + 1) for index in range(len(text))]
    characters: list[str] = []
    spans: list[tuple[int, int]] = []
    for index, character in enumerate(text):
        parts = _canonical_decomposition(character)
        characters.extend(parts)
        spans.extend([(index, index + 1)] * len(parts))
    _reorder_combining_marks(characters, spans)
    output, output_spans = _compose_decomposed(characters, spans)
    if output != normalized or len(output_spans) != len(output):
        return None
    if any(start < 0 or end > len(text) or start >= end for start, end in output_spans):
        return None
    return output, output_spans


def _hangul_syllable_decomposition(character: str) -> list[str] | None:
    """Algorithmic canonical decomposition of one Hangul syllable."""

    syllable_index = ord(character) - 0xAC00
    if syllable_index < 0 or syllable_index >= 11172:
        return None
    lead_index, remainder = divmod(syllable_index, 588)
    vowel_index, trail_index = divmod(remainder, 28)
    lead = chr(0x1100 + lead_index)
    vowel = chr(0x1161 + vowel_index)
    if trail_index == 0:
        return [lead, vowel]
    return [lead, vowel, chr(0x11A7 + trail_index)]


def _canonical_decomposition(character: str) -> list[str]:
    """Full canonical decomposition of one code point.

    A compatibility mapping (``<compat>`` and the other tagged mappings) is
    not applied. Hangul syllables use the algorithmic decomposition.
    """

    hangul = _hangul_syllable_decomposition(character)
    if hangul is not None:
        return hangul
    mapping = unicodedata.decomposition(character)
    if mapping == "" or mapping.startswith("<"):
        return [character]
    parts: list[str] = []
    for piece in mapping.split():
        parts.extend(_canonical_decomposition(chr(int(piece, 16))))
    return parts


@lru_cache(maxsize=4096)
def _compose_pair(left: str, right: str) -> str | None:
    """One canonical composition, or None when the pair stays two code points."""

    composed = unicodedata.normalize("NFC", left + right)
    if len(composed) == 1:
        return composed
    return None


def _reorder_combining_marks(
    characters: list[str],
    spans: list[tuple[int, int]],
) -> None:
    """Stable canonical combining-class sort inside each mark run."""

    index = 0
    size = len(characters)
    while index < size:
        if unicodedata.combining(characters[index]) == 0:
            index += 1
            continue
        end = index + 1
        while end < size and unicodedata.combining(characters[end]) > 0:
            end += 1
        order = sorted(
            range(index, end),
            key=lambda mark_index: unicodedata.combining(characters[mark_index]),
        )
        if order != list(range(index, end)):
            characters[index:end] = [characters[mark] for mark in order]
            spans[index:end] = [spans[mark] for mark in order]
        index = end


def _compose_decomposed(
    characters: list[str],
    spans: list[tuple[int, int]],
) -> tuple[str, list[tuple[int, int]]]:
    """Compose one NFD sequence and union the spans of each composed pair."""

    if not characters:
        return "", []
    output = [characters[0]]
    output_spans = [spans[0]]
    starter = 0 if unicodedata.combining(characters[0]) == 0 else None
    last_ccc = unicodedata.combining(characters[0])
    for character, span in zip(characters[1:], spans[1:], strict=True):
        combining_class = unicodedata.combining(character)
        if starter is not None and (last_ccc == 0 or last_ccc < combining_class):
            composed = _compose_pair(output[starter], character)
            if composed is not None:
                output[starter] = composed
                previous = output_spans[starter]
                output_spans[starter] = (
                    min(previous[0], span[0]),
                    max(previous[1], span[1]),
                )
                continue
        output.append(character)
        output_spans.append(span)
        if combining_class == 0:
            starter = len(output) - 1
            last_ccc = 0
        else:
            last_ccc = combining_class
    return "".join(output), output_spans


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
