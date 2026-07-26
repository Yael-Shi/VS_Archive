# Generated manually for post-PR4 translation search coverage.

from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0042_archive_item_search_index"),
    ]

    operations = [
        migrations.AddField(
            model_name="archiveitemsearchindex",
            name="hebrew_translation_text",
            field=models.TextField(blank=True, default=""),
        ),
    ]
