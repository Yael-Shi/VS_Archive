"""Read-only grouped review of pending non-person occurrence candidates."""

from __future__ import annotations

from unittest.mock import patch

from django.contrib.auth.models import Group, User
from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from documents.models import (
    ArchiveItem,
    ArchiveItemEntityOccurrence,
    ArchiveItemSearchIndex,
    Document,
    DocumentTextResult,
    ManualTextContent,
    NonPersonEntity,
    NonPersonEntityAlias,
    NonPersonEntityOccurrenceCandidate,
    NonPersonEntityOccurrenceCandidateMatch,
    NonPersonEntityOccurrenceProposal,
    NonPersonEntityOccurrenceReviewEvent,
)
from documents.services.archive_item_access import ARCHIVE_FAMILY_GROUP_NAME
from documents.services.archive_items import create_manual_text_archive_item
from documents.services.non_person_entity_occurrence_grouped_review import (
    CONTEXT_UNAVAILABLE,
    GROUP_DETAIL_PAGE_SIZE,
    MANY_ARCHIVE_ITEMS,
    MATCHED_TEXT_UNAVAILABLE,
    MULTIPLE_ALIAS_KINDS,
    MULTIPLE_MATCH_METHODS,
    OCR_VARIANT,
    PRIOR_REVIEW_HISTORY,
    REPRESENTATIVE_EXAMPLE_LIMIT,
    SHORT_SURFACE,
    SOURCE_STALE,
    OccurrenceGroupKey,
    occurrence_group_id,
    staff_grouped_occurrence_review_page,
    staff_occurrence_review_group_detail,
)
from documents.services.non_person_entity_occurrence_review import (
    _CONTEXT_RADIUS,
    staff_occurrence_review_rows,
)
from documents.services.non_person_entity_occurrences import (
    authoritative_text_context_for_item,
    locate_surface_occurrences,
    source_text_sha256,
)

MANUAL = ArchiveItemEntityOccurrence.TextKind.MANUAL_TEXT
OCR = ArchiveItemEntityOccurrence.TextKind.OCR_TRANSCRIPTION
PENDING = NonPersonEntityOccurrenceCandidate.Status.PENDING
NEEDS_RESEARCH = NonPersonEntityOccurrenceCandidate.Status.NEEDS_RESEARCH
CANONICAL = NonPersonEntityOccurrenceCandidateMatch.Method.CANONICAL_NAME
ALIAS = NonPersonEntityOccurrenceCandidateMatch.Method.ALIAS
DISPLAY = NonPersonEntityOccurrenceCandidateMatch.Method.DISPLAY_NAME
DETECT = NonPersonEntityOccurrenceReviewEvent.Action.DETECT
REJECT = NonPersonEntityOccurrenceReviewEvent.Action.REJECT
GROUPS = "archive-manage-entity-occurrence-groups"
GROUP = "archive-manage-entity-occurrence-group"
QUEUE = "archive-manage-entity-occurrence-proposals"
BODY = "ביקור בקהיר אחר הצהריים"
SURFACE = "קהיר"


def _entity(name: str, **overrides) -> NonPersonEntity:
    fields = {
        "canonical_name": name,
        "entity_type": NonPersonEntity.EntityType.PLACE,
        "entity_subtype": NonPersonEntity.EntitySubtype.CITY,
    }
    fields.update(overrides)
    return NonPersonEntity.objects.create(**fields)


def _item(title: str, body: str = BODY) -> ArchiveItem:
    return create_manual_text_archive_item(
        title=title,
        body=body,
        visibility=ArchiveItem.Visibility.PUBLIC,
    )


def _ocr_item(title: str, body: str) -> ArchiveItem:
    item = ArchiveItem.objects.create(
        title=title,
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
        status=DocumentTextResult.Status.NEEDS_REVIEW,
        engine_key=DocumentTextResult.OcrEngineKey.GEMINI,
        engine="gemini-test",
        prompt_variant=DocumentTextResult.OcrPromptVariant.PRINTED,
        text=body,
    )
    return item


