"""Django Admin hardening for restricted ArchiveItem visibility."""

from __future__ import annotations

from django.contrib.admin.sites import AdminSite
from django.contrib.auth import get_user_model
from django.contrib.auth.models import Permission
from django.contrib.contenttypes.models import ContentType
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from documents.admin import (
    ArchiveItemAdmin,
    CorrectionRequestAdmin,
    DocumentAdmin,
    DocumentTextResultAdmin,
    ManualTextContentAdmin,
    PhotoContentAdmin,
)
from documents.models import (
    ArchiveItem,
    CorrectionRequest,
    Document,
    DocumentTextResult,
    ManualTextContent,
    PhotoContent,
    TranskribusRun,
)
from documents.services.archive_item_access import VIEW_RESTRICTED_ARCHIVEITEM_CODENAME
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
)

User = get_user_model()

RESTRICTED_MANUAL_TITLE = "ADMIN-RESTRICTED-MANUAL-TITLE"
RESTRICTED_OCR_TITLE = "ADMIN-RESTRICTED-OCR-TITLE"
RESTRICTED_PHOTO_TITLE = "ADMIN-RESTRICTED-PHOTO-TITLE"
RESTRICTED_BODY = "admin-restricted-manual-body-secret"
RESTRICTED_OCR_TEXT = "admin-restricted-ocr-transcription-secret"
RESTRICTED_PHOTO_FILENAME = "admin-restricted-photo-secret.jpg"
RESTRICTED_PHOTO_KEY = "photos/admin-restricted/original-secret.jpg"
RESTRICTED_DOC_KEY = "documents/admin-restricted/original-secret.jpg"
PUBLIC_TITLE = "Admin public visible title"
PRIVATE_TITLE = "Admin private family title"
UNKNOWN_TITLE = "Admin unknown visibility title"


def _grant_restricted_permission(user):
    ct = ContentType.objects.get_for_model(ArchiveItem)
    perm = Permission.objects.get(
        codename=VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
        content_type=ct,
    )
    user.user_permissions.add(perm)
    user = User.objects.get(pk=user.pk)
    return user


def _grant_documents_model_permissions(user, *, include_restricted: bool):
    perms = Permission.objects.filter(content_type__app_label="documents")
    if not include_restricted:
        perms = perms.exclude(codename=VIEW_RESTRICTED_ARCHIVEITEM_CODENAME)
    user.user_permissions.set(perms)
    return User.objects.get(pk=user.pk)


def _create_photo(*, title: str, visibility: str) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=visibility,
    )
    PhotoContent.objects.create(
        archive_item=item,
        original_file_key=RESTRICTED_PHOTO_KEY
        if visibility == ArchiveItem.Visibility.RESTRICTED
        else "photos/other/original.jpg",
        original_filename=RESTRICTED_PHOTO_FILENAME
        if visibility == ArchiveItem.Visibility.RESTRICTED
        else "other.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=2048,
        upload_status=PhotoContent.UploadStatus.UPLOADED,
        thumbnail_file_key="photos/other/thumb.jpg",
        thumbnail_mime_type="image/jpeg",
        thumbnail_size_bytes=128,
    )
    return item


def _create_ocr(*, title: str, visibility: str, with_text: bool = False) -> Document:
    doc = create_ocr_document(
        title=title,
        doc_type=Document.DocType.IMAGE,
        text_input_type=Document.TextInputType.HANDWRITTEN,
        language=Document.Language.HEBREW,
        visibility=visibility,
        upload_status=Document.UploadStatus.UPLOADED,
        processing_state_user=Document.ProcessingState.READY,
        file_s3_key=RESTRICTED_DOC_KEY
        if visibility == ArchiveItem.Visibility.RESTRICTED
        else "documents/other/original.jpg",
        mime_type="image/jpeg",
        metadata_status=ArchiveItem.MetadataStatus.NEEDS_COMPLETION,
    )
    if with_text:
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="engine-admin-restricted",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            verification_status=DocumentTextResult.VerificationStatus.UNVERIFIED,
            text=RESTRICTED_OCR_TEXT,
        )
    return doc


