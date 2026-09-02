from __future__ import annotations

import math

from django.test import SimpleTestCase

from documents.services.arabic_printed_banding import (
    BANDING_STRATEGY,
    MAX_BAND_HEIGHT_RATIO,
    MAX_BANDS,
    ArabicPrintedBandingError,
    ArabicPrintedLineBox,
    ArabicPrintedWordBox,
    REASON_CANNOT_COVER,
    REASON_EMPTY_GEOMETRY,
    REASON_EXCEEDS_MAX_BANDS,
    REASON_INVALID_BANDING_CONFIG,
    REASON_INVALID_BOX,
    REASON_INVALID_IMAGE_DIMENSIONS,
    REASON_LINE_EXCEEDS_MAX_HEIGHT,
    REASON_UNORDERED_LINES,
    REASON_UNSAFE_CUT,
    REASON_WORD_CROSSES_CUT,
    assign_words_to_band_rects,
    band_rects_from_line_groups,
    cluster_arabic_printed_lines,
    gap_pixels,
    line_content_height,
    plan_arabic_printed_bands,
    plan_arabic_printed_line_groups,
    validate_arabic_printed_band_plan,
)

IMAGE_WIDTH = 200
IMAGE_HEIGHT = 1000


def _line(ymin: int, ymax: int, *indexes: int) -> ArabicPrintedLineBox:
    return ArabicPrintedLineBox(ymin=ymin, ymax=ymax, word_indexes=indexes)


def _word(
    index: int, *, xmin: int = 10, ymin: int, xmax: int = 90, ymax: int
) -> ArabicPrintedWordBox:
    return ArabicPrintedWordBox(
        index=index, xmin=xmin, ymin=ymin, xmax=xmax, ymax=ymax
    )


def _isolated_required_band_lines(count: int) -> list[ArabicPrintedLineBox]:
    """In-bounds lines that cannot share a band at max_height_ratio=0.10.

    image_height=1000 → bound 100px. Each line is 21px. Consecutive pair span
    is 141px, so the planner must use exactly `count` bands.
    """
    return [_line(i * 120, i * 120 + 20, i) for i in range(count)]


def _page321_like_lines() -> list[ArabicPrintedLineBox]:
    """Synthetic two-section page: compact upper block, large lower block.

    Image height 1000 so the 35% cap is 350px. A 50% cap can keep the lower
    block as one band; 35% must split that lower block into additional
    complete-line bands. Coordinates are synthetic, not a live document 321 dump.
    """
    lines = []
    index = 0
    for row in range(4):
        ymin = 20 + row * 50
        lines.append(_line(ymin, ymin + 39, index))
        index += 1
    for row in range(8):
        ymin = 600 + row * 50
        lines.append(_line(ymin, ymin + 39, index))
        index += 1
    return lines


def _page321_like_words() -> list[ArabicPrintedWordBox]:
    words = []
    index = 0
    for row in range(4):
        ymin = 20 + row * 50
        words.append(_word(index, ymin=ymin, ymax=ymin + 39))
        index += 1
    for row in range(8):
        ymin = 600 + row * 50
        words.append(_word(index, ymin=ymin, ymax=ymin + 39))
        index += 1
    return words


def _assert_band_invariants(bands, *, image_width: int, image_height: int, word_count: int):
    validate_arabic_printed_band_plan(
        bands,
        image_width=image_width,
        image_height=image_height,
        word_count=word_count,
    )
    previous = None
    for band in bands:
        assert band.left == 0
        assert band.right == image_width
        assert 0 <= band.top < band.bottom <= image_height
        if previous is not None:
            assert previous.bottom <= band.top
            assert previous.top < band.top
        previous = band


