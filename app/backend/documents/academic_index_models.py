"""Controlled topical index. Not categories, tags, events, or text pins.

State rows are written through ``documents.services.academic_index``.
``QuerySet.update`` and ``QuerySet.delete`` on these state models are rejected.
``bulk_create`` and direct SQL are not intercepted.

``AcademicIndexDecisionEvent`` is append-only. ``save`` after insert,
``delete``, and queryset ``update`` / ``delete`` / ``bulk_update`` raise.
Direct SQL is not intercepted.
"""

from __future__ import annotations

import re

from django.conf import settings
from django.core.exceptions import ValidationError
from django.core.validators import RegexValidator
from django.db import models

from documents.services.academic_index_guard import academic_index_state_write_allowed

_STABLE_KEY = RegexValidator(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")
_SHA256 = RegexValidator(r"^[0-9a-f]{64}$")
_WRITE_REQUIRED = (
    "Academic index state must be written through documents.services.academic_index."
)


class AcademicIndexStateQuerySet(models.QuerySet):
    """Block queryset updates and deletes. ``bulk_create`` and SQL are not blocked."""

    def update(self, **kwargs):
        raise ValidationError(_WRITE_REQUIRED)

    def delete(self):
        raise ValidationError(_WRITE_REQUIRED)

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError(_WRITE_REQUIRED)


class AcademicTopic(models.Model):
    """One controlled topic. Matching labels do not merge identities."""

    stable_key = models.CharField(max_length=64, unique=True, validators=[_STABLE_KEY])
    preferred_label = models.CharField(max_length=255)
    scope_note = models.TextField(blank=True, default="")
    is_public_candidate = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AcademicIndexStateQuerySet.as_manager()

    class Meta:
        indexes = [
            models.Index(fields=["preferred_label"], name="acad_topic_label_idx"),
        ]

    def clean(self) -> None:
        super().clean()
        self.preferred_label = (self.preferred_label or "").strip()
        if not self.preferred_label:
            raise ValidationError({"preferred_label": "A topic requires a preferred label."})
        self.stable_key = (self.stable_key or "").strip()

    def save(self, *args, **kwargs) -> None:
        if not academic_index_state_write_allowed():
            raise ValidationError(_WRITE_REQUIRED)
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_WRITE_REQUIRED)

    def __str__(self) -> str:
        return f"{self.stable_key} {self.preferred_label}"


class AcademicTopicLabel(models.Model):
    """Alternate label for one topic. The preferred label stays on the topic."""

    topic = models.ForeignKey(
        AcademicTopic,
        on_delete=models.CASCADE,
        related_name="alternate_labels",
    )
    label = models.CharField(max_length=255)
    language = models.CharField(max_length=8, default="he")
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AcademicIndexStateQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["topic", "label"],
                name="uniq_acad_topic_label_topic_label",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        self.label = (self.label or "").strip()
        if not self.label:
            raise ValidationError({"label": "An alternate label cannot be blank."})

    def save(self, *args, **kwargs) -> None:
        if not academic_index_state_write_allowed():
            raise ValidationError(_WRITE_REQUIRED)
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_WRITE_REQUIRED)

    def __str__(self) -> str:
        return self.label