@override_settings(UPLOADS_BUCKET_NAME="")
class RestrictedVisibilityAdminTests(TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        self.site = AdminSite()

        self.staff = User.objects.create_user(
            username="admin_staff_no_restricted",
            password="x",
            is_staff=True,
        )
        self.staff = _grant_documents_model_permissions(
            self.staff, include_restricted=False
        )

        self.staff_with_perm = User.objects.create_user(
            username="admin_staff_with_restricted",
            password="x",
            is_staff=True,
        )
        self.staff_with_perm = _grant_documents_model_permissions(
            self.staff_with_perm, include_restricted=True
        )

        self.superuser = User.objects.create_superuser(
            username="admin_super_restricted",
            password="x",
            email="admin-super@example.com",
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
        self.public_ocr = _create_ocr(
            title="Admin public OCR title",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        self.private_ocr = _create_ocr(
            title="Admin private OCR title",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        self.restricted_ocr = _create_ocr(
            title=RESTRICTED_OCR_TITLE,
            visibility=ArchiveItem.Visibility.RESTRICTED,
            with_text=True,
        )
        self.restricted_photo = _create_photo(
            title=RESTRICTED_PHOTO_TITLE,
            visibility=ArchiveItem.Visibility.RESTRICTED,
        )
        self.public_photo = _create_photo(
            title="Admin public photo title",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

        self.unknown_item = create_manual_text_archive_item(
            title=UNKNOWN_TITLE,
            body="unknown body",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        ArchiveItem.objects.filter(pk=self.unknown_item.pk).update(visibility="invalid")
        self.unknown_item.refresh_from_db()

        self.restricted_text_result = DocumentTextResult.objects.get(
            document=self.restricted_ocr,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
        )
        self.restricted_run = TranskribusRun.objects.create(
            document=self.restricted_ocr,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col-admin",
            model_id="42",
            remote_doc_id="admin-remote-777",
            pages_query="1",
            recognition_job_id="admin-job-1",
            page_index_to_page_nr={1: 1},
        )
        self.public_run = TranskribusRun.objects.create(
            document=self.public_ocr,
            status=TranskribusRun.Status.SUCCEEDED,
            mode=TranskribusRun.Mode.UPLOAD_CREATED,
            collection_id="col-public",
            model_id="42",
            remote_doc_id="admin-remote-public",
            pages_query="1",
            recognition_job_id="admin-job-public",
            page_index_to_page_nr={1: 1},
        )
        self.restricted_correction = CorrectionRequest.objects.create(
            document=self.restricted_ocr,
            status=CorrectionRequest.Status.OPEN,
            scope=CorrectionRequest.Scope.DATA,
            message="restricted correction secret message",
        )
        self.public_correction = CorrectionRequest.objects.create(
            document=self.public_ocr,
            status=CorrectionRequest.Status.OPEN,
            scope=CorrectionRequest.Scope.DATA,
            message="public correction message",
        )

    def _login(self, user):
        self.client.force_login(user)

    def _assert_hidden_secrets(self, response, *, allow_echo: str | None = None):
        """Assert restricted secrets are absent from response body.

        Admin search pages echo the ``q`` query into the search input. When the
        search term itself is a secret marker, pass it as ``allow_echo``.
        """
        content = response.content.decode(response.charset or "utf-8")
        if allow_echo:
            content = content.replace(allow_echo, "")
        secrets = (
            RESTRICTED_MANUAL_TITLE,
            RESTRICTED_OCR_TITLE,
            RESTRICTED_PHOTO_TITLE,
            RESTRICTED_BODY,
            RESTRICTED_OCR_TEXT,
            RESTRICTED_PHOTO_FILENAME,
            RESTRICTED_PHOTO_KEY,
            RESTRICTED_DOC_KEY,
            UNKNOWN_TITLE,
            "restricted correction secret message",
            "admin-remote-777",
        )
        for secret in secrets:
            self.assertNotIn(secret, content, msg=f"Leaked secret: {secret!r}")

    def _assert_empty_admin_search(self, response, *, label_fragment: str):
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "0 results")
        self.assertContains(response, f"0 {label_fragment}")

    def _assert_authorized_can_see_restricted(self, response):
        self.assertContains(response, RESTRICTED_MANUAL_TITLE)

    def test_archiveitem_changelist_hides_restricted_from_staff_without_perm(self):
        self._login(self.staff)
        url = reverse("admin:documents_archiveitem_changelist")
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, PUBLIC_TITLE)
        self.assertContains(resp, PRIVATE_TITLE)
        self._assert_hidden_secrets(resp)
        # Result count must not include restricted/unknown rows.
        qs = ArchiveItemAdmin(ArchiveItem, self.site).get_queryset(
            self._request(self.staff)
        )
        self.assertEqual(qs.count(), 5)
        self.assertFalse(
            qs.filter(visibility=ArchiveItem.Visibility.RESTRICTED).exists()
        )
        self.assertFalse(qs.filter(title=UNKNOWN_TITLE).exists())

    def test_archiveitem_changelist_shows_restricted_to_staff_with_perm(self):
        self._login(self.staff_with_perm)
        resp = self.client.get(reverse("admin:documents_archiveitem_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, PUBLIC_TITLE)
        self.assertContains(resp, PRIVATE_TITLE)
        self._assert_authorized_can_see_restricted(resp)
        self.assertContains(resp, RESTRICTED_OCR_TITLE)
        self.assertContains(resp, RESTRICTED_PHOTO_TITLE)
        self.assertNotContains(resp, UNKNOWN_TITLE)

    def test_archiveitem_changelist_shows_restricted_to_superuser(self):
        self._login(self.superuser)
        resp = self.client.get(reverse("admin:documents_archiveitem_changelist"))
        self.assertEqual(resp.status_code, 200)
        self._assert_authorized_can_see_restricted(resp)
        self.assertNotContains(resp, UNKNOWN_TITLE)

    def test_archiveitem_search_hides_restricted_from_staff_without_perm(self):
        self._login(self.staff)
        resp = self.client.get(
            reverse("admin:documents_archiveitem_changelist"),
            {"q": "ADMIN-RESTRICTED"},
        )
        self._assert_empty_admin_search(resp, label_fragment="archive items")
        # Search bar only echoes the short query token, not full restricted titles.
        self.assertNotContains(resp, RESTRICTED_MANUAL_TITLE)
        self.assertNotContains(resp, RESTRICTED_OCR_TITLE)
        self.assertNotContains(resp, RESTRICTED_PHOTO_TITLE)
        self.assertNotContains(resp, RESTRICTED_BODY)

    def test_archiveitem_search_finds_restricted_for_authorized_staff(self):
        self._login(self.staff_with_perm)
        resp = self.client.get(
            reverse("admin:documents_archiveitem_changelist"),
            {"q": RESTRICTED_MANUAL_TITLE},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, RESTRICTED_MANUAL_TITLE)

    def test_archiveitem_direct_url_non_disclosing_for_staff_without_perm(self):
        self._login(self.staff)
        url = reverse(
            "admin:documents_archiveitem_change",
            args=[self.restricted_manual.pk],
        )
        resp = self.client.get(url)
        self.assertIn(resp.status_code, (302, 404))
        if resp.status_code == 302:
            follow = self.client.get(url, follow=True)
            self.assertNotContains(follow, RESTRICTED_MANUAL_TITLE)
            self.assertNotContains(follow, RESTRICTED_BODY)

    def test_archiveitem_direct_url_allowed_for_staff_with_perm(self):
        self._login(self.staff_with_perm)
        url = reverse(
            "admin:documents_archiveitem_change",
            args=[self.restricted_manual.pk],
        )
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, RESTRICTED_MANUAL_TITLE)

    def test_unknown_visibility_hidden_even_for_superuser(self):
        self._login(self.superuser)
        qs = ArchiveItemAdmin(ArchiveItem, self.site).get_queryset(
            self._request(self.superuser)
        )
        self.assertFalse(qs.filter(pk=self.unknown_item.pk).exists())
        url = reverse(
            "admin:documents_archiveitem_change",
            args=[self.unknown_item.pk],
        )
        resp = self.client.get(url)
        self.assertIn(resp.status_code, (302, 404))

    def test_document_admin_hides_restricted_from_staff_without_perm(self):
        self._login(self.staff)
        resp = self.client.get(reverse("admin:documents_document_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Admin public OCR title")
        self.assertContains(resp, "Admin private OCR title")
        self._assert_hidden_secrets(resp)

        search = self.client.get(
            reverse("admin:documents_document_changelist"),
            {"q": RESTRICTED_OCR_TITLE},
        )
        self._assert_empty_admin_search(search, label_fragment="documents")
        self._assert_hidden_secrets(search, allow_echo=RESTRICTED_OCR_TITLE)

        detail = self.client.get(
            reverse("admin:documents_document_change", args=[self.restricted_ocr.pk])
        )
        self.assertIn(detail.status_code, (302, 404))

    def test_document_admin_allows_restricted_for_authorized_roles(self):
        for user in (self.staff_with_perm, self.superuser):
            self._login(user)
            resp = self.client.get(reverse("admin:documents_document_changelist"))
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, RESTRICTED_OCR_TITLE)
            detail = self.client.get(
                reverse(
                    "admin:documents_document_change",
                    args=[self.restricted_ocr.pk],
                )
            )
            self.assertEqual(detail.status_code, 200)
            self.assertContains(detail, RESTRICTED_OCR_TITLE)
            self.assertContains(detail, RESTRICTED_DOC_KEY)

    def test_manual_text_admin_hides_restricted_body(self):
        self._login(self.staff)
        resp = self.client.get(reverse("admin:documents_manualtextcontent_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, PUBLIC_TITLE)
        self.assertContains(resp, PRIVATE_TITLE)
        self._assert_hidden_secrets(resp)

        search = self.client.get(
            reverse("admin:documents_manualtextcontent_changelist"),
            {"q": RESTRICTED_BODY},
        )
        self._assert_empty_admin_search(search, label_fragment="manual text contents")
        self._assert_hidden_secrets(search, allow_echo=RESTRICTED_BODY)

        content = ManualTextContent.objects.get(archive_item=self.restricted_manual)
        detail = self.client.get(
            reverse("admin:documents_manualtextcontent_change", args=[content.pk])
        )
        self.assertIn(detail.status_code, (302, 404))

    def test_manual_text_admin_allows_restricted_for_authorized_staff(self):
        self._login(self.staff_with_perm)
        content = ManualTextContent.objects.get(archive_item=self.restricted_manual)
        resp = self.client.get(
            reverse("admin:documents_manualtextcontent_change", args=[content.pk])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, RESTRICTED_MANUAL_TITLE)
        self.assertContains(resp, RESTRICTED_BODY)

    def test_photo_admin_hides_restricted_filename_and_key(self):
        self._login(self.staff)
        resp = self.client.get(reverse("admin:documents_photocontent_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Admin public photo title")
        self._assert_hidden_secrets(resp)

        search = self.client.get(
            reverse("admin:documents_photocontent_changelist"),
            {"q": RESTRICTED_PHOTO_FILENAME},
        )
        self._assert_empty_admin_search(search, label_fragment="photo contents")
        self._assert_hidden_secrets(search, allow_echo=RESTRICTED_PHOTO_FILENAME)

        content = PhotoContent.objects.get(archive_item=self.restricted_photo)
        detail = self.client.get(
            reverse("admin:documents_photocontent_change", args=[content.pk])
        )
        self.assertIn(detail.status_code, (302, 404))

    def test_photo_admin_allows_restricted_for_superuser(self):
        self._login(self.superuser)
        content = PhotoContent.objects.get(archive_item=self.restricted_photo)
        resp = self.client.get(
            reverse("admin:documents_photocontent_change", args=[content.pk])
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, RESTRICTED_PHOTO_TITLE)
        self.assertContains(resp, RESTRICTED_PHOTO_FILENAME)
        self.assertContains(resp, RESTRICTED_PHOTO_KEY)

    def test_document_text_result_admin_hides_restricted_text(self):
        self._login(self.staff)
        resp = self.client.get(reverse("admin:documents_documenttextresult_changelist"))
        self.assertEqual(resp.status_code, 200)
        self._assert_hidden_secrets(resp)

        search = self.client.get(
            reverse("admin:documents_documenttextresult_changelist"),
            {"q": RESTRICTED_OCR_TITLE},
        )
        self.assertEqual(search.status_code, 200)
        self.assertContains(search, "0 document text results")
        self._assert_hidden_secrets(search, allow_echo=RESTRICTED_OCR_TITLE)

        detail = self.client.get(
            reverse(
                "admin:documents_documenttextresult_change",
                args=[self.restricted_text_result.pk],
            )
        )
        self.assertIn(detail.status_code, (302, 404))

    def test_document_text_result_admin_allows_restricted_for_authorized_staff(self):
        self._login(self.staff_with_perm)
        resp = self.client.get(
            reverse(
                "admin:documents_documenttextresult_change",
                args=[self.restricted_text_result.pk],
            )
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, RESTRICTED_OCR_TEXT)

    def test_transkribus_run_admin_hides_restricted_related_rows(self):
        self._login(self.staff)
        resp = self.client.get(reverse("admin:documents_transkribusrun_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "admin-remote-public")
        self._assert_hidden_secrets(resp)

        detail = self.client.get(
            reverse(
                "admin:documents_transkribusrun_change",
                args=[self.restricted_run.pk],
            )
        )
        self.assertIn(detail.status_code, (302, 404))

    def test_transkribus_run_admin_allows_restricted_for_authorized_staff(self):
        self._login(self.staff_with_perm)
        resp = self.client.get(
            reverse(
                "admin:documents_transkribusrun_change",
                args=[self.restricted_run.pk],
            )
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "admin-remote-777")

    def test_correction_request_admin_hides_restricted_rows(self):
        self._login(self.staff)
        resp = self.client.get(reverse("admin:documents_correctionrequest_changelist"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Admin public OCR title")
        self.assertContains(resp, "1 correction request")
        self._assert_hidden_secrets(resp)

        search = self.client.get(
            reverse("admin:documents_correctionrequest_changelist"),
            {"q": "restricted correction secret message"},
        )
        self.assertEqual(search.status_code, 200)
        self.assertContains(search, "0 correction requests")
        self._assert_hidden_secrets(
            search, allow_echo="restricted correction secret message"
        )

        detail = self.client.get(
            reverse(
                "admin:documents_correctionrequest_change",
                args=[self.restricted_correction.pk],
            )
        )
        self.assertIn(detail.status_code, (302, 404))

    def test_correction_request_admin_allows_restricted_for_superuser(self):
        self._login(self.superuser)
        resp = self.client.get(
            reverse(
                "admin:documents_correctionrequest_change",
                args=[self.restricted_correction.pk],
            )
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "restricted correction secret message")

    def test_fk_choice_querysets_hide_restricted_documents(self):
        request = self._request(self.staff)
        admin = CorrectionRequestAdmin(CorrectionRequest, self.site)
        field = CorrectionRequest._meta.get_field("document")
        formfield = admin.formfield_for_foreignkey(field, request)
        ids = set(formfield.queryset.values_list("pk", flat=True))
        self.assertIn(self.public_ocr.pk, ids)
        self.assertIn(self.private_ocr.pk, ids)
        self.assertNotIn(self.restricted_ocr.pk, ids)

        allowed = CorrectionRequestAdmin(CorrectionRequest, self.site)
        allowed_field = allowed.formfield_for_foreignkey(
            field, self._request(self.staff_with_perm)
        )
        allowed_ids = set(allowed_field.queryset.values_list("pk", flat=True))
        self.assertIn(self.restricted_ocr.pk, allowed_ids)

    def test_fk_choice_querysets_hide_restricted_archive_items(self):
        request = self._request(self.staff)
        admin = ManualTextContentAdmin(ManualTextContent, self.site)
        field = ManualTextContent._meta.get_field("archive_item")
        formfield = admin.formfield_for_foreignkey(field, request)
        ids = set(formfield.queryset.values_list("pk", flat=True))
        self.assertIn(self.public_manual.pk, ids)
        self.assertIn(self.private_manual.pk, ids)
        self.assertNotIn(self.restricted_manual.pk, ids)
        self.assertNotIn(self.unknown_item.pk, ids)

        super_field = DocumentAdmin(Document, self.site).formfield_for_foreignkey(
            Document._meta.get_field("archive_item"),
            self._request(self.superuser),
        )
        super_ids = set(super_field.queryset.values_list("pk", flat=True))
        self.assertIn(self.restricted_manual.pk, super_ids)
        self.assertNotIn(self.unknown_item.pk, super_ids)

    def test_get_search_results_respect_visibility_scope(self):
        admin = DocumentTextResultAdmin(DocumentTextResult, self.site)
        request = self._request(self.staff)
        qs = admin.get_queryset(request)
        qs, _ = admin.get_search_results(request, qs, RESTRICTED_OCR_TITLE)
        self.assertFalse(qs.filter(pk=self.restricted_text_result.pk).exists())

        allowed_request = self._request(self.staff_with_perm)
        allowed_qs = admin.get_queryset(allowed_request)
        allowed_qs, _ = admin.get_search_results(
            allowed_request, allowed_qs, RESTRICTED_OCR_TITLE
        )
        self.assertTrue(allowed_qs.filter(pk=self.restricted_text_result.pk).exists())

    def test_visibility_list_filter_does_not_leak_restricted_rows(self):
        self._login(self.staff)
        resp = self.client.get(
            reverse("admin:documents_archiveitem_changelist"),
            {"visibility__exact": ArchiveItem.Visibility.RESTRICTED},
        )
        self.assertEqual(resp.status_code, 200)
        self._assert_hidden_secrets(resp)

    def test_photo_and_manual_admins_preserve_public_private(self):
        for user in (self.staff, self.staff_with_perm, self.superuser):
            request = self._request(user)
            manual_qs = ManualTextContentAdmin(
                ManualTextContent, self.site
            ).get_queryset(request)
            self.assertTrue(manual_qs.filter(archive_item=self.public_manual).exists())
            self.assertTrue(manual_qs.filter(archive_item=self.private_manual).exists())
            photo_qs = PhotoContentAdmin(PhotoContent, self.site).get_queryset(request)
            self.assertTrue(photo_qs.filter(archive_item=self.public_photo).exists())

    def _request(self, user):
        request = self.factory.get("/admin/")
        request.user = user
        return request
