"""Model, save, currentness, and safety tests for Transkribus paragraph mappings."""

from __future__ import annotations

import hashlib

from django.contrib.auth import get_user_model
from django.core.exceptions import ValidationError
from django.db import IntegrityError, transaction
from django.test import SimpleTestCase, TestCase

from documents.models import (
    Document,
    DocumentTextResult,
    TranskribusParagraphBreak,
    TranskribusParagraphMapping,
    TranskribusSnapshotLine,
    TranskribusSnapshotPage,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.archive_items import create_ocr_document
from documents.services.transkribus_paragraph_mapping import (
    TranskribusParagraphMappingError,
    assess_paragraph_mapping_currentness,
    contributing_lines_for_snapshot,
    get_paragraph_mapping_for_snapshot,
    save_paragraph_mapping,
    validate_break_after_lines,
)

User = get_user_model()

_PARSER = "test_paragraph_mapping_v1"
_ENGINE = "transkribus-pylaia:42"
_TEXT = "Alpha\nBeta\nGamma"


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _create_doc(**kwargs) -> Document:
    defaults = dict(
        title="Paragraph mapping doc",
        doc_type=Document.DocType.PDF,
        language=Document.Language.HEBREW,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        upload_status=Document.UploadStatus.UPLOADED,
        file_s3_key="paragraph-mapping.pdf",
        mime_type="application/pdf",
    )
    defaults.update(kwargs)
    return create_ocr_document(**defaults)


def _ready_snapshot(
    document: Document,
    *,
    text: str = _TEXT,
    parser_version: str = _PARSER,
    hover_eligible: bool = True,
    storage_status: str = TranskribusTranscriptSnapshot.StorageStatus.READY,
) -> TranskribusTranscriptSnapshot:
    unique = f"{document.pk}:{TranskribusTranscriptSnapshot.objects.count()}:{text}"
    return TranskribusTranscriptSnapshot.objects.create(
        document=document,
        source_kind=TranskribusTranscriptSnapshot.SourceKind.AUTOMATIC_HTR,
        parser_version=parser_version,
        provider_identity_fingerprint=_sha(f"prov:{unique}"),
        raw_xml_fingerprint=_sha(f"raw:{unique}"),
        canonical_text=text,
        canonical_text_sha256=_sha(text),
        geometry_capability=TranskribusTranscriptSnapshot.GeometryCapability.VERIFIED,
        hover_eligible=hover_eligible,
        storage_status=storage_status,
    )


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
        image_width=1000,
        image_height=1500,
    )


def _add_line(
    page: TranskribusSnapshotPage,
    order: int,
    text: str,
    *,
    start: int,
    end: int,
    provider_line_id: str | None = None,
    contributes: bool = True,
    polygon_points=None,
) -> TranskribusSnapshotLine:
    return TranskribusSnapshotLine.objects.create(
        page=page,
        order_index=order,
        provider_region_id=f"region-{page.page_index}",
        provider_line_id=(
            provider_line_id
            if provider_line_id is not None
            else f"line-{page.page_index}-{order}"
        ),
        text=text,
        contributes_to_canonical=contributes,
        char_start=start,
        char_end=end,
        polygon_points=polygon_points or [[10.0, 10.0], [100.0, 10.0], [100.0, 20.0]],
        bbox_min_x=10.0,
        bbox_min_y=10.0,
        bbox_max_x=100.0,
        bbox_max_y=20.0,
        coords_valid=True,
        has_meaningful_geometry=True,
    )


def _three_line_snapshot(document: Document, **kwargs) -> tuple:
    snapshot = _ready_snapshot(document, **kwargs)
    page = _add_page(snapshot, 1)
    alpha = _add_line(page, 0, "Alpha", start=0, end=5)
    beta = _add_line(page, 1, "Beta", start=6, end=10)
    gamma = _add_line(page, 2, "Gamma", start=11, end=16)
    return snapshot, page, alpha, beta, gamma


