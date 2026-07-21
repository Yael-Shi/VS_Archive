from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Mapping, Sequence

import requests

from documents.models import Document, TranskribusRun
from documents.services.transkribus_page_xml_constants import PAGE_XML_NS

if TYPE_CHECKING:
    from documents.services.transkribus_engine import TrpPageMetadata
SAMPLE_TEXT_MAX_LEN = 120

LineGeometryCapability = str  # VERIFIED | PARTIAL | NOT_AVAILABLE | INDETERMINATE

_CONTROL_CHAR_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class TranskribusPageXmlGeometryError(ValueError):
    """Local validation failure for geometry audit (no remote call)."""


@dataclass(frozen=True)
class ParsedPoints:
    raw: str
    points: tuple[tuple[float, float], ...]
    malformed: bool
    degenerate: bool


@dataclass(frozen=True)
class BoundingBox:
    min_x: float
    min_y: float
    max_x: float
    max_y: float


@dataclass(frozen=True)
class LineSample:
    line_id: str
    text: str
    coords_points: str | None
    bounding_box: BoundingBox | None
    baseline_points: str | None


@dataclass(frozen=True)
class PageGeometryAudit:
    page_index: int
    page_nr: int
    provider_page_id: int | None
    transcript_ts_id: str | None
    transcript_job_id: str | None
    transcript_model_id: str | None
    mapping_trusted: bool
    page_namespace: str | None
    image_filename: str | None
    image_width: int | None
    image_height: int | None
    text_region_count: int
    text_line_count: int
    word_count: int
    reading_order_present: bool
    reading_order_resolved: bool
    lines_xml_order_differs_from_reading_order: int
    lines_with_non_empty_text: int
    lines_with_coords: int
    lines_with_parseable_coords: int
    lines_with_baseline: int
    lines_with_text_and_valid_coords: int
    lines_with_text_and_valid_baseline: int
    lines_with_provider_line_ids: int
    duplicate_line_ids: int
    malformed_polygons: int
    degenerate_polygons: int
    polygons_outside_page_bounds: int
    negative_or_outside_coordinates: int
    bounds_validation_available: bool
    sample_line: LineSample | None = None

    @property
    def page_capability(self) -> LineGeometryCapability:
        return _page_line_geometry_capability(self)


@dataclass(frozen=True)
class DocumentGeometryAudit:
    document_id: int
    transkribus_run_id: int
    remote_doc_id: str
    mapping_description: str
    page_mapping_reliable: bool
    pages: tuple[PageGeometryAudit, ...]
    warnings: tuple[str, ...] = field(default_factory=tuple)

    @property
    def line_geometry_capability(self) -> LineGeometryCapability:
        return _document_line_geometry_capability(self)

    @property
    def suitable_for_overlay_poc(self) -> bool:
        if not self.page_mapping_reliable:
            return False
        if self.line_geometry_capability != "VERIFIED":
            return False
        return all(
            page.polygons_outside_page_bounds == 0
            and page.negative_or_outside_coordinates == 0
            for page in self.pages
        )

    @property
    def baseline_only_fallback_needed(self) -> bool:
        if self.line_geometry_capability == "VERIFIED":
            return False
        return any(
            page.lines_with_text_and_valid_baseline > 0
            and page.lines_with_text_and_valid_coords < page.lines_with_non_empty_text
            for page in self.pages
        )

    @property
    def suitable_for_persistence_design(self) -> bool:
        return self.line_geometry_capability in {"VERIFIED", "PARTIAL"}


def _local_tag(tag: str) -> str:
    return tag.split("}")[-1] if "}" in tag else tag


def _ns_map(root: ET.Element) -> dict[str, str]:
    if root.tag.startswith("{"):
        uri = root.tag.split("}")[0][1:]
        if uri:
            return {"pc": uri}
    return {"pc": PAGE_XML_NS}


def _find_page_element(root: ET.Element, ns: dict[str, str]) -> ET.Element | None:
    page = root.find("pc:Page", ns)
    if page is not None:
        return page
    for el in root.iter():
        if _local_tag(el.tag) == "Page":
            return el
    return None


