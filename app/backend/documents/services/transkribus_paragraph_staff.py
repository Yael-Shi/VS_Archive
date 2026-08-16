"""Staff Transkribus paragraph editor and status helpers (PR3 + PR4).

Presentation metadata only: does not mutate transcription text, snapshot
lines, geometry, char offsets, hover IDs, or bindings. Ordinary saves
delegate to ``save_paragraph_mapping``. Historical adoption is an explicit
create-only write through ``adopt_historical_paragraph_mapping``.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from django.http import QueryDict
from django.utils.dateformat import format as format_datetime
from django.utils.timezone import localtime

from documents.models import (
    Document,
    TranskribusParagraphBreak,
    TranskribusParagraphMapping,
    TranskribusSnapshotLine,
    TranskribusTranscriptSnapshot,
)
from documents.services.text_presentation import source_text_is_rtl
from documents.services.transkribus_paragraph_adoption import (
    ParagraphMappingAdoptionError,
    ParagraphMappingAdoptionRefusal,
    adopt_historical_paragraph_mapping,
)
from documents.services.transkribus_paragraph_correspondence import (
    HistoricalParagraphMappingCandidate,
    discover_transferable_historical_mappings,
)
from documents.services.transkribus_paragraph_mapping import (
    ParagraphMappingCurrentness,
    TranskribusParagraphMappingError,
    assess_paragraph_mapping_currentness,
    contributing_lines_for_snapshot,
    save_paragraph_mapping,
)

STATUS_UNAVAILABLE = "אין תעתוק Transkribus מוצג כעת"
STATUS_STALE_BINDING = "התעתוק המוצג אינו תואם לגרסת Transkribus הנוכחית"
STATUS_NEVER_SAVED = "חלוקת פסקאות טרם נשמרה"
STATUS_ONE_PARAGRAPH = "נשמרה חלוקה: פסקה אחת"
STATUS_HISTORICAL_NOTE = "קיימת חלוקת פסקאות שמורה לתעתוק ישן יותר."

MSG_SAVED = "חלוקת הפסקאות נשמרה."
MSG_ADOPTED = "חלוקת הפסקאות מגרסה קודמת אומצה."
MSG_STALE_SUBMIT = "התעתוק המוצג השתנה מאז שנפתח העורך. רעננו את הדף ונסו שוב."
MSG_STALE_BINDING = (
    "לא ניתן לשמור חלוקת פסקאות כי התעתוק המוצג אינו תואם עוד לגרסת Transkribus."
)
MSG_UNAVAILABLE = "אין תעתוק Transkribus מוצג שאפשר לחלק לפסקאות."
MSG_MALFORMED = "אחת מגבולות הפסקה שנשלחו אינה תקינה."
MSG_DUPLICATE = "אותו גבול פסקה נשלח יותר מפעם אחת."
MSG_OTHER_SNAPSHOT = "לא ניתן לשמור: אחת השורות אינה שייכת לתעתוק המוצג כעת."
MSG_NON_CONTRIBUTING = "לא ניתן לשמור גבול אחרי שורה שאינה חלק מהתעתוק."
MSG_FINAL_LINE = "אין משמעות לגבול פסקה אחרי השורה האחרונה."
MSG_GENERIC = "לא ניתן לשמור את חלוקת הפסקאות. רעננו את הדף ונסו שוב."
MSG_ADOPT_ALREADY_EXISTS = (
    "לחלוקת הפסקאות הנוכחית כבר נשמרה חלוקה. לא בוצע אימוץ מגרסה קודמת."
)
MSG_ADOPT_UNAVAILABLE = "לא ניתן לאמץ את החלוקה מגרסה קודמת. רעננו את הדף ונסו שוב."

ADOPTION_INTRO_SINGLE = (
    "נמצאה חלוקת פסקאות מגרסת Transkribus קודמת. זו הצעה בלבד. "
    "אימוץ יוצר את החלוקה לגרסה הנוכחית; אפשר גם להתעלם ולהגדיר חלוקה ידנית."
)
ADOPTION_INTRO_MULTIPLE = (
    "נמצאו חלוקות פסקאות מגרסאות Transkribus קודמות. זו הצעה בלבד. "
    "בחרו איזו חלוקה לאמץ לגרסה הנוכחית, או הגדירו חלוקה ידנית."
)
ADOPTION_ACTION_LABEL = "אימוץ חלוקה זו לגרסה הנוכחית"

EDITOR_BREAK_FIELD = "break_after"
EDITOR_EXPECTED_DOCUMENT_FIELD = "expected_document_id"
EDITOR_EXPECTED_RESULT_FIELD = "expected_text_result_id"
EDITOR_EXPECTED_SNAPSHOT_FIELD = "expected_snapshot_id"
ADOPT_EXPECTED_SOURCE_MAPPING_FIELD = "expected_source_mapping_id"
ADOPT_EXPECTED_SOURCE_SNAPSHOT_FIELD = "expected_source_snapshot_id"


class ParagraphEditorError(ValueError):
    """Staff-facing paragraph editor validation or freshness failure."""

    def __init__(self, staff_message: str) -> None:
        super().__init__(staff_message)
        self.staff_message = staff_message


@dataclass(frozen=True)
class ParagraphMappingStaffStatus:
    """Human status for the currently displayed/bound Transkribus snapshot."""

    code: str
    label: str
    paragraph_count: int | None
    break_count: int | None
    editor_available: bool
    has_historical_mapping: bool
    historical_note: str | None
    bound_snapshot_id: int | None
    displayed_text_result_id: int | None
    is_structurally_fresh: bool


@dataclass(frozen=True)
class ParagraphEditorFreshness:
    """Immutable GET/POST token for the displayed result and bound snapshot."""

    document_id: int
    text_result_id: int
    snapshot_id: int


@dataclass(frozen=True)
class ParagraphEditorLine:
    """One contributing source line in editor order, plus optional break control."""

    line_id: int
    text: str
    page_index: int
    page_display_number: int
    is_page_start: bool
    can_break_after: bool
    break_after_selected: bool
    hover_line_id: str | None


@dataclass(frozen=True)
class HistoricalParagraphAdoptionSuggestion:
    """Staff-facing historical mapping choice. IDs are POST tokens only."""

    mapping_id: int
    source_snapshot_id: int
    created_at: datetime
    break_count: int
    paragraph_count: int
    version_label: str
    paragraph_label: str
    label: str


@dataclass(frozen=True)
class ParagraphEditorContext:
    """Bounded editor payload for the current displayed Transkribus snapshot."""

    available: bool
    unavailable_message: str | None
    snapshot: TranskribusTranscriptSnapshot | None
    freshness: ParagraphEditorFreshness | None
    lines: tuple[ParagraphEditorLine, ...]
    status: ParagraphMappingStaffStatus
    source_is_rtl: bool
    adoption_suggestions: tuple[HistoricalParagraphAdoptionSuggestion, ...] = ()
    adoption_intro: str | None = None


def status_n_paragraphs(count: int) -> str:
    return f"נשמרה חלוקה: {count} פסקאות"


def paragraph_count_phrase(paragraph_count: int) -> str:
    if paragraph_count == 1:
        return "פסקה אחת"
    return f"{paragraph_count} פסקאות"


def _format_version_datetimes(created_ats: tuple[datetime, ...]) -> tuple[str, ...]:
    minute_labels = tuple(
        format_datetime(localtime(created_at), "d.m.Y H:i")
        for created_at in created_ats
    )
    if len(set(minute_labels)) == len(minute_labels):
        return minute_labels
    return tuple(
        format_datetime(localtime(created_at), "d.m.Y H:i:s")
        for created_at in created_ats
    )


def _suggestions_from_candidates(
    candidates: tuple[HistoricalParagraphMappingCandidate, ...],
) -> tuple[HistoricalParagraphAdoptionSuggestion, ...]:
    version_labels = _format_version_datetimes(
        tuple(candidate.source_snapshot_created_at for candidate in candidates)
    )
    suggestions: list[HistoricalParagraphAdoptionSuggestion] = []
    for candidate, version_label in zip(candidates, version_labels, strict=True):
        break_count = len(candidate.break_after_source_line_ids)
        paragraph_count = break_count + 1
        paragraph_label = paragraph_count_phrase(paragraph_count)
        suggestions.append(
            HistoricalParagraphAdoptionSuggestion(
                mapping_id=candidate.mapping_id,
                source_snapshot_id=candidate.source_snapshot_id,
                created_at=candidate.source_snapshot_created_at,
                break_count=break_count,
                paragraph_count=paragraph_count,
                version_label=version_label,
                paragraph_label=paragraph_label,
                label=(
                    f"חלוקה מגרסת Transkribus מ־{version_label} · {paragraph_label}"
                ),
            )
        )
    return tuple(suggestions)


def build_historical_paragraph_adoption_suggestions(
    snapshot: TranskribusTranscriptSnapshot,
) -> tuple[HistoricalParagraphAdoptionSuggestion, ...]:
    """Eligible historical mappings for the editor, newest-first, none selected."""
    return _suggestions_from_candidates(
        discover_transferable_historical_mappings(snapshot)
    )


def _hover_line_id_for_source_line(line: TranskribusSnapshotLine) -> str:
    return f"p{line.page.page_index}-o{line.order_index}"


def _break_count_for_mapping(
    mapping: TranskribusParagraphMapping | None,
) -> int | None:
    if mapping is None:
        return None
    return TranskribusParagraphBreak.objects.filter(mapping_id=mapping.pk).count()


def _has_historical_mapping(
    document: Document,
    *,
    bound_snapshot_id: int | None,
) -> bool:
    qs = TranskribusParagraphMapping.objects.filter(document_id=document.pk)
    if bound_snapshot_id is not None:
        qs = qs.exclude(snapshot_id=bound_snapshot_id)
    return qs.exists()


def build_paragraph_mapping_staff_status(
    document: Document,
    *,
    assessment: ParagraphMappingCurrentness | None = None,
) -> ParagraphMappingStaffStatus:
    """Status for the currently displayed/bound snapshot, not historical ones."""
    resolved = assessment or assess_paragraph_mapping_currentness(document)
    bound_snapshot_id = resolved.bound_snapshot_id
    editor_available = bool(
        bound_snapshot_id is not None and resolved.is_structurally_fresh
    )
    break_count = _break_count_for_mapping(resolved.mapping)
    has_historical = False
    historical_note: str | None = None

    if bound_snapshot_id is None:
        return ParagraphMappingStaffStatus(
            code="UNAVAILABLE",
            label=STATUS_UNAVAILABLE,
            paragraph_count=None,
            break_count=None,
            editor_available=False,
            has_historical_mapping=_has_historical_mapping(
                document, bound_snapshot_id=None
            ),
            historical_note=None,
            bound_snapshot_id=None,
            displayed_text_result_id=resolved.displayed_text_result_id,
            is_structurally_fresh=resolved.is_structurally_fresh,
        )

    if not resolved.is_structurally_fresh:
        return ParagraphMappingStaffStatus(
            code="STALE_BINDING",
            label=STATUS_STALE_BINDING,
            paragraph_count=None,
            break_count=break_count,
            editor_available=False,
            has_historical_mapping=False,
            historical_note=None,
            bound_snapshot_id=bound_snapshot_id,
            displayed_text_result_id=resolved.displayed_text_result_id,
            is_structurally_fresh=False,
        )

    if not resolved.has_mapping:
        has_historical = _has_historical_mapping(
            document, bound_snapshot_id=bound_snapshot_id
        )
        if has_historical:
            historical_note = STATUS_HISTORICAL_NOTE
        return ParagraphMappingStaffStatus(
            code="NEVER_SAVED",
            label=STATUS_NEVER_SAVED,
            paragraph_count=None,
            break_count=None,
            editor_available=editor_available,
            has_historical_mapping=has_historical,
            historical_note=historical_note,
            bound_snapshot_id=bound_snapshot_id,
            displayed_text_result_id=resolved.displayed_text_result_id,
            is_structurally_fresh=True,
        )

    assert break_count is not None
    if break_count == 0:
        return ParagraphMappingStaffStatus(
            code="ONE_PARAGRAPH",
            label=STATUS_ONE_PARAGRAPH,
            paragraph_count=1,
            break_count=0,
            editor_available=editor_available,
            has_historical_mapping=False,
            historical_note=None,
            bound_snapshot_id=bound_snapshot_id,
            displayed_text_result_id=resolved.displayed_text_result_id,
            is_structurally_fresh=True,
        )

    paragraph_count = break_count + 1
    return ParagraphMappingStaffStatus(
        code="N_PARAGRAPHS",
        label=status_n_paragraphs(paragraph_count),
        paragraph_count=paragraph_count,
        break_count=break_count,
        editor_available=editor_available,
        has_historical_mapping=False,
        historical_note=None,
        bound_snapshot_id=bound_snapshot_id,
        displayed_text_result_id=resolved.displayed_text_result_id,
        is_structurally_fresh=True,
    )


def _selected_break_ids(
    mapping: TranskribusParagraphMapping | None,
) -> set[int]:
    if mapping is None:
        return set()
    return set(
        TranskribusParagraphBreak.objects.filter(mapping_id=mapping.pk).values_list(
            "after_line_id", flat=True
        )
    )


def build_paragraph_editor_context(
    document: Document,
    *,
    hover_line_ids: set[str] | None = None,
) -> ParagraphEditorContext:
    """Load the current bound snapshot and contributing lines with bounded queries."""
    assessment = assess_paragraph_mapping_currentness(document)
    status = build_paragraph_mapping_staff_status(document, assessment=assessment)
    rtl = source_text_is_rtl(document)

    if assessment.bound_snapshot_id is None:
        return ParagraphEditorContext(
            available=False,
            unavailable_message=MSG_UNAVAILABLE,
            snapshot=None,
            freshness=None,
            lines=(),
            status=status,
            source_is_rtl=rtl,
            adoption_suggestions=(),
            adoption_intro=None,
        )

    if not assessment.is_structurally_fresh:
        return ParagraphEditorContext(
            available=False,
            unavailable_message=MSG_STALE_BINDING,
            snapshot=None,
            freshness=None,
            lines=(),
            status=status,
            source_is_rtl=rtl,
            adoption_suggestions=(),
            adoption_intro=None,
        )

    snapshot = TranskribusTranscriptSnapshot.objects.filter(
        pk=assessment.bound_snapshot_id
    ).first()
    if snapshot is None or assessment.displayed_text_result_id is None:
        return ParagraphEditorContext(
            available=False,
            unavailable_message=MSG_UNAVAILABLE,
            snapshot=None,
            freshness=None,
            lines=(),
            status=status,
            source_is_rtl=rtl,
            adoption_suggestions=(),
            adoption_intro=None,
        )

    contributing = contributing_lines_for_snapshot(snapshot)
    selected = _selected_break_ids(assessment.mapping)
    hover_ids = hover_line_ids or set()
    lines: list[ParagraphEditorLine] = []
    previous_page_index: int | None = None
    last_index = len(contributing) - 1
    for index, line in enumerate(contributing):
        page_index = int(line.page.page_index)
        is_page_start = (
            previous_page_index is not None and page_index != previous_page_index
        )
        previous_page_index = page_index
        candidate_hover = _hover_line_id_for_source_line(line)
        lines.append(
            ParagraphEditorLine(
                line_id=int(line.pk),
                text=line.text or "",
                page_index=page_index,
                page_display_number=page_index,
                is_page_start=is_page_start,
                can_break_after=index != last_index,
                break_after_selected=int(line.pk) in selected,
                hover_line_id=candidate_hover if candidate_hover in hover_ids else None,
            )
        )

    freshness = ParagraphEditorFreshness(
        document_id=int(document.pk),
        text_result_id=int(assessment.displayed_text_result_id),
        snapshot_id=int(snapshot.pk),
    )
    adoption_suggestions: tuple[HistoricalParagraphAdoptionSuggestion, ...] = ()
    adoption_intro: str | None = None
    if not assessment.has_mapping:
        adoption_suggestions = build_historical_paragraph_adoption_suggestions(snapshot)
        if len(adoption_suggestions) == 1:
            adoption_intro = ADOPTION_INTRO_SINGLE
        elif len(adoption_suggestions) > 1:
            adoption_intro = ADOPTION_INTRO_MULTIPLE
    return ParagraphEditorContext(
        available=True,
        unavailable_message=None,
        snapshot=snapshot,
        freshness=freshness,
        lines=tuple(lines),
        status=status,
        source_is_rtl=rtl,
        adoption_suggestions=adoption_suggestions,
        adoption_intro=adoption_intro,
    )


def parse_break_after_line_ids(post: QueryDict) -> list[int]:
    """Parse submitted break-after identities. Empty list is a valid zero-break save."""
    raw_values = post.getlist(EDITOR_BREAK_FIELD)
    parsed: list[int] = []
    seen: set[int] = set()
    for raw in raw_values:
        value = str(raw).strip()
        if not value:
            raise ParagraphEditorError(MSG_MALFORMED)
        try:
            line_id = int(value)
        except ValueError as exc:
            raise ParagraphEditorError(MSG_MALFORMED) from exc
        if line_id in seen:
            raise ParagraphEditorError(MSG_DUPLICATE)
        seen.add(line_id)
        parsed.append(line_id)
    return parsed


def parse_editor_freshness(post: QueryDict) -> ParagraphEditorFreshness:
    try:
        document_id = int(str(post.get(EDITOR_EXPECTED_DOCUMENT_FIELD) or "").strip())
        text_result_id = int(str(post.get(EDITOR_EXPECTED_RESULT_FIELD) or "").strip())
        snapshot_id = int(str(post.get(EDITOR_EXPECTED_SNAPSHOT_FIELD) or "").strip())
    except (TypeError, ValueError) as exc:
        raise ParagraphEditorError(MSG_STALE_SUBMIT) from exc
    return ParagraphEditorFreshness(
        document_id=document_id,
        text_result_id=text_result_id,
        snapshot_id=snapshot_id,
    )


def editor_freshness_matches(
    document: Document,
    expected: ParagraphEditorFreshness,
    *,
    assessment: ParagraphMappingCurrentness | None = None,
) -> bool:
    resolved = assessment or assess_paragraph_mapping_currentness(document)
    return bool(
        expected.document_id == int(document.pk)
        and resolved.displayed_text_result_id == expected.text_result_id
        and resolved.bound_snapshot_id == expected.snapshot_id
        and resolved.is_structurally_fresh
        and resolved.bound_snapshot_id is not None
        and resolved.displayed_text_result_id is not None
    )


def staff_message_for_mapping_error(exc: TranskribusParagraphMappingError) -> str:
    text = str(exc)
    if "Duplicate paragraph break" in text:
        return MSG_DUPLICATE
    if "final contributing source line" in text:
        return MSG_FINAL_LINE
    if "must belong to the mapping's snapshot" in text:
        return MSG_OTHER_SNAPSHOT
    if "Unknown snapshot line" in text:
        return MSG_OTHER_SNAPSHOT
    if "contributing source line" in text:
        return MSG_NON_CONTRIBUTING
    return MSG_GENERIC


def save_paragraph_editor_mapping(
    document: Document,
    post: QueryDict,
    *,
    actor,
) -> TranskribusParagraphMapping:
    """Validate freshness and submitted breaks, then save via PR1 service."""
    assessment = assess_paragraph_mapping_currentness(document)
    if (
        assessment.bound_snapshot_id is None
        or assessment.displayed_text_result_id is None
    ):
        raise ParagraphEditorError(MSG_UNAVAILABLE)
    if not assessment.is_structurally_fresh:
        raise ParagraphEditorError(MSG_STALE_BINDING)

    expected = parse_editor_freshness(post)
    if not editor_freshness_matches(document, expected, assessment=assessment):
        raise ParagraphEditorError(MSG_STALE_SUBMIT)

    snapshot = TranskribusTranscriptSnapshot.objects.filter(
        pk=assessment.bound_snapshot_id
    ).first()
    if snapshot is None:
        raise ParagraphEditorError(MSG_UNAVAILABLE)

    break_ids = parse_break_after_line_ids(post)
    try:
        return save_paragraph_mapping(snapshot, break_ids, actor=actor)
    except TranskribusParagraphMappingError as exc:
        raise ParagraphEditorError(staff_message_for_mapping_error(exc)) from exc


def parse_adoption_source_ids(post: QueryDict) -> tuple[int, int]:
    try:
        mapping_id = int(
            str(post.get(ADOPT_EXPECTED_SOURCE_MAPPING_FIELD) or "").strip()
        )
        snapshot_id = int(
            str(post.get(ADOPT_EXPECTED_SOURCE_SNAPSHOT_FIELD) or "").strip()
        )
    except (TypeError, ValueError) as exc:
        raise ParagraphEditorError(MSG_ADOPT_UNAVAILABLE) from exc
    return mapping_id, snapshot_id


def _staff_message_for_adoption_error(exc: ParagraphMappingAdoptionError) -> str:
    if exc.code == ParagraphMappingAdoptionRefusal.TARGET_MAPPING_EXISTS:
        return MSG_ADOPT_ALREADY_EXISTS
    return MSG_ADOPT_UNAVAILABLE


def adopt_paragraph_editor_mapping(
    document: Document,
    post: QueryDict,
    *,
    actor,
) -> TranskribusParagraphMapping:
    """Revalidate editor freshness and source choice, then adopt create-only."""
    assessment = assess_paragraph_mapping_currentness(document)
    if (
        assessment.bound_snapshot_id is None
        or assessment.displayed_text_result_id is None
    ):
        raise ParagraphEditorError(MSG_UNAVAILABLE)
    if not assessment.is_structurally_fresh:
        raise ParagraphEditorError(MSG_STALE_BINDING)

    expected = parse_editor_freshness(post)
    if not editor_freshness_matches(document, expected, assessment=assessment):
        raise ParagraphEditorError(MSG_STALE_SUBMIT)

    snapshot = TranskribusTranscriptSnapshot.objects.filter(
        pk=assessment.bound_snapshot_id
    ).first()
    if snapshot is None:
        raise ParagraphEditorError(MSG_UNAVAILABLE)

    source_mapping_id, source_snapshot_id = parse_adoption_source_ids(post)
    source_mapping = (
        TranskribusParagraphMapping.objects.filter(
            pk=source_mapping_id,
            document_id=document.pk,
            snapshot_id=source_snapshot_id,
        )
        .select_related("snapshot")
        .first()
    )
    if source_mapping is None:
        raise ParagraphEditorError(MSG_ADOPT_UNAVAILABLE)

    try:
        return adopt_historical_paragraph_mapping(
            source_mapping,
            snapshot,
            actor=actor,
        )
    except ParagraphMappingAdoptionError as exc:
        raise ParagraphEditorError(_staff_message_for_adoption_error(exc)) from exc


__all__ = [
    "ADOPTION_ACTION_LABEL",
    "ADOPTION_INTRO_MULTIPLE",
    "ADOPTION_INTRO_SINGLE",
    "ADOPT_EXPECTED_SOURCE_MAPPING_FIELD",
    "ADOPT_EXPECTED_SOURCE_SNAPSHOT_FIELD",
    "EDITOR_BREAK_FIELD",
    "EDITOR_EXPECTED_DOCUMENT_FIELD",
    "EDITOR_EXPECTED_RESULT_FIELD",
    "EDITOR_EXPECTED_SNAPSHOT_FIELD",
    "MSG_ADOPTED",
    "MSG_ADOPT_ALREADY_EXISTS",
    "MSG_ADOPT_UNAVAILABLE",
    "MSG_GENERIC",
    "MSG_MALFORMED",
    "MSG_SAVED",
    "MSG_STALE_BINDING",
    "MSG_STALE_SUBMIT",
    "MSG_UNAVAILABLE",
    "HistoricalParagraphAdoptionSuggestion",
    "ParagraphEditorContext",
    "ParagraphEditorError",
    "ParagraphEditorFreshness",
    "ParagraphEditorLine",
    "ParagraphMappingStaffStatus",
    "STATUS_HISTORICAL_NOTE",
    "STATUS_NEVER_SAVED",
    "STATUS_ONE_PARAGRAPH",
    "STATUS_STALE_BINDING",
    "STATUS_UNAVAILABLE",
    "adopt_paragraph_editor_mapping",
    "build_historical_paragraph_adoption_suggestions",
    "build_paragraph_editor_context",
    "build_paragraph_mapping_staff_status",
    "editor_freshness_matches",
    "parse_adoption_source_ids",
    "parse_break_after_line_ids",
    "parse_editor_freshness",
    "paragraph_count_phrase",
    "save_paragraph_editor_mapping",
    "staff_message_for_mapping_error",
    "status_n_paragraphs",
]
