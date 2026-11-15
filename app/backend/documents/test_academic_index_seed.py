"""Contract tests for the approved Academic Index pilot seed importer."""

from __future__ import annotations

import threading
from io import StringIO
from unittest.mock import patch

from django.apps import apps
from django.core.exceptions import ValidationError
from django.core.management import call_command
from django.core.management.base import CommandError
from django.db import connection
from django.test import SimpleTestCase, TestCase, TransactionTestCase

from documents import archive_urls
from documents.academic_index_models import (
    AcademicIndexDecisionEvent,
    AcademicTopic,
    AcademicTopicLabel,
    AcademicTopicRelation,
    ArchiveItemTopicAssociation,
)
from documents.academic_index_seed_v2 import (
    ALTERNATE_LABELS,
    APPLY_CONFIRM_TOKEN,
    APPROVED_EVIDENCE_NOTE,
    ASSOCIATIONS,
    BROADER_EDGES,
    EXPECTED_ISSUER_ROW_COUNT,
    OWNER_AUTHORIZATION_TOKEN,
    PINNED_DISTINCT_ITEM_IDS,
    RELATED_EDGES,
    SOURCE_SPECIFICATION_SHA256,
    SPECIFICATION_V2_DOCUMENT_SHA256,
    TOPICS,
    BroaderEdge,
    RelatedEdge,
    SeedAssociation,
    SeedTopic,
    all_operation_keys,
    association_operation_key,
    evidence_note_rejection,
    seed_definition_errors,
    topic_operation_key,
)
from documents.models import (
    ArchiveCategory,
    ArchiveEvent,
    ArchiveItem,
    ArchiveItemAuthor,
    ArchiveItemEntityOccurrence,
    ArchiveItemPerson,
    Author,
    Document,
    DocumentMetadata,
    NonPersonEntity,
    NonPersonEntityAlias,
    NonPersonEntityCorrection,
    Person,
    PhotoContent,
    PhotoPerson,
    ReviewedNonPersonEntityDecision,
    Tag,
)
from documents.services import academic_index
from documents.services.academic_index_seed import (
    AcademicIndexSeedError,
    apply_approved_seed,
    inspect_approved_seed,
)

_ITEM_378_TITLE = "דוח על התנועה של הפועל המזרחי - בני עקיבא - במצרים (אנ')"
_APPROVED_EVIDENCE_NOTE = (
    "שיוך או קשר שאושר על ידי מנהלת הארכיון במסגרת פיילוט המפתח האקדמי, "
    "על בסיס סקירת מסמכי הארכיון. תמונות המקור לא נבדקו מחדש באופן עצמאי "
    "במסגרת טעינת הנתונים."
)
_SHA = "c" * 64


def _apply(**overrides):
    fields = {
        "confirm": APPLY_CONFIRM_TOKEN,
        "owner_authorization": OWNER_AUTHORIZATION_TOKEN,
    }
    fields.update(overrides)
    return apply_approved_seed(**fields)


def _command_apply(**overrides):
    options = {
        "apply": True,
        "confirm": APPLY_CONFIRM_TOKEN,
        "owner_authorization": OWNER_AUTHORIZATION_TOKEN,
    }
    options.update(overrides)
    stdout = StringIO()
    call_command("import_academic_index_seed", stdout=stdout, **options)
    return stdout.getvalue()


def _create_items(item_ids=PINNED_DISTINCT_ITEM_IDS) -> None:
    for item_id in item_ids:
        title = _ITEM_378_TITLE if item_id == 378 else f"Seed item {item_id}"
        ArchiveItem.objects.create(
            id=item_id,
            title=title,
            item_type=ArchiveItem.ItemType.MANUAL_TEXT,
            visibility=ArchiveItem.Visibility.PRIVATE,
        )


def _academic_counts() -> dict[str, int]:
    return {
        "topics": AcademicTopic.objects.count(),
        "labels": AcademicTopicLabel.objects.count(),
        "relations": AcademicTopicRelation.objects.count(),
        "associations": ArchiveItemTopicAssociation.objects.count(),
        "events": AcademicIndexDecisionEvent.objects.count(),
    }


