# Generated manually for corrected/current sync queue foundation (PR1).

import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0043_archiveitemsearchindex_hebrew_translation_text"),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name="TranskribusCorrectedCurrentSyncRequest",
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
                    "status",
                    models.CharField(
                        choices=[
                            ("QUEUED", "Queued"),
                            ("RUNNING", "Running"),
                            ("RECOVERY_REQUIRED", "Recovery required"),
                            ("COMPLETED", "Completed"),
                            ("REFUSED", "Refused"),
                            ("FAILED", "Failed"),
                            ("ENQUEUE_FAILED", "Enqueue failed"),
                        ],
                        max_length=32,
                    ),
                ),
                ("lease_token", models.UUIDField(blank=True, null=True)),
                ("lease_expires_at", models.DateTimeField(blank=True, null=True)),
                (
                    "failure_code",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "failure_message",
                    models.CharField(blank=True, default="", max_length=512),
                ),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("started_at", models.DateTimeField(blank=True, null=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                ("last_enqueued_at", models.DateTimeField(blank=True, null=True)),
                (
                    "attempt",
                    models.OneToOneField(
                        blank=True,
                        help_text=(
                            "Linked sync attempt once worker correlation succeeds. "
                            "RESTRICT preserves request provenance; delete the "
                            "request (or its document) before deleting a referenced "
                            "attempt."
                        ),
                        null=True,
                        on_delete=django.db.models.deletion.RESTRICT,
                        related_name="corrected_current_sync_request",
                        to="documents.transkribuscorrectedcurrentsyncattempt",
                    ),
                ),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="transkribus_corrected_current_sync_requests",
                        to="documents.document",
                    ),
                ),
                (
                    "initiated_by",
                    models.ForeignKey(
                        blank=True,
                        null=True,
                        on_delete=django.db.models.deletion.SET_NULL,
                        related_name="transkribus_corrected_current_sync_requests",
                        to=settings.AUTH_USER_MODEL,
                    ),
                ),
            ],
            options={
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddIndex(
            model_name="transkribuscorrectedcurrentsyncrequest",
            index=models.Index(
                fields=["document", "-created_at"],
                name="tr_cc_sync_req_doc_created_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="transkribuscorrectedcurrentsyncrequest",
            index=models.Index(
                fields=["document", "status"],
                name="tr_cc_sync_req_doc_status_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="transkribuscorrectedcurrentsyncrequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "status__in",
                        [
                            "QUEUED",
                            "RUNNING",
                            "RECOVERY_REQUIRED",
                            "COMPLETED",
                            "REFUSED",
                            "FAILED",
                            "ENQUEUE_FAILED",
                        ],
                    )
                ),
                name="tr_cc_sync_req_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="transkribuscorrectedcurrentsyncrequest",
            constraint=models.UniqueConstraint(
                condition=models.Q(
                    (
                        "status__in",
                        [
                            "QUEUED",
                            "RUNNING",
                            "RECOVERY_REQUIRED",
                            "ENQUEUE_FAILED",
                        ],
                    )
                ),
                fields=("document",),
                name="uniq_tr_cc_sync_req_active_doc",
            ),
        ),
        migrations.AddConstraint(
            model_name="transkribuscorrectedcurrentsyncrequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("status__in", ["QUEUED", "ENQUEUE_FAILED"]), _negated=True
                    ),
                    models.Q(
                        ("lease_token__isnull", True),
                        ("lease_expires_at__isnull", True),
                        ("started_at__isnull", True),
                        ("completed_at__isnull", True),
                        ("attempt__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="tr_cc_sync_req_queued_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="transkribuscorrectedcurrentsyncrequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "RUNNING"), _negated=True),
                    models.Q(
                        ("lease_token__isnull", False),
                        ("lease_expires_at__isnull", False),
                        ("started_at__isnull", False),
                        ("completed_at__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="tr_cc_sync_req_running_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="transkribuscorrectedcurrentsyncrequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "RECOVERY_REQUIRED"), _negated=True),
                    models.Q(
                        ("attempt__isnull", False),
                        ("lease_token__isnull", False),
                        ("lease_expires_at__isnull", True),
                        ("started_at__isnull", False),
                        ("completed_at__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="tr_cc_sync_req_recovery_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="transkribuscorrectedcurrentsyncrequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status__in", ["COMPLETED", "REFUSED"]), _negated=True),
                    models.Q(
                        ("attempt__isnull", False),
                        ("completed_at__isnull", False),
                    ),
                    _connector="OR",
                ),
                name="tr_cc_sync_req_success_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="transkribuscorrectedcurrentsyncrequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(("status", "FAILED"), _negated=True),
                    models.Q(
                        ("completed_at__isnull", False),
                        models.Q(("failure_code", ""), _negated=True),
                    ),
                    _connector="OR",
                ),
                name="tr_cc_sync_req_failed_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="transkribuscorrectedcurrentsyncrequest",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    models.Q(
                        ("status__in", ["COMPLETED", "REFUSED", "FAILED"]),
                        _negated=True,
                    ),
                    models.Q(
                        ("lease_token__isnull", True),
                        ("lease_expires_at__isnull", True),
                    ),
                    _connector="OR",
                ),
                name="tr_cc_sync_req_terminal_no_lease",
            ),
        ),
    ]
