"""Staff-only management of archive_family group membership."""

from django.contrib.auth import get_user_model
from django.contrib.auth.models import Group
from django.test import TestCase
from django.urls import reverse

from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME

User = get_user_model()


class ArchiveFamilyAccessManagePageTests(TestCase):
    URL = reverse("archive-manage-family-access")

    def setUp(self):
        self.staff = User.objects.create_user(
            username="family_access_staff",
            password="test-pass",
            is_staff=True,
        )
        self.superuser = User.objects.create_user(
            username="family_access_superuser",
            password="test-pass",
            is_staff=True,
            is_superuser=True,
        )
        self.regular_user = User.objects.create_user(
            username="family_access_user",
            email="family.user@example.com",
            password="test-pass",
            first_name="Family",
            last_name="Member",
        )
        self.unmatched_user = User.objects.create_user(
            username="unmatched_family_user",
            password="test-pass",
        )
        self.no_email_user = User.objects.create_user(
            username="no_email_family_user",
            password="test-pass",
            email="",
        )
        self.family_group = Group.objects.create(name=ARCHIVE_FAMILY_GROUP_NAME)

    def _post_membership(self, *, action: str, user_id):
        return self.client.post(
            self.URL,
            data={"action": action, "user_id": user_id},
        )

    def test_anonymous_cannot_access(self):
        resp = self.client.get(self.URL)
        self.assertIn(resp.status_code, (302, 403))

    def test_non_staff_cannot_access(self):
        self.client.force_login(self.regular_user)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 403)

    def test_staff_can_view_page(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ניהול גישת משפחה")
        self.assertContains(resp, self.regular_user.username)
        self.assertContains(resp, "הוסף גישת משפחה")
        self.assertContains(resp, "לצוות יש גישה מלאה")

    def test_staff_can_add_existing_regular_user_by_post(self):
        self.client.force_login(self.staff)
        resp = self._post_membership(action="add", user_id=self.regular_user.id)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(
            self.regular_user.groups.filter(name=ARCHIVE_FAMILY_GROUP_NAME).exists()
        )
        follow = self.client.get(resp["Location"])
        self.assertContains(follow, "גישת המשפחה נוספה למשתמש.")

    def test_staff_can_remove_existing_regular_user_by_post(self):
        self.regular_user.groups.add(self.family_group)
        self.client.force_login(self.staff)
        resp = self._post_membership(action="remove", user_id=self.regular_user.id)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(
            self.regular_user.groups.filter(name=ARCHIVE_FAMILY_GROUP_NAME).exists()
        )
        follow = self.client.get(resp["Location"])
        self.assertContains(follow, "גישת המשפחה הוסרה מהמשתמש.")

    def test_get_does_not_change_membership(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(
            self.regular_user.groups.filter(name=ARCHIVE_FAMILY_GROUP_NAME).exists()
        )

    def test_missing_family_access_configuration_is_handled_safely(self):
        self.family_group.delete()
        self.client.force_login(self.staff)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "הגדרת גישת משפחה חסרה")
        self.assertContains(resp, "קבוצת גישת המשפחה לא הוגדרה במערכת")
        self.assertNotContains(resp, "archive_family")

    def test_invalid_post_action_returns_400_and_changes_nothing(self):
        self.client.force_login(self.staff)
        resp = self._post_membership(action="toggle", user_id=self.regular_user.id)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(
            self.regular_user.groups.filter(name=ARCHIVE_FAMILY_GROUP_NAME).exists()
        )

    def test_missing_user_id_returns_400_and_changes_nothing(self):
        self.client.force_login(self.staff)
        resp = self.client.post(self.URL, data={"action": "add"})
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(
            self.regular_user.groups.filter(name=ARCHIVE_FAMILY_GROUP_NAME).exists()
        )

    def test_invalid_user_id_returns_400_and_changes_nothing(self):
        self.client.force_login(self.staff)
        resp = self.client.post(
            self.URL,
            data={"action": "add", "user_id": "not-a-number"},
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(
            self.regular_user.groups.filter(name=ARCHIVE_FAMILY_GROUP_NAME).exists()
        )

    def test_staff_target_cannot_be_modified_by_post(self):
        self.client.force_login(self.staff)
        resp = self._post_membership(action="add", user_id=self.staff.id)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(
            self.staff.groups.filter(name=ARCHIVE_FAMILY_GROUP_NAME).exists()
        )

    def test_superuser_target_cannot_be_modified_by_post(self):
        self.client.force_login(self.staff)
        resp = self._post_membership(action="add", user_id=self.superuser.id)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(
            self.superuser.groups.filter(name=ARCHIVE_FAMILY_GROUP_NAME).exists()
        )

    def test_normal_visible_page_copy_does_not_expose_archive_family_when_configured(
        self,
    ):
        self.client.force_login(self.staff)
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "archive_family")

    def test_search_by_username(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.URL, data={"q": "family_access_user"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.regular_user.username)
        self.assertNotContains(resp, self.unmatched_user.username)

    def test_user_without_email_is_shown_as_missing_email(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.URL, data={"q": self.no_email_user.username})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, self.no_email_user.username)
        self.assertContains(resp, "חסר דוא״ל")

    def test_user_without_email_does_not_get_add_button(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.URL, data={"q": self.no_email_user.username})
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, "הוסף גישת משפחה")
        self.assertContains(resp, "יש להוסיף דוא״ל לפני מתן גישת משפחה")

    def test_post_add_for_user_without_email_returns_400_and_changes_nothing(self):
        self.client.force_login(self.staff)
        resp = self._post_membership(action="add", user_id=self.no_email_user.id)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(
            self.no_email_user.groups.filter(name=ARCHIVE_FAMILY_GROUP_NAME).exists()
        )

    def test_post_remove_for_user_without_email_with_family_access_succeeds(self):
        self.no_email_user.groups.add(self.family_group)
        self.client.force_login(self.staff)
        resp = self._post_membership(action="remove", user_id=self.no_email_user.id)
        self.assertEqual(resp.status_code, 302)
        self.assertFalse(
            self.no_email_user.groups.filter(name=ARCHIVE_FAMILY_GROUP_NAME).exists()
        )
        follow = self.client.get(resp["Location"])
        self.assertContains(follow, "גישת המשפחה הוסרה מהמשתמש.")

    def test_whitespace_only_email_is_treated_as_missing(self):
        whitespace_email_user = User.objects.create_user(
            username="whitespace_email_family_user",
            password="test-pass",
            email="   ",
        )
        self.client.force_login(self.staff)
        resp = self.client.get(self.URL, data={"q": whitespace_email_user.username})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, whitespace_email_user.username)
        self.assertContains(resp, "חסר דוא״ל")
        self.assertNotContains(resp, "הוסף גישת משפחה")

        resp = self._post_membership(action="add", user_id=whitespace_email_user.id)
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(
            whitespace_email_user.groups.filter(name=ARCHIVE_FAMILY_GROUP_NAME).exists()
        )