def _source_row(
    doc: Document,
    *,
    text: str = _TEXT,
    source_revision: int = 1,
) -> DocumentTextResult:
    return DocumentTextResult.objects.create(
        document=doc,
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        engine=_ENGINE,
        engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        status=DocumentTextResult.Status.NEEDS_REVIEW,
        verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
        text=text,
        source_revision=source_revision,
    )


def _bind(
    *,
    text_result: DocumentTextResult,
    snapshot: TranskribusTranscriptSnapshot,
    bound_source_revision: int = 1,
) -> TranskribusTextResultBinding:
    return TranskribusTextResultBinding.objects.create(
        text_result=text_result,
        snapshot=snapshot,
        binding_role=TranskribusTextResultBinding.BindingRole.SNAPSHOT_SOURCE,
        bound_text_sha256=_sha(text_result.text or ""),
        bound_source_revision=bound_source_revision,
    )


class ParagraphMappingModelTests(TestCase):
    def setUp(self) -> None:
        self.doc = _create_doc()
        self.snapshot, self.page, self.alpha, self.beta, self.gamma = (
            _three_line_snapshot(self.doc)
        )
        self.user = User.objects.create_user(
            username="para-staff",
            password="x",
            is_staff=True,
        )

    def test_one_mapping_per_snapshot(self):
        TranskribusParagraphMapping.objects.create(
            snapshot=self.snapshot,
            document=self.doc,
        )
        with transaction.atomic(), self.assertRaises(IntegrityError):
            TranskribusParagraphMapping.objects.create(
                snapshot=self.snapshot,
                document=self.doc,
            )

    def test_same_document_validation(self):
        other = _create_doc(
            title="Other doc",
            file_s3_key="paragraph-mapping-other.pdf",
        )
        mapping = TranskribusParagraphMapping(
            snapshot=self.snapshot,
            document=other,
        )
        with self.assertRaises(ValidationError):
            mapping.full_clean()
        with self.assertRaises(ValidationError):
            mapping.save()

    def test_valid_break_after_contributing_line(self):
        mapping = save_paragraph_mapping(
            self.snapshot,
            [self.alpha.pk],
            actor=self.user,
        )
        self.assertEqual(mapping.breaks.count(), 1)
        self.assertEqual(mapping.breaks.get().after_line_id, self.alpha.pk)
        self.assertEqual(mapping.created_by_id, self.user.pk)
        self.assertEqual(mapping.updated_by_id, self.user.pk)

    def test_reject_break_from_another_snapshot(self):
        other_snapshot, _page, other_alpha, _beta, _gamma = _three_line_snapshot(
            self.doc,
            text="Other\nBeta\nGamma",
        )
        mapping = TranskribusParagraphMapping.objects.create(
            snapshot=self.snapshot,
            document=self.doc,
        )
        break_row = TranskribusParagraphBreak(
            mapping=mapping,
            after_line=other_alpha,
        )
        with self.assertRaises(ValidationError):
            break_row.full_clean()
        with self.assertRaises(ValidationError):
            break_row.save()
        with self.assertRaises(TranskribusParagraphMappingError):
            save_paragraph_mapping(self.snapshot, [other_alpha.pk])

    def test_reject_non_contributing_line(self):
        empty = _add_line(
            self.page,
            3,
            "",
            start=16,
            end=16,
            provider_line_id="empty",
            contributes=False,
        )
        mapping = TranskribusParagraphMapping.objects.create(
            snapshot=self.snapshot,
            document=self.doc,
        )
        break_row = TranskribusParagraphBreak(mapping=mapping, after_line=empty)
        with self.assertRaises(ValidationError):
            break_row.full_clean()
        with self.assertRaises(TranskribusParagraphMappingError):
            save_paragraph_mapping(self.snapshot, [empty.pk])

    def test_reject_final_contributing_line(self):
        mapping = TranskribusParagraphMapping.objects.create(
            snapshot=self.snapshot,
            document=self.doc,
        )
        break_row = TranskribusParagraphBreak(
            mapping=mapping,
            after_line=self.gamma,
        )
        with self.assertRaises(ValidationError):
            break_row.full_clean()
        with self.assertRaises(TranskribusParagraphMappingError):
            save_paragraph_mapping(self.snapshot, [self.gamma.pk])

    def test_duplicate_break_rejected(self):
        mapping = TranskribusParagraphMapping.objects.create(
            snapshot=self.snapshot,
            document=self.doc,
        )
        TranskribusParagraphBreak.objects.create(
            mapping=mapping,
            after_line=self.alpha,
        )
        with transaction.atomic(), self.assertRaises(IntegrityError):
            TranskribusParagraphBreak.objects.create(
                mapping=mapping,
                after_line=self.alpha,
            )
        with self.assertRaises(TranskribusParagraphMappingError):
            save_paragraph_mapping(self.snapshot, [self.alpha.pk, self.alpha.pk])

    def test_zero_break_mapping_is_valid_and_distinct_from_no_mapping(self):
        self.assertIsNone(get_paragraph_mapping_for_snapshot(self.snapshot))
        mapping = save_paragraph_mapping(self.snapshot, [])
        self.assertIsNotNone(mapping.pk)
        self.assertEqual(mapping.breaks.count(), 0)
        loaded = get_paragraph_mapping_for_snapshot(self.snapshot)
        self.assertEqual(loaded.pk, mapping.pk)
        self.assertEqual(loaded.breaks.count(), 0)

    def test_resave_updates_breaks_without_touching_canonical_or_lines(self):
        save_paragraph_mapping(self.snapshot, [self.alpha.pk])
        line_snapshot = list(
            TranskribusSnapshotLine.objects.filter(page__snapshot=self.snapshot)
            .order_by("order_index")
            .values(
                "pk",
                "text",
                "char_start",
                "char_end",
                "provider_line_id",
                "provider_region_id",
                "polygon_points",
                "bbox_min_x",
                "contributes_to_canonical",
            )
        )
        canonical = self.snapshot.canonical_text
        canonical_sha = self.snapshot.canonical_text_sha256

        mapping = save_paragraph_mapping(self.snapshot, [self.beta.pk], actor=self.user)
        self.assertEqual(mapping.breaks.count(), 1)
        self.assertEqual(mapping.breaks.get().after_line_id, self.beta.pk)
        self.assertEqual(
            TranskribusParagraphMapping.objects.filter(snapshot=self.snapshot).count(),
            1,
        )

        self.snapshot.refresh_from_db()
        self.assertEqual(self.snapshot.canonical_text, canonical)
        self.assertEqual(self.snapshot.canonical_text_sha256, canonical_sha)
        self.assertEqual(
            list(
                TranskribusSnapshotLine.objects.filter(page__snapshot=self.snapshot)
                .order_by("order_index")
                .values(
                    "pk",
                    "text",
                    "char_start",
                    "char_end",
                    "provider_line_id",
                    "provider_region_id",
                    "polygon_points",
                    "bbox_min_x",
                    "contributes_to_canonical",
                )
            ),
            line_snapshot,
        )

    def test_validate_break_after_lines_accepts_contributing_non_final(self):
        resolved = validate_break_after_lines(
            self.snapshot, [self.alpha.pk, self.beta.pk]
        )
        self.assertEqual([line.pk for line in resolved], [self.alpha.pk, self.beta.pk])

    def test_contributing_lines_skip_non_contributing_and_keep_order(self):
        page2 = _add_page(self.snapshot, 2)
        _add_line(self.page, 3, "", start=16, end=16, contributes=False)
        later = _add_line(page2, 0, "Delta", start=17, end=22)
        lines = contributing_lines_for_snapshot(self.snapshot)
        self.assertEqual(
            [line.pk for line in lines],
            [self.alpha.pk, self.beta.pk, self.gamma.pk, later.pk],
        )

    def test_actorless_resave_preserves_updated_by(self):
        mapping = save_paragraph_mapping(
            self.snapshot,
            [self.alpha.pk],
            actor=self.user,
        )
        self.assertEqual(mapping.updated_by_id, self.user.pk)
        resaved = save_paragraph_mapping(self.snapshot, [self.beta.pk])
        self.assertEqual(resaved.pk, mapping.pk)
        self.assertEqual(resaved.updated_by_id, self.user.pk)

    def test_copied_from_is_optional_and_same_document(self):
        other_snapshot, _page, _a, _b, _c = _three_line_snapshot(
            self.doc,
            text="Copy\nBeta\nGamma",
        )
        source = save_paragraph_mapping(self.snapshot, [self.alpha.pk])
        self.assertIsNone(source.copied_from_id)
        copied = save_paragraph_mapping(
            other_snapshot,
            [],
            copied_from=source,
        )
        self.assertEqual(copied.copied_from_id, source.pk)

        other_doc = _create_doc(
            title="Foreign",
            file_s3_key="paragraph-mapping-foreign.pdf",
        )
        foreign_snapshot, *_ = _three_line_snapshot(other_doc, text="F\nB\nG")
        foreign_mapping = TranskribusParagraphMapping.objects.create(
            snapshot=foreign_snapshot,
            document=other_doc,
        )
        unbound_snapshot, *_ = _three_line_snapshot(
            self.doc,
            text="Unbound\nBeta\nGamma",
        )
        invalid = TranskribusParagraphMapping(
            snapshot=unbound_snapshot,
            document=self.doc,
            copied_from=foreign_mapping,
        )
        with self.assertRaises(ValidationError) as raised:
            invalid.full_clean()
        self.assertIn("copied_from", raised.exception.message_dict)

    def test_ordinary_resave_clears_copied_from_without_touching_canonical(self):
        other_snapshot, _page, other_alpha, _b, _c = _three_line_snapshot(
            self.doc,
            text="Adopt\nBeta\nGamma",
        )
        source = save_paragraph_mapping(self.snapshot, [self.alpha.pk])
        self.assertIsNone(source.copied_from_id)
        adopted = save_paragraph_mapping(
            other_snapshot,
            [other_alpha.pk],
            copied_from=source,
        )
        self.assertEqual(adopted.copied_from_id, source.pk)

        line_snapshot = list(
            TranskribusSnapshotLine.objects.filter(page__snapshot=other_snapshot)
            .order_by("page__page_index", "order_index")
            .values(
                "pk",
                "text",
                "char_start",
                "char_end",
                "provider_line_id",
                "polygon_points",
            )
        )
        canonical = other_snapshot.canonical_text
        canonical_sha = other_snapshot.canonical_text_sha256

        resaved = save_paragraph_mapping(other_snapshot, [other_alpha.pk])
        self.assertEqual(resaved.pk, adopted.pk)
        self.assertIsNone(resaved.copied_from_id)
        other_snapshot.refresh_from_db()
        self.assertEqual(other_snapshot.canonical_text, canonical)
        self.assertEqual(other_snapshot.canonical_text_sha256, canonical_sha)
        self.assertEqual(
            list(
                TranskribusSnapshotLine.objects.filter(page__snapshot=other_snapshot)
                .order_by("page__page_index", "order_index")
                .values(
                    "pk",
                    "text",
                    "char_start",
                    "char_end",
                    "provider_line_id",
                    "polygon_points",
                )
            ),
            line_snapshot,
        )

    def test_page_boundary_is_independent_of_paragraph_boundary(self):
        page2 = _add_page(self.snapshot, 2)
        later = _add_line(page2, 0, "Delta", start=17, end=22)
        mapping = save_paragraph_mapping(self.snapshot, [self.gamma.pk])
        self.assertEqual(mapping.breaks.get().after_line_id, self.gamma.pk)
        with self.assertRaises(TranskribusParagraphMappingError):
            save_paragraph_mapping(self.snapshot, [later.pk])
        with self.assertRaises(ValidationError):
            TranskribusParagraphBreak(
                mapping=mapping,
                after_line=later,
            ).full_clean()

    def test_invalid_resave_leaves_existing_mapping_intact(self):
        mapping = save_paragraph_mapping(self.snapshot, [self.alpha.pk])
        mapping_id = mapping.pk
        with self.assertRaises(TranskribusParagraphMappingError):
            save_paragraph_mapping(self.snapshot, [self.gamma.pk])
        mapping.refresh_from_db()
        self.assertEqual(mapping.pk, mapping_id)
        self.assertEqual(
            list(mapping.breaks.values_list("after_line_id", flat=True)),
            [self.alpha.pk],
        )


