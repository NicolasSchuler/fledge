"""Artwork measurements of a PDF: printer marks, composition, drawings, strokes and regions."""

from __future__ import annotations

import math
import random
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from latexprep.errors import PreparationError
from latexprep.pdf_artwork import inspect_artwork_inputs, inspect_pdf_artwork
from latexprep.pdf_artwork_drawings import (
    color_family,
    effective_stroke_widths,
    placement_sampling,
    union_area,
)
from latexprep.pdf_artwork_options import (
    ArtworkLimits,
    ArtworkRule,
    PdfArtworkOptions,
    configured_artwork_rules,
)
from latexprep.pdf_artwork_rendered import GrayPage, region_ink
from latexprep.pdf_artwork_rules import RULES
from latexprep.pdf_artwork_trace import (
    Box,
    Matrix,
    Point,
    misaligned_frame_pages,
    parse_drawing_trace,
)
from latexprep.pdf_check_options import PdfRegion
from latexprep.pdf_tool_output import parse_mupdf_trace
from latexprep.raster_pixels import InkMask, RasterImage
from tests.pdf_fakes import (
    ArtworkRunner,
    raster,
    rectangle,
    trace,
    type3_objects,
)


def region(name: str = "Figure", *, left: float = 0, right: float = 100) -> PdfRegion:
    """Return a region on page 1 that spans from the top at 0 to the bottom at 100 points.

    Args:
        name: The name of the region in findings.
        left: The left edge in points.
        right: The right edge in points.

    Returns:
        The region.
    """
    return PdfRegion(name, 1, left, 0, right, 100)


def image(*, matrix: str = "100 0 0 100 0 0", width: int = 300, height: int = 300) -> str:
    """Return the mutool trace markup of an opaque image.

    Args:
        matrix: The transformation matrix of the image.
        width: The width of the image in pixels.
        height: The height of the image in pixels.

    Returns:
        A ``fill_image`` element.
    """
    return f'<fill_image transform="{matrix}" width="{width}" height="{height}" alpha="1"/>'


def stroke(
    *,
    first: tuple[float, float] = (10, 10),
    second: tuple[float, float] = (90, 10),
    matrix: str = "1 0 0 1 0 0",
    width: float = 1,
) -> str:
    """Return the mutool trace markup of one straight stroked line.

    Args:
        first: The (x, y) point where the line starts.
        second: The (x, y) point where the line ends.
        matrix: The transformation matrix of the path.
        width: The line width.

    Returns:
        A ``stroke_path`` element with one ``moveto`` and one ``lineto``.
    """
    return (
        f'<stroke_path linewidth="{width}" transform="{matrix}">'
        f'<moveto x="{first[0]}" y="{first[1]}"/>'
        f'<lineto x="{second[0]}" y="{second[1]}"/></stroke_path>'
    )


def colored(space: str | None) -> str:
    """Return the mutool trace markup of an empty fill path in the color space ``space``.

    Returns:
        A ``fill_path`` element, without a ``colorspace`` attribute when ``space`` is
        ``None``.
    """
    attribute = f' colorspace="{space}"' if space is not None else ""
    return f'<fill_path{attribute} color="0" transform="1 0 0 1 0 0"></fill_path>'


class ArtworkRuleCatalogueTests(unittest.TestCase):
    """The artwork rules: names match the catalogue, and configured rules follow the options."""

    def test_rule_names_match_the_catalogue_exactly(self) -> None:
        self.assertEqual({rule.value for rule in ArtworkRule}, {rule.name for rule in RULES})

    def test_configured_rules_follow_the_options_in_report_order(self) -> None:
        self.assertEqual(configured_artwork_rules(PdfArtworkOptions()), [])
        options = PdfArtworkOptions(
            grayscale_preview=True,
            check_clipping=True,
            forbid_bitmap_type3_glyphs=True,
            required_color_space="cmyk",
        )
        self.assertEqual(
            configured_artwork_rules(options),
            [
                ArtworkRule.CLIPPING_CANDIDATES,
                ArtworkRule.TYPE3_GLYPH_PROGRAMS,
                ArtworkRule.GRAYSCALE_PREVIEW,
                ArtworkRule.COLOR_SPACE,
            ],
        )
        self.assertEqual(ArtworkRule.COLOR_SPACE, "figure.color_space")