class AcademicIndexSeedFixtureTests(SimpleTestCase):
    def test_shipped_fixture_matches_the_approved_counts_and_text(self):
        self.assertEqual(seed_definition_errors(), ())
        self.assertEqual(
            [(topic.stable_key, topic.preferred_label) for topic in TOPICS],
            [
                ("AI-01", "עליית יהודי מצרים לישראל"),
                ("AI-02", "ארגון העלייה ממצרים"),
                ("AI-03", "הכשרה חלוצית של עולים ממצרים"),
                ("AI-04", "הכשרת מדריכים בתנועות הנוער הציוניות במצרים"),
                ("AI-05", "המאבק לשחרור אסירי הפרשה"),
                ("AI-06", "רפואת משפחה בישראל"),
                ("AI-07", "רפואה קהילתית ועבודת המרפאה"),
                ("AI-08", "התפתחות רפואת המשפחה בישראל"),
                ("AI-09", "הוראה והכשרת רופאי משפחה"),
                ("AI-10", "חילופי שבויים בהקשר של אסירי הפרשה"),
                ("AI-11", "הנצחת אסירי הפרשה והרוגיה"),
            ],
        )
        self.assertEqual(
            [(edge.narrower_key, edge.broader_key) for edge in BROADER_EDGES],
            [
                ("AI-02", "AI-01"),
                ("AI-03", "AI-01"),
                ("AI-07", "AI-06"),
                ("AI-08", "AI-06"),
                ("AI-09", "AI-06"),
                ("AI-10", "AI-05"),
            ],
        )
        self.assertEqual(
            [(edge.left_key, edge.right_key) for edge in RELATED_EDGES],
            [("AI-05", "AI-11")],
        )
        self.assertEqual(len(ASSOCIATIONS), 22)
        self.assertEqual(len({row.archive_item_id for row in ASSOCIATIONS}), 15)
        self.assertEqual(
            [row.batch_id for row in ASSOCIATIONS].count("01"),
            12,
        )
        self.assertEqual([row.batch_id for row in ASSOCIATIONS].count("02"), 4)
        self.assertEqual([row.batch_id for row in ASSOCIATIONS].count("03"), 6)
        self.assertEqual(ALTERNATE_LABELS, ())
        self.assertEqual(EXPECTED_ISSUER_ROW_COUNT, 0)
        self.assertNotIn("AI-04", {edge.narrower_key for edge in BROADER_EDGES})
        self.assertEqual(
            {
                (row.topic_key, row.relevance)
                for row in ASSOCIATIONS
                if row.archive_item_id == 378
            },
            {("AI-02", "CENTRAL"), ("AI-04", "CENTRAL")},
        )
        self.assertEqual(len(all_operation_keys()), 40)
        self.assertEqual(len(set(all_operation_keys())), 40)

    def test_specification_shas_and_approved_evidence_note_are_explicit(self):
        self.assertEqual(
            SOURCE_SPECIFICATION_SHA256,
            "a709aa3a750975b06630d867cc365f2abfb0ce238624ff6676ac0dae8ffab15e",
        )
        self.assertEqual(
            SPECIFICATION_V2_DOCUMENT_SHA256,
            "3a3b33f07066217bce2c56e2521585396c793b64e8fe32ad761e6d11cbc29a7f",
        )
        self.assertNotEqual(
            SOURCE_SPECIFICATION_SHA256, SPECIFICATION_V2_DOCUMENT_SHA256
        )
        self.assertEqual(APPROVED_EVIDENCE_NOTE, _APPROVED_EVIDENCE_NOTE)
        self.assertIsNone(evidence_note_rejection(APPROVED_EVIDENCE_NOTE))
        for placeholder in (
            "",
            "PROVISIONAL_EVIDENCE_NOTE_PENDING_OWNER_APPROVAL",
            "IMAGE_NOT_COLLATED",
            "placeholder",
            "TBD",
        ):
            self.assertIsNotNone(evidence_note_rejection(placeholder))
        self.assertIsNotNone(evidence_note_rejection("A different evidence note."))
        self.assertEqual(
            topic_operation_key("AI-04"), "academic-index-seed-v2:topic:AI-04"
        )

    def test_apply_command_has_no_provisional_acknowledgement_flag(self):
        from documents.management.commands.import_academic_index_seed import Command

        parser = Command().create_parser("manage.py", "import_academic_index_seed")
        destinations = {action.dest for action in parser._actions}
        self.assertIn("confirm", destinations)
        self.assertIn("owner_authorization", destinations)
        self.assertNotIn("acknowledge_provisional_evidence_note", destinations)

    def test_inconsistent_seed_definitions_are_rejected(self):
        self.assertTrue(
            seed_definition_errors(
                broader_edges=BROADER_EDGES + (BroaderEdge("AI-01", "AI-02"),)
            )
        )
        self.assertTrue(
            seed_definition_errors(
                broader_edges=BROADER_EDGES + (BroaderEdge("AI-04", "AI-01"),)
            )
        )
        self.assertTrue(
            seed_definition_errors(related_edges=(RelatedEdge("AI-05", "AI-10"),))
        )
        self.assertTrue(
            seed_definition_errors(
                associations=ASSOCIATIONS
                + (SeedAssociation(378, "AI-02", "CENTRAL", "01"),)
            )
        )
        self.assertTrue(
            seed_definition_errors(
                associations=ASSOCIATIONS[:-1]
                + (SeedAssociation(301, "AI-11", "LOW", "03"),)
            )
        )
        self.assertTrue(
            seed_definition_errors(
                topics=tuple(SeedTopic(topic.stable_key, "  ") for topic in TOPICS)
            )
        )

    def test_item_389_topic_mutation_fails_the_independent_contract(self):
        mutated = tuple(
            SeedAssociation(389, "AI-05", row.relevance, row.batch_id)
            if row.archive_item_id == 389 and row.topic_key == "AI-01"
            else row
            for row in ASSOCIATIONS
        )
        errors = seed_definition_errors(associations=mutated)
        self.assertTrue(
            any(
                "approved specification v2 section 5 contract" in error
                for error in errors
            )
        )
        self.assertEqual(seed_definition_errors(), ())

    def test_item_430_relevance_mutation_fails_the_independent_contract(self):
        mutated = tuple(
            SeedAssociation(430, "AI-03", "SECONDARY", row.batch_id)
            if row.archive_item_id == 430 and row.topic_key == "AI-03"
            else row
            for row in ASSOCIATIONS
        )
        errors = seed_definition_errors(associations=mutated)
        self.assertTrue(
            any(
                "approved specification v2 section 5 contract" in error
                for error in errors
            )
        )
        self.assertEqual(len(mutated), 22)
        self.assertEqual(seed_definition_errors(), ())

    def test_archive_routes_have_no_topic_path(self):
        routes = " ".join(str(pattern.pattern) for pattern in archive_urls.urlpatterns)
        self.assertNotIn("topics", routes)


