"""Regression: auto Tag PKs stay above frozen historical person-name Tag ids."""

from __future__ import annotations

import inspect
from unittest.mock import patch

from django.apps import apps
from django.core.management.color import no_style
from django.db import DEFAULT_DB_ALIAS, connection, connections
from django.db.models.query import QuerySet
from django.test import TestCase, TransactionTestCase
from django.utils.connection import ConnectionDoesNotExist

from documents.historical_person_tag_map import (
    historical_person_name_tag_ids,
    is_historical_person_name_tag,
    is_retired_historical_person_tag_name,
)
from documents.models import Tag
from documents.services.archive_items import (
    create_manual_text_archive_item,
    update_archive_item_discovery_metadata,
)
from documents.tag_pk_sequence_support import (
    SENTINEL_TAG_NAME_PREFIX,
    ensure_tag_pk_sequence_past_historical_ids,
    reset_pk_sequence,
)


def _frozen_max() -> int:
    return max(historical_person_name_tag_ids())


def _force_tag_sequence_to_django_empty_start() -> None:
    """Reproduce Django empty-table sequence reset (next auto PK would be 1)."""
    Tag.objects.all().delete()
    statements = connection.ops.sequence_reset_sql(no_style(), [Tag])
    with connection.cursor() as cursor:
        for sql in statements:
            cursor.execute(sql)
    if not statements:
        sqlite_sql = connection.ops.sequence_reset_by_name_sql(
            no_style(),
            [{"table": Tag._meta.db_table, "column": Tag._meta.pk.column}],
        )
        with connection.cursor() as cursor:
            for sql in sqlite_sql:
                cursor.execute(sql)


class TagPkSequenceEmptyResetTests(TestCase):
    def test_auto_tag_pk_is_above_frozen_max_after_reset_to_empty(self):
        sample_name = f"{SENTINEL_TAG_NAME_PREFIX}{'a' * 32}"
        self.assertLessEqual(len(sample_name), 64)
        self.assertFalse(is_retired_historical_person_tag_name(sample_name))

        _force_tag_sequence_to_django_empty_start()
        ensure_tag_pk_sequence_past_historical_ids(using=DEFAULT_DB_ALIAS)
        self.assertFalse(
            Tag.objects.filter(name__startswith=SENTINEL_TAG_NAME_PREFIX).exists()
        )

        tag = Tag.objects.create(name="ordinary-after-empty-reset")
        self.assertGreater(tag.pk, _frozen_max())
        self.assertFalse(is_historical_person_name_tag(tag.pk))

    def test_ensure_does_not_delete_preexisting_tags(self):
        keep = Tag.objects.create(name="keep-me-not-a-sentinel")
        decoy = Tag.objects.create(
            name=f"{SENTINEL_TAG_NAME_PREFIX}decoy_not_this_invocation"
        )
        ensure_tag_pk_sequence_past_historical_ids(using=DEFAULT_DB_ALIAS)
        self.assertTrue(Tag.objects.filter(pk=keep.pk, name=keep.name).exists())
        self.assertTrue(Tag.objects.filter(pk=decoy.pk, name=decoy.name).exists())

    def test_sentinel_removed_when_sequence_reset_raises(self):
        keep = Tag.objects.create(name="survive-failed-ensure")
        before = set(Tag.objects.values_list("pk", "name"))
        ops = connections[DEFAULT_DB_ALIAS].ops
        with patch.object(ops, "sequence_reset_sql", side_effect=RuntimeError("boom")):
            with self.assertRaisesMessage(RuntimeError, "boom"):
                ensure_tag_pk_sequence_past_historical_ids(using=DEFAULT_DB_ALIAS)
        self.assertEqual(set(Tag.objects.values_list("pk", "name")), before)
        self.assertTrue(Tag.objects.filter(pk=keep.pk).exists())

    def test_reset_pk_sequence_uses_provided_tag_model(self):
        tag_model = apps.get_model("documents", "Tag")
        mapped_id = min(historical_person_name_tag_ids())
        mapped = tag_model.objects.create(
            pk=mapped_id,
            name="mapped-via-provided-tag-model",
        )
        reset_pk_sequence(tag_model, using=DEFAULT_DB_ALIAS)
        ordinary = tag_model.objects.create(name="ordinary-via-provided-tag-model")
        self.assertEqual(mapped.pk, mapped_id)
        self.assertGreater(ordinary.pk, _frozen_max())
        self.assertTrue(tag_model.objects.filter(pk=mapped_id).exists())

    def test_reset_pk_sequence_does_not_substitute_live_tag_for_tag_table(self):
        class FakeHistoricalTag:
            class _meta:
                db_table = Tag._meta.db_table

        with patch(
            "documents.tag_pk_sequence_support.ensure_tag_pk_sequence_past_historical_ids"
        ) as mocked:
            reset_pk_sequence(FakeHistoricalTag, using=DEFAULT_DB_ALIAS)
        mocked.assert_called_once_with(
            using=DEFAULT_DB_ALIAS,
            tag_model=FakeHistoricalTag,
        )

    def test_ensure_and_reset_use_requested_alias(self):
        with self.assertRaises(ConnectionDoesNotExist):
            ensure_tag_pk_sequence_past_historical_ids(using="not_a_configured_alias")
        with self.assertRaises(ConnectionDoesNotExist):
            reset_pk_sequence(Tag, using="not_a_configured_alias")

    def test_ensure_runs_sequence_reset_sql_on_alias_ops_for_provided_model(self):
        recorded = {}
        ops = connections[DEFAULT_DB_ALIAS].ops
        real = ops.sequence_reset_sql

        def spy(style, model_list):
            recorded["models"] = list(model_list)
            return real(style, model_list)

        with patch.object(ops, "sequence_reset_sql", side_effect=spy):
            ensure_tag_pk_sequence_past_historical_ids(
                using=DEFAULT_DB_ALIAS,
                tag_model=Tag,
            )
        self.assertEqual(recorded["models"], [Tag])

    def test_ensure_does_not_call_sequence_reset_by_name_sql(self):
        ops = connections[DEFAULT_DB_ALIAS].ops
        with patch.object(
            ops,
            "sequence_reset_by_name_sql",
            wraps=ops.sequence_reset_by_name_sql,
        ) as spy:
            ensure_tag_pk_sequence_past_historical_ids(using=DEFAULT_DB_ALIAS)
        spy.assert_not_called()

    def test_cleanup_failure_does_not_hide_sequence_reset_error(self):
        ops = connections[DEFAULT_DB_ALIAS].ops
        with patch.object(QuerySet, "delete", side_effect=RuntimeError("cleanup-fail")):
            with patch.object(
                ops, "sequence_reset_sql", side_effect=RuntimeError("boom")
            ):
                with self.assertRaisesMessage(RuntimeError, "boom"):
                    ensure_tag_pk_sequence_past_historical_ids(using=DEFAULT_DB_ALIAS)


