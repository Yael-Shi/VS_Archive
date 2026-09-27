import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0069_personalias_metadata"),
    ]

    operations = [
        migrations.CreateModel(
            name="PersonFamilyName",
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
                ("name", models.CharField(max_length=255)),
                (
                    "role",
                    models.CharField(
                        choices=[
                            ("previous_family", "שם משפחה קודם"),
                            ("acquired_family", "שם משפחה שנרכש"),
                        ],
                        max_length=32,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "person",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="family_names",
                        to="documents.person",
                    ),
                ),
            ],
            options={
                "ordering": ["role", "name", "id"],
            },
        ),
        migrations.CreateModel(
            name="PersonRegistryImportBinding",
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
                ("source", models.CharField(max_length=255)),
                ("stable_key", models.CharField(max_length=255)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "person",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="registry_import_bindings",
                        to="documents.person",
                    ),
                ),
            ],
        ),
        migrations.AddConstraint(
            model_name="personfamilyname",
            constraint=models.UniqueConstraint(
                fields=("person", "name", "role"),
                name="uniq_person_family_name_person_name_role",
            ),
        ),
        migrations.AddConstraint(
            model_name="personregistryimportbinding",
            constraint=models.UniqueConstraint(
                fields=("source", "stable_key"),
                name="uniq_person_registry_import_binding_source_key",
            ),
        ),
    ]