class ParagraphMappingCurrentnessTests(TestCase):
    def setUp(self) -> None:
        self.doc = _create_doc()
        self.snapshot, self.page, self.alpha, self.beta, self.gamma = (
            _three_line_snapshot(self.doc)
        )
        self.result = _source_row(self.doc)
        self.binding = _bind(text_result=self.result, snapshot=self.snapshot)

    def test_structurally_fresh_same_snapshot_mapping_is_current(self):
        mapping = save_paragraph_mapping(self.snapshot, [self.alpha.pk])
        assessment = assess_paragraph_mapping_currentness(self.doc)
        self.assertTrue(assessment.has_mapping)
        self.assertTrue(assessment.is_current)
        self.assertTrue(assessment.is_structurally_fresh)
        self.assertEqual(assessment.mapping.pk, mapping.pk)
        self.assertEqual(assessment.bound_snapshot_id, self.snapshot.pk)

    def test_no_mapping_is_not_current(self):
        assessment = assess_paragraph_mapping_currentness(self.doc)
        self.assertFalse(assessment.has_mapping)
        self.assertFalse(assessment.is_current)
        self.assertTrue(assessment.is_structurally_fresh)
        self.assertIsNone(assessment.mapping)

    def test_zero_break_mapping_is_current_and_distinct_from_no_mapping(self):
        mapping = save_paragraph_mapping(self.snapshot, [])
        assessment = assess_paragraph_mapping_currentness(self.doc)
        self.assertTrue(assessment.has_mapping)
        self.assertTrue(assessment.is_current)
        self.assertEqual(assessment.mapping.pk, mapping.pk)
        self.assertEqual(assessment.mapping.breaks.count(), 0)

    def test_local_text_revision_drift_retains_mapping_but_not_current(self):
        mapping = save_paragraph_mapping(self.snapshot, [self.alpha.pk])
        self.result.text = "Alpha\nBeta\nGamma edited"
        self.result.source_revision = 2
        self.result.save(update_fields=["text", "source_revision", "updated_at"])

        assessment = assess_paragraph_mapping_currentness(self.doc, mapping=mapping)
        self.assertTrue(assessment.has_mapping)
        self.assertFalse(assessment.is_current)
        self.assertFalse(assessment.is_structurally_fresh)
        self.assertEqual(
            TranskribusParagraphMapping.objects.filter(pk=mapping.pk).count(),
            1,
        )
        mapping.refresh_from_db()
        self.assertEqual(mapping.breaks.get().after_line_id, self.alpha.pk)

    def test_binding_to_new_snapshot_makes_old_mapping_non_current(self):
        old_mapping = save_paragraph_mapping(self.snapshot, [self.alpha.pk])
        new_text = "New\nBeta\nGamma"
        new_snapshot, *_ = _three_line_snapshot(self.doc, text=new_text)
        self.result.text = new_text
        self.result.save(update_fields=["text", "updated_at"])
        self.binding.snapshot = new_snapshot
        self.binding.bound_text_sha256 = _sha(new_text)
        self.binding.save(update_fields=["snapshot", "bound_text_sha256"])

        old_assessment = assess_paragraph_mapping_currentness(
            self.doc,
            mapping=old_mapping,
        )
        self.assertTrue(old_assessment.has_mapping)
        self.assertTrue(old_assessment.is_structurally_fresh)
        self.assertFalse(old_assessment.is_current)
        self.assertEqual(old_assessment.bound_snapshot_id, new_snapshot.pk)
        self.assertEqual(old_assessment.mapping_snapshot_id, self.snapshot.pk)
        self.assertTrue(
            TranskribusParagraphMapping.objects.filter(pk=old_mapping.pk).exists()
        )

        displayed_assessment = assess_paragraph_mapping_currentness(self.doc)
        self.assertFalse(displayed_assessment.has_mapping)
        self.assertFalse(displayed_assessment.is_current)

    def test_mapping_for_currently_bound_new_snapshot_is_current(self):
        save_paragraph_mapping(self.snapshot, [self.alpha.pk])
        new_text = "New\nBeta\nGamma"
        new_snapshot, _page, new_alpha, _b, _c = _three_line_snapshot(
            self.doc,
            text=new_text,
        )
        self.result.text = new_text
        self.result.save(update_fields=["text", "updated_at"])
        self.binding.snapshot = new_snapshot
        self.binding.bound_text_sha256 = _sha(new_text)
        self.binding.save(update_fields=["snapshot", "bound_text_sha256"])
        new_mapping = save_paragraph_mapping(new_snapshot, [new_alpha.pk])

        assessment = assess_paragraph_mapping_currentness(self.doc)
        self.assertTrue(assessment.has_mapping)
        self.assertTrue(assessment.is_current)
        self.assertEqual(assessment.mapping.pk, new_mapping.pk)
        self.assertEqual(assessment.bound_snapshot_id, new_snapshot.pk)

    def test_hover_ineligible_does_not_invalidate_paragraph_currentness(self):
        self.snapshot.hover_eligible = False
        self.snapshot.save(update_fields=["hover_eligible"])
        mapping = save_paragraph_mapping(self.snapshot, [self.alpha.pk])
        assessment = assess_paragraph_mapping_currentness(self.doc)
        self.assertTrue(assessment.has_mapping)
        self.assertTrue(assessment.is_current)
        self.assertTrue(assessment.is_structurally_fresh)
        self.assertEqual(assessment.mapping.pk, mapping.pk)


