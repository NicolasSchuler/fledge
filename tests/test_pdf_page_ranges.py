"""Page ranges per ``pdftoppm`` process: planning, files, fallback and unchanged findings.

A comparison renders several pages per process. These tests cover how ranges are planned,
where a render's files are found, that a failing range is rendered again page by page so the
failure names its page, and that the findings do not depend on the range size, the budget or
the number of jobs.
"""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from latexprep.pdf_comparison import compare_pdfs
from latexprep.pdf_page_renders import (
    CHUNK_MAX_PAGES,
    plan_render_ranges,
    range_page_limit,
)
from latexprep.pdf_tools import DOCUMENT, render_page_argv, render_pages_argv, rendered_page_files
from latexprep.resource_budget import ResourceBudget
from latexprep.workflow_options import ComparisonOptions
from tests.fakes import Reply, ToolCall
from tests.page_render_fakes import RangeRunner
from tests.pdf_fakes import comparison_ppm


class RangePlanningTests(unittest.TestCase):
    """Which pages share a process."""

    def test_consecutive_pages_are_cut_at_the_page_limit(self) -> None:
        ranges = plan_render_ranges(range(1, 13), lambda _page: 3, max_pages=5)
        self.assertEqual(ranges, [(1, 5), (6, 10), (11, 12)])

    def test_a_gap_in_the_page_numbers_ends_a_range(self) -> None:
        ranges = plan_render_ranges([1, 2, 4, 5, 9], lambda _page: 3, max_pages=8)
        self.assertEqual(ranges, [(1, 2), (4, 5), (9, 9)])

    def test_raster_bytes_bound_a_range_but_a_large_page_still_gets_its_own(self) -> None:
        ranges = plan_render_ranges(
            range(1, 6), lambda page: 10 if page != 3 else 100, max_pages=8, max_bytes=25
        )
        self.assertEqual(ranges, [(1, 2), (3, 3), (4, 5)])

    def test_no_pages_need_no_range(self) -> None:
        self.assertEqual(plan_render_ranges([], lambda _page: 1, max_pages=8), [])

    def test_a_document_the_renderers_can_take_a_page_each_stays_one_page_per_process(
        self,
    ) -> None:
        self.assertEqual(range_page_limit(6, 4), 1)
        self.assertEqual(range_page_limit(8, 4), 1)
        self.assertEqual(range_page_limit(1, 1), 1)

    def test_longer_documents_are_cut_into_about_two_ranges_per_renderer(self) -> None:
        self.assertEqual(range_page_limit(60, 4), 8)
        self.assertEqual(range_page_limit(12, 2), 3)
        self.assertEqual(range_page_limit(12, 1), 6)
        self.assertEqual(range_page_limit(1000, 1), CHUNK_MAX_PAGES)


