# ArchiveItem foundation + Document.archive_item backfill.

from django.db import migrations, models
import django.db.models.deletion

ARCHIVE_ITEM_SHARED_FIELD_NAMES = (
    "title",
    "visibility",
    "date_start",
    "date_end",
    "date_precision",
    "metadata_status",
)


def _archive_item_values_from_document(document):
    return {name: getattr(document, name) for name in ARCHIVE_ITEM_SHARED_FIELD_NAMES}


def backfill_archive_items(apps, schema_editor):
    Document = apps.get_model("documents", "Document")
    ArchiveItem = apps.get_model("documents", "ArchiveItem")

    for document in Document.objects.filter(archive_item__isnull=True).iterator():
        archive_item = ArchiveItem.objects.create(
            **_archive_item_values_from_document(document),
            item_type="OCR_DOCUMENT",
        )
        ArchiveItem.objects.filter(pk=archive_item.pk).update(
            created_at=document.created_at,
            updated_at=document.updated_at,
        )
        document.archive_item_id = archive_item.pk
        document.save(update_fields=["archive_item_id"])


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0019_document_date_precision"),
    ]

    operations = [
        migrations.CreateModel(
            name="ArchiveItem",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("title", models.CharField(max_length=255)),
                (
                    "item_type",
                    models.CharField(
                        choices=[
                            ("OCR_DOCUMENT", "OCR document"),
                            ("MANUAL_TEXT", "Manual text"),
                            ("PHOTO", "Photo"),
                        ],
                        max_length=32,
                    ),
                ),
                (
                    "visibility",
                    models.CharField(
                        choices=[("private", "Private"), ("public", "Public")],
                        default="private",
                        max_length=16,
                    ),
                ),
                ("date_start", models.DateField(blank=True, null=True)),
                ("date_end", models.DateField(blank=True, null=True)),
                (
                    "date_precision",
                    models.CharField(
                        choices=[
                            ("EXACT_DAY", "Exact day"),
                            ("MONTH", "Month"),
                            ("YEAR", "Year"),
                            ("RANGE", "Range"),
                            ("UNKNOWN", "Unknown"),
                        ],
                        default="UNKNOWN",
                        max_length=16,
                    ),
                ),
                (
                    "metadata_status",
                    models.CharField(
                        choices=[
                            ("NEEDS_COMPLETION", "Needs completion"),
                            ("COMPLETED", "Completed"),
                        ],
                        default="NEEDS_COMPLETION",
                        max_length=32,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
        ),
        migrations.AddField(
            model_name="document",
            name="archive_item",
            field=models.OneToOneField(
                null=True,
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ocr_document",
                to="documents.archiveitem",
            ),
        ),
        migrations.RunPython(backfill_archive_items, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="document",
            name="archive_item",
            field=models.OneToOneField(
                on_delete=django.db.models.deletion.CASCADE,
                related_name="ocr_document",
                to="documents.archiveitem",
            ),
        ),
    ]
