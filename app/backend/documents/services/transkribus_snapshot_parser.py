"""Pure PAGE XML parser for immutable Transkribus transcript snapshots.

No database or S3 I/O. Parser v1 preserves the same TextLine / page ordering and
text-joining behavior as ``transkribus_engine.parse_page_xml_to_text`` /
``complete_pylaia_transcription_after_job`` (lines joined with ``\\n``, pages
with ``\\n\\n``). ReadingOrder may be reported but never reorders v1 output.
"""

from __future__ import annotations

import hashlib
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Mapping, Sequence

from documents.services import transkribus_engine as tr
from documents.services.transkribus_page_xml_geometry import (
    BoundingBox,
    LineGeometryCapability,
    TranskribusPageXmlGeometryError,
    _bounding_box,
    _child_attr,
    _collect_text_lines,
    _count_elements,
    _find_page_element,
    _local_tag,
    _ns_map,
    _overlay_polygon_coords_in_bounds,
    _parse_baseline_points,
    _parse_int_attr,
    _parse_polygon_points,
    _reading_order_line_ids,
)

# Stable identity for this normalization contract. Future parser versions must
# use a new constant so the same raw PAGE XML can be reparsed without mutating
# older snapshots.
PARSER_VERSION = "page_xml_snapshot_v1"

PAGE_SEPARATOR = "\n\n"
LINE_SEPARATOR = "\n"


class TranskribusSnapshotParseError(ValueError):
    """Invalid PAGE XML or page metadata for snapshot parsing."""


@dataclass(frozen=True)
class ParsedSnapshotLine:
    order_index: int
    provider_region_id: str | None
    provider_line_id: str | None
    text: str
    contributes_to_canonical: bool
    char_start: int
    char_end: int
    polygon_points: tuple[tuple[float, float], ...]
    baseline_points: tuple[tuple[float, float], ...]
    bbox: BoundingBox | None
    coords_valid: bool
    baseline_valid: bool
    has_meaningful_geometry: bool


