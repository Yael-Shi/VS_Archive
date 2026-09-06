from __future__ import annotations

from django.core.management.base import BaseCommand, CommandError

from documents.services.arabic_printed_ambiguous_fence_resolution import (
    ArabicPrintedAmbiguousFenceResolutionError,
    apply_arabic_printed_ambiguous_fence_resolution,
    plan_arabic_printed_ambiguous_fence_resolution,
)


class Command(BaseCommand):
    help = (
        "Resolve a reviewed Arabic printed Vision or Antigravity-create "
        "ambiguous fence. Default is dry-run. --apply writes the minimum "
        "checkpoint fields only. Does not call providers, SQS, or OCR."
    )

    def add_arguments(self, parser):
        parser.add_argument("document_id", type=int, help="Document id.")
        parser.add_argument(
            "--page-index",
            type=int,
            required=True,
            help="Zero-based page index on the current-contract attempt.",
        )
        parser.add_argument(
            "--band-index",
            type=int,
            default=None,
            help="Zero-based band index. Required for primary/fallback fences.",
        )
        parser.add_argument(
            "--mode",
            required=True,
            choices=("no-provider-call", "bind-interaction"),
            help="Explicit resolution mode.",
        )
        parser.add_argument(
            "--expected-failure-code",
            required=True,
            help=(
                "Exact current fence code: ARABIC_PRINTED_VISION_AMBIGUOUS, "
                "ARABIC_PRINTED_PRIMARY_AMBIGUOUS, or "
                "ARABIC_PRINTED_FALLBACK_AMBIGUOUS."
            ),
        )
        parser.add_argument(
            "--interaction-id",
            default="",
            help="Provider interaction id for bind-interaction only.",
        )
        parser.add_argument(
            "--reason",
            required=True,
            help="Operator audit reason (required, persisted when a field exists).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Persist the planned writes. Default is dry-run.",
        )

    def handle(self, *args, **options):
        document_id = int(options["document_id"])
        page_index = int(options["page_index"])
        band_index = options["band_index"]
        if band_index is not None:
            band_index = int(band_index)
        mode = str(options["mode"])
        expected_failure_code = str(options["expected_failure_code"])
        interaction_id = str(options["interaction_id"] or "").strip() or None
        reason = str(options["reason"])
        apply_mode = bool(options["apply"])

        try:
            if apply_mode:
                plan = apply_arabic_printed_ambiguous_fence_resolution(
                    document_id=document_id,
                    page_index=page_index,
                    band_index=band_index,
                    mode=mode,
                    expected_failure_code=expected_failure_code,
                    interaction_id=interaction_id,
                    reason=reason,
                )
            else:
                plan = plan_arabic_printed_ambiguous_fence_resolution(
                    document_id=document_id,
                    page_index=page_index,
                    band_index=band_index,
                    mode=mode,
                    expected_failure_code=expected_failure_code,
                    interaction_id=interaction_id,
                    reason=reason,
                )
        except ArabicPrintedAmbiguousFenceResolutionError as exc:
            raise CommandError(exc.message) from exc

        mode_label = "apply" if plan.applied else "dry-run"
        self.stdout.write(
            f"mode={mode_label} resolution={plan.mode} target={plan.target} "
            f"document_id={plan.document_id} attempt_id={plan.attempt_id} "
            f"page_index={plan.page_index} band_index={plan.band_index} "
            f"expected_failure_code={plan.expected_failure_code} "
            f"audit_field={plan.audit_field}"
        )
        for change in plan.changes:
            self.stdout.write(
                f"  {change.model}.{change.field}: {change.before!r} -> {change.after!r}"
            )
        if not plan.applied:
            self.stdout.write("no changes made (dry-run)")
        else:
            self.stdout.write(self.style.SUCCESS("ambiguous fence resolution applied"))
