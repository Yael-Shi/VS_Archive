"""Arabic printed banded document-level base quality (PR3)."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase, override_settings

from documents.management.commands.run_worker import Command
from documents.models import ArabicPrintedOcrPageCheckpoint, Document, DocumentTextResult
from documents.services.archive_items import create_ocr_document
from documents.services.arabic_printed_text_quality import (
    quality_from_banded_page_qualities,
)
from documents.services.gemini_engine import GeminiResult
from documents.services.htr_adapters.base import HtrResult
from documents.services.non_hebrew_hebrew_translation import (
    persist_hebrew_translation_result,
)
from documents.services.ocr_routing import OcrRouteConfig
from documents.services.text_quality import HUMAN_VERIFIED, NEEDS_CORRECTION

UNASSISTED = ArabicPrintedOcrPageCheckpoint.PageQuality.UNASSISTED
ASSISTED = ArabicPrintedOcrPageCheckpoint.PageQuality.ASSISTED
MIXED = ArabicPrintedOcrPageCheckpoint.PageQuality.MIXED
LQ = ArabicPrintedOcrPageCheckpoint.PageQuality.CLOUD_VISION_LOW_QUALITY

UNKNOWN = DocumentTextResult.Quality.UNKNOWN
LOW = DocumentTextResult.Quality.LOW
MEDIUM = DocumentTextResult.Quality.MEDIUM
GOOD = DocumentTextResult.Quality.GOOD

_BANDED_ENGINE = "antigravity-banded:unassisted"
_GEMINI_ENGINE = "gemini-2.0-flash"
_TRANSKRIBUS_ENGINE = "transkribus-pylaia:564149"

_ANTIGRAVITY_ROUTE = OcrRouteConfig(
    engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
    prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
)
_GEMINI_ROUTE = OcrRouteConfig(
    engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
    prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
)
_TRANSKRIBUS_ROUTE = OcrRouteConfig(
    engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
    prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
)


class ArabicPrintedBandedQualityScorerTests(SimpleTestCase):
    def _score(self, page_qualities, assembled_text="assembled text"):
        return quality_from_banded_page_qualities(
            page_qualities,
            assembled_text=assembled_text,
        )

    def test_empty_evidence_is_unknown(self):
        self.assertEqual(self._score([]), UNKNOWN)

    def test_missing_assembled_text_is_unknown(self):
        self.assertEqual(self._score([UNASSISTED], assembled_text=None), UNKNOWN)

    def test_empty_assembled_text_is_unknown(self):
        self.assertEqual(self._score([UNASSISTED], assembled_text=""), UNKNOWN)

    def test_whitespace_only_assembled_text_is_unknown(self):
        self.assertEqual(self._score([UNASSISTED], assembled_text="   "), UNKNOWN)
        self.assertEqual(self._score([UNASSISTED], assembled_text="\n\t"), UNKNOWN)

    def test_missing_none_page_quality_is_unknown(self):
        self.assertEqual(self._score([None]), UNKNOWN)
        self.assertEqual(self._score([UNASSISTED, None]), UNKNOWN)

    def test_blank_page_quality_is_unknown(self):
        self.assertEqual(self._score([""]), UNKNOWN)
        self.assertEqual(self._score(["   "]), UNKNOWN)

    def test_unknown_page_quality_token_is_unknown(self):
        self.assertEqual(self._score(["UNKNOWN"]), UNKNOWN)
        self.assertEqual(self._score(["not-a-quality"]), UNKNOWN)
        self.assertEqual(self._score([UNASSISTED, "ASSISTED_FALLBACK"]), UNKNOWN)

    def test_all_unassisted_is_good(self):
        self.assertEqual(self._score([UNASSISTED]), GOOD)
        self.assertEqual(self._score([UNASSISTED, UNASSISTED]), GOOD)

    def test_all_unassisted_with_unclear_is_medium(self):
        self.assertEqual(
            self._score([UNASSISTED], assembled_text="hello [UNCLEAR] world"),
            MEDIUM,
        )

    def test_assisted_is_medium(self):
        self.assertEqual(self._score([ASSISTED]), MEDIUM)

    def test_mixed_is_medium(self):
        self.assertEqual(self._score([MIXED]), MEDIUM)

    def test_unassisted_plus_assisted_is_medium(self):
        self.assertEqual(self._score([UNASSISTED, ASSISTED]), MEDIUM)

    def test_one_lq_of_one_is_low(self):
        self.assertEqual(self._score([LQ]), LOW)

    def test_one_lq_of_two_is_low(self):
        self.assertEqual(self._score([LQ, UNASSISTED]), LOW)

    def test_one_lq_of_three_is_medium(self):
        self.assertEqual(self._score([LQ, UNASSISTED, UNASSISTED]), MEDIUM)

    def test_two_lq_of_three_is_low(self):
        self.assertEqual(self._score([LQ, LQ, UNASSISTED]), LOW)

    def test_two_lq_of_four_is_low(self):
        self.assertEqual(self._score([LQ, LQ, UNASSISTED, MIXED]), LOW)

    def test_document_321_shape_is_medium(self):
        self.assertEqual(
            self._score([LQ, MIXED, UNASSISTED, UNASSISTED, MIXED]),
            MEDIUM,
        )

    def test_assisted_or_mixed_alone_never_low(self):
        self.assertNotEqual(self._score([ASSISTED]), LOW)
        self.assertNotEqual(self._score([MIXED]), LOW)
        self.assertNotEqual(self._score([ASSISTED, MIXED]), LOW)


@override_settings(UPLOADS_BUCKET_NAME="")
class ArabicPrintedBandedQualityPersistenceTests(TestCase):
    def setUp(self):
        self.command = Command()
        self.command._cfg = SimpleNamespace(
            min_text_length=5,
            gemini_double_pass=False,
            gemini_consistency_min_ratio=0.85,
            gemini_temperature=0.2,
            gemini_top_k=40,
            gemini_top_p=0.95,
            gemini_max_output_tokens=8192,
        )
        self._translation_patcher = patch(
            "documents.management.commands.run_worker.translate_text_to_hebrew_with_gemini",
            return_value=GeminiResult(
                text="translated hebrew text long enough",
                engine_name="gemini-2.0-flash",
            ),
        )
        self.mock_translate = self._translation_patcher.start()

    def tearDown(self):
        self._translation_patcher.stop()

    def _arabic_doc(self) -> Document:
        return create_ocr_document(
            title="Arabic printed quality persist",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.ARABIC,
            text_input_type=Document.TextInputType.PRINTED,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="documents/quality-ar/original.jpg",
            mime_type="image/jpeg",
        )

    def _hebrew_doc(self) -> Document:
        return create_ocr_document(
            title="Hebrew quality persist",
            doc_type=Document.DocType.IMAGE,
            language=Document.Language.HEBREW,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            upload_status=Document.UploadStatus.UPLOADED,
            file_s3_key="documents/quality-he/original.jpg",
            mime_type="image/jpeg",
        )

    def _save(
        self,
        doc: Document,
        htr: HtrResult,
        *,
        is_he: bool = False,
        route: OcrRouteConfig = _ANTIGRAVITY_ROUTE,
        engine: str | None = None,
    ):
        self.command._save_htr_results(
            doc,
            engine or htr.engine_name,
            is_he,
            htr,
            route,
        )

    def _source(self, doc: Document, engine: str) -> DocumentTextResult:
        return DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=engine,
        )

    def _hebrew(self, doc: Document, engine: str) -> DocumentTextResult:
        return DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=engine,
        )

    def test_source_text_persists_htr_quality(self):
        doc = self._arabic_doc()
        htr = HtrResult(
            text="banded assembled source",
            needs_review=True,
            engine_name=_BANDED_ENGINE,
            quality=GOOD,
        )
        self._save(doc, htr)
        row = self._source(doc, _BANDED_ENGINE)
        self.assertEqual(row.quality, GOOD)
        self.assertEqual(row.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(
            row.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertEqual(row.engine, _BANDED_ENGINE)
        self.assertEqual(row.engine_key, DocumentTextResult.OcrEngineKey.ANTIGRAVITY)

    def test_same_engine_rerun_overwrites_prior_base_quality(self):
        doc = self._arabic_doc()
        self._save(
            doc,
            HtrResult(
                text="first banded text",
                needs_review=True,
                engine_name=_BANDED_ENGINE,
                quality=LOW,
            ),
        )
        self.assertEqual(self._source(doc, _BANDED_ENGINE).quality, LOW)
        self._save(
            doc,
            HtrResult(
                text="second banded text",
                needs_review=True,
                engine_name=_BANDED_ENGINE,
                quality=GOOD,
            ),
        )
        row = self._source(doc, _BANDED_ENGINE)
        self.assertEqual(row.quality, GOOD)
        self.assertEqual(row.text, "second banded text")
        self.assertEqual(row.engine, _BANDED_ENGINE)
        self.assertEqual(
            DocumentTextResult.objects.filter(
                document=doc,
                result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            ).count(),
            1,
        )

    def test_quality_none_does_not_overwrite_existing_quality(self):
        doc = self._arabic_doc()
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=_GEMINI_ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="prior scored text",
            quality=MEDIUM,
        )
        self._save(
            doc,
            HtrResult(
                text="gemini rerun text long enough",
                needs_review=False,
                engine_name=_GEMINI_ENGINE,
                quality=None,
            ),
            route=_GEMINI_ROUTE,
        )
        row = self._source(doc, _GEMINI_ENGINE)
        self.assertEqual(row.quality, MEDIUM)
        self.assertEqual(row.text, "gemini rerun text long enough")

    def test_new_non_scoring_row_defaults_unknown(self):
        doc = self._arabic_doc()
        self._save(
            doc,
            HtrResult(
                text="gemini first persist",
                needs_review=False,
                engine_name=_GEMINI_ENGINE,
            ),
            route=_GEMINI_ROUTE,
        )
        self.assertEqual(self._source(doc, _GEMINI_ENGINE).quality, UNKNOWN)

    def test_gemini_and_non_banded_antigravity_remain_unknown(self):
        doc = self._arabic_doc()
        self._save(
            doc,
            HtrResult(
                text="json antigravity transcript",
                needs_review=True,
                engine_name="antigravity-preview-agent",
            ),
        )
        self.assertEqual(
            self._source(doc, "antigravity-preview-agent").quality,
            UNKNOWN,
        )

    def test_transkribus_remains_unknown(self):
        doc = self._hebrew_doc()
        self._save(
            doc,
            HtrResult(
                text="transkribus transcript long enough",
                needs_review=False,
                engine_name=_TRANSKRIBUS_ENGINE,
            ),
            is_he=True,
            route=_TRANSKRIBUS_ROUTE,
        )
        self.assertEqual(self._source(doc, _TRANSKRIBUS_ENGINE).quality, UNKNOWN)
        self.assertEqual(self._hebrew(doc, _TRANSKRIBUS_ENGINE).quality, UNKNOWN)

    def test_hebrew_native_scored_source_does_not_copy_quality_onto_hebrew_text(self):
        doc = self._hebrew_doc()
        self._save(
            doc,
            HtrResult(
                text="hebrew native transcript long enough",
                needs_review=False,
                engine_name=_TRANSKRIBUS_ENGINE,
                quality=GOOD,
            ),
            is_he=True,
            route=_TRANSKRIBUS_ROUTE,
        )
        self.assertEqual(self._source(doc, _TRANSKRIBUS_ENGINE).quality, GOOD)
        self.assertEqual(self._hebrew(doc, _TRANSKRIBUS_ENGINE).quality, UNKNOWN)
        self.mock_translate.assert_not_called()

    def test_non_hebrew_translation_inherits_scored_source_quality(self):
        doc = self._arabic_doc()
        self._save(
            doc,
            HtrResult(
                text="banded assembled source",
                needs_review=True,
                engine_name=_BANDED_ENGINE,
                quality=GOOD,
            ),
        )
        persist_hebrew_translation_result(
            doc,
            _BANDED_ENGINE,
            translation=GeminiResult(
                text="translated hebrew text long enough",
                engine_name=_BANDED_ENGINE,
            ),
            min_text_length=5,
        )
        self.assertEqual(self._source(doc, _BANDED_ENGINE).quality, GOOD)
        hebrew = self._hebrew(doc, _BANDED_ENGINE)
        self.assertEqual(hebrew.quality, GOOD)
        self.assertEqual(
            hebrew.prompt_variant,
            DocumentTextResult.OcrPromptVariant.HEBREW_TRANSLATION,
        )
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )

    def test_human_verified_and_needs_correction_are_never_persisted(self):
        doc = self._arabic_doc()
        self._save(
            doc,
            HtrResult(
                text="must not persist presentation quality",
                needs_review=True,
                engine_name=_BANDED_ENGINE,
                quality=HUMAN_VERIFIED,
            ),
        )
        self.assertEqual(self._source(doc, _BANDED_ENGINE).quality, UNKNOWN)
        self.assertNotIn(
            HUMAN_VERIFIED,
            DocumentTextResult.Quality.values,
        )
        self.assertNotIn(
            NEEDS_CORRECTION,
            DocumentTextResult.Quality.values,
        )
        self._save(
            doc,
            HtrResult(
                text="must not persist presentation quality",
                needs_review=True,
                engine_name=_BANDED_ENGINE,
                quality=NEEDS_CORRECTION,
            ),
        )
        self.assertEqual(self._source(doc, _BANDED_ENGINE).quality, UNKNOWN)

    def test_rejected_same_engine_rerun_returns_to_unverified_and_can_update_quality(
        self,
    ):
        doc = self._arabic_doc()
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=_BANDED_ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.ANTIGRAVITY,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.REJECTED,
            text="rejected source",
            quality=LOW,
        )
        self._save(
            doc,
            HtrResult(
                text="reprocessed banded source",
                needs_review=True,
                engine_name=_BANDED_ENGINE,
                quality=MEDIUM,
            ),
        )
        row = self._source(doc, _BANDED_ENGINE)
        self.assertEqual(
            row.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertEqual(row.quality, MEDIUM)
        self.assertEqual(row.engine, _BANDED_ENGINE)
