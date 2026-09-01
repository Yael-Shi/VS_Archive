"""Staff global Author rename page and service."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib import admin as django_admin
from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import Resolver404, resolve, reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemAuthor,
    ArchiveItemPerson,
    ArchiveItemSearchIndex,
    Author,
    Document,
    Person,
    PersonAlias,
    PhotoPerson,
)
from documents.services.archive_advanced_search import (
    filter_archive_items_by_advanced_filters,
    normalize_archive_advanced_filters,
)
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_item_authors import (
    AUTHOR_JOINED_TOO_LONG_ERROR,
    AUTHOR_LINKS_CHANGED_RETRY_ERROR,
    AUTHOR_NAME_COLLISION_ERROR,
    AUTHOR_NAME_REQUIRED_ERROR,
    AUTHOR_NAME_TOO_LONG_ERROR,
    ArchiveItemAuthorError,
    affected_archive_item_ids_for_author,
    affected_archive_items_for_author,
    ordered_authors,
    rename_author,
)
from documents.services.archive_item_presentation import (
    filter_archive_items_by_search_query,
)
from documents.services.archive_items import (
    create_manual_text_archive_item,
    create_ocr_document,
    create_video_archive_item,
)
from documents.services.archive_search_index import (
    archive_items_for_search_index_build,
    rebuild_archive_item_search_index,
)
from documents.views import AUTHOR_NAME_UPDATED_MSG

YOUTUBE_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def _create_item(*, title: str, author_name: str = "") -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        title=title,
        visibility=ArchiveItem.Visibility.PUBLIC,
        author_name=author_name,
    )


def _link(item: ArchiveItem, author: Author, *, position: int) -> ArchiveItemAuthor:
    return ArchiveItemAuthor.objects.create(
        archive_item=item,
        author=author,
        position=position,
    )


def _rebuild(archive_item_id: int) -> ArchiveItemSearchIndex:
    item = archive_items_for_search_index_build(
        archive_item_ids=[archive_item_id]
    ).get()
    return rebuild_archive_item_search_index(item)


def _metadata(archive_item_id: int) -> str:
    return ArchiveItemSearchIndex.objects.get(
        archive_item_id=archive_item_id
    ).metadata_text


def _search_ids(token: str) -> list[int]:
    return list(
        filter_archive_items_by_search_query(
            ArchiveItem.objects.all(), token
        ).values_list("pk", flat=True)
    )


def _edit_url(author: Author) -> str:
    return reverse("archive-manage-author-edit", kwargs={"author_id": author.id})


def _author_name(item: ArchiveItem) -> str:
    item.refresh_from_db()
    return item.author_name


class AuthorRenameRouteTests(TestCase):
    def test_route_resolves_and_no_list_or_delete_routes_exist(self):
        match = resolve("/archive/manage/authors/1/edit/")
        self.assertEqual(match.url_name, "archive-manage-author-edit")
        for path in (
            "/archive/manage/authors/",
            "/archive/manage/authors/1/delete/",
            "/archive/manage/authors/1/merge/",
        ):
            with self.assertRaises(Resolver404):
                resolve(path)

    def test_author_models_stay_out_of_django_admin(self):
        self.assertFalse(django_admin.site.is_registered(Author))
        self.assertFalse(django_admin.site.is_registered(ArchiveItemAuthor))


class AuthorRenameAccessTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="author_edit_staff",
            password="test-pass",
            is_staff=True,
        )
        self.author = Author.objects.create(name="יעקב כהן")
        self.url = _edit_url(self.author)

    def test_staff_can_open_author_edit(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "עריכת מחבר/ת")
        self.assertContains(resp, "יעקב כהן")
        self.assertContains(resp, 'id="author_name"')
        self.assertContains(resp, 'name="name"')
        self.assertContains(resp, "עדכון שם מחבר/ת")

    def test_anonymous_is_redirected_to_login(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertTrue(resp["Location"].startswith("/accounts/login/"))

    def test_non_staff_is_forbidden(self):
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        user = User.objects.create_user(
            username="author_edit_family",
            password="test-pass",
        )
        user.groups.add(family_group)
        self.client.force_login(user)
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 403)

    def test_nonexistent_author_is_404(self):
        self.client.force_login(self.staff)
        resp = self.client.get(
            reverse("archive-manage-author-edit", kwargs={"author_id": 999999})
        )
        self.assertEqual(resp.status_code, 404)

    def test_family_and_anonymous_cannot_post_rename(self):
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        family = User.objects.create_user(
            username="author_post_family",
            password="test-pass",
        )
        family.groups.add(family_group)

        anon = self.client.post(self.url, data={"name": "anon-author-token"})
        self.assertEqual(anon.status_code, 302)
        self.assertTrue(anon["Location"].startswith("/accounts/login/"))

        self.client.force_login(family)
        forbidden = self.client.post(self.url, data={"name": "family-author-token"})
        self.assertEqual(forbidden.status_code, 403)

        self.author.refresh_from_db()
        self.assertEqual(self.author.name, "יעקב כהן")


class AuthorRenamePreviewTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="author_preview_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.author = Author.objects.create(name="PreviewAuthorToken")
        self.other_author = Author.objects.create(name="OtherAuthorToken")
        self.first = _create_item(
            title="PreviewFirstItem", author_name="PreviewAuthorToken"
        )
        self.second = _create_item(
            title="PreviewSecondItem", author_name="PreviewAuthorToken"
        )
        self.unrelated = _create_item(
            title="PreviewUnrelatedItem", author_name="OtherAuthorToken"
        )
        _link(self.first, self.author, position=0)
        _link(self.second, self.author, position=0)
        _link(self.unrelated, self.other_author, position=0)
        self.url = _edit_url(self.author)

    def test_preview_lists_affected_items_with_edit_links_and_count(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "פריטים שיושפעו (2)")
        self.assertContains(resp, "PreviewFirstItem")
        self.assertContains(resp, "PreviewSecondItem")
        self.assertNotContains(resp, "PreviewUnrelatedItem")
        self.assertContains(
            resp,
            reverse("archive-manage-edit", kwargs={"item_id": self.first.id}),
        )
        self.assertContains(
            resp,
            reverse("archive-manage-edit", kwargs={"item_id": self.second.id}),
        )
        self.assertEqual(resp.context["affected_item_count"], 2)

    def test_preview_performs_no_writes(self):
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_indexes"
        ) as mocked_sync:
            resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        mocked_sync.assert_not_called()
        self.author.refresh_from_db()
        self.assertEqual(self.author.name, "PreviewAuthorToken")
        self.assertEqual(_author_name(self.first), "PreviewAuthorToken")
        self.assertEqual(_author_name(self.second), "PreviewAuthorToken")

    def test_author_with_no_links_shows_empty_state(self):
        lonely = Author.objects.create(name="LonelyAuthorToken")
        resp = self.client.get(_edit_url(lonely))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "פריטים שיושפעו (0)")
        self.assertContains(resp, "אין פריטים משויכים")

    def test_affected_helpers_return_linked_items_in_ascending_order(self):
        self.assertEqual(
            affected_archive_item_ids_for_author(self.author),
            sorted([self.first.pk, self.second.pk]),
        )
        self.assertEqual(
            [item.pk for item in affected_archive_items_for_author(self.author)],
            sorted([self.first.pk, self.second.pk]),
        )


class AuthorRenameRejectionTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="author_reject_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.author = Author.objects.create(name="RejectAuthorToken")
        self.item = _create_item(title="RejectItem", author_name="RejectAuthorToken")
        _link(self.item, self.author, position=0)
        _rebuild(self.item.pk)
        self.url = _edit_url(self.author)

    def _assert_nothing_changed(self):
        self.author.refresh_from_db()
        self.assertEqual(self.author.name, "RejectAuthorToken")
        self.assertEqual(_author_name(self.item), "RejectAuthorToken")
        self.assertIn("RejectAuthorToken", _metadata(self.item.pk))

    def test_blank_name_is_rejected(self):
        for blank in ("", "   ", "\n\t "):
            resp = self.client.post(self.url, data={"name": blank})
            self.assertEqual(resp.status_code, 200)
            self.assertContains(resp, AUTHOR_NAME_REQUIRED_ERROR)
            self._assert_nothing_changed()

    def test_name_over_255_chars_is_rejected(self):
        resp = self.client.post(self.url, data={"name": "x" * 256})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, AUTHOR_NAME_TOO_LONG_ERROR)
        self._assert_nothing_changed()

    def test_exact_collision_with_another_author_is_rejected_without_merge(self):
        other = Author.objects.create(name="CollisionAuthorToken")
        other_item = _create_item(
            title="CollisionItem", author_name="CollisionAuthorToken"
        )
        _link(other_item, other, position=0)

        resp = self.client.post(self.url, data={"name": "CollisionAuthorToken"})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, AUTHOR_NAME_COLLISION_ERROR)
        self._assert_nothing_changed()

        self.assertEqual(Author.objects.count(), 2)
        self.assertEqual(Author.objects.filter(name="CollisionAuthorToken").count(), 1)
        other.refresh_from_db()
        self.assertEqual(other.name, "CollisionAuthorToken")
        self.assertEqual(_author_name(other_item), "CollisionAuthorToken")
        self.assertEqual(ordered_authors(self.item), [self.author])
        self.assertEqual(ordered_authors(other_item), [other])

    def test_collision_check_ignores_the_renamed_author_itself(self):
        resp = self.client.post(
            self.url,
            data={"name": "RejectAuthorToken"},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertNotContains(resp, AUTHOR_NAME_COLLISION_ERROR)

    def test_rebuilt_joined_author_name_over_255_is_rejected(self):
        co_author = Author.objects.create(name="y" * 200)
        multi_item = _create_item(title="MultiAuthorItem")
        _link(multi_item, self.author, position=0)
        _link(multi_item, co_author, position=1)
        multi_item.author_name = f"RejectAuthorToken, {'y' * 200}"
        multi_item.save(update_fields=["author_name", "updated_at"])
        _rebuild(multi_item.pk)

        resp = self.client.post(self.url, data={"name": "z" * 100})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, AUTHOR_JOINED_TOO_LONG_ERROR)
        self._assert_nothing_changed()
        self.assertEqual(_author_name(multi_item), f"RejectAuthorToken, {'y' * 200}")
        self.assertNotIn("z" * 100, _metadata(multi_item.pk))

    def test_error_preserves_submitted_input_and_still_shows_preview(self):
        resp = self.client.post(self.url, data={"name": "x" * 256})
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "x" * 256)
        self.assertContains(resp, "פריטים שיושפעו (1)")
        self.assertContains(resp, "RejectItem")
        self.assertEqual(resp.context["author_name"], "x" * 256)

    def test_service_raises_domain_error_for_rejections(self):
        Author.objects.create(name="ServiceCollisionToken")
        for submitted, expected in (
            ("   ", AUTHOR_NAME_REQUIRED_ERROR),
            ("x" * 256, AUTHOR_NAME_TOO_LONG_ERROR),
            ("ServiceCollisionToken", AUTHOR_NAME_COLLISION_ERROR),
        ):
            with self.assertRaises(ArchiveItemAuthorError) as raised:
                rename_author(self.author, name=submitted)
            self.assertEqual(raised.exception.message, expected)
        self._assert_nothing_changed()


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class AuthorRenameSuccessTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="author_rename_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.author = Author.objects.create(name="OldAuthorToken")
        self.co_author = Author.objects.create(name="CoAuthorToken")
        self.solo_item = _create_item(title="SoloItem", author_name="OldAuthorToken")
        self.multi_item = _create_item(
            title="MultiItem", author_name="CoAuthorToken, OldAuthorToken"
        )
        self.unrelated_item = _create_item(
            title="UnrelatedItem", author_name="CoAuthorToken"
        )
        _link(self.solo_item, self.author, position=0)
        _link(self.multi_item, self.co_author, position=0)
        _link(self.multi_item, self.author, position=1)
        _link(self.unrelated_item, self.co_author, position=0)
        for item in (self.solo_item, self.multi_item, self.unrelated_item):
            _rebuild(item.pk)
        self.url = _edit_url(self.author)

    def test_rename_updates_author_and_rebuilds_every_author_name(self):
        resp = self.client.post(
            self.url,
            data={"name": "  NewAuthorToken  "},
            follow=True,
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, AUTHOR_NAME_UPDATED_MSG)

        self.author.refresh_from_db()
        self.assertEqual(self.author.name, "NewAuthorToken")
        self.assertEqual(_author_name(self.solo_item), "NewAuthorToken")
        self.assertEqual(_author_name(self.multi_item), "CoAuthorToken, NewAuthorToken")
        self.assertEqual(_author_name(self.unrelated_item), "CoAuthorToken")

        self.co_author.refresh_from_db()
        self.assertEqual(self.co_author.name, "CoAuthorToken")
        self.assertEqual(Author.objects.count(), 2)

    def test_rename_preserves_link_rows_and_positions(self):
        rename_author(self.author, name="PositionSafeToken")
        links = list(
            ArchiveItemAuthor.objects.filter(archive_item=self.multi_item).order_by(
                "position"
            )
        )
        self.assertEqual([link.position for link in links], [0, 1])
        self.assertEqual(
            [link.author_id for link in links],
            [self.co_author.pk, self.author.pk],
        )
        self.assertEqual(ArchiveItemAuthor.objects.count(), 4)

    def test_rename_refreshes_affected_search_indexes(self):
        rename_author(self.author, name="IndexedAuthorToken")

        solo_metadata = _metadata(self.solo_item.pk)
        multi_metadata = _metadata(self.multi_item.pk)
        self.assertIn("IndexedAuthorToken", solo_metadata)
        self.assertNotIn("OldAuthorToken", solo_metadata)
        self.assertIn("IndexedAuthorToken", multi_metadata)
        self.assertNotIn("OldAuthorToken", multi_metadata)
        self.assertNotIn("IndexedAuthorToken", _metadata(self.unrelated_item.pk))

        self.assertEqual(
            sorted(_search_ids("IndexedAuthorToken")),
            sorted([self.solo_item.pk, self.multi_item.pk]),
        )
        self.assertEqual(_search_ids("OldAuthorToken"), [])

    def test_rename_syncs_indexes_with_sorted_affected_ids(self):
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_indexes"
        ) as mocked_sync:
            rename_author(self.author, name="MockedSyncToken")
        mocked_sync.assert_called_once_with(
            sorted([self.solo_item.pk, self.multi_item.pk])
        )

    def test_advanced_author_filter_uses_the_new_name(self):
        rename_author(self.author, name="FilterAuthorToken")
        filters = normalize_archive_advanced_filters({"author": "FilterAuthorToken"})
        matched = filter_archive_items_by_advanced_filters(
            ArchiveItem.objects.all(), filters
        )
        self.assertEqual(
            list(matched.values_list("pk", flat=True)), [self.solo_item.pk]
        )

        stale = normalize_archive_advanced_filters({"author": "OldAuthorToken"})
        self.assertEqual(
            list(
                filter_archive_items_by_advanced_filters(
                    ArchiveItem.objects.all(), stale
                ).values_list("pk", flat=True)
            ),
            [],
        )

    def test_view_redirects_after_success(self):
        resp = self.client.post(self.url, data={"name": "RedirectAuthorToken"})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], self.url)

    def test_rename_to_current_name_is_a_no_op(self):
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_indexes"
        ) as mocked_sync:
            returned = rename_author(self.author, name="  OldAuthorToken  ")
        mocked_sync.assert_not_called()
        self.assertEqual(returned.name, "OldAuthorToken")
        self.assertEqual(_author_name(self.solo_item), "OldAuthorToken")

    def test_rename_with_no_affected_items_skips_index_fan_out(self):
        lonely = Author.objects.create(name="LonelyRenameToken")
        with patch(
            "documents.services.archive_search_index.sync_archive_item_search_indexes"
        ) as mocked_sync:
            rename_author(lonely, name="LonelyRenamedToken")
        mocked_sync.assert_not_called()
        lonely.refresh_from_db()
        self.assertEqual(lonely.name, "LonelyRenamedToken")

    def test_index_failure_rolls_back_the_whole_rename(self):
        with (
            patch(
                "documents.services.archive_search_index."
                "sync_archive_item_search_indexes",
                side_effect=RuntimeError("index boom"),
            ),
            self.assertRaises(RuntimeError),
        ):
            rename_author(self.author, name="RolledBackToken")

        self.author.refresh_from_db()
        self.assertEqual(self.author.name, "OldAuthorToken")
        self.assertEqual(_author_name(self.solo_item), "OldAuthorToken")
        self.assertEqual(_author_name(self.multi_item), "CoAuthorToken, OldAuthorToken")
        self.assertIn("OldAuthorToken", _metadata(self.solo_item.pk))
        self.assertNotIn("RolledBackToken", _metadata(self.solo_item.pk))

    def test_rename_rebuilds_author_name_that_had_drifted(self):
        ArchiveItem.objects.filter(pk=self.multi_item.pk).update(
            author_name="stale drifted value"
        )
        rename_author(self.author, name="DriftFixedToken")
        self.assertEqual(
            _author_name(self.multi_item), "CoAuthorToken, DriftFixedToken"
        )

    def test_rename_locks_items_before_authors_and_locks_coauthors(self):
        item_table = ArchiveItem._meta.db_table
        author_table = Author._meta.db_table
        with CaptureQueriesContext(connection) as ctx:
            rename_author(self.author, name="LockOrderToken")
        for_update = [
            query["sql"]
            for query in ctx.captured_queries
            if "FOR UPDATE" in query["sql"].upper()
        ]
        item_lock_indexes = [
            index for index, sql in enumerate(for_update) if item_table in sql
        ]
        author_lock_indexes = [
            index for index, sql in enumerate(for_update) if author_table in sql
        ]
        self.assertTrue(item_lock_indexes)
        self.assertTrue(author_lock_indexes)
        self.assertLess(min(item_lock_indexes), min(author_lock_indexes))
        author_lock_sql = " ".join(
            sql for index, sql in enumerate(for_update) if index in author_lock_indexes
        )
        self.assertIn(str(self.author.pk), author_lock_sql)
        self.assertIn(str(self.co_author.pk), author_lock_sql)

    def test_rename_fails_closed_when_a_new_item_is_linked_after_author_locks(self):
        from documents.services import archive_item_authors as authors_mod

        late_item = _create_item(title="LateLinkedItem", author_name="OldAuthorToken")
        real_lock_authors = authors_mod._lock_authors_for_update

        def lock_then_attach(**kwargs):
            locked = real_lock_authors(**kwargs)
            _link(late_item, self.author, position=0)
            return locked

        with patch.object(
            authors_mod, "_lock_authors_for_update", side_effect=lock_then_attach
        ):
            with self.assertRaises(ArchiveItemAuthorError) as raised:
                rename_author(self.author, name="LateLinkToken")
        self.assertEqual(raised.exception.message, AUTHOR_LINKS_CHANGED_RETRY_ERROR)
        self.author.refresh_from_db()
        self.assertEqual(self.author.name, "OldAuthorToken")
        self.assertEqual(_author_name(self.solo_item), "OldAuthorToken")
        self.assertEqual(_author_name(self.multi_item), "CoAuthorToken, OldAuthorToken")
        self.assertEqual(_author_name(late_item), "OldAuthorToken")
        self.assertNotIn("LateLinkToken", _metadata(self.solo_item.pk))


class AuthorRenamePersonIsolationTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="author_isolation_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.author = Author.objects.create(name="IsolationAuthorToken")
        self.item = _create_item(
            title="IsolationItem", author_name="IsolationAuthorToken"
        )
        _link(self.item, self.author, position=0)
        self.person = Person.objects.create(name="IsolationAuthorToken")
        PersonAlias.objects.create(person=self.person, name="IsolationAliasToken")
        ArchiveItemPerson.objects.create(archive_item=self.item, person=self.person)

    def test_rename_does_not_touch_person_rows(self):
        before = (
            Person.objects.count(),
            PersonAlias.objects.count(),
            ArchiveItemPerson.objects.count(),
            PhotoPerson.objects.count(),
        )
        self.client.post(
            _edit_url(self.author),
            data={"name": "IsolationRenamedToken"},
        )
        self.assertEqual(
            (
                Person.objects.count(),
                PersonAlias.objects.count(),
                ArchiveItemPerson.objects.count(),
                PhotoPerson.objects.count(),
            ),
            before,
        )
        self.person.refresh_from_db()
        self.assertEqual(self.person.name, "IsolationAuthorToken")
        self.author.refresh_from_db()
        self.assertEqual(self.author.name, "IsolationRenamedToken")

    def test_person_with_matching_name_is_not_a_collision(self):
        renamed = rename_author(self.author, name="IsolationRenamedToken")
        self.assertEqual(renamed.name, "IsolationRenamedToken")


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class AuthorEditLinkOnItemFormTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="author_link_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_manual_text_edit_page_links_to_author_edit(self):
        item = create_manual_text_archive_item(
            title="LinkedAuthorItem",
            body="body",
            visibility=ArchiveItem.Visibility.PUBLIC,
            staff_author_ids=[],
            new_author_name="LinkedAuthorToken",
        )
        author = ordered_authors(item)[0]
        resp = self.client.get(
            reverse("archive-manage-edit", kwargs={"item_id": item.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, _edit_url(author))
        self.assertContains(resp, "עריכת מחבר/ת")

    def test_ocr_edit_page_links_to_author_edit(self):
        doc = create_ocr_document(
            title="LinkedOcrAuthorItem",
            doc_type=Document.DocType.IMAGE,
            text_input_type=Document.TextInputType.HANDWRITTEN,
            visibility=ArchiveItem.Visibility.PUBLIC,
            staff_author_ids=[],
            new_author_name="LinkedOcrAuthorToken",
        )
        item = doc.archive_item
        author = ordered_authors(item)[0]
        resp = self.client.get(
            reverse("archive-manage-edit", kwargs={"item_id": item.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, _edit_url(author))
        self.assertContains(resp, "עריכת מחבר/ת")

    def test_video_edit_page_links_to_author_edit(self):
        item = create_video_archive_item(
            title="LinkedVideoAuthorItem",
            source_url=YOUTUBE_URL,
            visibility=ArchiveItem.Visibility.PUBLIC,
            staff_author_ids=[],
            new_author_name="LinkedVideoAuthorToken",
        )
        author = ordered_authors(item)[0]
        resp = self.client.get(
            reverse("archive-manage-edit", kwargs={"item_id": item.id})
        )
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, _edit_url(author))
        self.assertContains(resp, "עריכת מחבר/ת")

    def test_photo_pages_have_no_author_controls_or_author_edit_link(self):
        photo_item = ArchiveItem.objects.create(
            item_type=ArchiveItem.ItemType.PHOTO,
            title="PhotoNoAuthors",
            visibility=ArchiveItem.Visibility.PUBLIC,
        )
        create_resp = self.client.get(
            reverse("archive-manage-new"), {"item_type": "photo"}
        )
        edit_resp = self.client.get(
            reverse("archive-manage-edit", kwargs={"item_id": photo_item.id})
        )
        for resp in (create_resp, edit_resp):
            self.assertEqual(resp.status_code, 200)
            self.assertNotContains(resp, "עריכת מחבר/ת")
            self.assertNotContains(resp, 'name="new_author_name"')
