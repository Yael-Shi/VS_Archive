"""Public Author page: ArchiveItemAuthor membership and browse cards."""

from __future__ import annotations

from datetime import timedelta

from django.contrib.auth.models import Group, Permission, User
from django.contrib.contenttypes.models import ContentType
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from documents.models import (
    ArchiveItem,
    ArchiveItemAuthor,
    ArchiveItemPerson,
    Author,
    Person,
    PhotoContent,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
)
from documents.services.archive_item_presentation import (
    ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE,
    person_public_page_url,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.author_public import author_public_page_url
from documents.services.photo_gallery import public_photo_detail_url


def _public_manual(title: str) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="Public body",
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _private_manual(title: str) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="Private body",
        visibility=ArchiveItem.Visibility.PRIVATE,
    )


def _restricted_manual(title: str) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body="Restricted body",
        visibility=ArchiveItem.Visibility.RESTRICTED,
    )


def _link(item: ArchiveItem, author: Author, *, position: int = 0) -> None:
    ArchiveItemAuthor.objects.create(
        archive_item=item, author=author, position=position
    )


def _author_page(author: Author) -> str:
    return reverse("archive-author-detail", kwargs={"author_id": author.id})


def _grant_restricted_permission(user: User) -> User:
    ct = ContentType.objects.get_for_model(ArchiveItem)
    perm = Permission.objects.get(
        codename=VIEW_RESTRICTED_ARCHIVEITEM_CODENAME,
        content_type=ct,
    )
    user.user_permissions.add(perm)
    if hasattr(user, "_perm_cache"):
        delattr(user, "_perm_cache")
    if hasattr(user, "_user_perm_cache"):
        delattr(user, "_user_perm_cache")
    return user


def _create_photo_item(
    *,
    title: str,
    visibility: str = ArchiveItem.Visibility.PUBLIC,
) -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=visibility,
    )


def _add_photo(
    item: ArchiveItem,
    *,
    position: int = 1,
    uploaded: bool = True,
    failed: bool = False,
) -> PhotoContent:
    if failed:
        status = PhotoContent.UploadStatus.FAILED
    elif uploaded:
        status = PhotoContent.UploadStatus.UPLOADED
    else:
        status = PhotoContent.UploadStatus.PENDING
    photo = PhotoContent.objects.create(
        archive_item=item,
        position=position,
        original_file_key="",
        original_filename="photo.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=status,
        upload_error="",
    )
    if uploaded and not failed:
        photo.original_file_key = f"photos/{photo.id}/original.jpg"
        photo.save(update_fields=["original_file_key", "updated_at"])
    return photo


def _titles(response) -> set[str]:
    return {item.title for item in response.context["items"]}


def _card_urls(response) -> list[str]:
    return [card.detail_url for card in response.context["browse_cards"]]


class AuthorPublicPageUrlTests(TestCase):
    def test_helper_and_named_route_match(self):
        author = Author.objects.create(name="URL Author")
        self.assertEqual(
            author_public_page_url(author.id),
            f"/archive/authors/{author.id}/",
        )
        self.assertEqual(author_public_page_url(author.id), _author_page(author))


class AuthorPublicPageAuthorizedTests(TestCase):
    def test_authorized_page_shows_h1_count_and_cards(self):
        author = Author.objects.create(name="Ada Lovelace")
        first = _public_manual("Ada public letter")
        second = _public_manual("Ada public note")
        _link(first, author)
        _link(second, author)

        resp = self.client.get(_author_page(author))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "<h1", html=False)
        self.assertContains(resp, "Ada Lovelace")
        self.assertContains(resp, "פריטים קשורים")
        self.assertContains(resp, "נמצאו 2 תוצאות")
        self.assertEqual(resp.context["total_count"], 2)
        self.assertEqual(_titles(resp), {"Ada public letter", "Ada public note"})
        self.assertContains(resp, "Ada public letter")
        self.assertContains(resp, "Ada public note")
        self.assertContains(resp, "חזרה לארכיון")
        html = resp.content.decode("utf-8")
        self.assertNotIn("עריכת מחבר", html)
        self.assertNotIn(
            reverse("archive-manage-author-edit", kwargs={"author_id": author.id}),
            html,
        )
        self.assertEqual(
            set(_card_urls(resp)),
            {
                reverse("archive-detail", kwargs={"item_id": first.id}),
                reverse("archive-detail", kwargs={"item_id": second.id}),
            },
        )

    def test_duplicate_names_use_distinct_id_urls(self):
        first = Author.objects.create(name="Same Name")
        second = Author.objects.create(name="Same Name")
        _link(_public_manual("First same-name item"), first)
        _link(_public_manual("Second same-name item"), second)

        first_resp = self.client.get(_author_page(first))
        second_resp = self.client.get(_author_page(second))
        self.assertEqual(first_resp.status_code, 200)
        self.assertEqual(second_resp.status_code, 200)
        self.assertNotEqual(_author_page(first), _author_page(second))
        self.assertEqual(_titles(first_resp), {"First same-name item"})
        self.assertEqual(_titles(second_resp), {"Second same-name item"})

    def test_missing_author_is_404(self):
        resp = self.client.get(
            reverse("archive-author-detail", kwargs={"author_id": 999999})
        )
        self.assertEqual(resp.status_code, 404)

    def test_unlinked_author_is_404(self):
        author = Author.objects.create(name="Unlinked Author")
        resp = self.client.get(_author_page(author))
        self.assertEqual(resp.status_code, 404)