def _text_line_unicode(text_line: ET.Element, ns: dict[str, str]) -> str:
    unicode_el = text_line.find("pc:TextEquiv/pc:Unicode", ns)
    if unicode_el is not None and unicode_el.text:
        return unicode_el.text
    unicode_el = text_line.find("TextEquiv/Unicode")
    if unicode_el is not None and unicode_el.text:
        return unicode_el.text
    return ""


def _child_attr(el: ET.Element | None, attr: str) -> str | None:
    if el is None:
        return None
    raw = el.get(attr)
    if raw is None:
        return None
    stripped = raw.strip()
    return stripped or None


def _parse_int_attr(el: ET.Element | None, attr: str) -> int | None:
    raw = _child_attr(el, attr)
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


def _parse_coord_tokens(
    raw: str | None,
) -> tuple[str, tuple[tuple[float, float], ...], bool]:
    """Parse PAGE ``points`` attribute into coordinate pairs.

    Returns ``(raw, pairs, malformed)``.
    """
    if raw is None or not raw.strip():
        return raw or "", (), True

    pairs: list[tuple[float, float]] = []
    for token in raw.strip().split():
        if "," not in token:
            return raw, (), True
        xs, ys = token.split(",", 1)
        try:
            pairs.append((float(xs), float(ys)))
        except ValueError:
            return raw, (), True
    return raw, tuple(pairs), False


def _parse_polygon_points(raw: str | None) -> ParsedPoints:
    raw_value, pairs, malformed = _parse_coord_tokens(raw)
    if malformed:
        return ParsedPoints(raw=raw_value, points=(), malformed=True, degenerate=True)

    if len(pairs) < 3:
        return ParsedPoints(
            raw=raw_value,
            points=pairs,
            malformed=False,
            degenerate=True,
        )

    distinct = {(round(x, 6), round(y, 6)) for x, y in pairs}
    return ParsedPoints(
        raw=raw_value,
        points=pairs,
        malformed=False,
        degenerate=len(distinct) < 3,
    )


def _parse_baseline_points(raw: str | None) -> ParsedPoints:
    raw_value, pairs, malformed = _parse_coord_tokens(raw)
    if malformed:
        return ParsedPoints(raw=raw_value, points=(), malformed=True, degenerate=True)

    if len(pairs) < 2:
        return ParsedPoints(
            raw=raw_value,
            points=pairs,
            malformed=False,
            degenerate=True,
        )

    distinct = {(round(x, 6), round(y, 6)) for x, y in pairs}
    return ParsedPoints(
        raw=raw_value,
        points=pairs,
        malformed=False,
        degenerate=len(distinct) < 2,
    )


def _bounding_box(points: Sequence[tuple[float, float]]) -> BoundingBox | None:
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return BoundingBox(min(xs), min(ys), max(xs), max(ys))


def _points_outside_page(
    points: Sequence[tuple[float, float]],
    *,
    width: int | None,
    height: int | None,
) -> tuple[bool, bool]:
    """Return (negative_or_outside, outside_declared_bounds)."""
    if not points:
        return False, False
    negative_or_outside = False
    outside_bounds = False
    for x, y in points:
        if x < 0 or y < 0:
            negative_or_outside = True
        if width is not None and (x > width or x < 0):
            negative_or_outside = True
            outside_bounds = True
        if height is not None and (y > height or y < 0):
            negative_or_outside = True
            outside_bounds = True
    return negative_or_outside, outside_bounds


def _overlay_polygon_coords_in_bounds(
    points: Sequence[tuple[float, float]],
    *,
    width: int | None,
    height: int | None,
) -> tuple[bool, bool, bool]:
    """Return ``(coords_in_bounds, negative_or_outside, outside_declared_bounds)``.

    Negative coordinates invalidate overlay geometry even when page dimensions are
    unknown. Overflow above declared width/height is evaluated only when both
    dimensions are present.
    """
    if not points:
        return True, False, False
    negative_or_outside, outside_declared_bounds = _points_outside_page(
        points,
        width=width,
        height=height,
    )
    bounds_validation_available = width is not None and height is not None
    coords_in_bounds = True
    if negative_or_outside:
        coords_in_bounds = False
    elif bounds_validation_available and outside_declared_bounds:
        coords_in_bounds = False
    reported_outside = outside_declared_bounds if bounds_validation_available else False
    return coords_in_bounds, negative_or_outside, reported_outside


