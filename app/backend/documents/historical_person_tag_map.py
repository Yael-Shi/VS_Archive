"""Frozen production Tag.id → Person.id map from migration 0055.

Identity is Tag.id → Person.id. Person.name is not unique and is never
used as a lookup key. Names in comments are display/provenance-only.

Runtime consumers may derive the blocked Tag-id set from this map and
look up Person ids by Tag id. They must not look up by name. Stage A
public presentation, Stage B mapped-tag browse redirects / public
Tag-choice hiding, D1 relation cleanup, and mapped-Tag reuse/delete
blocks use this map. D2 Tag-row deletion remains out of scope.
"""

from __future__ import annotations

# Frozen production pairs. Person ids 1–29 are 0055 creation order
# (APPROVED_PERSON_NAME_TAGS sequence after an empty Person table).
# Display/provenance-only names below are not identity.
HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID: tuple[tuple[int, int], ...] = (
    (2, 1),  # display/provenance-only: רפאל רקנטי
    (4, 2),  # display/provenance-only: פליקס בן זקן
    (5, 3),  # display/provenance-only: יוסף קטאוי
    (7, 4),  # display/provenance-only: אלי פלג
    (8, 5),  # display/provenance-only: אליהו ברכה
    (10, 6),  # display/provenance-only: לאון קסטרו
    (11, 7),  # display/provenance-only: הרב נחום אפנדי
    (14, 8),  # display/provenance-only: מדרכי אביצור
    (15, 9),  # display/provenance-only: הרב דר' משה ונטורה
    (16, 10),  # display/provenance-only: אלי כהן
    (19, 11),  # display/provenance-only: המלך פארוק
    (20, 12),  # display/provenance-only: יולנדה הארמר- גבאי
    (23, 13),  # display/provenance-only: משה מרזוק
    (24, 14),  # display/provenance-only: שמואל עזר
    (25, 15),  # display/provenance-only: איסר הראל
    (26, 16),  # display/provenance-only: רוברט דסה
    (27, 17),  # display/provenance-only: ויקטור לוי
    (28, 18),  # display/provenance-only: מרסל ניניו
    (29, 19),  # display/provenance-only: שלמה הלל
    (30, 20),  # display/provenance-only: שלמה פלטנר
    (31, 21),  # display/provenance-only: מקס בינט
    (32, 22),  # display/provenance-only: אלי נעים
    (33, 23),  # display/provenance-only: יצחק לוי - גבלאוי
    (34, 24),  # display/provenance-only: שמואל שפיטלניק
    (35, 25),  # display/provenance-only: פיליפ נתנזון
    (36, 26),  # display/provenance-only: מוריס זקס
    (37, 27),  # display/provenance-only: אברי אלעד
    (38, 28),  # display/provenance-only: עובדיה דנון
    (39, 29),  # display/provenance-only: מאיר מיוחס
)

# Keyed by Tag.id only. Do not key or look up by Person.name / Tag.name.
PERSON_ID_BY_HISTORICAL_PERSON_NAME_TAG_ID: dict[int, int] = dict(
    HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID
)


def person_id_for_historical_person_name_tag(tag_id: int) -> int | None:
    """Return the production Person.id for a frozen historical person Tag.id.

    Unknown Tag ids return None. There is no name parameter.
    """
    return PERSON_ID_BY_HISTORICAL_PERSON_NAME_TAG_ID.get(tag_id)


def historical_person_name_tag_ids() -> frozenset[int]:
    """Return the frozen historical person Tag.id set. ID membership only."""
    return frozenset(PERSON_ID_BY_HISTORICAL_PERSON_NAME_TAG_ID)


def is_historical_person_name_tag(tag_id: int) -> bool:
    """True when ``tag_id`` is a frozen historical person Tag.id. ID only."""
    return tag_id in PERSON_ID_BY_HISTORICAL_PERSON_NAME_TAG_ID