class AcademicIndexSeedDryRunTests(TestCase):
    def test_database_name_is_the_disposable_test_database(self):
        self.assertTrue(str(connection.settings_dict["NAME"]).startswith("test_"))

    def test_default_dry_run_writes_nothing(self):
        _create_items()
        before = _academic_counts()

        def fail_save(self, *args, **kwargs):
            raise AssertionError(f"{type(self).__name__}.save during dry-run")

        stdout = StringIO()
        with (
            patch.object(AcademicTopic, "save", fail_save),
            patch.object(AcademicTopicLabel, "save", fail_save),
            patch.object(AcademicTopicRelation, "save", fail_save),
            patch.object(ArchiveItemTopicAssociation, "save", fail_save),
            patch.object(AcademicIndexDecisionEvent, "save", fail_save),
            patch.object(ArchiveItem, "save", fail_save),
        ):
            call_command("import_academic_index_seed", stdout=stdout)
        text = stdout.getvalue()
        self.assertIn("mode=dry-run", text)
        self.assertIn("outcome=would_create", text)
        self.assertIn("decision_events_written=0", text)
        self.assertIn("topics=11", text)
        self.assertIn("broader=6", text)
        self.assertIn("related=1", text)
        self.assertIn("associations=22", text)
        self.assertIn("distinct_items=15", text)
        self.assertIn("issuer_rows=0", text)
        self.assertEqual(_academic_counts(), before)

    def test_confirmation_tokens_without_apply_do_not_write(self):
        _create_items()
        stdout = StringIO()
        call_command(
            "import_academic_index_seed",
            stdout=stdout,
            confirm=APPLY_CONFIRM_TOKEN,
            owner_authorization=OWNER_AUTHORIZATION_TOKEN,
        )
        self.assertIn("outcome=would_create", stdout.getvalue())
        self.assertEqual(_academic_counts()["events"], 0)
        self.assertEqual(_academic_counts()["topics"], 0)

    def test_missing_archive_item_aborts_without_writes(self):
        _create_items(
            tuple(item_id for item_id in PINNED_DISTINCT_ITEM_IDS if item_id != 430)
        )
        with self.assertRaises(CommandError) as raised:
            call_command("import_academic_index_seed")
        self.assertIn("missing_archive_item_ids=430", str(raised.exception))
        self.assertEqual(_academic_counts()["topics"], 0)
        self.assertEqual(_academic_counts()["events"], 0)
        self.assertEqual(_academic_counts()["associations"], 0)
        self.assertTrue(ArchiveItem.objects.filter(pk=378).exists())
        self.assertFalse(ArchiveItem.objects.filter(pk=430).exists())


