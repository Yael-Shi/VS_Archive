from __future__ import annotations

import os
from datetime import datetime, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from documents.models import ProcessDocumentRequest
from documents.services.process_document_request_recovery import (
    DEFAULT_RECOVERY_MINIMUM_AGE,
    ProcessDocumentRequestRecoveryError,
    assess_process_document_request_recovery,
    process_document_recovery_candidates,
    recover_process_document_request,
)


class Command(BaseCommand):
    help = (
        "Report or requeue stranded durable PROCESS_DOCUMENT Requests. "
        "Default is dry-run. --apply requires an explicit scope or --all-eligible."
    )

    def add_arguments(self, parser):
        scope = parser.add_mutually_exclusive_group()
        scope.add_argument(
            "--request-id",
            action="append",
            type=int,
            dest="request_ids",
            help="Restrict to one Request id. Repeat to select multiple Requests.",
        )
        scope.add_argument(
            "--document-id",
            action="append",
            type=int,
            dest="document_ids",
            help="Restrict to one Document id. Repeat to select multiple Documents.",
        )
        scope.add_argument(
            "--all-eligible",
            action="store_true",
            help="Explicitly authorize an apply run across all eligible Requests.",
        )
        parser.add_argument(
            "--older-than-minutes",
            type=int,
            default=int(DEFAULT_RECOVERY_MINIMUM_AGE.total_seconds() // 60),
            help=("Require the Request to be at least this old. Default: 15 minutes."),
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=100,
            help="Maximum Requests to inspect or recover. Default: 100.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Reserve and resend eligible Requests. Default is dry-run.",
        )

    @staticmethod
    def _positive_ids(values: list[int] | None, *, option: str) -> list[int]:
        ids = list(dict.fromkeys(values or []))
        if any(value < 1 for value in ids):
            raise CommandError(f"{option} values must be positive integers.")
        return ids

    def _selected_request_ids(
        self,
        *,
        request_ids: list[int],
        document_ids: list[int],
        now: datetime,
        minimum_age: timedelta,
        limit: int,
    ) -> list[int]:
        if request_ids:
            existing = set(
                ProcessDocumentRequest.objects.filter(pk__in=request_ids).values_list(
                    "pk",
                    flat=True,
                )
            )
            missing = sorted(set(request_ids) - existing)
            if missing:
                missing_text = ",".join(str(value) for value in missing)
                raise CommandError(f"Request ids not found: {missing_text}")
            queryset = ProcessDocumentRequest.objects.filter(pk__in=request_ids)
        elif document_ids:
            queryset = ProcessDocumentRequest.objects.filter(
                document_id__in=document_ids,
                status__in=(
                    ProcessDocumentRequest.Status.QUEUED,
                    ProcessDocumentRequest.Status.RUNNING,
                    ProcessDocumentRequest.Status.RECOVERY_REQUIRED,
                    ProcessDocumentRequest.Status.ENQUEUE_FAILED,
                ),
            )
        else:
            queryset = process_document_recovery_candidates(
                now=now,
                minimum_age=minimum_age,
            )

        return list(queryset.order_by("pk").values_list("pk", flat=True)[:limit])

    def handle(self, *args, **options):
        apply_mode = bool(options["apply"])
        all_eligible = bool(options["all_eligible"])
        request_ids = self._positive_ids(
            options.get("request_ids"),
            option="--request-id",
        )
        document_ids = self._positive_ids(
            options.get("document_ids"),
            option="--document-id",
        )
        older_than_minutes = int(options["older_than_minutes"])
        limit = int(options["limit"])

        if older_than_minutes < 1:
            raise CommandError("--older-than-minutes must be at least 1.")
        if limit < 1 or limit > 1000:
            raise CommandError("--limit must be between 1 and 1000.")
        if request_ids and len(request_ids) > limit:
            raise CommandError(
                "The number of --request-id values cannot exceed --limit."
            )
        if document_ids and len(document_ids) > limit:
            raise CommandError(
                "The number of --document-id values cannot exceed --limit."
            )
        if apply_mode and not (request_ids or document_ids or all_eligible):
            raise CommandError(
                "--apply requires --request-id, --document-id, or --all-eligible."
            )

        minimum_age = timedelta(minutes=older_than_minutes)
        now = timezone.now()
        collection_id = os.getenv("TRANSKRIBUS_COLLECTION_ID") or ""
        model_id = os.getenv("TRANSKRIBUS_MODEL_ID") or ""
        selected_ids = self._selected_request_ids(
            request_ids=request_ids,
            document_ids=document_ids,
            now=now,
            minimum_age=minimum_age,
            limit=limit,
        )

        mode = "apply" if apply_mode else "dry-run"
        self.stdout.write(
            f"mode={mode} older_than_minutes={older_than_minutes} "
            f"limit={limit} selected={len(selected_ids)}"
        )

        eligible_count = 0
        handled_count = 0
        skipped_count = 0
        send_failure_count = 0

        for request_id in selected_ids:
            try:
                if apply_mode:
                    result = recover_process_document_request(
                        request_id,
                        now=now,
                        minimum_age=minimum_age,
                        collection_id=collection_id,
                        model_id=model_id,
                    )
                    assessment = result.assessment
                else:
                    result = None
                    assessment = assess_process_document_request_recovery(
                        request_id,
                        now=now,
                        minimum_age=minimum_age,
                        collection_id=collection_id,
                        model_id=model_id,
                    )
            except ProcessDocumentRequestRecoveryError as exc:
                raise CommandError(f"recovery error: {exc}") from exc

            request = assessment.request
            age_seconds = int(assessment.age.total_seconds())
            if not assessment.eligible:
                skipped_count += 1
                self.stdout.write(
                    f"request_id={request.pk} document_id={request.document_id} "
                    f"status={request.status} age_seconds={age_seconds} "
                    f"eligible=false reason={assessment.reason} action=skipped"
                )
                continue

            eligible_count += 1
            if not apply_mode:
                self.stdout.write(
                    f"request_id={request.pk} document_id={request.document_id} "
                    f"status={request.status} age_seconds={age_seconds} "
                    f"eligible=true reason={assessment.reason} action=would_requeue"
                )
                continue

            assert result is not None
            assert result.enqueue_result is not None
            enqueue_result = result.enqueue_result
            if enqueue_result.outcome in {
                "REENQUEUED",
                "ALREADY_QUEUED",
                "ALREADY_RUNNING",
                "ALREADY_TERMINAL",
                "BLOCKED_RECOVERY_REQUIRED",
            }:
                handled_count += 1
            elif enqueue_result.outcome in {
                "ENQUEUE_FAILED",
                "ENQUEUE_OUTCOME_UNKNOWN",
            }:
                send_failure_count += 1
            else:
                raise AssertionError(
                    "Unhandled PROCESS_DOCUMENT recovery enqueue outcome: "
                    f"{enqueue_result.outcome}"
                )

            self.stdout.write(
                f"request_id={request.pk} document_id={request.document_id} "
                f"status_before={request.status} age_seconds={age_seconds} "
                f"eligible=true reason={assessment.reason} action=requeue "
                f"enqueue_outcome={enqueue_result.outcome} "
                f"observed_status={enqueue_result.observed_status}"
            )

        self.stdout.write(
            f"summary selected={len(selected_ids)} eligible={eligible_count} "
            f"handled={handled_count} skipped={skipped_count} "
            f"send_failures={send_failure_count}"
        )
        if not apply_mode:
            self.stdout.write("no changes made (dry-run)")
        elif send_failure_count:
            raise CommandError(
                f"{send_failure_count} Request send attempt(s) failed "
                "or were ambiguous."
            )
        else:
            self.stdout.write(self.style.SUCCESS("PROCESS_DOCUMENT recovery complete"))
