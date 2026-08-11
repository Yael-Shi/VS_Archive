from django.contrib.auth import get_user_model
from django.test import Client, TestCase
from django.urls import reverse

from public.models import PublicContentBlock
from public.services.public_content import (
    CONTACT_EMAIL,
    DEFAULT_PUBLIC_CONTENT,
)

User = get_user_model()


def _create_test_user(username, *, is_staff=False):
    return User.objects.create_user(
        username=username,
        password="test-pass",
        is_staff=is_staff,
    )


def _content_edit_post_data(*, title_biography, body_biography):
    return {
        "title_page_intro": "",
        "body_page_intro": DEFAULT_PUBLIC_CONTENT["page_intro"]["body"],
        "title_biography": title_biography,
        "body_biography": body_biography,
        "title_biography_extended": "",
        "body_biography_extended": DEFAULT_PUBLIC_CONTENT["biography_extended"]["body"],
        "title_about_archive": DEFAULT_PUBLIC_CONTENT["about_archive"]["title"],
        "body_about_archive": DEFAULT_PUBLIC_CONTENT["about_archive"]["body"],
        "title_creator_note": DEFAULT_PUBLIC_CONTENT["creator_note"]["title"],
        "body_creator_note": DEFAULT_PUBLIC_CONTENT["creator_note"]["body"],
        "title_contact_note": "יצירת קשר",
        "body_contact_note": f"מייל: {CONTACT_EMAIL}",
    }


class PublicNavTests(TestCase):
    def test_public_nav_omits_redundant_home_link(self):
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'class="nav-brand-link"')
        self.assertContains(resp, "ארכיון")
        self.assertContains(resp, "אודות")
        self.assertNotContains(resp, '<a class="btn btn-link" href="/">דף הבית</a>')


def _nav_archive_search_form_html(html: str) -> str:
    marker = 'class="nav-archive-search"'
    start = html.find(marker)
    if start < 0:
        raise AssertionError("nav-archive-search form not found")
    form_start = html.rfind("<form", 0, start)
    if form_start < 0:
        raise AssertionError("nav-archive-search opening form tag not found")
    form_end = html.find("</form>", start)
    if form_end < 0:
        raise AssertionError("nav-archive-search closing form tag not found")
    return html[form_start : form_end + len("</form>")]


def _input_tag_by_id(html: str, input_id: str) -> str:
    needle = f'id="{input_id}"'
    idx = html.find(needle)
    if idx < 0:
        raise AssertionError(f'input id="{input_id}" not found')
    tag_start = html.rfind("<input", 0, idx)
    if tag_start < 0:
        raise AssertionError(f'opening <input for id="{input_id}" not found')
    tag_end = html.find(">", idx)
    if tag_end < 0:
        raise AssertionError(f'closing > for id="{input_id}" not found')
    return html[tag_start : tag_end + 1]