class AcademicIndexSeedApplyTests(TestCase):
    def test_apply_creates_exact_rows_and_repeat_is_a_no_op(self):
        _create_items()
        catalog = _catalog_sentinels()
        summary = _command_apply()
        self.assertIn("mode=apply", summary)
        self.assertIn("outcome=applied", summary)
        self.assertIn("decision_events_written=40", summary)
        self.assertIn(SOURCE_SPECIFICATION_SHA256, summary)
        self.assertIn("evidence_note_status=OWNER_APPROVED", summary)
        self.assertIn(_APPROVED_EVIDENCE_NOTE, summary)
        self._assert_exact_seed()
        self.assertEqual(_catalog_fingerprint(), catalog)

        topic_updates = {
            topic.stable_key: topic.updated_at for topic in AcademicTopic.objects.all()
        }
        event_ids = list(
            AcademicIndexDecisionEvent.objects.order_by("id").values_list(
                "id", flat=True
            )
        )
        again = _command_apply()
        self.assertIn("outcome=already_applied", again)
        self.assertIn("decision_events_written=0", again)
        self.assertEqual(
            list(
                AcademicIndexDecisionEvent.objects.order_by("id").values_list(
                    "id", flat=True
                )
            ),
            event_ids,
        )
        self.assertEqual(
            {
                topic.stable_key: topic.updated_at
                for topic in AcademicTopic.objects.all()
            },
            topic_updates,
        )
        self.assertEqual(_catalog_fingerprint(), catalog)
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), 40)

    def test_direct_service_replay_of_a_seed_key_does_not_append(self):
        _create_items()
        _apply()
        topic = AcademicTopic.objects.get(stable_key="AI-02")
        item = ArchiveItem.objects.get(pk=378)
        before = AcademicIndexDecisionEvent.objects.count()
        replay = academic_index.approve_item_topic(
            item,
            topic,
            "CENTRAL",
            action=AcademicIndexDecisionEvent.Action.IMPORT,
            specification_sha256=SOURCE_SPECIFICATION_SHA256,
            operation_key=association_operation_key(378, "AI-02"),
            batch_id="01",
            evidence_kind=AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL,
            evidence_note=APPROVED_EVIDENCE_NOTE,
            reason="Approved academic index pilot seed from specification v2 section 5.",
        )
        self.assertFalse(replay.created)
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), before)

    def test_reused_seed_operation_key_rejects_a_different_request(self):
        _create_items()
        _apply()
        topic = AcademicTopic.objects.get(stable_key="AI-02")
        item = ArchiveItem.objects.get(pk=378)
        before = _academic_counts()
        with self.assertRaises(ValidationError):
            academic_index.approve_item_topic(
                item,
                topic,
                "SECONDARY",
                action=AcademicIndexDecisionEvent.Action.IMPORT,
                specification_sha256=SOURCE_SPECIFICATION_SHA256,
                operation_key=association_operation_key(378, "AI-02"),
                batch_id="01",
                evidence_kind=AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL,
                evidence_note=APPROVED_EVIDENCE_NOTE,
                reason="Approved academic index pilot seed from specification v2 section 5.",
            )
        self.assertEqual(_academic_counts(), before)
        association = ArchiveItemTopicAssociation.objects.get(
            archive_item_id=378, topic=topic
        )
        self.assertEqual(association.relevance, "CENTRAL")

    def test_failed_apply_rolls_back_every_academic_row(self):
        _create_items()

        def fail_broader(*args, **kwargs):
            raise ValidationError("forced seed failure")

        with (
            patch(
                "documents.services.academic_index_seed.academic_index.approve_broader_relation",
                fail_broader,
            ),
            self.assertRaises(AcademicIndexSeedError),
        ):
            _apply()
        self.assertEqual(_academic_counts()["topics"], 0)
        self.assertEqual(_academic_counts()["relations"], 0)
        self.assertEqual(_academic_counts()["associations"], 0)
        self.assertEqual(_academic_counts()["events"], 0)

    def test_apply_tokens_are_separate_and_required(self):
        _create_items()
        with self.assertRaises(CommandError):
            _command_apply(confirm="no")
        with self.assertRaises(CommandError):
            _command_apply(owner_authorization="no")
        self.assertEqual(_academic_counts()["events"], 0)
        self.assertEqual(_academic_counts()["topics"], 0)

    def test_placeholder_evidence_note_is_rejected_with_valid_tokens(self):
        _create_items()
        placeholders = (
            "PROVISIONAL_EVIDENCE_NOTE_PENDING_OWNER_APPROVAL",
            "IMAGE_NOT_COLLATED",
            "",
            "placeholder",
            "A different evidence note.",
        )
        for note in placeholders:
            with (
                patch(
                    "documents.services.academic_index_seed.APPROVED_EVIDENCE_NOTE",
                    note,
                ),
                self.assertRaises(CommandError) as raised,
            ):
                _command_apply()
            self.assertIn("rejected", str(raised.exception).casefold())
            self.assertEqual(_academic_counts()["events"], 0)
            self.assertEqual(_academic_counts()["topics"], 0)

    def _assert_exact_seed(self):
        topics = {topic.stable_key: topic for topic in AcademicTopic.objects.all()}
        self.assertEqual(set(topics), {topic.stable_key for topic in TOPICS})
        for seed_topic in TOPICS:
            row = topics[seed_topic.stable_key]
            self.assertEqual(row.preferred_label, seed_topic.preferred_label)
            self.assertEqual(row.scope_note, "")
            self.assertTrue(row.is_public_candidate)
        self.assertEqual(AcademicTopicLabel.objects.count(), 0)
        broader = {
            (relation.narrower.stable_key, relation.broader.stable_key)
            for relation in AcademicTopicRelation.objects.filter(
                relation_kind=AcademicTopicRelation.Kind.BROADER,
                status=AcademicTopicRelation.Status.APPROVED,
            )
        }
        self.assertEqual(
            broader,
            {(edge.narrower_key, edge.broader_key) for edge in BROADER_EDGES},
        )
        related = AcademicTopicRelation.objects.get(
            relation_kind=AcademicTopicRelation.Kind.RELATED
        )
        self.assertEqual(related.status, AcademicTopicRelation.Status.APPROVED)
        low = related.topic_low
        high = related.topic_high
        self.assertIsNotNone(low)
        self.assertIsNotNone(high)
        assert low is not None and high is not None
        self.assertEqual((low.stable_key, high.stable_key), ("AI-05", "AI-11"))
        self.assertEqual(AcademicTopicRelation.objects.count(), 7)
        self.assertFalse(
            AcademicTopicRelation.objects.filter(narrower__stable_key="AI-04").exists()
        )
        associations = {
            (row.archive_item_id, row.topic.stable_key): row.relevance
            for row in ArchiveItemTopicAssociation.objects.select_related("topic")
        }
        self.assertEqual(
            associations,
            {
                (row.archive_item_id, row.topic_key): row.relevance
                for row in ASSOCIATIONS
            },
        )
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), 40)
        self.assertTrue(
            all(
                event.action == AcademicIndexDecisionEvent.Action.IMPORT
                and event.evidence_kind
                == AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL
                and event.evidence_note == _APPROVED_EVIDENCE_NOTE
                and event.specification_sha256 == SOURCE_SPECIFICATION_SHA256
                for event in AcademicIndexDecisionEvent.objects.all()
            )
        )
        self.assertFalse(
            AcademicIndexDecisionEvent.objects.filter(
                evidence_note="IMAGE_NOT_COLLATED"
            ).exists()
        )
        self.assertEqual(
            academic_index.approved_ancestor_ids(topics["AI-04"].pk), set()
        )
        self.assertEqual(
            academic_index.direct_approved_item_ids(topics["AI-04"].pk), {378}
        )
        self.assertEqual(
            academic_index.direct_approved_item_ids(topics["AI-01"].pk), {264, 389}
        )
        self.assertEqual(
            academic_index.inherited_approved_item_ids(topics["AI-01"].pk),
            {378, 379, 366, 430},
        )
        self.assertEqual(
            academic_index.direct_approved_item_ids(topics["AI-05"].pk),
            {147, 164, 173, 144, 324},
        )
        self.assertEqual(
            academic_index.inherited_approved_item_ids(topics["AI-05"].pk), set()
        )
        self.assertEqual(
            academic_index.direct_approved_item_ids(topics["AI-06"].pk), set()
        )
        self.assertEqual(
            academic_index.inherited_approved_item_ids(topics["AI-06"].pk), {13, 284}
        )
        self.assertEqual(
            academic_index.direct_approved_item_ids(topics["AI-11"].pk), {163, 301}
        )
        self.assertFalse(
            academic_index.direct_approved_item_ids(topics["AI-05"].pk)
            & academic_index.direct_approved_item_ids(topics["AI-11"].pk)
        )
        self.assertEqual(associations[(324, "AI-05")], "SECONDARY")
        self.assertEqual(associations[(324, "AI-10")], "CENTRAL")
        self.assertNotIn(386, associations)
        model_names = {
            model.__name__ for model in apps.get_app_config("documents").get_models()
        }
        self.assertFalse(any("issuer" in name.lower() for name in model_names))
        self.assertEqual(ArchiveItem.objects.get(pk=378).title, _ITEM_378_TITLE)


