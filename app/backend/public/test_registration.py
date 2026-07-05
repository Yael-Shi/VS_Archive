"""Public self-registration and pending-approval flow tests."""

from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.core.cache import cache
from django.db import IntegrityError
from django.test import RequestFactory, TestCase, override_settings
from django.urls import reverse

from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from public.services.registration import (
    DUPLICATE_EMAIL_ERROR,
    EMAIL_ATTEMPT_LIMIT,
    HONEYPOT_FIELD_NAME,
    IP_ATTEMPT_LIMIT,
    THROTTLE_ERROR,
    WEAK_PASSWORD_ERROR,
    normalize_email,
    process_registration,
)

User = get_user_model()

_LOC_MEM_CACHE = {
    "default": {
        "BACKEND": "django.core.cache.backends.locmem.LocMemCache",
    }
}


@override_settings(CACHES=_LOC_MEM_CACHE)
class PublicRegistrationTests(TestCase):
    REGISTER_URL = reverse("public-register")
    PENDING_URL = reverse("pending-approval")
    ARCHIVE_LIST_URL = reverse("archive-list")

    def setUp(self):
        cache.clear()
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.valid_password = "SecurePass123!"
        self._email_counter = 0

    def _unique_email(self, label: str) -> str:
        self._email_counter += 1
        return f"{label}.{self._email_counter}@example.com"

    def _registration_payload(self, **overrides):
        email = overrides.pop("email", self._unique_email("register"))
        payload = {
            "first_name": "ישראל",
            "last_name": "ישראלי",
            "email": email,
            "password1": self.valid_password,
            "password2": self.valid_password,
            HONEYPOT_FIELD_NAME: "",
        }
        payload.update(overrides)
        return payload

    def _post_register(self, **overrides):
        return self.client.post(
            self.REGISTER_URL, self._registration_payload(**overrides)
        )

    def _get_user_by_email(self, email: str):
        return User.objects.get(username=normalize_email(email))

    def _assert_redirect_to(self, resp, url: str):
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], url)

    def test_anonymous_user_can_view_registration_page(self):
        resp = self.client.get(self.REGISTER_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "הרשמה")
        self.assertContains(resp, 'name="email"')

    def test_valid_registration_creates_non_staff_non_superuser_user(self):
        email = self._unique_email("valid.user")
        resp = self._post_register(email=email)
        self._assert_redirect_to(resp, self.PENDING_URL)

        user = self._get_user_by_email(email)
        self.assertFalse(user.is_staff)
        self.assertFalse(user.is_superuser)
        self.assertEqual(user.first_name, "ישראל")
        self.assertEqual(user.last_name, "ישראלי")
        self.assertEqual(user.email, normalize_email(email))

    def test_registered_user_is_not_added_to_archive_family(self):
        email = self._unique_email("not.family")
        self._post_register(email=email)
        user = self._get_user_by_email(email)
        self.assertFalse(user.groups.filter(name=ARCHIVE_FAMILY_GROUP_NAME).exists())

    def test_email_is_required(self):
        resp = self._post_register(email="")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "יש למלא כתובת דוא״ל")
        self.assertEqual(User.objects.count(), 0)

    def test_duplicate_email_is_rejected_case_insensitively(self):
        User.objects.create_user(
            username="existing@example.com",
            email="Existing@Example.com",
            password="test-pass",
        )
        resp = self._post_register(email="existing@EXAMPLE.com")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, DUPLICATE_EMAIL_ERROR)
        self.assertEqual(User.objects.count(), 1)

    def test_password_validation_is_enforced(self):
        email = self._unique_email("weak.pass")
        resp = self._post_register(email=email, password1="123", password2="123")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username=normalize_email(email)).exists())
        self.assertContains(resp, WEAK_PASSWORD_ERROR)

    def test_honeypot_submission_does_not_create_user(self):
        resp = self._post_register(**{HONEYPOT_FIELD_NAME: "Acme Corp"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(User.objects.count(), 0)

    def test_throttle_allows_exactly_ip_limit_then_blocks_next_attempt(self):
        user_count_before = User.objects.count()
        allowed_emails = [
            self._unique_email(f"ip-allowed-{i}") for i in range(IP_ATTEMPT_LIMIT)
        ]
        for index, email in enumerate(allowed_emails, start=1):
            resp = self._post_register(email=email, password2="wrong")
            self.assertEqual(
                resp.status_code,
                200,
                msg=f"attempt {index} should reach registration processing",
            )
            self.assertNotContains(resp, THROTTLE_ERROR)

        blocked_email = self._unique_email("ip-blocked")
        blocked = self._post_register(email=blocked_email, password2="wrong")
        self.assertEqual(blocked.status_code, 200)
        self.assertContains(blocked, THROTTLE_ERROR)
        self.assertEqual(User.objects.count(), user_count_before)
        for email in [*allowed_emails, blocked_email]:
            self.assertFalse(
                User.objects.filter(username=normalize_email(email)).exists()
            )

    def test_throttle_allows_exactly_email_limit_then_blocks_next_attempt(self):
        email = self._unique_email("email-throttle")

        for index in range(EMAIL_ATTEMPT_LIMIT):
            resp = self._post_register(email=email, password2="wrong")
            self.assertEqual(resp.status_code, 200, msg=f"attempt {index + 1}")
            self.assertNotContains(resp, THROTTLE_ERROR)

        blocked = self._post_register(email=email)
        self.assertEqual(blocked.status_code, 200)
        self.assertContains(blocked, THROTTLE_ERROR)
        self.assertFalse(User.objects.filter(username=normalize_email(email)).exists())

    def test_integrity_error_returns_duplicate_email_error(self):
        request = RequestFactory().post("/accounts/register/")
        post_data = self._registration_payload(email=self._unique_email("race"))

        with patch.object(User, "save", side_effect=IntegrityError):
            result = process_registration(request=request, post_data=post_data)

        self.assertIsNone(result.user)
        self.assertEqual(result.errors, [DUPLICATE_EMAIL_ERROR])

    def test_pending_approval_message_is_shown_after_registration(self):
        self._post_register(email=self._unique_email("pending"))
        resp = self.client.get(self.PENDING_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "החשבון ממתין לאישור")
        self.assertContains(resp, "צוות הארכיון יבדוק את הבקשה")

    def test_authenticated_approved_family_user_behavior_is_unchanged(self):
        family_user = User.objects.create_user(
            username="family_member",
            email="family.member@example.com",
            password="test-pass",
        )
        family_user.groups.add(self.family_group)
        self.client.force_login(family_user)

        register_resp = self.client.get(self.REGISTER_URL)
        self._assert_redirect_to(register_resp, self.ARCHIVE_LIST_URL)

        pending_resp = self.client.get(self.PENDING_URL)
        self._assert_redirect_to(pending_resp, self.ARCHIVE_LIST_URL)

    def test_visible_user_facing_copy_does_not_expose_archive_family(self):
        register_resp = self.client.get(self.REGISTER_URL)
        self.assertEqual(register_resp.status_code, 200)
        self.assertNotContains(register_resp, "archive_family")

        self._post_register(email=self._unique_email("copy-check"))
        pending_resp = self.client.get(self.PENDING_URL)
        self.assertEqual(pending_resp.status_code, 200)
        self.assertNotContains(pending_resp, "archive_family")

        login_resp = self.client.get(reverse("login"))
        self.assertEqual(login_resp.status_code, 200)
        self.assertNotContains(login_resp, "archive_family")

        home_resp = self.client.get(reverse("public-home"))
        self.assertEqual(home_resp.status_code, 200)
        self.assertNotContains(home_resp, "archive_family")

    def test_pending_user_without_family_access_can_view_pending_page(self):
        user = User.objects.create_user(
            username="pending.user@example.com",
            email="pending.user@example.com",
            password="test-pass",
        )
        self.client.force_login(user)
        resp = self.client.get(self.PENDING_URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "החשבון ממתין לאישור")

    def test_registration_link_visible_to_anonymous_users_in_nav(self):
        resp = self.client.get(reverse("public-home"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, reverse("public-register"))
        self.assertContains(resp, "הרשמה")
