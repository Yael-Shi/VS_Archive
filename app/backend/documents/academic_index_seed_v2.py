"""Approved Academic Index pilot seed, specification v2 section 5.

The tuples below are the human-approved values. The editorial "Other" column
in that section is not a scope note and is not imported. AI-04 has no broader
edge. There is no issuer row.

``SOURCE_SPECIFICATION_SHA256`` is the v1 specification SHA recorded in v2
section 0 and required on IMPORT events by v2 section 7.5. It is not a hash
of this module. ``SPECIFICATION_V2_DOCUMENT_SHA256`` identifies the v2 file
these tuples were copied from. Decision events store the v1 SHA.

``APPROVED_EVIDENCE_NOTE`` is the owner-approved evidence note. It is stored
with ``evidence_kind=OWNER_APPROVAL``. Provisional and placeholder notes,
including ``IMAGE_NOT_COLLATED``, are rejected before apply.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

SOURCE_SPECIFICATION_SHA256 = (
    "a709aa3a750975b06630d867cc365f2abfb0ce238624ff6676ac0dae8ffab15e"
)
SPECIFICATION_V2_DOCUMENT_SHA256 = (
    "3a3b33f07066217bce2c56e2521585396c793b64e8fe32ad761e6d11cbc29a7f"
)

APPROVED_EVIDENCE_NOTE = (
    "שיוך או קשר שאושר על ידי מנהלת הארכיון במסגרת פיילוט המפתח האקדמי, "
    "על בסיס סקירת מסמכי הארכיון. תמונות המקור לא נבדקו מחדש באופן עצמאי "
    "במסגרת טעינת הנתונים."
)
_PLACEHOLDER_EVIDENCE_NOTES = frozenset(
    {
        "provisional_evidence_note_pending_owner_approval",
        "image_not_collated",
        "provisional",
        "placeholder",
        "tbd",
        "todo",
        "pending",
        "n/a",
        "na",
        "none",
        "unknown",
    }
)
_PLACEHOLDER_EVIDENCE_MARKERS = (
    "provisional",
    "placeholder",
    "image_not_collated",
    "pending_owner_approval",
)
SEED_REASON = "Approved academic index pilot seed from specification v2 section 5."
TOPIC_BATCH_ID = "topics"
OPERATION_KEY_PREFIX = "academic-index-seed-v2"

APPLY_CONFIRM_TOKEN = "academic-index-seed-v2-apply"
OWNER_AUTHORIZATION_TOKEN = "ACADEMIC_INDEX_SEED_V2_OWNER_AUTHORIZED"

EXPECTED_TOPIC_COUNT = 11
EXPECTED_BROADER_COUNT = 6
EXPECTED_RELATED_COUNT = 1
EXPECTED_ASSOCIATION_COUNT = 22
EXPECTED_DISTINCT_ITEM_COUNT = 15
EXPECTED_ISSUER_ROW_COUNT = 0

PINNED_DISTINCT_ITEM_IDS = (
    13,
    144,
    147,
    163,
    164,
    173,
    264,
    284,
    301,
    324,
    366,
    378,
    379,
    389,
    430,
)
EXCLUDED_ARCHIVE_ITEM_IDS = (84, 360, 363, 386, 422)
ALTERNATE_LABELS: tuple[tuple[str, str], ...] = ()

APPROVED_FIXTURE_SHA256 = (
    "950d028c1594127907cd916aaaa31eae8d07a0f5573eaa33e784ba1803790caa"
)
APPROVED_FIXTURE_CANONICAL = """\
topic	AI-01	עליית יהודי מצרים לישראל
topic	AI-02	ארגון העלייה ממצרים
topic	AI-03	הכשרה חלוצית של עולים ממצרים
topic	AI-04	הכשרת מדריכים בתנועות הנוער הציוניות במצרים
topic	AI-05	המאבק לשחרור אסירי הפרשה
topic	AI-06	רפואת משפחה בישראל
topic	AI-07	רפואה קהילתית ועבודת המרפאה
topic	AI-08	התפתחות רפואת המשפחה בישראל
topic	AI-09	הוראה והכשרת רופאי משפחה
topic	AI-10	חילופי שבויים בהקשר של אסירי הפרשה
topic	AI-11	הנצחת אסירי הפרשה והרוגיה
broader	AI-02	AI-01
broader	AI-03	AI-01
broader	AI-07	AI-06
broader	AI-08	AI-06
broader	AI-09	AI-06
broader	AI-10	AI-05
related	AI-05	AI-11
association	378	AI-02	CENTRAL	01
association	378	AI-04	CENTRAL	01
association	379	AI-02	CENTRAL	01
association	389	AI-01	CENTRAL	01
association	264	AI-01	CENTRAL	01
association	366	AI-03	SECONDARY	01
association	430	AI-03	CENTRAL	01
association	147	AI-05	CENTRAL	01
association	164	AI-05	CENTRAL	01
association	173	AI-05	CENTRAL	01
association	144	AI-05	CENTRAL	01
association	324	AI-05	SECONDARY	01
association	13	AI-08	CENTRAL	02
association	13	AI-09	CENTRAL	02
association	284	AI-07	CENTRAL	02
association	284	AI-09	SECONDARY	02
association	164	AI-10	CENTRAL	03
association	173	AI-10	CENTRAL	03
association	144	AI-10	SECONDARY	03
association	324	AI-10	CENTRAL	03
association	163	AI-11	CENTRAL	03
association	301	AI-11	CENTRAL	03
"""


@dataclass(frozen=True)
class SeedTopic:
    stable_key: str
    preferred_label: str


@dataclass(frozen=True)
class BroaderEdge:
    narrower_key: str
    broader_key: str


@dataclass(frozen=True)
class RelatedEdge:
    left_key: str
    right_key: str


@dataclass(frozen=True)
class SeedAssociation:
    archive_item_id: int
    topic_key: str
    relevance: str
    batch_id: str


TOPICS: tuple[SeedTopic, ...] = (
    SeedTopic("AI-01", "עליית יהודי מצרים לישראל"),
    SeedTopic("AI-02", "ארגון העלייה ממצרים"),
    SeedTopic("AI-03", "הכשרה חלוצית של עולים ממצרים"),
    SeedTopic("AI-04", "הכשרת מדריכים בתנועות הנוער הציוניות במצרים"),
    SeedTopic("AI-05", "המאבק לשחרור אסירי הפרשה"),
    SeedTopic("AI-06", "רפואת משפחה בישראל"),
    SeedTopic("AI-07", "רפואה קהילתית ועבודת המרפאה"),
    SeedTopic("AI-08", "התפתחות רפואת המשפחה בישראל"),
    SeedTopic("AI-09", "הוראה והכשרת רופאי משפחה"),
    SeedTopic("AI-10", "חילופי שבויים בהקשר של אסירי הפרשה"),
    SeedTopic("AI-11", "הנצחת אסירי הפרשה והרוגיה"),
)

BROADER_EDGES: tuple[BroaderEdge, ...] = (
    BroaderEdge("AI-02", "AI-01"),
    BroaderEdge("AI-03", "AI-01"),
    BroaderEdge("AI-07", "AI-06"),
    BroaderEdge("AI-08", "AI-06"),
    BroaderEdge("AI-09", "AI-06"),
    BroaderEdge("AI-10", "AI-05"),
)

RELATED_EDGES: tuple[RelatedEdge, ...] = (RelatedEdge("AI-05", "AI-11"),)

ASSOCIATIONS: tuple[SeedAssociation, ...] = (
    SeedAssociation(378, "AI-02", "CENTRAL", "01"),
    SeedAssociation(378, "AI-04", "CENTRAL", "01"),
    SeedAssociation(379, "AI-02", "CENTRAL", "01"),
    SeedAssociation(389, "AI-01", "CENTRAL", "01"),
    SeedAssociation(264, "AI-01", "CENTRAL", "01"),
    SeedAssociation(366, "AI-03", "SECONDARY", "01"),
    SeedAssociation(430, "AI-03", "CENTRAL", "01"),
    SeedAssociation(147, "AI-05", "CENTRAL", "01"),
    SeedAssociation(164, "AI-05", "CENTRAL", "01"),
    SeedAssociation(173, "AI-05", "CENTRAL", "01"),
    SeedAssociation(144, "AI-05", "CENTRAL", "01"),
    SeedAssociation(324, "AI-05", "SECONDARY", "01"),
    SeedAssociation(13, "AI-08", "CENTRAL", "02"),
    SeedAssociation(13, "AI-09", "CENTRAL", "02"),
    SeedAssociation(284, "AI-07", "CENTRAL", "02"),
    SeedAssociation(284, "AI-09", "SECONDARY", "02"),
    SeedAssociation(164, "AI-10", "CENTRAL", "03"),
    SeedAssociation(173, "AI-10", "CENTRAL", "03"),
    SeedAssociation(144, "AI-10", "SECONDARY", "03"),
    SeedAssociation(324, "AI-10", "CENTRAL", "03"),
    SeedAssociation(163, "AI-11", "CENTRAL", "03"),
    SeedAssociation(301, "AI-11", "CENTRAL", "03"),
)

_APPROVED_BROADER = frozenset(
    {
        ("AI-02", "AI-01"),
        ("AI-03", "AI-01"),
        ("AI-07", "AI-06"),
        ("AI-08", "AI-06"),
        ("AI-09", "AI-06"),
        ("AI-10", "AI-05"),
    }
)
_BATCH_COUNTS = {"01": 12, "02": 4, "03": 6}


def evidence_note_rejection(note: str) -> str | None:
    """Reject blank, provisional, and placeholder notes before any apply."""

    text = (note or "").strip()
    folded = text.casefold()
    if (
        not text
        or folded in _PLACEHOLDER_EVIDENCE_NOTES
        or any(marker in folded for marker in _PLACEHOLDER_EVIDENCE_MARKERS)
    ):
        return "Provisional or placeholder evidence notes are rejected."
    if text != APPROVED_EVIDENCE_NOTE:
        return "Evidence note is not the owner-approved text and is rejected."
    return None


def canonical_seed_fixture(
    topics: tuple[SeedTopic, ...],
    broader_edges: tuple[BroaderEdge, ...],
    related_edges: tuple[RelatedEdge, ...],
    associations: tuple[SeedAssociation, ...],
) -> str:
    """Stable text for one candidate fixture. The approved contract is separate."""

    lines = [f"topic\t{topic.stable_key}\t{topic.preferred_label}" for topic in topics]
    lines.extend(
        f"broader\t{edge.narrower_key}\t{edge.broader_key}" for edge in broader_edges
    )
    lines.extend(
        f"related\t{edge.left_key}\t{edge.right_key}" for edge in related_edges
    )
    lines.extend(
        (
            f"association\t{row.archive_item_id}\t{row.topic_key}\t"
            f"{row.relevance}\t{row.batch_id}"
        )
        for row in associations
    )
    return "\n".join(lines) + "\n"


def _fixture_contract_errors(
    topics: tuple[SeedTopic, ...],
    broader_edges: tuple[BroaderEdge, ...],
    related_edges: tuple[RelatedEdge, ...],
    associations: tuple[SeedAssociation, ...],
) -> tuple[str, ...]:
    canonical = canonical_seed_fixture(
        topics, broader_edges, related_edges, associations
    )
    errors: list[str] = []
    if canonical != APPROVED_FIXTURE_CANONICAL:
        errors.append(
            "Fixture rows do not match the approved specification v2 section 5 contract."
        )
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    if digest != APPROVED_FIXTURE_SHA256:
        errors.append(
            "Fixture SHA-256 does not match the approved specification contract."
        )
    return tuple(errors)


def topic_operation_key(stable_key: str) -> str:
    return f"{OPERATION_KEY_PREFIX}:topic:{stable_key}"


def broader_operation_key(narrower_key: str, broader_key: str) -> str:
    return f"{OPERATION_KEY_PREFIX}:broader:{narrower_key}:{broader_key}"


def related_operation_key(left_key: str, right_key: str) -> str:
    low, high = sorted((left_key, right_key))
    return f"{OPERATION_KEY_PREFIX}:related:{low}:{high}"


def association_operation_key(archive_item_id: int, topic_key: str) -> str:
    return f"{OPERATION_KEY_PREFIX}:item:{archive_item_id}:{topic_key}"


def all_operation_keys() -> tuple[str, ...]:
    keys = [topic_operation_key(topic.stable_key) for topic in TOPICS]
    keys.extend(
        broader_operation_key(edge.narrower_key, edge.broader_key)
        for edge in BROADER_EDGES
    )
    keys.extend(
        related_operation_key(edge.left_key, edge.right_key) for edge in RELATED_EDGES
    )
    keys.extend(
        association_operation_key(row.archive_item_id, row.topic_key)
        for row in ASSOCIATIONS
    )
    return tuple(keys)


def seed_definition_errors(
    topics: tuple[SeedTopic, ...] = TOPICS,
    broader_edges: tuple[BroaderEdge, ...] = BROADER_EDGES,
    related_edges: tuple[RelatedEdge, ...] = RELATED_EDGES,
    associations: tuple[SeedAssociation, ...] = ASSOCIATIONS,
    alternate_labels: tuple[tuple[str, str], ...] = ALTERNATE_LABELS,
) -> tuple[str, ...]:
    """Return contradictions in a seed definition. The shipped tuples return none."""

    errors: list[str] = list(
        _fixture_contract_errors(topics, broader_edges, related_edges, associations)
    )
    expected_keys = tuple(f"AI-{index:02d}" for index in range(1, 12))
    actual_keys = tuple(topic.stable_key for topic in topics)
    if actual_keys != expected_keys:
        errors.append("Topics must be exactly AI-01 through AI-11 in that order.")
    if len(topics) != EXPECTED_TOPIC_COUNT:
        errors.append("Topic count must be 11.")
    labels: dict[str, str] = {}
    for topic in topics:
        label = topic.preferred_label
        if not label or label != label.strip():
            errors.append(f"{topic.stable_key} preferred label is blank or padded.")
        labels[topic.stable_key] = label
    if len(broader_edges) != EXPECTED_BROADER_COUNT:
        errors.append("Broader edge count must be 6.")
    broader_pairs = tuple(
        (edge.narrower_key, edge.broader_key) for edge in broader_edges
    )
    if frozenset(broader_pairs) != _APPROVED_BROADER or len(set(broader_pairs)) != len(
        broader_pairs
    ):
        errors.append("Broader edges do not match the approved six pairs.")
    parents: dict[str, set[str]] = {}
    for narrower_key, broader_key in broader_pairs:
        if narrower_key not in labels or broader_key not in labels:
            errors.append(
                f"Broader edge {narrower_key}->{broader_key} names an unknown topic."
            )
            continue
        if narrower_key == broader_key:
            errors.append(f"{narrower_key} cannot be its own broader topic.")
        parents.setdefault(narrower_key, set()).add(broader_key)
    if any(len(parent_keys) > 1 for parent_keys in parents.values()):
        errors.append("This seed has no multi-parent topic.")
    if "AI-04" in parents:
        errors.append("AI-04 must not have a broader edge.")
    if _has_cycle(parents):
        errors.append("Broader edges contain a cycle.")
    if related_edges != (RelatedEdge("AI-05", "AI-11"),):
        errors.append("The only related edge is AI-05 with AI-11.")
    for edge in related_edges:
        if edge.left_key == edge.right_key:
            errors.append("A topic cannot be related to itself.")
        if edge.left_key in _ancestors(
            edge.right_key, parents
        ) or edge.right_key in _ancestors(edge.left_key, parents):
            errors.append("Related edge overlaps a broader/narrower path.")
    if len(associations) != EXPECTED_ASSOCIATION_COUNT:
        errors.append("Association count must be 22.")
    seen_pairs: set[tuple[int, str]] = set()
    batch_counts = {"01": 0, "02": 0, "03": 0}
    item_ids: set[int] = set()
    item_378: set[tuple[str, str]] = set()
    for row in associations:
        pair = (row.archive_item_id, row.topic_key)
        if pair in seen_pairs:
            errors.append(
                f"Duplicate association {row.archive_item_id} {row.topic_key}."
            )
        seen_pairs.add(pair)
        if row.topic_key not in labels:
            errors.append(f"Association topic {row.topic_key} is not in the seed.")
        if row.relevance not in {"CENTRAL", "SECONDARY"}:
            errors.append(f"Relevance {row.relevance!r} is not CENTRAL or SECONDARY.")
        if row.batch_id not in batch_counts:
            errors.append(f"Batch {row.batch_id!r} is not 01, 02, or 03.")
        else:
            batch_counts[row.batch_id] += 1
        if row.archive_item_id in EXCLUDED_ARCHIVE_ITEM_IDS:
            errors.append(
                f"Archive item {row.archive_item_id} is excluded from the seed."
            )
        item_ids.add(row.archive_item_id)
        if row.archive_item_id == 378:
            item_378.add((row.topic_key, row.relevance))
    if tuple(sorted(item_ids)) != PINNED_DISTINCT_ITEM_IDS:
        errors.append("Distinct archive item ids do not match the approved 15.")
    if batch_counts != _BATCH_COUNTS:
        errors.append("Batch sizes must be 12, 4, and 6.")
    if item_378 != {("AI-02", "CENTRAL"), ("AI-04", "CENTRAL")}:
        errors.append("Item 378 must link only to AI-02 and AI-04, both CENTRAL.")
    if alternate_labels:
        errors.append("The approved seed has no alternate labels.")
    if EXPECTED_ISSUER_ROW_COUNT != 0:
        errors.append("The approved seed has no issuer rows.")
    operation_keys: list[str] = []
    operation_keys.extend(topic_operation_key(topic.stable_key) for topic in topics)
    operation_keys.extend(
        broader_operation_key(edge.narrower_key, edge.broader_key)
        for edge in broader_edges
    )
    operation_keys.extend(
        related_operation_key(edge.left_key, edge.right_key) for edge in related_edges
    )
    operation_keys.extend(
        association_operation_key(row.archive_item_id, row.topic_key)
        for row in associations
    )
    if len(operation_keys) != len(set(operation_keys)):
        errors.append("Seed operation keys are not unique.")
    note_error = evidence_note_rejection(APPROVED_EVIDENCE_NOTE)
    if note_error:
        errors.append(note_error)
    return tuple(errors)


def _ancestors(topic_key: str, parents: dict[str, set[str]]) -> set[str]:
    found: set[str] = set()
    stack = list(parents.get(topic_key, ()))
    while stack:
        parent = stack.pop()
        if parent in found:
            continue
        found.add(parent)
        stack.extend(parents.get(parent, ()))
    return found


def _has_cycle(parents: dict[str, set[str]]) -> bool:
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(topic_key: str) -> bool:
        if topic_key in visiting:
            return True
        if topic_key in visited:
            return False
        visiting.add(topic_key)
        for parent in parents.get(topic_key, ()):
            if visit(parent):
                return True
        visiting.remove(topic_key)
        visited.add(topic_key)
        return False

    return any(visit(topic_key) for topic_key in list(parents))


if hashlib.sha256(APPROVED_FIXTURE_CANONICAL.encode("utf-8")).hexdigest() != (
    APPROVED_FIXTURE_SHA256
):
    raise RuntimeError(
        "Approved fixture SHA-256 does not match the canonical specification contract."
    )


_SHIPPED_ERRORS = seed_definition_errors()
if _SHIPPED_ERRORS:
    raise RuntimeError(
        "Academic index seed v2 is inconsistent: " + "; ".join(_SHIPPED_ERRORS)
    )
