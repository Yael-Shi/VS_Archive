from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from documents.services.process_document_request_expired_lease import (
    DEFAULT_EXPIRED_LEASE_FENCE_LIMIT,
    MAX_EXPIRED_LEASE_FENCE_LIMIT,
    ExpiredLeaseFenceOutcome,
    ExpiredLeaseFenceResult,
    fence_process_document_request_expired_lease,
    inspect_process_document_request_expired_lease,
    select_expired_lease_fence_request_ids,
)


class Command(BaseCommand):
    help = (
        "Fence expired RUNNING ProcessDocumentRequest leases to "
        "RECOVERY_REQUIRED. Default is dry-run. --apply never sends SQS or "
        "calls a provider. --apply requires --request-id, --document-id, or "
        "--all-eligible."
    )

    def add_arguments(self, parser):
        parser.add_argument(
            "--request-id",
            action="append",
            type=int,
            dest="request_ids",
            help="Inspect or fence one Request id. Repeat to select multiple.",
        )
        parser.add_argument(
            "--document-id",
            action="append",
            type=int,
            dest="document_ids",
            help="Inspect active Requests for one Document id. Repeat allowed.",
        )
        parser.add_argument(
            "--all-eligible",
            action="store_true",
            help="Select expired RUNNING Requests across all documents.",
        )
        parser.add_argument(
            "--limit",
            type=int,
            default=DEFAULT_EXPIRED_LEASE_FENCE_LIMIT,
            help=(
                "Maximum Requests to inspect or fence. Default: "
                f"{DEFAULT_EXPIRED_LEASE_FENCE_LIMIT}."
            ),
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Commit fencing writes. Default is dry-run.",
        )

    @staticmethod
    def _positive_ids(values: list[int] | None, *, option: str) -> list[int]:
        ids = list(dict.fromkeys(values or []))
        if any(value < 1 for value in ids):
            raise CommandError(f"{option} values must be positive integers.")
        return ids

    @staticmethod
    def _format_row(result: ExpiredLeaseFenceResult) -> str:
        lease = (
            result.lease_expires_at.isoformat()
            if result.lease_expires_at is not None
            else "none"
        )
        parts = [
            f"outcome={result.outcome}",
            f"request_id={result.request_id}",
            f"document_id={result.document_id}",
            f"status={result.status}",
            f"lease_expires_at={lease}",
        ]
        if result.document_processing_state is not None:
            parts.append(
                f"document_processing_state={result.document_processing_state}"
            )
        if result.expired_by_seconds is not None:
            parts.append(f"expired_by_seconds={result.expired_by_seconds}")
        return " ".join(parts)

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
        limit = int(options["limit"])

        if limit < 1 or limit > MAX_EXPIRED_LEASE_FENCE_LIMIT:
            raise CommandError(
                f"--limit must be between 1 and {MAX_EXPIRED_LEASE_FENCE_LIMIT}."
            )
        if request_ids and len(request_ids) > limit:
            raise CommandError(
                "The number of --request-id values cannot exceed --limit."
            )
        if document_ids and len(document_ids) > limit:
            raise CommandError(
                "The number of --document-id values cannot exceed --limit."
            )
        if all_eligible and (request_ids or document_ids):
            raise CommandError(
                "--all-eligible cannot be combined with --request-id or --document-id."
            )
        if apply_mode and not (request_ids or document_ids or all_eligible):
            raise CommandError(
                "--apply requires --request-id, --document-id, or --all-eligible."
            )

        now = timezone.now()
        selected_ids = select_expired_lease_fence_request_ids(
            request_ids=request_ids,
            document_ids=document_ids,
            all_eligible=all_eligible or not (request_ids or document_ids),
            limit=limit,
            now=now,
        )

        mode = "apply" if apply_mode else "dry-run"
        self.stdout.write(f"mode={mode} limit={limit} selected={len(selected_ids)}")

        eligible_count = 0
        fenced_count = 0
        skipped_count = 0
        not_found_count = 0
        error_count = 0

        for request_id in selected_ids:
            try:
                allowed_document_ids = frozenset(document_ids) if document_ids else None
                if apply_mode:
                    result = fence_process_document_request_expired_lease(
                        request_id,
                        now=now,
                        allowed_document_ids=allowed_document_ids,
                    )
                else:
                    result = inspect_process_document_request_expired_lease(
                        request_id,
                        now=now,
                        allowed_document_ids=allowed_document_ids,
                    )
            except Exception as exc:
                error_count += 1
                self.stdout.write(
                    f"outcome={ExpiredLeaseFenceOutcome.ERROR} "
                    f"request_id={request_id} document_id=None "
                    f"status=None lease_expires_at=none "
                    f"error_class={type(exc).__name__}"
                )
                continue

            self.stdout.write(self._format_row(result))
            if result.outcome == ExpiredLeaseFenceOutcome.ELIGIBLE:
                eligible_count += 1
            elif result.outcome == ExpiredLeaseFenceOutcome.FENCED:
                fenced_count += 1
            elif result.outcome == ExpiredLeaseFenceOutcome.NOT_FOUND:
                not_found_count += 1
            elif result.outcome == ExpiredLeaseFenceOutcome.ERROR:
                error_count += 1
            else:
                skipped_count += 1

        self.stdout.write(
            f"summary selected={len(selected_ids)} eligible={eligible_count} "
            f"fenced={fenced_count} skipped={skipped_count} "
            f"not_found={not_found_count} errors={error_count}"
        )
        if not apply_mode:
            self.stdout.write("no changes made (dry-run)")
        elif error_count:
            raise CommandError(f"{error_count} Request fence attempt(s) failed.")
        else:
            self.stdout.write(
                self.style.SUCCESS("PROCESS_DOCUMENT expired-lease fencing complete")
            )
