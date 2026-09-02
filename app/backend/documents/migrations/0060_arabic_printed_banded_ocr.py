import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):
    dependencies = [
        ("documents", "0059_backfill_authors_from_author_name"),
    ]

    operations = [
        migrations.CreateModel(
            name="ArabicPrintedOcrAttempt",
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
                        related_name="arabic_printed_ocr_attempts",
                        to="documents.document",
                    ),
                ),
            ],
        ),
        migrations.CreateModel(
            name="ArabicPrintedOcrPageCheckpoint",
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
                ("page_index", models.IntegerField()),
                ("page_fingerprint", models.CharField(max_length=64)),
                ("source_content_fingerprint", models.CharField(max_length=64)),
                ("oriented_image_sha256", models.CharField(max_length=64)),
                ("oriented_image_width", models.IntegerField()),
                ("oriented_image_height", models.IntegerField()),
                (
                    "cloud_vision_response_sha256",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("cloud_vision_call_count", models.PositiveSmallIntegerField(default=0)),
                ("banding_contract_fingerprint", models.CharField(max_length=64)),
                ("banding_strategy", models.CharField(max_length=64)),
                ("band_count", models.PositiveSmallIntegerField(default=0)),
                (
                    "max_band_height_ratio",
                    models.DecimalField(
                        decimal_places=3, default="0.350", max_digits=4
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PLANNING", "Planning"),
                            ("RUNNING", "Running"),
                            ("SUCCEEDED", "Succeeded"),
                            ("FAILED", "Failed"),
                        ],
                        default="PLANNING",
                        max_length=32,
                    ),
                ),
                ("lease_token", models.UUIDField(blank=True, null=True)),
                ("lease_expires_at", models.DateTimeField(blank=True, null=True)),
                ("assembled_text", models.TextField(blank=True, null=True)),
                (
                    "page_quality",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("UNASSISTED", "Unassisted"),
                            ("ASSISTED", "Assisted"),
                            ("MIXED", "Mixed"),
                            (
                                "CLOUD_VISION_LOW_QUALITY",
                                "Cloud Vision low quality",
                            ),
                        ],
                        default="",
                        max_length=32,
                    ),
                ),
                (
                    "runtime_engine_marker",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "antigravity_create_count",
                    models.PositiveSmallIntegerField(default=0),
                ),
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
                (
                    "attempt",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="page_checkpoints",
                        to="documents.arabicprintedocrattempt",
                    ),
                ),
            ],
            options={"ordering": ["page_index"]},
        ),
        migrations.CreateModel(
            name="ArabicPrintedOcrBandCheckpoint",
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
                ("band_index", models.IntegerField()),
                ("rect_x", models.IntegerField()),
                ("rect_y", models.IntegerField()),
                ("rect_width", models.IntegerField()),
                ("rect_height", models.IntegerField()),
                ("crop_mime", models.CharField(max_length=64)),
                ("crop_byte_length", models.PositiveIntegerField()),
                ("crop_sha256", models.CharField(max_length=64)),
                ("vision_draft_text", models.TextField()),
                ("vision_draft_byte_length", models.PositiveIntegerField()),
                ("vision_draft_sha256", models.CharField(max_length=64)),
                (
                    "selected_result",
                    models.CharField(
                        blank=True,
                        choices=[
                            ("UNASSISTED", "Unassisted"),
                            ("ASSISTED_FALLBACK", "Assisted fallback"),
                            (
                                "CLOUD_VISION_LOW_QUALITY",
                                "Cloud Vision low quality",
                            ),
                        ],
                        default="",
                        max_length=32,
                    ),
                ),
                (
                    "status",
                    models.CharField(
                        choices=[
                            ("PENDING", "Pending"),
                            ("PRIMARY_RUNNING", "Primary running"),
                            ("CANCEL_PENDING", "Cancel pending"),
                            ("FALLBACK_RUNNING", "Fallback running"),
                            ("SUCCEEDED", "Succeeded"),
                            ("FAILED", "Failed"),
                        ],
                        default="PENDING",
                        max_length=32,
                    ),
                ),
                (
                    "primary_interaction_id",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                (
                    "primary_provider_status",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "primary_latency_ms",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                (
                    "primary_failure_type",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "primary_safe_diagnostics",
                    models.CharField(blank=True, default="", max_length=512),
                ),
                (
                    "fallback_interaction_id",
                    models.CharField(blank=True, default="", max_length=128),
                ),
                (
                    "fallback_provider_status",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "fallback_latency_ms",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                (
                    "fallback_failure_type",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "fallback_safe_diagnostics",
                    models.CharField(blank=True, default="", max_length=512),
                ),
                ("cancel_attempted", models.BooleanField(default=False)),
                (
                    "cancel_attempted_at",
                    models.DateTimeField(blank=True, null=True),
                ),
                ("cancel_http_status", models.IntegerField(blank=True, null=True)),
                (
                    "cancel_confirmed_status",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                (
                    "cancel_safe_diagnostics",
                    models.CharField(blank=True, default="", max_length=512),
                ),
                ("prior_attempts", models.JSONField(default=list)),
                ("transcription_text", models.TextField(blank=True, null=True)),
                (
                    "transcription_byte_length",
                    models.PositiveIntegerField(blank=True, null=True),
                ),
                (
                    "transcription_sha256",
                    models.CharField(blank=True, default="", max_length=64),
                ),
                ("create_call_count", models.PositiveSmallIntegerField(default=0)),
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
                ("completed_at", models.DateTimeField(blank=True, null=True)),
                (
                    "page_checkpoint",
                    models.ForeignKey(
                        on_delete=django.db.models.deletion.CASCADE,
                        related_name="band_checkpoints",
                        to="documents.arabicprintedocrpagecheckpoint",
                    ),
                ),
            ],
            options={"ordering": ["band_index"]},
        ),
        migrations.AddIndex(
            model_name="arabicprintedocrattempt",
            index=models.Index(
                fields=["document", "-created_at"],
                name="ar_pr_ocr_attempt_doc_idx",
            ),
        ),
        migrations.AddIndex(
            model_name="arabicprintedocrattempt",
            index=models.Index(
                fields=["document", "status"],
                name="ar_pr_ocr_attempt_status_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrattempt",
            constraint=models.UniqueConstraint(
                fields=("document", "identity_fingerprint"),
                name="uniq_ar_pr_ocr_attempt_identity",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(("expected_page_count__gte", 1)),
                name="ar_pr_ocr_attempt_page_count_gte_1",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    ("status__in", ["IN_PROGRESS", "PARTIAL", "COMPLETED"])
                ),
                name="ar_pr_ocr_attempt_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "COMPLETED"), _negated=True)
                | (
                    models.Q(("completed_at__isnull", False))
                    & models.Q(("missing_page_indices", []))
                ),
                name="ar_pr_ocr_attempt_completed_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrattempt",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "COMPLETED"))
                | models.Q(("completed_at__isnull", True)),
                name="ar_pr_ocr_attempt_noncompleted_shape",
            ),
        ),
        migrations.AddIndex(
            model_name="arabicprintedocrpagecheckpoint",
            index=models.Index(
                fields=["attempt", "status"],
                name="ar_pr_ocr_page_status_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrpagecheckpoint",
            constraint=models.UniqueConstraint(
                fields=("attempt", "page_index"),
                name="uniq_ar_pr_ocr_attempt_page",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrpagecheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("page_index__gte", 0)),
                name="ar_pr_ocr_page_index_gte_0",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrpagecheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("oriented_image_width__gte", 1))
                & models.Q(("oriented_image_height__gte", 1)),
                name="ar_pr_ocr_page_oriented_dims",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrpagecheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("cloud_vision_call_count__lte", 1)),
                name="ar_pr_ocr_page_vision_calls",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrpagecheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("antigravity_create_count__lte", 12)),
                name="ar_pr_ocr_page_ag_creates",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrpagecheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("band_count__lte", 6)),
                name="ar_pr_ocr_page_band_count_lte_6",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrpagecheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "status__in",
                        ["PLANNING", "RUNNING", "SUCCEEDED", "FAILED"],
                    )
                ),
                name="ar_pr_ocr_page_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrpagecheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "PLANNING"), _negated=True)
                | (
                    models.Q(("lease_token__isnull", True))
                    & models.Q(("lease_expires_at__isnull", True))
                    & models.Q(("completed_at__isnull", True))
                    & models.Q(("assembled_text__isnull", True))
                    & models.Q(("page_quality", ""))
                    & models.Q(("runtime_engine_marker", ""))
                    & models.Q(("failure_code", ""))
                    & models.Q(("failure_message", ""))
                ),
                name="ar_pr_ocr_page_planning_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrpagecheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "RUNNING"), _negated=True)
                | (
                    models.Q(("lease_token__isnull", False))
                    & models.Q(("lease_expires_at__isnull", False))
                    & models.Q(("completed_at__isnull", True))
                    & models.Q(("assembled_text__isnull", True))
                    & models.Q(("page_quality", ""))
                    & models.Q(("runtime_engine_marker", ""))
                    & models.Q(("failure_code", ""))
                    & models.Q(("failure_message", ""))
                ),
                name="ar_pr_ocr_page_running_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrpagecheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "SUCCEEDED"), _negated=True)
                | (
                    models.Q(("lease_token__isnull", True))
                    & models.Q(("lease_expires_at__isnull", True))
                    & models.Q(("completed_at__isnull", False))
                    & models.Q(("assembled_text__isnull", False))
                    & models.Q(("assembled_text", ""), _negated=True)
                    & models.Q(
                        (
                            "page_quality__in",
                            [
                                "UNASSISTED",
                                "ASSISTED",
                                "MIXED",
                                "CLOUD_VISION_LOW_QUALITY",
                            ],
                        )
                    )
                    & models.Q(("runtime_engine_marker", ""), _negated=True)
                    & models.Q(("failure_code", ""))
                    & models.Q(("failure_message", ""))
                    & models.Q(("band_count__gte", 1))
                ),
                name="ar_pr_ocr_page_succeeded_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrpagecheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "FAILED"), _negated=True)
                | (
                    models.Q(("lease_token__isnull", True))
                    & models.Q(("lease_expires_at__isnull", True))
                    & models.Q(("completed_at__isnull", False))
                    & models.Q(("assembled_text__isnull", True))
                    & models.Q(("page_quality", ""))
                    & models.Q(("runtime_engine_marker", ""))
                    & models.Q(("failure_code", ""), _negated=True)
                ),
                name="ar_pr_ocr_page_failed_shape",
            ),
        ),
        migrations.AddIndex(
            model_name="arabicprintedocrbandcheckpoint",
            index=models.Index(
                fields=["page_checkpoint", "status"],
                name="ar_pr_ocr_band_status_idx",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrbandcheckpoint",
            constraint=models.UniqueConstraint(
                fields=("page_checkpoint", "band_index"),
                name="uniq_ar_pr_ocr_page_band",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrbandcheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("band_index__gte", 0)),
                name="ar_pr_ocr_band_index_gte_0",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrbandcheckpoint",
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(("rect_x__gte", 0))
                    & models.Q(("rect_y__gte", 0))
                    & models.Q(("rect_width__gte", 1))
                    & models.Q(("rect_height__gte", 1))
                ),
                name="ar_pr_ocr_band_rect_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrbandcheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("create_call_count__lte", 2)),
                name="ar_pr_ocr_band_create_count",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrbandcheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(
                    (
                        "status__in",
                        [
                            "PENDING",
                            "PRIMARY_RUNNING",
                            "CANCEL_PENDING",
                            "FALLBACK_RUNNING",
                            "SUCCEEDED",
                            "FAILED",
                        ],
                    )
                ),
                name="ar_pr_ocr_band_status_valid",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrbandcheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "SUCCEEDED"), _negated=True)
                | (
                    models.Q(
                        (
                            "selected_result__in",
                            [
                                "UNASSISTED",
                                "ASSISTED_FALLBACK",
                                "CLOUD_VISION_LOW_QUALITY",
                            ],
                        )
                    )
                    & models.Q(("transcription_text__isnull", False))
                    & models.Q(("transcription_text", ""), _negated=True)
                    & models.Q(("transcription_byte_length__isnull", False))
                    & models.Q(("transcription_sha256", ""), _negated=True)
                    & models.Q(("completed_at__isnull", False))
                    & models.Q(("failure_code", ""))
                ),
                name="ar_pr_ocr_band_succeeded_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrbandcheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "SUCCEEDED"))
                | (
                    models.Q(("selected_result", ""))
                    & models.Q(("transcription_text__isnull", True))
                    & models.Q(("transcription_sha256", ""))
                    & models.Q(("transcription_byte_length__isnull", True))
                ),
                name="ar_pr_ocr_band_nonsuccess_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrbandcheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "FAILED"), _negated=True)
                | (
                    models.Q(("completed_at__isnull", False))
                    & models.Q(("failure_code", ""), _negated=True)
                ),
                name="ar_pr_ocr_band_failed_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrbandcheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "CANCEL_PENDING"), _negated=True)
                | (
                    models.Q(("cancel_attempted", True))
                    & models.Q(("cancel_attempted_at__isnull", False))
                ),
                name="ar_pr_ocr_band_cancel_shape",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrbandcheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "PRIMARY_RUNNING"), _negated=True)
                | models.Q(("create_call_count", 1)),
                name="ar_pr_ocr_band_primary_count",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrbandcheckpoint",
            constraint=models.CheckConstraint(
                condition=models.Q(("status", "FALLBACK_RUNNING"), _negated=True)
                | models.Q(("create_call_count", 2)),
                name="ar_pr_ocr_band_fallback_count",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrbandcheckpoint",
            constraint=models.CheckConstraint(
                condition=~(
                    models.Q(("status", "SUCCEEDED"))
                    & models.Q(("selected_result", "UNASSISTED"))
                )
                | models.Q(("create_call_count", 1)),
                name="ar_pr_ocr_band_unassisted_count",
            ),
        ),
        migrations.AddConstraint(
            model_name="arabicprintedocrbandcheckpoint",
            constraint=models.CheckConstraint(
                condition=~(
                    models.Q(("status", "SUCCEEDED"))
                    & models.Q(("selected_result", "ASSISTED_FALLBACK"))
                )
                | models.Q(("create_call_count", 2)),
                name="ar_pr_ocr_band_assisted_count",
            ),
        ),
    ]
