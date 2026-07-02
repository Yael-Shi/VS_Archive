from django.contrib.auth.models import User
from django.test import Client, TestCase
from django.urls import reverse

from public.models import PublicContentBlock
from public.services.public_content import (
    CONTACT_EMAIL,
    DEFAULT_PUBLIC_CONTENT,
)


class PublicNavTests(TestCase):
    def test_public_nav_omits_redundant_home_link(self):
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="nav-brand-link"')
        self.assertContains(resp, "ארכיון")
        self.assertContains(resp, "אודות")
        self.assertNotContains(resp, '<a class="btn btn-link" href="/">דף הבית</a>')


class AboutPageCopyTests(TestCase):
    def test_about_page_does_not_show_english_version_roadmap_copy(self):
        resp = self.client.get(reverse("public-about"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(
            resp, "כרגע האתר מוצג בעברית. בעתיד תתווסף גם גרסה באנגלית."
        )


class PublicContentBlockTests(TestCase):
    def setUp(self):
        self.about_url = reverse("public-about")
        self.edit_url = reverse("public-content-edit")
        self.staff = User.objects.create_user(
            username="content_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="content_viewer",
            password="test-pass",
            is_staff=False,
        )

    def test_about_page_renders_fallback_content_when_db_blocks_missing(self):
        resp = self.client.get(self.about_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, DEFAULT_PUBLIC_CONTENT["biography"]["body"][:40])
        self.assertContains(resp, DEFAULT_PUBLIC_CONTENT["about_archive"]["title"])
        self.assertContains(resp, DEFAULT_PUBLIC_CONTENT["creator_note"]["body"][:40])

    def test_about_page_renders_saved_db_content(self):
        PublicContentBlock.objects.create(
            key="biography",
            title="כותרת ביוגרפיה מותאמת",
            body="טקסט ביוגרפיה מותאם לבדיקה.",
        )
        PublicContentBlock.objects.create(
            key="contact_note",
            title="יצירת קשר",
            body=f"פנייה במייל: {CONTACT_EMAIL}",
        )

        resp = self.client.get(self.about_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "כותרת ביוגרפיה מותאמת")
        self.assertContains(resp, "טקסט ביוגרפיה מותאם לבדיקה.")

    def test_about_page_includes_contact_email(self):
        resp = self.client.get(self.about_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, CONTACT_EMAIL)

    def test_staff_can_access_edit_page(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.edit_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "עריכת תוכן ציבורי")

    def test_anonymous_cannot_access_edit_page(self):
        resp = self.client.get(self.edit_url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])

    def test_non_staff_cannot_access_edit_page(self):
        self.client.force_login(self.viewer)
        resp = self.client.get(self.edit_url)
        self.assertEqual(resp.status_code, 403)

    def test_saving_edited_content_updates_public_page(self):
        self.client.force_login(self.staff)
        biography_extended_body = DEFAULT_PUBLIC_CONTENT["biography_extended"]["body"]
        save_resp = self.client.post(
            self.edit_url,
            {
                "title_page_intro": "",
                "body_page_intro": DEFAULT_PUBLIC_CONTENT["page_intro"]["body"],
                "title_biography": "ביוגרפיה מעודכנת",
                "body_biography": "גוף ביוגרפיה מעודכן לבדיקה.",
                "title_biography_extended": "",
                "body_biography_extended": biography_extended_body,
                "title_about_archive": DEFAULT_PUBLIC_CONTENT["about_archive"]["title"],
                "body_about_archive": DEFAULT_PUBLIC_CONTENT["about_archive"]["body"],
                "title_creator_note": DEFAULT_PUBLIC_CONTENT["creator_note"]["title"],
                "body_creator_note": DEFAULT_PUBLIC_CONTENT["creator_note"]["body"],
                "title_contact_note": "יצירת קשר",
                "body_contact_note": f"מייל: {CONTACT_EMAIL}",
            },
        )
        self.assertEqual(save_resp.status_code, 200)
        self.assertContains(save_resp, "התוכן נשמר בהצלחה")

        about_resp = self.client.get(self.about_url)
        self.assertEqual(about_resp.status_code, 200)
        self.assertContains(about_resp, "ביוגרפיה מעודכנת")
        self.assertContains(about_resp, "גוף ביוגרפיה מעודכן לבדיקה.")


class PublicContentEditSecurityTests(TestCase):
    ATTACK_BIO_TITLE = "כותרת ביוגרפיה לא מורשית"
    ATTACK_BIO_BODY = "גוף ביוגרפיה לא מורשה לבדיקת אבטחה."

    def setUp(self):
        self.about_url = reverse("public-about")
        self.edit_url = reverse("public-content-edit")
        self.staff = User.objects.create_user(
            username="content_edit_security_staff",
            password="test-pass",
            is_staff=True,
        )
        self.viewer = User.objects.create_user(
            username="content_edit_security_viewer",
            password="test-pass",
            is_staff=False,
        )
        self.csrf_client = Client(enforce_csrf_checks=True)

    def _attack_post_data(self):
        biography_extended_body = DEFAULT_PUBLIC_CONTENT["biography_extended"]["body"]
        return {
            "title_page_intro": "",
            "body_page_intro": DEFAULT_PUBLIC_CONTENT["page_intro"]["body"],
            "title_biography": self.ATTACK_BIO_TITLE,
            "body_biography": self.ATTACK_BIO_BODY,
            "title_biography_extended": "",
            "body_biography_extended": biography_extended_body,
            "title_about_archive": DEFAULT_PUBLIC_CONTENT["about_archive"]["title"],
            "body_about_archive": DEFAULT_PUBLIC_CONTENT["about_archive"]["body"],
            "title_creator_note": DEFAULT_PUBLIC_CONTENT["creator_note"]["title"],
            "body_creator_note": DEFAULT_PUBLIC_CONTENT["creator_note"]["body"],
            "title_contact_note": "יצירת קשר",
            "body_contact_note": f"מייל: {CONTACT_EMAIL}",
        }

    def _assert_attack_content_not_persisted(self):
        self.assertFalse(PublicContentBlock.objects.filter(key="biography").exists())
        about_resp = self.client.get(self.about_url)
        self.assertEqual(about_resp.status_code, 200)
        self.assertNotContains(about_resp, self.ATTACK_BIO_TITLE)
        self.assertNotContains(about_resp, self.ATTACK_BIO_BODY)

    def test_anonymous_post_redirects_to_login_and_does_not_save(self):
        resp = self.client.post(self.edit_url, self._attack_post_data())
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])
        self._assert_attack_content_not_persisted()

    def test_non_staff_post_returns_403_and_does_not_save(self):
        self.client.force_login(self.viewer)
        resp = self.client.post(self.edit_url, self._attack_post_data())
        self.assertEqual(resp.status_code, 403)
        self._assert_attack_content_not_persisted()

    def test_staff_post_without_csrf_is_rejected_and_does_not_save(self):
        PublicContentBlock.objects.create(
            key="biography",
            title="כותרת ביוגרפיה מקורית",
            body="גוף ביוגרפיה מקורי לבדיקת CSRF.",
        )
        self.csrf_client.force_login(self.staff)

        resp = self.csrf_client.post(self.edit_url, self._attack_post_data())
        self.assertEqual(resp.status_code, 403)

        block = PublicContentBlock.objects.get(key="biography")
        self.assertEqual(block.title, "כותרת ביוגרפיה מקורית")
        self.assertEqual(block.body, "גוף ביוגרפיה מקורי לבדיקת CSRF.")

        about_resp = self.client.get(self.about_url)
        self.assertEqual(about_resp.status_code, 200)
        self.assertContains(about_resp, "כותרת ביוגרפיה מקורית")
        self.assertContains(about_resp, "גוף ביוגרפיה מקורי לבדיקת CSRF.")
        self.assertNotContains(about_resp, self.ATTACK_BIO_TITLE)
        self.assertNotContains(about_resp, self.ATTACK_BIO_BODY)
