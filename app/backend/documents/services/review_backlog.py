from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Iterable, List, Sequence

from django.db.models import Exists, OuterRef, Prefetch, Q, QuerySet
from django.db.models.query import prefetch_related_objects

from documents.models import Document, DocumentTextResult

# Matches ``is_review_pending_text_result`` (non-empty after strip).
_REVIEWABLE_TEXT_Q = (
    ~Q(text__isnull=True)
    & ~Q(text__exact="")
    & ~Q(text__regex=r"^\s*$")
)


def review_pending_text_result_filter() -> Q:
    """Rows that belong in the בקרת תמלול backlog."""
    return Q(
        status=DocumentTextResult.Status.NEEDS_REVIEW,
        verification_status__in=(
            DocumentTextResult.VerificationStatus.UNVERIFIED,
            DocumentTextResult.VerificationStatus.REJECTED,
        ),
    ) & _REVIEWABLE_TEXT_Q


def documents_in_review_backlog(
    *,
    q: str = "",
    language: str = "",
    text_input_type: str = "",
    processing_state_user: str = "",
    engine_key: str = "",
    result_type: str = "",
    verification_status: str = "",
) -> QuerySet[Document]:
    pending = review_pending_text_result_filter()
    qs = (
        Document.objects.filter(
            Exists(
                DocumentTextResult.objects.filter(
                    document=OuterRef("pk"),
                ).filter(pending)
            )
        )
        .order_by("-updated_at")
        .distinct()
    )

    q = (q or "").strip()
    if q:
        if q.isdigit():
            qs = qs.filter(Q(id=int(q)) | Q(title__icontains=q))
        else:
            qs = qs.filter(
                Q(title__icontains=q) | Q(file_original_name__icontains=q)
            )

    if language:
        qs = qs.filter(language=language)
    if text_input_type:
        qs = qs.filter(text_input_type=text_input_type)
    if processing_state_user:
        qs = qs.filter(processing_state_user=processing_state_user)

    if engine_key:
        qs = qs.filter(
            Exists(
                DocumentTextResult.objects.filter(
                    document=OuterRef("pk"),
                )
                .filter(pending)
                .filter(engine_key=engine_key)
            )
        )
    if result_type:
        qs = qs.filter(
            Exists(
                DocumentTextResult.objects.filter(
                    document=OuterRef("pk"),
                )
                .filter(pending)
                .filter(result_type=result_type)
            )
        )
    if verification_status:
        qs = qs.filter(
            Exists(
                DocumentTextResult.objects.filter(
                    document=OuterRef("pk"),
                )
                .filter(pending)
                .filter(verification_status=verification_status)
            )
        )

    return qs


def parse_review_reasons(raw: str) -> List[str]:
    s = (raw or "").strip()
    if not s:
        return []
    try:
        parsed = json.loads(s)
    except json.JSONDecodeError:
        return [s]
    if isinstance(parsed, list):
        return [str(x) for x in parsed if x]
    return [str(parsed)]


@dataclass(frozen=True)
class ReviewBacklogRowSummary:
    pending_count: int
    result_types: List[str]
    engine_keys: List[str]
    verification_statuses: List[str]
    review_reasons: List[str]


def _unique_sorted(values: Iterable[str]) -> List[str]:
    return sorted({v for v in values if v})


def is_review_pending_text_result(row: DocumentTextResult) -> bool:
    if row.status != DocumentTextResult.Status.NEEDS_REVIEW:
        return False
    if row.verification_status not in (
        DocumentTextResult.VerificationStatus.UNVERIFIED,
        DocumentTextResult.VerificationStatus.REJECTED,
    ):
        return False
    return bool((row.text or "").strip())


def is_review_editable_text_result(row: DocumentTextResult) -> bool:
    """
    Whether staff may overwrite ``DocumentTextResult.text`` from בקרת תמלול.

    Currently matches ``is_review_pending_text_result`` (NEEDS_REVIEW, UNVERIFIED
    or REJECTED, non-empty text). A future explicit reopen/edit workflow may
    diverge (e.g. allow editing after verification with a separate action).
    """
    return is_review_pending_text_result(row)


def summarize_pending_text_results(
    text_results: Sequence[DocumentTextResult],
) -> ReviewBacklogRowSummary:
    pending = [r for r in text_results if is_review_pending_text_result(r)]

    reason_set: set[str] = set()
    for row in pending:
        reason_set.update(parse_review_reasons(row.review_reasons))

    return ReviewBacklogRowSummary(
        pending_count=len(pending),
        result_types=_unique_sorted(r.result_type for r in pending),
        engine_keys=_unique_sorted(r.engine_key for r in pending),
        verification_statuses=_unique_sorted(r.verification_status for r in pending),
        review_reasons=sorted(reason_set),
    )


def attach_review_summaries(
    docs: Sequence[Document],
) -> List[tuple[Document, ReviewBacklogRowSummary]]:
    """
    Build (document, pending summary) pairs for the review backlog table.

    Callers should pass documents from a queryset that already used
    ``prefetch_related("text_results")`` (see ``review_backlog_page``).
    If ``text_results`` were not prefetched, this loads them in one batched
    query for the page via ``prefetch_related_objects`` (no per-row N+1).
    """
    if docs:
        prefetch_related_objects(
            docs,
            Prefetch(
                "text_results",
                queryset=DocumentTextResult.objects.all(),
            ),
        )
    out: List[tuple[Document, ReviewBacklogRowSummary]] = []
    for doc in docs:
        rows = list(doc.text_results.all())
        out.append((doc, summarize_pending_text_results(rows)))
    return out
