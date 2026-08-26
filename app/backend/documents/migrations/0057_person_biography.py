from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0056_archive_item_person_suggestion"),
    ]

    operations = [
        migrations.AddField(
            model_name="person",
            name="biography",
            field=models.TextField(blank=True, default=""),
        ),
    ]
