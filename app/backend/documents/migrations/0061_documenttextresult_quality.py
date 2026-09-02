from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0060_arabic_printed_banded_ocr"),
    ]

    operations = [
        migrations.AddField(
            model_name="documenttextresult",
            name="quality",
            field=models.CharField(
                choices=[
                    ("UNKNOWN", "Unknown"),
                    ("LOW", "Low"),
                    ("MEDIUM", "Medium"),
                    ("GOOD", "Good"),
                ],
                default="UNKNOWN",
                help_text=(
                    "Automatic/base public quality (UNKNOWN/LOW/MEDIUM/GOOD). "
                    "HUMAN_VERIFIED and NEEDS_CORRECTION are presentation-only and "
                    "are not persisted."
                ),
                max_length=32,
            ),
        ),
        migrations.AddConstraint(
            model_name="documenttextresult",
            constraint=models.CheckConstraint(
                condition=models.Q(quality__in=["UNKNOWN", "LOW", "MEDIUM", "GOOD"]),
                name="dtr_quality_persisted_values",
            ),
        ),
    ]