class ArabicPrintedBandingTests(SimpleTestCase):
    def test_strategy_and_bounds_constants(self):
        self.assertEqual(BANDING_STRATEGY, "structural-gap-v3-hybrid")
        self.assertEqual(MAX_BANDS, 6)
        self.assertEqual(MAX_BAND_HEIGHT_RATIO, 0.35)

    def test_invalid_image_dimensions(self):
        word = _word(0, ymin=10, ymax=20)
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_bands(
                [word], image_width=0, image_height=IMAGE_HEIGHT
            )
        self.assertEqual(ctx.exception.reason, REASON_INVALID_IMAGE_DIMENSIONS)
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_bands(
                [word], image_width=IMAGE_WIDTH, image_height=-1
            )
        self.assertEqual(ctx.exception.reason, REASON_INVALID_IMAGE_DIMENSIONS)
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_line_groups(
                [_line(0, 10, 0)], image_height=True  # noqa: FBT003
            )
        self.assertEqual(ctx.exception.reason, REASON_INVALID_IMAGE_DIMENSIONS)

    def test_invalid_and_out_of_bounds_boxes(self):
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_bands([], image_width=IMAGE_WIDTH, image_height=IMAGE_HEIGHT)
        self.assertEqual(ctx.exception.reason, REASON_EMPTY_GEOMETRY)

        inverted = _word(0, xmin=90, ymin=10, xmax=10, ymax=20)
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_bands(
                [inverted], image_width=IMAGE_WIDTH, image_height=IMAGE_HEIGHT
            )
        self.assertEqual(ctx.exception.reason, REASON_INVALID_BOX)

        outside = _word(0, ymin=10, ymax=IMAGE_HEIGHT)
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_bands(
                [outside], image_width=IMAGE_WIDTH, image_height=IMAGE_HEIGHT
            )
        self.assertEqual(ctx.exception.reason, REASON_INVALID_BOX)

        duplicate = [
            _word(0, ymin=10, ymax=20),
            _word(0, ymin=80, ymax=90),
        ]
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_bands(
                duplicate, image_width=IMAGE_WIDTH, image_height=IMAGE_HEIGHT
            )
        self.assertEqual(ctx.exception.reason, REASON_INVALID_BOX)

    def test_max_height_and_six_band_bounds(self):
        tall = [_line(0, 350, 0)]
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_line_groups(tall, image_height=IMAGE_HEIGHT)
        self.assertEqual(ctx.exception.reason, REASON_LINE_EXCEEDS_MAX_HEIGHT)
        self.assertIn("35%", str(ctx.exception))

        packable = [_line(10, 80, 0), _line(90, 150, 1)]
        self.assertEqual(
            plan_arabic_printed_line_groups(packable, image_height=IMAGE_HEIGHT),
            [(0, 1)],
        )

        split = [_line(10, 80, 0), _line(600, 670, 1)]
        groups = plan_arabic_printed_line_groups(split, image_height=IMAGE_HEIGHT)
        self.assertEqual(groups, [(0, 0), (1, 1)])
        for start, end in groups:
            height = split[end].ymax - split[start].ymin + 1
            self.assertLessEqual(height, 0.35 * IMAGE_HEIGHT)

        isolated = [
            _line(0, 20, 0),
            _line(490, 510, 1),
            _line(980, 999, 2),
        ]
        self.assertEqual(
            len(plan_arabic_printed_line_groups(isolated, image_height=IMAGE_HEIGHT)),
            3,
        )
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_line_groups(
                isolated, image_height=IMAGE_HEIGHT, max_bands=2
            )
        self.assertEqual(ctx.exception.reason, REASON_EXCEEDS_MAX_BANDS)
        self.assertIn("exceeds the maximum", str(ctx.exception))

    def test_exact_35_percent_boundary_allows_350_rejects_351(self):
        # Arm E rejects only when content_height > 0.35 * image_height.
        # Inclusive height 350 is allowed; 351 (ymin=0, ymax=350) fails.
        allowed = [_line(0, 349, 0)]
        self.assertEqual(line_content_height(allowed, 0, 0), 350)
        self.assertEqual(
            plan_arabic_printed_line_groups(allowed, image_height=IMAGE_HEIGHT),
            [(0, 0)],
        )
        rejected = [_line(0, 350, 0)]
        self.assertEqual(line_content_height(rejected, 0, 0), 351)
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_line_groups(rejected, image_height=IMAGE_HEIGHT)
        self.assertEqual(ctx.exception.reason, REASON_LINE_EXCEEDS_MAX_HEIGHT)
        self.assertIn("35%", str(ctx.exception))

    def test_float_sensitive_height_matches_arm_e_multiplication(self):
        image_height = 180
        max_height_ratio = 0.35
        bound = max_height_ratio * image_height
        self.assertEqual(bound, 0.35 * 180)
        self.assertEqual(bound, 62.99999999999999)
        rejected = [_line(0, 62, 0)]
        self.assertEqual(line_content_height(rejected, 0, 0), 63)
        self.assertTrue(63 > bound)
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_line_groups(
                rejected,
                image_height=image_height,
                max_height_ratio=max_height_ratio,
            )
        self.assertEqual(ctx.exception.reason, REASON_LINE_EXCEEDS_MAX_HEIGHT)

    def test_page321_like_geometry_splits_tall_lower_block(self):
        lines = _page321_like_lines()
        groups_50 = plan_arabic_printed_line_groups(
            lines, image_height=IMAGE_HEIGHT, max_height_ratio=0.50
        )
        self.assertEqual(groups_50, [(0, 3), (4, 11)])
        groups = plan_arabic_printed_line_groups(lines, image_height=IMAGE_HEIGHT)
        self.assertEqual(groups, [(0, 3), (4, 4), (5, 11)])
        self.assertGreater(len(groups), len(groups_50))
        self.assertEqual(gap_pixels(lines, 3), 390)
        for start, end in groups:
            self.assertLessEqual(
                line_content_height(lines, start, end), 0.35 * IMAGE_HEIGHT
            )
            for line in lines[start : end + 1]:
                self.assertGreaterEqual(line.ymin, lines[start].ymin)
                self.assertLessEqual(line.ymax, lines[end].ymax)
        rects, gaps = band_rects_from_line_groups(
            lines, groups, image_width=IMAGE_WIDTH
        )
        self.assertEqual(
            rects,
            [
                (0, 20, 200, 405),
                (0, 405, 200, 645),
                (0, 645, 200, 990),
            ],
        )
        self.assertEqual(gaps, [None, 390, 10])
        words = _page321_like_words()
        assignments = assign_words_to_band_rects(words, rects)
        self.assertEqual(
            assignments,
            [
                (0, 1, 2, 3),
                (4,),
                (5, 6, 7, 8, 9, 10, 11),
            ],
        )
        assigned_ids = [index for group in assignments for index in group]
        self.assertEqual(assigned_ids, list(range(12)))
        self.assertEqual(len(assigned_ids), len(set(assigned_ids)))
        for word in words:
            matches = [
                band
                for band, (_left, top, _right, bottom) in enumerate(rects)
                if word.ymin >= top and word.ymax < bottom
            ]
            self.assertEqual(len(matches), 1)

        bands = plan_arabic_printed_bands(
            words, image_width=IMAGE_WIDTH, image_height=IMAGE_HEIGHT
        )
        self.assertEqual(
            [(band.left, band.top, band.right, band.bottom) for band in bands],
            rects,
        )
        _assert_band_invariants(
            bands,
            image_width=IMAGE_WIDTH,
            image_height=IMAGE_HEIGHT,
            word_count=12,
        )

    def test_minimum_band_count_wins(self):
        lines = [
            _line(0, 20, 0),
            _line(80, 100, 1),
            _line(200, 220, 2),
            _line(600, 620, 3),
        ]
        groups = plan_arabic_printed_line_groups(lines, image_height=IMAGE_HEIGHT)
        self.assertEqual(groups, [(0, 2), (3, 3)])
        self.assertEqual(len(groups), 2)
        three_band_gap_sum = (
            gap_pixels(lines, 0) + gap_pixels(lines, 1) + gap_pixels(lines, 2)
        )
        chosen_gap = gap_pixels(lines, 2)
        self.assertGreater(three_band_gap_sum, chosen_gap)
        for start, end in groups:
            self.assertLessEqual(
                line_content_height(lines, start, end), 0.35 * IMAGE_HEIGHT
            )

    def test_larger_feasible_whitespace_gap_wins(self):
        lines = [
            _line(0, 40, 0),
            _line(80, 120, 1),
            _line(380, 410, 2),
        ]
        groups = plan_arabic_printed_line_groups(lines, image_height=IMAGE_HEIGHT)
        self.assertEqual(groups, [(0, 1), (2, 2)])
        small_gap = gap_pixels(lines, 0)
        large_gap = gap_pixels(lines, 1)
        self.assertGreater(large_gap, small_gap)
        self.assertLessEqual(line_content_height(lines, 1, 2), 0.35 * IMAGE_HEIGHT)

    def test_positional_tie_break_is_deterministic(self):
        lines = [
            _line(0, 20, 0),
            _line(260, 280, 1),
            _line(520, 540, 2),
        ]
        self.assertEqual(gap_pixels(lines, 0), gap_pixels(lines, 1))
        first = plan_arabic_printed_line_groups(lines, image_height=IMAGE_HEIGHT)
        second = plan_arabic_printed_line_groups(lines, image_height=IMAGE_HEIGHT)
        self.assertEqual(first, second)
        self.assertEqual(first, [(0, 0), (1, 2)])

    def test_line_split_prevention_rejects_overlapping_line_boxes(self):
        overlapping = [_line(0, 80, 0), _line(40, 120, 1)]
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_line_groups(overlapping, image_height=IMAGE_HEIGHT)
        self.assertEqual(ctx.exception.reason, REASON_UNSAFE_CUT)

    def test_word_crossing_a_cut_fails_closed(self):
        rects = [(0, 10, 200, 100), (0, 100, 200, 200)]
        crossing = _word(0, ymin=90, ymax=110)
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            assign_words_to_band_rects([crossing], rects)
        self.assertEqual(ctx.exception.reason, REASON_WORD_CROSSES_CUT)

    def test_invalid_banding_config_fail_closed(self):
        lines = [_line(0, 10, 0)]
        invalid_max_bands = (
            True,
            False,
            0,
            -1,
            7,
            1.0,
            None,
            "6",
        )
        for max_bands in invalid_max_bands:
            with self.subTest(max_bands=max_bands):
                with self.assertRaises(ArabicPrintedBandingError) as ctx:
                    plan_arabic_printed_line_groups(
                        lines,
                        image_height=IMAGE_HEIGHT,
                        max_bands=max_bands,
                    )
                self.assertEqual(ctx.exception.reason, REASON_INVALID_BANDING_CONFIG)

        invalid_ratios = (
            True,
            False,
            None,
            "0.35",
            0,
            -0.1,
            1.01,
            math.nan,
            math.inf,
            -math.inf,
        )
        for ratio in invalid_ratios:
            with self.subTest(max_height_ratio=ratio):
                with self.assertRaises(ArabicPrintedBandingError) as ctx:
                    plan_arabic_printed_line_groups(
                        lines,
                        image_height=IMAGE_HEIGHT,
                        max_height_ratio=ratio,
                    )
                self.assertEqual(ctx.exception.reason, REASON_INVALID_BANDING_CONFIG)

        self.assertEqual(
            plan_arabic_printed_line_groups(
                [_line(0, 999, 0)],
                image_height=IMAGE_HEIGHT,
                max_height_ratio=1,
            ),
            [(0, 0)],
        )

    def test_direct_line_group_bounds_uniqueness_and_order(self):
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_line_groups(
                [_line(-1, 10, 0)], image_height=IMAGE_HEIGHT
            )
        self.assertEqual(ctx.exception.reason, REASON_INVALID_BOX)

        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_line_groups(
                [_line(0, IMAGE_HEIGHT, 0)], image_height=IMAGE_HEIGHT
            )
        self.assertEqual(ctx.exception.reason, REASON_INVALID_BOX)

        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_line_groups(
                [_line(10, 20, 0), _line(40, 50, 0)],
                image_height=IMAGE_HEIGHT,
            )
        self.assertEqual(ctx.exception.reason, REASON_INVALID_BOX)

        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_line_groups(
                [_line(80, 90, 1), _line(10, 20, 0)],
                image_height=IMAGE_HEIGHT,
            )
        self.assertEqual(ctx.exception.reason, REASON_UNORDERED_LINES)

    def test_six_required_bands_succeeds_at_explicit_tenth_ratio(self):
        lines = _isolated_required_band_lines(6)
        bound = 0.10 * IMAGE_HEIGHT
        self.assertEqual(len(lines), 6)
        self.assertLess(lines[-1].ymax, IMAGE_HEIGHT)
        for index in range(5):
            self.assertGreater(line_content_height(lines, index, index + 1), bound)
            self.assertLessEqual(line_content_height(lines, index, index), bound)
        groups = plan_arabic_printed_line_groups(
            lines,
            image_height=IMAGE_HEIGHT,
            max_height_ratio=0.10,
        )
        self.assertEqual(groups, [(i, i) for i in range(6)])
        self.assertEqual(MAX_BAND_HEIGHT_RATIO, 0.35)

    def test_seven_required_bands_fails_at_explicit_tenth_ratio(self):
        lines = _isolated_required_band_lines(7)
        bound = 0.10 * IMAGE_HEIGHT
        self.assertLess(lines[-1].ymax, IMAGE_HEIGHT)
        for index in range(6):
            self.assertGreater(line_content_height(lines, index, index + 1), bound)
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_line_groups(
                lines,
                image_height=IMAGE_HEIGHT,
                max_height_ratio=0.10,
            )
        self.assertEqual(ctx.exception.reason, REASON_EXCEEDS_MAX_BANDS)
        self.assertIn("exceeds the maximum", str(ctx.exception))

    def test_impossible_partition_when_a_line_cannot_fit(self):
        with self.assertRaises(ArabicPrintedBandingError) as ctx:
            plan_arabic_printed_line_groups(
                [_line(0, 500, 0), _line(600, 620, 1)],
                image_height=IMAGE_HEIGHT,
            )
        self.assertEqual(ctx.exception.reason, REASON_LINE_EXCEEDS_MAX_HEIGHT)
        self.assertNotEqual(ctx.exception.reason, REASON_CANNOT_COVER)

    def test_clustered_words_plan_full_width_ordered_non_overlapping_bands(self):
        words = [
            _word(0, ymin=10, ymax=80),
            _word(1, ymin=600, ymax=670),
        ]
        bands = plan_arabic_printed_bands(
            words, image_width=IMAGE_WIDTH, image_height=IMAGE_HEIGHT
        )
        self.assertEqual(len(bands), 2)
        self.assertEqual([band.word_indexes for band in bands], [(0,), (1,)])
        _assert_band_invariants(
            bands,
            image_width=IMAGE_WIDTH,
            image_height=IMAGE_HEIGHT,
            word_count=2,
        )
        clustered = cluster_arabic_printed_lines(words)
        self.assertEqual(len(clustered), 2)

    def test_identical_input_is_deterministic(self):
        words = _page321_like_words()
        first = plan_arabic_printed_bands(
            words, image_width=IMAGE_WIDTH, image_height=IMAGE_HEIGHT
        )
        second = plan_arabic_printed_bands(
            words, image_width=IMAGE_WIDTH, image_height=IMAGE_HEIGHT
        )
        self.assertEqual(first, second)
