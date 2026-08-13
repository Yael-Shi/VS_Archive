from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

User = get_user_model()


class LoginRedirectTests(TestCase):
    def setUp(self):
        self.password = "test-pass"
        self.user = User.objects.create_user(
            username="login_redirect_user",
            password=self.password,
        )
        self.login_url = reverse("login")
        self.archive_list_url = reverse("archive-list")
        self.protected_url = reverse("public-content-edit")

    def _post_login(self, *, login_url=None):
        return self.client.post(
            login_url or self.login_url,
            {
                "username": self.user.username,
                "password": self.password,
            },
        )

    def test_direct_login_without_next_redirects_to_archive_list(self):
        resp = self._post_login()
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self.archive_list_url)

    def test_login_with_next_redirects_to_requested_protected_destination(self):
        bounce = self.client.get(self.protected_url)
        self.assertEqual(bounce.status_code, 302)
        self.assertIn(self.login_url, bounce["Location"])

        resp = self._post_login(login_url=bounce["Location"])
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self.protected_url)
        self.assertNotEqual(resp["Location"], self.archive_list_url)
