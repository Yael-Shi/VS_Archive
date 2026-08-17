"""PR4: public multi-photo gallery on PHOTO ArchiveItem detail."""

from __future__ import annotations

from datetime import date
from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import SimpleTestCase, TestCase, override_settings
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    ArchiveItemPerson,
    Person,
    PersonAlias,
    PhotoContent,
    PhotoPerson,
    Tag,
)
from documents.services.archive_item_access import (
    ARCHIVE_FAMILY_GROUP_NAME,
    get_viewable_archive_item,
)
from documents.services.photo_gallery import (
    PUBLIC_PHOTO_QUERY_PARAM,
    build_public_photo_gallery,
    identified_people_display_names,
    parse_public_photo_selector,
    public_photo_alt_text,
    public_photo_detail_url,
    public_renderable_photo_contents,
)


def _people_sql(captured_queries) -> list[str]:
    return [
        query["sql"]
        for query in captured_queries
        if "documents_photoperson" in query["sql"].lower()
        or (
            "documents_person" in query["sql"].lower()
            and "documents_photoperson" not in query["sql"].lower()
        )
    ]


def _create_photo_item(
    *, title: str, visibility=ArchiveItem.Visibility.PUBLIC
) -> ArchiveItem:
    return ArchiveItem.objects.create(
        item_type=ArchiveItem.ItemType.PHOTO,
        title=title,
        visibility=visibility,
        public_note="Shared public note",
    )


def _add_photo(
    item: ArchiveItem,
    *,
    position: int,
    filename: str = "photo.jpg",
    description: str = "",
    location: str = "",
    context: str = "",
    people_present: str = "",
    notes: str = "",
    upload_status=PhotoContent.UploadStatus.UPLOADED,
    original_file_key: str | None = None,
    thumbnail_file_key: str = "",
    date_start=None,
    date_end=None,
    date_precision=ArchiveItem.DatePrecision.UNKNOWN,
) -> PhotoContent:
    resolved_key = original_file_key if original_file_key is not None else "pending-key"
    photo = PhotoContent.objects.create(
        archive_item=item,
        position=position,
        original_file_key=resolved_key,
        original_filename=filename,
        original_mime_type="image/jpeg",
        original_size_bytes=1024,
        upload_status=upload_status,
        upload_error="",
        description=description,
        location=location,
        context=context,
        people_present=people_present,
        notes=notes,
        thumbnail_file_key=thumbnail_file_key,
        thumbnail_mime_type="image/jpeg" if thumbnail_file_key else "",
        thumbnail_size_bytes=256 if thumbnail_file_key else None,
        date_start=date_start,
        date_end=date_end,
        date_precision=date_precision,
    )
    if (
        original_file_key is None
        and upload_status == PhotoContent.UploadStatus.UPLOADED
    ):
        photo.original_file_key = f"photos/{photo.id}/original.jpg"
        photo.save(update_fields=["original_file_key", "updated_at"])
    return photo


def _presign_url(*, bucket: str, key: str, expires_in: int = 3600) -> str:
    _ = bucket, expires_in
    kind = "thumb" if "thumb" in key else "orig"
    token = format(sum((index + 1) * ord(char) for index, char in enumerate(key)), "x")
    return f"https://s3.example/presigned/{kind}-{token}"


class PublicPhotoSelectorUnitTests(SimpleTestCase):
    def test_parse_public_photo_selector_accepts_positive_ids(self):
        self.assertEqual(parse_public_photo_selector("12"), 12)
        self.assertIsNone(parse_public_photo_selector(None))
        self.assertIsNone(parse_public_photo_selector(""))
        self.assertIsNone(parse_public_photo_selector("abc"))
        self.assertIsNone(parse_public_photo_selector("0"))
        self.assertIsNone(parse_public_photo_selector("-3"))

    def test_alt_text_prefers_description_and_omits_ids(self):
        photo = PhotoContent(description="Picnic", id=99)
        self.assertEqual(
            public_photo_alt_text(
                photo,
                item_title="Album",
                display_index=2,
                total=5,
            ),
            "Picnic",
        )
        photo.description = ""
        alt = public_photo_alt_text(
            photo,
            item_title="Album",
            display_index=2,
            total=5,
        )
        self.assertEqual(alt, "Album — תמונה 2 מתוך 5")
        self.assertNotIn("99", alt)


