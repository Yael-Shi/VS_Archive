from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0021_manualtextcontent"),
    ]

    operations = [
        migrations.AddField(
            model_name="archiveitem",
            name="author_name",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
        migrations.AddField(
            model_name="archiveitem",
            name="source_title",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
