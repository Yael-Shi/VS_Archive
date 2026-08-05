"""Manager-facing ArchiveItem visibility display (UI presentation only)."""

from __future__ import annotations

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveMetadataSuggestion,
    Document,
    DocumentTextResult,
    TranscriptionEditSuggestion,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
)
from documents.services.archive_item_presentation import (
    archive_visibility_ui_choices,
    visibility_choice_label,
    visibility_display_label,
    visibility_label,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
)
from documents.templatetags.archive_labels import (
    archive_visibility_choice_label,
    archive_visibility_display_label,
    archive_visibility_label,
)

User = get_user_model()

RESTRICTED_CHOICE_LABEL = "רגיש — למורשים בלבד"
RESTRICTED_DISPLAY_LABEL = "רגיש"


def _grant_restricted_permission(user):
    ct = ContentType.objects.get_for_model(ArchiveItem)
    perm = Permission.objects.get(
        codename=VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
        content_type=ct,
    )
    user.user_permissions.add(perm)
    if hasattr(user, "_perm_cache"):
        delattr(user, "_perm_cache")
    if hasattr(user, "_user_perm_cache"):
        delattr(user, "_user_perm_cache")
    return user


def _create_review_ocr(*, title: str, visibility: str) -> Document:
    doc = create_ocr_document(
        title=title,
        doc_type=Document.DocType.IMAGE,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        language=Document.Language.HEBREW,
        visibility=visibility,
        upload_status=Document.UploadStatus.UPLOADED,
        processing_state_user=Document.ProcessingState.READY,
        file_s3_key=f"documents/visibility-ui/{title}.jpg",
        mime_type="image/jpeg",
        metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
    )
    DocumentTextResult.objects.create(
        document=doc,
        result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
        engine="engine-visibility-ui",
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
        status=DocumentTextResult.Status.NEEDS_REVIEW,
        verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
        text="visibility-ui-review-text",
        review_reasons='["AUTOMATIC_OCR_REQUIRES_HUMAN_REVIEW"]',
    )
    return doc


@override_settings(
    STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage"
)
class VisibilityDisplayLabelTests(TestCase):
    def test_short_display_labels(self):
        self.assertEqual(visibility_display_label("public"), "ציבורי")
        self.assertEqual(visibility_display_label("private"), "פרטי")
        self.assertEqual(
            visibility_display_label(ArchiveItem.Visibility.RESTRICTED),
            RESTRICTED_DISPLAY_LABEL,
        )

    def test_choice_labels_keep_full_restricted_text(self):
        self.assertEqual(visibility_choice_label("public"), "ציבורי")
        self.assertEqual(visibility_choice_label("private"), "פרטי")
        self.assertEqual(
            visibility_choice_label(ArchiveItem.Visibility.RESTRICTED),
            RESTRICTED_CHOICE_LABEL,
        )
        self.assertEqual(
            visibility_label(ArchiveItem.Visibility.RESTRICTED),
            RESTRICTED_CHOICE_LABEL,
        )

    def test_unknown_visibility_is_not_mapped_to_a_known_label(self):
        self.assertEqual(visibility_display_label("secret"), "secret")
        self.assertEqual(visibility_choice_label("secret"), "secret")
        self.assertNotEqual(visibility_display_label("secret"), "ציבורי")
        self.assertNotEqual(visibility_display_label("secret"), "פרטי")
        self.assertNotEqual(
            visibility_display_label("secret"), RESTRICTED_DISPLAY_LABEL
        )

    def test_ui_choices_still_use_full_restricted_choice_label(self):
        staff = User.objects.create_user(
            username="choice_staff", password="x", is_staff=True
        )
        _grant_restricted_permission(staff)
        choices = archive_visibility_ui_choices(staff)
        self.assertIn(
            (ArchiveItem.Visibility.RESTRICTED, RESTRICTED_CHOICE_LABEL),
            choices,
        )
        labels = [label for _value, label in choices]
        self.assertEqual(
            labels.count(RESTRICTED_CHOICE_LABEL),
            1,
        )
        self.assertFalse(any(label == RESTRICTED_DISPLAY_LABEL for label in labels))

    def test_template_filters_keep_display_and_choice_semantics(self):
        restricted = ArchiveItem.Visibility.RESTRICTED
        self.assertEqual(
            archive_visibility_display_label(restricted),
            RESTRICTED_DISPLAY_LABEL,
        )
        self.assertEqual(
            archive_visibility_choice_label(restricted),
            RESTRICTED_CHOICE_LABEL,
        )
        self.assertEqual(
            archive_visibility_label(restricted),
            RESTRICTED_CHOICE_LABEL,
        )
        self.assertEqual(
            archive_visibility_label(restricted),
            visibility_label(restricted),
        )
        self.assertEqual(
            archive_visibility_label(restricted),
            visibility_choice_label(restricted),
        )


