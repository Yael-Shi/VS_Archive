# One-time historical backfill: approved person-name Tags → Person + ArchiveItemPerson.
#
# Source of identity is the frozen Tag id mapping below, not Person.name.
# Reverse is intentionally a no-op: Person.name is not unique and this
# migration does not persist a Tag→Person mapping table, so reverse cannot
# safely identify which Person rows it created after later staff links.

from django.db import migrations, transaction


class PersonNameTagBackfillError(Exception):
    """Fail-closed precondition failure; no Person / ArchiveItemPerson writes."""


# Frozen production Tag id → exact stored Tag.name. Comparison is exact
# stored-string equality only (no strip/casefold/transliteration/fuzzy).
APPROVED_PERSON_NAME_TAGS: tuple[tuple[int, str], ...] = (
    (2, "רפאל רקנטי"),
    (4, "פליקס בן זקן"),
    (5, "יוסף קטאוי"),
    (7, "אלי פלג"),
    (8, "אליהו ברכה"),
    (10, "לאון קסטרו"),
    (11, "הרב נחום אפנדי"),
    (14, "מדרכי אביצור"),
    (15, "הרב דר' משה ונטורה"),
    (16, "אלי כהן"),
    (19, "המלך פארוק"),
    (20, "יולנדה הארמר- גבאי"),
    (23, "משה מרזוק"),
    (24, "שמואל עזר"),
    (25, "איסר הראל"),
    (26, "רוברט דסה"),
    (27, "ויקטור לוי"),
    (28, "מרסל ניניו"),
    (29, "שלמה הלל"),
    (30, "שלמה פלטנר"),
    (31, "מקס בינט"),
    (32, "אלי נעים"),
    (33, "יצחק לוי - גבלאוי"),
    (34, "שמואל שפיטלניק"),
    (35, "פיליפ נתנזון"),
    (36, "מוריס זקס"),
    (37, "אברי אלעד"),
    (38, "עובדיה דנון"),
    (39, "מאיר מיוחס"),
)

APPROVED_PERSON_NAME_TAG_IDS: tuple[int, ...] = tuple(
    tag_id for tag_id, _name in APPROVED_PERSON_NAME_TAGS
)


def _validate_approved_person_name_tags(Tag):
    expected_by_id = dict(APPROVED_PERSON_NAME_TAGS)
    tags_by_id = {
        tag.pk: tag
        for tag in Tag.objects.filter(pk__in=expected_by_id)
    }
    if not tags_by_id:
        # Empty / test / new environments have none of the frozen production
        # Tag ids. Skip rather than fail migrate. A partial set still fails.
        return None
    missing_ids = sorted(set(expected_by_id) - set(tags_by_id))
    if missing_ids:
        raise PersonNameTagBackfillError(
            "Approved person-name Tag ids are missing: "
            + ", ".join(str(tag_id) for tag_id in missing_ids)
        )
    mismatches = []
    for tag_id, expected_name in APPROVED_PERSON_NAME_TAGS:
        actual_name = tags_by_id[tag_id].name
        if actual_name != expected_name:
            mismatches.append(
                f"id={tag_id} expected={expected_name!r} actual={actual_name!r}"
            )
    if mismatches:
        raise PersonNameTagBackfillError(
            "Approved person-name Tag names do not match exactly: "
            + "; ".join(mismatches)
        )
    return tags_by_id


def _tagged_archive_item_ids(tag):
    return list(
        tag.archive_items.order_by("pk").values_list("pk", flat=True).distinct()
    )


def _intended_person_id_for_tag(
    ArchiveItemPerson,
    *,
    tag_id,
    expected_name,
    archive_item_ids,
):
    """Resolve this Tag's intended Person without using Person.name uniqueness.

    Identity is: the distinct Person already linked via ArchiveItemPerson to
    current ArchiveItem.tags relations for this Tag, whose canonical name
    equals the approved Tag name exactly. Zero matches → create later.
    More than one distinct Person → fail closed (unsafe identity).
    Same-name Person rows that are not linked through these tagged items
    are ignored (they are different identities). Tags with no current
    ArchiveItem.tags relations cannot be re-identified by this lookup;
    Django applies this migration once and the write phase is atomic.
    """
    if not archive_item_ids:
        return None
    person_ids = list(
        ArchiveItemPerson.objects.filter(
            archive_item_id__in=archive_item_ids,
            person__name=expected_name,
        )
        .values_list("person_id", flat=True)
        .distinct()
        .order_by("person_id")
    )
    if len(person_ids) > 1:
        raise PersonNameTagBackfillError(
            "Ambiguous Person identity for approved Tag id="
            f"{tag_id}: {len(person_ids)} Person rows with exact name "
            f"{expected_name!r} already linked via ArchiveItemPerson to "
            "currently tagged ArchiveItems."
        )
    if person_ids:
        return person_ids[0]
    return None


def backfill_persons_from_approved_person_name_tags(apps, schema_editor):
    """Create Person + ArchiveItemPerson from approved Tag ids.

    Does not write PhotoPerson, PersonAlias, Tag, ArchiveItem.tags, or
    Document.tags_m2m. Does not rebuild the archive search index.
    """
    Tag = apps.get_model("documents", "Tag")
    Person = apps.get_model("documents", "Person")
    ArchiveItemPerson = apps.get_model("documents", "ArchiveItemPerson")

    tags_by_id = _validate_approved_person_name_tags(Tag)
    if tags_by_id is None:
        return

    plans = []
    for tag_id, expected_name in APPROVED_PERSON_NAME_TAGS:
        tag = tags_by_id[tag_id]
        archive_item_ids = _tagged_archive_item_ids(tag)
        intended_person_id = _intended_person_id_for_tag(
            ArchiveItemPerson,
            tag_id=tag_id,
            expected_name=expected_name,
            archive_item_ids=archive_item_ids,
        )
        plans.append((tag, expected_name, archive_item_ids, intended_person_id))

    with transaction.atomic():
        for tag, expected_name, archive_item_ids, intended_person_id in plans:
            if intended_person_id is None:
                person = Person.objects.create(name=tag.name)
                person_id = person.pk
            else:
                person_id = intended_person_id
            for archive_item_id in archive_item_ids:
                ArchiveItemPerson.objects.get_or_create(
                    archive_item_id=archive_item_id,
                    person_id=person_id,
                )


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0054_personalias"),
    ]

    operations = [
        migrations.RunPython(
            backfill_persons_from_approved_person_name_tags,
            migrations.RunPython.noop,
        ),
    ]
