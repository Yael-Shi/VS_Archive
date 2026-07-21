"""Build SnapshotPageInput lists from production-selected PAGE XML pages.

Production ``PageImage.page_index`` is 1-based. Some historical test/run fixtures
store 0-based keys in ``TranskribusRun.page_index_to_page_nr``. Snapshot rows
require ``page_index >= 1``; conversion happens only at this boundary.
"""

from __future__ import annotations

from typing import Mapping, Sequence

from documents.services.transkribus_page_xml_types import (
    SelectedTranscriptPage,
    SnapshotPageInput,
)


class TranskribusPageMappingError(ValueError):
    """Trusted page_index ↔ pageNr mapping is missing, inconsistent, or ambiguous."""


def normalize_page_index_to_page_nr(
    raw_mapping: Mapping | None,
) -> dict[int, int]:
    """Return mapping with **snapshot/PageImage 1-based** page_index keys.

    Accepted shapes only:
    - dense ``0..N-1`` (legacy/test) → converted to ``1..N``;
    - dense ``1..N`` (production) → preserved.

    Rejects gaps, mixed bases, duplicate keys after integer coercion, and any
    other key set.
    """
    if not raw_mapping:
        raise TranskribusPageMappingError(
            "Trusted page_index_to_page_nr mapping is missing or empty."
        )
    parsed: dict[int, int] = {}
    seen_nrs: set[int] = set()
    for raw_index, raw_nr in dict(raw_mapping).items():
        if isinstance(raw_index, bool) or isinstance(raw_nr, bool):
            raise TranskribusPageMappingError(
                "Trusted page_index_to_page_nr must not use boolean keys/values."
            )
        try:
            page_index = int(raw_index)
            page_nr = int(raw_nr)
        except (TypeError, ValueError) as exc:
            raise TranskribusPageMappingError(
                "Trusted page_index_to_page_nr contains non-integer keys/values."
            ) from exc
        if page_index < 0:
            raise TranskribusPageMappingError(
                f"Trusted mapping page_index={page_index} is below 0."
            )
        if page_nr < 1:
            raise TranskribusPageMappingError(
                f"Trusted mapping pageNr={page_nr} is below 1."
            )
        if page_index in parsed:
            raise TranskribusPageMappingError(
                f"Trusted mapping has duplicate page_index={page_index} "
                "after integer coercion."
            )
        if page_nr in seen_nrs:
            raise TranskribusPageMappingError(
                f"Trusted mapping assigns duplicate pageNr={page_nr}."
            )
        seen_nrs.add(page_nr)
        parsed[page_index] = page_nr
    if not parsed:
        raise TranskribusPageMappingError(
            "Trusted page_index_to_page_nr mapping is missing or empty."
        )

    keys = sorted(parsed.keys())
    n = len(keys)
    if keys == list(range(0, n)):
        return {idx + 1: parsed[idx] for idx in keys}
    if keys == list(range(1, n + 1)):
        return {idx: parsed[idx] for idx in keys}
    raise TranskribusPageMappingError(
        f"Trusted mapping page_index keys must be dense 0..N-1 or 1..N, got {keys}."
    )


def snapshot_pages_from_upload_mapping(
    selected_pages: Sequence[SelectedTranscriptPage],
    page_index_to_page_nr: Mapping | None,
) -> list[SnapshotPageInput]:
    """Assign 1-based snapshot page_index from the run's trusted map (converted)."""
    index_to_nr = normalize_page_index_to_page_nr(page_index_to_page_nr)
    nr_to_index = {nr: idx for idx, nr in index_to_nr.items()}

    if not selected_pages:
        raise TranskribusPageMappingError("No selected PAGE XML pages to map.")

    selected_nrs = [p.page_nr for p in selected_pages]
    if len(selected_nrs) != len(set(selected_nrs)):
        raise TranskribusPageMappingError(
            "Selected PAGE XML pages contain duplicate pageNr values."
        )

    mapped_nrs = set(nr_to_index.keys())
    selected_nr_set = set(selected_nrs)
    if mapped_nrs != selected_nr_set:
        raise TranskribusPageMappingError(
            "Trusted page mapping pageNr set does not match selected transcript "
            f"pageNr set (mapped={sorted(mapped_nrs)}, selected={sorted(selected_nr_set)})."
        )

    out: list[SnapshotPageInput] = []
    for page in selected_pages:
        page_index = nr_to_index[page.page_nr]
        out.append(
            SnapshotPageInput(
                page_index=page_index,
                page_nr=page.page_nr,
                transcript_ts_id=page.transcript_ts_id,
                page_xml=page.page_xml,
                provider_page_id=page.provider_page_id,
                remote_transcript_status=page.remote_transcript_status,
            )
        )
    out.sort(key=lambda p: p.page_index)
    return out


def snapshot_pages_from_existing_server_traversal(
    selected_pages: Sequence[SelectedTranscriptPage],
) -> list[SnapshotPageInput]:
    """Assign page_index as 1..N in production selection/traversal order.

    Not a trusted DocumentSourceFile ↔ Trp mapping. Association.mapping_trusted
    must be False; do not mutate snapshot.hover_eligible after READY.
    """
    if not selected_pages:
        raise TranskribusPageMappingError("No selected PAGE XML pages to store.")

    seen_nrs: set[int] = set()
    out: list[SnapshotPageInput] = []
    for traversal_index, page in enumerate(selected_pages, start=1):
        if page.page_nr in seen_nrs:
            raise TranskribusPageMappingError(
                f"Selected PAGE XML pages contain duplicate pageNr={page.page_nr}."
            )
        seen_nrs.add(page.page_nr)
        out.append(
            SnapshotPageInput(
                page_index=traversal_index,
                page_nr=page.page_nr,
                transcript_ts_id=page.transcript_ts_id,
                page_xml=page.page_xml,
                provider_page_id=page.provider_page_id,
                remote_transcript_status=page.remote_transcript_status,
            )
        )
    return out
