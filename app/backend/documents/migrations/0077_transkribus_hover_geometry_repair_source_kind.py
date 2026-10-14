# Generated for the hover-geometry repair snapshot source kind.

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0076_non_person_entity_taxonomy_extension"),
    ]

    operations = [
        migrations.AlterField(
            model_name="transkribustranscriptsnapshot",
            name="source_kind",
            field=models.CharField(
                choices=[
                    ("AUTOMATIC_HTR", "Automatic HTR"),
                    ("CORRECTED_CURRENT_SYNC", "Corrected-current sync"),
                    ("LEGACY_IMPORT", "Legacy Transkribus import"),
                    ("HOVER_GEOMETRY_REPAIR", "Hover geometry repair"),
                ],
                max_length=32,
            ),
        ),
    ]