def _collect_text_lines(page_el: ET.Element, ns: dict[str, str]) -> list[ET.Element]:
    lines = page_el.findall(".//pc:TextLine", ns)
    if lines:
        return lines
    return [el for el in page_el.iter() if _local_tag(el.tag) == "TextLine"]


def _count_elements(page_el: ET.Element, ns: dict[str, str], name: str) -> int:
    tagged = page_el.findall(f".//pc:{name}", ns)
    if tagged:
        return len(tagged)
    return sum(1 for el in page_el.iter() if _local_tag(el.tag) == name)


def _reading_order_line_ids(
    page_el: ET.Element, ns: dict[str, str]
) -> list[str] | None:
    """Resolve explicit TextLine ids referenced by ReadingOrder, when possible.

    Unresolved reading order does not prove absence or invalidity of PAGE reading
    order; only explicitly resolvable line references are compared to XML order.
    """
    reading_order = page_el.find("pc:ReadingOrder", ns)
    if reading_order is None:
        for el in page_el:
            if _local_tag(el.tag) == "ReadingOrder":
                reading_order = el
                break
    if reading_order is None:
        return None

    ordered_ids: list[str] = []

    def walk(group: ET.Element) -> None:
        for child in group:
            tag = _local_tag(child.tag)
            if tag in {"RegionRef", "LineRef"}:
                ref = (
                    child.get("regionRef") or child.get("lineRef") or child.get("idRef")
                )
                if ref:
                    ordered_ids.append(ref.strip())
            elif tag == "OrderedGroup":
                walk(child)

    walk(reading_order)
    if not ordered_ids:
        return None

    line_ids_in_doc = {
        (_child_attr(line, "id") or "") for line in _collect_text_lines(page_el, ns)
    }
    line_ids_in_doc.discard("")

    resolved_line_ids = [rid for rid in ordered_ids if rid in line_ids_in_doc]
    if not resolved_line_ids:
        return None
    return resolved_line_ids


