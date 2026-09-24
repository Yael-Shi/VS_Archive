from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0068_alter_transkribustranscriptsnapshot_source_kind"),
    ]

    operations = [
        migrations.AddField(
            model_name="personalias",
            name="kind",
            field=models.CharField(
                choices=[
                    ("unspecified", "לא צוין"),
                    ("other_language", "שם בשפה אחרת"),
                    ("cover_identity", "שם כיסוי"),
                    ("code_name", "שם קוד"),
                    ("underground_name", "שם מחתרתי"),
                    ("nickname", "כינוי / שם מוכר"),
                    ("name_variant", "וריאנט של השם"),
                    ("spelling_variant", "וריאנט איות"),
                    ("ocr_variant", "וריאנט OCR"),
                    ("partial_name", "שם חלקי"),
                    ("other", "אחר"),
                ],
                default="unspecified",
                max_length=32,
            ),
        ),
        migrations.AddField(
            model_name="personalias",
            name="language",
            field=models.CharField(
                blank=True,
                choices=[
                    ("he", "עברית"),
                    ("en", "אנגלית"),
                    ("fr", "צרפתית"),
                    ("ar", "ערבית"),
                ],
                default="",
                max_length=8,
            ),
        ),
        migrations.AddField(
            model_name="personalias",
            name="display_publicly",
            field=models.BooleanField(default=False),
        ),
    ]