class AcademicIndexSeedConflictTests(TestCase):
    def test_different_preferred_label_aborts_without_overwrite(self):
        _create_items()
        academic_index.create_topic(stable_key="AI-01", preferred_label="שונה")
        with self.assertRaises(AcademicIndexSeedError):
            _apply()
        self.assertEqual(
            AcademicTopic.objects.get(stable_key="AI-01").preferred_label, "שונה"
        )
        self.assertEqual(AcademicTopic.objects.count(), 1)
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), 0)
        self.assertEqual(ArchiveItemTopicAssociation.objects.count(), 0)

    def test_different_relevance_aborts_without_supersede(self):
        _create_items()
        academic_index.create_topic(
            stable_key="AI-02", preferred_label="ארגון העלייה ממצרים"
        )
        topic = AcademicTopic.objects.get(stable_key="AI-02")
        academic_index.approve_item_topic(
            ArchiveItem.objects.get(pk=378),
            topic,
            "SECONDARY",
        )
        before_events = AcademicIndexDecisionEvent.objects.count()
        with self.assertRaises(AcademicIndexSeedError):
            _apply()
        association = ArchiveItemTopicAssociation.objects.get(
            archive_item_id=378, topic=topic
        )
        self.assertEqual(association.relevance, "SECONDARY")
        self.assertEqual(
            association.status, ArchiveItemTopicAssociation.Status.APPROVED
        )
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), before_events)
        self.assertFalse(
            AcademicIndexDecisionEvent.objects.filter(
                action=AcademicIndexDecisionEvent.Action.SUPERSEDE
            ).exists()
        )
        self.assertEqual(AcademicTopic.objects.count(), 1)

    def test_partial_seed_rows_abort_without_filling_the_rest(self):
        _create_items()
        academic_index.create_topic(
            stable_key="AI-01",
            preferred_label="עליית יהודי מצרים לישראל",
            scope_note="",
            is_public_candidate=True,
            action=AcademicIndexDecisionEvent.Action.IMPORT,
            specification_sha256=SOURCE_SPECIFICATION_SHA256,
            operation_key=topic_operation_key("AI-01"),
            batch_id="topics",
            evidence_kind=AcademicIndexDecisionEvent.EvidenceKind.OWNER_APPROVAL,
            evidence_note=APPROVED_EVIDENCE_NOTE,
            reason="Approved academic index pilot seed from specification v2 section 5.",
        )
        before = _academic_counts()
        with self.assertRaises(AcademicIndexSeedError):
            _apply()
        self.assertEqual(_academic_counts(), before)
        self.assertFalse(AcademicTopic.objects.filter(stable_key="AI-02").exists())

    def test_an_extra_topic_aborts_and_leaves_the_seed_unchanged(self):
        _create_items()
        _apply()
        academic_index.create_topic(stable_key="AI-EXTRA", preferred_label="נוסף")
        before = _academic_counts()
        with self.assertRaises(AcademicIndexSeedError):
            _apply()
        self.assertEqual(_academic_counts(), before)
        self.assertEqual(
            AcademicTopic.objects.get(stable_key="AI-04").preferred_label,
            "הכשרת מדריכים בתנועות הנוער הציוניות במצרים",
        )
        self.assertFalse(
            AcademicTopicRelation.objects.filter(narrower__stable_key="AI-04").exists()
        )