def sanitize_sample_text(text: str, *, max_len: int = SAMPLE_TEXT_MAX_LEN) -> str:
    cleaned = _CONTROL_CHAR_RE.sub(" ", text or "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    if len(cleaned) <= max_len:
        return cleaned
    return cleaned[: max_len - 3] + "..."


def analyze_page_xml_geometry(
    page_xml: bytes,
    *,
    page_index: int,
    page_nr: int,
    provider_page_id: int | None = None,
    transcript_ts_id: str | None = None,
    transcript_job_id: str | None = None,
    transcript_model_id: str | None = None,
    mapping_trusted: bool = True,
    include_sample_text: bool = False,
) -> PageGeometryAudit:
    try:
        root = ET.fromstring(page_xml)
    except ET.ParseError as exc:
        raise TranskribusPageXmlGeometryError(
            f"Invalid PAGE XML for page_index={page_index}"
        ) from exc

    ns = _ns_map(root)
    page_el = _find_page_element(root, ns)
    if page_el is None:
        raise TranskribusPageXmlGeometryError(
            f"PAGE XML missing Page element for page_index={page_index}"
        )

    page_namespace = root.tag.split("}")[0][1:] if root.tag.startswith("{") else None
    image_filename = _child_attr(page_el, "imageFilename")
    image_width = _parse_int_attr(page_el, "imageWidth")
    image_height = _parse_int_attr(page_el, "imageHeight")
    bounds_validation_available = image_width is not None and image_height is not None

    text_lines = _collect_text_lines(page_el, ns)
    reading_order_ids = _reading_order_line_ids(page_el, ns)
    reading_order_present = reading_order_ids is not None or any(
        _local_tag(el.tag) == "ReadingOrder" for el in page_el
    )

    xml_order_ids: list[str] = []
    seen_ids: dict[str, int] = {}
    duplicate_line_ids = 0

    lines_with_non_empty_text = 0
    lines_with_coords = 0
    lines_with_parseable_coords = 0
    lines_with_baseline = 0
    lines_with_text_and_valid_coords = 0
    lines_with_text_and_valid_baseline = 0
    lines_with_provider_line_ids = 0
    malformed_polygons = 0
    degenerate_polygons = 0
    polygons_outside_page_bounds = 0
    negative_or_outside_coordinates = 0

    sample_line: LineSample | None = None

    for line_el in text_lines:
        line_id = _child_attr(line_el, "id") or ""
        if line_id:
            lines_with_provider_line_ids += 1
            xml_order_ids.append(line_id)
            seen_ids[line_id] = seen_ids.get(line_id, 0) + 1

        unicode_text = _text_line_unicode(line_el, ns)
        has_text = bool(unicode_text.strip())
        if has_text:
            lines_with_non_empty_text += 1

        coords_el = line_el.find("pc:Coords", ns)
        if coords_el is None:
            for child in line_el:
                if _local_tag(child.tag) == "Coords":
                    coords_el = child
                    break

        coords_raw = _child_attr(coords_el, "points") if coords_el is not None else None
        if coords_el is not None and coords_raw is not None:
            lines_with_coords += 1
        parsed_coords = _parse_polygon_points(coords_raw)
        if (
            coords_el is not None
            and coords_raw is not None
            and not parsed_coords.malformed
        ):
            lines_with_parseable_coords += 1
        if parsed_coords.malformed and coords_el is not None and coords_raw:
            malformed_polygons += 1
        elif parsed_coords.degenerate and coords_el is not None and coords_raw:
            degenerate_polygons += 1

        baseline_el = line_el.find("pc:Baseline", ns)
        if baseline_el is None:
            for child in line_el:
                if _local_tag(child.tag) == "Baseline":
                    baseline_el = child
                    break
        baseline_raw = (
            _child_attr(baseline_el, "points") if baseline_el is not None else None
        )
        parsed_baseline = _parse_baseline_points(baseline_raw)
        if baseline_el is not None and baseline_raw and not parsed_baseline.malformed:
            lines_with_baseline += 1

        coords_structurally_valid = (
            coords_el is not None
            and coords_raw
            and not parsed_coords.malformed
            and not parsed_coords.degenerate
        )
        baseline_valid = (
            baseline_el is not None
            and baseline_raw
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

        if has_text and coords_valid:
            lines_with_text_and_valid_coords += 1
        if has_text and baseline_valid:
            lines_with_text_and_valid_baseline += 1

        if include_sample_text and sample_line is None and has_text and coords_valid:
            sample_line = LineSample(
                line_id=line_id or "(missing)",
                text=sanitize_sample_text(unicode_text),
                coords_points=parsed_coords.raw,
                bounding_box=_bounding_box(parsed_coords.points),
                baseline_points=parsed_baseline.raw if baseline_valid else None,
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

    return PageGeometryAudit(
        page_index=page_index,
        page_nr=page_nr,
        provider_page_id=provider_page_id,
        transcript_ts_id=transcript_ts_id,
        transcript_job_id=transcript_job_id,
        transcript_model_id=transcript_model_id,
        mapping_trusted=mapping_trusted,
        page_namespace=page_namespace,
        image_filename=image_filename,
        image_width=image_width,
        image_height=image_height,
        text_region_count=_count_elements(page_el, ns, "TextRegion"),
        text_line_count=len(text_lines),
        word_count=_count_elements(page_el, ns, "Word"),
        reading_order_present=reading_order_present,
        reading_order_resolved=reading_order_resolved,
        lines_xml_order_differs_from_reading_order=lines_xml_order_differs,
        lines_with_non_empty_text=lines_with_non_empty_text,
        lines_with_coords=lines_with_coords,
        lines_with_parseable_coords=lines_with_parseable_coords,
        lines_with_baseline=lines_with_baseline,
        lines_with_text_and_valid_coords=lines_with_text_and_valid_coords,
        lines_with_text_and_valid_baseline=lines_with_text_and_valid_baseline,
        lines_with_provider_line_ids=lines_with_provider_line_ids,
        duplicate_line_ids=duplicate_line_ids,
        malformed_polygons=malformed_polygons,
        degenerate_polygons=degenerate_polygons,
        polygons_outside_page_bounds=polygons_outside_page_bounds,
        negative_or_outside_coordinates=negative_or_outside_coordinates,
        bounds_validation_available=bounds_validation_available,
        sample_line=sample_line,
    )


def _page_line_geometry_capability(page: PageGeometryAudit) -> LineGeometryCapability:
    if not page.mapping_trusted:
        return "INDETERMINATE"
    if page.lines_with_non_empty_text == 0:
        return "INDETERMINATE"
    if (
        page.bounds_validation_available
        and page.lines_with_text_and_valid_coords == page.lines_with_non_empty_text
        and page.lines_with_non_empty_text > 0
        and page.polygons_outside_page_bounds == 0
        and page.negative_or_outside_coordinates == 0
    ):
        return "VERIFIED"
    if (
        page.lines_with_text_and_valid_coords > 0
        or page.lines_with_parseable_coords > 0
    ):
        return "PARTIAL"
    if page.text_line_count > 0 or page.lines_with_non_empty_text > 0:
        return "NOT_AVAILABLE"
    return "INDETERMINATE"


def _document_line_geometry_capability(
    audit: DocumentGeometryAudit,
) -> LineGeometryCapability:
    if not audit.page_mapping_reliable or not audit.pages:
        return "INDETERMINATE"

    caps = {page.page_capability for page in audit.pages}
    if caps == {"VERIFIED"}:
        return "VERIFIED"
    if "INDETERMINATE" in caps and caps <= {"INDETERMINATE"}:
        return "INDETERMINATE"
    if audit.pages and all(
        page.page_capability == "NOT_AVAILABLE" for page in audit.pages
    ):
        return "NOT_AVAILABLE"
    if "VERIFIED" in caps or "PARTIAL" in caps:
        return "PARTIAL"
    return "INDETERMINATE"


def _normalize_page_index_map(raw: Any) -> dict[int, int]:
    if not isinstance(raw, dict) or not raw:
        raise TranskribusPageXmlGeometryError(
            "TranskribusRun.page_index_to_page_nr is missing or empty."
        )
    out: dict[int, int] = {}
    seen_page_nrs: dict[int, int] = {}
    for key, value in raw.items():
        try:
            page_index = int(key)
            page_nr = int(value)
        except (TypeError, ValueError) as exc:
            raise TranskribusPageXmlGeometryError(
                "TranskribusRun.page_index_to_page_nr contains non-integer keys/values."
            ) from exc
        if page_index < 1:
            raise TranskribusPageXmlGeometryError(
                "TranskribusRun.page_index_to_page_nr contains page_index below 1."
            )
        if page_nr < 1:
            raise TranskribusPageXmlGeometryError(
                "TranskribusRun.page_index_to_page_nr contains Transkribus pageNr below 1."
            )
        if page_nr in seen_page_nrs and seen_page_nrs[page_nr] != page_index:
            raise TranskribusPageXmlGeometryError(
                "TranskribusRun.page_index_to_page_nr assigns duplicate Transkribus "
                f"pageNr={page_nr} to multiple local page indexes."
            )
        seen_page_nrs[page_nr] = page_index
        out[page_index] = page_nr
    return out


def resolve_audit_transkribus_run(document_id: int) -> TranskribusRun:
    if not Document.objects.filter(pk=document_id).exists():
        raise TranskribusPageXmlGeometryError(
            f"Document id={document_id} does not exist."
        )

    runs = TranskribusRun.objects.filter(document_id=document_id).order_by(
        "-created_at", "-id"
    )
    if not runs.exists():
        raise TranskribusPageXmlGeometryError(
            f"Document id={document_id} has no TranskribusRun rows."
        )

    for run in runs:
        if run.mode == TranskribusRun.Mode.EXISTING_SERVER:
            continue
        if run.mode != TranskribusRun.Mode.UPLOAD_CREATED:
            continue
        if run.status != TranskribusRun.Status.SUCCEEDED:
            continue
        if not (run.remote_doc_id or "").strip():
            continue
        if not (run.pages_query or "").strip():
            continue
        if not (run.recognition_job_id or "").strip():
            continue
        try:
            _normalize_page_index_map(run.page_index_to_page_nr)
        except TranskribusPageXmlGeometryError:
            continue
        return run

    if runs.filter(mode=TranskribusRun.Mode.EXISTING_SERVER).exists():
        raise TranskribusPageXmlGeometryError(
            "Document has EXISTING_SERVER TranskribusRun rows only. "
            "Local page mapping is not trusted for geometry audit; "
            "use an UPLOAD_CREATED run with page_index_to_page_nr."
        )

    raise TranskribusPageXmlGeometryError(
        "No usable UPLOAD_CREATED TranskribusRun with remote_doc_id, pages_query, "
        "recognition_job_id, and page_index_to_page_nr was found."
    )


def resolve_page_indices_to_audit(
    page_index_map: Mapping[int, int],
    *,
    page_index: int | None,
) -> list[int]:
    if page_index is not None:
        if page_index < 1:
            raise TranskribusPageXmlGeometryError(
                "--page-index must be a 1-based positive integer."
            )
        if page_index not in page_index_map:
            raise TranskribusPageXmlGeometryError(
                f"--page-index={page_index} is not present in "
                "TranskribusRun.page_index_to_page_nr."
            )
        return [page_index]
    return sorted(page_index_map.keys())


def _page_metadata_by_nr(
    pages_meta: Sequence[TrpPageMetadata], page_nr: int
) -> TrpPageMetadata:
    from documents.services import transkribus_engine as tr

    matches = [pm for pm in pages_meta if pm.page_nr == page_nr]
    if len(matches) == 1:
        return matches[0]
    if not matches:
        raise tr.TranskribusPermanentError(
            f"Transkribus pages metadata missing pageNr={page_nr}"
        )
    raise tr.TranskribusPermanentError(
        f"Transkribus pages metadata returned duplicate pageNr={page_nr}"
    )


def _safe_transcript_identifiers(
    chosen: Mapping[str, Any],
) -> tuple[str | None, str | None, str | None]:
    ts_id = chosen.get("tsId")
    job_id = chosen.get("jobId")
    model_id = chosen.get("modelId")
    return (
        str(ts_id).strip() if ts_id is not None and str(ts_id).strip() else None,
        str(job_id).strip() if job_id is not None and str(job_id).strip() else None,
        str(model_id).strip()
        if model_id is not None and str(model_id).strip()
        else None,
    )


def fetch_document_geometry_audit(
    *,
    document_id: int,
    page_index: int | None = None,
    include_sample_text: bool = False,
    username: str,
    password: str,
    bearer_token: str,
    session_factory: Callable[[], requests.Session] | None = None,
    fetch_xml: Callable[..., bytes] | None = None,
    fetch_pages_metadata: Callable[..., list[TrpPageMetadata]] | None = None,
    login: Callable[..., None] | None = None,
) -> DocumentGeometryAudit:
    from documents.services import transkribus_engine as tr

    run = resolve_audit_transkribus_run(document_id)
    page_index_map = _normalize_page_index_map(run.page_index_to_page_nr)
    page_indices = resolve_page_indices_to_audit(page_index_map, page_index=page_index)

    session_factory = session_factory or requests.Session
    fetch_xml_fn = fetch_xml or tr.fetch_transcript_xml
    fetch_pages_fn = fetch_pages_metadata or tr.fetch_pages_metadata
    login_fn = login or tr.login_trp_server

    pages: list[PageGeometryAudit] = []
    with session_factory() as session:
        login_fn(session, username=username, password=password)
        pages_meta = fetch_pages_fn(
            session,
            collection_id=run.collection_id,
            document_id=str(run.remote_doc_id).strip(),
            pages_query=str(run.pages_query).strip(),
        )
        if not pages_meta:
            raise tr.TranskribusPermanentError(
                "Transkribus pages metadata returned empty list"
            )

        for local_page_index in page_indices:
            page_nr = page_index_map[local_page_index]
            pm = _page_metadata_by_nr(pages_meta, page_nr)
            chosen = tr.pick_transcript(
                pm.transcripts,
                job_id=str(run.recognition_job_id).strip(),
                model_id=str(run.model_id).strip(),
            )
            if chosen is None:
                raise tr.TranskribusPermanentError(
                    f"No transcript matched job/model for pageNr={page_nr}"
                )
            url = chosen.get("url")
            if not url or not isinstance(url, str):
                raise tr.TranskribusPermanentError(
                    f"Transcript URL missing for pageNr={page_nr}"
                )
            xml_bytes = fetch_xml_fn(
                url,
                bearer_token=bearer_token,
            )
            ts_id, job_id, model_id = _safe_transcript_identifiers(chosen)
            pages.append(
                analyze_page_xml_geometry(
                    xml_bytes,
                    page_index=local_page_index,
                    page_nr=page_nr,
                    provider_page_id=pm.page_id,
                    transcript_ts_id=ts_id,
                    transcript_job_id=job_id,
                    transcript_model_id=model_id,
                    mapping_trusted=True,
                    include_sample_text=include_sample_text,
                )
            )

    warnings = (
        "This audit inspects one document sample only; do not generalize to all "
        "Transkribus documents or routes.",
        "Verdict reflects stored UPLOAD_CREATED page_index_to_page_nr mapping only.",
        "Unresolved PAGE reading order does not prove absence or invalidity of "
        "reading order; only explicitly resolvable line references are compared.",
    )
    return DocumentGeometryAudit(
        document_id=document_id,
        transkribus_run_id=run.id,
        remote_doc_id=str(run.remote_doc_id).strip(),
        mapping_description="trusted upload-created mapping",
        page_mapping_reliable=True,
        pages=tuple(pages),
        warnings=warnings,
    )


def audit_to_json_dict(audit: DocumentGeometryAudit) -> dict[str, Any]:
    return {
        "document_id": audit.document_id,
        "transkribus_run_id": audit.transkribus_run_id,
        "remote_doc_id": audit.remote_doc_id,
        "mapping_description": audit.mapping_description,
        "page_mapping_reliable": audit.page_mapping_reliable,
        "pages": [_page_audit_to_dict(page) for page in audit.pages],
        "verdict": {
            "line_geometry_capability": audit.line_geometry_capability,
            "page_mapping_reliable": audit.page_mapping_reliable,
            "suitable_for_overlay_poc": audit.suitable_for_overlay_poc,
            "baseline_only_fallback_needed": audit.baseline_only_fallback_needed,
            "suitable_for_persistence_design": audit.suitable_for_persistence_design,
        },
        "warnings": list(audit.warnings),
    }


def _page_audit_to_dict(page: PageGeometryAudit) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "page_index": page.page_index,
        "page_nr": page.page_nr,
        "provider_page_id": page.provider_page_id,
        "transcript_ts_id": page.transcript_ts_id,
        "transcript_job_id": page.transcript_job_id,
        "transcript_model_id": page.transcript_model_id,
        "mapping_trusted": page.mapping_trusted,
        "page_namespace": page.page_namespace,
        "image_filename": page.image_filename,
        "image_width": page.image_width,
        "image_height": page.image_height,
        "text_region_count": page.text_region_count,
        "text_line_count": page.text_line_count,
        "word_count": page.word_count,
        "reading_order_present": page.reading_order_present,
        "reading_order_resolved": page.reading_order_resolved,
        "lines_xml_order_differs_from_reading_order": (
            page.lines_xml_order_differs_from_reading_order
        ),
        "lines_with_non_empty_text": page.lines_with_non_empty_text,
        "lines_with_coords": page.lines_with_coords,
        "lines_with_parseable_coords": page.lines_with_parseable_coords,
        "lines_with_baseline": page.lines_with_baseline,
        "lines_with_text_and_valid_coords": page.lines_with_text_and_valid_coords,
        "lines_with_text_and_valid_baseline": page.lines_with_text_and_valid_baseline,
        "lines_with_provider_line_ids": page.lines_with_provider_line_ids,
        "duplicate_line_ids": page.duplicate_line_ids,
        "malformed_polygons": page.malformed_polygons,
        "degenerate_polygons": page.degenerate_polygons,
        "polygons_outside_page_bounds": page.polygons_outside_page_bounds,
        "negative_or_outside_coordinates": page.negative_or_outside_coordinates,
        "bounds_validation_available": page.bounds_validation_available,
        "page_capability": page.page_capability,
    }
    if page.sample_line is not None:
        payload["sample_line"] = {
            "line_id": page.sample_line.line_id,
            "text": page.sample_line.text,
            "coords_points": page.sample_line.coords_points,
            "bounding_box": (
                {
                    "min_x": page.sample_line.bounding_box.min_x,
                    "min_y": page.sample_line.bounding_box.min_y,
                    "max_x": page.sample_line.bounding_box.max_x,
                    "max_y": page.sample_line.bounding_box.max_y,
                }
                if page.sample_line.bounding_box is not None
                else None
            ),
            "baseline_points": page.sample_line.baseline_points,
        }
    return payload
