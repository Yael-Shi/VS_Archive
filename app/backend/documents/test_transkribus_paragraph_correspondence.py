"""Strict cross-snapshot correspondence and historical suggestion discovery tests."""

from __future__ import annotations

import hashlib
from datetime import timedelta

from django.test import TestCase
from django.utils import timezone

from documents.models import (
    Document,
    TranskribusParagraphMapping,
    TranskribusSnapshotLine,
    TranskribusSnapshotPage,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_items import create_ocr_document
from documents.services.transkribus_paragraph_correspondence import (
    CorrespondenceRefusal,
    discover_transferable_historical_mappings,
    prove_contributing_line_correspondence,
)
from documents.services.transkribus_paragraph_mapping import save_paragraph_mapping

_PARSER = "test_paragraph_correspondence_v1"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _create_doc(**kwargs) -> Document:
    defaults = dict(
        title="Paragraph correspondence doc",
        doc_type=Document.DocType.PDF,
        language=Document.Language.HEBREW,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        upload_status=Document.UploadStatus.UPLOADED,
        file_s3_key="paragraph-correspondence.pdf",
        mime_type="application/pdf",
    )
    defaults.update(kwargs)
    return create_ocr_document(**defaults)


def _ready_snapshot(
    document: Document,
    *,
    text: str,
    parser_version: str = _PARSER,
    storage_status: str = TranskribusTranscriptSnapshot.StorageStatus.READY,
    created_at=None,
) -> TranskribusTranscriptSnapshot:
    unique = f"{document.pk}:{TranskribusTranscriptSnapshot.objects.count()}:{text}"
    snapshot = TranskribusTranscriptSnapshot.objects.create(
        document=document,
        source_kind=TranskribusTranscriptSnapshot.SourceKind.CORRECTED_CURRENT_SYNC,
        parser_version=parser_version,
        provider_identity_fingerprint=_sha(f"prov:{unique}"),
        raw_xml_fingerprint=_sha(f"raw:{unique}"),
        canonical_text=text,
        canonical_text_sha256=_sha(text),
        geometry_capability=TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED,
        hover_eligible=True,
        storage_status=storage_status,
    )
    if created_at is not None:
        TranskribusTranscriptSnapshot.objects.filter(pk=snapshot.pk).update(
            created_at=created_at
        )
        snapshot.refresh_from_db()
    return snapshot


def _add_page(
    snapshot: TranskribusTranscriptSnapshot,
    page_index: int,
) -> TranskribusSnapshotPage:
    return TranskribusSnapshotPage.objects.create(
        snapshot=snapshot,
        page_index=page_index,
        page_nr=page_index,
        transcript_ts_id=f"ts-{snapshot.pk}-{page_index}",
        page_geometry_capability=TranskribusSnapshotPage.GeometryCapability.VERIFIED,
    )


def _add_line(
    page: TranskribusSnapshotPage,
    order: int,
    text: str,
    *,
    start: int,
    end: int,
    provider_line_id: str,
    contributes: bool = True,
) -> TranskribusSnapshotLine:
    return TranskribusSnapshotLine.objects.create(
        page=page,
        order_index=order,
        provider_region_id="region-ignored",
        provider_line_id=provider_line_id,
        text=text,
        contributes_to_canonical=contributes,
        char_start=start,
        char_end=end,
    )


def _two_page_snapshot(
    document: Document,
    *,
    lines: tuple[tuple[str, str], ...],
    text: str,
    parser_version: str = _PARSER,
    storage_status: str = TranskribusTranscriptSnapshot.StorageStatus.READY,
    created_at=None,
    extra_page: bool = False,
    skip_second_page: bool = False,
) -> tuple[TranskribusTranscriptSnapshot, list[TranskribusSnapshotLine]]:
    snapshot = _ready_snapshot(
        document,
        text=text,
        parser_version=parser_version,
        storage_status=storage_status,
        created_at=created_at,
    )
    page1 = _add_page(snapshot, 1)
    created_lines = [
        _add_line(
            page1,
            0,
            lines[0][1],
            start=0,
            end=max(len(lines[0][1]), 1),
            provider_line_id=lines[0][0],
        ),
        _add_line(
            page1,
            1,
            lines[1][1],
            start=10,
            end=10 + max(len(lines[1][1]), 1),
            provider_line_id=lines[1][0],
        ),
    ]
    if not skip_second_page:
        page2 = _add_page(snapshot, 2)
        created_lines.append(
            _add_line(
                page2,
                0,
                lines[2][1],
                start=20,
                end=20 + max(len(lines[2][1]), 1),
                provider_line_id=lines[2][0],
            )
        )
    if extra_page:
        page3 = _add_page(snapshot, 3)
        created_lines.append(
            _add_line(
                page3,
                0,
                "extra",
                start=30,
                end=35,
                provider_line_id="line-extra",
            )
        )
    return snapshot, created_lines


_SOURCE_LINES = (("line-a", "Alpha"), ("line-b", "Beta"), ("line-c", "Gamma"))
_TARGET_LINES = (("line-a", "Alfa"), ("line-b", "Betta"), ("line-c", "Gama"))


class ContributingLineCorrespondenceTests(TestCase):
    def setUp(self) -> None:
        self.doc = _create_doc()

    def test_allows_matching_identities_with_different_text(self):
        source, source_lines = _two_page_snapshot(
            self.doc,
            lines=_SOURCE_LINES,
            text="Alpha\nBeta\nGamma",
        )
        target, target_lines = _two_page_snapshot(
            self.doc,
            lines=_TARGET_LINES,
            text="Alfa\nBetta\nGama",
        )
        proof = prove_contributing_line_correspondence(source, target)
        self.assertTrue(proof.compatible)
        self.assertIsNone(proof.refusal_reason)
        self.assertEqual(len(proof.line_correspondence), 3)
        self.assertEqual(
            proof.line_correspondence[0].source_line_id, source_lines[0].pk
        )
        self.assertEqual(
            proof.line_correspondence[0].target_line_id, target_lines[0].pk
        )
        self.assertNotEqual(source_lines[0].pk, target_lines[0].pk)
        self.assertNotEqual(source_lines[0].text, target_lines[0].text)
        self.assertEqual(proof.line_correspondence[0].provider_line_id, "line-a")
        self.assertEqual(proof.line_correspondence[0].page_index, 1)
        self.assertEqual(proof.line_correspondence[2].page_index, 2)

    def test_refuses_different_document(self):
        other = _create_doc(
            title="Other",
            file_s3_key="paragraph-correspondence-other.pdf",
        )
        source, _ = _two_page_snapshot(self.doc, lines=_SOURCE_LINES, text="A\nB\nC")
        target, _ = _two_page_snapshot(other, lines=_TARGET_LINES, text="A2\nB2\nC2")
        proof = prove_contributing_line_correspondence(source, target)
        self.assertFalse(proof.compatible)
        self.assertEqual(proof.refusal_reason, CorrespondenceRefusal.DIFFERENT_DOCUMENT)
        self.assertEqual(proof.line_correspondence, ())

    def test_refuses_non_ready_snapshot(self):
        source, _ = _two_page_snapshot(self.doc, lines=_SOURCE_LINES, text="A\nB\nC")
        target, _ = _two_page_snapshot(
            self.doc,
            lines=_TARGET_LINES,
            text="A2\nB2\nC2",
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.PENDING_UPLOAD,
        )
        proof = prove_contributing_line_correspondence(source, target)
        self.assertFalse(proof.compatible)
        self.assertEqual(proof.refusal_reason, CorrespondenceRefusal.TARGET_NOT_READY)

        pending_source, _ = _two_page_snapshot(
            self.doc,
            lines=_SOURCE_LINES,
            text="P\nB\nC",
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.FAILED,
        )
        ready_target, _ = _two_page_snapshot(
            self.doc,
            lines=_TARGET_LINES,
            text="R\nB\nC",
        )
        source_proof = prove_contributing_line_correspondence(
            pending_source,
            ready_target,
        )
        self.assertEqual(
            source_proof.refusal_reason,
            CorrespondenceRefusal.SOURCE_NOT_READY,
        )

    def test_refuses_parser_mismatch(self):
        source, _ = _two_page_snapshot(self.doc, lines=_SOURCE_LINES, text="A\nB\nC")
        target, _ = _two_page_snapshot(
            self.doc,
            lines=_TARGET_LINES,
            text="A2\nB2\nC2",
            parser_version="other_parser_v9",
        )
        proof = prove_contributing_line_correspondence(source, target)
        self.assertEqual(
            proof.refusal_reason,
            CorrespondenceRefusal.PARSER_VERSION_MISMATCH,
        )

    def test_refuses_page_structure_mismatch(self):
        source, _ = _two_page_snapshot(self.doc, lines=_SOURCE_LINES, text="A\nB\nC")
        target, _ = _two_page_snapshot(
            self.doc,
            lines=_TARGET_LINES + (("line-extra", "X"),),
            text="A2\nB2\nC2\nX",
            extra_page=True,
        )
        proof = prove_contributing_line_correspondence(source, target)
        self.assertEqual(
            proof.refusal_reason,
            CorrespondenceRefusal.PAGE_STRUCTURE_MISMATCH,
        )

    def test_refuses_contributing_line_count_mismatch(self):
        source, _ = _two_page_snapshot(self.doc, lines=_SOURCE_LINES, text="A\nB\nC")
        target, _ = _two_page_snapshot(
            self.doc,
            lines=_TARGET_LINES,
            text="A2\nB2",
            skip_second_page=True,
        )
        page2 = _add_page(target, 2)
        _add_line(
            page2,
            0,
            "kept-non-contributing",
            start=20,
            end=21,
            provider_line_id="line-c",
            contributes=False,
        )
        proof = prove_contributing_line_correspondence(source, target)
        self.assertEqual(
            proof.refusal_reason,
            CorrespondenceRefusal.CONTRIBUTING_LINE_COUNT_MISMATCH,
        )

    def test_refuses_missing_provider_line_id(self):
        source, source_lines = _two_page_snapshot(
            self.doc,
            lines=_SOURCE_LINES,
            text="A\nB\nC",
        )
        target, _ = _two_page_snapshot(self.doc, lines=_TARGET_LINES, text="A2\nB2\nC2")
        source_lines[1].provider_line_id = ""
        source_lines[1].save(update_fields=["provider_line_id"])
        proof = prove_contributing_line_correspondence(source, target)
        self.assertEqual(
            proof.refusal_reason,
            CorrespondenceRefusal.MISSING_PROVIDER_LINE_ID,
        )

    def test_refuses_duplicate_provider_line_id(self):
        source, source_lines = _two_page_snapshot(
            self.doc,
            lines=_SOURCE_LINES,
            text="A\nB\nC",
        )
        target, _ = _two_page_snapshot(self.doc, lines=_TARGET_LINES, text="A2\nB2\nC2")
        source_lines[1].provider_line_id = source_lines[0].provider_line_id
        source_lines[1].save(update_fields=["provider_line_id"])
        proof = prove_contributing_line_correspondence(source, target)
        self.assertEqual(
            proof.refusal_reason,
            CorrespondenceRefusal.DUPLICATE_PROVIDER_LINE_ID,
        )

    def test_refuses_missing_provider_line_id_on_later_page(self):
        source, source_lines = _two_page_snapshot(
            self.doc,
            lines=_SOURCE_LINES,
            text="A\nB\nC",
        )
        target, _ = _two_page_snapshot(self.doc, lines=_TARGET_LINES, text="A2\nB2\nC2")
        later_line = source_lines[2]
        self.assertEqual(later_line.page.page_index, 2)
        later_line.provider_line_id = ""
        later_line.save(update_fields=["provider_line_id"])
        proof = prove_contributing_line_correspondence(source, target)
        self.assertEqual(
            proof.refusal_reason,
            CorrespondenceRefusal.MISSING_PROVIDER_LINE_ID,
        )

    def test_refuses_duplicate_provider_line_id_on_later_page(self):
        source, source_lines = _two_page_snapshot(
            self.doc,
            lines=_SOURCE_LINES,
            text="A\nB\nC",
        )
        target, _ = _two_page_snapshot(self.doc, lines=_TARGET_LINES, text="A2\nB2\nC2")
        later_line = source_lines[2]
        self.assertEqual(later_line.page.page_index, 2)
        _add_line(
            later_line.page,
            1,
            "Extra",
            start=30,
            end=35,
            provider_line_id=later_line.provider_line_id,
        )
        proof = prove_contributing_line_correspondence(source, target)
        self.assertEqual(
            proof.refusal_reason,
            CorrespondenceRefusal.DUPLICATE_PROVIDER_LINE_ID,
        )

    def test_refuses_reordered_identity_sequence(self):
        source, _ = _two_page_snapshot(self.doc, lines=_SOURCE_LINES, text="A\nB\nC")
        reordered = (("line-b", "Alfa"), ("line-a", "Betta"), ("line-c", "Gama"))
        target, _ = _two_page_snapshot(self.doc, lines=reordered, text="A2\nB2\nC2")
        proof = prove_contributing_line_correspondence(source, target)
        self.assertEqual(
            proof.refusal_reason,
            CorrespondenceRefusal.IDENTITY_SEQUENCE_MISMATCH,
        )

    def test_refuses_changed_id(self):
        source, _ = _two_page_snapshot(self.doc, lines=_SOURCE_LINES, text="A\nB\nC")
        changed = (("line-a", "Alfa"), ("line-b-changed", "Betta"), ("line-c", "Gama"))
        target, _ = _two_page_snapshot(self.doc, lines=changed, text="A2\nB2\nC2")
        proof = prove_contributing_line_correspondence(source, target)
        self.assertEqual(
            proof.refusal_reason,
            CorrespondenceRefusal.IDENTITY_SEQUENCE_MISMATCH,
        )

    def test_refuses_same_line_count_with_different_identities(self):
        source, _ = _two_page_snapshot(self.doc, lines=_SOURCE_LINES, text="A\nB\nC")
        different = (("line-x", "Alfa"), ("line-y", "Betta"), ("line-z", "Gama"))
        target, _ = _two_page_snapshot(self.doc, lines=different, text="A2\nB2\nC2")
        proof = prove_contributing_line_correspondence(source, target)
        self.assertEqual(
            proof.refusal_reason,
            CorrespondenceRefusal.IDENTITY_SEQUENCE_MISMATCH,
        )
        self.assertEqual(proof.line_correspondence, ())


class HistoricalSuggestionDiscoveryTests(TestCase):
    def setUp(self) -> None:
        self.doc = _create_doc()
        self.now = timezone.now()

    def _snapshot_at(self, *, hours_ago: int, text: str, lines=_SOURCE_LINES):
        return _two_page_snapshot(
            self.doc,
            lines=lines,
            text=text,
            created_at=self.now - timedelta(hours=hours_ago),
        )

    def test_finds_one_compatible_older_mapping(self):
        source, source_lines = self._snapshot_at(hours_ago=2, text="Alpha\nBeta\nGamma")
        target, target_lines = self._snapshot_at(
            hours_ago=0,
            text="Alfa\nBetta\nGama",
            lines=_TARGET_LINES,
        )
        mapping = save_paragraph_mapping(source, [source_lines[0].pk])
        candidates = discover_transferable_historical_mappings(target)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].mapping_id, mapping.pk)
        self.assertEqual(candidates[0].source_snapshot_id, source.pk)
        self.assertEqual(
            candidates[0].break_after_source_line_ids,
            (source_lines[0].pk,),
        )
        self.assertTrue(candidates[0].correspondence.compatible)
        self.assertEqual(
            candidates[0].correspondence.line_correspondence[0].target_line_id,
            target_lines[0].pk,
        )
        self.assertNotEqual(source_lines[0].pk, target_lines[0].pk)

    def test_finds_mapping_older_than_immediately_previous_snapshot(self):
        oldest, oldest_lines = self._snapshot_at(hours_ago=6, text="Old\nBeta\nGamma")
        _middle, _ = self._snapshot_at(
            hours_ago=3,
            text="Mid\nBeta\nGamma",
            lines=_TARGET_LINES,
        )
        target, _ = self._snapshot_at(
            hours_ago=0,
            text="Now\nBeta\nGamma",
            lines=_TARGET_LINES,
        )
        mapping = save_paragraph_mapping(
            oldest, [oldest_lines[0].pk, oldest_lines[1].pk]
        )
        candidates = discover_transferable_historical_mappings(target)
        self.assertEqual([c.mapping_id for c in candidates], [mapping.pk])
        self.assertEqual(candidates[0].source_snapshot_id, oldest.pk)

    def test_returns_compatible_candidates_newest_to_oldest(self):
        oldest, oldest_lines = self._snapshot_at(hours_ago=9, text="A1\nB\nC")
        middle, middle_lines = self._snapshot_at(
            hours_ago=4,
            text="A2\nB\nC",
            lines=_TARGET_LINES,
        )
        target, _ = self._snapshot_at(
            hours_ago=0,
            text="A3\nB\nC",
            lines=_TARGET_LINES,
        )
        oldest_mapping = save_paragraph_mapping(oldest, [oldest_lines[0].pk])
        middle_mapping = save_paragraph_mapping(middle, [middle_lines[1].pk])
        candidates = discover_transferable_historical_mappings(target)
        self.assertEqual(
            [c.mapping_id for c in candidates],
            [middle_mapping.pk, oldest_mapping.pk],
        )
        self.assertEqual(
            [c.source_snapshot_id for c in candidates],
            [middle.pk, oldest.pk],
        )

    def test_ignores_historical_snapshots_with_no_mapping(self):
        _older_without, _ = self._snapshot_at(hours_ago=5, text="A\nB\nC")
        mapped, mapped_lines = self._snapshot_at(
            hours_ago=2,
            text="A2\nB\nC",
            lines=_TARGET_LINES,
        )
        target, _ = self._snapshot_at(
            hours_ago=0,
            text="A3\nB\nC",
            lines=_TARGET_LINES,
        )
        mapping = save_paragraph_mapping(mapped, [mapped_lines[0].pk])
        candidates = discover_transferable_historical_mappings(target)
        self.assertEqual([c.mapping_id for c in candidates], [mapping.pk])

    def test_excludes_structurally_incompatible_mappings(self):
        compatible, compatible_lines = self._snapshot_at(
            hours_ago=4,
            text="A\nB\nC",
        )
        incompatible, incompatible_lines = _two_page_snapshot(
            self.doc,
            lines=(("line-x", "X"), ("line-y", "Y"), ("line-z", "Z")),
            text="X\nY\nZ",
            created_at=self.now - timedelta(hours=2),
        )
        target, _ = self._snapshot_at(
            hours_ago=0,
            text="A3\nB\nC",
            lines=_TARGET_LINES,
        )
        keep = save_paragraph_mapping(compatible, [compatible_lines[0].pk])
        save_paragraph_mapping(incompatible, [incompatible_lines[0].pk])
        candidates = discover_transferable_historical_mappings(target)
        self.assertEqual([c.mapping_id for c in candidates], [keep.pk])

    def test_discovery_is_read_only_and_does_not_adopt_or_overwrite(self):
        source, source_lines = self._snapshot_at(hours_ago=3, text="A\nB\nC")
        target, target_lines = self._snapshot_at(
            hours_ago=0,
            text="A2\nB\nC",
            lines=_TARGET_LINES,
        )
        historical = save_paragraph_mapping(source, [source_lines[0].pk])
        current = save_paragraph_mapping(target, [target_lines[1].pk])
        before_ids = set(
            TranskribusParagraphMapping.objects.values_list("pk", flat=True)
        )
        before_breaks = list(current.breaks.values_list("after_line_id", flat=True))

        candidates = discover_transferable_historical_mappings(target)

        self.assertEqual([c.mapping_id for c in candidates], [historical.pk])
        after_ids = set(
            TranskribusParagraphMapping.objects.values_list("pk", flat=True)
        )
        self.assertEqual(after_ids, before_ids)
        current.refresh_from_db()
        self.assertEqual(
            list(current.breaks.values_list("after_line_id", flat=True)),
            before_breaks,
        )
        self.assertEqual(current.copied_from_id, None)
        self.assertEqual(
            TranskribusParagraphMapping.objects.get(pk=historical.pk)
            .breaks.get()
            .after_line_id,
            source_lines[0].pk,
        )

    def test_no_fuzzy_fallback_for_near_miss_identities(self):
        source, source_lines = self._snapshot_at(hours_ago=2, text="A\nB\nC")
        near_miss, _ = _two_page_snapshot(
            self.doc,
            lines=(("line-a", "Alfa"), ("line-b2", "Betta"), ("line-c", "Gama")),
            text="Alfa\nBetta\nGama",
            created_at=self.now - timedelta(hours=0),
        )
        save_paragraph_mapping(source, [source_lines[0].pk])
        candidates = discover_transferable_historical_mappings(near_miss)
        self.assertEqual(candidates, ())
        self.assertFalse(
            TranskribusParagraphMapping.objects.filter(snapshot=near_miss).exists()
        )

    def test_zero_break_historical_mapping_is_a_suggestion(self):
        source, _ = self._snapshot_at(hours_ago=2, text="Alpha\nBeta\nGamma")
        target, _ = self._snapshot_at(
            hours_ago=0,
            text="Alfa\nBetta\nGama",
            lines=_TARGET_LINES,
        )
        mapping = save_paragraph_mapping(source, [])
        candidates = discover_transferable_historical_mappings(target)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].mapping_id, mapping.pk)
        self.assertEqual(candidates[0].break_after_source_line_ids, ())
        self.assertTrue(candidates[0].correspondence.compatible)