class PublicNavArchiveSearchTests(TestCase):
    """PR3: compact global header q search in shared public nav."""

    ARCHIVE_LIST_URL = reverse("archive-list")

    def _assert_nav_archive_search_contract(self, resp):
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        form_html = _nav_archive_search_form_html(html)

        self.assertIn('method="get"', form_html)
        self.assertIn(f'action="{self.ARCHIVE_LIST_URL}"', form_html)
        self.assertIn('role="search"', form_html)
        self.assertIn('for="nav-archive-search-q"', form_html)
        self.assertIn("חיפוש בארכיון", form_html)
        self.assertIn('class="visually-hidden"', form_html)

        input_tag = _input_tag_by_id(form_html, "nav-archive-search-q")
        self.assertIn('type="search"', input_tag)
        self.assertIn('name="q"', input_tag)
        self.assertIn('class="nav-archive-search__input"', input_tag)
        self.assertIn('placeholder="חיפוש בארכיון…"', input_tag)
        self.assertNotIn("value=", input_tag)

        self.assertIn('type="submit"', form_html)
        self.assertIn("nav-archive-search__submit", form_html)
        self.assertRegex(
            form_html,
            r'<button[^>]*type="submit"[^>]*>\s*חיפוש\s*</button>',
        )

        # q-only: no advanced / pagination / type state preserved by the form.
        for forbidden in (
            'name="author"',
            'name="category"',
            'name="event"',
            'name="tag"',
            'name="year"',
            'name="year_to"',
            'name="item_type"',
            'name="per_page"',
            'name="page"',
            'name="advanced"',
        ):
            self.assertNotIn(forbidden, form_html)

        return html

    def test_shared_public_nav_renders_archive_search_form(self):
        resp = self.client.get(self.ARCHIVE_LIST_URL)
        self._assert_nav_archive_search_contract(resp)

    def test_home_page_includes_header_search(self):
        resp = self.client.get(reverse("public-home"))
        self._assert_nav_archive_search_contract(resp)

    def test_about_page_includes_header_search(self):
        resp = self.client.get(reverse("public-about"))
        self._assert_nav_archive_search_contract(resp)

    def test_archive_page_keeps_main_search_and_empty_nav_q(self):
        resp = self.client.get(self.ARCHIVE_LIST_URL, {"q": "חיים מרזוק"})
        html = self._assert_nav_archive_search_contract(resp)

        self.assertIn('id="archive-search-form"', html)
        self.assertIn('id="archive-filter-q"', html)
        main_input = _input_tag_by_id(html, "archive-filter-q")
        self.assertIn('name="q"', main_input)
        self.assertIn("חיים מרזוק", main_input)

        nav_input = _input_tag_by_id(html, "nav-archive-search-q")
        self.assertNotIn("value=", nav_input)
        self.assertNotIn("חיים מרזוק", nav_input)

    def test_archive_detail_includes_shared_header_search(self):
        from documents.models import ArchiveItem
        from documents.services.archive_items import create_manual_text_archive_item

        item = create_manual_text_archive_item(
            title="Nav header search detail item",
            body="detail body",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        resp = self.client.get(
            reverse("archive-detail", kwargs={"item_id": item.pk})
        )
        self._assert_nav_archive_search_contract(resp)

    def test_header_q_get_uses_existing_archive_search_contract(self):
        from documents.models import ArchiveItem
        from documents.services.archive_items import create_manual_text_archive_item
        from documents.services.archive_search_index import (
            sync_archive_item_search_index,
        )

        hit = create_manual_text_archive_item(
            title="חיים מרזוק header chain",
            body="body without the query token",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        miss = create_manual_text_archive_item(
            title="unrelated archive title",
            body="no match here",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        sync_archive_item_search_index(hit.pk)
        sync_archive_item_search_index(miss.pk)

        # Same GET contract a nav form submit produces: /archive/?q=...
        resp = self.client.get(self.ARCHIVE_LIST_URL, {"q": "חיים מרזוק"})
        html = self._assert_nav_archive_search_contract(resp)

        self.assertEqual(resp.status_code, 200)
        page_ids = [item.pk for item in resp.context["items"]]
        self.assertIn(hit.pk, page_ids)
        self.assertNotIn(miss.pk, page_ids)
        self.assertContains(resp, "חיים מרזוק header chain")
        self.assertIn("חיפוש מתקדם", html)
        self.assertIn('id="archive-search-form"', html)
        self.assertNotIn("advanced", resp.wsgi_request.GET)

    def test_global_search_does_not_carry_advanced_or_current_query_state(self):
        resp = self.client.get(
            self.ARCHIVE_LIST_URL,
            {
                "q": "existing-q",
                "author": "Some Author",
                "category": "1",
                "event": "2",
                "tag": "3",
                "year": "1950",
                "year_to": "1960",
                "item_type": "manual_text",
                "per_page": "48",
                "page": "2",
                "advanced": "1",
            },
        )
        html = self._assert_nav_archive_search_contract(resp)
        form_html = _nav_archive_search_form_html(html)
        self.assertEqual(form_html.count('name="q"'), 1)
        self.assertNotIn("existing-q", form_html)
        self.assertNotIn("Some Author", form_html)

    def test_responsive_scoped_css_classes_present_without_staff_nav_change(self):
        staff = _create_test_user("nav_search_staff", is_staff=True)
        self.client.force_login(staff)
        resp = self.client.get(reverse("public-home"))
        html = self._assert_nav_archive_search_contract(resp)

        self.assertIn("nav-archive-search", html)
        self.assertIn("nav-archive-search__input", html)
        self.assertIn("nav-archive-search__submit", html)
        self.assertContains(resp, 'class="card nav-shell nav-staff-panel"')
        self.assertContains(resp, "nav-staff-links")
        self.assertContains(resp, "ניהול ארכיון")

        staff_panel_start = html.find("nav-staff-panel")
        self.assertGreater(staff_panel_start, 0)
        staff_section = html[staff_panel_start:]
        self.assertNotIn("nav-archive-search", staff_section)

    def test_anonymous_and_authenticated_nav_still_render(self):
        anon = self.client.get(reverse("public-home"))
        anon_html = self._assert_nav_archive_search_contract(anon)
        self.assertIn("הרשמה", anon_html)
        self.assertIn("התחברות", anon_html)
        self.assertNotIn("התנתקות", anon_html)

        user = _create_test_user("nav_search_viewer")
        self.client.force_login(user)
        auth = self.client.get(reverse("public-home"))
        auth_html = self._assert_nav_archive_search_contract(auth)
        self.assertIn("התנתקות", auth_html)
        self.assertIn(user.username, auth_html)
        self.assertNotIn("הרשמה", auth_html)


class AboutPageCopyTests(TestCase):
    def test_about_page_does_not_show_english_version_roadmap_copy(self):
        resp = self.client.get(reverse("public-about"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(
            resp, "כרגע האתר מוצג בעברית. בעתיד תתווסף גם גרסה באנגלית."
        )


class ForbiddenPageCopyTests(TestCase):
    FORBIDDEN_MARKERS = (
        "Admins only",
        "is_staff",
        "is_superuser",
        "403",
        "גישה חסומה",
        "לפי הרשאות",
    )

    def setUp(self):
        self.edit_url = reverse("public-content-edit")
        self.viewer = _create_test_user("forbidden_page_viewer")

    def test_non_staff_forbidden_page_shows_family_friendly_hebrew_copy(self):
        self.client.force_login(self.viewer)
        resp = self.client.get(self.edit_url)
        self.assertEqual(resp.status_code, 403)
        self.assertContains(resp, "אין לך גישה לעמוד הזה", status_code=403)
        self.assertContains(resp, reverse("archive-list"), status_code=403)
        self.assertContains(resp, "חזרה לארכיון", status_code=403)
        self.assertContains(resp, "חזרה לדף הבית", status_code=403)
        html = resp.content.decode()
        for marker in self.FORBIDDEN_MARKERS:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, html)


class PublicContentBlockTests(TestCase):
    def setUp(self):
        self.about_url = reverse("public-about")
        self.edit_url = reverse("public-content-edit")
        self.staff = _create_test_user("content_staff", is_staff=True)
        self.viewer = _create_test_user("content_viewer")

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
        save_resp = self.client.post(
            self.edit_url,
            _content_edit_post_data(
                title_biography="ביוגרפיה מעודכנת",
                body_biography="גוף ביוגרפיה מעודכן לבדיקה.",
            ),
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
        self.staff = _create_test_user("content_edit_security_staff", is_staff=True)
        self.viewer = _create_test_user("content_edit_security_viewer")
        self.csrf_client = Client(enforce_csrf_checks=True)

    def _attack_post_data(self):
        return _content_edit_post_data(
            title_biography=self.ATTACK_BIO_TITLE,
            body_biography=self.ATTACK_BIO_BODY,
        )

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