class TraceGeometryTests(unittest.TestCase):
    """Trace geometry: matrices and boxes, drawing extents and clips, and misaligned frames."""

    def parsed(self, content: str, *, box: str = "0 0 100 100"):
        """Parse the drawings of a one-page trace that holds ``content``.

        Args:
            content: The trace markup of the page.
            box: The media box of the page.

        Returns:
            The parsed drawing trace of page 1.
        """
        return parse_drawing_trace(parse_mupdf_trace(trace(content, box=box), 1))

    def test_matrix_and_box_primitives(self) -> None:
        matrix = Matrix(0, 1, -1, 0, 10, 20)
        self.assertEqual(matrix.apply(Point(2, 3)), (7, 22))
        self.assertEqual(matrix.determinant, 1)
        box = Box.around([Point(1, 2), Point(5, 1)])
        self.assertEqual(box, (1, 1, 5, 2))
        self.assertTrue(box.contains((1.5, 1, 5, 2)))
        self.assertFalse(box.contains((0, 1, 5, 2)))
        self.assertTrue(box.contains((0.95, 1, 5, 2.05), 0.1))
        self.assertTrue(Box(0, 0, 10, 10).clipped(Box(20, 0, 30, 10)).is_empty)
        with self.assertRaises(PreparationError):
            Box.around([])
        with self.assertRaises(PreparationError):
            Matrix(1, 0, 0, 1, 0, 0).apply(Point(math.inf, 0))

    def test_drawings_keep_extent_clip_and_page_uncertainty(self) -> None:
        trace_ = self.parsed(
            rectangle(tag="clip_path", right=50) + image() + "<pop_clip/>" + image()
        )
        first, second = trace_.drawings
        self.assertEqual((first.index, second.index), (1, 2))
        self.assertEqual(first.box, (0, 0, 100, 100))
        self.assertEqual(first.clip, (10, 10, 50, 90))
        self.assertEqual(second.clip, (0, 0, 100, 100))
        self.assertEqual(trace_.trace_uncertain_pages, frozenset())
        grouped = self.parsed(image() + "<group>" + image() + "</group>" + image())
        self.assertEqual(grouped.trace_uncertain_pages, frozenset({1}))
        # Only drawings after the first uncertain element are marked uncertain.
        self.assertEqual([d.uncertain for d in grouped.drawings], [False, True, True])
        with self.assertRaises(PreparationError):
            self.parsed("<pop_clip/>")
        with self.assertRaises(PreparationError):
            self.parsed("", box="10 10 5 5")

    def test_misaligned_frames_are_a_pure_function_of_boxes_and_sizes(self) -> None:
        boxes = [
            Box(0, 0, 100, 100),
            Box(0, 0, 100.4, 100),
            Box(5, 0, 105, 100),
            Box(0, 0, 90, 100),
        ]
        sizes = [(100, 100)] * 4
        self.assertEqual(misaligned_frame_pages(boxes, sizes, 0.5), frozenset({3, 4}))
        self.assertEqual(misaligned_frame_pages(boxes, sizes, 0), frozenset({2, 3, 4}))
        self.assertEqual(misaligned_frame_pages(boxes, sizes, 0.5), frozenset({3, 4}))


