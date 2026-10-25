"""Read-only preflight for reconstructed stage 2 occurrence links."""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    Document,
    DocumentTextResult,
    NonPersonEntity,
    NonPersonEntityAlias,
    NonPersonEntityCorrection,
    NonPersonEntityOccurrenceCandidate,
    NonPersonEntityOccurrenceCandidateMatch,
    NonPersonEntityOccurrenceProposal,
    NonPersonEntityOccurrenceReviewEvent,
    ReviewedNonPersonEntityDecision,
)
from documents.services.non_person_entity_bnei_akiva_shadow_correction import (
    OCCURRENCE_IDENTITIES,
    SURVIVOR_ENTITY_ID,
)
from documents.services.non_person_entity_occurrences import (
    SURFACE_V1,
    locate_surface_occurrences,
    normalize_surface_v1,
    source_text_sha256,
)
from documents.services.non_person_entity_review_preflight import (
    THIRD_DECISION_SOURCE,
)
from documents.services.non_person_entity_stage2_occurrence_preflight import (
    RECORDED_CANDIDATE_SHA256,
    _REQUIRED_COLUMNS,
    preflight_stage2_occurrence_links,
)

_BODY = "The Cairo market is quiet."
_OCCURRENCE_98 = next(
    identity for identity in OCCURRENCE_IDENTITIES if identity.occurrence_id == 98
)


def _snapshot() -> dict[str, list]:
    return {
        "occurrences": list(
            ArchiveItemEntityOccurrence.objects.order_by("pk").values_list(
                "id",
                "archive_item_id",
                "text_kind",
                "source_text_sha256",
                "normalization_version",
                "normalized_surface",
                "occurrence_ordinal",
                "resolution_status",
                "entity_id",
                "decision_id",
                "matched_text",
            )
        ),
        "proposals": list(
            NonPersonEntityOccurrenceProposal.objects.order_by("pk").values_list(
                "id",
                "archive_item_id",
                "normalized_surface",
                "occurrence_ordinal",
                "matched_text",
            )
        ),
        "candidates": list(
            NonPersonEntityOccurrenceCandidate.objects.order_by("pk").values_list(
                "id",
                "proposal_id",
                "candidate_entity_id",
                "status",
                "resolved_entity_id",
            )
        ),
        "matches": list(
            NonPersonEntityOccurrenceCandidateMatch.objects.order_by("pk").values_list(
                "id", "candidate_id", "method", "matched_value"
            )
        ),
        "events": list(
            NonPersonEntityOccurrenceReviewEvent.objects.order_by("pk").values_list(
                "id", "action", "proposal_id", "candidate_id"
            )
        ),
        "decisions": list(
            ReviewedNonPersonEntityDecision.objects.order_by("pk").values_list(
                "id",
                "source",
                "candidate_id",
                "decision",
                "result_entity_id",
                "final_canonical",
            )
        ),
        "corrections": list(
            NonPersonEntityCorrection.objects.order_by("pk").values_list(
                "id",
                "decision_id",
                "effective_entity_id",
                "retired_entity_id",
            )
        ),
        "aliases": list(
            NonPersonEntityAlias.objects.order_by("pk").values_list(
                "id", "entity_id", "name", "kind"
            )
        ),
        "entities": list(
            NonPersonEntity.objects.order_by("pk").values_list(
                "id",
                "canonical_name",
                "display_name",
                "entity_type",
                "entity_subtype",
            )
        ),
    }


def _ocr_item(body: str) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        title="ocr",
        item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        visibility=ArchiveItem.Visibility.PRIVATE,
    )
    document = Document.objects.create(
        doc_type=Document.DocType.PDF,
        text_input_type=Document.TextInputType.PRINTED,
        language="en",
        archive_item=item,
        upload_status=Document.UploadStatus.UPLOADED,
    )
    DocumentTextResult.objects.create(
        document=document,
        result_type=DocumentTextResult.ResultType.SOURCE_TEXT,
        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
        status=DocumentTextResult.Status.NEEDS_REVIEW,
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        engine="gemini-test",
        text=body,
    )
    return item


