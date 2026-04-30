# Generated manually: add non-null engine_key and prompt_variant with backfill.

from django.db import migrations, models


def backfill_ocr_observability(apps, schema_editor):
    DocumentTextResult = apps.get_model("documents", "DocumentTextResult")
    DocumentTextResult.objects.all().update(
        engine_key="GEMINI",
        prompt_variant="handwritten",
    )


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0013_document_text_input_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="documenttextresult",
            name="engine_key",
            field=models.CharField(
                choices=[("GEMINI", "Gemini")],
                max_length=32,
                null=True,
            ),
        ),
        migrations.AddField(
            model_name="documenttextresult",
            name="prompt_variant",
            field=models.CharField(
                choices=[
                    ("handwritten", "Handwritten"),
                    ("printed", "Printed"),
                ],
                max_length=32,
                null=True,
            ),
        ),
        migrations.RunPython(backfill_ocr_observability, migrations.RunPython.noop),
        migrations.AlterField(
            model_name="documenttextresult",
            name="engine_key",
            field=models.CharField(
                choices=[("GEMINI", "Gemini")],
                max_length=32,
            ),
        ),
        migrations.AlterField(
            model_name="documenttextresult",
            name="prompt_variant",
            field=models.CharField(
                choices=[
                    ("handwritten", "Handwritten"),
                    ("printed", "Printed"),
                ],
                max_length=32,
            ),
        ),
    ]
