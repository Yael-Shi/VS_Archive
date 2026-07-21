"""Dependency-neutral PAGE XML types shared by engine and snapshot services."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class SelectedTranscriptPage:
    """One PAGE XML page retained from the production transcript selector.

    ``page_index`` is assigned by the caller (trusted upload map or EXISTING_SERVER
    traversal order). Selection itself happens once in ``ordered_transcript_selections``.
    """

    page_nr: int
    transcript_ts_id: str
    page_xml: bytes
    url: str
    provider_page_id: int | None = None
    remote_transcript_status: str | None = None


@dataclass(frozen=True)
class SnapshotPageInput:
    """One PAGE XML page plus provider metadata for document-level parse."""

    page_index: int
    page_nr: int
    transcript_ts_id: str
    page_xml: bytes
    provider_page_id: int | None = None
    remote_transcript_status: str | None = None
