from __future__ import annotations

from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

from documents.services.document_access import is_document_admin
from documents.services.transkribus_hover_geometry_repair import (
    HOVER_GEOMETRY_REPAIR_TARGETS,
    HoverGeometryRepairError,
    apply_hover_geometry_repair,
    plan_hover_geometry_repair,
)


class Command(BaseCommand):
    help = (
        "Plan or apply a parser-v2 hover-geometry rebind for document 358 or 393. "
        "Default is read-only. --apply stores a new snapshot from the PAGE XML "
        "already in S3 and rebinds. It does not fetch Transkribus or edit text."
    )

    def add_arguments(self, parser):
        parser.add_argument("--document-id", type=int, required=True)
        parser.add_argument("--snapshot-id", type=int, required=True)
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Store the replacement snapshot and rebind. Default is dry-run.",
        )
        parser.add_argument(
            "--initiated-by-user-id",
            type=int,
            default=None,
            help="Active document-admin user id. Required with --apply.",
        )

    def handle(self, *args, **options):
        document_id = int(options["document_id"])
        snapshot_id = int(options["snapshot_id"])
        apply_mode = bool(options["apply"])
        user_id = options["initiated_by_user_id"]
        allowed = ", ".join(
            f"{doc_id}->{snap_id}"
            for doc_id, snap_id in sorted(HOVER_GEOMETRY_REPAIR_TARGETS.items())
        )
        self.stdout.write(f"allowed_repairs={allowed}")

        try:
            plan = plan_hover_geometry_repair(
                document_id=document_id,
                snapshot_id=snapshot_id,
            )
        except HoverGeometryRepairError as exc:
            raise CommandError(f"{exc.code}: {exc}") from None

        self.stdout.write(f"outcome={plan.outcome}")
        self.stdout.write(f"document_id={plan.document_id}")
        self.stdout.write(f"source_snapshot_id={plan.source_snapshot_id}")
        self.stdout.write(f"source_parser_version={plan.source_parser_version}")
        self.stdout.write(f"proposed_parser_version={plan.proposed_parser_version}")
        self.stdout.write(f"source_result_id={plan.source_result_id}")
        self.stdout.write(f"hebrew_result_id={plan.hebrew_result_id}")
        self.stdout.write(f"source_binding_id={plan.source_binding_id}")
        self.stdout.write(f"hebrew_binding_id={plan.hebrew_binding_id}")
        self.stdout.write(f"source_revision={plan.source_revision}")
        self.stdout.write(
            f"based_on_source_revision={plan.based_on_source_revision}"
        )
        self.stdout.write(f"bound_source_revision={plan.bound_source_revision}")
        self.stdout.write(
            f"source_verification_status={plan.source_verification_status}"
        )
        self.stdout.write(
            f"hebrew_verification_status={plan.hebrew_verification_status}"
        )
        self.stdout.write(f"canonical_text_sha256={plan.canonical_text_sha256}")
        self.stdout.write(f"source_text_sha256={plan.source_text_sha256}")
        self.stdout.write(f"hebrew_text_sha256={plan.hebrew_text_sha256}")
        self.stdout.write(f"canonical_text_chars={plan.canonical_text_chars}")
        self.stdout.write(f"geometry_capability={plan.geometry_capability}")
        self.stdout.write(f"hover_eligible={plan.hover_eligible}")
        self.stdout.write(
            f"paragraph_mapping_id={plan.paragraph_mapping_id}"
        )
        self.stdout.write(
            f"replacement_snapshot_id={plan.replacement_snapshot_id}"
        )
        for warning in plan.normalized_polygon_warnings:
            self.stdout.write(f"normalization={warning}")

        if not apply_mode:
            self.stdout.write("dry_run=true")
            return

        if user_id is None:
            raise CommandError("--initiated-by-user-id is required with --apply.")
        User = get_user_model()
        try:
            actor = User.objects.get(pk=int(user_id))
        except User.DoesNotExist:
            raise CommandError(f"User id={user_id} does not exist.") from None
        if not actor.is_active or not is_document_admin(actor):
            raise CommandError(
                f"User id={user_id} is not an active document administrator."
            )

        try:
            result = apply_hover_geometry_repair(plan=plan, actor=actor)
        except HoverGeometryRepairError as exc:
            raise CommandError(f"{exc.code}: {exc}") from None

        self.stdout.write(f"apply_outcome={result.outcome}")
        self.stdout.write(
            f"replacement_snapshot_id={result.replacement_snapshot_id}"
        )
        self.stdout.write(f"source_revision={result.source_revision}")
        self.stdout.write(
            f"source_verification_status={result.source_verification_status}"
        )
        self.stdout.write(
            f"hebrew_verification_status={result.hebrew_verification_status}"
        )
