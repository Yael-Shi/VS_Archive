from django.test import TestCase
from django.urls import reverse


class AboutPageCopyTests(TestCase):
    def test_about_page_does_not_show_english_version_roadmap_copy(self):
        resp = self.client.get(reverse("public-about"))
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(
            resp, "כרגע האתר מוצג בעברית. בעתיד תתווסף גם גרסה באנגלית."
        )
