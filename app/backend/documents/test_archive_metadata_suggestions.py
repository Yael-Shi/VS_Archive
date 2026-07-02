from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.test import TestCase, override_settings
from django.urls import reverse

from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    ArchiveMetadataSuggestion,
    Document,
    DocumentTextResult,
    Tag,
)
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_items import create_manual_text_archive_item, create_ocr_document
from documents.services.archive_metadata_suggestion_review import (
    ArchiveMetadataSuggestionReviewError,
    approve_suggestion,
    parse_suggested_metadata_values,
    reject_suggestion,
)
from public.services.registration import HONEYPOT_FIELD_NAME


@override_settings(UPLOADS_BUCKET_NAME="")
class ArchiveMetadataSuggestionPublicFlowTests(TestCase):
    def setUp(self):
        Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)

    def _create_family_user(self, username: str = "family_metadata_user") -> User:
        user = User.objects.create_user(username=username, password="test-pass")
        user.groups.add(Group.objects.get(name=ARCHIVE_FAMILY_GROUP_NAME))
        return user

    def _create_public_manual_text_item(self, *, title: str = "Public manual item") -> ArchiveItem:
        return create_manual_text_archive_item(
            title=title,
            body="גוף הטקסט",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

    def _form_url(self, item_id: int) -> str:
        return reverse("archive-metadata-suggestion-new", kwargs={"item_id": item_id})

    def _thanks_url(self, item_id: int) -> str:
        return reverse("archive-metadata-suggestion-thanks", kwargs={"item_id": item_id})

    def _detail_url(self, item_id: int) -> str:
        return reverse("archive-detail", kwargs={"item_id": item_id})

    def _valid_post_data(self, **overrides) -> dict[str, str]:
        data = {
            "submitter_name": "מציע/ה",
            "submitter_email": "suggester@example.com",
            "submitter_note": "הערה למנהלים",
            "suggested_categories": "קטגוריה חדשה",
            "suggested_events": "",
            "suggested_tags": "תגית חדשה",
            HONEYPOT_FIELD_NAME: "",
        }
        data.update(overrides)
        return data

    def test_anonymous_can_get_form_for_public_item(self):
        item = self._create_public_manual_text_item()
        resp = self.client.get(self._form_url(item.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "הצעת שיוך לפריט")
        self.assertContains(resp, "ההצעה תיבדק לפני שינוי באתר")

    def test_anonymous_cannot_access_private_item(self):
        item = self._create_public_manual_text_item()
        item.visibility = ArchiveItem.Visibility.PRIVATE
        item.save(update_fields=["visibility"])

        self.assertEqual(self.client.get(self._form_url(item.id)).status_code, 404)
        self.assertEqual(
            self.client.post(
                self._form_url(item.id), self._valid_post_data()
            ).status_code,
            404,
        )

    def test_family_viewer_can_access_private_item(self):
        item = self._create_public_manual_text_item()
        item.visibility = ArchiveItem.Visibility.PRIVATE
        item.save(update_fields=["visibility"])

        self.client.force_login(self._create_family_user())
        resp = self.client.get(self._form_url(item.id))
        self.assertEqual(resp.status_code, 200)

    def test_post_creates_suggestion_without_mutating_item_metadata(self):
        item = self._create_public_manual_text_item()
        category = ArchiveCategory.objects.create(name="קיים", slug="existing-cat")
        event = ArchiveEvent.objects.create(name="אירוע קיים", slug="existing-event")
        tag = Tag.objects.create(name="תגית-קיימת")
        item.categories.add(category)
        item.events.add(event)
        item.tags.add(tag)

        resp = self.client.post(self._form_url(item.id), self._valid_post_data())
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self._thanks_url(item.id))

        suggestion = ArchiveMetadataSuggestion.objects.get()
        self.assertEqual(suggestion.archive_item_id, item.id)
        self.assertEqual(suggestion.suggested_categories, "קטגוריה חדשה")
        self.assertEqual(suggestion.suggested_tags, "תגית חדשה")
        self.assertEqual(suggestion.submitter_name, "מציע/ה")
        self.assertEqual(suggestion.submitter_email, "suggester@example.com")
        self.assertEqual(suggestion.submitter_note, "הערה למנהלים")
        self.assertEqual(
            suggestion.status,
            ArchiveMetadataSuggestion.Status.PENDING,
        )
        self.assertIsNone(suggestion.submitter_user)

        item.refresh_from_db()
        self.assertEqual(list(item.categories.values_list("name", flat=True)), ["קיים"])
        self.assertEqual(list(item.events.values_list("name", flat=True)), ["אירוע קיים"])
        self.assertEqual(list(item.tags.values_list("name", flat=True)), ["תגית-קיימת"])

    def test_name_required(self):
        item = self._create_public_manual_text_item()
        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(submitter_name=""),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "יש למלא שם.")
        self.assertEqual(ArchiveMetadataSuggestion.objects.count(), 0)

    def test_at_least_one_metadata_field_required(self):
        item = self._create_public_manual_text_item()
        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(
                suggested_categories="",
                suggested_events="   ",
                suggested_tags="",
            ),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "יש להזין לפחות קטגוריה, אירוע או תגית מוצעים.")
        self.assertEqual(ArchiveMetadataSuggestion.objects.count(), 0)

    def test_honeypot_does_not_create_suggestion(self):
        item = self._create_public_manual_text_item()
        resp = self.client.post(
            self._form_url(item.id),
            self._valid_post_data(**{HONEYPOT_FIELD_NAME: "bot corp"}),
        )
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self._thanks_url(item.id))
        self.assertEqual(ArchiveMetadataSuggestion.objects.count(), 0)

    def test_thanks_page_shows_success_message(self):
        item = self._create_public_manual_text_item()
        resp = self.client.get(self._thanks_url(item.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "תודה, ההצעה נשלחה לבדיקה.")

    def test_detail_button_shown_on_archive_item_page(self):
        item = self._create_public_manual_text_item()
        resp = self.client.get(self._detail_url(item.id))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "הציעו קטגוריה, אירוע או תגית")
        self.assertContains(resp, self._form_url(item.id))

    def test_detail_button_shown_on_ocr_document_page(self):
        doc = create_ocr_document(
            title="Public OCR metadata suggestion",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            language=Document.Language.HEBREW,
            visibility=Document.Visibility.PUBLIC,
            upload_status=Document.UploadStatus.UPLOADED,
            processing_state_user=Document.ProcessingState.READY,
            file_s3_key="documents/50/original.jpg",
            mime_type="image/jpeg",
        )
        DocumentTextResult.objects.create(
            document=doc,
            result_type=DocumentTextResult.ResultType.HEBREW_TEXT,
            engine="engine-a",
            engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
            prompt_variant=DocumentTextResult.OcrPromptVariant.HANDWRITTEN,
            status=DocumentTextResult.Status.NEEDS_REVIEW,
            text="טקסט",
        )
        item_id = doc.archive_item_id
        assert item_id is not None

        resp = self.client.get(
            reverse("documents-detail-page", kwargs={"doc_id": doc.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "הציעו קטגוריה, אירוע או תגית")
        self.assertContains(resp, self._form_url(item_id))


@override_settings(UPLOADS_BUCKET_NAME="")
class ArchiveMetadataSuggestionStaffUiTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="metadata_suggestion_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="metadata_suggestion_viewer",
            password="test-pass",
            is_staff=False,
        )

    def _create_item(self, *, title: str) -> ArchiveItem:
        return create_manual_text_archive_item(
            title=title,
            body="גוף",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

    def _create_suggestion(
        self,
        item: ArchiveItem,
        *,
        submitter_name: str = "מציע/ה",
        submitter_email: str = "suggester@example.com",
        submitter_note: str = "הערה",
        suggested_categories: str = "קטגוריה",
        suggested_events: str = "אירוע",
        suggested_tags: str = "תגית",
    ) -> ArchiveMetadataSuggestion:
        return ArchiveMetadataSuggestion.objects.create(
            archive_item=item,
            submitter_name=submitter_name,
            submitter_email=submitter_email,
            submitter_note=submitter_note,
            suggested_categories=suggested_categories,
            suggested_events=suggested_events,
            suggested_tags=suggested_tags,
        )

    def _backlog_url(self) -> str:
        return reverse("archive-metadata-suggestion-backlog")

    def test_non_staff_cannot_access_staff_backlog(self):
        self.client.force_login(self.viewer)
        resp = self.client.get(self._backlog_url())
        self.assertEqual(resp.status_code, 403)

    def test_staff_can_access_backlog(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self._backlog_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "הצעות שיוך לפריטי ארכיון")

    def test_backlog_lists_pending_suggestions(self):
        item_a = self._create_item(title="Item A")
        item_b = self._create_item(title="Item B")
        self._create_suggestion(item_a, submitter_name="ראשון")
        self._create_suggestion(item_b, submitter_name="שני")

        self.client.force_login(self.staff)
        resp = self.client.get(self._backlog_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Item A")
        self.assertContains(resp, "Item B")
        self.assertContains(resp, "ראשון")
        self.assertContains(resp, "שני")
        self.assertContains(resp, "קטגוריה")
        self.assertContains(resp, "אירוע")
        self.assertContains(resp, "תגית")
        self.assertContains(resp, "הערה")

    def test_staff_nav_contains_backlog_link(self):
        self.client.force_login(self.staff)
        resp = self.client.get(reverse("archive-list"))
        self.assertContains(resp, "הצעות שיוך לפריטי ארכיון")
        self.assertContains(resp, self._backlog_url())


@override_settings(UPLOADS_BUCKET_NAME="")
class ArchiveMetadataSuggestionReviewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="metadata_review_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="metadata_review_viewer",
            password="test-pass",
            is_staff=False,
        )

    def _create_item(self, *, title: str = "Review item") -> ArchiveItem:
        return create_manual_text_archive_item(
            title=title,
            body="גוף",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )

    def _create_suggestion(
        self,
        item: ArchiveItem,
        *,
        suggested_categories: str = "",
        suggested_events: str = "",
        suggested_tags: str = "",
        status: str = ArchiveMetadataSuggestion.Status.PENDING,
    ) -> ArchiveMetadataSuggestion:
        return ArchiveMetadataSuggestion.objects.create(
            archive_item=item,
            submitter_name="מציע/ה",
            suggested_categories=suggested_categories,
            suggested_events=suggested_events,
            suggested_tags=suggested_tags,
            status=status,
        )

    def _backlog_url(self) -> str:
        return reverse("archive-metadata-suggestion-backlog")

    def _approve_url(self, suggestion_id: int) -> str:
        return reverse(
            "archive-metadata-suggestion-approve",
            kwargs={"suggestion_id": suggestion_id},
        )

    def _reject_url(self, suggestion_id: int) -> str:
        return reverse(
            "archive-metadata-suggestion-reject",
            kwargs={"suggestion_id": suggestion_id},
        )

    def test_parse_suggested_metadata_values_splits_trims_and_deduplicates(self):
        self.assertEqual(
            parse_suggested_metadata_values("  א , ב\nא ,  \nב"),
            ["א", "ב"],
        )

    def test_staff_can_approve_pending_suggestion(self):
        item = self._create_item()
        suggestion = self._create_suggestion(
            item,
            suggested_categories="קטגוריה חדשה",
            suggested_tags="תגית חדשה",
        )

        self.client.force_login(self.staff)
        resp = self.client.post(self._approve_url(suggestion.id))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self._backlog_url())

        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, ArchiveMetadataSuggestion.Status.APPROVED)
        self.assertEqual(suggestion.reviewed_by, self.staff)
        self.assertIsNotNone(suggestion.reviewed_at)

    def test_approve_creates_and_attaches_suggested_metadata(self):
        item = self._create_item()
        suggestion = self._create_suggestion(
            item,
            suggested_categories="קטגוריה חדשה",
            suggested_events="אירוע חדש",
            suggested_tags="תגית-חדשה",
        )

        approve_suggestion(suggestion.id, reviewer=self.staff)

        item.refresh_from_db()
        self.assertEqual(
            list(item.categories.order_by("name").values_list("name", flat=True)),
            ["קטגוריה חדשה"],
        )
        self.assertEqual(
            list(item.events.order_by("name").values_list("name", flat=True)),
            ["אירוע חדש"],
        )
        self.assertEqual(
            list(item.tags.order_by("name").values_list("name", flat=True)),
            ["תגית-חדשה"],
        )
        self.assertEqual(ArchiveCategory.objects.count(), 1)
        self.assertEqual(ArchiveEvent.objects.count(), 1)
        self.assertEqual(Tag.objects.count(), 1)

    def test_approve_preserves_existing_metadata(self):
        item = self._create_item()
        existing_category = ArchiveCategory.objects.create(
            name="קיים",
            slug="existing-category",
        )
        existing_event = ArchiveEvent.objects.create(
            name="אירוע קיים",
            slug="existing-event",
        )
        existing_tag = Tag.objects.create(name="תגית-קיימת")
        item.categories.add(existing_category)
        item.events.add(existing_event)
        item.tags.add(existing_tag)

        suggestion = self._create_suggestion(
            item,
            suggested_categories="קטגוריה חדשה",
            suggested_events="אירוע חדש",
            suggested_tags="תגית-חדשה",
        )

        approve_suggestion(suggestion.id, reviewer=self.staff)

        item.refresh_from_db()
        self.assertEqual(
            set(item.categories.values_list("name", flat=True)),
            {"קיים", "קטגוריה חדשה"},
        )
        self.assertEqual(
            set(item.events.values_list("name", flat=True)),
            {"אירוע קיים", "אירוע חדש"},
        )
        self.assertEqual(
            set(item.tags.values_list("name", flat=True)),
            {"תגית-קיימת", "תגית-חדשה"},
        )

    def test_approve_deduplicates_repeated_suggested_values(self):
        item = self._create_item()
        suggestion = self._create_suggestion(
            item,
            suggested_categories="א, א\nא",
            suggested_events="ב, ב",
            suggested_tags="ג\nג, ג",
        )

        approve_suggestion(suggestion.id, reviewer=self.staff)

        item.refresh_from_db()
        self.assertEqual(list(item.categories.values_list("name", flat=True)), ["א"])
        self.assertEqual(list(item.events.values_list("name", flat=True)), ["ב"])
        self.assertEqual(list(item.tags.values_list("name", flat=True)), ["ג"])
        self.assertEqual(ArchiveCategory.objects.count(), 1)
        self.assertEqual(ArchiveEvent.objects.count(), 1)
        self.assertEqual(Tag.objects.count(), 1)

    def test_approve_does_not_create_blank_metadata_records(self):
        item = self._create_item()
        suggestion = self._create_suggestion(
            item,
            suggested_categories=", , \n  ",
            suggested_events="   ",
            suggested_tags="\n,",
        )

        approve_suggestion(suggestion.id, reviewer=self.staff)

        item.refresh_from_db()
        self.assertEqual(item.categories.count(), 0)
        self.assertEqual(item.events.count(), 0)
        self.assertEqual(item.tags.count(), 0)
        self.assertEqual(ArchiveCategory.objects.count(), 0)
        self.assertEqual(ArchiveEvent.objects.count(), 0)
        self.assertEqual(Tag.objects.count(), 0)

    def test_approve_reuses_existing_records_by_exact_name(self):
        item = self._create_item()
        category = ArchiveCategory.objects.create(name="קטגוריה", slug="category")
        event = ArchiveEvent.objects.create(name="אירוע", slug="event")
        tag = Tag.objects.create(name="תגית")

        suggestion = self._create_suggestion(
            item,
            suggested_categories="קטגוריה",
            suggested_events="אירוע",
            suggested_tags="תגית",
        )

        approve_suggestion(suggestion.id, reviewer=self.staff)

        self.assertEqual(ArchiveCategory.objects.count(), 1)
        self.assertEqual(ArchiveEvent.objects.count(), 1)
        self.assertEqual(Tag.objects.count(), 1)
        item.refresh_from_db()
        self.assertEqual(item.categories.get(), category)
        self.assertEqual(item.events.get(), event)
        self.assertEqual(item.tags.get(), tag)

    def test_staff_can_reject_pending_suggestion(self):
        item = self._create_item()
        suggestion = self._create_suggestion(
            item,
            suggested_categories="קטגוריה",
            suggested_tags="תגית",
        )

        self.client.force_login(self.staff)
        resp = self.client.post(self._reject_url(suggestion.id))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self._backlog_url())

        suggestion.refresh_from_db()
        self.assertEqual(suggestion.status, ArchiveMetadataSuggestion.Status.REJECTED)
        self.assertEqual(suggestion.reviewed_by, self.staff)
        self.assertIsNotNone(suggestion.reviewed_at)

    def test_reject_does_not_mutate_archive_item_metadata(self):
        item = self._create_item()
        category = ArchiveCategory.objects.create(name="קיים", slug="existing")
        item.categories.add(category)
        suggestion = self._create_suggestion(
            item,
            suggested_categories="קטגוריה חדשה",
            suggested_tags="תגית חדשה",
        )

        reject_suggestion(suggestion.id, reviewer=self.staff)

        item.refresh_from_db()
        self.assertEqual(list(item.categories.values_list("name", flat=True)), ["קיים"])
        self.assertEqual(item.events.count(), 0)
        self.assertEqual(item.tags.count(), 0)
        self.assertEqual(ArchiveCategory.objects.count(), 1)

    def test_non_staff_cannot_approve_or_reject(self):
        item = self._create_item()
        suggestion = self._create_suggestion(
            item,
            suggested_categories="קטגוריה",
        )

        self.client.force_login(self.viewer)
        self.assertEqual(
            self.client.post(self._approve_url(suggestion.id)).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(self._reject_url(suggestion.id)).status_code,
            403,
        )

    def test_backlog_shows_actions_only_for_pending_suggestions(self):
        item = self._create_item()
        pending = self._create_suggestion(
            item,
            suggested_categories="ממתין",
            suggested_tags="ממתין",
        )
        approved = self._create_suggestion(
            item,
            suggested_categories="אושר",
            status=ArchiveMetadataSuggestion.Status.APPROVED,
        )
        rejected = self._create_suggestion(
            item,
            suggested_categories="נדחה",
            status=ArchiveMetadataSuggestion.Status.REJECTED,
        )

        self.client.force_login(self.staff)
        resp = self.client.get(self._backlog_url())
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "אישור", count=1)
        self.assertContains(resp, "דחייה", count=1)
        self.assertContains(
            resp,
            reverse(
                "archive-metadata-suggestion-approve",
                kwargs={"suggestion_id": pending.id},
            ),
        )
        self.assertNotContains(
            resp,
            reverse(
                "archive-metadata-suggestion-approve",
                kwargs={"suggestion_id": approved.id},
            ),
        )
        self.assertNotContains(
            resp,
            reverse(
                "archive-metadata-suggestion-approve",
                kwargs={"suggestion_id": rejected.id},
            ),
        )

    def test_already_reviewed_suggestion_cannot_be_approved_or_rejected_again(self):
        item = self._create_item()
        suggestion = self._create_suggestion(
            item,
            suggested_categories="קטגוריה",
            status=ArchiveMetadataSuggestion.Status.APPROVED,
        )

        with self.assertRaises(ArchiveMetadataSuggestionReviewError):
            approve_suggestion(suggestion.id, reviewer=self.staff)

        suggestion.status = ArchiveMetadataSuggestion.Status.REJECTED
        suggestion.save(update_fields=["status"])

        with self.assertRaises(ArchiveMetadataSuggestionReviewError):
            reject_suggestion(suggestion.id, reviewer=self.staff)
