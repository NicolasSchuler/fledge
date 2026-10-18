"""Artwork checks that render pages: previews, render caps and the contrast of rendered samples."""

from __future__ import annotations

import asyncio
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
from latexprep.pdf_artwork import inspect_pdf_artwork
from latexprep.pdf_artwork_options import (
    ArtworkLimits,
    PdfArtworkOptions,
    PdfContrastSample,
)
from latexprep.pdf_check_options import PdfRegion
from latexprep.resource_budget import ResourceBudget
from tests.pdf_fakes import (
    ArtworkRunner,
    contrast_sample,
    raster,
    rgb_raster,
    trace,
)


def region(name: str = "Figure", *, left: float = 0, right: float = 100) -> PdfRegion:
    return PdfRegion(name, 1, left, 0, right, 100)


def image(*, matrix: str = "100 0 0 100 0 0", width: int = 300, height: int = 300) -> str:
    return f'<fill_image transform="{matrix}" width="{width}" height="{height}" alpha="1"/>'


class RenderedArtworkTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
