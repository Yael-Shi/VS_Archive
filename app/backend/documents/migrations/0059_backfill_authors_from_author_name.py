# One-time backfill: exact non-empty ArchiveItem.author_name → Author +
# ArchiveItemAuthor at position 0. Does not split commas, strip titles, merge
# spelling variants, or create Person / ArchiveItemPerson / PhotoPerson rows.
# Reverse deletes Author / ArchiveItemAuthor rows; author_name is unchanged.

from django.db import migrations


def backfill_authors_from_author_name(apps, schema_editor):
    ArchiveItem = apps.get_model("documents", "ArchiveItem")
    Author = apps.get_model("documents", "Author")
    ArchiveItemAuthor = apps.get_model("documents", "ArchiveItemAuthor")

    authors_by_name: dict[str, object] = {}
    links: list[object] = []

    for item in (
        ArchiveItem.objects.exclude(author_name="").order_by("pk").iterator()
    ):
        name = item.author_name
        if not name.strip():
            continue
        author = authors_by_name.get(name)
        if author is None:
            author = Author.objects.create(name=name)
            authors_by_name[name] = author
        links.append(
            ArchiveItemAuthor(
                archive_item=item,
                author=author,
                position=0,
            )
        )

    if links:
        ArchiveItemAuthor.objects.bulk_create(links)


def reverse_backfill_authors_from_author_name(apps, schema_editor):
    Author = apps.get_model("documents", "Author")
    ArchiveItemAuthor = apps.get_model("documents", "ArchiveItemAuthor")
    ArchiveItemAuthor.objects.all().delete()
    Author.objects.all().delete()


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0058_author_foundation"),
    ]

    operations = [
        migrations.RunPython(
            backfill_authors_from_author_name,
            reverse_backfill_authors_from_author_name,
        ),
    ]