def _proposal(
    item: ArchiveItem,
    body: str,
    *,
    text_kind: str = MANUAL,
    ordinal: int = 1,
    surface: str = SURFACE,
    matched_text: str = SURFACE,
    normalization_version: str = "surface-v1",
    sha: str | None = None,
) -> NonPersonEntityOccurrenceProposal:
    return NonPersonEntityOccurrenceProposal.objects.create(
        archive_item=item,
        text_kind=text_kind,
        source_text_sha256=source_text_sha256(body) if sha is None else sha,
        normalization_version=normalization_version,
        normalized_surface=surface,
        occurrence_ordinal=ordinal,
        matched_text=matched_text,
    )


def _candidate(
    proposal: NonPersonEntityOccurrenceProposal,
    entity: NonPersonEntity,
    *,
    matches: tuple[tuple[str, str], ...] = ((CANONICAL, ""),),
    status: str = PENDING,
) -> NonPersonEntityOccurrenceCandidate:
    candidate = NonPersonEntityOccurrenceCandidate.objects.create(
        proposal=proposal,
        candidate_entity=entity,
        status=status,
    )
    for method, alias_kind in matches:
        NonPersonEntityOccurrenceCandidateMatch.objects.create(
            candidate=candidate,
            method=method,
            matched_value=f"{method}:{alias_kind or entity.canonical_name}",
            alias_kind=alias_kind,
        )
    return candidate


def _groups(**filters):
    return list(staff_grouped_occurrence_review_page(**filters).groups)


def _domain_counts() -> dict[str, int]:
    return {
        "proposals": NonPersonEntityOccurrenceProposal.objects.count(),
        "candidates": NonPersonEntityOccurrenceCandidate.objects.count(),
        "matches": NonPersonEntityOccurrenceCandidateMatch.objects.count(),
        "events": NonPersonEntityOccurrenceReviewEvent.objects.count(),
        "occurrences": ArchiveItemEntityOccurrence.objects.count(),
        "aliases": NonPersonEntityAlias.objects.count(),
        "entities": NonPersonEntity.objects.count(),
        "search": ArchiveItemSearchIndex.objects.count(),
    }


