# Current entity for one historical non-person decision.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0077_transkribus_hover_geometry_repair_source_kind"),
    ]

    operations = [
        migrations.CreateModel(
            name="NonPersonEntityCorrection",
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
                ("canonical_name", models.CharField(max_length=255)),
                (
                    "display_name",
                    models.CharField(blank=True, default="", max_length=255),
                ),
                (
                    "entity_subtype",
                    models.CharField(blank=True, default="", max_length=40),
                ),
                ("approval_reference", models.CharField(max_length=255)),
                ("manifest_sha256", models.CharField(max_length=64)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "decision",
                    models.OneToOneField(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="correction",
                        to="documents.reviewednonpersonentitydecision",
                    ),
                ),
                (
                    "effective_entity",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="effective_corrections",
                        to="documents.nonpersonentity",
                    ),
                ),
                (
                    "retired_entity",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="retirement_corrections",
                        to="documents.nonpersonentity",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        condition=models.Q(retired_entity__isnull=False),
                        fields=("retired_entity",),
                        name="uniq_non_person_correction_retired_entity",
                    ),
                    models.CheckConstraint(
                        condition=(
                            models.Q(retired_entity__isnull=True)
                            | ~models.Q(retired_entity=models.F("effective_entity"))
                        ),
                        name="non_person_correction_retired_is_distinct",
                    ),
                ],
            },
        ),
    ]
