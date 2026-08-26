"""Frozen production Tag.id → Person.id map plus retired historical Tag names.

Identity is Tag.id → Person.id. Person.name is not unique and is never
used as a lookup key. Frozen Tag names are an immutable retired-name
policy (D2a): exact match after caller tag-name parse/trim only. They
are not Person identity, aliases, or a broad person-name denylist.

Runtime consumers may derive the blocked Tag-id set from this map, look
up Person ids by Tag id, and test exact retired Tag names. They must not
look up Person by name and must not import migration modules.

Stage A public presentation, Stage B mapped-tag browse redirects / public
Tag-choice hiding, D1 relation cleanup, mapped-Tag reuse/delete blocks,
D2a retired-name write-path blocks, and D2b Tag-row deletion
(``delete_historical_person_tag_rows``) use this map. Mapped Tag rows may
be absent after D2b; the map remains the identity and retired-name
artifact.
"""

from __future__ import annotations

# Frozen production triples: Tag.id, Person.id, exact historical Tag.name.
# Person ids 1–29 are 0055 creation order (APPROVED_PERSON_NAME_TAGS
# sequence after an empty Person table). Names are retired-name policy
# only and must already be trimmed non-empty unique strings.
HISTORICAL_PERSON_NAME_TAG_RECORDS: tuple[tuple[int, int, str], ...] = (
    (2, 1, "רפאל רקנטי"),
    (4, 2, "פליקס בן זקן"),
    (5, 3, "יוסף קטאוי"),
    (7, 4, "אלי פלג"),
    (8, 5, "אליהו ברכה"),
    (10, 6, "לאון קסטרו"),
    (11, 7, "הרב נחום אפנדי"),
    (14, 8, "מדרכי אביצור"),
    (15, 9, "הרב דר' משה ונטורה"),
    (16, 10, "אלי כהן"),
    (19, 11, "המלך פארוק"),
    (20, 12, "יולנדה הארמר- גבאי"),
    (23, 13, "משה מרזוק"),
    (24, 14, "שמואל עזר"),
    (25, 15, "איסר הראל"),
    (26, 16, "רוברט דסה"),
    (27, 17, "ויקטור לוי"),
    (28, 18, "מרסל ניניו"),
    (29, 19, "שלמה הלל"),
    (30, 20, "שלמה פלטנר"),
    (31, 21, "מקס בינט"),
    (32, 22, "אלי נעים"),
    (33, 23, "יצחק לוי - גבלאוי"),
    (34, 24, "שמואל שפיטלניק"),
    (35, 25, "פיליפ נתנזון"),
    (36, 26, "מוריס זקס"),
    (37, 27, "אברי אלעד"),
    (38, 28, "עובדיה דנון"),
    (39, 29, "מאיר מיוחס"),
)

_EXPECTED_HISTORICAL_PERSON_TAG_RECORD_COUNT = 29


def validate_historical_person_name_tag_records(
    records: tuple[tuple[int, int, str], ...] = HISTORICAL_PERSON_NAME_TAG_RECORDS,
) -> None:
    """Fail closed if the frozen artifact is incomplete or non-unique."""
    if len(records) != _EXPECTED_HISTORICAL_PERSON_TAG_RECORD_COUNT:
        raise ValueError(
            "historical person-name Tag records must contain "
            f"{_EXPECTED_HISTORICAL_PERSON_TAG_RECORD_COUNT} entries, "
            f"got {len(records)}"
        )
    tag_ids = [tag_id for tag_id, _person_id, _name in records]
    person_ids = [person_id for _tag_id, person_id, _name in records]
    names = [name for _tag_id, _person_id, name in records]
    if len(set(tag_ids)) != len(tag_ids):
        raise ValueError("historical person-name Tag ids must be unique")
    if len(set(person_ids)) != len(person_ids):
        raise ValueError("historical person-name Person ids must be unique")
    if len(set(names)) != len(names):
        raise ValueError("historical person-name Tag names must be unique")
    for name in names:
        if not isinstance(name, str) or not name or name != name.strip():
            raise ValueError(
                "historical person-name Tag names must be non-empty and already trimmed"
            )


validate_historical_person_name_tag_records()

# Keyed by Tag.id only. Do not key or look up by Person.name / Tag.name.
HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID: tuple[tuple[int, int], ...] = tuple(
    (tag_id, person_id)
    for tag_id, person_id, _name in HISTORICAL_PERSON_NAME_TAG_RECORDS
)
PERSON_ID_BY_HISTORICAL_PERSON_NAME_TAG_ID: dict[int, int] = dict(
    HISTORICAL_PERSON_NAME_TAG_TO_PERSON_ID
)
_RETIRED_HISTORICAL_PERSON_TAG_NAMES: frozenset[str] = frozenset(
    name for _tag_id, _person_id, name in HISTORICAL_PERSON_NAME_TAG_RECORDS
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


def historical_person_tag_retired_names() -> frozenset[str]:
    """Return the frozen historical Tag.name set. Exact strings; not Person.name."""
    return _RETIRED_HISTORICAL_PERSON_TAG_NAMES


def is_retired_historical_person_tag_name(name: str) -> bool:
    """True when ``name`` equals a frozen historical Tag.name exactly.

    Callers must pass already-normalized names (existing tag-name parse/trim).
    No casefold, fuzzy, alias, or Person.name matching.
    """
    return name in _RETIRED_HISTORICAL_PERSON_TAG_NAMES
