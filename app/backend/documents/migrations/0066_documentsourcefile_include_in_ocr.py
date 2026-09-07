from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0065_document_processing_state_recovery_required"),
    ]

    operations = [
        migrations.AddField(
            model_name="documentsourcefile",
            name="include_in_ocr",
            field=models.BooleanField(
                default=True,
                help_text=(
                    "When False, this source file is stored and displayed but is "
                    "excluded from every OCR/reprocess/recovery path."
                ),
            ),
        ),
    ]
