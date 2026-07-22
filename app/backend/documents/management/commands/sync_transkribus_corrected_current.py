from __future__ import annotations

import os

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from documents.services.transkribus_corrected_current_sync import (
    CorrectedCurrentSyncError,
    CorrectedCurrentSyncResult,
    run_corrected_current_transkribus_sync,
)


class Command(BaseCommand):
    help = (
        "Run one staff corrected/current Transkribus sync for a Document "
        "(worker environment). Creates a new sync attempt each invocation. "
        "Does not activate the resolved snapshot, update canonical DocumentTextResult, "
        "or enqueue SQS."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--document-id",
            type=int,
            required=True,
            help="VS-Archive Document id to sync (positive integer).",
        )
        parser.add_argument(
            "--initiated-by-user-id",
            type=int,
            required=True,
            help=(
                "Staff user id recorded as initiated_by on the sync attempt "
                "(positive integer)."
            ),
        )

    def handle(self, *args, **options):
        document_id = int(options["document_id"])
        initiated_by_user_id = int(options["initiated_by_user_id"])

        if document_id < 1:
            raise CommandError("--document-id must be a positive integer.")
        if initiated_by_user_id < 1:
            raise CommandError("--initiated-by-user-id must be a positive integer.")

        User = get_user_model()
        try:
            user = User.objects.get(pk=initiated_by_user_id)
        except User.DoesNotExist:
            raise CommandError(
                f"User id={initiated_by_user_id} does not exist."
            ) from None

        if not user.is_active:
            raise CommandError(f"User id={initiated_by_user_id} is inactive.")
        if not user.is_staff:
            raise CommandError(f"User id={initiated_by_user_id} is not staff.")

        username = (os.getenv("TRANSKRIBUS_USERNAME") or "").strip()
        password = (os.getenv("TRANSKRIBUS_PASSWORD") or "").strip()
        bearer_token = (os.getenv("TRANSKRIBUS_API_TOKEN") or "").strip()
        if not username or not password:
            raise CommandError(
                "Missing Transkribus session credentials. Set TRANSKRIBUS_USERNAME "
                "and TRANSKRIBUS_PASSWORD."
            )
        if not bearer_token:
            raise CommandError(
                "Missing Transkribus transcript bearer token. Set TRANSKRIBUS_API_TOKEN."
            )

        try:
            result = run_corrected_current_transkribus_sync(
                document_id=document_id,
                initiated_by=user,
                username=username,
                password=password,
                bearer_token=bearer_token,
            )
        except CorrectedCurrentSyncError as exc:
            raise CommandError(_format_sync_error(exc)) from None

        self._write_result(result)

    def _write_result(self, result: CorrectedCurrentSyncResult) -> None:
        attempt = result.attempt
        self.stdout.write(f"attempt_id={attempt.pk}")
        self.stdout.write(f"status={attempt.status}")
        if result.snapshot is not None:
            self.stdout.write(f"resolved_snapshot_id={result.snapshot.pk}")
        if result.storage_outcome is not None:
            outcome = result.storage_outcome
            outcome_value = getattr(outcome, "value", outcome)
            self.stdout.write(f"storage_outcome={outcome_value}")


def _format_sync_error(exc: CorrectedCurrentSyncError) -> str:
    """Build a CommandError message from safe public sync-error fields only."""
    parts = [str(exc)]
    if exc.attempt_id is not None:
        parts.append(f"attempt_id={exc.attempt_id}")
    if exc.failure_code:
        parts.append(f"failure_code={exc.failure_code}")
    return " ".join(parts)