class DrawingMeasurementTests(unittest.TestCase):
    """Drawing measurements: union area, color families, stroke widths, placement sampling."""

    def drawings(self, content: str):
        """Parse the drawings of a one-page trace that holds ``content``.

        Returns:
            The drawings of page 1.
        """
        return parse_drawing_trace(parse_mupdf_trace(trace(content), 1)).drawings

    def test_union_area_counts_overlaps_once_and_clips_to_the_region(self) -> None:
        region = Box(0, 0, 10, 10)
        self.assertEqual(union_area([], region), 0)
        self.assertEqual(union_area([Box(0, 0, 6, 10), Box(4, 0, 10, 10)], region), 100)
        self.assertEqual(
            union_area([Box(0, 0, 5, 5), Box(0, 0, 5, 5), Box(20, 20, 30, 30)], region), 25
        )
        self.assertEqual(union_area([Box(-5, -5, 5, 5)], region), 25)
        too_many = [Box(0, 0, 1, 1)] * 1001
        with self.assertRaises(PreparationError):
            union_area(too_many, region)

    def test_color_families_cover_device_and_calibrated_names_and_leave_the_rest_unknown(
        self,
    ) -> None:
        for name, family in (
            ("DeviceRGB", "rgb"),
            ("CalRGB", "rgb"),
            ("DeviceCMYK", "cmyk"),
            ("DeviceGray", "gray"),
            ("CalGray", "gray"),
            ("Separation(Spot)", None),
            ("DeviceN(Cyan,Magenta)", None),
            ("Indexed(DeviceRGB)", None),
            ("Lab", None),
            ("ICCBased(Unknown profile)", None),
        ):
            with self.subTest(name=name):
                self.assertEqual(color_family(name), family)

    def test_stroke_width_is_measured_normal_to_each_segment_after_placement(self) -> None:
        (plain,) = self.drawings(stroke(width=2))
        self.assertEqual(effective_stroke_widths(plain), [2])
        (squeezed,) = self.drawings(stroke(matrix="2 0 0 .5 0 0", width=0.5))
        self.assertEqual(effective_stroke_widths(squeezed), [0.25])
        (hairline,) = self.drawings(stroke(width=0))
        self.assertEqual(effective_stroke_widths(hairline), [])
        (singular,) = self.drawings(stroke(matrix="1 0 0 0 0 0"))
        self.assertEqual(effective_stroke_widths(singular), [])

    def test_placement_sampling_reports_axis_and_coarsest_direction_resolution(self) -> None:
        (sheared,) = self.drawings(image(matrix="100 0 100 100 0 0", width=100, height=100))
        sampling = placement_sampling(sheared)
        assert sampling is not None
        self.assertEqual(sampling.x_dpi, 72)
        self.assertAlmostEqual(sampling.y_dpi, 72 / math.sqrt(2))
        self.assertLess(sampling.minimum_direction_dpi, 50)
        (flat,) = self.drawings(image(matrix="0 0 0 100 0 0"))
        self.assertIsNone(placement_sampling(flat))


class RegionInkTests(unittest.TestCase):
    """:func:`region_ink`: ink, edge ink and content bounds match a per-pixel reference."""

    def test_ink_edge_ink_and_content_bounds_match_a_per_pixel_reference(self) -> None:
        rng = random.Random(11)
        for case in range(400):
            width, height = rng.randint(1, 24), rng.randint(1, 20)
            pixels = bytearray([255]) * (width * height)
            for _ in range(rng.randint(0, 10)):
                pixels[rng.randrange(width * height)] = rng.randrange(256)
            width_pt, height_pt = rng.uniform(10, 200), rng.uniform(10, 200)
            left, right = sorted(rng.randint(0, width) for _ in range(2))
            top, bottom = sorted(rng.randint(0, height) for _ in range(2))
            if left == right or top == bottom:
                continue
            options = PdfArtworkOptions(
                figure_edge_band_pt=rng.choice([None, 0.5, 6, 500]),
                background_gray=rng.choice([255, 0, 100]),
                ink_difference=rng.randint(1, 255),
            )
            band = options.figure_edge_band_pt or 0
            band_x, band_y = (
                math.ceil(band * width / width_pt),
                math.ceil(band * height / height_pt),
            )
            ink = [
                (x, y)
                for y in range(top, bottom)
                for x in range(left, right)
                if abs(pixels[y * width + x] - options.background_gray) >= options.ink_difference
            ]
            image = RasterImage(width, height, 1, bytes(pixels))
            result = region_ink(
                GrayPage(1, image, width_pt, height_pt),
                InkMask.from_gray(image, options.background_gray, options.ink_difference),
                (left, top, right, bottom),
                options,
            )
            with self.subTest(case=case):
                self.assertEqual(result.ink_pixels, len(ink))
                if not ink:
                    self.assertEqual(result[1:], (0, 1, None))
                    continue
                edge = sum(
                    x < left + band_x
                    or x >= right - band_x
                    or y < top + band_y
                    or y >= bottom - band_y
                    for x, y in ink
                )
                xs, ys = [x for x, _ in ink], [y for _, y in ink]
                area = (max(xs) - min(xs) + 1) * (max(ys) - min(ys) + 1)
                self.assertEqual(result.edge_ink_pixels, edge)
                self.assertEqual(
                    result.outside_content_box_ratio, 1 - area / ((right - left) * (bottom - top))
                )
                self.assertEqual(
                    result.content_bounds_pt,
                    (
                        min(xs) * width_pt / width,
                        min(ys) * height_pt / height,
                        (max(xs) + 1) * width_pt / width,
                        (max(ys) + 1) * height_pt / height,
                    ),
                )


