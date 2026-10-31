"""How the artwork adapter uses the budget, the tools and the trace, and how it fails."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from latexprep.errors import PreparationError
from latexprep.pdf_artwork import inspect_pdf_artwork
from latexprep.pdf_artwork_options import (
    ArtworkLimits,
    ArtworkRule,
    PdfArtworkOptions,
)
from latexprep.pdf_check_options import PdfRegion
from latexprep.pdf_tool_output import parse_mupdf_trace
from latexprep.redaction import redact_data
from latexprep.resource_budget import ResourceBudget
from tests.pdf_fakes import (
    ArtworkRunner,
    contrast_sample,
    rectangle,
    trace,
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


class ArtworkAdapterTests(unittest.IsolatedAsyncioTestCase):
    """How :func:`inspect_pdf_artwork` uses the budget, tools and trace, and how it fails."""

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

    async def test_missing_trace_tool_is_inconclusive(self) -> None:
        options = PdfArtworkOptions(required_color_space="rgb")
        finding = await self.status(options, "inconclusive", missing="mutool")
        self.assertEqual((finding.code, finding.severity), ("PDF313", "error"))
        runner = ArtworkRunner()
        self.assertEqual(await self.check(PdfArtworkOptions(), runner), [])
        self.assertFalse(any(call[0][0] == "mutool" for call in runner.calls))


if __name__ == "__main__":
    unittest.main()