def _catalog_sentinels() -> dict:
    person = Person.objects.create(id=186, name="מוריס מזרחי", honorific="פאשה")
    author = Author.objects.create(name="מוריס מזרחי", person=person)
    item = ArchiveItem.objects.get(pk=378)
    ArchiveItemAuthor.objects.create(archive_item=item, author=author, position=1)
    ArchiveItemPerson.objects.create(archive_item=item, person=person)
    entity = NonPersonEntity.objects.create(
        canonical_name="הנהלת בני עקיבא במצרים",
        entity_type=NonPersonEntity.EntityType.ORGANIZATION,
    )
    NonPersonEntityAlias.objects.create(
        entity=entity,
        name="בני עקיבא",
        kind=NonPersonEntityAlias.Kind.ABBREVIATION,
    )
    decision = ReviewedNonPersonEntityDecision.objects.create(
        source="academic-index-seed-test",
        candidate_id="seed-sentinel",
        workbook_sha256=_SHA,
        decision=ReviewedNonPersonEntityDecision.Decision.SKIP,
        review_status=ReviewedNonPersonEntityDecision.ReviewStatus.CLOSED,
    )
    ArchiveItemEntityOccurrence.objects.create(
        archive_item=item,
        text_kind=ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT,
        source_text_sha256=_SHA,
        normalized_surface="bnei akiva",
        occurrence_ordinal=1,
        resolution_status=ArchiveItemEntityOccurrence.ResolutionStatus.RESOLVED,
        entity=entity,
        decision=decision,
        matched_text="הנהלת בני עקיבא במצרים",
    )
    ArchiveCategory.objects.create(name="קטגוריה", slug="seed-category")
    ArchiveEvent.objects.create(name="אירוע", slug="seed-event")
    Tag.objects.create(name="seed-tag")
    photo_item = ArchiveItem.objects.create(
        title="Photo sentinel",
        item_type=ArchiveItem.ItemType.PHOTO,
        visibility=ArchiveItem.Visibility.PRIVATE,
    )
    photo = PhotoContent.objects.create(
        archive_item=photo_item,
        position=1,
        original_file_key="photos/seed-sentinel/original.jpg",
        original_filename="scan.jpg",
        original_mime_type="image/jpeg",
        original_size_bytes=10,
    )
    PhotoPerson.objects.create(photo_content=photo, person=person)
    other = ArchiveItem.objects.create(
        title="Document 386 host",
        item_type=ArchiveItem.ItemType.OCR_DOCUMENT,
        visibility=ArchiveItem.Visibility.PRIVATE,
    )
    document = Document.objects.create(
        id=386,
        doc_type=Document.DocType.IMAGE,
        text_input_type=Document.TextInputType.PRINTED,
        archive_item=other,
    )
    DocumentMetadata.objects.create(
        document=document,
        donor="secret-donor",
        collection="secret-collection",
        original_location="secret-location",
        notes="secret-notes",
    )
    return _catalog_fingerprint()


