# Academic index schema only. No seed data.

import django.core.validators
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("documents", "0078_non_person_entity_correction"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="AcademicTopic",
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
                    "stable_key",
                    models.CharField(
                        max_length=64,
                        unique=True,
                        validators=[
                            django.core.validators.RegexValidator(
                                "^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$"
                            )
                        ],
                    ),
                ),
                ("preferred_label", models.CharField(max_length=255)),
                ("scope_note", models.TextField(blank=True, default="")),
                ("is_public_candidate", models.BooleanField(default=False)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
            ],
            options={
                "indexes": [
                    models.Index(fields=["preferred_label"], name="acad_topic_label_idx"),
                ],
            },
        ),
        migrations.CreateModel(
            name="AcademicIndexDecisionEvent",
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
                    "action",
                    models.CharField(
                        choices=[
                            ("IMPORT", "IMPORT"),
                            ("APPROVE", "APPROVE"),
                            ("REJECT", "REJECT"),
                            ("DEFER", "DEFER"),
                            ("RETRACT", "RETRACT"),
                            ("SUPERSEDE", "SUPERSEDE"),
                        ],
                        max_length=16,
                    ),
                ),
                (
                    "target_kind",
                    models.CharField(
                        choices=[
                            ("TOPIC", "TOPIC"),
                            ("TOPIC_LABEL", "TOPIC_LABEL"),
                            ("TOPIC_RELATION", "TOPIC_RELATION"),
                            ("ITEM_ASSOCIATION", "ITEM_ASSOCIATION"),
                        ],
                        max_length=32,
                    ),
                ),
                ("target_id", models.PositiveIntegerField(blank=True, null=True)),
                ("batch_id", models.CharField(blank=True, default="", max_length=64)),
                (
                    "evidence_kind",
                    models.CharField(
                        choices=[
                            ("OWNER_APPROVAL", "OWNER_APPROVAL"),
                            ("ORIGINAL_IMAGE", "ORIGINAL_IMAGE"),
                            ("TRANSCRIPTION", "TRANSCRIPTION"),
                            ("TRANSLATION", "TRANSLATION"),
                            ("METADATA", "METADATA"),
                            ("OWNER_KNOWLEDGE", "OWNER_KNOWLEDGE"),
                            ("EXTERNAL_SOURCE", "EXTERNAL_SOURCE"),
                        ],
                        max_length=32,
                    ),
                ),
                ("evidence_note", models.TextField(blank=True, default="")),
                ("reason", models.TextField(blank=True, default="")),
                (
                    "specification_sha256",
                    models.CharField(
                        blank=True,
                        default="",
                        max_length=64,
                        validators=[
                            django.core.validators.RegexValidator("^[0-9a-f]{64}$")
                        ],
                    ),
                ),
                ("operation_key", models.CharField(blank=True, default="", max_length=255)),
                ("snapshot", models.JSONField()),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "actor",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="academic_index_decision_events",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["target_kind", "target_id", "id"],
                        name="acad_decision_target_idx",
                    ),
                ],
                "constraints": [
                    models.UniqueConstraint(
                        condition=~models.Q(operation_key=""),
                        fields=("operation_key",),
                        name="uniq_acad_decision_operation_key",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="AcademicTopicLabel",
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
                ("label", models.CharField(max_length=255)),
                ("language", models.CharField(default="he", max_length=8)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                (
                    "topic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="alternate_labels",
                        to="documents.academictopic",
                    ),
                ),
            ],
            options={
                "constraints": [
                    models.UniqueConstraint(
                        fields=("topic", "label"),
                        name="uniq_acad_topic_label_topic_label",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="AcademicTopicRelation",
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
                    "relation_kind",
                    models.CharField(
                        choices=[("BROADER", "BROADER"), ("RELATED", "RELATED")],
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("APPROVED", "APPROVED"), ("RETRACTED", "RETRACTED")],
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "broader",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="as_broader_edges",
                        to="documents.academictopic",
                    ),
                ),
                (
                    "decision_event",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="topic_relations",
                        to="documents.academicindexdecisionevent",
                    ),
                ),
                (
                    "narrower",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="as_narrower_edges",
                        to="documents.academictopic",
                    ),
                ),
                (
                    "topic_high",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="as_related_high_edges",
                        to="documents.academictopic",
                    ),
                ),
                (
                    "topic_low",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="as_related_low_edges",
                        to="documents.academictopic",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["status", "relation_kind"],
                        name="acad_rel_status_kind_idx",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=(
                            (
                                models.Q(
                                    relation_kind="BROADER",
                                    narrower__isnull=False,
                                    broader__isnull=False,
                                    topic_low__isnull=True,
                                    topic_high__isnull=True,
                                )
                                & ~models.Q(narrower=models.F("broader"))
                            )
                            | (
                                models.Q(
                                    relation_kind="RELATED",
                                    narrower__isnull=True,
                                    broader__isnull=True,
                                    topic_low__isnull=False,
                                    topic_high__isnull=False,
                                )
                                & models.Q(topic_low_id__lt=models.F("topic_high_id"))
                            )
                        ),
                        name="acad_topic_relation_kind_shape",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(status__in=["APPROVED", "RETRACTED"]),
                        name="acad_topic_relation_status",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(relation_kind="BROADER"),
                        fields=("narrower", "broader"),
                        name="uniq_acad_rel_broader_pair",
                    ),
                    models.UniqueConstraint(
                        condition=models.Q(relation_kind="RELATED"),
                        fields=("topic_low", "topic_high"),
                        name="uniq_acad_rel_related_pair",
                    ),
                ],
            },
        ),
        migrations.CreateModel(
            name="ArchiveItemTopicAssociation",
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
                    "relevance",
                    models.CharField(
                        choices=[("CENTRAL", "CENTRAL"), ("SECONDARY", "SECONDARY")],
                        max_length=16,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[("APPROVED", "APPROVED"), ("RETRACTED", "RETRACTED")],
                        max_length=16,
                    ),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                (
                    "archive_item",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="topic_associations",
                        to="documents.archiveitem",
                    ),
                ),
                (
                    "decision_event",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="item_associations",
                        to="documents.academicindexdecisionevent",
                    ),
                ),
                (
                    "topic",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.PROTECT,
                        related_name="item_associations",
                        to="documents.academictopic",
                    ),
                ),
            ],
            options={
                "indexes": [
                    models.Index(
                        fields=["topic", "status"],
                        name="acad_item_topic_status_idx",
                    ),
                ],
                "constraints": [
                    models.CheckConstraint(
                        condition=models.Q(relevance__in=["CENTRAL", "SECONDARY"]),
                        name="acad_item_topic_relevance",
                    ),
                    models.CheckConstraint(
                        condition=models.Q(status__in=["APPROVED", "RETRACTED"]),
                        name="acad_item_topic_status",
                    ),
                    models.UniqueConstraint(
                        fields=("archive_item", "topic"),
                        name="uniq_acad_item_topic_pair",
                    ),
                ],
            },
        ),
    ]