class AcademicTopicRelation(models.Model):
    """One direct topic edge.

    ``BROADER`` stores ``narrower`` / ``broader`` only. The inverse and
    transitive closure are computed. ``RELATED`` stores ``topic_low`` /
    ``topic_high`` with ``topic_low_id < topic_high_id`` so the symmetric
    edge exists once.

    ``status`` is ``APPROVED`` or ``RETRACTED``. Retraction updates this row.
    It does not delete it. Active uniqueness is one row per natural key,
    including retracted rows, so reactivation cannot insert a second edge.
    """

    class Kind(models.TextChoices):
        BROADER = "BROADER", "BROADER"
        RELATED = "RELATED", "RELATED"

    class Status(models.TextChoices):
        APPROVED = "APPROVED", "APPROVED"
        RETRACTED = "RETRACTED", "RETRACTED"

    relation_kind = models.CharField(max_length=16, choices=Kind.choices)
    status = models.CharField(max_length=16, choices=Status.choices)
    narrower = models.ForeignKey(
        AcademicTopic,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="as_narrower_edges",
    )
    broader = models.ForeignKey(
        AcademicTopic,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="as_broader_edges",
    )
    topic_low = models.ForeignKey(
        AcademicTopic,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="as_related_low_edges",
    )
    topic_high = models.ForeignKey(
        AcademicTopic,
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="as_related_high_edges",
    )
    decision_event = models.ForeignKey(
        "AcademicIndexDecisionEvent",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="topic_relations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AcademicIndexStateQuerySet.as_manager()

    class Meta:
        constraints = [
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
                fields=["narrower", "broader"],
                condition=models.Q(relation_kind="BROADER"),
                name="uniq_acad_rel_broader_pair",
            ),
            models.UniqueConstraint(
                fields=["topic_low", "topic_high"],
                condition=models.Q(relation_kind="RELATED"),
                name="uniq_acad_rel_related_pair",
            ),
        ]
        indexes = [
            models.Index(
                fields=["status", "relation_kind"],
                name="acad_rel_status_kind_idx",
            ),
        ]

    def save(self, *args, **kwargs) -> None:
        if not academic_index_state_write_allowed():
            raise ValidationError(_WRITE_REQUIRED)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_WRITE_REQUIRED)

    def __str__(self) -> str:
        return f"AcademicTopicRelation({self.relation_kind}, {self.status}, id={self.pk})"


class ArchiveItemTopicAssociation(models.Model):
    """Direct aboutness of one archive item to one topic.

    Inherited ancestor membership is not stored. This row does not depend on
    an OCR text SHA. Retraction keeps the row and appends a decision event.
    """

    class Relevance(models.TextChoices):
        CENTRAL = "CENTRAL", "CENTRAL"
        SECONDARY = "SECONDARY", "SECONDARY"

    class Status(models.TextChoices):
        APPROVED = "APPROVED", "APPROVED"
        RETRACTED = "RETRACTED", "RETRACTED"

    archive_item = models.ForeignKey(
        "documents.ArchiveItem",
        on_delete=models.CASCADE,
        related_name="topic_associations",
    )
    topic = models.ForeignKey(
        AcademicTopic,
        on_delete=models.PROTECT,
        related_name="item_associations",
    )
    relevance = models.CharField(max_length=16, choices=Relevance.choices)
    status = models.CharField(max_length=16, choices=Status.choices)
    decision_event = models.ForeignKey(
        "AcademicIndexDecisionEvent",
        on_delete=models.PROTECT,
        null=True,
        blank=True,
        related_name="item_associations",
    )
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    objects = AcademicIndexStateQuerySet.as_manager()

    class Meta:
        constraints = [
            models.CheckConstraint(
                condition=models.Q(relevance__in=["CENTRAL", "SECONDARY"]),
                name="acad_item_topic_relevance",
            ),
            models.CheckConstraint(
                condition=models.Q(status__in=["APPROVED", "RETRACTED"]),
                name="acad_item_topic_status",
            ),
            models.UniqueConstraint(
                fields=["archive_item", "topic"],
                name="uniq_acad_item_topic_pair",
            ),
        ]
        indexes = [
            models.Index(fields=["topic", "status"], name="acad_item_topic_status_idx"),
        ]

    def save(self, *args, **kwargs) -> None:
        if not academic_index_state_write_allowed():
            raise ValidationError(_WRITE_REQUIRED)
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError(_WRITE_REQUIRED)

    def __str__(self) -> str:
        return (
            "ArchiveItemTopicAssociation("
            f"item={self.archive_item_id}, topic={self.topic_id}, "
            f"{self.relevance}, {self.status})"
        )


class AcademicIndexDecisionEventQuerySet(models.QuerySet):
    """Reject ORM updates and deletes. Direct SQL is not intercepted."""

    def update(self, **kwargs):
        raise ValidationError("AcademicIndexDecisionEvent rows are append-only.")

    def delete(self):
        raise ValidationError("AcademicIndexDecisionEvent rows are append-only.")

    def bulk_update(self, objs, fields, batch_size=None):
        raise ValidationError("AcademicIndexDecisionEvent rows are append-only.")