@override_settings(UPLOADS_BUCKET_NAME="test-uploads-bucket")
class PhotoPublicGalleryTests(TestCase):
    def setUp(self):
        self.family_group, _ = Group.objects.get_or_create(
            name=ARCHIVE_FAMILY_GROUP_NAME
        )
        self.family_user = User.objects.create_user(
            username="gallery_family",
            password="test-pass",
        )
        self.family_user.groups.add(self.family_group)
        self.staff = User.objects.create_user(
            username="gallery_staff",
            password="test-pass",
            is_staff=True,
        )
        self.item = _create_photo_item(title="Family album")
        self.p1 = _add_photo(
            self.item,
            position=1,
            filename="one.jpg",
            description="First picnic",
            location="Jerusalem",
            people_present="someone in the back",
            thumbnail_file_key="photos/p1/thumb_400.jpg",
            date_start=date(1950, 1, 1),
            date_end=date(1950, 12, 31),
            date_precision=ArchiveItem.DatePrecision.YEAR,
        )
        self.p2 = _add_photo(
            self.item,
            position=2,
            filename="two.jpg",
            description="Second outing",
            context="After the ceremony",
            notes="From box 4",
            thumbnail_file_key="photos/p2/thumb_400.jpg",
            date_start=date(1951, 6, 1),
            date_end=date(1951, 6, 30),
            date_precision=ArchiveItem.DatePrecision.MONTH,
        )
        self.p3 = _add_photo(
            self.item,
            position=3,
            filename="three.jpg",
            description="Third gathering",
            thumbnail_file_key="",
        )
        self.pending = _add_photo(
            self.item,
            position=4,
            filename="pending.jpg",
            description="Pending should stay hidden",
            upload_status=PhotoContent.UploadStatus.PENDING,
            original_file_key="",
        )
        self.failed = _add_photo(
            self.item,
            position=5,
            filename="failed.jpg",
            description="Failed should stay hidden",
            upload_status=PhotoContent.UploadStatus.FAILED,
            original_file_key="photos/failed/original.jpg",
        )
        self.empty_key = _add_photo(
            self.item,
            position=6,
            filename="empty.jpg",
            description="Empty key should stay hidden",
            upload_status=PhotoContent.UploadStatus.UPLOADED,
            original_file_key="",
        )
        self.ada = Person.objects.create(name="Ada")
        self.rivka = Person.objects.create(name="Rivka")
        PhotoPerson.objects.create(photo_content=self.p1, person=self.rivka)
        PhotoPerson.objects.create(photo_content=self.p1, person=self.ada)
        PhotoPerson.objects.create(photo_content=self.p2, person=self.rivka)
        self.category = ArchiveCategory.objects.create(
            name="Gallery category",
            slug="gallery-category",
        )
        self.event = ArchiveEvent.objects.create(
            name="Gallery event",
            slug="gallery-event",
        )
        self.tag = Tag.objects.create(name="gallery-tag")
        self.item.categories.add(self.category)
        self.item.events.add(self.event)
        self.item.tags.add(self.tag)

        self.other_item = _create_photo_item(title="Other album")
        self.other_photo = _add_photo(
            self.other_item,
            position=1,
            filename="other.jpg",
            description="Foreign photo description",
        )

        self.presign_view = patch(
            "documents.views.create_presigned_get",
            side_effect=_presign_url,
        )
        self.presign_thumbs = patch(
            "documents.services.photo_archive_urls.create_presigned_get",
            side_effect=_presign_url,
        )
        self.presign_view.start()
        self.presign_thumbs.start()
        self.addCleanup(self.presign_view.stop)
        self.addCleanup(self.presign_thumbs.stop)

    def _detail(self, item=None, photo=None):
        item = item or self.item
        url = reverse("archive-detail", kwargs={"item_id": item.id})
        if photo is not None:
            url = f"{url}?{PUBLIC_PHOTO_QUERY_PARAM}={photo}"
        return self.client.get(url)

    def test_one_photo_detail_stays_simple(self):
        single = _create_photo_item(title="Single photo item")
        photo = _add_photo(
            single,
            position=1,
            description="Only photo caption",
            location="Haifa",
        )
        resp = self._detail(item=single)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Single photo item")
        self.assertContains(resp, "Only photo caption")
        self.assertContains(resp, "Haifa")
        self.assertContains(resp, 'class="photo-detail__image"')
        self.assertContains(resp, _presign_url(bucket="x", key=photo.original_file_key))
        self.assertNotContains(resp, "photo-gallery")
        self.assertNotContains(resp, "הקודמת")
        self.assertNotContains(resp, "הבאה")
        self.assertNotContains(resp, "1 מתוך 1")
        self.assertNotContains(resp, "photo-gallery__nav")
        self.assertContains(resp, "חזרה לארכיון")
        self.assertContains(resp, "הוספת מידע על הפריט")

    def test_multi_photo_renders_renderable_photos_in_position_id_order(self):
        resp = self._detail()
        self.assertEqual(resp.status_code, 200)
        html = resp.content.decode("utf-8")
        self.assertContains(resp, "photo-gallery")
        self.assertContains(resp, "1 מתוך 3")
        self.assertContains(resp, "הבאה")
        self.assertNotContains(resp, ">הקודמת</a>")
        thumbs_start = html.index("photo-gallery__thumbs")
        thumbs_html = html[thumbs_start:]
        first_href = public_photo_detail_url(self.item.id, self.p1.id)
        second_href = public_photo_detail_url(self.item.id, self.p2.id)
        third_href = public_photo_detail_url(self.item.id, self.p3.id)
        self.assertLess(thumbs_html.index(first_href), thumbs_html.index(second_href))
        self.assertLess(thumbs_html.index(second_href), thumbs_html.index(third_href))
        self.assertContains(resp, "First picnic")
        self.assertNotContains(resp, "Second outing")
        self.assertNotContains(resp, "Pending should stay hidden")
        self.assertNotContains(resp, "Failed should stay hidden")
        self.assertNotContains(resp, "Empty key should stay hidden")

    def test_selected_photo_can_be_changed_and_prev_next_resolve(self):
        resp = self._detail(photo=self.p2.id)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "2 מתוך 3")
        self.assertContains(resp, "Second outing")
        self.assertContains(resp, "After the ceremony")
        self.assertContains(resp, "From box 4")
        self.assertNotContains(resp, "First picnic")
        self.assertNotContains(resp, "Third gathering")
        self.assertContains(resp, public_photo_detail_url(self.item.id, self.p1.id))
        self.assertContains(resp, public_photo_detail_url(self.item.id, self.p3.id))
        self.assertContains(resp, ">הקודמת</a>")
        self.assertContains(resp, ">הבאה</a>")
        self.assertContains(
            resp, _presign_url(bucket="x", key=self.p2.original_file_key)
        )
        self.assertNotContains(
            resp,
            _presign_url(bucket="x", key=self.p1.original_file_key),
        )

        last = self._detail(photo=self.p3.id)
        self.assertContains(last, "3 מתוך 3")
        self.assertContains(last, ">הקודמת</a>")
        self.assertNotContains(last, ">הבאה</a>")
        self.assertContains(last, "photo-gallery__thumb-fallback")

    def test_shared_metadata_shown_once_and_stays_in_header(self):
        resp = self._detail(photo=self.p2.id)
        html = resp.content.decode("utf-8")
        self.assertContains(resp, "Family album")
        self.assertEqual(html.count("Shared public note"), 1)
        self.assertEqual(html.count("Gallery category"), 1)
        self.assertEqual(html.count("Gallery event"), 1)
        self.assertEqual(html.count("gallery-tag"), 1)
        header = html[
            html.index("archive-detail-photo-header") : html.index("</header>")
        ]
        self.assertIn("Gallery category", header)
        self.assertNotIn("Second outing", header)
        self.assertIn("Second outing", html)

    def test_public_detail_shows_canonical_person_names_not_aliases(self):
        PersonAlias.objects.create(person=self.ada, name="Ada Lovelace")
        PersonAlias.objects.create(person=self.rivka, name="Rivka Cohen")
        resp = self._detail()
        self.assertContains(resp, "Ada, Rivka")
        self.assertNotContains(resp, "Ada Lovelace")
        self.assertNotContains(resp, "Rivka Cohen")
        self.assertEqual(
            identified_people_display_names(self.p1),
            ["Ada", "Rivka"],
        )

        url = reverse("archive-detail", kwargs={"item_id": self.item.id})
        with CaptureQueriesContext(connection) as ctx:
            detail = self.client.get(url)
        self.assertEqual(detail.status_code, 200)
        alias_sql = [
            query["sql"]
            for query in ctx.captured_queries
            if "documents_personalias" in query["sql"].lower()
        ]
        self.assertEqual(alias_sql, [])

    def test_identified_people_and_people_present_stay_separate(self):
        resp = self._detail()
        self.assertContains(resp, "אנשים מזוהים:")
        self.assertContains(resp, "Ada, Rivka")
        self.assertContains(resp, "נוכחים:")
        self.assertContains(resp, "someone in the back")
        html = resp.content.decode("utf-8")
        identified = html[html.index("אנשים מזוהים:") : html.index("נוכחים:")]
        self.assertIn("Ada", identified)
        self.assertNotIn("someone in the back", identified)
        identified_value = identified.split("</span>", 1)[0]
        self.assertNotIn("person", identified_value.lower())
        self.assertNotIn(str(self.p1.id), identified_value)

        second = self._detail(photo=self.p2.id)
        self.assertContains(second, "אנשים מזוהים:")
        self.assertContains(second, "Rivka")
        self.assertNotContains(second, "Ada, Rivka")
        self.assertNotContains(second, "someone in the back")

    def test_item_level_person_is_not_shown_as_photo_identity(self):
        outsider = Person.objects.create(name="Item-only person")
        ArchiveItemPerson.objects.create(archive_item=self.item, person=outsider)
        resp = self._detail()
        self.assertNotContains(resp, "Item-only person")

    def test_per_photo_dates_render_with_archive_formatting(self):
        resp = self._detail()
        self.assertContains(resp, "תאריך התמונה:")
        self.assertContains(resp, "1950")
        second = self._detail(photo=self.p2.id)
        self.assertContains(second, "06/1951")
        self.assertNotContains(second, "1950")
        third = self._detail(photo=self.p3.id)
        self.assertNotContains(third, "תאריך התמונה:")

    def test_pending_failed_and_empty_key_are_excluded_from_gallery(self):
        photos = public_renderable_photo_contents(self.item)
        self.assertEqual(
            [photo.id for photo in photos], [self.p1.id, self.p2.id, self.p3.id]
        )
        resp = self._detail(photo=self.pending.id)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "First picnic")
        self.assertNotContains(resp, "Pending should stay hidden")
        self.assertNotContains(resp, self.pending.original_filename)

    def test_invalid_and_foreign_photo_selection_falls_back_to_first(self):
        for raw in ("abc", "0", "-1", "999999", str(self.other_photo.id)):
            resp = self._detail(photo=raw)
            self.assertEqual(resp.status_code, 200, msg=raw)
            self.assertContains(resp, "First picnic")
            self.assertNotContains(resp, "Foreign photo description")
            self.assertContains(
                resp,
                _presign_url(bucket="x", key=self.p1.original_file_key),
            )
            self.assertNotContains(
                resp,
                _presign_url(bucket="x", key=self.other_photo.original_file_key),
            )

    def test_no_raw_s3_keys_or_upload_status_are_rendered(self):
        resp = self._detail(photo=self.p2.id)
        self.assertNotContains(resp, self.p1.original_file_key)
        self.assertNotContains(resp, self.p2.original_file_key)
        self.assertNotContains(resp, self.p1.thumbnail_file_key)
        self.assertNotContains(resp, self.p2.thumbnail_file_key)
        self.assertNotContains(resp, "PENDING")
        self.assertNotContains(resp, "FAILED")
        self.assertNotContains(resp, "upload_status")
        self.assertNotContains(resp, "original_file_key")

    def test_private_family_access_rules_unchanged(self):
        private_item = _create_photo_item(
            title="Private album",
            visibility=ArchiveItem.Visibility.PRIVATE,
        )
        _add_photo(private_item, position=1, description="Secret first")
        _add_photo(private_item, position=2, description="Secret second")
        anonymous = self._detail(item=private_item)
        self.assertEqual(anonymous.status_code, 404)

        self.client.force_login(self.family_user)
        family = self._detail(item=private_item)
        self.assertEqual(family.status_code, 200)
        self.assertContains(family, "Secret first")
        self.assertContains(family, "photo-gallery")

    def test_browse_card_still_uses_primary_photo_only(self):
        second_thumb = "photos/unique-second-thumb/thumb_400.jpg"
        self.p2.thumbnail_file_key = second_thumb
        self.p2.save(update_fields=["thumbnail_file_key", "updated_at"])
        resp = self.client.get(reverse("archive-list"))
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Family album")
        self.assertContains(resp, "archive-browse-card")
        self.assertContains(
            resp,
            _presign_url(bucket="x", key=self.p1.thumbnail_file_key),
        )
        self.assertNotContains(resp, _presign_url(bucket="x", key=second_thumb))
        self.assertContains(resp, "archive-browse-card--photo-preview")

    def test_browse_eligibility_still_requires_first_photo(self):
        blocked = _create_photo_item(title="Blocked until first uploads")
        _add_photo(
            blocked,
            position=1,
            upload_status=PhotoContent.UploadStatus.PENDING,
            original_file_key="",
        )
        uploaded_second = _add_photo(
            blocked,
            position=2,
            description="Second is uploaded",
        )
        listing = self.client.get(reverse("archive-list"))
        self.assertNotContains(listing, "Blocked until first uploads")
        detail = self._detail(item=blocked, photo=uploaded_second.id)
        self.assertEqual(detail.status_code, 404)

    def test_staff_management_routes_unaffected(self):
        self.client.force_login(self.staff)
        edit = self.client.get(
            reverse("archive-manage-edit", kwargs={"item_id": self.item.id})
        )
        add = self.client.get(
            reverse("archive-manage-photo-add", kwargs={"item_id": self.item.id})
        )
        photo_edit = self.client.get(
            reverse(
                "archive-manage-photo-edit",
                kwargs={"item_id": self.item.id, "photo_id": self.p2.id},
            )
        )
        self.assertEqual(edit.status_code, 200)
        self.assertEqual(add.status_code, 200)
        self.assertEqual(photo_edit.status_code, 200)
        self.assertContains(edit, "one.jpg")
        self.assertContains(edit, "two.jpg")

    def test_gallery_query_count_does_not_grow_n_plus_one(self):
        url = reverse("archive-detail", kwargs={"item_id": self.item.id})
        self.client.get(url)

        def _detail_query_counts() -> tuple[int, int]:
            with CaptureQueriesContext(connection) as ctx:
                resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200)
            return len(_people_sql(ctx.captured_queries)), len(ctx.captured_queries)

        people_before, total_before = _detail_query_counts()
        self.assertLessEqual(people_before, 2)

        PersonAlias.objects.create(person=self.ada, name="Ada Lovelace")
        PersonAlias.objects.create(person=self.rivka, name="Rivka Cohen")
        people_with_aliases, total_with_aliases = _detail_query_counts()
        self.assertEqual(people_before, people_with_aliases)
        self.assertEqual(total_before, total_with_aliases)

        for offset in range(4, 8):
            extra = _add_photo(
                self.item,
                position=10 + offset,
                description=f"Extra {offset}",
            )
            PhotoPerson.objects.create(
                photo_content=extra,
                person=Person.objects.create(name=f"Person {offset}"),
            )

        people_after, total_after = _detail_query_counts()
        self.assertEqual(people_before, people_after)
        self.assertEqual(total_before, total_after)

    def test_gallery_builder_does_not_query_people_on_viewable_item(self):
        item = get_viewable_archive_item(None, self.item.id)
        with CaptureQueriesContext(connection) as ctx:
            primary = item.primary_photo_content
            gallery = build_public_photo_gallery(
                item,
                selected_photo_param=str(self.p2.id),
                bucket="test-uploads-bucket",
            )
        self.assertIsNotNone(gallery)
        assert gallery is not None
        self.assertEqual(primary.pk, self.p1.pk)
        self.assertEqual(gallery.identified_people_names, ["Rivka"])
        self.assertEqual(_people_sql(ctx.captured_queries), [])
        self.assertEqual(len(ctx.captured_queries), 0)

    def test_missing_thumbnail_does_not_block_selection(self):
        resp = self._detail(photo=self.p3.id)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "Third gathering")
        self.assertContains(resp, 'aria-current="true"')
        self.assertContains(resp, "photo-gallery__thumb-fallback")

    def test_builder_does_not_presign_originals_for_unselected_photos(self):
        with patch(
            "documents.services.photo_gallery.presign_photo_thumbnail_url",
            return_value="https://s3.example/thumb-only",
        ) as mock_thumb:
            gallery = build_public_photo_gallery(
                self.item,
                selected_photo_param=str(self.p2.id),
                bucket="test-uploads-bucket",
            )
        self.assertIsNotNone(gallery)
        assert gallery is not None
        self.assertEqual(gallery.selected.id, self.p2.id)
        self.assertEqual(mock_thumb.call_count, 3)
        self.assertEqual(
            [item.photo.id for item in gallery.selector_items],
            [self.p1.id, self.p2.id, self.p3.id],
        )


class PhotoPublicGalleryStyleTests(SimpleTestCase):
    def test_gallery_css_reuses_photo_detail_width_cap(self):
        from django.conf import settings

        css_path = settings.BASE_DIR / "public" / "static" / "public" / "app.css"
        css = css_path.read_text(encoding="utf-8")
        gallery_start = css.index(".photo-gallery {")
        gallery_block = css[gallery_start : css.index("}", gallery_start) + 1]
        self.assertIn("max-width: 960px", gallery_block)
        self.assertIn("width: 100%", gallery_block)
        thumb_start = css.index(".photo-gallery__thumb {")
        thumb_block = css[thumb_start : css.index("}", thumb_start) + 1]
        self.assertIn("min-width: 44px", thumb_block)
        self.assertIn("min-height: 44px", thumb_block)
