"""Restricted ArchiveItem visibility — centralized authz and staff read-leak regressions."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group, Permission
from django.contrib.contenttypes.models import ContentType
from django.http import Http404
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemPersonSuggestion,
    ArchiveMetadataSuggestion,
    Document,
    DocumentTextResult,
    Person,
    PhotoContent,
    TranscriptionEditSuggestion,
    TranskribusCorrectedCurrentSyncAttempt,
    TranskribusRun,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
    archive_browse_queryset_for_user,
    archive_item_queryset_for_user,
    can_view_archive_item,
    can_view_restricted_archive_items,
    filter_archive_items_for_user,
    get_accessible_archive_item,
    get_viewable_archive_item,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
)
from documents.services.document_access import (
    document_queryset_for_user,
    filter_documents_for_user,
    get_viewable_document,
    user_can_view_document,
)

User = get_user_model()

RESTRICTED_MANUAL_TITLE = "RESTRICTED-MANUAL-SECRET-TITLE"
RESTRICTED_OCR_TITLE = "RESTRICTED-OCR-SECRET-TITLE"
RESTRICTED_PHOTO_TITLE = "RESTRICTED-PHOTO-SECRET-TITLE"
RESTRICTED_BODY = "restricted-manual-body-secret"
RESTRICTED_OCR_TEXT = "restricted-ocr-transcription-secret"
PUBLIC_TITLE = "Public visible title"
PRIVATE_TITLE = "Private family title"


def _grant_restricted_permission(user):
    ct = ContentType.objects.get_for_model(ArchiveItem)
    perm = Permission.objects.get(
        codename=VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
        content_type=ct,
    )
    user.user_permissions.add(perm)
    # Clear Django's permission cache on the instance.
    if hasattr(user, "_perm_cache"):
        delattr(user, "_perm_cache")
    if hasattr(user, "_user_perm_cache"):
        delattr(user, "_user_perm_cache")
    return user


def _create_photo(
    *,
    title: str,
    visibility: str,
    upload_status=PhotoContent.UploadStatus.UPLOADED,
) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=visibility,
    )
    PhotoContent.objects.create(
        archive_item=item,
        original_file_key="photos/restricted/original.jpg",
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=2048,
        upload_status=upload_status,
        thumbnail_file_key="photos/restricted/thumb.jpg",
        thumbnail_mime_type="image/jpeg",
        thumbnail_size_bytes=128,
    )
    return item


def _create_ocr(
    *,
    title: str,
    visibility: str,
    with_review_text: bool = False,
) -> Document:
    doc = create_ocr_document(
        title=title,
        doc_type=Document.DocType.IMAGE,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        language=Document.Language.HEBREW,
        visibility=visibility,
        upload_status=Document.UploadStatus.UPLOADED,
        processing_state_user=Document.ProcessingState.READY,
        file_s3_key="documents/restricted/original.jpg",
        mime_type="image/jpeg",
        metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
    )
    if with_review_text:
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="engine-restricted",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text=RESTRICTED_OCR_TEXT,
        )
    return doc


@override_settings(UPLOADS_BUCKET_NAME="")
class RestrictedVisibilityHelperTests(TestCase):
    def setUp(self):
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.anonymous = None
        self.plain = User.objects.create_user(username="plain_user", password="x")
        self.family = User.objects.create_user(username="family_user", password="x")
        self.family.groups.add(self.family_group)
        self.family_with_perm = User.objects.create_user(
            username="family_restricted", password="x"
        )
        self.family_with_perm.groups.add(self.family_group)
        _grant_restricted_permission(self.family_with_perm)
        self.staff = User.objects.create_user(
            username="staff_user", password="x", is_staff=True
        )
        self.staff_with_perm = User.objects.create_user(
            username="staff_restricted", password="x", is_staff=True
        )
        _grant_restricted_permission(self.staff_with_perm)
        self.superuser = User.objects.create_superuser(
            username="super_user", password="x", email="super@example.com"
        )

        self.public_manual = create_manual_text_archive_item(
            title=PUBLIC_TITLE,
            body="public body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.private_manual = create_manual_text_archive_item(
            title=PRIVATE_TITLE,
            body="private body",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.restricted_manual = create_manual_text_archive_item(
            title=RESTRICTED_MANUAL_TITLE,
            body=RESTRICTED_BODY,
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        self.restricted_ocr = _create_ocr(
            title=RESTRICTED_OCR_TITLE,
            visibility=ArchiveItem.Visibility.RESTRICTED,
            with_review_text=True,
        )
        self.restricted_photo = _create_photo(
            title=RESTRICTED_PHOTO_TITLE,
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        self.unknown_item = create_manual_text_archive_item(
            title="Unknown visibility item",
            body="unknown body",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        ArchiveItem.objects.filter(pk=self.unknown_item.pk).update(visibility="invalid")
        self.unknown_item.refresh_from_db()

    def test_permission_predicate_requires_explicit_perm(self):
        self.assertFalse(can_view_restricted_archive_items(self.anonymous))
        self.assertFalse(can_view_restricted_archive_items(self.plain))
        self.assertFalse(can_view_restricted_archive_items(self.family))
        self.assertFalse(can_view_restricted_archive_items(self.staff))
        self.assertTrue(can_view_restricted_archive_items(self.family_with_perm))
        self.assertTrue(can_view_restricted_archive_items(self.staff_with_perm))
        self.assertTrue(can_view_restricted_archive_items(self.superuser))

    def test_public_unchanged_for_all_roles(self):
        for user in (
            self.anonymous,
            self.plain,
            self.family,
            self.staff,
            self.superuser,
        ):
            self.assertTrue(can_view_archive_item(user, self.public_manual))
            self.assertIn(
                self.public_manual.pk,
                archive_item_queryset_for_user(user).values_list("pk", flat=True),
            )

    def test_private_unchanged_family_and_staff(self):
        self.assertFalse(can_view_archive_item(self.anonymous, self.private_manual))
        self.assertFalse(can_view_archive_item(self.plain, self.private_manual))
        self.assertTrue(can_view_archive_item(self.family, self.private_manual))
        self.assertTrue(can_view_archive_item(self.staff, self.private_manual))
        self.assertTrue(can_view_archive_item(self.superuser, self.private_manual))
        # Staff without restricted perm still sees private, not restricted.
        staff_ids = set(
            archive_item_queryset_for_user(self.staff).values_list("pk", flat=True)
        )
        self.assertIn(self.private_manual.pk, staff_ids)
        self.assertNotIn(self.restricted_manual.pk, staff_ids)

    def test_restricted_visible_only_with_explicit_permission(self):
        for item in (
            self.restricted_manual,
            self.restricted_ocr.archive_item,
            self.restricted_photo,
        ):
            self.assertFalse(can_view_archive_item(self.anonymous, item))
            self.assertFalse(can_view_archive_item(self.plain, item))
            self.assertFalse(can_view_archive_item(self.family, item))
            self.assertFalse(can_view_archive_item(self.staff, item))
            self.assertTrue(can_view_archive_item(self.family_with_perm, item))
            self.assertTrue(can_view_archive_item(self.staff_with_perm, item))
            self.assertTrue(can_view_archive_item(self.superuser, item))

    def test_unknown_visibility_fails_closed_for_everyone(self):
        for user in (
            self.anonymous,
            self.plain,
            self.family,
            self.family_with_perm,
            self.staff,
            self.staff_with_perm,
            self.superuser,
        ):
            self.assertFalse(can_view_archive_item(user, self.unknown_item))
            self.assertNotIn(
                self.unknown_item.pk,
                filter_archive_items_for_user(
                    user, ArchiveItem.objects.all()
                ).values_list("pk", flat=True),
            )

    def test_get_viewable_helpers_return_404_when_unauthorized(self):
        with self.assertRaises(Http404):
            get_viewable_archive_item(self.staff, self.restricted_manual.pk)
        with self.assertRaises(Http404):
            get_accessible_archive_item(self.staff, self.restricted_manual.pk)
        with self.assertRaises(Http404):
            get_viewable_document(self.staff, self.restricted_ocr.pk)
        self.assertFalse(user_can_view_document(self.staff, self.restricted_ocr))

        item = get_viewable_archive_item(
            self.staff_with_perm, self.restricted_manual.pk
        )
        self.assertEqual(item.pk, self.restricted_manual.pk)
        doc = get_viewable_document(self.staff_with_perm, self.restricted_ocr.pk)
        self.assertEqual(doc.pk, self.restricted_ocr.pk)

    def test_document_filter_excludes_restricted_for_staff_without_perm(self):
        staff_ids = set(
            document_queryset_for_user(self.staff).values_list("pk", flat=True)
        )
        self.assertNotIn(self.restricted_ocr.pk, staff_ids)
        permitted_ids = set(
            filter_documents_for_user(
                self.staff_with_perm, Document.objects.all()
            ).values_list("pk", flat=True)
        )
        self.assertIn(self.restricted_ocr.pk, permitted_ids)

    def test_browse_queryset_hides_restricted_without_permission(self):
        self.assertFalse(
            archive_browse_queryset_for_user(None)
            .filter(pk=self.restricted_manual.pk)
            .exists()
        )
        self.assertFalse(
            archive_browse_queryset_for_user(self.staff)
            .filter(pk=self.restricted_photo.pk)
            .exists()
        )
        self.assertTrue(
            archive_browse_queryset_for_user(self.superuser)
            .filter(pk=self.restricted_photo.pk)
            .exists()
        )


@override_settings(UPLOADS_BUCKET_NAME="")
class RestrictedVisibilitySurfaceTests(TestCase):
    def setUp(self):
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.plain = User.objects.create_user(username="surf_plain", password="x")
        self.family = User.objects.create_user(username="surf_family", password="x")
        self.family.groups.add(self.family_group)
        self.family_with_perm = User.objects.create_user(
            username="surf_family_perm", password="x"
        )
        self.family_with_perm.groups.add(self.family_group)
        _grant_restricted_permission(self.family_with_perm)
        self.staff = User.objects.create_user(
            username="surf_staff", password="x", is_staff=True
        )
        self.staff_with_perm = User.objects.create_user(
            username="surf_staff_perm", password="x", is_staff=True
        )
        _grant_restricted_permission(self.staff_with_perm)
        self.superuser = User.objects.create_superuser(
            username="surf_super", password="x", email="surf-super@example.com"
        )

        self.public_manual = create_manual_text_archive_item(
            title=PUBLIC_TITLE,
            body="public body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.private_manual = create_manual_text_archive_item(
            title=PRIVATE_TITLE,
            body="private body",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.restricted_manual = create_manual_text_archive_item(
            title=RESTRICTED_MANUAL_TITLE,
            body=RESTRICTED_BODY,
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        self.restricted_ocr = _create_ocr(
            title=RESTRICTED_OCR_TITLE,
            visibility=ArchiveItem.Visibility.RESTRICTED,
            with_review_text=True,
        )
        self.restricted_photo = _create_photo(
            title=RESTRICTED_PHOTO_TITLE,
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        self.suggestion = TranscriptionEditSuggestion.objects.create(
            document=self.restricted_ocr,
            current_text_snapshot=RESTRICTED_OCR_TEXT,
            suggested_text="suggested restricted text",
            submitter_name="Submitter",
        )
        self.metadata_suggestion = ArchiveMetadataSuggestion.objects.create(
            archive_item=self.restricted_manual,
            suggested_categories="cat",
            suggested_events="",
            suggested_tags="",
            submitter_name="Submitter",
        )

        self.person_suggestion_person = Person.objects.create(
            name="Restricted-Person-Suggestion-Secret"
        )
        self.person_suggestion = ArchiveItemPersonSuggestion.objects.create(
            archive_item=self.restricted_manual,
            person=self.person_suggestion_person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="Submitter",
        )
        self.sync_run = TranskribusRun.objects.create(
            document=self.restricted_ocr,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
        )
        self.sync_attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.restricted_ocr,
            transkribus_run=self.sync_run,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )

    def test_archive_list_and_detail_roles(self):
        # Anonymous: public only.
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, PUBLIC_TITLE)
        self.assertNotContains(resp, RESTRICTED_MANUAL_TITLE)
        self.assertNotContains(resp, PRIVATE_TITLE)

        for item in (
            self.restricted_manual,
            self.restricted_photo,
        ):
            self.assertEqual(
                self.client.get(
                    reverse("archive-detail", kwargs={"item_id": item.pk})
                ).status_code,
                404,
            )

        # Family without perm: private yes, restricted no.
        self.client.force_login(self.family)
        resp = self.client.get(reverse("archive-list"))
        self.assertContains(resp, PRIVATE_TITLE)
        self.assertNotContains(resp, RESTRICTED_MANUAL_TITLE)
        self.assertEqual(
            self.client.get(
                reverse("archive-detail", kwargs={"item_id": self.restricted_manual.pk})
            ).status_code,
            404,
        )

        # Family with perm: restricted yes.
        self.client.force_login(self.family_with_perm)
        resp = self.client.get(reverse("archive-list"))
        self.assertContains(resp, RESTRICTED_MANUAL_TITLE)
        self.assertEqual(
            self.client.get(
                reverse("archive-detail", kwargs={"item_id": self.restricted_manual.pk})
            ).status_code,
            200,
        )

        # Staff without perm: private yes, restricted no.
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-list"))
        self.assertContains(resp, PRIVATE_TITLE)
        self.assertNotContains(resp, RESTRICTED_MANUAL_TITLE)
        self.assertEqual(
            self.client.get(
                reverse("archive-detail", kwargs={"item_id": self.restricted_manual.pk})
            ).status_code,
            404,
        )

        # Staff with perm + superuser.
        for user in (self.staff_with_perm, self.superuser):
            self.client.force_login(user)
            resp = self.client.get(reverse("archive-list"))
            self.assertContains(resp, RESTRICTED_MANUAL_TITLE)
            self.assertEqual(
                self.client.get(
                    reverse(
                        "archive-detail",
                        kwargs={"item_id": self.restricted_manual.pk},
                    )
                ).status_code,
                200,
            )

    @override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
    @patch("documents.views.create_presigned_get", return_value="https://example/p")
    def test_restricted_photo_presign_only_when_authorized(self, mock_presign):
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.restricted_photo.pk})
        )
        self.assertEqual(resp.status_code, 404)
        mock_presign.assert_not_called()

        self.client.force_login(self.staff_with_perm)
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": self.restricted_photo.pk})
        )
        self.assertEqual(resp.status_code, 200)
        mock_presign.assert_called()
        self.assertContains(resp, RESTRICTED_PHOTO_TITLE)

    def test_restricted_ocr_document_detail_and_api(self):
        self.client.force_login(self.staff)
        self.assertEqual(
            self.client.get(
                reverse(
                    "documents-detail-page",
                    kwargs={"doc_id": self.restricted_ocr.pk},
                )
            ).status_code,
            404,
        )
        api = self.client.get(reverse("documents-list-api"))
        self.assertEqual(api.status_code, 200)
        body = api.json()
        titles = {item.get("title") for item in body["items"]}
        self.assertNotIn(RESTRICTED_OCR_TITLE, titles)

        self.client.force_login(self.staff_with_perm)
        self.assertEqual(
            self.client.get(
                reverse(
                    "documents-detail-page",
                    kwargs={"doc_id": self.restricted_ocr.pk},
                )
            ).status_code,
            200,
        )

    def test_staff_manage_list_edit_delete_hide_restricted_without_perm(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, RESTRICTED_MANUAL_TITLE)
        self.assertNotContains(resp, RESTRICTED_PHOTO_TITLE)
        self.assertEqual(
            self.client.get(
                reverse(
                    "archive-manage-edit",
                    kwargs={"item_id": self.restricted_manual.pk},
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse(
                    "archive-manage-delete",
                    kwargs={"item_id": self.restricted_manual.pk},
                )
            ).status_code,
            404,
        )

        self.client.force_login(self.staff_with_perm)
        resp = self.client.get(reverse("archive-manage-list"))
        self.assertContains(resp, RESTRICTED_MANUAL_TITLE)
        self.assertEqual(
            self.client.get(
                reverse(
                    "archive-manage-edit",
                    kwargs={"item_id": self.restricted_manual.pk},
                )
            ).status_code,
            200,
        )

    def test_admin_and_review_backlogs_hide_restricted(self):
        self.client.force_login(self.staff)
        backlog = self.client.get(reverse("admin-backlog-page"))
        self.assertEqual(backlog.status_code, 200)
        self.assertNotContains(backlog, RESTRICTED_OCR_TITLE)

        review = self.client.get(reverse("review-backlog-page"))
        self.assertEqual(review.status_code, 200)
        self.assertNotContains(review, RESTRICTED_OCR_TITLE)
        self.assertNotContains(review, RESTRICTED_OCR_TEXT)

        detail = self.client.get(
            reverse("review-detail-page", kwargs={"doc_id": self.restricted_ocr.pk})
        )
        self.assertEqual(detail.status_code, 404)

        result = self.restricted_ocr.text_results.get()
        verify = self.client.post(
            reverse("review-text-result-verify", kwargs={"result_id": result.pk})
        )
        self.assertEqual(verify.status_code, 404)

        self.client.force_login(self.staff_with_perm)
        review_ok = self.client.get(reverse("review-backlog-page"))
        self.assertContains(review_ok, RESTRICTED_OCR_TITLE)
        detail_ok = self.client.get(
            reverse("review-detail-page", kwargs={"doc_id": self.restricted_ocr.pk})
        )
        self.assertEqual(detail_ok.status_code, 200)
        self.assertContains(detail_ok, RESTRICTED_OCR_TEXT)

    def test_suggestion_and_sync_surfaces_hide_restricted(self):
        self.client.force_login(self.staff)

        t_backlog = self.client.get(reverse("transcription-suggestion-backlog"))
        self.assertEqual(t_backlog.status_code, 200)
        self.assertNotContains(t_backlog, RESTRICTED_OCR_TITLE)
        self.assertNotContains(t_backlog, "suggested restricted text")

        t_detail = self.client.get(
            reverse(
                "transcription-suggestion-detail",
                kwargs={"suggestion_id": self.suggestion.pk},
            )
        )
        self.assertEqual(t_detail.status_code, 404)

        m_backlog = self.client.get(reverse("archive-metadata-suggestion-backlog"))
        self.assertEqual(m_backlog.status_code, 200)
        self.assertNotContains(m_backlog, RESTRICTED_MANUAL_TITLE)

        p_backlog = self.client.get(reverse("archive-item-person-suggestion-backlog"))
        self.assertEqual(p_backlog.status_code, 200)
        self.assertNotContains(p_backlog, RESTRICTED_MANUAL_TITLE)
        self.assertNotContains(p_backlog, "Restricted-Person-Suggestion-Secret")

        sync_list = self.client.get(
            reverse(
                "corrected-current-sync-attempts",
                kwargs={"doc_id": self.restricted_ocr.pk},
            )
        )
        self.assertEqual(sync_list.status_code, 404)
        sync_detail = self.client.get(
            reverse(
                "corrected-current-sync-attempt-detail",
                kwargs={
                    "doc_id": self.restricted_ocr.pk,
                    "attempt_id": self.sync_attempt.pk,
                },
            )
        )
        self.assertEqual(sync_detail.status_code, 404)

        paragraph_editor = self.client.get(
            reverse(
                "transkribus-paragraphs",
                kwargs={"doc_id": self.restricted_ocr.pk},
            )
        )
        self.assertEqual(paragraph_editor.status_code, 404)

        self.client.force_login(self.staff_with_perm)
        t_detail_ok = self.client.get(
            reverse(
                "transcription-suggestion-detail",
                kwargs={"suggestion_id": self.suggestion.pk},
            )
        )
        self.assertEqual(t_detail_ok.status_code, 200)
        self.assertContains(t_detail_ok, RESTRICTED_OCR_TITLE)
        sync_ok = self.client.get(
            reverse(
                "corrected-current-sync-attempts",
                kwargs={"doc_id": self.restricted_ocr.pk},
            )
        )
        self.assertEqual(sync_ok.status_code, 200)
        paragraph_ok = self.client.get(
            reverse(
                "transkribus-paragraphs",
                kwargs={"doc_id": self.restricted_ocr.pk},
            )
        )
        self.assertEqual(paragraph_ok.status_code, 200)
        p_ok = self.client.get(reverse("archive-item-person-suggestion-backlog"))
        self.assertContains(p_ok, RESTRICTED_MANUAL_TITLE)
        self.assertContains(p_ok, "Restricted-Person-Suggestion-Secret")

    def test_public_suggestion_form_404_for_restricted_without_perm(self):
        self.assertEqual(
            self.client.get(
                reverse(
                    "transcription-suggestion-new",
                    kwargs={"doc_id": self.restricted_ocr.pk},
                )
            ).status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                reverse(
                    "archive-metadata-suggestion-new",
                    kwargs={"item_id": self.restricted_manual.pk},
                )
            ).status_code,
            404,
        )
        self.client.force_login(self.family_with_perm)
        # OCR suggestion form requires displayable text — present here.
        self.assertEqual(
            self.client.get(
                reverse(
                    "transcription-suggestion-new",
                    kwargs={"doc_id": self.restricted_ocr.pk},
                )
            ).status_code,
            200,
        )


@override_settings(UPLOADS_BUCKET_NAME="")
class RestrictedVisibilityMutationGateTests(TestCase):
    """Staff without restricted permission must 404 before mutation/enqueue calls."""

    def setUp(self):
        self.staff = User.objects.create_user(
            username="mut_staff", password="x", is_staff=True
        )
        self.staff_with_perm = User.objects.create_user(
            username="mut_staff_perm", password="x", is_staff=True
        )
        _grant_restricted_permission(self.staff_with_perm)

        self.restricted_manual = create_manual_text_archive_item(
            title=RESTRICTED_MANUAL_TITLE,
            body=RESTRICTED_BODY,
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        self.restricted_ocr = _create_ocr(
            title=RESTRICTED_OCR_TITLE,
            visibility=ArchiveItem.Visibility.RESTRICTED,
            with_review_text=True,
        )
        self.pending_result = self.restricted_ocr.text_results.get()
        self.verified_result = DocumentTextResult.objects.create(
            document=self.restricted_ocr,
            result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
            engine="engine-verified-restricted",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.SUCCEEDED,
            verification_status=DocumentTextResult.VerificationStatus.VERIFIED,
            text="verified restricted source",
            source_revision=1,
        )
        self.suggestion = TranscriptionEditSuggestion.objects.create(
            document=self.restricted_ocr,
            current_text_snapshot=RESTRICTED_OCR_TEXT,
            suggested_text="suggested restricted text",
            submitter_name="Submitter",
        )
        self.metadata_suggestion = ArchiveMetadataSuggestion.objects.create(
            archive_item=self.restricted_manual,
            suggested_categories="cat",
            suggested_events="",
            suggested_tags="",
            submitter_name="Submitter",
        )

        self.person_suggestion_person = Person.objects.create(
            name="Restricted-Person-Suggestion-Secret"
        )
        self.person_suggestion = ArchiveItemPersonSuggestion.objects.create(
            archive_item=self.restricted_manual,
            person=self.person_suggestion_person,
            action=ArchiveItemPersonSuggestion.Action.ADD,
            submitter_name="Submitter",
        )
        self.sync_run = TranskribusRun.objects.create(
            document=self.restricted_ocr,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col",
            model_id="42",
            remote_doc_id="777",
            pages_query="1",
            recognition_job_id="job-1",
            page_index_to_page_nr={1: 1},
        )
        # STARTED satisfies DB shape constraints; activation authz runs before
        # eligibility/service work, so COMPLETED+snapshot is unnecessary here.
        self.sync_attempt = TranskribusCorrectedCurrentSyncAttempt.objects.create(
            document=self.restricted_ocr,
            transkribus_run=self.sync_run,
            status=TranskribusCorrectedCurrentSyncAttempt.Status.STARTED,
        )

    def _post_as(self, user, url, data=None):
        self.client.force_login(user)
        return self.client.post(url, data=data or {})

    @patch("documents.views.edit_pending_text_result")
    def test_review_update_text_404_without_perm_before_mutation(self, mock_edit):
        resp = self._post_as(
            self.staff,
            reverse(
                "review-text-result-update-text",
                kwargs={"result_id": self.pending_result.pk},
            ),
            data={"text": "new text"},
        )
        self.assertEqual(resp.status_code, 404)
        mock_edit.assert_not_called()

    @patch("documents.views.edit_verified_text_result")
    def test_review_verified_edit_404_without_perm_before_mutation(self, mock_edit):
        resp = self._post_as(
            self.staff,
            reverse(
                "review-text-result-verified-edit",
                kwargs={"result_id": self.verified_result.pk},
            ),
            data={"text": "edited verified"},
        )
        self.assertEqual(resp.status_code, 404)
        mock_edit.assert_not_called()

    def test_review_verify_reject_404_without_perm_no_status_change(self):
        before = self.pending_result.verification_status
        verify = self._post_as(
            self.staff,
            reverse(
                "review-text-result-verify",
                kwargs={"result_id": self.pending_result.pk},
            ),
            data={"text": self.pending_result.text},
        )
        self.assertEqual(verify.status_code, 404)
        self.pending_result.refresh_from_db()
        self.assertEqual(self.pending_result.verification_status, before)

        reject = self._post_as(
            self.staff,
            reverse(
                "review-text-result-reject",
                kwargs={"result_id": self.pending_result.pk},
            ),
        )
        self.assertEqual(reject.status_code, 404)
        self.pending_result.refresh_from_db()
        self.assertEqual(self.pending_result.verification_status, before)

    def test_review_verify_succeeds_with_permission(self):
        resp = self._post_as(
            self.staff_with_perm,
            reverse(
                "review-text-result-verify",
                kwargs={"result_id": self.pending_result.pk},
            ),
            data={"text": self.pending_result.text},
        )
        self.assertEqual(resp.status_code, 302)
        self.pending_result.refresh_from_db()
        self.assertEqual(
            self.pending_result.verification_status,
            DocumentTextResult.VerificationStatus.VERIFIED,
        )

    @patch("documents.views.verify_pending_text_result")
    def test_review_verify_404_without_perm_before_mutation(self, mock_verify):
        resp = self._post_as(
            self.staff,
            reverse(
                "review-text-result-verify",
                kwargs={"result_id": self.pending_result.pk},
            ),
            data={"text": self.pending_result.text},
        )
        self.assertEqual(resp.status_code, 404)
        mock_verify.assert_not_called()

    @patch("documents.views.approve_suggestion")
    def test_transcription_approve_404_without_perm_before_mutation(self, mock_approve):
        resp = self._post_as(
            self.staff,
            reverse(
                "transcription-suggestion-approve",
                kwargs={"suggestion_id": self.suggestion.pk},
            ),
            data={"approved_text": "approved"},
        )
        self.assertEqual(resp.status_code, 404)
        mock_approve.assert_not_called()

    @patch("documents.views.reject_suggestion")
    def test_transcription_reject_404_without_perm_before_mutation(self, mock_reject):
        resp = self._post_as(
            self.staff,
            reverse(
                "transcription-suggestion-reject",
                kwargs={"suggestion_id": self.suggestion.pk},
            ),
        )
        self.assertEqual(resp.status_code, 404)
        mock_reject.assert_not_called()

    @patch("documents.views.approve_suggestion")
    def test_transcription_approve_calls_service_with_permission(self, mock_approve):
        resp = self._post_as(
            self.staff_with_perm,
            reverse(
                "transcription-suggestion-approve",
                kwargs={"suggestion_id": self.suggestion.pk},
            ),
            data={"approved_text": "approved"},
        )
        self.assertEqual(resp.status_code, 302)
        mock_approve.assert_called_once()

    @patch("documents.views.approve_archive_metadata_suggestion")
    def test_metadata_approve_404_without_perm_before_mutation(self, mock_approve):
        resp = self._post_as(
            self.staff,
            reverse(
                "archive-metadata-suggestion-approve",
                kwargs={"suggestion_id": self.metadata_suggestion.pk},
            ),
        )
        self.assertEqual(resp.status_code, 404)
        mock_approve.assert_not_called()

    @patch("documents.views.reject_archive_metadata_suggestion")
    def test_metadata_reject_404_without_perm_before_mutation(self, mock_reject):
        resp = self._post_as(
            self.staff,
            reverse(
                "archive-metadata-suggestion-reject",
                kwargs={"suggestion_id": self.metadata_suggestion.pk},
            ),
        )
        self.assertEqual(resp.status_code, 404)
        mock_reject.assert_not_called()

    @patch("documents.views.approve_archive_metadata_suggestion")
    def test_metadata_approve_calls_service_with_permission(self, mock_approve):
        resp = self._post_as(
            self.staff_with_perm,
            reverse(
                "archive-metadata-suggestion-approve",
                kwargs={"suggestion_id": self.metadata_suggestion.pk},
            ),
        )
        self.assertEqual(resp.status_code, 302)
        mock_approve.assert_called_once()

    @patch("documents.views.approve_archive_item_person_suggestion")
    def test_person_suggestion_approve_404_without_perm_before_mutation(
        self, mock_approve
    ):
        resp = self._post_as(
            self.staff,
            reverse(
                "archive-item-person-suggestion-approve",
                kwargs={"suggestion_id": self.person_suggestion.pk},
            ),
        )
        self.assertEqual(resp.status_code, 404)
        mock_approve.assert_not_called()

    @patch("documents.views.reject_archive_item_person_suggestion")
    def test_person_suggestion_reject_404_without_perm_before_mutation(
        self, mock_reject
    ):
        resp = self._post_as(
            self.staff,
            reverse(
                "archive-item-person-suggestion-reject",
                kwargs={"suggestion_id": self.person_suggestion.pk},
            ),
        )
        self.assertEqual(resp.status_code, 404)
        mock_reject.assert_not_called()

    @patch("documents.views.approve_archive_item_person_suggestion")
    def test_person_suggestion_approve_calls_service_with_permission(
        self, mock_approve
    ):
        resp = self._post_as(
            self.staff_with_perm,
            reverse(
                "archive-item-person-suggestion-approve",
                kwargs={"suggestion_id": self.person_suggestion.pk},
            ),
        )
        self.assertEqual(resp.status_code, 302)
        mock_approve.assert_called_once()

    @patch("documents.views.activate_corrected_current_sync_attempt")
    def test_corrected_current_activate_404_without_perm_before_service(
        self, mock_activate
    ):
        resp = self._post_as(
            self.staff,
            reverse(
                "corrected-current-sync-attempt-activate",
                kwargs={
                    "doc_id": self.restricted_ocr.pk,
                    "attempt_id": self.sync_attempt.pk,
                },
            ),
            data={
                "confirm_replace": "1",
                "source_text_result_id": str(self.verified_result.pk),
                "expected_source_revision": "1",
                "expected_source_sha256": "a" * 64,
            },
        )
        self.assertEqual(resp.status_code, 404)
        mock_activate.assert_not_called()

    @patch("documents.views.activate_corrected_current_sync_attempt")
    def test_corrected_current_activate_reaches_service_with_permission(
        self, mock_activate
    ):
        from documents.services.transkribus_corrected_current_activation import (
            CorrectedCurrentActivationResult,
        )

        mock_activate.return_value = CorrectedCurrentActivationResult(
            attempt_id=self.sync_attempt.pk,
            snapshot_id=1,
            source_result_id=self.verified_result.pk,
            hebrew_result_id=None,
            engine="transkribus",
            bound_source_revision=1,
            outcome="APPLIED",
            source_text_changed=True,
            hebrew_mirror_updated=False,
        )
        resp = self._post_as(
            self.staff_with_perm,
            reverse(
                "corrected-current-sync-attempt-activate",
                kwargs={
                    "doc_id": self.restricted_ocr.pk,
                    "attempt_id": self.sync_attempt.pk,
                },
            ),
            data={
                "confirm_replace": "1",
                "source_text_result_id": str(self.verified_result.pk),
                "expected_source_revision": "1",
                "expected_source_sha256": "a" * 64,
            },
        )
        self.assertEqual(resp.status_code, 302)
        mock_activate.assert_called_once()

    @patch("documents.views.enqueue_transkribus_corrected_current_sync")
    @patch("documents.views._is_transkribus_corrected_current_sync_ui_eligible")
    def test_corrected_current_enqueue_404_without_perm_before_service(
        self, mock_eligible, mock_enqueue
    ):
        resp = self._post_as(
            self.staff,
            reverse(
                "corrected-current-sync-enqueue",
                kwargs={"doc_id": self.restricted_ocr.pk},
            ),
        )
        self.assertEqual(resp.status_code, 404)
        mock_eligible.assert_not_called()
        mock_enqueue.assert_not_called()

    @patch("documents.views.enqueue_transkribus_corrected_current_sync")
    @patch(
        "documents.views._is_transkribus_corrected_current_sync_ui_eligible",
        return_value=True,
    )
    def test_corrected_current_enqueue_reaches_service_with_permission(
        self, mock_eligible, mock_enqueue
    ):
        mock_enqueue.return_value = SimpleNamespace(
            outcome="CREATED_AND_ENQUEUED",
            request=SimpleNamespace(pk=55),
            message_sent=True,
            observed_status="QUEUED",
        )
        resp = self._post_as(
            self.staff_with_perm,
            reverse(
                "corrected-current-sync-enqueue",
                kwargs={"doc_id": self.restricted_ocr.pk},
            ),
        )
        self.assertEqual(resp.status_code, 302)
        mock_eligible.assert_called_once()
        mock_enqueue.assert_called_once_with(
            document_id=self.restricted_ocr.pk,
            initiated_by=self.staff_with_perm,
        )

    @patch("documents.views.save_paragraph_editor_mapping")
    def test_paragraph_editor_post_404_without_perm_before_service(self, mock_save):
        resp = self._post_as(
            self.staff,
            reverse(
                "transkribus-paragraphs",
                kwargs={"doc_id": self.restricted_ocr.pk},
            ),
            data={
                "expected_document_id": str(self.restricted_ocr.pk),
                "expected_text_result_id": "1",
                "expected_snapshot_id": "1",
            },
        )
        self.assertEqual(resp.status_code, 404)
        mock_save.assert_not_called()

    @patch("documents.views.adopt_paragraph_editor_mapping")
    def test_paragraph_adopt_post_404_without_perm_before_service(self, mock_adopt):
        resp = self._post_as(
            self.staff,
            reverse(
                "transkribus-paragraphs-adopt",
                kwargs={"doc_id": self.restricted_ocr.pk},
            ),
            data={
                "expected_document_id": str(self.restricted_ocr.pk),
                "expected_text_result_id": "1",
                "expected_snapshot_id": "1",
                "expected_source_mapping_id": "1",
                "expected_source_snapshot_id": "1",
            },
        )
        self.assertEqual(resp.status_code, 404)
        mock_adopt.assert_not_called()

    @patch("documents.views.validate_required_env")
    @patch("documents.views.apply_ocr_reprocess")
    def test_ocr_reprocess_404_without_perm_before_env_or_apply(
        self, mock_apply, mock_validate
    ):
        resp = self._post_as(
            self.staff,
            reverse(
                "documents-ocr-reprocess",
                kwargs={"doc_id": self.restricted_ocr.pk},
            ),
        )
        self.assertEqual(resp.status_code, 404)
        mock_validate.assert_not_called()
        mock_apply.assert_not_called()

    @patch("documents.views.validate_required_env")
    @patch("documents.views.apply_ocr_reprocess")
    def test_ocr_reprocess_calls_apply_with_permission(self, mock_apply, mock_validate):
        from documents.services.ocr_reprocess import (
            OcrReprocessAssessment,
            OcrRetryMode,
        )

        mock_validate.return_value = SimpleNamespace(
            transkribus_collection_id="col",
            transkribus_model_id="42",
        )
        mock_apply.return_value = SimpleNamespace(
            assessment=OcrReprocessAssessment(
                document_id=self.restricted_ocr.pk,
                retry_mode=OcrRetryMode.NORMAL_REENQUEUE,
            ),
            enqueue_result=SimpleNamespace(
                outcome="CREATED_AND_ENQUEUED",
                request=SimpleNamespace(pk=99),
            ),
        )
        resp = self._post_as(
            self.staff_with_perm,
            reverse(
                "documents-ocr-reprocess",
                kwargs={"doc_id": self.restricted_ocr.pk},
            ),
        )
        self.assertEqual(resp.status_code, 302)
        mock_apply.assert_called_once()

    @patch("documents.views.enqueue_hebrew_translation_retry")
    def test_hebrew_translation_retry_404_without_perm_before_enqueue(
        self, mock_enqueue
    ):
        resp = self._post_as(
            self.staff,
            reverse(
                "documents-hebrew-translation-retry",
                kwargs={"doc_id": self.restricted_ocr.pk},
            ),
        )
        self.assertEqual(resp.status_code, 404)
        mock_enqueue.assert_not_called()

    @patch("documents.views.enqueue_hebrew_translation_retry")
    def test_hebrew_translation_retry_calls_enqueue_with_permission(self, mock_enqueue):
        mock_enqueue.return_value = SimpleNamespace(
            outcome="CREATED_AND_ENQUEUED",
            request=SimpleNamespace(pk=77),
        )
        resp = self._post_as(
            self.staff_with_perm,
            reverse(
                "documents-hebrew-translation-retry",
                kwargs={"doc_id": self.restricted_ocr.pk},
            ),
        )
        self.assertEqual(resp.status_code, 302)
        mock_enqueue.assert_called_once_with(
            self.restricted_ocr.pk,
            initiated_by=self.staff_with_perm,
        )

    @patch("documents.views.edit_pending_text_result")
    def test_review_update_text_reaches_mutation_with_permission(self, mock_edit):
        from documents.services.verified_text_result_edit import (
            PendingTextResultEditResult,
        )

        mock_edit.return_value = PendingTextResultEditResult(
            row=self.pending_result,
            text_saved=True,
        )
        resp = self._post_as(
            self.staff_with_perm,
            reverse(
                "review-text-result-update-text",
                kwargs={"result_id": self.pending_result.pk},
            ),
            data={"text": "updated pending text"},
        )
        self.assertEqual(resp.status_code, 302)
        mock_edit.assert_called_once()