class AuthorPublicPageVisibilityTests(TestCase):
    def setUp(self):
        self.author = Author.objects.create(name="Visibility Author")
        self.public = _public_manual("AUTHOR-PUBLIC-TITLE")
        self.private = _private_manual("AUTHOR-PRIVATE-TITLE")
        self.restricted = _restricted_manual("AUTHOR-RESTRICTED-TITLE")
        _link(self.public, self.author, position=0)
        _link(self.private, self.author, position=0)
        _link(self.restricted, self.author, position=0)
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        self.family = User.objects.create_user(
            username="author-page-family", password="x"
        )
        self.family.groups.add(family_group)
        self.restricted_user = _grant_restricted_permission(
            User.objects.create_user(username="author-page-restricted", password="x")
        )

    def test_anonymous_sees_public_count_only(self):
        resp = self.client.get(_author_page(self.author))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 1)
        self.assertEqual(_titles(resp), {"AUTHOR-PUBLIC-TITLE"})
        html = resp.content.decode("utf-8")
        self.assertIn("AUTHOR-PUBLIC-TITLE", html)
        self.assertNotIn("AUTHOR-PRIVATE-TITLE", html)
        self.assertNotIn("AUTHOR-RESTRICTED-TITLE", html)
        self.assertContains(resp, "נמצאו 1 תוצאות")

    def test_family_sees_private_not_restricted(self):
        self.client.force_login(self.family)
        resp = self.client.get(_author_page(self.author))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 2)
        self.assertEqual(_titles(resp), {"AUTHOR-PUBLIC-TITLE", "AUTHOR-PRIVATE-TITLE"})
        self.assertNotIn("AUTHOR-RESTRICTED-TITLE", _titles(resp))

    def test_restricted_user_sees_restricted_not_private(self):
        self.client.force_login(self.restricted_user)
        resp = self.client.get(_author_page(self.author))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 2)
        self.assertEqual(
            _titles(resp), {"AUTHOR-PUBLIC-TITLE", "AUTHOR-RESTRICTED-TITLE"}
        )
        self.assertNotIn("AUTHOR-PRIVATE-TITLE", _titles(resp))

    def test_private_only_is_404_for_anonymous(self):
        author = Author.objects.create(name="Private Only Author")
        _link(_private_manual("Hidden private letter"), author)
        resp = self.client.get(_author_page(author))
        self.assertEqual(resp.status_code, 404)

    def test_restricted_only_is_404_for_anonymous_and_family(self):
        author = Author.objects.create(name="Restricted Only Author")
        _link(_restricted_manual("Hidden restricted letter"), author)
        anon = self.client.get(_author_page(author))
        self.assertEqual(anon.status_code, 404)
        self.client.force_login(self.family)
        family = self.client.get(_author_page(author))
        self.assertEqual(family.status_code, 404)


