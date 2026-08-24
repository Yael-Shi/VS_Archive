"""Structural tests for the frozen historical person-name Tag → Person map."""

from __future__ import annotations

import importlib
import inspect
from unittest import TestCase

from documents.historical_person_tag_map import (
    HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID,
    PERSON_ID_BY_HISTORICAL_PERSON_NAME_TAG_ID,
    historical_person_name_tag_ids,
    is_historical_person_name_tag,
    person_id_for_historical_person_name_tag,
)

_migration_module = importlib.import_module(
    "documents.migrations.0055_backfill_person_from_person_name_tags"
)
APPROVED_PERSON_NAME_TAG_IDS = _migration_module.APPROVED_PERSON_NAME_TAG_IDS

EXPECTED_PAIRS: tuple[tuple[int, int], ...] = (
    (2, 1),
    (4, 2),
    (5, 3),
    (7, 4),
    (8, 5),
    (10, 6),
    (11, 7),
    (14, 8),
    (15, 9),
    (16, 10),
    (19, 11),
    (20, 12),
    (23, 13),
    (24, 14),
    (25, 15),
    (26, 16),
    (27, 17),
    (28, 18),
    (29, 19),
    (30, 20),
    (31, 21),
    (32, 22),
    (33, 23),
    (34, 24),
    (35, 25),
    (36, 26),
    (37, 27),
    (38, 28),
    (39, 29),
)


class HistoricalPersonTagMapTests(TestCase):
    def test_encodes_exactly_twenty_nine_tag_id_person_id_pairs(self):
        self.assertEqual(HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID, EXPECTED_PAIRS)
        self.assertEqual(len(HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID), 29)

    def test_tag_ids_match_approved_0055_ids_in_order(self):
        tag_ids = tuple(
            tag_id for tag_id, _person_id in HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID
        )
        self.assertEqual(tag_ids, APPROVED_PERSON_NAME_TAG_IDS)

    def test_tag_ids_are_unique(self):
        tag_ids = [
            tag_id for tag_id, _person_id in HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID
        ]
        self.assertEqual(len(tag_ids), len(set(tag_ids)))

    def test_person_ids_are_unique(self):
        person_ids = [
            person_id for _tag_id, person_id in HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID
        ]
        self.assertEqual(len(person_ids), len(set(person_ids)))

    def test_lookup_dict_is_keyed_only_by_tag_id(self):
        self.assertEqual(
            PERSON_ID_BY_HISTORICAL_PERSON_NAME_TAG_ID,
            dict(HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID),
        )
        self.assertEqual(
            set(PERSON_ID_BY_HISTORICAL_PERSON_NAME_TAG_ID),
            set(APPROVED_PERSON_NAME_TAG_IDS),
        )

    def test_lookup_uses_tag_id_only(self):
        signature = inspect.signature(person_id_for_historical_person_name_tag)
        self.assertEqual(list(signature.parameters), ["tag_id"])
        self.assertIn(signature.parameters["tag_id"].annotation, (int, "int"))
        self.assertNotIn("name", signature.parameters)
        for tag_id, person_id in EXPECTED_PAIRS:
            self.assertEqual(
                person_id_for_historical_person_name_tag(tag_id),
                person_id,
            )

    def test_unknown_tag_id_returns_none(self):
        self.assertIsNone(person_id_for_historical_person_name_tag(0))
        self.assertIsNone(person_id_for_historical_person_name_tag(1))
        self.assertIsNone(person_id_for_historical_person_name_tag(3))
        self.assertIsNone(person_id_for_historical_person_name_tag(40))
        self.assertIsNone(person_id_for_historical_person_name_tag(-2))

    def test_blocked_id_set_is_derived_from_the_frozen_map_only(self):
        self.assertEqual(
            historical_person_name_tag_ids(),
            frozenset(PERSON_ID_BY_HISTORICAL_PERSON_NAME_TAG_ID),
        )
        self.assertEqual(len(historical_person_name_tag_ids()), 29)
        signature = inspect.signature(historical_person_name_tag_ids)
        self.assertEqual(list(signature.parameters), [])
        membership = inspect.signature(is_historical_person_name_tag)
        self.assertEqual(list(membership.parameters), ["tag_id"])
        self.assertNotIn("name", membership.parameters)
        for tag_id, _person_id in EXPECTED_PAIRS:
            self.assertTrue(is_historical_person_name_tag(tag_id))
        self.assertFalse(is_historical_person_name_tag(1))
        self.assertFalse(is_historical_person_name_tag(3))
        self.assertFalse(is_historical_person_name_tag(40))

    def test_module_has_no_name_based_lookup(self):
        module = importlib.import_module("documents.historical_person_tag_map")
        public_names = [name for name in dir(module) if not name.startswith("_")]
        self.assertNotIn("person_id_for_historical_person_name", public_names)
        for name in public_names:
            self.assertNotIn("by_name", name.lower())
            self.assertNotIn("from_name", name.lower())
            self.assertNotIn("name_to", name.lower())