class PdfArtworkTests(unittest.IsolatedAsyncioTestCase):
    """Artwork rules through :func:`inspect_pdf_artwork` with a controlled PDF tool double."""

    async def asyncSetUp(self) -> None:
        """Create a temporary folder that holds a placeholder ``figure.pdf``."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "figure.pdf"
        self.pdf.write_bytes(b"%PDF-fixture")
        self.sequence = 0

    async def asyncTearDown(self) -> None:
        """Remove the temporary folder."""
        self.temporary.cleanup()

    def work(self) -> Path:
        """Name a new work folder for one inspection; the folder is not created.

        Returns:
            ``artwork-<n>`` in the temporary folder, where ``n`` counts the calls from 1.
        """
        self.sequence += 1
        return self.root / f"artwork-{self.sequence}"

    async def check(
        self,
        options: PdfArtworkOptions,
        runner: ArtworkRunner | None = None,
        limits: ArtworkLimits | None = None,
    ):
        """Inspect the artwork of the PDF in a new work folder.

        Args:
            options: The artwork checks to run.
            runner: The PDF tool double, or ``None`` for a default :class:`ArtworkRunner`.
            limits: The artwork limits, or ``None`` for the defaults.

        Returns:
            The findings of the inspection.
        """
        return await inspect_pdf_artwork(
            self.pdf, self.work(), runner or ArtworkRunner(), options=options, limits=limits
        )

    async def status_for(self, options: PdfArtworkOptions, expected: str, runner: ArtworkRunner):
        """Inspect the PDF and assert that there is one finding, with the status ``expected``.

        Args:
            options: The artwork checks to run.
            expected: The status the finding must have.
            runner: The PDF tool double.

        Returns:
            The one finding.
        """
        findings = await self.check(options, runner)
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].status, expected, findings)
        return findings[0]

    async def status(self, options: PdfArtworkOptions, expected: str, **values: object):
        """Inspect the PDF as :meth:`status_for` does, with an :class:`ArtworkRunner`.

        Args:
            options: The artwork checks to run.
            expected: The status the finding must have.
            **values: Tool outputs that replace the defaults of the runner.

        Returns:
            The one finding.
        """
        return await self.status_for(options, expected, ArtworkRunner(**values))

    async def test_crop_marks_need_trim_aligned_pairs_and_keep_exemptions_and_uncertainty(
        self,
    ) -> None:
        options = PdfArtworkOptions(
            detect_printer_marks=True,
            trim_regions=(PdfRegion("Trim", 1, 20, 20, 80, 80),),
            printer_mark_max_length_pt=10,
        )
        clean = await self.status(options, "passed", trace=trace(stroke()))
        self.assertEqual(
            clean.message,
            "No short crop or registration strokes were found outside the trim region of "
            "1 measured page.",
        )
        marks = stroke(first=(5, 20), second=(15, 20)) + stroke(first=(20, 5), second=(20, 15))
        finding = await self.status(options, "failed", trace=trace(marks))
        self.assertEqual(finding.details["violations"][0]["corner_pt"], (20, 20))
        self.assertEqual(
            finding.message,
            "Possible printer marks outside the trim region on page 1: 1 crop-mark corner and "
            "0 crossed registration stroke pairs.",
        )
        await self.status(
            options, "passed", trace=trace(marks.split("</stroke_path>")[0] + "</stroke_path>")
        )
        exempt = await self.status(
            replace(options, printer_mark_exemptions=(1,)), "inconclusive", trace=trace(marks)
        )
        self.assertEqual(exempt.details["violation_count"], 0)
        await self.status(options, "inconclusive", missing="mutool")

    async def test_composition_does_not_assume_type3_text_is_vector(self) -> None:
        options = PdfArtworkOptions(classify_artwork=True)
        text = (
            '<fill_text transform="1 0 0 1 0 0"><span>'
            '<g unicode="A" x="10" y="10"/></span></fill_text>'
        )
        finding = await self.status(options, "passed", trace=trace(text))
        self.assertEqual(finding.details["classification"], "vector")
        runner = ArtworkRunner(trace=trace(text))
        runner.fonts = runner.fonts.replace("Type 1", "Type 3")
        result = await self.check(options, runner)
        self.assertEqual(result[0].status, "inconclusive")
        self.assertEqual(result[0].details["unclassified_text_operations"], 1)
        self.assertIsNone(result[0].details["font_inventory_error"])
        self.assertNotIn("pdffonts", result[0].message)
        failed = await self.status(options, "inconclusive", trace=trace(text), missing="pdffonts")
        self.assertIn("pdffonts could not list the fonts (Missing tool: pdffonts)", failed.message)
        self.assertEqual(failed.details["font_inventory_error"], "Missing tool: pdffonts")

    async def test_type3_decoding_excludes_image_pixels(self) -> None:
        runner = ArtworkRunner(objects=type3_objects(b"/Picture Do", nested=True))
        result = await self.check(PdfArtworkOptions(forbid_bitmap_type3_glyphs=True), runner)
        self.assertEqual(result[0].status, "failed")
        command = next(argv for argv, _, _ in runner.calls if "--json-stream-data=inline" in argv)
        self.assertIn("--json-object=5,0", command)
        self.assertIn("--json-object=6,0", command)
        self.assertNotIn("--json-object=7,0", command)

    async def test_registration_crosses_are_located_outside_trim_only(self) -> None:
        options = PdfArtworkOptions(
            detect_printer_marks=True,
            trim_regions=(PdfRegion("Trim", 1, 20, 20, 80, 80),),
            printer_mark_max_length_pt=10,
        )
        crossed = stroke(first=(5, 50), second=(15, 50)) + stroke(first=(10, 45), second=(10, 55))
        finding = await self.status(options, "failed", trace=trace(crossed))
        self.assertEqual(finding.details["violations"][0]["center_pt"], (10, 50))
        inside = stroke(first=(45, 50), second=(55, 50)) + stroke(first=(50, 45), second=(50, 55))
        await self.status(options, "passed", trace=trace(inside))

    async def test_whitespace_measures_outer_content_box_and_explicit_background(self) -> None:
        options = PdfArtworkOptions(figure_regions=(region(),), max_figure_whitespace_ratio=0.5)
        good = await self.status(options, "passed")
        self.assertAlmostEqual(good.details["outside_content_box_ratio"], 0.36)
        self.assertEqual(
            good.message,
            "Figure on page 1 leaves 36.0% of its area empty around the content, within the "
            "50.0% limit.",
        )
        bad = await self.status(options, "failed", raster=raster(box=(40, 40, 60, 60)))
        self.assertAlmostEqual(bad.details["outside_content_box_ratio"], 0.96)
        self.assertEqual(bad.details["content_bounds_pt"], (40, 40, 60, 60))
        self.assertIn("leaves 96.0% of its area empty", bad.message)
        self.assertIn("above the 50.0% limit", bad.message)
        await self.status(
            replace(options, background_gray=0), "passed", raster=raster(background=0)
        )
        await self.status(options, "inconclusive", raster=b"P5\n100 100\n255\nshort")
        await self.status(replace(options, figure_regions=()), "inconclusive")

    async def test_figure_edge_ink_is_advisory_and_rejects_cropped_page_frame(self) -> None:
        options = PdfArtworkOptions(figure_regions=(region(),), figure_edge_band_pt=1)
        await self.status(options, "passed")
        finding = await self.status(options, "failed", raster=raster(box=(0, 20, 30, 60)))
        self.assertEqual(finding.severity, "warning")
        self.assertEqual(finding.details["violations"][0]["edge_ink_pixels"], 40)
        self.assertIn(
            "Ink touches the outer 1 pt border of Figure on page 1 (40 pixels)", finding.message
        )
        runner = ArtworkRunner()
        runner.geometry = runner.geometry.replace("CropBox: 0 0 100 100", "CropBox: 10 10 90 90")
        result = await self.check(options, runner)
        self.assertEqual(result[0].status, "inconclusive")

    async def test_drawing_bounds_transform_images_and_reject_unmeasured_curves(self) -> None:
        options = PdfArtworkOptions(drawing_regions=(PdfRegion("Area", 1, 10, 10, 90, 90),))
        inside = await self.status(options, "passed", trace=trace(image(matrix="0 70 -70 0 85 15")))
        self.assertEqual(
            inside.message,
            "All measured images and filled shapes (1) lie inside the allowed drawing regions.",
        )
        bad = await self.status(options, "failed", trace=trace(image(matrix="80 0 0 80 5 10")))
        self.assertEqual(bad.details["violations"][0]["bounds_pt"], (5, 10, 85, 90))
        self.assertEqual(
            bad.message,
            "Drawings outside the allowed drawing regions: 1; the first is an image on page 1.",
        )
        curve = (
            '<fill_path transform="1 0 0 1 0 0"><moveto x="10" y="10"/>'
            '<curveto x1="20" y1="5" x2="30" y2="40" x3="50" y3="50"/></fill_path>'
        )
        await self.status(options, "inconclusive", trace=trace(curve))
        await self.status(options, "inconclusive", missing="mutool")

    async def test_clipping_tracks_rectangles_and_restores_nested_clip_state(self) -> None:
        options = PdfArtworkOptions(check_clipping=True)
        await self.status(options, "passed", trace=trace(image()))
        clipped = rectangle(tag="clip_path", right=50) + image() + "<pop_clip/>" + image()
        finding = await self.status(options, "failed", trace=trace(clipped))
        self.assertEqual(finding.details["violation_count"], 1)
        self.assertEqual(finding.details["measured_operations"], 2)
        self.assertEqual(finding.details["violations"][0]["clip_bounds_pt"], (10, 10, 50, 90))
        self.assertEqual(
            finding.message,
            "Images and shapes extending past a clip rectangle or the page: 1; the first is "
            "an image on page 1. Check that the cropping is intended.",
        )
        await self.status(
            options, "inconclusive", trace=trace("<clip_text/>" + image() + "<pop_clip/>")
        )
        await self.status(options, "inconclusive", trace=trace("<pop_clip/>" + image()))

    async def test_composition_distinguishes_raster_vector_mixed_and_unknown(self) -> None:
        options = PdfArtworkOptions(classify_artwork=True)
        for content, expected in (
            (rectangle(), "vector"),
            (image(), "raster"),
            (rectangle() + image(), "mixed"),
        ):
            finding = await self.status(options, "passed", trace=trace(content))
            self.assertEqual(finding.details["classification"], expected)
        await self.status(options, "inconclusive", trace=trace())
        await self.status(options, "inconclusive", trace=trace(image() + "<future_paint/>"))
        await self.status(options, "inconclusive", trace=trace("<group>" + image() + "</group>"))
        await self.status(options, "inconclusive", missing="mutool")

    async def test_category_dpi_measures_rotation_shear_both_axes_and_repeated_placements(
        self,
    ) -> None:
        options = PdfArtworkOptions(
            artwork_category="line drawing", category_min_dpi=(("line drawing", 200),)
        )
        good = await self.status(options, "passed", trace=trace(image(matrix="0 100 -100 0 100 0")))
        self.assertIn("All 1 image placements are at or above the 200 dpi minimum", good.message)
        bad = await self.status(
            options, "failed", trace=trace(image(height=100) + image(height=100))
        )
        self.assertEqual(bad.details["violation_count"], 2)
        self.assertEqual(bad.details["violations"][0]["x_dpi"], 216)
        self.assertEqual(bad.details["violations"][0]["y_dpi"], 72)
        self.assertEqual(bad.details["violations"][0]["effective_dpi"], 72)
        self.assertEqual(
            bad.message,
            "Image placements below the 200 dpi minimum for 'line drawing': 2 of 2; "
            "the lowest is 72 dpi on page 1.",
        )
        sheared = replace(options, category_min_dpi=(("line drawing", 60),))
        finding = await self.status(
            sheared, "failed", trace=trace(image(matrix="100 0 100 100 0 0", width=100, height=100))
        )
        self.assertLess(finding.details["violations"][0]["minimum_direction_dpi"], 50)
        await self.status(options, "inconclusive", trace=trace(image(matrix="0 0 0 100 0 0")))
        await self.status(options, "passed", trace=trace(rectangle()))

    async def test_type3_programs_distinguish_vector_bitmap_nested_forms_and_opaque_data(
        self,
    ) -> None:
        options = PdfArtworkOptions(classify_type3_glyphs=True, forbid_bitmap_type3_glyphs=True)
        vector = await self.status(options, "passed")
        self.assertEqual(vector.details["inventory"][0]["classification"], "vector")
        bitmap = await self.status(
            options, "failed", objects=type3_objects(b"500 0 d0 /Picture Do")
        )
        self.assertEqual(bitmap.details["violations"][0]["classification"], "bitmap")
        await self.status(options, "failed", objects=type3_objects(b"/Picture Do", nested=True))
        mixed = await self.status(
            options, "failed", objects=type3_objects(b"0 0 10 10 re f /Picture Do")
        )
        self.assertEqual(mixed.details["violations"][0]["classification"], "mixed")
        await self.status(options, "inconclusive", objects=type3_objects(filtered=True))
        await self.status(options, "inconclusive", missing="qpdf")

    async def test_type3_operator_looking_comments_strings_and_inline_tails_do_not_false_pass(
        self,
    ) -> None:
        options = PdfArtworkOptions(forbid_bitmap_type3_glyphs=True)
        await self.status(
            options, "passed", objects=type3_objects(b"% /Picture Do\n0 0 10 10 re f")
        )
        await self.status(options, "inconclusive", objects=type3_objects(b"(/Picture Do) Tj"))
        inline = await self.status(
            options, "failed", objects=type3_objects(b"BI /W 1 /H 1 ID x EI")
        )
        self.assertTrue(inline.details["incomplete"])
        await self.status(options, "inconclusive", objects=type3_objects(b"/Unknown Do"))
        await self.status(
            options, "inconclusive", objects=type3_objects(b"/Pattern cs /P scn 0 0 10 10 re f")
        )

    async def test_affine_stroke_width_uses_path_normal_not_largest_axis(self) -> None:
        options = PdfArtworkOptions(min_stroke_width_pt=0.4)
        await self.status(
            options,
            "passed",
            trace=trace(stroke(matrix="2 0 0 .5 0 0", width=0.5, first=(10, 10), second=(10, 90))),
        )
        bad = await self.status(
            options, "failed", trace=trace(stroke(matrix="2 0 0 .5 0 0", width=0.5))
        )
        self.assertEqual(bad.details["violations"][0]["minimum_effective_width_pt"], 0.25)
        self.assertEqual(
            bad.message,
            "Strokes thinner than 0.4 pt after placement: 1; the thinnest is 0.25 pt on page 1.",
        )
        await self.status(options, "passed", trace=trace(stroke(matrix="0 2 -2 0 0 0", width=0.2)))
        await self.status(options, "inconclusive", trace=trace(stroke(width=0)))
        curve = (
            '<stroke_path linewidth="1" transform="2 0 0 .5 0 0">'
            '<curveto x1="0" y1="0" x2="10" y2="0" x3="20" y3="20"/></stroke_path>'
        )
        await self.status(options, "inconclusive", trace=trace(curve))

    async def test_raster_region_coverage_uses_union_and_keeps_text_ambiguity(self) -> None:
        options = PdfArtworkOptions(
            raster_regions=(region("Table 1"),), min_raster_region_coverage=0.8
        )
        candidate = await self.status(options, "failed", trace=trace(image()))
        self.assertEqual(candidate.details["raster_coverage"], 1)
        self.assertIn("Table 1 on page 1 is 100.0% covered by raster images", candidate.message)
        self.assertIn("scan or a raster table", candidate.message)
        duplicate = image(matrix="60 0 0 100 0 0") * 2
        valid = await self.status(options, "passed", trace=trace(duplicate))
        self.assertAlmostEqual(valid.details["raster_coverage"], 0.6)
        self.assertEqual(valid.message, "Raster images cover 60.0% of Table 1 on page 1.")
        text = '<fill_text transform="1 0 0 1 0 0"><span><g x="10" y="10"/></span></fill_text>'
        await self.status(options, "inconclusive", trace=trace(image() + text))
        await self.status(options, "inconclusive", trace=trace(image(matrix="80 20 0 80 0 0")))
        await self.status(options, "inconclusive", missing="mutool")

    async def test_input_figures_use_whole_page_and_keep_source_attribution(self) -> None:
        findings = await inspect_artwork_inputs(
            (self.pdf,),
            self.work(),
            ArtworkRunner(),
            options=PdfArtworkOptions(classify_artwork=True, max_figure_whitespace_ratio=0.5),
        )
        self.assertEqual(len(findings), 2)
        self.assertTrue(all(item.path == str(self.pdf) for item in findings))
        self.assertTrue(all(item.status == "passed" for item in findings))

    async def test_traced_figure_color_families_against_required_space(self) -> None:
        rgb = PdfArtworkOptions(required_color_space="rgb")
        cmyk = PdfArtworkOptions(required_color_space="cmyk")
        mixed = trace(colored("DeviceRGB") + colored("DeviceGray") + colored("ICCBased(RGB)"))
        passed = await self.status(rgb, "passed", trace=mixed)
        self.assertEqual(passed.code, "PDF313")
        self.assertEqual(passed.details["color_spaces"]["DeviceRGB"], 1)
        self.assertEqual(passed.message, "All traced colour spaces are RGB or grayscale.")
        failed = await self.status(cmyk, "failed", trace=mixed)
        self.assertEqual(failed.severity, "error")
        self.assertEqual(
            failed.message,
            "Painted operations outside the required CMYK family: 2; for example DeviceRGB "
            "on page 1.",
        )
        self.assertEqual(
            [item["color_space"] for item in failed.details["violations"]],
            ["DeviceRGB", "ICCBased(RGB)"],
        )
        await self.status(
            cmyk, "passed", trace=trace(colored("DeviceCMYK") + colored("DeviceGray"))
        )
        # Separation/unnamed operations and an empty trace are never a pass.
        for content in (colored("Separation(Gold)"), colored(None), ""):
            with self.subTest(content=content):
                await self.status(cmyk, "inconclusive", trace=trace(content))
        findings = await inspect_artwork_inputs(
            (self.pdf,), self.work(), ArtworkRunner(trace=mixed), options=cmyk
        )
        self.assertEqual(
            [(item.status, item.path) for item in findings], [("failed", str(self.pdf))]
        )
        with self.assertRaises(PreparationError):
            PdfArtworkOptions(required_color_space="srgb")

    async def test_input_figures_without_regions_are_measured_whole_but_manuscripts_are_not(
        self,
    ) -> None:
        options = PdfArtworkOptions(max_figure_whitespace_ratio=0.5)
        manuscript = await self.check(options)
        self.assertEqual([item.status for item in manuscript], ["inconclusive"])
        figures = await inspect_artwork_inputs(
            (self.pdf,), self.work(), ArtworkRunner(), options=options
        )
        self.assertEqual([item.status for item in figures], ["passed"])
        self.assertIn("Input page 1 on page 1", figures[0].message)


if __name__ == "__main__":
    unittest.main()