def _entity(name: str, *, entity_id: int | None = None) -> NonPersonEntity:
    fields = {
        "canonical_name": name,
        "entity_type": NonPersonEntity.EntityType.PLACE,
    }
    if entity_id is not None:
        fields["id"] = entity_id
    return NonPersonEntity.objects.create(**fields)


def _decision(
    entity: NonPersonEntity,
    *,
    candidate_id: str = "EC0001",
    action: str = ReviewedNonPersonEntityDecision.Decision.APPROVE,
) -> ReviewedNonPersonEntityDecision:
    return ReviewedNonPersonEntityDecision.objects.create(
        source=THIRD_DECISION_SOURCE,
        candidate_id=candidate_id,
        workbook_sha256="a" * 64,
        decision=action,
        review_status=ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED,
        final_canonical=entity.canonical_name,
        result_entity=entity,
    )


def _occurrence_98() -> None:
    _entity("הנהלת הצללים של בני עקיבא במצרים", entity_id=SURVIVOR_ENTITY_ID)
    ArchiveItem.objects.create(
        id=_OCCURRENCE_98.archive_item_id,
        title="item 112",
        item_type=ArchiveItem.ItemType.MANUAL_TEXT,
        visibility=ArchiveItem.Visibility.PRIVATE,
    )
    ArchiveItemEntityOccurrence.objects.create(
        id=_OCCURRENCE_98.occurrence_id,
        archive_item_id=_OCCURRENCE_98.archive_item_id,
        text_kind=_OCCURRENCE_98.text_kind,
        source_text_sha256=_OCCURRENCE_98.source_text_sha256,
        normalization_version=_OCCURRENCE_98.normalization_version,
        normalized_surface=_OCCURRENCE_98.normalized_surface,
        occurrence_ordinal=_OCCURRENCE_98.occurrence_ordinal,
        resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
        entity_id=SURVIVOR_ENTITY_ID,
        matched_text=_OCCURRENCE_98.normalized_surface,
    )


def _candidate_row(
    item: ArchiveItem,
    decision: ReviewedNonPersonEntityDecision,
    entity: NonPersonEntity,
    **overrides,
) -> dict[str, str]:
    digest = source_text_sha256(_BODY)
    row = {
        "mention_index": "1",
        "archive_item_id": str(item.pk),
        "proposed_candidate_id": decision.candidate_id,
        "ledger_latest_contract": "third",
        "dev_decision_ids": str(decision.pk),
        "dev_decision_action": decision.decision,
        "dev_effective_entity_id": str(entity.pk),
        "current_text_kind": ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
        "reconstructed_start": "4",
        "reconstructed_end": "9",
        "reconstructed_text": "Cairo",
        "computed_normalized_surface": "cairo",
        "computed_occurrence_ordinal": "1",
        "computed_source_sha256": digest,
        "post_merge_technical_status": "TECHNICAL_CANDIDATE",
        "approved_for_apply": "False",
    }
    row.update(overrides)
    return row


def _write_csv(directory: Path, rows: list[dict[str, str]]) -> tuple[Path, str]:
    path = directory / "candidates.csv"
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(_REQUIRED_COLUMNS))
        writer.writeheader()
        writer.writerows(rows)
    digest = hashlib.sha256(path.read_bytes()).hexdigest()
    return path, digest