class GroupedOccurrenceReviewTests(TestCase):
    def test_same_entity_surface_provenance_and_text_kind_is_one_group(self):
        entity = _entity("קהיר")
        first = _candidate(_proposal(_item("פריט א"), BODY), entity)
        second = _candidate(_proposal(_item("פריט ב"), BODY), entity)

        groups = _groups()

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group.candidate_ids, tuple(sorted((first.pk, second.pk))))
        self.assertEqual(group.candidate_entity_id, entity.pk)
        self.assertEqual(group.normalized_surface, SURFACE)
        self.assertEqual(group.text_kind, MANUAL)
        self.assertEqual(group.match_methods, (CANONICAL,))
        self.assertEqual(len(group.group_id), 64)

    def test_same_surface_with_different_entities_stays_separate(self):
        cairo = _entity("קהיר")
        other = _entity("עיר אחרת")
        _candidate(_proposal(_item("פריט א"), BODY), cairo)
        _candidate(_proposal(_item("פריט ב"), BODY), other)

        groups = _groups()

        self.assertEqual(
            {group.candidate_entity_id for group in groups},
            {cairo.pk, other.pk},
        )
        self.assertEqual(len({group.group_id for group in groups}), 2)

    def test_manual_text_and_ocr_are_separate_groups(self):
        entity = _entity("קהיר")
        _candidate(_proposal(_item("ידני"), BODY), entity)
        _candidate(
            _proposal(_ocr_item("תעתיק", BODY), BODY, text_kind=OCR),
            entity,
        )

        groups = _groups()

        self.assertEqual({group.text_kind for group in groups}, {MANUAL, OCR})
        self.assertEqual(len(groups), 2)

    def test_different_match_provenance_is_a_separate_group(self):
        entity = _entity("קהיר")
        canonical = _candidate(_proposal(_item("קנוני"), BODY), entity)
        alias = _candidate(
            _proposal(_item("חלופי"), BODY),
            entity,
            matches=((ALIAS, NonPersonEntityAlias.Kind.SPELLING_VARIANT),),
        )

        groups = _groups()
        by_id = {group.candidate_ids: group for group in groups}

        self.assertEqual(len(groups), 2)
        self.assertEqual(by_id[(canonical.pk,)].match_methods, (CANONICAL,))
        self.assertEqual(by_id[(alias.pk,)].alias_kinds, ("SPELLING_VARIANT",))

    def test_complete_provenance_ignores_match_row_order(self):
        entity = _entity("קהיר")
        pairs = (
            (CANONICAL, ""),
            (ALIAS, NonPersonEntityAlias.Kind.OCR_VARIANT),
        )
        first = _candidate(_proposal(_item("סדר א"), BODY), entity, matches=pairs)
        second = _candidate(
            _proposal(_item("סדר ב"), BODY),
            entity,
            matches=tuple(reversed(pairs)),
        )

        groups = _groups()

        self.assertEqual(len(groups), 1)
        group = groups[0]
        self.assertEqual(group.candidate_ids, (first.pk, second.pk))
        self.assertEqual(group.match_methods, (ALIAS, CANONICAL))
        self.assertEqual(group.alias_kinds, (NonPersonEntityAlias.Kind.OCR_VARIANT,))
        expected = occurrence_group_id(
            OccurrenceGroupKey(
                candidate_entity_id=entity.pk,
                normalized_surface=SURFACE,
                text_kind=MANUAL,
                provenance=tuple(sorted(pairs)),
            )
        )
        self.assertEqual(group.group_id, expected)

    def test_counts_cover_repeated_and_cross_item_occurrences(self):
        entity = _entity("קהיר")
        body = "קהיר ואחר כך קהיר"
        item = _item("כפול", body)
        repeated = [
            _candidate(
                _proposal(item, body, ordinal=ordinal),
                entity,
            )
            for ordinal in (1, 2)
        ]
        other = _candidate(_proposal(_item("נוסף"), BODY), entity)

        group = _groups()[0]

        self.assertEqual(group.candidate_count, 3)
        self.assertEqual(group.archive_item_count, 2)
        self.assertEqual(
            group.candidate_ids,
            (repeated[0].pk, repeated[1].pk, other.pk),
        )
        self.assertEqual(
            group.archive_item_ids, (item.pk, other.proposal.archive_item_id)
        )
        self.assertEqual(NonPersonEntityOccurrenceProposal.objects.count(), 3)

    def test_ordering_is_deterministic(self):
        later_name = _entity("קהיר")
        earlier_name = _entity("אלכסנדריה")
        late_item = _item("מאוחר", "עיר אלכסנדריה")
        early_item = _item("מוקדם", "עיר אלכסנדריה ואלכסנדריה")
        late_candidate = _candidate(
            _proposal(
                late_item,
                "עיר אלכסנדריה",
                surface="אלכסנדריה",
                matched_text="אלכסנדריה",
                ordinal=1,
            ),
            earlier_name,
        )
        early_second = _candidate(
            _proposal(
                early_item,
                "עיר אלכסנדריה ואלכסנדריה",
                surface="אלכסנדריה",
                matched_text="אלכסנדריה",
                ordinal=2,
            ),
            earlier_name,
        )
        early_first = _candidate(
            _proposal(
                early_item,
                "עיר אלכסנדריה ואלכסנדריה",
                surface="אלכסנדריה",
                matched_text="אלכסנדריה",
                ordinal=1,
            ),
            earlier_name,
        )
        cairo = _candidate(_proposal(_item("קהיר פריט"), BODY), later_name)

        first_read = _groups()
        second_read = _groups()

        self.assertEqual(
            [group.candidate_entity_id for group in first_read],
            [earlier_name.pk, later_name.pk],
        )
        self.assertEqual(
            first_read[0].candidate_ids,
            (late_candidate.pk, early_first.pk, early_second.pk),
        )
        self.assertEqual(first_read[1].candidate_ids, (cairo.pk,))
        self.assertEqual(
            [(group.group_id, group.candidate_ids) for group in first_read],
            [(group.group_id, group.candidate_ids) for group in second_read],
        )

    def test_representative_sample_is_deterministic_and_capped(self):
        entity = _entity("קהיר")
        created = [
            _candidate(_proposal(_item(f"פריט {index}"), BODY), entity)
            for index in range(8)
        ]

        first_read = _groups()[0]
        second_read = _groups()[0]
        sample_ids = tuple(
            example.candidate_id for example in first_read.representative_examples
        )

        self.assertEqual(first_read.candidate_count, 8)
        self.assertLessEqual(len(sample_ids), REPRESENTATIVE_EXAMPLE_LIMIT)
        self.assertIn(created[0].pk, sample_ids)
        self.assertIn(created[-1].pk, sample_ids)
        self.assertEqual(
            sample_ids,
            tuple(
                example.candidate_id for example in second_read.representative_examples
            ),
        )
        self.assertEqual(first_read.candidate_ids, tuple(row.pk for row in created))
        self.assertIn(MANY_ARCHIVE_ITEMS, first_read.risk_flags)

    def test_stale_source_stays_in_group_without_a_guessed_snippet(self):
        entity = _entity("קהיר")
        item = _item("פריט", BODY)
        candidate = _candidate(_proposal(item, BODY, matched_text="קהיר"), entity)
        content = ManualTextContent.objects.get(archive_item=item)
        content.body = "טקסט חדש לגמרי בלי השם"
        content.save(update_fields=["body", "updated_at"])

        group = _groups()[0]
        example = group.representative_examples[0]

        self.assertEqual(group.candidate_ids, (candidate.pk,))
        self.assertIn(SOURCE_STALE, group.risk_flags)
        self.assertIn(CONTEXT_UNAVAILABLE, group.risk_flags)
        self.assertFalse(example.has_context)
        self.assertEqual(example.context_before, "")
        self.assertEqual(example.context_match, "")
        self.assertEqual(example.context_after, "")
        self.assertNotIn("טקסט חדש", example.matched_text)
        self.assertEqual(candidate.status, PENDING)

    def test_context_window_comes_from_authoritative_text(self):
        entity = _entity("קהיר")
        prefix = "א" * 80
        suffix = "ב" * 80
        body = f"{prefix}{SURFACE}{suffix}"
        item = _item("חלון", body)
        _candidate(_proposal(item, body), entity)
        located = locate_surface_occurrences(body, SURFACE)
        assert located.occurrences is not None
        occurrence = located.occurrences[0]

        example = _groups()[0].representative_examples[0]

        self.assertTrue(example.has_context)
        self.assertEqual(example.context_match, occurrence.matched_text)
        self.assertEqual(
            example.context_before,
            body[max(0, occurrence.start - _CONTEXT_RADIUS) : occurrence.start],
        )
        self.assertEqual(
            example.context_after,
            body[occurrence.end : occurrence.end + _CONTEXT_RADIUS],
        )
        self.assertLess(len(example.context_before), len(prefix))
        self.assertEqual(example.occurrence_ordinal, 1)
        self.assertEqual(example.item_title, "חלון")

    def test_ocr_variant_and_short_surface_flags(self):
        entity = _entity("קהיר")
        variant = _candidate(
            _proposal(_item("וריאנט"), BODY),
            entity,
            matches=((ALIAS, NonPersonEntityAlias.Kind.OCR_VARIANT),),
        )
        short_body = "כאן תל שם"
        short = _candidate(
            _proposal(
                _item("קצר", short_body),
                short_body,
                surface="תל",
                matched_text="תל",
            ),
            entity,
            matches=((CANONICAL, ""),),
        )

        by_candidate = {group.candidate_ids: group for group in _groups()}

        self.assertIn(OCR_VARIANT, by_candidate[(variant.pk,)].risk_flags)
        self.assertIn(SHORT_SURFACE, by_candidate[(short.pk,)].risk_flags)
        self.assertNotIn(OCR_VARIANT, by_candidate[(short.pk,)].risk_flags)

    def test_multiple_methods_and_alias_kinds_are_group_flags(self):
        entity = _entity("קהיר")
        _candidate(
            _proposal(_item("מרובה"), BODY),
            entity,
            matches=(
                (CANONICAL, ""),
                (ALIAS, NonPersonEntityAlias.Kind.SPELLING_VARIANT),
                (DISPLAY, NonPersonEntityAlias.Kind.LANGUAGE_VARIANT),
            ),
        )

        flags = _groups()[0].risk_flags

        self.assertIn(MULTIPLE_MATCH_METHODS, flags)
        self.assertIn(MULTIPLE_ALIAS_KINDS, flags)

    def test_ordinal_gap_is_not_a_risk_flag(self):
        entity = _entity("קהיר")
        body = "קהיר ואז קהיר ואז קהיר"
        item = _item("פער", body)
        _candidate(_proposal(item, body, ordinal=1), entity)
        _candidate(_proposal(item, body, ordinal=3), entity)

        group = _groups()[0]

        self.assertEqual(group.candidate_count, 2)
        self.assertEqual(group.risk_flags, ())

    def test_prior_review_history_ignores_detect_only_events(self):
        entity = _entity("קהיר")
        plain = _candidate(_proposal(_item("רגיל"), BODY), entity)
        NonPersonEntityOccurrenceReviewEvent.objects.create(
            proposal=plain.proposal,
            candidate=plain,
            action=DETECT,
        )
        reviewed = _candidate(_proposal(_item("נבדק"), BODY), entity)
        NonPersonEntityOccurrenceReviewEvent.objects.create(
            proposal=reviewed.proposal,
            candidate=reviewed,
            action=REJECT,
        )

        group = _groups()[0]

        self.assertIn(PRIOR_REVIEW_HISTORY, group.risk_flags)
        self.assertEqual(
            tuple(example.candidate_id for example in group.outlier_examples),
            (reviewed.pk,),
        )
        self.assertEqual(plain.status, PENDING)
        self.assertEqual(reviewed.status, PENDING)

    def test_blank_matched_text_keeps_reconstructed_context(self):
        entity = _entity("קהיר")
        _candidate(_proposal(_item("ריק"), BODY, matched_text=""), entity)

        group = _groups()[0]
        example = group.representative_examples[0]

        self.assertIn(MATCHED_TEXT_UNAVAILABLE, group.risk_flags)
        self.assertTrue(example.has_context)
        self.assertEqual(example.context_match, SURFACE)

    def test_needs_research_is_not_grouped(self):
        entity = _entity("קהיר")
        pending = _candidate(_proposal(_item("ממתין"), BODY), entity)
        _candidate(
            _proposal(_item("בירור"), BODY),
            entity,
            status=NEEDS_RESEARCH,
        )

        groups = _groups()

        self.assertEqual(groups[0].candidate_ids, (pending.pk,))

    def test_existing_occurrence_queue_is_unchanged(self):
        staff = User.objects.create_user(
            username="group_staff",
            password="test-pass",
            is_staff=True,
        )
        entity = _entity("קהיר")
        item = _item("כותרת לבדיקה")
        candidate = _candidate(_proposal(item, BODY), entity)
        self.client.force_login(staff)

        rows = staff_occurrence_review_rows()
        queue = self.client.get(reverse(QUEUE))

        self.assertEqual([row.candidate_id for row in rows], [candidate.pk])
        self.assertEqual(queue.status_code, 200)
        self.assertContains(queue, "כותרת לבדיקה")
        self.assertContains(queue, "לפי קבוצות")
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertFalse(
            ArchiveItemEntityOccurrence.objects.filter(archive_item=item).exists()
        )

    def test_grouped_reads_do_not_write_domain_rows(self):
        entity = _entity("קהיר")
        candidate = _candidate(_proposal(_item("קריאה"), BODY), entity)
        before = _domain_counts()
        with CaptureQueriesContext(connection) as captured:
            page = staff_grouped_occurrence_review_page()
        for query in captured.captured_queries:
            sql = query["sql"].lstrip().upper()
            self.assertTrue(sql.startswith("SELECT"), sql)

        staff = User.objects.create_user(
            username="group_reader",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(staff)
        before_get = _domain_counts()
        response = self.client.get(reverse(GROUPS))
        posted = self.client.post(reverse(GROUPS), {"action": "approve"})

        self.assertEqual(page.groups[0].candidate_ids, (candidate.pk,))
        self.assertEqual(response.status_code, 200)
        self.assertNotContains(response, "אשר אזכור")
        self.assertNotContains(response, 'name="action"')
        self.assertEqual(posted.status_code, 405)
        self.assertEqual(_domain_counts(), before_get)
        candidate.refresh_from_db()
        self.assertEqual(candidate.status, PENDING)
        self.assertEqual(before["occurrences"], 0)

    def test_staff_page_filters_and_detail_lists_every_member(self):
        staff = User.objects.create_user(
            username="group_filter",
            password="test-pass",
            is_staff=True,
        )
        cairo = _entity("קהיר")
        other = _entity("אלכסנדריה")
        cairo_item = _item("פריט קהיר")
        cairo_candidate = _candidate(_proposal(cairo_item, BODY), cairo)
        _candidate(
            _proposal(
                _item("פריט אלכסנדריה", "עיר אלכסנדריה"),
                "עיר אלכסנדריה",
                surface="אלכסנדריה",
                matched_text="אלכסנדריה",
            ),
            other,
        )
        ocr_candidate = _candidate(
            _proposal(_ocr_item("תעתיק קהיר", BODY), BODY, text_kind=OCR),
            cairo,
        )
        self.client.force_login(staff)

        page = self.client.get(reverse(GROUPS), {"q": "קהיר", "entity": cairo.pk})
        kind = self.client.get(
            reverse(GROUPS),
            {"entity": cairo.pk, "text_kind": OCR},
        )
        risk = self.client.get(reverse(GROUPS), {"risk": "none"})
        flagged = self.client.get(reverse(GROUPS), {"risk": OCR_VARIANT})
        cairo_group = next(
            group for group in _groups() if cairo_candidate.pk in group.candidate_ids
        )
        detail = self.client.get(
            reverse(GROUP, kwargs={"group_id": cairo_group.group_id})
        )

        self.assertContains(page, "פריט קהיר")
        self.assertNotContains(page, "פריט אלכסנדריה")
        self.assertContains(kind, "תעתיק קהיר")
        self.assertNotContains(kind, "פריט קהיר")
        self.assertContains(risk, "פריט קהיר")
        self.assertNotContains(flagged, "פריט קהיר")
        self.assertContains(
            detail,
            reverse(
                "archive-manage-entity-occurrence-proposal",
                kwargs={"candidate_id": cairo_candidate.pk},
            ),
        )
        self.assertNotContains(
            detail,
            reverse(
                "archive-manage-entity-occurrence-proposal",
                kwargs={"candidate_id": ocr_candidate.pk},
            ),
        )
        self.assertNotContains(detail, "אשר אזכור")

    def test_non_admin_is_denied(self):
        family_group, _ = Group.objects.get_or_create(name=ARCHIVE_FAMILY_GROUP_NAME)
        user = User.objects.create_user(username="group_family", password="test-pass")
        user.groups.add(family_group)
        self.client.force_login(user)
        entity = _entity("קהיר")
        _candidate(_proposal(_item("חסום"), BODY), entity)
        group_id = _groups()[0].group_id

        queue = self.client.get(reverse(GROUPS))
        detail = self.client.get(reverse(GROUP, kwargs={"group_id": group_id}))

        self.assertEqual(queue.status_code, 403)
        self.assertEqual(detail.status_code, 403)


class GroupedReviewScaleTests(TestCase):
    def test_large_group_bounds_context_and_keeps_unsampled_stale_flag(self):
        entity = _entity("קהיר")
        count = 120
        body = "קהיר " * count
        item = _item("פריט גדול", body)
        created = []
        for ordinal in range(1, count + 1):
            matched = "" if ordinal in {2, 3} else SURFACE
            sha = "0" * 64 if ordinal == 60 else None
            created.append(
                _candidate(
                    _proposal(
                        item,
                        body,
                        ordinal=ordinal,
                        matched_text=matched,
                        sha=sha,
                    ),
                    entity,
                )
            )
        stale = created[59]
        module = "documents.services.non_person_entity_occurrence_grouped_review"
        with (
            patch(
                f"{module}.authoritative_text_context_for_item",
                wraps=authoritative_text_context_for_item,
            ) as contexts,
            patch(
                f"{module}.locate_surface_occurrences",
                wraps=locate_surface_occurrences,
            ) as located,
            patch(f"{module}._context_parts", wraps=_context_parts_real()) as windows,
        ):
            group = _groups()[0]

        sample_ids = [example.candidate_id for example in group.representative_examples]
        self.assertEqual(group.candidate_count, count)
        self.assertEqual(group.archive_item_count, 1)
        self.assertEqual(len(group.candidate_ids), count)
        self.assertIn(SOURCE_STALE, group.risk_flags)
        self.assertIn(CONTEXT_UNAVAILABLE, group.risk_flags)
        self.assertNotIn(stale.pk, sample_ids)
        self.assertLessEqual(len(sample_ids), REPRESENTATIVE_EXAMPLE_LIMIT)
        self.assertIn(created[0].pk, sample_ids)
        self.assertIn(created[-1].pk, sample_ids)
        self.assertEqual(contexts.call_count, 1)
        self.assertEqual(located.call_count, 1)
        self.assertLessEqual(windows.call_count, REPRESENTATIVE_EXAMPLE_LIMIT)
        self.assertLess(windows.call_count, count)
        again = _groups()[0]
        self.assertEqual(again.group_id, group.group_id)
        self.assertEqual(again.candidate_ids, group.candidate_ids)
        self.assertEqual(
            [example.candidate_id for example in again.representative_examples],
            sample_ids,
        )

    def test_detail_page_builds_context_only_for_displayed_members(self):
        cairo = _entity("קהיר")
        other = _entity("אלכסנדריה")
        count = GROUP_DETAIL_PAGE_SIZE * 2 + 20
        body = "קהיר " * count
        item = _item("פריט ממושך", body)
        created = [
            _candidate(_proposal(item, body, ordinal=ordinal), cairo)
            for ordinal in range(1, count + 1)
        ]
        other_body = "עיר אלכסנדריה"
        other_item = _item("פריט אחר", other_body)
        _candidate(
            _proposal(
                other_item,
                other_body,
                surface="אלכסנדריה",
                matched_text="אלכסנדריה",
            ),
            other,
        )
        group_id = next(
            group.group_id
            for group in _groups()
            if created[0].pk in group.candidate_ids
        )
        seen_items: list[int] = []

        def _record_context(archive_item):
            seen_items.append(archive_item.pk)
            return authoritative_text_context_for_item(archive_item)

        module = "documents.services.non_person_entity_occurrence_grouped_review"
        with (
            patch(
                f"{module}.authoritative_text_context_for_item",
                side_effect=_record_context,
            ),
            patch(f"{module}._context_parts", wraps=_context_parts_real()) as windows,
        ):
            detail = staff_occurrence_review_group_detail(group_id, page=2)
        assert detail is not None
        start = GROUP_DETAIL_PAGE_SIZE
        expected = created[start : start + GROUP_DETAIL_PAGE_SIZE]

        self.assertEqual(detail.page, 2)
        self.assertEqual(detail.page_count, 3)
        self.assertEqual(len(detail.examples), GROUP_DETAIL_PAGE_SIZE)
        self.assertEqual(
            [example.candidate_id for example in detail.examples],
            [candidate.pk for candidate in expected],
        )
        self.assertEqual(detail.examples[0].occurrence_ordinal, start + 1)
        self.assertEqual(windows.call_count, GROUP_DETAIL_PAGE_SIZE)
        self.assertLess(windows.call_count, count)
        self.assertEqual(seen_items, [item.pk])
        self.assertEqual(len(detail.group.candidate_ids), count)
        again = staff_occurrence_review_group_detail(group_id, page=2)
        assert again is not None
        self.assertEqual(
            [example.candidate_id for example in again.examples],
            [example.candidate_id for example in detail.examples],
        )
        filtered = [group.group_id for group in _groups(query="קהיר")]
        self.assertEqual(filtered, [group.group_id for group in _groups(query="קהיר")])
        self.assertEqual(filtered, [group_id])

        staff = User.objects.create_user(
            username="group_page_staff",
            password="test-pass",
            is_staff=True,
        )
        self.client.force_login(staff)
        before = _domain_counts()
        response = self.client.get(
            reverse(GROUP, kwargs={"group_id": group_id}),
            {"page": 2},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(_domain_counts(), before)
        self.assertContains(response, "עמוד 2 מתוך 3")
        self.assertContains(
            response,
            reverse(
                "archive-manage-entity-occurrence-proposal",
                kwargs={"candidate_id": expected[0].pk},
            ),
        )
        self.assertNotContains(
            response,
            reverse(
                "archive-manage-entity-occurrence-proposal",
                kwargs={"candidate_id": created[0].pk},
            ),
        )
        self.assertNotContains(response, "אשר אזכור")
        created[0].refresh_from_db()
        self.assertEqual(created[0].status, PENDING)


def _context_parts_real():
    from documents.services.non_person_entity_occurrence_review import (
        _context_parts,
    )

    return _context_parts
