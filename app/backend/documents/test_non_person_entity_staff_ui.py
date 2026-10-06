"""Staff non-person registry index, edit, and alias CRUD."""

from __future__ import annotations

from django.contrib.auth.models import Group, User
from django.test import TestCase
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    ArchiveItemSearchIndex,
    NonPersonEntity,
    NonPersonEntityAlias,
)
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_item_presentation import (
    filter_archive_items_by_search_query,
)
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.archive_search_index import (
    archive_items_for_search_index_build,
    rebuild_archive_item_search_index,
)
from documents.services.non_person_entity_occurrences import source_text_sha256
from documents.services.non_person_entity_staff import (
    ALIAS_DUPLICATE_ERROR,
    ALIAS_SHARED_WARNING,
    ENTITY_CANONICAL_NAME_REQUIRED_ERROR,
    ENTITY_TYPE_INVALID_ERROR,
    STALE_OCCURRENCE_STATUS,
    VALID_OCCURRENCE_STATUS,
)
from documents.views import (
    ENTITY_ALIAS_ADDED_MSG,
    ENTITY_ALIAS_DELETED_MSG,
    ENTITY_ALIAS_UPDATED_MSG,
    ENTITY_UPDATED_MSG,
)

INDEX = "archive-manage-entities"
EDIT = "archive-manage-entity-edit"
ALIAS_EDIT = "archive-manage-entity-alias-edit"
ALIAS_DELETE = "archive-manage-entity-alias-delete"
PUBLIC_DETAIL = "archive-non-person-detail"
PUBLIC_INDEX = "archive-non-person-index"


def _entity(**overrides) -> NonPersonEntity:
    fields = {
        "canonical_name": "קהיר",
        "entity_type": NonPersonEntity.EntityType.PLACE,
        "entity_subtype": NonPersonEntity.EntitySubtype.CITY,
    }
    fields.update(overrides)
    return NonPersonEntity.objects.create(**fields)


def _alias(entity: NonPersonEntity, name: str, kind: str) -> NonPersonEntityAlias:
    return NonPersonEntityAlias.objects.create(entity=entity, name=name, kind=kind)


def _edit_url(entity: NonPersonEntity) -> str:
    return reverse(EDIT, kwargs={"entity_id": entity.id})


def _pin(
    item: ArchiveItem,
    entity: NonPersonEntity,
    body: str,
    *,
    sha: str | None = None,
    matched_text: str = "קהיר",
) -> ArchiveItemEntityOccurrence:
    return ArchiveItemEntityOccurrence.objects.create(
        archive_item=item,
        text_kind=ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
        source_text_sha256=source_text_sha256(body) if sha is None else sha,
        normalization_version="surface-v1",
        normalized_surface="קהיר",
        occurrence_ordinal=1,
        resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
        entity=entity,
        matched_text=matched_text,
    )


def _occurrence_snapshot(occurrence: ArchiveItemEntityOccurrence) -> tuple:
    occurrence.refresh_from_db()
    return (
        occurrence.archive_item_id,
        occurrence.text_kind,
        occurrence.source_text_sha256,
        occurrence.normalization_version,
        occurrence.normalized_surface,
        occurrence.occurrence_ordinal,
        occurrence.resolution_status,
        occurrence.entity_id,
        occurrence.decision_id,
        occurrence.matched_text,
        occurrence.updated_at,
    )


class EntityStaffAccessTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="entity_staff",
            password="test-pass",
            is_staff=True,
        )
        self.entity = _entity()
        self.index_url = reverse(INDEX)
        self.edit_url = _edit_url(self.entity)

    def test_non_admin_is_denied(self):
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        user = User.objects.create_user(username="entity_family", password="test-pass")
        user.groups.add(family_group)
        self.client.force_login(user)

        index_resp = self.client.get(self.index_url)
        edit_resp = self.client.get(self.edit_url)

        self.assertEqual(index_resp.status_code, 403)
        self.assertEqual(edit_resp.status_code, 403)

    def test_admin_can_open_registry_index(self):
        self.client.force_login(self.staff)
        resp = self.client.get(self.index_url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "ניהול מקומות וארגונים")
        self.assertContains(resp, self.entity.canonical_name)
        self.assertContains(resp, _edit_url(self.entity))
        manage = self.client.get(reverse("archive-manage-list"))
        self.assertContains(manage, reverse(INDEX))
        self.assertContains(manage, "ניהול מקומות וארגונים")


class EntityStaffIndexTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="entity_index_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)

    def test_q_matches_canonical_display_and_alias_once(self):
        matched = _entity(canonical_name="UniqueCanon", display_name="UniqueDisplay")
        _alias(matched, "UniqueAlias", NonPersonEntityAlias.Kind.SPELLING_VARIANT)
        _entity(canonical_name="Other Place", display_name="Elsewhere")

        for token in ("UniqueCanon", "uniquedisplay", "uniquealias"):
            resp = self.client.get(reverse(INDEX), {"q": token})
            rows = resp.context["entities"]
            self.assertEqual([row.entity_id for row in rows], [matched.id])

    def test_ordering_is_public_name_then_pk(self):
        gamma = _entity(canonical_name="aaa", display_name="Gamma", entity_subtype="")
        beta = _entity(canonical_name="zzz", display_name="Beta", entity_subtype="")
        alpha = _entity(canonical_name="Alpha", display_name="", entity_subtype="")
        same_first = _entity(
            canonical_name="hidden-a", display_name="Same", entity_subtype=""
        )
        same_second = _entity(
            canonical_name="hidden-b", display_name="Same", entity_subtype=""
        )

        resp = self.client.get(reverse(INDEX))
        ids = [row.entity_id for row in resp.context["entities"]]

        self.assertEqual(
            ids,
            [alpha.id, beta.id, gamma.id, same_first.id, same_second.id],
        )


class EntityStaffEditTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="entity_edit_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.entity = _entity(canonical_name="BeforeName", display_name="BeforeDisplay")
        self.url = _edit_url(self.entity)

    def _post_entity(self, **overrides):
        data = {
            "action": "update_entity",
            "canonical_name": self.entity.canonical_name,
            "display_name": self.entity.display_name,
            "entity_type": self.entity.entity_type,
            "entity_subtype": self.entity.entity_subtype,
        }
        data.update(overrides)
        return self.client.post(self.url, data, follow=True)

    def test_admin_can_open_edit_page(self):
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, "עריכת רשומה")
        self.assertContains(resp, "BeforeName")
        self.assertContains(resp, "הוספת שם חלופי")
        self.assertNotContains(resp, "מיזוג")

    def test_field_updates_save_and_redirect_to_edit(self):
        resp = self._post_entity(
            canonical_name="  AfterName  ",
            display_name="  AfterDisplay  ",
            entity_type=NonPersonEntity.EntityType.ORGANIZATION,
            entity_subtype=NonPersonEntity.EntitySubtype.SCHOOL,
        )
        self.assertEqual(resp.redirect_chain, [(self.url, 302)])
        self.assertContains(resp, ENTITY_UPDATED_MSG)
        self.entity.refresh_from_db()
        self.assertEqual(self.entity.canonical_name, "AfterName")
        self.assertEqual(self.entity.display_name, "AfterDisplay")
        self.assertEqual(
            self.entity.entity_type, NonPersonEntity.EntityType.ORGANIZATION
        )
        self.assertEqual(
            self.entity.entity_subtype, NonPersonEntity.EntitySubtype.SCHOOL
        )

    def test_invalid_type_and_blank_canonical_name_are_rejected(self):
        invalid_type = self._post_entity(entity_type="NOT_A_TYPE")
        blank_name = self._post_entity(canonical_name="   ")
        self.entity.refresh_from_db()

        self.assertEqual(invalid_type.status_code, 200)
        self.assertContains(invalid_type, ENTITY_TYPE_INVALID_ERROR)
        self.assertEqual(blank_name.status_code, 200)
        self.assertContains(blank_name, ENTITY_CANONICAL_NAME_REQUIRED_ERROR)
        self.assertEqual(self.entity.canonical_name, "BeforeName")
        self.assertEqual(self.entity.entity_type, NonPersonEntity.EntityType.PLACE)
        self.assertEqual(invalid_type.redirect_chain, [])
        self.assertEqual(blank_name.redirect_chain, [])


class EntityStaffAliasTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="entity_alias_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.entity = _entity(canonical_name="AliasHost")
        self.url = _edit_url(self.entity)

    def test_alias_add_saves_kind_and_allows_cross_entity_duplicate(self):
        other = _entity(canonical_name="OtherHost")
        _alias(other, "SharedAlias", NonPersonEntityAlias.Kind.ABBREVIATION)

        added = self.client.post(
            self.url,
            {
                "action": "add_alias",
                "alias_name": "  SharedAlias  ",
                "alias_kind": NonPersonEntityAlias.Kind.OCR_VARIANT,
            },
            follow=True,
        )
        alias = NonPersonEntityAlias.objects.get(entity=self.entity)

        self.assertContains(added, ENTITY_ALIAS_ADDED_MSG)
        self.assertContains(added, ALIAS_SHARED_WARNING)
        self.assertEqual(alias.name, "SharedAlias")
        self.assertEqual(alias.kind, NonPersonEntityAlias.Kind.OCR_VARIANT)
        self.assertEqual(
            NonPersonEntityAlias.objects.filter(name="SharedAlias").count(),
            2,
        )

        duplicate = self.client.post(
            self.url,
            {
                "action": "add_alias",
                "alias_name": "SharedAlias",
                "alias_kind": NonPersonEntityAlias.Kind.CURRENT_NAME,
            },
        )
        self.assertEqual(duplicate.status_code, 200)
        self.assertContains(duplicate, ALIAS_DUPLICATE_ERROR)
        self.assertEqual(self.entity.aliases.count(), 1)

    def test_alias_edit_updates_name_and_kind_and_rejects_duplicate(self):
        alias = _alias(self.entity, "OldAlias", NonPersonEntityAlias.Kind.ABBREVIATION)
        _alias(self.entity, "TakenAlias", NonPersonEntityAlias.Kind.CURRENT_NAME)
        edit_url = reverse(
            ALIAS_EDIT,
            kwargs={"entity_id": self.entity.id, "alias_id": alias.id},
        )

        updated = self.client.post(
            edit_url,
            {
                "name": "  NewAlias  ",
                "kind": NonPersonEntityAlias.Kind.TRANSLITERATION_VARIANT,
            },
            follow=True,
        )
        alias.refresh_from_db()
        self.assertContains(updated, ENTITY_ALIAS_UPDATED_MSG)
        self.assertEqual(alias.name, "NewAlias")
        self.assertEqual(alias.kind, NonPersonEntityAlias.Kind.TRANSLITERATION_VARIANT)

        conflict = self.client.post(
            edit_url,
            {"name": "TakenAlias", "kind": NonPersonEntityAlias.Kind.ABBREVIATION},
        )
        alias.refresh_from_db()
        self.assertEqual(conflict.status_code, 200)
        self.assertContains(conflict, ALIAS_DUPLICATE_ERROR)
        self.assertEqual(alias.name, "NewAlias")

    def test_alias_delete_confirmation_removes_alias_only(self):
        alias = _alias(self.entity, "GoingAway", NonPersonEntityAlias.Kind.ABBREVIATION)
        delete_url = reverse(
            ALIAS_DELETE,
            kwargs={"entity_id": self.entity.id, "alias_id": alias.id},
        )

        confirm = self.client.get(delete_url)
        self.assertEqual(confirm.status_code, 200)
        self.assertContains(confirm, "מחיקת שם חלופי")
        self.assertContains(confirm, "GoingAway")

        deleted = self.client.post(delete_url, follow=True)
        self.entity.refresh_from_db()
        self.assertContains(deleted, ENTITY_ALIAS_DELETED_MSG)
        self.assertFalse(NonPersonEntityAlias.objects.filter(pk=alias.id).exists())
        self.assertEqual(self.entity.canonical_name, "AliasHost")
        self.assertTrue(NonPersonEntity.objects.filter(pk=self.entity.id).exists())


class EntityStaffOccurrenceDisplayTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="entity_occurrence_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.entity = _entity(canonical_name="קהיר")
        self.body = "ביקור בקהיר"
        self.valid_item = create_manual_text_archive_item(
            title="פריט תקין",
            body=self.body,
        )
        self.stale_item = create_manual_text_archive_item(
            title="פריט מיושן",
            body=self.body,
        )
        self.valid_pin = _pin(
            self.valid_item, self.entity, self.body, matched_text="מצאתי"
        )
        self.stale_pin = _pin(
            self.stale_item,
            self.entity,
            self.body,
            sha="a" * 64,
            matched_text="טקסט ישן",
        )
        self.url = _edit_url(self.entity)

    def test_valid_and_stale_occurrences_are_read_only(self):
        before = (
            _occurrence_snapshot(self.valid_pin),
            _occurrence_snapshot(self.stale_pin),
        )
        resp = self.client.get(self.url)
        html = resp.content.decode()

        self.assertContains(resp, "פריט תקין")
        self.assertContains(resp, "מצאתי")
        self.assertContains(resp, VALID_OCCURRENCE_STATUS)
        self.assertContains(resp, "פריט מיושן")
        self.assertContains(resp, "טקסט ישן")
        self.assertContains(resp, STALE_OCCURRENCE_STATUS)
        self.assertNotIn("a" * 64, html)
        self.assertNotContains(resp, "הסרת קישור")
        self.assertNotContains(resp, "העברת קישור")
        self.assertEqual(
            (
                _occurrence_snapshot(self.valid_pin),
                _occurrence_snapshot(self.stale_pin),
            ),
            before,
        )
        self.assertEqual(ArchiveItemEntityOccurrence.objects.count(), 2)

    def test_renaming_entity_does_not_change_occurrence_or_validity(self):
        before = _occurrence_snapshot(self.valid_pin)
        resp = self.client.post(
            self.url,
            {
                "action": "update_entity",
                "canonical_name": "שם חדש לגמרי",
                "display_name": "שם תצוגה חדש",
                "entity_type": NonPersonEntity.EntityType.EVENT,
                "entity_subtype": "",
            },
            follow=True,
        )
        self.assertContains(resp, VALID_OCCURRENCE_STATUS)
        self.assertContains(resp, "פריט תקין")
        self.assertEqual(_occurrence_snapshot(self.valid_pin), before)
        self.assertEqual(self.valid_pin.entity_id, self.entity.id)


class EntityStaffPublicNonRegressionTests(TestCase):
    def setUp(self):
        self.staff = User.objects.create_user(
            username="entity_public_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(self.staff)
        self.entity = _entity(canonical_name="PublicHost", display_name="Public Name")

    def test_ocr_variant_stays_hidden_on_public_detail(self):
        self.client.post(
            _edit_url(self.entity),
            {
                "action": "add_alias",
                "alias_name": "OCRSECRET",
                "alias_kind": NonPersonEntityAlias.Kind.OCR_VARIANT,
            },
        )
        self.client.logout()
        resp = self.client.get(
            reverse(PUBLIC_DETAIL, kwargs={"entity_id": self.entity.id})
        )
        self.assertNotContains(resp, "OCRSECRET")
        self.assertNotContains(resp, "שמות נוספים")

    def test_public_lookup_sees_alias_without_search_index_changes(self):
        item = create_manual_text_archive_item(
            title="VisibleArchiveTitle",
            body="plain body",
        )
        loaded = archive_items_for_search_index_build(archive_item_ids=[item.pk]).get()
        rebuild_archive_item_search_index(loaded)
        index = ArchiveItemSearchIndex.objects.get(archive_item=item)
        before = (
            index.title_text,
            index.metadata_text,
            index.body_text,
            index.hebrew_translation_text,
            index.updated_at,
        )

        self.client.post(
            _edit_url(self.entity),
            {
                "action": "add_alias",
                "alias_name": "AliasOnlyToken",
                "alias_kind": NonPersonEntityAlias.Kind.SPELLING_VARIANT,
            },
        )
        index.refresh_from_db()
        self.assertEqual(
            (
                index.title_text,
                index.metadata_text,
                index.body_text,
                index.hebrew_translation_text,
                index.updated_at,
            ),
            before,
        )

        self.client.logout()
        registry = self.client.get(reverse(PUBLIC_INDEX), {"q": "AliasOnlyToken"})
        self.assertContains(registry, "Public Name")
        archive_by_alias = self.client.get(
            reverse("archive-list"),
            {"q": "AliasOnlyToken"},
        )
        archive_by_title = self.client.get(
            reverse("archive-list"),
            {"q": "VisibleArchiveTitle"},
        )
        self.assertNotContains(archive_by_alias, "VisibleArchiveTitle")
        self.assertContains(archive_by_title, "VisibleArchiveTitle")
        self.assertEqual(
            list(
                filter_archive_items_by_search_query(
                    ArchiveItem.objects.all(),
                    "AliasOnlyToken",
                ).values_list("pk", flat=True)
            ),
            [],
        )
        self.assertEqual(
            list(
                filter_archive_items_by_search_query(
                    ArchiveItem.objects.all(),
                    "VisibleArchiveTitle",
                ).values_list("pk", flat=True)
            ),
            [item.pk],
        )