class AuthorPublicPageMembershipTests(TestCase):
    def test_author_name_only_item_is_not_listed(self):
        author = Author.objects.create(name="Name Only Author")
        linked = _public_manual("Linked letter")
        _link(linked, author)
        name_only = _public_manual("Name only letter")
        name_only.author_name = author.name
        name_only.save(update_fields=["author_name"])

        resp = self.client.get(_author_page(author))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.context["total_count"], 1)
        self.assertEqual(_titles(resp), {"Linked letter"})
        self.assertNotIn("Name only letter", _titles(resp))

    def test_author_name_only_without_link_is_404(self):
        author = Author.objects.create(name="Unlinked Name Author")
        item = _public_manual("Name only inaccessible")
        item.author_name = author.name
        item.save(update_fields=["author_name"])
        self.assertEqual(self.client.get(_author_page(author)).status_code, 404)

    def test_no_person_inference_on_detail(self):
        author = Author.objects.create(name="Shared Token")
        person = Person.objects.create(name="Shared Token")
        person_item = _public_manual("Person only letter")
        ArchiveItemPerson.objects.create(archive_item=person_item, person=person)
        self.assertEqual(self.client.get(_author_page(author)).status_code, 404)

        author_item = _public_manual("Author letter")
        _link(author_item, author)
        resp = self.client.get(_author_page(author))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(_titles(resp), {"Author letter"})
        self.assertNotIn("Person only letter", _titles(resp))

    def test_pending_photo_only_is_404_and_renderable_photo_uses_normal_url(self):
        pending_author = Author.objects.create(name="Pending Photo Author")
        pending_item = _create_photo_item(title="Pending album")
        _add_photo(pending_item, uploaded=False)
        _link(pending_item, pending_author)
        self.assertEqual(
            self.client.get(_author_page(pending_author)).status_code, 404
        )

        author = Author.objects.create(name="Renderable Photo Author")
        item = _create_photo_item(title="Renderable album")
        photo = _add_photo(item)
        _link(item, author)
        resp = self.client.get(_author_page(author))
        self.assertEqual(resp.status_code, 200)
        expected = reverse("archive-detail", kwargs={"item_id": item.id})
        self.assertEqual(_card_urls(resp), [expected])
        self.assertNotIn("photo=", _card_urls(resp)[0])
        self.assertNotIn(public_photo_detail_url(item.id, photo.id), _card_urls(resp))

    def test_coauthors_do_not_duplicate_or_leak_other_items(self):
        item = _public_manual("Shared letter")
        other = _public_manual("Other letter")
        first = Author.objects.create(name="Coauthor A")
        second = Author.objects.create(name="Coauthor B")
        _link(item, first, position=0)
        _link(item, second, position=1)
        _link(other, first, position=0)

        first_resp = self.client.get(_author_page(first))
        second_resp = self.client.get(_author_page(second))
        self.assertEqual(first_resp.context["total_count"], 2)
        self.assertEqual(second_resp.context["total_count"], 1)
        self.assertEqual(_titles(first_resp), {"Shared letter", "Other letter"})
        self.assertEqual(_titles(second_resp), {"Shared letter"})

    def test_order_is_created_at_then_pk(self):
        author = Author.objects.create(name="Ordered Author")
        older = _public_manual("Older letter")
        newer = _public_manual("Newer letter")
        now = timezone.now()
        ArchiveItem.objects.filter(pk=older.pk).update(
            created_at=now - timedelta(days=1)
        )
        ArchiveItem.objects.filter(pk=newer.pk).update(created_at=now)
        _link(older, author)
        _link(newer, author)
        resp = self.client.get(_author_page(author))
        self.assertEqual(
            [item.title for item in resp.context["items"]],
            ["Newer letter", "Older letter"],
        )


class AuthorPublicPagePaginationTests(TestCase):
    def test_paginates_at_fixed_48_and_clamps_out_of_range_pages(self):
        author = Author.objects.create(name="Paged Author")
        items = []
        for index in range(ARCHIVE_PUBLIC_LIST_DEFAULT_PER_PAGE + 1):
            item = _public_manual(f"AUTHORPAGE-{index:02d}")
            _link(item, author)
            items.append(item)

        page1 = self.client.get(_author_page(author))
        self.assertEqual(page1.status_code, 200)
        self.assertEqual(page1.context["total_count"], 49)
        self.assertEqual(len(page1.context["items"]), 48)
        self.assertEqual(len(page1.context["browse_cards"]), 48)
        self.assertTrue(page1.context["show_page_nav"])
        self.assertContains(page1, "נמצאו 49 תוצאות")
        self.assertContains(page1, "?page=2")
        html1 = page1.content.decode("utf-8")
        self.assertNotIn('name="per_page"', html1)
        self.assertNotIn("archive-type-filter", html1)
        self.assertNotIn("archive-search-form", html1)

        page2 = self.client.get(_author_page(author), {"page": "2"})
        self.assertEqual(page2.status_code, 200)
        self.assertEqual(len(page2.context["items"]), 1)
        self.assertContains(page2, "הקודם")
        self.assertContains(
            page2,
            f'class="archive-list-pagination__nav-link" href="{_author_page(author)}"',
        )
        self.assertEqual(
            {item.pk for item in page1.context["items"]}
            | {item.pk for item in page2.context["items"]},
            {item.pk for item in items},
        )
        self.assertFalse(
            {item.pk for item in page1.context["items"]}
            & {item.pk for item in page2.context["items"]}
        )

        clamped = self.client.get(_author_page(author), {"page": "999"})
        self.assertEqual(clamped.status_code, 200)
        self.assertEqual(clamped.context["page"], 2)
        self.assertEqual(
            [item.pk for item in clamped.context["items"]],
            [item.pk for item in page2.context["items"]],
        )

        invalid = self.client.get(_author_page(author), {"page": "abc"})
        self.assertEqual(invalid.status_code, 200)
        self.assertEqual(invalid.context["page"], 1)
        self.assertEqual(
            [item.pk for item in invalid.context["items"]],
            [item.pk for item in page1.context["items"]],
        )

        ignored = self.client.get(
            _author_page(author),
            {"per_page": "24", "q": "ignored", "item_type": "photo"},
        )
        self.assertEqual(ignored.status_code, 200)
        self.assertEqual(len(ignored.context["items"]), 48)
        self.assertEqual(ignored.context["per_page"], 48)

    def test_single_page_omits_page_nav(self):
        author = Author.objects.create(name="Single Page Author")
        _link(_public_manual("Only letter"), author)
        resp = self.client.get(_author_page(author))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.context["show_page_nav"])
        self.assertNotContains(resp, "archive-list-pagination")


