from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0012_documenttextresult_review_reasons"),
    ]

    operations = [
        migrations.AddField(
            model_name="document",
            name="text_input_type",
            field=models.CharField(
                choices=[("HANDWRITTEN", "Handwritten"), ("PRINTED", "Printed")],
                default="HANDWRITTEN",
                max_length=16,
            ),
            preserve_default=False,
        ),
    ]
