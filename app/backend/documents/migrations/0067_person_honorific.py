from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0066_documentsourcefile_include_in_ocr"),
    ]

    operations = [
        migrations.AddField(
            model_name="person",
            name="honorific",
            field=models.CharField(blank=True, default="", max_length=255),
        ),
    ]