class Stage2OccurrencePreflightTests(TestCase):
    def test_matching_row_is_eligible_and_writes_no_database_rows(self):
        _occurrence_98()
        item = _ocr_item(_BODY)
        entity = _entity("קהיר")
        decision = _decision(entity)
        directory = Path(self._dir())
        source, digest = _write_csv(directory, [_candidate_row(item, decision, entity)])
        before_bytes = source.read_bytes()
        before = _snapshot()
        output = directory / "out"

        result = preflight_stage2_occurrence_links(
            source,
            expected_sha256=digest,
            output_dir=output,
        )

        self.assertTrue(result.clean)
        self.assertEqual(result.eligible_count, 1)
        self.assertTrue(result.occurrence_98_ok)
        self.assertEqual(result.file_sha256, digest)
        self.assertEqual(_snapshot(), before)
        self.assertEqual(source.read_bytes(), before_bytes)
        report = json.loads(
            (output / "stage2_occurrence_import_preflight.json").read_text(
                encoding="utf-8"
            )
        )
        self.assertEqual(report["database_writes"], 0)
        self.assertFalse(report["approved_for_apply_changed"])
        self.assertFalse(report["write_authorized"])
        self.assertFalse(report["pending_proposals_modified"])
        written = (output / "stage2_occurrence_import_preflight.csv").read_text(
            encoding="utf-8"
        )
        self.assertIn("eligible", written)
        self.assertIn("false", written)
        self.assertIn("approved_for_apply stays false", written)
        markdown = (output / "stage2_occurrence_import_preflight.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("Read-only dry run", markdown)

    def test_effective_correction_overrides_the_csv_entity_id(self):
        _occurrence_98()
        item = _ocr_item(_BODY)
        retired = _entity("שם ישן")
        survivor = _entity("שם חדש")
        decision = _decision(retired)
        NonPersonEntityCorrection.objects.create(
            decision=decision,
            effective_entity=survivor,
            retired_entity=retired,
            canonical_name=survivor.canonical_name,
            display_name="",
            entity_subtype="",
            approval_reference="test-correction",
            manifest_sha256="b" * 64,
        )
        directory = Path(self._dir())
        source, digest = _write_csv(
            directory,
            [
                _candidate_row(
                    item,
                    decision,
                    retired,
                    dev_effective_entity_id=str(retired.pk),
                )
            ],
        )
        before = _snapshot()

        result = preflight_stage2_occurrence_links(
            source, expected_sha256=digest, output_dir=directory / "out"
        )

        self.assertFalse(result.clean)
        self.assertEqual(result.status_counts["entity_drift"], 1)
        text = (directory / "out" / "stage2_occurrence_import_preflight.csv").read_text(
            encoding="utf-8"
        )
        self.assertIn(str(survivor.pk), text)
        self.assertIn("was not used", text)
        self.assertEqual(_snapshot(), before)

    def test_retired_effective_entity_is_not_eligible(self):
        _occurrence_98()
        item = _ocr_item(_BODY)
        retired = _entity("ישות שהוצאה")
        survivor = _entity("ישות פעילה")
        owner = _decision(survivor, candidate_id="EC9999")
        NonPersonEntityCorrection.objects.create(
            decision=owner,
            effective_entity=survivor,
            retired_entity=retired,
            canonical_name=survivor.canonical_name,
            display_name="",
            entity_subtype="",
            approval_reference="test-retirement",
            manifest_sha256="c" * 64,
        )
        decision = _decision(retired, candidate_id="EC0001")
        directory = Path(self._dir())
        source, digest = _write_csv(
            directory, [_candidate_row(item, decision, retired)]
        )
        before = _snapshot()

        result = preflight_stage2_occurrence_links(
            source, expected_sha256=digest, output_dir=directory / "out"
        )

        self.assertFalse(result.clean)
        self.assertEqual(result.status_counts["retired_entity"], 1)
        self.assertEqual(_snapshot(), before)

    def test_pending_proposal_overlap_is_reported_and_left_pending(self):
        _occurrence_98()
        item = _ocr_item(_BODY)
        entity = _entity("קהיר")
        decision = _decision(entity)
        digest = source_text_sha256(_BODY)
        proposal = NonPersonEntityOccurrenceProposal.objects.create(
            archive_item=item,
            text_kind=ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
            source_text_sha256=digest,
            normalization_version=SURFACE_V1,
            normalized_surface="cairo",
            occurrence_ordinal=1,
            matched_text="Cairo",
        )
        candidate = NonPersonEntityOccurrenceCandidate.objects.create(
            proposal=proposal,
            candidate_entity=entity,
            status=NonPersonEntityOccurrenceCandidate.Status.PENDING,
        )
        directory = Path(self._dir())
        source, file_digest = _write_csv(
            directory, [_candidate_row(item, decision, entity)]
        )
        before = _snapshot()

        result = preflight_stage2_occurrence_links(
            source, expected_sha256=file_digest, output_dir=directory / "out"
        )

        self.assertFalse(result.clean)
        self.assertEqual(result.status_counts["proposal_overlap"], 1)
        candidate.refresh_from_db()
        self.assertEqual(
            candidate.status, NonPersonEntityOccurrenceCandidate.Status.PENDING
        )
        self.assertEqual(_snapshot(), before)

    def test_existing_link_and_different_entity_are_not_inserts(self):
        _occurrence_98()
        item = _ocr_item(_BODY)
        entity = _entity("קהיר")
        other = _entity("אלכסנדריה")
        decision = _decision(entity)
        digest = source_text_sha256(_BODY)
        shared = {
            "archive_item": item,
            "text_kind": ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION,
            "source_text_sha256": digest,
            "normalization_version": SURFACE_V1,
            "normalized_surface": "cairo",
            "occurrence_ordinal": 1,
            "resolution_status": ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
            "matched_text": "Cairo",
        }
        ArchiveItemEntityOccurrence.objects.create(entity=entity, **shared)
        directory = Path(self._dir())
        source, file_digest = _write_csv(
            directory, [_candidate_row(item, decision, entity, mention_index="10")]
        )
        before = _snapshot()
        linked = preflight_stage2_occurrence_links(
            source, expected_sha256=file_digest, output_dir=directory / "linked"
        )
        self.assertEqual(linked.status_counts["already_linked"], 1)
        self.assertEqual(_snapshot(), before)

        ArchiveItemEntityOccurrence.objects.filter(archive_item=item).update(
            entity=other
        )
        before = _snapshot()
        source, file_digest = _write_csv(
            directory, [_candidate_row(item, decision, entity, mention_index="11")]
        )
        collided = preflight_stage2_occurrence_links(
            source, expected_sha256=file_digest, output_dir=directory / "collided"
        )
        self.assertEqual(collided.status_counts["identity_collision"], 1)
        self.assertEqual(_snapshot(), before)

    def test_source_sha_drift_and_duplicate_identity(self):
        _occurrence_98()
        item = _ocr_item(_BODY)
        entity = _entity("קהיר")
        decision = _decision(entity)
        drifted = _candidate_row(
            item,
            decision,
            entity,
            mention_index="3",
            computed_source_sha256="d" * 64,
        )
        first = _candidate_row(item, decision, entity, mention_index="1")
        second = _candidate_row(item, decision, entity, mention_index="2")
        directory = Path(self._dir())
        source, digest = _write_csv(directory, [drifted, first, second])
        before = _snapshot()

        result = preflight_stage2_occurrence_links(
            source, expected_sha256=digest, output_dir=directory / "out"
        )

        self.assertFalse(result.clean)
        self.assertEqual(result.status_counts["source_sha_drift"], 1)
        self.assertEqual(result.status_counts["duplicate_identity"], 2)
        self.assertEqual(_snapshot(), before)

    def test_occurrence_98_drift_and_reproposal_are_refused(self):
        _occurrence_98()
        pin = ArchiveItemEntityOccurrence.objects.get(pk=98)
        pin.normalized_surface = "changed"
        pin.save(update_fields=["normalized_surface", "updated_at"])
        item = _ocr_item(_BODY)
        entity = _entity("קהיר")
        decision = _decision(entity)
        again = _candidate_row(
            item,
            decision,
            entity,
            mention_index="1385",
            archive_item_id=str(_OCCURRENCE_98.archive_item_id),
            current_text_kind=_OCCURRENCE_98.text_kind,
            computed_source_sha256=_OCCURRENCE_98.source_text_sha256,
            computed_normalized_surface=_OCCURRENCE_98.normalized_surface,
            computed_occurrence_ordinal=str(_OCCURRENCE_98.occurrence_ordinal),
            reconstructed_text=_OCCURRENCE_98.normalized_surface,
            reconstructed_start="1",
            reconstructed_end=str(1 + len(_OCCURRENCE_98.normalized_surface)),
        )
        directory = Path(self._dir())
        source, digest = _write_csv(directory, [again])
        before = _snapshot()

        result = preflight_stage2_occurrence_links(
            source, expected_sha256=digest, output_dir=directory / "out"
        )

        self.assertFalse(result.clean)
        self.assertFalse(result.occurrence_98_ok)
        self.assertIn("occurrence 98 identity drifted", result.file_errors)
        self.assertEqual(result.status_counts["excluded_occurrence_98"], 1)
        self.assertEqual(_snapshot(), before)

    def test_embedded_newlines_keep_the_recorded_span_and_surface(self):
        _occurrence_98()
        cases = (
            ("lf", "note ארץ\nישראל end", "ארץ ישראל", "EC0101"),
            ("crlf", "note ארץ\r\nישראל end", "ארץ ישראל", "EC0102"),
        )
        for name, body, surface, candidate_id in cases:
            with self.subTest(name=name):
                item = _ocr_item(body)
                entity = _entity("ארץ ישראל")
                decision = _decision(entity, candidate_id=candidate_id)
                located = locate_surface_occurrences(body, surface)
                self.assertIsNotNone(located.occurrences)
                assert located.occurrences is not None
                match = located.occurrences[0]
                self.assertIn("\n", body[match.start : match.end])
                directory = Path(self._dir())
                source, digest = _write_csv(
                    directory,
                    [
                        _candidate_row(
                            item,
                            decision,
                            entity,
                            reconstructed_start=str(match.start),
                            reconstructed_end=str(match.end),
                            reconstructed_text=body[match.start : match.end],
                            computed_normalized_surface=normalize_surface_v1(
                                body[match.start : match.end]
                            ),
                            computed_occurrence_ordinal=str(match.ordinal),
                            computed_source_sha256=source_text_sha256(body),
                        )
                    ],
                )
                before_bytes = source.read_bytes()
                before = _snapshot()

                result = preflight_stage2_occurrence_links(
                    source,
                    expected_sha256=digest,
                    output_dir=directory / "out",
                )

                self.assertTrue(result.clean, result.summary)
                self.assertEqual(result.status_counts["eligible"], 1)
                self.assertEqual(source.read_bytes(), before_bytes)
                self.assertEqual(_snapshot(), before)

    def test_zero_offset_is_valid_when_the_mention_starts_the_text(self):
        _occurrence_98()
        body = "Cairo at the start"
        item = _ocr_item(body)
        entity = _entity("קהיר")
        decision = _decision(entity)
        directory = Path(self._dir())
        source, digest = _write_csv(
            directory,
            [
                _candidate_row(
                    item,
                    decision,
                    entity,
                    reconstructed_start="0",
                    reconstructed_end="5",
                    reconstructed_text="Cairo",
                    computed_normalized_surface="cairo",
                    computed_source_sha256=source_text_sha256(body),
                )
            ],
        )
        before = _snapshot()

        result = preflight_stage2_occurrence_links(
            source, expected_sha256=digest, output_dir=directory / "out"
        )

        self.assertTrue(result.clean, result.summary)
        self.assertEqual(result.eligible_count, 1)
        self.assertEqual(_snapshot(), before)

    def test_incorrect_span_and_mismatched_surface_stay_rejected(self):
        _occurrence_98()
        item = _ocr_item(_BODY)
        entity = _entity("קהיר")
        decision = _decision(entity)
        directory = Path(self._dir())
        wrong_span, span_digest = _write_csv(
            directory,
            [
                _candidate_row(
                    item,
                    decision,
                    entity,
                    reconstructed_start="0",
                    reconstructed_end="5",
                    reconstructed_text="Cairo",
                )
            ],
        )
        span_result = preflight_stage2_occurrence_links(
            wrong_span,
            expected_sha256=span_digest,
            output_dir=directory / "span",
        )
        self.assertEqual(span_result.status_counts["boundary_drift"], 1)

        wrong_surface, surface_digest = _write_csv(
            directory,
            [
                _candidate_row(
                    item,
                    decision,
                    entity,
                    computed_normalized_surface="egypt",
                )
            ],
        )
        before = _snapshot()
        surface_result = preflight_stage2_occurrence_links(
            wrong_surface,
            expected_sha256=surface_digest,
            output_dir=directory / "surface",
        )
        self.assertEqual(surface_result.status_counts["surface_drift"], 1)
        self.assertEqual(_snapshot(), before)

    def test_true_approved_for_apply_is_not_accepted_or_written_back(self):
        _occurrence_98()
        item = _ocr_item(_BODY)
        entity = _entity("קהיר")
        decision = _decision(entity)
        directory = Path(self._dir())
        source, digest = _write_csv(
            directory,
            [_candidate_row(item, decision, entity, approved_for_apply="true")],
        )
        before_bytes = source.read_bytes()
        before = _snapshot()

        result = preflight_stage2_occurrence_links(
            source, expected_sha256=digest, output_dir=directory / "out"
        )

        self.assertFalse(result.clean)
        self.assertEqual(result.status_counts["invalid_row"], 1)
        self.assertEqual(source.read_bytes(), before_bytes)
        self.assertEqual(_snapshot(), before)

    def test_hash_mismatch_writes_a_report_and_no_database_rows(self):
        _entity("לא נגע")
        directory = Path(self._dir())
        source = directory / "candidates.csv"
        source.write_text("mention_index\n1\n", encoding="utf-8")
        before = _snapshot()

        result = preflight_stage2_occurrence_links(
            source,
            expected_sha256="e" * 64,
            output_dir=directory / "out",
        )

        self.assertFalse(result.clean)
        self.assertEqual(result.row_count, 0)
        self.assertTrue(result.file_errors)
        self.assertEqual(_snapshot(), before)
        self.assertTrue(
            (directory / "out" / "stage2_occurrence_import_preflight.json").exists()
        )

    def test_command_rejects_a_different_hash_and_apply(self):
        _entity("לא נגע")
        directory = Path(self._dir())
        source = directory / "candidates.csv"
        source.write_bytes(b"not-the-recorded-file\n")
        before = _snapshot()

        with self.assertRaises(CommandError):
            call_command(
                "import_stage2_occurrence_links",
                "--csv",
                str(source),
                "--expected-sha256",
                "f" * 64,
                "--output-dir",
                str(directory / "out"),
            )
        with self.assertRaises(CommandError):
            call_command(
                "import_stage2_occurrence_links",
                "--csv",
                str(source),
                "--expected-sha256",
                RECORDED_CANDIDATE_SHA256,
                "--output-dir",
                str(directory / "recorded"),
                "--apply",
            )
        self.assertEqual(_snapshot(), before)
        self.assertFalse((directory / "out").exists())

    def _dir(self) -> str:
        import tempfile

        directory = tempfile.mkdtemp()
        self.addCleanup(
            lambda: __import__("shutil").rmtree(directory, ignore_errors=True)
        )
        return directory