class AcademicIndexDecisionEvent(models.Model):
    """Append-only academic-index decision.

    There is no ``updated_at``. ``save`` after the row exists raises.
    Queryset ``update``, ``delete``, and ``bulk_update`` raise.
    Direct SQL is not intercepted.
    """

    class Action(models.TextChoices):
        IMPORT = "IMPORT", "IMPORT"
        APPROVE = "APPROVE", "APPROVE"
        REJECT = "REJECT", "REJECT"
        DEFER = "DEFER", "DEFER"
        RETRACT = "RETRACT", "RETRACT"
        SUPERSEDE = "SUPERSEDE", "SUPERSEDE"

    class TargetKind(models.TextChoices):
        TOPIC = "TOPIC", "TOPIC"
        TOPIC_LABEL = "TOPIC_LABEL", "TOPIC_LABEL"
        TOPIC_RELATION = "TOPIC_RELATION", "TOPIC_RELATION"
        ITEM_ASSOCIATION = "ITEM_ASSOCIATION", "ITEM_ASSOCIATION"

    class EvidenceKind(models.TextChoices):
        OWNER_APPROVAL = "OWNER_APPROVAL", "OWNER_APPROVAL"
        ORIGINAL_IMAGE = "ORIGINAL_IMAGE", "ORIGINAL_IMAGE"
        TRANSCRIPTION = "TRANSCRIPTION", "TRANSCRIPTION"
        TRANSLATION = "TRANSLATION", "TRANSLATION"
        METADATA = "METADATA", "METADATA"
        OWNER_KNOWLEDGE = "OWNER_KNOWLEDGE", "OWNER_KNOWLEDGE"
        EXTERNAL_SOURCE = "EXTERNAL_SOURCE", "EXTERNAL_SOURCE"

    action = models.CharField(max_length=16, choices=Action.choices)
    target_kind = models.CharField(max_length=32, choices=TargetKind.choices)
    target_id = models.PositiveIntegerField(null=True, blank=True)
    batch_id = models.CharField(max_length=64, blank=True, default="")
    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="academic_index_decision_events",
    )
    evidence_kind = models.CharField(max_length=32, choices=EvidenceKind.choices)
    evidence_note = models.TextField(blank=True, default="")
    reason = models.TextField(blank=True, default="")
    specification_sha256 = models.CharField(
        max_length=64,
        blank=True,
        default="",
        validators=[_SHA256],
    )
    operation_key = models.CharField(max_length=255, blank=True, default="")
    snapshot = models.JSONField()
    created_at = models.DateTimeField(auto_now_add=True)

    objects = AcademicIndexDecisionEventQuerySet.as_manager()

    class Meta:
        constraints = [
            models.UniqueConstraint(
                fields=["operation_key"],
                condition=~models.Q(operation_key=""),
                name="uniq_acad_decision_operation_key",
            ),
        ]
        indexes = [
            models.Index(
                fields=["target_kind", "target_id", "id"],
                name="acad_decision_target_idx",
            ),
        ]

    def clean(self) -> None:
        super().clean()
        if self.specification_sha256:
            if not re.fullmatch(r"[0-9a-f]{64}", self.specification_sha256):
                raise ValidationError(
                    {"specification_sha256": "specification_sha256 must be 64 lowercase hex characters."}
                )
        if self.action == self.Action.IMPORT and not self.specification_sha256:
            raise ValidationError(
                {"specification_sha256": "IMPORT events require specification_sha256."}
            )
        if not isinstance(self.snapshot, dict):
            raise ValidationError({"snapshot": "snapshot must be a JSON object."})

    def save(self, *args, **kwargs) -> None:
        if not academic_index_state_write_allowed():
            raise ValidationError(_WRITE_REQUIRED)
        if self.pk is not None and self.__class__.objects.filter(pk=self.pk).exists():
            raise ValidationError("AcademicIndexDecisionEvent rows are append-only.")
        self.full_clean()
        super().save(*args, **kwargs)

    def delete(self, *args, **kwargs):
        raise ValidationError("AcademicIndexDecisionEvent rows are append-only.")

    def __str__(self) -> str:
        return (
            "AcademicIndexDecisionEvent("
            f"{self.action}, {self.target_kind}, target_id={self.target_id})"
        )
