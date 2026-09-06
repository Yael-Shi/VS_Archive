import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0062_reviewed_person_import_binding"),
    ]

    operations = [
        migrations.AddField(
            model_name="author",
            name="person",
            field=models.ForeignKey(
                blank=True,
                null=True,
                on_delete=django.db.models.deletion.SET_NULL,
                related_name="author_identities",
                to="documents.person",
            ),
        ),
    ]
