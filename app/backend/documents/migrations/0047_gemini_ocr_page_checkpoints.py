import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0046_process_document_request_partial_status"),
    ]

    operations = [
        migrations.CreateModel(
            name="GeminiOcrAttempt",
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
                ("identity_fingerprint", models.CharField(max_length=64)),
                ("source_fingerprint", models.CharField(max_length=64)),
                ("route_fingerprint", models.CharField(max_length=64)),
                ("prompt_fingerprint", models.CharField(max_length=64)),
                ("config_fingerprint", models.CharField(max_length=64)),
                ("prompt_contract_version", models.CharField(max_length=64)),
                ("model_candidates", models.JSONField(default=list)),
                ("expected_page_count", models.PositiveIntegerField()),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("IN_PROGRESS", "In progress"),
                            ("PARTIAL", "Partial"),
                            ("COMPLETED", "Completed"),
                        ],
                        default="IN_PROGRESS",
                        max_length=32,
                    ),
                ),
                ("missing_page_indices", models.JSONField(default=list)),
                ("created_at", models.DateTimeField(auto_now_add=True)),
                ("updated_at", models.DateTimeField(auto_now=True)),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "document",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="gemini_ocr_attempts",
                        to="documents.document",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="GeminiOcrPageCheckpoint",
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
                ("page_index", models.PositiveIntegerField()),
                ("page_fingerprint", models.CharField(max_length=64)),
                ("source_content_fingerprint", models.CharField(max_length=64)),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("RUNNING", "Running"),
                            ("SUCCEEDED", "Succeeded"),
                            ("FAILED", "Failed"),
                        ],
                        max_length=32,
                    ),
                ),
                ("lease_token", models.UUIDField(blank=True, null=True)),
                ("lease_expires_at", models.DateTimeField(blank=True, null=True)),
                (
                    "actual_model",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("text", models.TextField(blank=True, null=True)),
                ("needs_review", models.BooleanField(default=False)),
                ("review_reasons", models.JSONField(default=list)),
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
                ("started_at", models.DateTimeField()),
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "attempt",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="page_checkpoints",
                        to="documents.geminiocrattempt",
                    ),
                ),
            ],
            options={"ordering": ["page_index"]},
        ),
        migrations.AddIndex(
            model_name="geminiocrattempt",
            index=models.Index(
                fields=["document", "-created_at"],
                name="gem_ocr_attempt_doc_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="geminiocrattempt",
            index=models.Index(
                fields=["document", "status"],
                name="gem_ocr_attempt_status_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="geminiocrattempt",
            constraint=models.UniqueConstraint(
                fields=("document", "identity_fingerprint"),
                name="uniq_gem_ocr_attempt_identity",
            ),
        ),
        migrations.AddConstraint(
            model_name="geminiocrattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(("expected_page_count__gte", 1)),
                name="gem_ocr_attempt_page_count_gte_1",
            ),
        ),
        migrations.AddConstraint(
            model_name="geminiocrattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ["IN_PROGRESS", "PARTIAL", "COMPLETED"])
                ),
                name="gem_ocr_attempt_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="geminiocrattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "COMPLETED"), _negated=True)
                | (
                    models.Q(("completed_at__isnull", False))
                    & models.Q(("missing_page_indices", []))
                ),
                name="gem_ocr_attempt_completed_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="geminiocrattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "COMPLETED"))
                | models.Q(("completed_at__isnull", True)),
                name="gem_ocr_attempt_noncompleted_shape",
            ),
        ),
        migrations.AddIndex(
            model_name="geminiocrpagecheckpoint",
            index=models.Index(
                fields=["attempt", "status"],
                name="gem_ocr_page_status_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="geminiocrpagecheckpoint",
            constraint=models.UniqueConstraint(
                fields=("attempt", "page_index"),
                name="uniq_gem_ocr_attempt_page",
            ),
        ),
        migrations.AddConstraint(
            model_name="geminiocrpagecheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("page_index__gte", 1)),
                name="gem_ocr_page_index_gte_1",
            ),
        ),
        migrations.AddConstraint(
            model_name="geminiocrpagecheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("status__in", ["RUNNING", "SUCCEEDED", "FAILED"])),
                name="gem_ocr_page_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="geminiocrpagecheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "RUNNING"), _negated=True)
                | (
                    models.Q(("lease_token__isnull", False))
                    & models.Q(("lease_expires_at__isnull", False))
                    & models.Q(("completed_at__isnull", True))
                    & models.Q(("actual_model", ""))
                    & models.Q(("text__isnull", True))
                    & models.Q(("failure_code", ""))
                    & models.Q(("failure_message", ""))
                ),
                name="gem_ocr_page_running_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="geminiocrpagecheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "SUCCEEDED"), _negated=True)
                | (
                    models.Q(("lease_token__isnull", True))
                    & models.Q(("lease_expires_at__isnull", True))
                    & models.Q(("completed_at__isnull", False))
                    & models.Q(("actual_model", ""), _negated=True)
                    & models.Q(("text__isnull", False))
                    & models.Q(("text", ""), _negated=True)
                    & models.Q(("failure_code", ""))
                    & models.Q(("failure_message", ""))
                ),
                name="gem_ocr_page_succeeded_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="geminiocrpagecheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "FAILED"), _negated=True)
                | (
                    models.Q(("lease_token__isnull", True))
                    & models.Q(("lease_expires_at__isnull", True))
                    & models.Q(("completed_at__isnull", False))
                    & models.Q(("actual_model", ""))
                    & models.Q(("text__isnull", True))
                    & models.Q(("failure_code", ""), _negated=True)
                ),
                name="gem_ocr_page_failed_shape",
            ),
        ),
    ]