class RangeFilesTests(unittest.TestCase):
    """The command that renders a range and the files it leaves."""

    def test_the_range_command_is_the_single_page_command_for_several_pages(self) -> None:
        self.assertEqual(
            render_page_argv(3, 144, "rgb"),
            [
                "pdftoppm",
                "-f",
                "3",
                "-l",
                "3",
                "-cropbox",
                "-singlefile",
                "-r",
                "144",
                DOCUMENT,
                "page",
            ],
        )
        self.assertEqual(
            render_pages_argv(3, 9, 144, "rgb"),
            ["pdftoppm", "-f", "3", "-l", "9", "-cropbox", "-r", "144", DOCUMENT, "page"],
        )
        self.assertIn("-png", render_pages_argv(1, 2, 144, "png"))

    def test_page_files_are_found_by_number_whatever_the_digit_count(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            for number in (1, 2, 7, 12):
                (directory / f"page-{number:03d}.ppm").write_bytes(b"x")
            (directory / "page-09.pgm").write_bytes(b"x")
            (directory / "other-03.ppm").write_bytes(b"x")
            (directory / "page-300.ppm").write_bytes(b"x")
            found = rendered_page_files(directory, "rgb", first=1, last=12)
        self.assertEqual(sorted(found), [1, 2, 7, 12])

    def test_a_directory_that_cannot_be_read_has_no_pages(self) -> None:
        self.assertEqual(
            rendered_page_files(Path("/nonexistent-fledge"), "rgb", first=1, last=2), {}
        )

    def test_a_hostile_file_name_is_ignored_without_cost(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            (directory / f"page-{'1' * 200}.ppm").write_bytes(b"x")
            (directory / ("page-1" + "-1" * 60 + ".ppm")).write_bytes(b"x")
            (directory / "page-2.ppm").write_bytes(b"x")
            found = rendered_page_files(directory, "rgb", first=1, last=12)
        self.assertEqual(sorted(found), [2])

    def test_only_the_first_entries_of_a_directory_are_read(self) -> None:
        with tempfile.TemporaryDirectory() as name:
            directory = Path(name)
            for number in range(1, 400):
                (directory / f"page-{number:03d}.ppm").write_bytes(b"x")
            found = rendered_page_files(directory, "rgb", first=1, last=399)
        self.assertLessEqual(len(found), 256)


class RangeComparisonTests(unittest.IsolatedAsyncioTestCase):
    """A comparison that renders in ranges reports what a page-by-page comparison reports."""

    async def asyncSetUp(self) -> None:
        """Create a left and a right PDF in a temporary directory."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.left, self.right = self.root / "left.pdf", self.root / "right.pdf"
        self.left.write_bytes(b"%PDF-left")
        self.right.write_bytes(b"%PDF-right")

    async def compare(
        self,
        runner: RangeRunner,
        name: str,
        *,
        budget: ResourceBudget | None = None,
        options: ComparisonOptions | None = None,
    ) -> list[dict[str, object]]:
        """Compare the two PDFs and return the findings as plain data."""
        findings = await compare_pdfs(
            self.left, self.right, self.root / name, runner, budget=budget, options=options
        )
        return [asdict(finding) for finding in findings]

    async def test_a_long_document_is_rendered_in_ranges_not_page_by_page(self) -> None:
        runner = RangeRunner(pages=12, changed_pages=(5,))
        findings = await self.compare(runner, "ranges")
        rendering = next(item for item in findings if item["rule"] == "compare.rendering")
        self.assertEqual(rendering["details"]["changed_pages"], [5])
        self.assertEqual(runner.single_renders, [])
        self.assertEqual(
            sorted(runner.range_renders),
            [
                ("left", 1, 6),
                ("left", 7, 12),
                ("right", 1, 6),
                ("right", 7, 12),
            ],
        )
        self.assertFalse((self.root / "ranges/pages-0001-0006").exists())

    async def test_single_and_range_renders_accept_rounded_crop_boxes_and_rotation(self) -> None:
        class CroppedRunner(RangeRunner):
            """Render a rounded, rotated CropBox inside a larger MediaBox."""

            rotation: int
            pixels: tuple[int, int]

            def respond(self, call: ToolCall) -> Reply:
                """Substitute the crop and its raster for the default comparison page."""
                reply = super().respond(call)
                if call.tool == "pdfinfo" and "-box" in call.argv:
                    boxes = "".join(
                        f"Page {page} size: 20 x 30 pts\nPage {page} rot: {self.rotation}\n"
                        f"Page {page} MediaBox: 0 0 612 792\n"
                        f"Page {page} CropBox: 5 10 25 40\n"
                        for page in range(1, self.pages + 1)
                    )
                    return replace(reply, stdout=reply.stdout.splitlines()[0] + "\n" + boxes)
                if reply.files:
                    raster = comparison_ppm(self.pixels)
                    return replace(reply, files=dict.fromkeys(reply.files, raster))
                return reply

        # pdfinfo rounds fractional point sizes; allow one pixel on either axis in either
        # direction. For quarter turns, the raster dimensions must follow the rotated CropBox.
        for pages, rotation, pixels in ((1, 0, (41, 60)), (3, 90, (59, 41)), (3, 270, (60, 41))):
            with self.subTest(pages=pages, rotation=rotation):
                runner = CroppedRunner(pages=pages)
                runner.rotation, runner.pixels = rotation, pixels
                findings = await self.compare(runner, f"cropped-{rotation}")
                statuses = {item["rule"]: item["status"] for item in findings}
                self.assertNotIn("compare.unavailable", statuses)
                self.assertEqual(statuses["compare.rendering"], "passed")
                self.assertEqual(bool(runner.range_renders), pages > 1)

    async def test_findings_are_identical_with_one_page_per_process(self) -> None:
        ranged = await self.compare(RangeRunner(pages=12, changed_pages=(2, 9)), "ranged")
        # A runner that leaves every range without files makes each page fall back to its own
        # process, which is how every page was rendered before ranges.
        paged_runner = RangeRunner(pages=12, changed_pages=(2, 9), omit_pages=tuple(range(1, 13)))
        paged = await self.compare(paged_runner, "paged")
        self.assertEqual(ranged, paged)
        self.assertEqual(len(paged_runner.single_renders), 24)

    async def test_findings_and_their_order_do_not_depend_on_the_jobs(self) -> None:
        results = []
        for index, budget in enumerate(
            (
                None,
                ResourceBudget(1, 2048),
                ResourceBudget(2, 2048, render_jobs=2),
                ResourceBudget(8, 2048, render_jobs=8),
            )
        ):
            runner = RangeRunner(pages=20, changed_pages=(3, 4, 17))
            results.append(await self.compare(runner, f"jobs-{index}", budget=budget))
        for other in results[1:]:
            self.assertEqual(other, results[0])
        rendering = next(item for item in results[0] if item["rule"] == "compare.rendering")
        self.assertEqual(rendering["details"]["changed_pages"], [3, 4, 17])
        self.assertEqual([row["page"] for row in rendering["details"]["measurements"]], [3, 4, 17])

    async def test_a_failing_range_is_rendered_again_page_by_page(self) -> None:
        runner = RangeRunner(pages=6, changed_pages=(4,), fail_ranges=True)
        findings = await self.compare(runner, "failed-range")
        rendering = next(item for item in findings if item["rule"] == "compare.rendering")
        self.assertEqual(rendering["details"]["changed_pages"], [4])
        self.assertEqual(
            sorted(runner.single_renders),
            [(side, page) for side in ("left", "right") for page in range(1, 7)],
        )
        self.assertFalse(any(item["rule"] == "compare.unavailable" for item in findings))

    async def test_a_page_missing_from_a_range_is_rendered_alone(self) -> None:
        runner = RangeRunner(pages=6, omit_pages=(2,))
        findings = await self.compare(runner, "missing-file")
        self.assertEqual(sorted(runner.single_renders), [("left", 2), ("right", 2)])
        self.assertTrue(
            next(item for item in findings if item["rule"] == "compare.rendering")["status"]
            == "passed"
        )

    async def test_a_damaged_page_is_reported_for_that_page_only(self) -> None:
        runner = RangeRunner(pages=6, changed_pages=(5,), fail_ranges=True, fail_pages=(3,))
        findings = await self.compare(runner, "damaged")
        unavailable = next(item for item in findings if item["rule"] == "compare.unavailable")
        self.assertEqual(unavailable["details"]["unavailable_pages"], [3])
        self.assertEqual(unavailable["details"]["completed_pages"], [1, 2, 4, 5, 6])
        self.assertEqual(unavailable["details"]["known_changed_pages"], [5])
        self.assertIn("Page 3 rendering", unavailable["message"])
        self.assertFalse(any(item["rule"] == "compare.rendering" for item in findings))

    async def test_pixel_counts_with_tolerances_are_identical_to_single_page_renders(self) -> None:
        options = ComparisonOptions(1, 0.5)
        ranged = await self.compare(
            RangeRunner(pages=10, changed_pages=(2, 7)), "tol-a", options=options
        )
        paged = await self.compare(
            RangeRunner(pages=10, changed_pages=(2, 7), omit_pages=tuple(range(1, 11))),
            "tol-b",
            options=options,
        )
        self.assertEqual(ranged, paged)
        rendering = next(item for item in ranged if item["rule"] == "compare.rendering")
        measured = rendering["details"]["measurements"]
        self.assertEqual([row["page"] for row in measured], [2, 7])
        self.assertTrue(all(row["changed_pixels"] == 1 for row in measured))