class AuthorPublicPageLinkedPersonRedirectTests(TestCase):
    def test_linked_author_private_only_is_404_even_if_person_is_independently_public(
        self,
    ):
        person = Person.objects.create(name="Independently Public Person")
        ArchiveItemPerson.objects.create(
            archive_item=_public_manual("Public AIP letter"), person=person
        )
        author = Author.objects.create(name="Private Only Linked Author", person=person)
        _link(_private_manual("Hidden authored letter"), author)

        resp = self.client.get(_author_page(author))
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.has_header("Location"))

        person_resp = self.client.get(
            reverse("archive-person-detail", kwargs={"person_id": person.id})
        )
        self.assertEqual(person_resp.status_code, 200)

    def test_linked_author_redirects_to_person_detail(self):
        person = Person.objects.create(name="Canonical Linked Person")
        author = Author.objects.create(name="Linked Bibliographic Author", person=person)
        _link(_public_manual("Linked authored letter"), author)

        resp = self.client.get(_author_page(author))
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], person_public_page_url(person.id))
        followed = self.client.get(resp["Location"])
        self.assertEqual(followed.status_code, 200)
        self.assertContains(
            followed,
            '<h1 class="page-title">Canonical Linked Person</h1>',
            html=True,
        )
        self.assertContains(followed, "Linked Bibliographic Author")
        person_href = person_public_page_url(person.id)
        self.assertContains(
            followed,
            f'<a href="{person_href}">Linked Bibliographic Author</a>',
            html=True,
        )
        self.assertNotContains(followed, author_public_page_url(author.id))

    def test_family_user_gets_redirect_for_private_linked_author(self):
        person = Person.objects.create(name="Family Linked Person")
        author = Author.objects.create(name="Family Linked Author", person=person)
        _link(_private_manual("Family authored letter"), author)
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        family = User.objects.create_user(
            username="linked-author-family-redirect", password="x"
        )
        family.groups.add(family_group)

        anon = self.client.get(_author_page(author))
        self.assertEqual(anon.status_code, 404)

        self.client.force_login(family)
        family_resp = self.client.get(_author_page(author))
        self.assertEqual(family_resp.status_code, 302)
        self.assertEqual(family_resp["Location"], person_public_page_url(person.id))
        followed = self.client.get(family_resp["Location"])
        self.assertEqual(followed.status_code, 200)
        self.assertContains(followed, "Family Linked Person")

    def test_unlinked_author_keeps_author_detail(self):
        author = Author.objects.create(name="Still Unlinked Author")
        _link(_public_manual("Unlinked authored letter"), author)
        resp = self.client.get(_author_page(author))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Still Unlinked Author")
        self.assertEqual(_titles(resp), {"Unlinked authored letter"})

    def test_linked_author_without_visible_membership_is_404(self):
        person = Person.objects.create(name="Hidden Linked Person")
        author = Author.objects.create(name="Hidden Linked Author", person=person)
        _link(_private_manual("Hidden authored letter"), author)
        resp = self.client.get(_author_page(author))
        self.assertEqual(resp.status_code, 404)
        self.assertFalse(resp.has_header("Location"))