@dataclass(frozen=True)
class ParsedSnapshotPage:
    page_index: int
    page_nr: int
    transcript_ts_id: str
    provider_page_id: int | None
    page_namespace: str | None
    image_filename: str | None
    image_width: int | None
    image_height: int | None
    page_xml_sha256: str
    remote_transcript_status: str | None
    text_region_count: int
    text_line_count: int
    lines_with_non_empty_text: int
    lines_with_provider_line_ids: int
    duplicate_line_ids: int
    reading_order_present: bool
    reading_order_resolved: bool
    lines_xml_order_differs_from_reading_order: int
    page_geometry_capability: LineGeometryCapability
    canonical_text: str
    lines: tuple[ParsedSnapshotLine, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class ParsedSnapshotDocument:
    parser_version: str
    canonical_text: str
    canonical_text_sha256: str
    provider_identity_fingerprint: str
    raw_xml_fingerprint: str
    geometry_capability: LineGeometryCapability
    hover_eligible: bool
    pages: tuple[ParsedSnapshotPage, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class SnapshotPageInput:
    """One PAGE XML page plus provider metadata for document-level parse."""

    page_index: int
    page_nr: int
    transcript_ts_id: str
    page_xml: bytes
    provider_page_id: int | None = None
    remote_transcript_status: str | None = None


def compute_sha256_hex(data: bytes | str) -> str:
    if isinstance(data, str):
        data = data.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def compute_provider_identity_fingerprint(
    pages: Sequence[tuple[int, str]],
) -> str:
    """Fingerprint ordered ``(page_index, transcript_ts_id)`` pairs.

    Not unique at the schema layer: the same provider tsIds with different PAGE
    XML content must be allowed to create a new snapshot.
    """
    lines = [
        f"{int(page_index)}:{str(ts_id).strip()}"
        for page_index, ts_id in pages
        if str(ts_id).strip()
    ]
    return compute_sha256_hex("\n".join(lines))


def compute_raw_xml_fingerprint(page_xml_sha256_list: Sequence[str]) -> str:
    """Fingerprint ordered per-page PAGE XML content hashes."""
    return compute_sha256_hex("\n".join(page_xml_sha256_list))


def _production_line_unicode(text_line: ET.Element, ns: dict[str, str]) -> str:
    """Match ``parse_page_xml_to_text`` Unicode extraction exactly."""
    unicode_el = text_line.find("pc:TextEquiv/pc:Unicode", ns)
    if unicode_el is not None and unicode_el.text:
        return unicode_el.text
    unicode_el = text_line.find("TextEquiv/Unicode")
    if unicode_el is not None and unicode_el.text:
        return unicode_el.text
    return ""


def _production_text_lines(root: ET.Element, ns: dict[str, str]) -> list[ET.Element]:
    """TextLine iteration used by production ``parse_page_xml_to_text``."""
    return list(root.findall(".//pc:TextLine", ns))


def _parent_map(root: ET.Element) -> dict[ET.Element, ET.Element]:
    return {child: parent for parent in root.iter() for child in parent}


def _region_id_for_line(
    text_line: ET.Element, parent_map: Mapping[ET.Element, ET.Element]
) -> str | None:
    node: ET.Element | None = parent_map.get(text_line)
    while node is not None:
        if _local_tag(node.tag) == "TextRegion":
            return _child_attr(node, "id")
        node = parent_map.get(node)
    return None


def _page_capability_from_lines(
    *,
    image_width: int | None,
    image_height: int | None,
    lines: Sequence[ParsedSnapshotLine],
    polygons_outside_page_bounds: int,
    negative_or_outside_coordinates: int,
) -> LineGeometryCapability:
    """Mirror geometry-audit page capability rules for structured lines."""
    contributing_or_geom = [
        line
        for line in lines
        if line.contributes_to_canonical or line.has_meaningful_geometry
    ]
    lines_with_non_empty_text = sum(
        1 for line in lines if line.contributes_to_canonical
    )
    if lines_with_non_empty_text == 0 and not contributing_or_geom:
        return "INDETERMINATE"
    if lines_with_non_empty_text == 0:
        # Empty geometry-only page: treat like audit when no non-empty text.
        return "INDETERMINATE"

    bounds_validation_available = image_width is not None and image_height is not None
    lines_with_text_and_valid_coords = sum(
        1 for line in lines if line.contributes_to_canonical and line.coords_valid
    )
    # Structurally parseable non-degenerate polygons (may still be out of bounds).
    parseable_poly = sum(1 for line in lines if len(line.polygon_points) >= 3)

    if (
        bounds_validation_available
        and lines_with_text_and_valid_coords == lines_with_non_empty_text
        and lines_with_non_empty_text > 0
        and polygons_outside_page_bounds == 0
        and negative_or_outside_coordinates == 0
    ):
        return "VERIFIED"
    if lines_with_text_and_valid_coords > 0 or parseable_poly > 0:
        return "PARTIAL"
    if lines or lines_with_non_empty_text > 0:
        return "NOT_AVAILABLE"
    return "INDETERMINATE"


def _document_geometry_capability(
    page_caps: Sequence[LineGeometryCapability],
) -> LineGeometryCapability:
    if not page_caps:
        return "INDETERMINATE"
    caps = set(page_caps)
    if caps == {"VERIFIED"}:
        return "VERIFIED"
    if caps <= {"INDETERMINATE"}:
        return "INDETERMINATE"
    if all(cap == "NOT_AVAILABLE" for cap in page_caps):
        return "NOT_AVAILABLE"
    if "VERIFIED" in caps or "PARTIAL" in caps:
        return "PARTIAL"
    return "INDETERMINATE"


def parse_page_xml_for_snapshot(
    page_xml: bytes,
    *,
    page_index: int,
    page_nr: int,
    transcript_ts_id: str,
    provider_page_id: int | None = None,
    remote_transcript_status: str | None = None,
) -> ParsedSnapshotPage:
    """Parse one PAGE XML page into structured snapshot fields (no I/O)."""
    if page_index < 1:
        raise TranskribusSnapshotParseError(
            "page_index must be a 1-based positive integer."
        )
    if page_nr < 1:
        raise TranskribusSnapshotParseError("page_nr must be a positive integer.")
    ts_id = str(transcript_ts_id).strip()
    if not ts_id:
        raise TranskribusSnapshotParseError("transcript_ts_id must be non-empty.")

    try:
        root = ET.fromstring(page_xml)
    except ET.ParseError as exc:
        raise TranskribusSnapshotParseError(
            f"Invalid PAGE XML for page_index={page_index}"
        ) from exc

    ns = _ns_map(root)
    page_el = _find_page_element(root, ns)
    if page_el is None:
        raise TranskribusSnapshotParseError(
            f"PAGE XML missing Page element for page_index={page_index}"
        )

    page_namespace = root.tag.split("}")[0][1:] if root.tag.startswith("{") else None
    image_filename = _child_attr(page_el, "imageFilename")
    image_width = _parse_int_attr(page_el, "imageWidth")
    image_height = _parse_int_attr(page_el, "imageHeight")

    # Production text order: namespaced TextLine findall on the document root.
    production_lines = _production_text_lines(root, ns)
    parent_map = _parent_map(root)

    reading_order_ids = _reading_order_line_ids(page_el, ns)
    reading_order_present = reading_order_ids is not None or any(
        _local_tag(el.tag) == "ReadingOrder" for el in page_el
    )

    xml_order_ids: list[str] = []
    seen_ids: dict[str, int] = {}
    parsed_lines: list[ParsedSnapshotLine] = []
    contributing_texts: list[str] = []

    polygons_outside_page_bounds = 0
    negative_or_outside_coordinates = 0
    warnings: list[str] = []

    # Track offsets into this page's canonical text (production join of contributing lines).
    canonical_cursor = 0

    for text_line in production_lines:
        line_id = _child_attr(text_line, "id")
        if line_id:
            xml_order_ids.append(line_id)
            seen_ids[line_id] = seen_ids.get(line_id, 0) + 1

        unicode_text = _production_line_unicode(text_line, ns)
        contributes = bool(unicode_text)

        coords_el = text_line.find("pc:Coords", ns)
        if coords_el is None:
            for child in text_line:
                if _local_tag(child.tag) == "Coords":
                    coords_el = child
                    break
        coords_raw = _child_attr(coords_el, "points") if coords_el is not None else None
        parsed_coords = _parse_polygon_points(coords_raw)

        baseline_el = text_line.find("pc:Baseline", ns)
        if baseline_el is None:
            for child in text_line:
                if _local_tag(child.tag) == "Baseline":
                    baseline_el = child
                    break
        baseline_raw = (
            _child_attr(baseline_el, "points") if baseline_el is not None else None
        )
        parsed_baseline = _parse_baseline_points(baseline_raw)

        coords_structurally_valid = (
            coords_el is not None
            and bool(coords_raw)
            and not parsed_coords.malformed
            and not parsed_coords.degenerate
        )
        baseline_valid = (
            baseline_el is not None
            and bool(baseline_raw)
            and not parsed_baseline.malformed
            and not parsed_baseline.degenerate
        )

        coords_in_bounds = True
        if coords_structurally_valid and parsed_coords.points:
            in_bounds, neg, outside = _overlay_polygon_coords_in_bounds(
                parsed_coords.points,
                width=image_width,
                height=image_height,
            )
            coords_in_bounds = in_bounds
            if neg:
                negative_or_outside_coordinates += 1
            if outside:
                polygons_outside_page_bounds += 1

        coords_valid = coords_structurally_valid and coords_in_bounds
        has_meaningful_geometry = coords_valid or baseline_valid

        # Keep empty geometry-bearing (or identified) lines; omit empty noise.
        retain_line = contributes or has_meaningful_geometry or bool(line_id)
        if not retain_line:
            continue

        if contributes:
            if contributing_texts:
                canonical_cursor += len(LINE_SEPARATOR)
            char_start = canonical_cursor
            char_end = canonical_cursor + len(unicode_text)
            canonical_cursor = char_end
            contributing_texts.append(unicode_text)
        else:
            char_start = canonical_cursor
            char_end = canonical_cursor

        bbox = (
            _bounding_box(parsed_coords.points)
            if coords_valid and parsed_coords.points
            else None
        )

        parsed_lines.append(
            ParsedSnapshotLine(
                order_index=len(parsed_lines),
                provider_region_id=_region_id_for_line(text_line, parent_map),
                provider_line_id=line_id,
                text=unicode_text,
                contributes_to_canonical=contributes,
                char_start=char_start,
                char_end=char_end,
                polygon_points=parsed_coords.points
                if not parsed_coords.malformed
                else (),
                baseline_points=(
                    parsed_baseline.points if not parsed_baseline.malformed else ()
                ),
                bbox=bbox,
                coords_valid=coords_valid,
                baseline_valid=baseline_valid,
                has_meaningful_geometry=has_meaningful_geometry,
            )
        )

    canonical_text = LINE_SEPARATOR.join(contributing_texts)
    # Parity guard against production single-page parser.
    production_text = tr.parse_page_xml_to_text(page_xml)
    if canonical_text != production_text:
        raise TranskribusSnapshotParseError(
            f"Canonical text diverged from production parse_page_xml_to_text "
            f"for page_index={page_index}"
        )

    duplicate_line_ids = sum(1 for count in seen_ids.values() if count > 1)
    reading_order_resolved = False
    lines_xml_order_differs = 0
    if reading_order_ids is not None and xml_order_ids:
        reading_order_resolved = True
        xml_index = {line_id: idx for idx, line_id in enumerate(xml_order_ids)}
        for idx, line_id in enumerate(reading_order_ids):
            if line_id in xml_index and xml_index[line_id] != idx:
                lines_xml_order_differs += 1
        if lines_xml_order_differs:
            warnings.append(
                f"ReadingOrder diverges from XML TextLine order for "
                f"page_index={page_index} "
                f"({lines_xml_order_differs} line id position(s)); "
                "v1 canonical text keeps XML order."
            )

    page_capability = _page_capability_from_lines(
        image_width=image_width,
        image_height=image_height,
        lines=parsed_lines,
        polygons_outside_page_bounds=polygons_outside_page_bounds,
        negative_or_outside_coordinates=negative_or_outside_coordinates,
    )

    # Prefer geometry-audit TextLine count when ns fallback finds more elements;
    # production path may miss unnamespaced lines. Capability still uses production set.
    audit_line_count = len(_collect_text_lines(page_el, ns))

    return ParsedSnapshotPage(
        page_index=page_index,
        page_nr=page_nr,
        transcript_ts_id=ts_id,
        provider_page_id=provider_page_id,
        page_namespace=page_namespace,
        image_filename=image_filename,
        image_width=image_width,
        image_height=image_height,
        page_xml_sha256=compute_sha256_hex(page_xml),
        remote_transcript_status=(
            str(remote_transcript_status).strip()
            if remote_transcript_status and str(remote_transcript_status).strip()
            else None
        ),
        text_region_count=_count_elements(page_el, ns, "TextRegion"),
        text_line_count=max(len(production_lines), audit_line_count),
        lines_with_non_empty_text=sum(
            1 for line in parsed_lines if line.contributes_to_canonical
        ),
        lines_with_provider_line_ids=sum(
            1 for line in parsed_lines if line.provider_line_id
        ),
        duplicate_line_ids=duplicate_line_ids,
        reading_order_present=reading_order_present,
        reading_order_resolved=reading_order_resolved,
        lines_xml_order_differs_from_reading_order=lines_xml_order_differs,
        page_geometry_capability=page_capability,
        canonical_text=canonical_text,
        lines=tuple(parsed_lines),
        warnings=tuple(warnings),
    )


def parse_document_pages_for_snapshot(
    pages: Sequence[SnapshotPageInput],
) -> ParsedSnapshotDocument:
    """Parse ordered pages into a document-level snapshot parse result (no I/O)."""
    if not pages:
        raise TranskribusSnapshotParseError("At least one PAGE XML page is required.")

    seen_indexes: set[int] = set()
    parsed_pages: list[ParsedSnapshotPage] = []
    all_warnings: list[str] = []

    for page_input in pages:
        if page_input.page_index in seen_indexes:
            raise TranskribusSnapshotParseError(
                f"Duplicate page_index={page_input.page_index} in snapshot page inputs."
            )
        seen_indexes.add(page_input.page_index)
        parsed = parse_page_xml_for_snapshot(
            page_input.page_xml,
            page_index=page_input.page_index,
            page_nr=page_input.page_nr,
            transcript_ts_id=page_input.transcript_ts_id,
            provider_page_id=page_input.provider_page_id,
            remote_transcript_status=page_input.remote_transcript_status,
        )
        parsed_pages.append(parsed)
        all_warnings.extend(parsed.warnings)

    parsed_pages.sort(key=lambda p: p.page_index)
    page_canonicals = [page.canonical_text for page in parsed_pages]
    document_canonical = PAGE_SEPARATOR.join(page_canonicals)

    # Remap per-page offsets into document-canonical coordinates.
    remapped_pages: list[ParsedSnapshotPage] = []
    cursor = 0
    for index, page in enumerate(parsed_pages):
        if index > 0:
            cursor += len(PAGE_SEPARATOR)
        page_offset = cursor
        remapped_lines = tuple(
            ParsedSnapshotLine(
                order_index=line.order_index,
                provider_region_id=line.provider_region_id,
                provider_line_id=line.provider_line_id,
                text=line.text,
                contributes_to_canonical=line.contributes_to_canonical,
                char_start=line.char_start + page_offset,
                char_end=line.char_end + page_offset,
                polygon_points=line.polygon_points,
                baseline_points=line.baseline_points,
                bbox=line.bbox,
                coords_valid=line.coords_valid,
                baseline_valid=line.baseline_valid,
                has_meaningful_geometry=line.has_meaningful_geometry,
            )
            for line in page.lines
        )
        remapped_pages.append(
            ParsedSnapshotPage(
                page_index=page.page_index,
                page_nr=page.page_nr,
                transcript_ts_id=page.transcript_ts_id,
                provider_page_id=page.provider_page_id,
                page_namespace=page.page_namespace,
                image_filename=page.image_filename,
                image_width=page.image_width,
                image_height=page.image_height,
                page_xml_sha256=page.page_xml_sha256,
                remote_transcript_status=page.remote_transcript_status,
                text_region_count=page.text_region_count,
                text_line_count=page.text_line_count,
                lines_with_non_empty_text=page.lines_with_non_empty_text,
                lines_with_provider_line_ids=page.lines_with_provider_line_ids,
                duplicate_line_ids=page.duplicate_line_ids,
                reading_order_present=page.reading_order_present,
                reading_order_resolved=page.reading_order_resolved,
                lines_xml_order_differs_from_reading_order=(
                    page.lines_xml_order_differs_from_reading_order
                ),
                page_geometry_capability=page.page_geometry_capability,
                canonical_text=page.canonical_text,
                lines=remapped_lines,
                warnings=page.warnings,
            )
        )
        cursor += len(page.canonical_text)

    # Multi-page parity with production page join.
    production_joined = PAGE_SEPARATOR.join(
        tr.parse_page_xml_to_text(p.page_xml)
        for p in sorted(pages, key=lambda x: x.page_index)
    )
    if document_canonical != production_joined:
        raise TranskribusSnapshotParseError(
            "Document canonical text diverged from production page joining "
            "(parse_page_xml_to_text + \\n\\n)."
        )

    provider_fp = compute_provider_identity_fingerprint(
        [(p.page_index, p.transcript_ts_id) for p in remapped_pages]
    )
    raw_fp = compute_raw_xml_fingerprint([p.page_xml_sha256 for p in remapped_pages])
    geometry_capability = _document_geometry_capability(
        [p.page_geometry_capability for p in remapped_pages]
    )
    hover_eligible = geometry_capability == "VERIFIED" and all(
        p.page_geometry_capability == "VERIFIED" for p in remapped_pages
    )

    return ParsedSnapshotDocument(
        parser_version=PARSER_VERSION,
        canonical_text=document_canonical,
        canonical_text_sha256=compute_sha256_hex(document_canonical),
        provider_identity_fingerprint=provider_fp,
        raw_xml_fingerprint=raw_fp,
        geometry_capability=geometry_capability,
        hover_eligible=hover_eligible,
        pages=tuple(remapped_pages),
        warnings=tuple(all_warnings),
    )


# Re-export geometry error for callers that validate page maps alongside parse.
__all__ = [
    "PARSER_VERSION",
    "PAGE_SEPARATOR",
    "LINE_SEPARATOR",
    "TranskribusSnapshotParseError",
    "ParsedSnapshotLine",
    "ParsedSnapshotPage",
    "ParsedSnapshotDocument",
    "SnapshotPageInput",
    "compute_sha256_hex",
    "compute_provider_identity_fingerprint",
    "compute_raw_xml_fingerprint",
    "parse_page_xml_for_snapshot",
    "parse_document_pages_for_snapshot",
    "TranskribusPageXmlGeometryError",
]