def _catalog_fingerprint() -> dict:
    return {
        "people": list(
            Person.objects.order_by("id").values_list("id", "name", "honorific")
        ),
        "authors": list(
            Author.objects.order_by("id").values_list("id", "name", "person_id")
        ),
        "item_authors": list(
            ArchiveItemAuthor.objects.order_by("id").values_list(
                "archive_item_id", "author_id", "position"
            )
        ),
        "item_people": list(
            ArchiveItemPerson.objects.order_by("id").values_list(
                "archive_item_id", "person_id"
            )
        ),
        "photo_people": list(
            PhotoPerson.objects.order_by("id").values_list(
                "photo_content_id", "person_id"
            )
        ),
        "entities": list(
            NonPersonEntity.objects.order_by("id").values_list(
                "id", "canonical_name", "entity_type"
            )
        ),
        "aliases": list(
            NonPersonEntityAlias.objects.order_by("id").values_list(
                "entity_id", "name", "kind"
            )
        ),
        "corrections": NonPersonEntityCorrection.objects.count(),
        "decisions": list(
            ReviewedNonPersonEntityDecision.objects.order_by("id").values_list(
                "id", "candidate_id", "decision"
            )
        ),
        "occurrences": list(
            ArchiveItemEntityOccurrence.objects.order_by("id").values_list(
                "archive_item_id",
                "entity_id",
                "source_text_sha256",
                "matched_text",
                "occurrence_ordinal",
            )
        ),
        "categories": list(
            ArchiveCategory.objects.order_by("id").values_list("id", "name", "slug")
        ),
        "events": list(
            ArchiveEvent.objects.order_by("id").values_list("id", "name", "slug")
        ),
        "tags": list(Tag.objects.order_by("id").values_list("id", "name")),
        "items": list(
            ArchiveItem.objects.order_by("id").values_list(
                "id", "title", "item_type", "visibility"
            )
        ),
        "documents": list(
            Document.objects.order_by("id").values_list(
                "id", "archive_item_id", "doc_type"
            )
        ),
        "metadata": list(
            DocumentMetadata.objects.order_by("id").values_list(
                "document_id", "donor", "collection", "original_location", "notes"
            )
        ),
    }


def _null_decision_pointer(model, pk: int) -> None:
    quoted = connection.ops.quote_name(model._meta.db_table)
    with connection.cursor() as cursor:
        cursor.execute(
            f"UPDATE {quoted} SET decision_event_id = NULL WHERE id = %s",
            [pk],
        )