@override_settings(
    STATICFILES_STORAGE="django.contrib.staticfiles.storage.StaticFilesStorage",
    UPLOADS_BUCKET_NAME="",
)
class VisibilityMetadataUiSurfaceTests(TestCase):
    def setUp(self):
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.staff = User.objects.create_user(
            username="vis_ui_staff", password="x", is_staff=True
        )
        self.staff_with_perm = User.objects.create_user(
            username="vis_ui_staff_perm", password="x", is_staff=True
        )
        _grant_restricted_permission(self.staff_with_perm)
        self.family = User.objects.create_user(username="vis_ui_family", password="x")
        self.family.groups.add(self.family_group)

        self.public_item = create_manual_text_archive_item(
            title="Vis-UI Public Manual",
            body="public body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.private_item = create_manual_text_archive_item(
            title="Vis-UI Private Manual",
            body="private body",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.restricted_item = create_manual_text_archive_item(
            title="Vis-UI Restricted Manual",
            body="restricted body",
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )

        self.public_ocr = _create_review_ocr(
            title="Vis-UI Public OCR",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.private_ocr = _create_review_ocr(
            title="Vis-UI Private OCR",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.restricted_ocr = _create_review_ocr(
            title="Vis-UI Restricted OCR",
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )

        ArchiveMetadataSuggestion.objects.create(
            archive_item=self.public_item,
            submitter_name="Suggestor",
            suggested_categories="cat",
            status=ArchiveMetadataSuggestion.Status.PENDING,
        )
        ArchiveMetadataSuggestion.objects.create(
            archive_item=self.restricted_item,
            submitter_name="Suggestor Restricted",
            suggested_categories="secret-cat",
            status=ArchiveMetadataSuggestion.Status.PENDING,
        )
        TranscriptionEditSuggestion.objects.create(
            document=self.public_ocr,
            current_text_snapshot="visibility-ui-review-text",
            suggested_text="suggested public text",
            submitter_name="Transcribe Suggestor",
            status=TranscriptionEditSuggestion.Status.PENDING,
        )
        TranscriptionEditSuggestion.objects.create(
            document=self.restricted_ocr,
            current_text_snapshot="visibility-ui-review-text",
            suggested_text="suggested restricted text",
            submitter_name="Transcribe Restricted",
            status=TranscriptionEditSuggestion.Status.PENDING,
        )

    def test_manage_list_shows_short_visibility_for_each_value(self):
        self.client.force_login(self.staff_with_perm)
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("ציבורי", html)
        self.assertIn("פרטי", html)
        self.assertIn(RESTRICTED_DISPLAY_LABEL, html)
        self.assertNotIn(RESTRICTED_CHOICE_LABEL, html)
        self.assertIn(self.public_item.title, html)
        self.assertIn(self.private_item.title, html)
        self.assertIn(self.restricted_item.title, html)

    def test_archive_detail_shows_short_visibility_for_managers_only(self):
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_item.pk})
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn("ציבורי", html)
        self.assertNotIn('class="badge badge-ok"', html)
        self.assertNotIn(RESTRICTED_CHOICE_LABEL, html)

        self.client.force_login(self.family)
        family_resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_item.pk})
        )
        self.assertEqual(family_resp.status_code, 200)
        family_html = family_resp.content.decode()
        # Family viewers keep date/title; manager visibility text is not exposed.
        self.assertNotIn("ציבורי", family_html)
        self.assertNotIn("פרטי", family_html)
        self.assertNotIn(RESTRICTED_DISPLAY_LABEL, family_html)

        self.client.logout()
        anon = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.public_item.pk})
        )
        # Anonymous may view public items; still no manager visibility metadata.
        self.assertEqual(anon.status_code, 200)
        anon_html = anon.content.decode()
        self.assertNotIn("ציבורי", anon_html)

    def test_restricted_detail_remains_404_for_unauthorized_staff(self):
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.restricted_item.pk})
        )
        self.assertEqual(resp.status_code, 404)

        self.client.force_login(self.staff_with_perm)
        ok = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.restricted_item.pk})
        )
        self.assertEqual(ok.status_code, 200)
        self.assertContains(ok, RESTRICTED_DISPLAY_LABEL)
        self.assertNotContains(ok, RESTRICTED_CHOICE_LABEL)

    def test_documents_list_shows_plain_short_visibility_for_staff(self):
        self.client.force_login(self.staff_with_perm)
        resp = self.client.get(reverse("documents-list-page"))
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertRegex(html, r"<td>\s*ציבורי\s*</td>")
        self.assertRegex(html, r"<td>\s*פרטי\s*</td>")
        self.assertRegex(html, r"<td>\s*רגיש\s*</td>")
        # Visibility is ordinary table text, not a badge.
        self.assertNotRegex(
            html,
            r"badge[^>]*>\s*(ציבורי|פרטי|רגיש)\s*<",
        )
        # Filter select remains a form-choice control with the full restricted label.
        self.assertIn(RESTRICTED_CHOICE_LABEL, html)
        self.assertIn(
            f'value="{ArchiveItem.Visibility.RESTRICTED}"',
            html,
        )

    def test_documents_list_active_visibility_chip_shows_short_value_only(self):
        self.client.force_login(self.staff_with_perm)
        resp = self.client.get(
            reverse("documents-list-page"),
            {"visibility": ArchiveItem.Visibility.PUBLIC},
        )
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode()
        self.assertIn(
            '<span class="filter-chip">ציבורי</span>',
            html,
        )
        self.assertNotIn(
            '<span class="filter-chip__key">נראות:</span>',
            html,
        )
        self.assertNotRegex(html, r"נראות:\s*ציבורי")
        # Select control label and full restricted choice option remain.
        self.assertIn('for="filter-visibility">נראות</label>', html)
        self.assertIn(RESTRICTED_CHOICE_LABEL, html)
        self.assertIn(
            f'value="{ArchiveItem.Visibility.RESTRICTED}"',
            html,
        )

    def test_edit_form_keeps_full_restricted_choice_label(self):
        self.client.force_login(self.staff_with_perm)
        resp = self.client.get(
            reverse("archive-manage-edit", kwargs={"item_id": self.public_item.pk})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, RESTRICTED_CHOICE_LABEL)
        # The select option uses the full label; short-only display is not required here.
        html = resp.content.decode()
        self.assertIn(f'value="{ArchiveItem.Visibility.RESTRICTED}"', html)

    def test_backlog_and_review_queues_show_short_visibility(self):
        self.client.force_login(self.staff_with_perm)
        backlog = self.client.get(reverse("admin-backlog-page"))
        self.assertEqual(backlog.status_code, 200)
        backlog_html = backlog.content.decode()
        self.assertIn("ציבורי", backlog_html)
        self.assertIn("פרטי", backlog_html)
        self.assertIn(RESTRICTED_DISPLAY_LABEL, backlog_html)
        self.assertNotIn(RESTRICTED_CHOICE_LABEL, backlog_html)

        review = self.client.get(reverse("review-backlog-page"))
        self.assertEqual(review.status_code, 200)
        review_html = review.content.decode()
        self.assertIn("ציבורי", review_html)
        self.assertIn("פרטי", review_html)
        self.assertIn(RESTRICTED_DISPLAY_LABEL, review_html)
        self.assertNotIn(RESTRICTED_CHOICE_LABEL, review_html)

        review_detail = self.client.get(
            reverse("review-detail-page", kwargs={"doc_id": self.public_ocr.pk})
        )
        self.assertEqual(review_detail.status_code, 200)
        self.assertContains(review_detail, "ציבורי")
        self.assertNotContains(review_detail, RESTRICTED_CHOICE_LABEL)

    def test_suggestion_queues_show_short_visibility_without_leaking_restricted(self):
        self.client.force_login(self.staff)
        meta = self.client.get(reverse("archive-metadata-suggestion-backlog"))
        self.assertEqual(meta.status_code, 200)
        meta_html = meta.content.decode()
        self.assertIn(self.public_item.title, meta_html)
        self.assertIn("ציבורי", meta_html)
        self.assertNotIn(self.restricted_item.title, meta_html)
        self.assertNotIn(RESTRICTED_CHOICE_LABEL, meta_html)

        trans = self.client.get(reverse("transcription-suggestion-backlog"))
        self.assertEqual(trans.status_code, 200)
        trans_html = trans.content.decode()
        self.assertIn(self.public_ocr.archive_item.title, trans_html)
        self.assertIn("ציבורי", trans_html)
        self.assertNotIn(self.restricted_ocr.archive_item.title, trans_html)
        self.assertNotIn(RESTRICTED_CHOICE_LABEL, trans_html)

        self.client.force_login(self.staff_with_perm)
        meta_ok = self.client.get(reverse("archive-metadata-suggestion-backlog"))
        self.assertContains(meta_ok, self.restricted_item.title)
        self.assertContains(meta_ok, RESTRICTED_DISPLAY_LABEL)
        self.assertNotContains(meta_ok, RESTRICTED_CHOICE_LABEL)

        trans_ok = self.client.get(reverse("transcription-suggestion-backlog"))
        self.assertContains(trans_ok, self.restricted_ocr.archive_item.title)
        self.assertContains(trans_ok, RESTRICTED_DISPLAY_LABEL)
        self.assertNotContains(trans_ok, RESTRICTED_CHOICE_LABEL)

    def test_document_detail_lead_shows_short_visibility_for_staff_only(self):
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": self.public_ocr.pk})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ציבורי")
        self.assertNotContains(resp, RESTRICTED_CHOICE_LABEL)
        # Old technical-details badge mapping must not remain.
        html = resp.content.decode()
        self.assertNotIn('badge-ok">ציבורי', html)

        self.client.logout()
        anon = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": self.public_ocr.pk})
        )
        self.assertEqual(anon.status_code, 200)
        self.assertNotContains(anon, "ציבורי")
