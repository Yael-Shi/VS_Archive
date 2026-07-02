from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0033_archive_metadata_suggestion"),
    ]

    operations = [
        migrations.AddField(
            model_name="archiveitem",
            name="public_note",
            field=models.TextField(blank=True, default=""),
        ),
    ]
