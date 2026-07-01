# Generated manually: add ANTIGRAVITY to DocumentTextResult.engine_key choices.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0031_documenttextresult_revisions_and_edits"),
    ]

    operations = [
        migrations.AlterField(
            model_name="documenttextresult",
            name="engine_key",
            field=models.CharField(
                choices=[
                    ("GEMINI", "Gemini"),
                    ("TRANSKRIBUS", "Transkribus"),
                    ("ANTIGRAVITY", "Antigravity"),
                ],
                max_length=32,
            ),
        ),
    ]