class ParagraphMappingModelFieldTests(SimpleTestCase):
    def test_mapping_model_does_not_store_offsets_or_hover_ids(self):
        mapping_fields = {
            field.name for field in TranskribusParagraphMapping._meta.fields
        }
        break_fields = {field.name for field in TranskribusParagraphBreak._meta.fields}
        forbidden = {
            "char_start",
            "char_end",
            "text",
            "canonical_text",
            "provider_region_id",
            "provider_line_id",
            "hover_line_id",
        }
        self.assertTrue(forbidden.isdisjoint(mapping_fields))
        self.assertTrue(forbidden.isdisjoint(break_fields))


class ParagraphMappingSafetyTests(TestCase):
    def test_save_does_not_modify_canonical_geometry_or_dtr_text(self):
        doc = _create_doc()
        snapshot, _page, alpha, beta, _gamma = _three_line_snapshot(doc)
        result = _source_row(doc)
        _bind(text_result=result, snapshot=snapshot)
        dtr_text = result.text
        canonical = snapshot.canonical_text
        lines = list(
            TranskribusSnapshotLine.objects.filter(page__snapshot=snapshot).values(
                "pk",
                "text",
                "char_start",
                "char_end",
                "polygon_points",
                "bbox_min_x",
                "bbox_min_y",
                "bbox_max_x",
                "bbox_max_y",
                "provider_line_id",
                "provider_region_id",
            )
        )

        save_paragraph_mapping(snapshot, [alpha.pk, beta.pk])

        result.refresh_from_db()
        snapshot.refresh_from_db()
        self.assertEqual(result.text, dtr_text)
        self.assertEqual(snapshot.canonical_text, canonical)
        self.assertEqual(
            list(
                TranskribusSnapshotLine.objects.filter(page__snapshot=snapshot).values(
                    "pk",
                    "text",
                    "char_start",
                    "char_end",
                    "polygon_points",
                    "bbox_min_x",
                    "bbox_min_y",
                    "bbox_max_x",
                    "bbox_max_y",
                    "provider_line_id",
                    "provider_region_id",
                )
            ),
            lines,
        )