class AcademicIndexSeedAuditPointerTests(TestCase):
    def test_null_decision_pointers_refuse_dry_run_and_apply(self):
        _create_items()
        _apply()
        association = ArchiveItemTopicAssociation.objects.get(
            archive_item_id=389, topic__stable_key="AI-01"
        )
        relation = AcademicTopicRelation.objects.get(
            relation_kind=AcademicTopicRelation.Kind.BROADER,
            narrower__stable_key="AI-02",
            broader__stable_key="AI-01",
        )
        _null_decision_pointer(ArchiveItemTopicAssociation, association.pk)
        _null_decision_pointer(AcademicTopicRelation, relation.pk)
        events_before = AcademicIndexDecisionEvent.objects.count()

        inspection = inspect_approved_seed()
        self.assertEqual(inspection.outcome, "aborted")
        self.assertTrue(
            any(
                "missing its decision event pointer" in conflict
                for conflict in inspection.conflicts
            )
        )
        with self.assertRaises(CommandError) as dry_run:
            call_command("import_academic_index_seed", stdout=StringIO())
        self.assertIn("missing its decision event pointer", str(dry_run.exception))
        with self.assertRaises(AcademicIndexSeedError):
            _apply()
        with self.assertRaises(CommandError):
            _command_apply()

        association.refresh_from_db()
        relation.refresh_from_db()
        self.assertIsNone(association.decision_event_id)
        self.assertIsNone(relation.decision_event_id)
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), events_before)
        self.assertEqual(AcademicTopic.objects.count(), 11)

    def test_incorrect_decision_pointer_is_a_conflict_and_is_not_repaired(self):
        _create_items()
        _apply()
        association = ArchiveItemTopicAssociation.objects.get(
            archive_item_id=430, topic__stable_key="AI-03"
        )
        other = AcademicIndexDecisionEvent.objects.get(
            operation_key=topic_operation_key("AI-01")
        )
        quoted = connection.ops.quote_name(ArchiveItemTopicAssociation._meta.db_table)
        with connection.cursor() as cursor:
            cursor.execute(
                f"UPDATE {quoted} SET decision_event_id = %s WHERE id = %s",
                [other.pk, association.pk],
            )
        events_before = list(
            AcademicIndexDecisionEvent.objects.order_by("id").values_list(
                "id", "action"
            )
        )

        inspection = inspect_approved_seed()
        self.assertEqual(inspection.outcome, "aborted")
        self.assertTrue(
            any(
                "does not audit this row" in conflict
                for conflict in inspection.conflicts
            )
        )
        with self.assertRaises(AcademicIndexSeedError):
            _apply()

        association.refresh_from_db()
        self.assertEqual(association.decision_event_id, other.pk)
        self.assertEqual(
            list(
                AcademicIndexDecisionEvent.objects.order_by("id").values_list(
                    "id", "action"
                )
            ),
            events_before,
        )

    def test_later_audited_reapproval_remains_an_exact_seed(self):
        _create_items()
        _apply()
        association = ArchiveItemTopicAssociation.objects.get(
            archive_item_id=430, topic__stable_key="AI-03"
        )
        import_event_id = association.decision_event_id
        events_before = AcademicIndexDecisionEvent.objects.count()
        academic_index.retract_item_topic(
            association,
            evidence_note="Later owner retract.",
            reason="Later owner retract.",
        )
        academic_index.approve_item_topic(
            ArchiveItem.objects.get(pk=430),
            AcademicTopic.objects.get(stable_key="AI-03"),
            ArchiveItemTopicAssociation.Relevance.CENTRAL,
            evidence_note="Later owner approval.",
            reason="Later owner approval.",
        )
        association.refresh_from_db()
        later_event = association.decision_event
        self.assertIsNotNone(later_event)
        assert later_event is not None
        self.assertNotEqual(later_event.pk, import_event_id)
        self.assertEqual(
            later_event.action,
            AcademicIndexDecisionEvent.Action.APPROVE,
        )

        inspection = inspect_approved_seed()
        self.assertEqual(inspection.outcome, "already_applied", inspection.conflicts)
        result = _apply()
        self.assertEqual(result.outcome, "already_applied")
        self.assertEqual(result.decision_events_written, 0)
        association.refresh_from_db()
        self.assertNotEqual(association.decision_event_id, import_event_id)
        self.assertEqual(
            AcademicIndexDecisionEvent.objects.count(),
            events_before + 2,
        )

    def test_dry_run_does_not_take_the_apply_lock(self):
        _create_items()

        def boom():
            raise AssertionError("dry-run took the apply lock")

        with patch(
            "documents.services.academic_index_seed._lock_seed_apply",
            boom,
        ):
            stdout = StringIO()
            call_command("import_academic_index_seed", stdout=stdout)
        self.assertIn("dry-run", stdout.getvalue())
        self.assertEqual(_academic_counts()["events"], 0)


class AcademicIndexSeedConcurrencyTests(TransactionTestCase):
    def test_concurrent_identical_applies_commit_one_seed(self):
        self.assertTrue(str(connection.settings_dict["NAME"]).startswith("test_"))
        _create_items()
        barrier = threading.Barrier(2)
        outcomes = []
        errors = []

        def worker():
            try:
                barrier.wait(timeout=30)
                result = _apply()
                outcomes.append((result.outcome, result.decision_events_written))
            except Exception as exc:  # pragma: no cover - assertion reports it
                errors.append(repr(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(errors, [])
        self.assertEqual(sorted(outcomes), [("already_applied", 0), ("applied", 40)])
        self.assertEqual(AcademicTopic.objects.count(), 11)
        self.assertEqual(AcademicTopicRelation.objects.count(), 7)
        self.assertEqual(ArchiveItemTopicAssociation.objects.count(), 22)
        self.assertEqual(AcademicIndexDecisionEvent.objects.count(), 40)
        self.assertEqual(
            set(
                AcademicIndexDecisionEvent.objects.values_list(
                    "operation_key", flat=True
                )
            ),
            set(all_operation_keys()),
        )

    def test_concurrent_conflicting_applies_leave_the_existing_row(self):
        self.assertTrue(str(connection.settings_dict["NAME"]).startswith("test_"))
        _create_items()
        academic_index.create_topic(
            stable_key="AI-01",
            preferred_label="not the approved label",
            evidence_note="preexisting",
            reason="preexisting",
        )
        before = _academic_counts()
        barrier = threading.Barrier(2)
        errors = []

        def worker():
            try:
                barrier.wait(timeout=30)
                _apply()
                errors.append("apply succeeded")
            except AcademicIndexSeedError as exc:
                errors.append(str(exc))
            except Exception as exc:  # pragma: no cover - assertion reports it
                errors.append(repr(exc))
            finally:
                connection.close()

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=120)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(len(errors), 2)
        self.assertTrue(all("apply succeeded" not in error for error in errors))
        self.assertEqual(_academic_counts(), before)
        self.assertEqual(
            AcademicTopic.objects.get(stable_key="AI-01").preferred_label,
            "not the approved label",
        )
        self.assertEqual(ArchiveItemTopicAssociation.objects.count(), 0)
