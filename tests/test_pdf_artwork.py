from __future__ import annotations

import asyncio
import math
import random
import struct
import tempfile
import time
import unittest
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any
from unittest.mock import patch

from latexprep.cancellation import cancellation_point as owned_cancellation_point
from latexprep.command_runner import CommandResult
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
    PdfContrastSample,
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
from latexprep.redaction import redact_data
from latexprep.resource_budget import ResourceBudget
from tests.support import (
    ArtworkRunner,
    contrast_sample,
    raster,
    rectangle,
    rgb_raster,
    trace,
    type3_objects,
)


def region(name: str = "Figure", *, left: float = 0, right: float = 100) -> PdfRegion:
    return PdfRegion(name, 1, left, 0, right, 100)


def image(*, matrix: str = "100 0 0 100 0 0", width: int = 300, height: int = 300) -> str:
    return f'<fill_image transform="{matrix}" width="{width}" height="{height}" alpha="1"/>'


def stroke(
    *,
    first: tuple[float, float] = (10, 10),
    second: tuple[float, float] = (90, 10),
    matrix: str = "1 0 0 1 0 0",
    width: float = 1,
) -> str:
    return (
        f'<stroke_path linewidth="{width}" transform="{matrix}">'
        f'<moveto x="{first[0]}" y="{first[1]}"/>'
        f'<lineto x="{second[0]}" y="{second[1]}"/></stroke_path>'
    )


def colored(space: str | None) -> str:
    attribute = f' colorspace="{space}"' if space is not None else ""
    return f'<fill_path{attribute} color="0" transform="1 0 0 1 0 0"></fill_path>'


class PdfArtworkTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pdf = self.root / "figure.pdf"
        self.pdf.write_bytes(b"%PDF-fixture")
        self.sequence = 0

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    def work(self) -> Path:
        self.sequence += 1
        return self.root / f"artwork-{self.sequence}"

    async def check(
        self,
        options: PdfArtworkOptions,
        runner: ArtworkRunner | None = None,
        limits: ArtworkLimits | None = None,
    ):
        return await inspect_pdf_artwork(
            self.pdf, self.work(), runner or ArtworkRunner(), options=options, limits=limits
        )

    async def status_for(self, options: PdfArtworkOptions, expected: str, runner: ArtworkRunner):
        findings = await self.check(options, runner)
        self.assertEqual(len(findings), 1, findings)
        self.assertEqual(findings[0].status, expected, findings)
        return findings[0]

    async def status(self, options: PdfArtworkOptions, expected: str, **values: object):
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

    async def test_grayscale_preview_retains_validated_artifact_and_rejects_partial_output(
        self,
    ) -> None:
        options = PdfArtworkOptions(grayscale_preview=True)
        finding = await self.status(options, "passed")
        preview = Path(finding.details["artifacts"][0])
        self.assertTrue(preview.is_file())
        png = preview.read_bytes()
        self.assertEqual(png[:8], b"\x89PNG\r\n\x1a\n")
        self.assertEqual(struct.unpack(">II", png[16:24]), (100, 100))
        # Decode the PNG's compressed rows independently and compare every pixel.
        offset = 33
        length = struct.unpack(">I", png[offset : offset + 4])[0]
        self.assertEqual(png[offset + 4 : offset + 8], b"IDAT")
        raw = zlib.decompress(png[offset + 8 : offset + 8 + length])
        self.assertEqual(len(raw), 10100)
        self.assertEqual(
            b"".join(raw[i + 1 : i + 101] for i in range(0, len(raw), 101)),
            raster().split(b"\n", 3)[3],
        )
        self.assertFalse(
            (preview.parent.parent / "artwork-render-1" / "command" / "page.pgm").exists()
        )
        await self.status(options, "inconclusive", raster=b"P5\n1 1\n255\n\0")
        await self.status(options, "inconclusive", missing="pdftoppm")
        self.assertEqual(self.pdf.read_bytes(), b"%PDF-fixture")

    async def test_whole_document_renders_keep_the_rendering_page_cap(self) -> None:
        class LongDocumentRunner(ArtworkRunner):
            async def run(self, argv: list[str], *args: Any, **kwargs: Any) -> CommandResult:
                result = await super().run(argv, *args, **kwargs)
                if argv[0] == "pdfinfo" and "-box" not in argv and argv[-1] != "-v":
                    return replace(result, stdout="Pages: 301\n")
                return result

        pages = "".join(
            f"Page {page} size: 100 x 100 pts\nPage {page} rot: 0\n"
            f"Page {page} MediaBox: 0 0 100 100\nPage {page} CropBox: 0 0 100 100\n"
            for page in range(1, 302)
        )
        runner = LongDocumentRunner(geometry=pages)
        finding = await self.status_for(
            PdfArtworkOptions(grayscale_preview=True), "inconclusive", runner
        )
        self.assertEqual(finding.rule, "pdf.grayscale_preview")
        self.assertIn("supports at most 300 pages", finding.message)
        self.assertFalse(any(argv[0] == "pdftoppm" for argv, _, _ in runner.calls))
        # Explicit regions render only their own pages, so a long document stays measurable.
        await self.status_for(
            PdfArtworkOptions(figure_regions=(region(),), max_figure_whitespace_ratio=0.5),
            "passed",
            LongDocumentRunner(geometry=pages),
        )

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

    async def test_rendered_sample_contrast_uses_actual_foreground_and_background_pixels(
        self,
    ) -> None:
        options = PdfArtworkOptions(contrast_samples=(contrast_sample(),))
        runner = ArtworkRunner()
        result = await self.check(options, runner)
        self.assertEqual(result[0].status, "passed")
        self.assertEqual(result[0].details["contrast_ratio_min"], 21)
        self.assertEqual(result[0].details["contrast_ratio_max"], 21)
        self.assertEqual(
            result[0].message,
            "Sample 'Label/background' on page 1 has a contrast ratio of at least 21.00:1 "
            "(required 4.5:1).",
        )
        self.assertFalse(any(argv[0] == "mutool" for argv, _, _ in runner.calls))
        light = await self.status(options, "failed", rgb=rgb_raster(foreground=(180, 180, 180)))
        self.assertIn(
            "Sample 'Label/background' on page 1 has a contrast ratio of at most", light.message
        )
        self.assertIn("below the required 4.5:1", light.message)
        dark_background = await self.status(
            options, "failed", rgb=rgb_raster(background=(20, 20, 20))
        )
        self.assertEqual(
            dark_background.details["background_rgb_range"], ((20, 20, 20), (20, 20, 20))
        )
        # Rendered gray can result from compositing; source black is not substituted.
        await self.status(options, "failed", rgb=rgb_raster(foreground=(128, 128, 128)))
        await self.status(
            PdfArtworkOptions(contrast_samples=(contrast_sample(minimum=21),)), "passed"
        )
        self.assertFalse(any(self.root.rglob("page.ppm")))

    async def test_rendered_sample_contrast_requires_homogeneous_patches_or_explicit_tolerance(
        self,
    ) -> None:
        options = PdfArtworkOptions(contrast_samples=(contrast_sample(),))
        varied = rgb_raster(alternate=(1, 1, 1))
        uneven = await self.status(options, "inconclusive", rgb=varied)
        self.assertIn(
            "has uneven colours (channel values vary by up to 1; the allowed spread is 0)",
            uneven.message,
        )
        self.assertIn("max_channel_spread", uneven.message)
        tolerated = PdfArtworkOptions(contrast_samples=(contrast_sample(tolerance=1),))
        await self.status(tolerated, "passed", rgb=varied)
        crossing = PdfArtworkOptions(contrast_samples=(contrast_sample(tolerance=30),))
        uncertain = await self.status(
            crossing,
            "inconclusive",
            rgb=rgb_raster(foreground=(100, 100, 100), alternate=(130, 130, 130)),
        )
        self.assertLess(uncertain.details["contrast_ratio_min"], 4.5)
        self.assertGreater(uncertain.details["contrast_ratio_max"], 4.5)
        self.assertEqual(uncertain.details["violation_count"], 0)
        self.assertIn("cannot be confirmed or ruled out", uncertain.message)

    async def test_rendered_sample_contrast_rejects_incomplete_render_and_unsupported_regions(
        self,
    ) -> None:
        options = PdfArtworkOptions(contrast_samples=(contrast_sample(),))
        await self.status(options, "inconclusive", missing="pdftoppm")
        await self.status(options, "inconclusive", rgb=b"P6\n100 100\n255\ntruncated")
        tiny = replace(contrast_sample(), foreground=PdfRegion("Tiny", 1, 10, 10, 10.1, 10.1))
        await self.status(replace(options, contrast_samples=(tiny,)), "inconclusive")
        outside = replace(contrast_sample(), background=PdfRegion("Outside", 1, 80, 80, 110, 110))
        await self.status(replace(options, contrast_samples=(outside,)), "inconclusive")
        runner = ArtworkRunner()
        runner.geometry = runner.geometry.replace("rot: 0", "rot: 90")
        result = await self.check(options, runner)
        self.assertEqual(result[0].status, "inconclusive")
        self.assertFalse(any(argv[0] == "pdftoppm" for argv, _, _ in runner.calls))
        for minimum in (0, 22, float("nan")):
            with self.assertRaises(PreparationError):
                contrast_sample(minimum=minimum)
        with self.assertRaises(PreparationError):
            contrast_sample(tolerance=256)
        self.assertFalse(any(self.root.rglob("page.ppm")))

    async def test_rendered_region_work_caps_cover_all_regions_and_contrast_pairs(self) -> None:
        options = PdfArtworkOptions(
            figure_regions=tuple(region(f"Figure {index}") for index in range(3)),
            max_figure_whitespace_ratio=0.5,
        )
        findings = await self.check(options, limits=ArtworkLimits(max_pixel_visits_per_page=20_000))
        self.assertEqual([item.status for item in findings], ["passed", "passed", "inconclusive"])
        self.assertIn("bounded pixel sampling", findings[-1].message)
        samples = tuple(replace(contrast_sample(), name=f"Sample {index}") for index in range(3))
        findings = await self.check(
            PdfArtworkOptions(contrast_samples=samples),
            limits=ArtworkLimits(max_pixel_visits_per_page=400),
        )
        self.assertEqual([item.status for item in findings], ["passed", "passed", "inconclusive"])
        self.assertIn("bounded pixel sampling", findings[-1].message)

    async def test_pixel_workers_yield_to_timers_and_cancellation_drains_the_owned_lease(
        self,
    ) -> None:
        large = PdfRegion("Large", 1, 0, 0, 500, 500)
        selections = (
            PdfArtworkOptions(
                figure_regions=tuple(replace(large, name=f"Figure {i}") for i in range(50)),
                max_figure_whitespace_ratio=0.5,
            ),
            PdfArtworkOptions(
                contrast_samples=tuple(
                    PdfContrastSample(f"Sample {i}", large, large, 1) for i in range(50)
                )
            ),
        )
        for options in selections:
            budget = ResourceBudget(1, 256)
            runner = ArtworkRunner()
            runner.geometry = runner.geometry.replace("100", "500")
            runner.raster = b"P5\n500 500\n255\n" + bytes(250_000)
            runner.rgb = b"P6\n500 500\n255\n" + bytes(750_000)
            runner.budget = runner.expected_budget = budget
            started = asyncio.Event()
            loop = asyncio.get_running_loop()
            signaled = False

            def progress(
                *, loop: asyncio.AbstractEventLoop = loop, started: asyncio.Event = started
            ) -> None:
                nonlocal signaled
                if not signaled:
                    signaled = True
                    loop.call_soon_threadsafe(started.set)
                # Vectorized pixel scans finish within milliseconds, so each checkpoint
                # takes a moment to stand in for a substantial job in the worker thread.
                time.sleep(0.001)
                owned_cancellation_point()

            with (
                patch("latexprep.pdf_artwork_rendered.cancellation_point", side_effect=progress),
                patch("latexprep.raster_pixels.cancellation_point", side_effect=progress),
            ):
                task = asyncio.create_task(self.check(options, runner))
                await asyncio.wait_for(started.wait(), 2)
                # The timer must run before this substantial pixel job finishes.
                await asyncio.sleep(0.01)
                self.assertFalse(
                    task.done(), "Pixel scanning blocked the event loop until completion"
                )
                self.assertEqual(budget.statistics()["active_cpu"], 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await asyncio.wait_for(task, 2)
            self.assertEqual(budget.statistics()["active_cpu"], 0)
            self.assertFalse(any(self.root.rglob("page.pgm")))
            self.assertFalse(any(self.root.rglob("page.ppm")))

    async def test_trace_geometry_runs_off_the_event_loop_and_stops_on_cancellation(self) -> None:
        regions = tuple(PdfRegion(f"Table {index}", 1, 0, 0, 100, 100) for index in range(300))
        options = PdfArtworkOptions(raster_regions=regions, min_raster_region_coverage=0.5)
        budget = ResourceBudget(1, 256)
        runner = ArtworkRunner(trace=trace(image()))
        runner.budget = runner.expected_budget = budget
        started = asyncio.Event()
        loop = asyncio.get_running_loop()
        signaled = False

        def progress() -> None:
            nonlocal signaled
            if not signaled:
                signaled = True
                loop.call_soon_threadsafe(started.set)
            time.sleep(0.001)
            owned_cancellation_point()

        with patch("latexprep.pdf_artwork_drawings.cancellation_point", side_effect=progress):
            task = asyncio.create_task(self.check(options, runner))
            await asyncio.wait_for(started.wait(), 2)
            # The timer must run before the 300 regions are measured.
            await asyncio.sleep(0.01)
            self.assertFalse(task.done(), "Trace measurements blocked the event loop")
            self.assertEqual(budget.statistics()["active_cpu"], 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, 2)
        self.assertEqual(budget.statistics()["active_cpu"], 0)

    async def test_adapters_use_shared_budget_isolated_commands_and_frozen_inputs(self) -> None:
        budget = ResourceBudget(2, 512)
        runner = ArtworkRunner()
        runner.expected_budget = runner.budget = budget
        options = PdfArtworkOptions(
            classify_artwork=True,
            classify_type3_glyphs=True,
            grayscale_preview=True,
            contrast_samples=(contrast_sample(),),
        )
        result = await self.check(options, runner)
        self.assertTrue(all(item.status == "passed" for item in result))
        actual = [call for call in runner.calls if call[0][-1] not in {"-v", "--version"}]
        self.assertEqual(len(actual), len({call[1] for call in actual}))
        self.assertTrue(all(len(call[2]) == 1 and call[2][0] != self.pdf for call in actual))
        self.assertIsNone(budget.current_lease)

    async def test_failures_and_cancellation_do_not_report_success_or_leak_leases(self) -> None:
        for failure in ("timeout", "output", "resource"):
            await self.status(
                PdfArtworkOptions(classify_artwork=True), "inconclusive", failure=failure
            )
        budget = ResourceBudget(1, 256)
        runner = ArtworkRunner(block=asyncio.Event())
        runner.budget = runner.expected_budget = budget
        task = asyncio.create_task(self.check(PdfArtworkOptions(classify_artwork=True), runner))
        await runner.started.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        async with budget.lease("after cancellation", memory_mb=128):
            self.assertIsNotNone(budget.current_lease)

    async def test_unselected_checks_do_not_run_tools_and_options_reject_implicit_policies(
        self,
    ) -> None:
        runner = ArtworkRunner()
        self.assertEqual(await self.check(PdfArtworkOptions(), runner), [])
        self.assertEqual(runner.calls, [])
        invalid: tuple[dict[str, Any], ...] = (
            {"detect_printer_marks": True},
            {"category_min_dpi": (("photo", 300),)},
            {"raster_regions": (region(),)},
            {"raster_dpi": True},
            {"max_figure_whitespace_ratio": 2},
            {"min_stroke_width_pt": float("nan")},
        )
        for values in invalid:
            with self.assertRaises(PreparationError):
                PdfArtworkOptions(**values)

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

    async def test_trace_is_parsed_once_for_drawing_and_color_space_measurements(self) -> None:
        options = PdfArtworkOptions(classify_artwork=True, required_color_space="rgb")
        runner = ArtworkRunner(trace=trace(colored("DeviceRGB")))
        with patch("latexprep.pdf_artwork.parse_mupdf_trace", wraps=parse_mupdf_trace) as parsed:
            result = await self.check(options, runner)
        self.assertEqual([item.status for item in result], ["passed", "passed"])
        self.assertEqual(parsed.call_count, 1)
        traces = [argv for argv, _, _ in runner.calls if argv[0] == "mutool"]
        self.assertEqual(len([argv for argv in traces if argv[-1] not in {"-v", "--version"}]), 1)

    async def test_unreadable_trace_makes_every_trace_rule_unavailable_with_the_cause(self) -> None:
        options = PdfArtworkOptions(
            classify_artwork=True,
            check_clipping=True,
            artwork_category="photo",
            category_min_dpi=(("photo", 300),),
            required_color_space="rgb",
        )
        result = await self.check(options, ArtworkRunner(trace="<document><page number='2'/>"))
        self.assertEqual(
            [item.rule for item in result],
            [
                ArtworkRule.CLIPPING_CANDIDATES,
                ArtworkRule.ARTWORK_COMPOSITION,
                ArtworkRule.CATEGORY_RESOLUTION,
                ArtworkRule.COLOR_SPACE,
            ],
        )
        self.assertTrue(all(item.status == "inconclusive" for item in result))
        severities = {item.rule: item.severity for item in result}
        self.assertEqual(severities[ArtworkRule.CATEGORY_RESOLUTION], "error")
        self.assertEqual(severities[ArtworkRule.CLIPPING_CANDIDATES], "warning")

    async def test_findings_survive_report_redaction_with_plain_report_values(self) -> None:
        marks = stroke(first=(5, 20), second=(15, 20)) + stroke(first=(20, 5), second=(20, 15))
        options = PdfArtworkOptions(
            detect_printer_marks=True,
            trim_regions=(PdfRegion("Trim", 1, 20, 20, 80, 80),),
            printer_mark_max_length_pt=10,
            drawing_regions=(PdfRegion("Area", 1, 10, 10, 90, 90),),
            check_clipping=True,
            min_stroke_width_pt=5,
            raster_regions=(region("T"),),
            min_raster_region_coverage=0.5,
        )
        content = marks + image(matrix="80 0 0 80 5 10") + rectangle(tag="clip_path", right=50)
        findings = await self.check(options, ArtworkRunner(trace=trace(content + image())))
        data = redact_data([finding.details for finding in findings])
        self.assertNotIn("unsupported value", str(data))
        bounds = next(f for f in findings if f.rule == ArtworkRule.DRAWING_BOUNDS)
        self.assertIs(type(bounds.details["violations"][0]["bounds_pt"]), tuple)
        self.assertIs(type(bounds.details["violations"][0]["bounds_pt"][0]), float)

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

    async def test_missing_trace_tool_is_inconclusive(self) -> None:
        options = PdfArtworkOptions(required_color_space="rgb")
        finding = await self.status(options, "inconclusive", missing="mutool")
        self.assertEqual((finding.code, finding.severity), ("PDF313", "error"))
        runner = ArtworkRunner()
        self.assertEqual(await self.check(PdfArtworkOptions(), runner), [])
        self.assertFalse(any(call[0][0] == "mutool" for call in runner.calls))


class ArtworkRuleCatalogueTests(unittest.TestCase):
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
    def parsed(self, content: str, *, box: str = "0 0 100 100"):
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
    def drawings(self, content: str):
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


if __name__ == "__main__":
    unittest.main()
