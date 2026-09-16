from __future__ import annotations

from unittest.mock import Mock, patch

from django.contrib.auth import get_user_model
from django.test import TestCase

from documents.models import (
    ArchiveItem,
    Document,
    DocumentTextResult,
    DocumentTextResultEdit,
    TranskribusTextResultBinding,
    TranskribusTranscriptSnapshot,
)
from documents.services.transkribus_legacy_import import (
    LegacyImportError,
    LegacyImportPlan,
    LegacySelectedPage,
    LegacyTextEquivalence,
    apply_legacy_transkribus_import,
    classify_legacy_text,
    normalize_legacy_whitespace,
)

User = get_user_model()


class LegacyWhitespaceEquivalenceTests(TestCase):
    def test_exact(self):
        self.assertEqual(
            classify_legacy_text("abc\n123", "abc\n123"),
            LegacyTextEquivalence.EXACT,
        )

    def test_crlf_is_whitespace_only(self):
        self.assertEqual(
            classify_legacy_text("abc\r\n123", "abc\n123"),
            LegacyTextEquivalence.WHITESPACE_ONLY,
        )

    def test_nbsp_is_whitespace_only(self):
        self.assertEqual(
            classify_legacy_text("abc def", "abc\u00a0def"),
            LegacyTextEquivalence.WHITESPACE_ONLY,
        )

    def test_horizontal_space_runs_are_whitespace_only(self):
        self.assertEqual(
            classify_legacy_text("abc   def", "abc def"),
            LegacyTextEquivalence.WHITESPACE_ONLY,
        )

    def test_line_edge_spaces_are_whitespace_only(self):
        self.assertEqual(
            classify_legacy_text(" abc \n def ", "abc\ndef"),
            LegacyTextEquivalence.WHITESPACE_ONLY,
        )

    def test_blank_lines_are_not_removed(self):
        self.assertEqual(
            classify_legacy_text("abc\n\n123", "abc\n123"),
            LegacyTextEquivalence.CONTENT_MISMATCH,
        )

    def test_actual_text_change_is_content_mismatch(self):
        self.assertEqual(
            classify_legacy_text("abc", "abd"),
            LegacyTextEquivalence.CONTENT_MISMATCH,
        )

    def test_normalizer_does_not_change_letters_or_punctuation(self):
        value = 'אב"ג, 123!'
        self.assertEqual(normalize_legacy_whitespace(value), value)


class LegacyImportApplyTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="legacy-import-admin",
            password="x",
            is_staff=True,
            is_superuser=True,
        )
        self.item = ArchiveItem.objects.create(
            title="Legacy Transkribus test",
        )
        self.document = Document.objects.create(
            archive_item=self.item,
            language=Document.Language.HEBREW,
        )
        self.source = DocumentTextResult.objects.create(
            document=self.document,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="transkribus-pylaia:test",
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.SUCCEEDED,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="SOURCE MUST STAY",
            source_revision=7,
        )
        self.hebrew = DocumentTextResult.objects.create(
            document=self.document,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="transkribus-pylaia:test",
            engine_key=DocumentTextResult.OcrEngineKey.TRANSKRIBUS,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.SUCCEEDED,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="abc\r\ndef",
            source_revision=1,
            based_on_source_revision=7,
        )

    def _snapshot(self, *, text="abc\ndef", hover_eligible=True):
        import hashlib

        sha = hashlib.sha256(text.encode("utf-8")).hexdigest()
        return TranskribusTranscriptSnapshot.objects.create(
            document=self.document,
            source_kind=TranskribusTranscriptSnapshot.SourceKind.LEGACY_IMPORT,
            remote_doc_id="14560983",
            collection_id="2339723",
            model_id="",
            recognition_job_id="",
            parser_version="page_xml_snapshot_v1",
            provider_identity_fingerprint="p" * 64,
            raw_xml_fingerprint="r" * 64,
            canonical_text=text,
            canonical_text_sha256=sha,
            geometry_capability="LINE_GEOMETRY",
            hover_eligible=hover_eligible,
            storage_status=TranskribusTranscriptSnapshot.StorageStatus.READY,
            created_by=self.user,
        )

    def _plan(
        self,
        *,
        equivalence=LegacyTextEquivalence.WHITESPACE_ONLY,
        current_text_sha256=None,
        canonical_text="abc\ndef",
    ):
        import hashlib

        current = self.hebrew.text or ""
        return LegacyImportPlan(
            document_id=self.document.id,
            text_result_id=self.hebrew.id,
            collection_id="2339723",
            remote_doc_id="14560983",
            page_index_to_page_nr={1: 2},
            selected_pages=(
                LegacySelectedPage(
                    page_index=1,
                    page_nr=2,
                    transcript_ts_id="280183640",
                    remote_transcript_status="GT",
                ),
            ),
            current_text_sha256=current_text_sha256
            or hashlib.sha256(current.encode("utf-8")).hexdigest(),
            canonical_text_sha256=hashlib.sha256(
                canonical_text.encode("utf-8")
            ).hexdigest(),
            current_text_chars=len(current),
            canonical_text_chars=len(canonical_text),
            based_on_source_revision=7,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            equivalence=equivalence,
            bytes_would_change=current != canonical_text,
            canonical_text=canonical_text,
            snapshot_inputs=(),
            geometry_capability="LINE_GEOMETRY",
            hover_eligible=True,
        )

    @patch(
        "documents.services.transkribus_legacy_import.sync_archive_item_search_index"
    )
    @patch(
        "documents.services.transkribus_legacy_import.is_binding_trusted_for_hover",
        return_value=True,
    )
    @patch(
        "documents.services.transkribus_legacy_import.is_binding_structurally_fresh",
        return_value=True,
    )
    @patch(
        "documents.services.transkribus_legacy_import.store_transkribus_transcript_snapshot"
    )
    def test_whitespace_only_aligns_hebrew_preserves_verified_and_source(
        self,
        store_snapshot,
        _fresh,
        _hover,
        _sync_index,
    ):
        snapshot = self._snapshot()
        store_snapshot.return_value = Mock(
            snapshot=snapshot,
            outcome=Mock(value="CREATED"),
        )

        result = apply_legacy_transkribus_import(
            plan=self._plan(),
            actor=self.user,
        )

        self.hebrew.refresh_from_db()
        self.source.refresh_from_db()

        self.assertEqual(self.hebrew.text, "abc\ndef")
        self.assertEqual(
            self.hebrew.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )
        self.assertEqual(self.hebrew.based_on_source_revision, 7)

        self.assertEqual(self.source.text, "SOURCE MUST STAY")
        self.assertEqual(self.source.source_revision, 7)
        self.assertEqual(
            self.source.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )

        self.assertTrue(result.text_changed)

        edit = DocumentTextResultEdit.objects.get(text_result=self.hebrew)
        self.assertEqual(edit.edit_type, DocumentTextResultEdit.EditType.HEBREW_TEXT)
        self.assertEqual(edit.old_text, "abc\r\ndef")
        self.assertEqual(edit.new_text, "abc\ndef")

        binding = TranskribusTextResultBinding.objects.get(text_result=self.hebrew)
        self.assertEqual(binding.snapshot_id, snapshot.id)
        self.assertEqual(
            binding.binding_role,
            TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
        )
        self.assertEqual(binding.bound_source_revision, 7)

        self.assertFalse(
            TranskribusTextResultBinding.objects.filter(
                text_result=self.source
            ).exists()
        )

    @patch(
        "documents.services.transkribus_legacy_import.store_transkribus_transcript_snapshot"
    )
    def test_content_mismatch_is_blocked_before_storage(self, store_snapshot):
        with self.assertRaises(LegacyImportError):
            apply_legacy_transkribus_import(
                plan=self._plan(equivalence=LegacyTextEquivalence.CONTENT_MISMATCH),
                actor=self.user,
            )

        store_snapshot.assert_not_called()
        self.assertFalse(
            TranskribusTextResultBinding.objects.filter(
                text_result=self.hebrew
            ).exists()
        )

    @patch(
        "documents.services.transkribus_legacy_import.store_transkribus_transcript_snapshot"
    )
    def test_stale_text_is_blocked(self, store_snapshot):
        snapshot = self._snapshot()
        store_snapshot.return_value = Mock(
            snapshot=snapshot,
            outcome=Mock(value="CREATED"),
        )

        plan = self._plan()

        self.hebrew.text = "changed after preview"
        self.hebrew.save(update_fields=["text", "updated_at"])

        with self.assertRaisesRegex(LegacyImportError, "changed after"):
            apply_legacy_transkribus_import(
                plan=plan,
                actor=self.user,
            )

        self.assertFalse(
            TranskribusTextResultBinding.objects.filter(
                text_result=self.hebrew
            ).exists()
        )

    @patch(
        "documents.services.transkribus_legacy_import.sync_archive_item_search_index"
    )
    @patch(
        "documents.services.transkribus_legacy_import.is_binding_trusted_for_hover",
        return_value=True,
    )
    @patch(
        "documents.services.transkribus_legacy_import.is_binding_structurally_fresh",
        return_value=True,
    )
    @patch(
        "documents.services.transkribus_legacy_import.store_transkribus_transcript_snapshot"
    )
    def test_exact_text_does_not_create_edit(
        self,
        store_snapshot,
        _fresh,
        _hover,
        _sync_index,
    ):
        self.hebrew.text = "abc\ndef"
        self.hebrew.save(update_fields=["text", "updated_at"])

        snapshot = self._snapshot()
        store_snapshot.return_value = Mock(
            snapshot=snapshot,
            outcome=Mock(value="REUSED_EXISTING"),
        )

        result = apply_legacy_transkribus_import(
            plan=self._plan(
                equivalence=LegacyTextEquivalence.EXACT,
                canonical_text="abc\ndef",
            ),
            actor=self.user,
        )

        self.assertFalse(result.text_changed)
        self.assertFalse(
            DocumentTextResultEdit.objects.filter(text_result=self.hebrew).exists()
        )

    @patch(
        "documents.services.transkribus_legacy_import.sync_archive_item_search_index"
    )
    @patch(
        "documents.services.transkribus_legacy_import.is_binding_trusted_for_hover",
        return_value=True,
    )
    @patch(
        "documents.services.transkribus_legacy_import.is_binding_structurally_fresh",
        return_value=True,
    )
    @patch(
        "documents.services.transkribus_legacy_import.store_transkribus_transcript_snapshot"
    )
    def test_second_apply_is_idempotent_for_text_and_edit_history(
        self,
        store_snapshot,
        _fresh,
        _hover,
        _sync_index,
    ):
        snapshot = self._snapshot()
        store_snapshot.return_value = Mock(
            snapshot=snapshot,
            outcome=Mock(value="REUSED_EXISTING"),
        )

        first_plan = self._plan()
        apply_legacy_transkribus_import(
            plan=first_plan,
            actor=self.user,
        )

        self.hebrew.refresh_from_db()
        self.assertEqual(
            DocumentTextResultEdit.objects.filter(text_result=self.hebrew).count(),
            1,
        )

        second_plan = self._plan(
            equivalence=LegacyTextEquivalence.EXACT,
            canonical_text="abc\ndef",
        )
        second = apply_legacy_transkribus_import(
            plan=second_plan,
            actor=self.user,
        )

        self.assertFalse(second.text_changed)
        self.assertEqual(
            DocumentTextResultEdit.objects.filter(text_result=self.hebrew).count(),
            1,
        )

    @patch(
        "documents.services.transkribus_legacy_import.store_transkribus_transcript_snapshot"
    )
    def test_non_admin_actor_is_blocked_before_storage(self, store_snapshot):
        user = User.objects.create_user(
            username="legacy-import-non-admin",
            password="x",
        )

        with self.assertRaisesRegex(LegacyImportError, "administrator"):
            apply_legacy_transkribus_import(
                plan=self._plan(),
                actor=user,
            )

        store_snapshot.assert_not_called()

    @patch(
        "documents.services.transkribus_legacy_import.store_transkribus_transcript_snapshot"
    )
    def test_snapshot_sha_must_match_prepared_plan(self, store_snapshot):
        snapshot = self._snapshot(text="different")
        store_snapshot.return_value = Mock(
            snapshot=snapshot,
            outcome=Mock(value="CREATED"),
        )

        with self.assertRaisesRegex(LegacyImportError, "prepared import plan"):
            apply_legacy_transkribus_import(
                plan=self._plan(),
                actor=self.user,
            )

        self.hebrew.refresh_from_db()
        self.assertEqual(self.hebrew.text, "abc\r\ndef")

    @patch(
        "documents.services.transkribus_legacy_import.store_transkribus_transcript_snapshot"
    )
    def test_verification_status_change_after_plan_is_stale(self, store_snapshot):
        snapshot = self._snapshot()
        store_snapshot.return_value = Mock(
            snapshot=snapshot,
            outcome=Mock(value="CREATED"),
        )

        plan = self._plan()
        self.hebrew.verification_status = (
            DocumentTextResult.VerificationStatus.UNVERIFIED
        )
        self.hebrew.save(update_fields=["verification_status", "updated_at"])

        with self.assertRaisesRegex(LegacyImportError, "verification_status changed"):
            apply_legacy_transkribus_import(
                plan=plan,
                actor=self.user,
            )

    @patch(
        "documents.services.transkribus_legacy_import.is_binding_trusted_for_hover",
        return_value=False,
    )
    @patch(
        "documents.services.transkribus_legacy_import.is_binding_structurally_fresh",
        return_value=True,
    )
    @patch(
        "documents.services.transkribus_legacy_import.store_transkribus_transcript_snapshot"
    )
    def test_untrusted_hover_rolls_back_text_and_binding(
        self,
        store_snapshot,
        _fresh,
        _hover,
    ):
        snapshot = self._snapshot()
        store_snapshot.return_value = Mock(
            snapshot=snapshot,
            outcome=Mock(value="CREATED"),
        )

        with self.assertRaisesRegex(LegacyImportError, "not trusted for hover"):
            apply_legacy_transkribus_import(
                plan=self._plan(),
                actor=self.user,
            )

        self.hebrew.refresh_from_db()
        self.assertEqual(self.hebrew.text, "abc\r\ndef")
        self.assertFalse(
            TranskribusTextResultBinding.objects.filter(
                text_result=self.hebrew
            ).exists()
        )
        self.assertFalse(
            DocumentTextResultEdit.objects.filter(text_result=self.hebrew).exists()
        )

    @patch(
        "documents.services.transkribus_legacy_import.sync_archive_item_search_index"
    )
    @patch(
        "documents.services.transkribus_legacy_import.store_transkribus_transcript_snapshot"
    )
    def test_real_binding_freshness_and_hover_trust_succeed(
        self,
        store_snapshot,
        _sync_index,
    ):
        self.hebrew.text = "abc\ndef"
        self.hebrew.save(update_fields=["text", "updated_at"])

        snapshot = self._snapshot(
            text="abc\ndef",
            hover_eligible=True,
        )
        store_snapshot.return_value = Mock(
            snapshot=snapshot,
            outcome=Mock(value="REUSED_EXISTING"),
        )

        result = apply_legacy_transkribus_import(
            plan=self._plan(
                equivalence=LegacyTextEquivalence.EXACT,
                canonical_text="abc\ndef",
            ),
            actor=self.user,
        )

        self.assertTrue(result.binding_structurally_fresh)
        self.assertTrue(result.hover_trusted)

        binding = TranskribusTextResultBinding.objects.get(text_result=self.hebrew)
        self.assertEqual(
            binding.binding_role,
            TranskribusTextResultBinding.BindingRole.HEBREW_MIRROR,
        )
        self.assertEqual(binding.bound_source_revision, 7)
