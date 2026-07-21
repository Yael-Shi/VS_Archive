# Generated manually for Transkribus run→snapshot local-completion association.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0039_transkribus_transcript_snapshot_models"),
    ]

    operations = [
        migrations.CreateModel(
            name="TranskribusRunAutomaticSnapshot",
            fields=[
                (
                    "id",
                    models.BigAutoField(
                        auto_created=True,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                (
                    "mapping_trusted",
                    models.BooleanField(
                        default=False,
                        help_text=(
                            "True when page_index↔pageNr came from a trusted upload "
                            "mapping. False for EXISTING_SERVER traversal-only indexes."
                        ),
                    ),
                ),
                (
                    "review_reasons",
                    models.JSONField(blank=True, default=list),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "run",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="automatic_snapshot_association",
                        to="documents.transkribusrun",
                    ),
                ),
                (
                    "snapshot",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="run_associations",
                        to="documents.transkribustranscriptsnapshot",
                    ),
                ),
            ],
        ),
    ]
