from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0007_remove_document_metadata_and_more"),
    ]

    operations = [
        migrations.AlterField(
            model_name="document",
            name="language",
            field=models.CharField(
                blank=True,
                choices=[
                    ("he", "Hebrew"),
                    ("en", "English"),
                    ("fr", "French"),
                    ("ar", "Arabic"),
                ],
                max_length=8,
                null=True,
            ),
        ),
    ]