class TagPkSequenceTransactionResetTests(TransactionTestCase):
    reset_sequences = True

    def test_reset_sequences_true_does_not_restore_frozen_collision(self):
        tag = Tag.objects.create(name="ordinary-after-transaction-reset")
        self.assertGreater(tag.pk, _frozen_max())
        self.assertFalse(is_historical_person_name_tag(tag.pk))

    def test_three_step_order_reproducer_stays_unmapped(self):
        """reset-to-1, one auto Tag, then discovery ``And Tag`` must not hit frozen ids.

        Same collision as the preflight CLI order (TransactionTestCase
        ``reset_sequences=True``, then ``test_empty_environment_is_noop``, then
        ``test_person_group_ands_with_category_event_tag_and_year``).
        """
        first = Tag.objects.create(name="unrelated-topic")
        self.assertGreater(first.pk, _frozen_max())
        first.delete()

        victim = Tag.objects.create(name="And Tag")
        self.assertGreater(victim.pk, _frozen_max())
        self.assertFalse(is_historical_person_name_tag(victim.pk))

        item = create_manual_text_archive_item(title="And match", body="body")
        update_archive_item_discovery_metadata(
            item,
            category_names=[],
            event_names=[],
            tag_names=["And Tag"],
        )
        self.assertEqual(
            list(item.tags.values_list("name", flat=True)),
            ["And Tag"],
        )

    def test_reset_sequences_wrap_is_idempotent(self):
        # Private Django API: TransactionTestCase._reset_sequences
        # (django.test.testcases). The project wrap must stay idempotent.
        from django.test import TransactionTestCase as DjangoTransactionTestCase

        import conftest

        first = DjangoTransactionTestCase._reset_sequences
        self.assertTrue(getattr(first, "_vs_archive_tag_pk_guard", False))
        self.assertEqual(list(inspect.signature(first).parameters), ["db_name"])
        conftest._install_transaction_testcase_tag_pk_guard()
        conftest._install_transaction_testcase_tag_pk_guard()
        self.assertIs(DjangoTransactionTestCase._reset_sequences, first)

    def test_reset_sequences_wrap_advances_tag_on_the_reset_alias(self):
        from django.test import TransactionTestCase as DjangoTransactionTestCase

        with patch(
            "documents.tag_pk_sequence_support.ensure_tag_pk_sequence_past_historical_ids"
        ) as mocked:
            DjangoTransactionTestCase._reset_sequences(DEFAULT_DB_ALIAS)
            mocked.assert_called_once_with(using=DEFAULT_DB_ALIAS, tag_model=Tag)
        ensure_tag_pk_sequence_past_historical_ids(using=DEFAULT_DB_ALIAS)

    def test_reset_sequences_wrap_skips_ensure_for_non_tag_write_alias(self):
        from django.db import router
        from django.test import TransactionTestCase as DjangoTransactionTestCase

        with patch.object(router, "db_for_write", return_value="other_alias"):
            with patch(
                "documents.tag_pk_sequence_support.ensure_tag_pk_sequence_past_historical_ids"
            ) as mocked:
                DjangoTransactionTestCase._reset_sequences(DEFAULT_DB_ALIAS)
                mocked.assert_not_called()
        ensure_tag_pk_sequence_past_historical_ids(using=DEFAULT_DB_ALIAS)


class TagPkSequenceMappedIdTests(TestCase):
    def test_explicit_mapped_id_create_still_works(self):
        mapped_id = min(historical_person_name_tag_ids())
        mapped = Tag.objects.create(
            pk=mapped_id,
            name="mapped-id-still-allowed-in-tests",
        )
        self.assertEqual(mapped.pk, mapped_id)
        self.assertTrue(is_historical_person_name_tag(mapped.pk))

        ensure_tag_pk_sequence_past_historical_ids(using=DEFAULT_DB_ALIAS)
        ordinary = Tag.objects.create(name="ordinary-after-mapped-id")
        self.assertGreater(ordinary.pk, _frozen_max())
        self.assertNotEqual(ordinary.pk, mapped_id)
        self.assertTrue(Tag.objects.filter(pk=mapped_id).exists())
