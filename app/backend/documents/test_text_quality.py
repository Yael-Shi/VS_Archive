"""Foundation tests for persisted base quality and effective public quality."""

from __future__ import annotations

from django.contrib.auth.models import User
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import Document, DocumentTextResult
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
)
from documents.services.gemini_engine import GeminiResult
from documents.services.non_hebrew_hebrew_translation import (
    persist_hebrew_translation_result,
)
from documents.services.text_quality import (
    HUMAN_VERIFIED,
    NEEDS_CORRECTION,
    capped_inherited_base_quality,
    effective_public_text_quality_for_manual_text,
    effective_public_text_quality_for_result,
)
from documents.services.verified_text_result_edit import (
    edit_pending_text_result,
    edit_verified_text_result,
    verify_pending_text_result,
)


@override_settings(UPLOADS_BUCKET_NAME="")
class TextQualityFoundationTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="text_quality_staff",
            password="test-pass",
            is_staff=True,
        )

    def _create_doc(self) -> Document:
        return create_ocr_document(
            title="Quality foundation doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/quality/original.jpg",
            mime_type="image/jpeg",
        )

    def _create_result(
        self,
        doc: Document,
        *,
        engine: str = "engine-quality",
        quality: str | None = None,
        verification_status: str = DocumentTextResult.VerificationStatus.UNVERIFIED,
        text: str = "sample text",
    ) -> DocumentTextResult:
        kwargs = {
            "document": doc,
            "result_type": DocumentTextResult.ResultType.SOURCE_TEXT,
            "engine": engine,
            "engine_key": DocumentTextResult.OcrEngineKey.GEMINI,
            "prompt_variant": DocumentTextResult.OcrPromptVariant.PRINTED,
            "status": DocumentTextResult.Status.NEEDS_REVIEW,
            "verification_status": verification_status,
            "text": text,
        }
        if quality is not None:
            kwargs["quality"] = quality
        return DocumentTextResult.objects.create(**kwargs)

    def test_new_row_defaults_quality_to_unknown(self):
        row = self._create_result(self._create_doc())
        self.assertEqual(row.quality, DocumentTextResult.Quality.UNKNOWN)

    def test_persisted_base_values_are_exposed_when_unverified(self):
        doc = self._create_doc()
        for index, quality in enumerate(DocumentTextResult.Quality.values):
            row = self._create_result(
                doc,
                engine=f"engine-{index}",
                quality=quality,
                verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            )
            self.assertEqual(row.quality, quality)
            self.assertEqual(effective_public_text_quality_for_result(row), quality)

    def test_verified_overrides_persisted_base_quality_to_human_verified(self):
        doc = self._create_doc()
        for index, quality in enumerate(DocumentTextResult.Quality.values):
            row = self._create_result(
                doc,
                engine=f"verified-{index}",
                quality=quality,
                verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            )
            self.assertEqual(row.quality, quality)
            self.assertEqual(
                effective_public_text_quality_for_result(row),
                HUMAN_VERIFIED,
            )

    def test_rejected_good_resolves_to_needs_correction(self):
        row = self._create_result(
            self._create_doc(),
            quality=DocumentTextResult.Quality.GOOD,
            verification_status=DocumentTextResult.VerificationStatus.REJECTED,
        )
        self.assertEqual(row.quality, DocumentTextResult.Quality.GOOD)
        self.assertEqual(
            effective_public_text_quality_for_result(row),
            NEEDS_CORRECTION,
        )
        self.assertNotEqual(
            effective_public_text_quality_for_result(row),
            DocumentTextResult.Quality.LOW,
        )
        self.assertNotEqual(
            effective_public_text_quality_for_result(row),
            DocumentTextResult.Quality.GOOD,
        )

    def test_rejected_low_resolves_to_needs_correction(self):
        row = self._create_result(
            self._create_doc(),
            quality=DocumentTextResult.Quality.LOW,
            verification_status=DocumentTextResult.VerificationStatus.REJECTED,
        )
        self.assertEqual(row.quality, DocumentTextResult.Quality.LOW)
        self.assertEqual(
            effective_public_text_quality_for_result(row),
            NEEDS_CORRECTION,
        )

    def test_rejection_does_not_change_persisted_quality(self):
        row = self._create_result(
            self._create_doc(),
            quality=DocumentTextResult.Quality.GOOD,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="pending reject",
        )
        self.client.force_login(self.staff)
        response = self.client.post(
            reverse("review-text-result-reject", kwargs={"result_id": row.pk}),
        )
        self.assertEqual(response.status_code, 302)
        row.refresh_from_db()
        self.assertEqual(
            row.verification_status,
            DocumentTextResult.VerificationStatus.REJECTED,
        )
        self.assertEqual(row.quality, DocumentTextResult.Quality.GOOD)
        self.assertEqual(
            effective_public_text_quality_for_result(row),
            NEEDS_CORRECTION,
        )

    def test_pending_ocr_text_edit_preserves_quality_and_verification(self):
        row = self._create_result(
            self._create_doc(),
            quality=DocumentTextResult.Quality.MEDIUM,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="before edit",
        )
        edit_pending_text_result(
            result_id=row.id,
            new_text="after edit",
            editor=self.staff,
        )
        row.refresh_from_db()
        self.assertEqual(row.text, "after edit")
        self.assertEqual(row.quality, DocumentTextResult.Quality.MEDIUM)
        self.assertEqual(
            row.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )

    def test_verified_ocr_text_edit_preserves_quality(self):
        row = self._create_result(
            self._create_doc(),
            quality=DocumentTextResult.Quality.LOW,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="verified before",
        )
        edit_verified_text_result(
            result_id=row.id,
            new_text="verified after",
            editor=self.staff,
        )
        row.refresh_from_db()
        self.assertEqual(row.text, "verified after")
        self.assertEqual(row.quality, DocumentTextResult.Quality.LOW)
        self.assertEqual(
            row.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            effective_public_text_quality_for_result(row),
            HUMAN_VERIFIED,
        )

    def test_verify_does_not_overwrite_base_quality(self):
        row = self._create_result(
            self._create_doc(),
            quality=DocumentTextResult.Quality.GOOD,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text="ready to verify",
        )
        verify_pending_text_result(
            result_id=row.id,
            new_text="ready to verify",
            editor=self.staff,
        )
        row.refresh_from_db()
        self.assertEqual(row.quality, DocumentTextResult.Quality.GOOD)
        self.assertEqual(
            row.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(
            effective_public_text_quality_for_result(row),
            HUMAN_VERIFIED,
        )

    def test_staff_created_manual_text_resolves_to_human_verified(self):
        item = create_manual_text_archive_item(
            title="Staff manual text",
            body="Typed by staff",
        )
        content = item.manual_text_content
        self.assertEqual(
            effective_public_text_quality_for_manual_text(content),
            HUMAN_VERIFIED,
        )

    def test_capped_inherited_base_quality_hook(self):
        self.assertEqual(
            capped_inherited_base_quality(DocumentTextResult.Quality.GOOD),
            DocumentTextResult.Quality.GOOD,
        )
        self.assertEqual(
            capped_inherited_base_quality(
                DocumentTextResult.Quality.MEDIUM,
                DocumentTextResult.Quality.GOOD,
            ),
            DocumentTextResult.Quality.MEDIUM,
        )
        self.assertEqual(
            capped_inherited_base_quality(
                DocumentTextResult.Quality.GOOD,
                DocumentTextResult.Quality.LOW,
            ),
            DocumentTextResult.Quality.LOW,
        )
        self.assertEqual(
            capped_inherited_base_quality(
                HUMAN_VERIFIED,
                DocumentTextResult.Quality.GOOD,
            ),
            DocumentTextResult.Quality.UNKNOWN,
        )


@override_settings(UPLOADS_BUCKET_NAME="")
class TextQualityTranslationInheritanceTests(TestCase):
    ENGINE = "gemini-2.0-flash"

    def _create_doc(self) -> Document:
        return create_ocr_document(
            title="Translation quality inherit doc",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.PRINTED,
            language=Document.Language.ENGLISH,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.PARTIAL,
            file_s3_key="documents/quality/translation.jpg",
            mime_type="image/jpeg",
        )

    def _create_source(
        self,
        doc: Document,
        *,
        quality: str = DocumentTextResult.Quality.UNKNOWN,
        verification_status: str = DocumentTextResult.VerificationStatus.UNVERIFIED,
        source_revision: int = 3,
    ) -> DocumentTextResult:
        return DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine=self.ENGINE,
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=verification_status,
            text="recognized source text long enough",
            quality=quality,
            source_revision=source_revision,
        )

    def _persist_success(self, doc: Document) -> DocumentTextResult:
        persist_hebrew_translation_result(
            doc,
            self.ENGINE,
            translation=GeminiResult(
                text="translated hebrew text long enough",
                engine_name=self.ENGINE,
            ),
            min_text_length=5,
        )
        return DocumentTextResult.objects.get(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine=self.ENGINE,
        )

    def test_successful_translation_inherits_unknown_low_medium_and_good(self):
        for quality in (
            DocumentTextResult.Quality.UNKNOWN,
            DocumentTextResult.Quality.LOW,
            DocumentTextResult.Quality.MEDIUM,
            DocumentTextResult.Quality.GOOD,
        ):
            with self.subTest(quality=quality):
                doc = self._create_doc()
                source = self._create_source(doc, quality=quality)
                hebrew = self._persist_success(doc)
                self.assertEqual(hebrew.quality, quality)
                self.assertEqual(
                    hebrew.based_on_source_revision,
                    source.source_revision,
                )
                self.assertEqual(
                    hebrew.status,
                    DocumentTextResult.Status.NEEDS_REVIEW,
                )
                self.assertEqual(
                    hebrew.verification_status,
                    DocumentTextResult.VerificationStatus.UNVERIFIED,
                )
                self.assertNotIn(hebrew.quality, (HUMAN_VERIFIED, NEEDS_CORRECTION))

    def test_verified_source_does_not_promote_persisted_translation_quality(self):
        doc = self._create_doc()
        self._create_source(
            doc,
            quality=DocumentTextResult.Quality.GOOD,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
        )
        hebrew = self._persist_success(doc)
        self.assertEqual(hebrew.quality, DocumentTextResult.Quality.GOOD)
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )
        self.assertEqual(
            effective_public_text_quality_for_result(hebrew),
            DocumentTextResult.Quality.GOOD,
        )
        self.assertNotEqual(
            effective_public_text_quality_for_result(hebrew),
            HUMAN_VERIFIED,
        )

    def test_rejected_source_does_not_persist_needs_correction_on_translation(self):
        doc = self._create_doc()
        self._create_source(
            doc,
            quality=DocumentTextResult.Quality.MEDIUM,
            verification_status=DocumentTextResult.VerificationStatus.REJECTED,
        )
        hebrew = self._persist_success(doc)
        self.assertEqual(hebrew.quality, DocumentTextResult.Quality.MEDIUM)
        self.assertNotEqual(hebrew.quality, NEEDS_CORRECTION)
        self.assertEqual(
            hebrew.verification_status,
            DocumentTextResult.VerificationStatus.UNVERIFIED,
        )

    def test_missing_source_fails_closed_to_unknown_without_persistence_failure(self):
        doc = self._create_doc()
        hebrew = self._persist_success(doc)
        self.assertEqual(hebrew.quality, DocumentTextResult.Quality.UNKNOWN)
        self.assertIsNone(hebrew.based_on_source_revision)
        self.assertEqual(hebrew.status, DocumentTextResult.Status.NEEDS_REVIEW)
        self.assertEqual(hebrew.text, "translated hebrew text long enough")
