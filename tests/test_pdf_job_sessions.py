"""The PDF sessions, measurements and page digests that the stages of one job share.

Within a job scope a PDF is frozen once, a read-only measurement runs once per PDF and a page
whose raster was digested is not rendered again, however many inspections and comparisons look
at the PDF. Outside a scope nothing is shared. The tool probes started early are remembered by
the runner and never repeated.
"""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import asdict, replace
from pathlib import Path

from latexprep.errors import PreparationError
from latexprep.pdf_check_pipeline import PdfCheckSettings, inspect_standalone_pdf
from latexprep.pdf_comparison import compare_pdfs
from latexprep.pdf_inspection import inspect_pdf
from latexprep.pdf_job_sessions import job_pdf_sessions, pdf_job_scope
from latexprep.resource_budget import ResourceBudget
from latexprep.workflow_options import ComparisonOptions
from tests.fakes import Reply, ToolCall
from tests.page_render_fakes import RangeRunner
from tests.pdf_fakes import comparison_ppm

PAGES = 12


class ProbeRecordingRunner(RangeRunner):
    """A range runner that notes how many version probes run at the same moment.

    Attributes:
        active: The number of probes running now.
        peak: The largest number of probes that ran at once.
    """

    def __init__(self) -> None:
        super().__init__(pages=PAGES)
        self.active = self.peak = 0

    async def tool_version(self, argv, workspace):
        """Probe as the base does, after a pause in which another probe can start."""
        self.active += 1
        self.peak = max(self.peak, self.active)
        try:
            await asyncio.sleep(0.01)
            return await super().tool_version(argv, workspace)
        finally:
            self.active -= 1


class JobSessionTests(unittest.IsolatedAsyncioTestCase):
    """What a job's PDFs share, and what stays separate."""

    async def asyncSetUp(self) -> None:
        """Write three PDFs: ``plain.pdf`` on the left side, and two on the right side.

        The fake renders a PDF whose bytes contain "right" as the right side, so ``first`` and
        ``second`` render black wherever the runner says a page changed, and ``plain`` white.
        """
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.plain = self.root / "plain.pdf"
        self.first = self.root / "first.pdf"
        self.second = self.root / "second.pdf"
        self.plain.write_bytes(b"%PDF-plain")
        self.first.write_bytes(b"%PDF-right-first")
        self.second.write_bytes(b"%PDF-right-second")
        self.budget = ResourceBudget(4, 4096, render_jobs=2)

    def commands(self, runner: RangeRunner, tool: str) -> list[list[str]]:
        """Return the commands of ``tool`` the runner ran, probes excluded."""
        return [argv for argv in runner.commands if argv[0] == tool]

    async def compare(
        self,
        runner: RangeRunner,
        left: Path,
        right: Path,
        name: str,
        *,
        options: ComparisonOptions | None = None,
    ) -> list[dict[str, object]]:
        """Compare two PDFs with the job's budget and return the findings as plain data."""
        findings = await compare_pdfs(
            left, right, self.root / name, runner, budget=self.budget, options=options
        )
        return [asdict(finding) for finding in findings]

    async def test_a_pdf_that_two_comparisons_share_is_measured_and_rendered_once(self) -> None:
        runner = RangeRunner(pages=PAGES, changed_pages=(2,))
        async with pdf_job_scope(self.root / "job", runner, self.budget):
            await self.compare(runner, self.plain, self.first, "compare-1")
            renders_after_first = len(runner.renders)
            second = await self.compare(runner, self.first, self.second, "compare-2")
        # Twelve pages with two renderers go in four ranges of three pages. The first
        # comparison renders both sides; the second only its new right side.
        self.assertEqual(renders_after_first, 8)
        self.assertEqual(len(runner.renders) - renders_after_first, 4)
        self.assertEqual({side for side, *_ in runner.renders[renders_after_first:]}, {"right"})
        self.assertEqual(len(self.commands(runner, "pdfinfo")), 6)
        self.assertEqual(len(self.commands(runner, "pdftotext")), 3)
        rendering = next(item for item in second if item["rule"] == "compare.rendering")
        self.assertEqual(rendering["status"], "passed")

    async def test_nothing_is_shared_outside_a_job_scope(self) -> None:
        runner = RangeRunner(pages=PAGES, changed_pages=(2,))
        await self.compare(runner, self.plain, self.first, "compare-1")
        renders_after_first = len(runner.renders)
        await self.compare(runner, self.first, self.second, "compare-2")
        self.assertEqual(len(runner.renders), 2 * renders_after_first)
        self.assertEqual(len(self.commands(runner, "pdfinfo")), 8)

    async def test_findings_do_not_depend_on_sharing(self) -> None:
        shared_runner = RangeRunner(pages=PAGES, changed_pages=(2, 5))
        async with pdf_job_scope(self.root / "job", shared_runner, self.budget):
            await self.compare(shared_runner, self.plain, self.first, "shared-1")
            shared = await self.compare(shared_runner, self.first, self.plain, "shared-2")
        alone_runner = RangeRunner(pages=PAGES, changed_pages=(2, 5))
        await self.compare(alone_runner, self.plain, self.first, "alone-1")
        alone = await self.compare(alone_runner, self.first, self.plain, "alone-2")
        self.assertEqual(shared, alone)
        rendering = next(item for item in shared if item["rule"] == "compare.rendering")
        self.assertEqual(rendering["details"]["changed_pages"], [2, 5])

    async def test_known_digests_need_no_render_for_an_exact_comparison(self) -> None:
        runner = RangeRunner(pages=PAGES, changed_pages=(2,))
        async with pdf_job_scope(self.root / "job", runner, self.budget):
            await self.compare(runner, self.plain, self.first, "compare-1")
            renders = len(runner.renders)
            swapped = await self.compare(runner, self.first, self.plain, "compare-2")
        self.assertEqual(len(runner.renders), renders)
        rendering = next(item for item in swapped if item["rule"] == "compare.rendering")
        self.assertEqual(rendering["details"]["changed_pages"], [2])

    async def test_equal_mis_sized_rasters_are_inconclusive_and_not_remembered(self) -> None:
        class MisSizedRunner(RangeRunner):
            """Write identical mis-sized PPMs until the override is cleared."""

            override: bytes | None = None

            def respond(self, call: ToolCall) -> Reply:
                """Replace rendered pages with the override when set."""
                reply = super().respond(call)
                if reply.files and self.override is not None:
                    return replace(reply, files=dict.fromkeys(reply.files, self.override))
                return reply

        # The real rasters of these 20 x 30 point pages are 40 x 60 at 144 DPI.
        for pixels in ((38, 60), (40, 58), (60, 40)):
            with self.subTest(pixels=pixels):
                runner = MisSizedRunner(pages=1)
                runner.override = comparison_ppm(pixels)
                async with pdf_job_scope(self.root / f"job-{pixels[0]}", runner, self.budget):
                    first = await self.compare(runner, self.plain, self.first, f"bad-{pixels[0]}")
                    runner.override = None
                    second = await self.compare(runner, self.plain, self.first, f"ok-{pixels[0]}")
                by_rule = {item["rule"]: item for item in first}
                self.assertNotIn("compare.rendering", by_rule)
                self.assertEqual(by_rule["compare.text"]["status"], "passed")
                unavailable = by_rule["compare.unavailable"]
                self.assertEqual(unavailable["status"], "inconclusive")
                self.assertEqual(unavailable["details"]["unavailable_pages"], [1])
                self.assertIn("do not match the page geometry", unavailable["message"])
                self.assertEqual(runner.single_renders, [("left", 1), ("right", 1)] * 2)
                self.assertTrue(all(item["status"] == "passed" for item in second))

    async def test_pixel_counts_render_only_the_pages_that_differ_again(self) -> None:
        options = ComparisonOptions(1, 0.5)
        runner = RangeRunner(pages=PAGES, changed_pages=(2, 9))
        async with pdf_job_scope(self.root / "job", runner, self.budget):
            await self.compare(runner, self.plain, self.first, "compare-1", options=options)
            renders = len(runner.renders)
            swapped = await self.compare(
                runner, self.first, self.plain, "compare-2", options=options
            )
        # Every digest was known, so the pairs are told apart without a render; only the
        # two pages whose pixels are counted are rendered again, alone and on both sides.
        again = runner.renders[renders:]
        self.assertEqual(len(again), 4)
        self.assertTrue(all(single for *_, single in again))
        self.assertEqual({first for _, first, _, _ in again}, {2, 9})
        rendering = next(item for item in swapped if item["rule"] == "compare.rendering")
        measurements = rendering["details"]["measurements"]
        self.assertEqual([row["page"] for row in measurements], [2, 9])
        self.assertTrue(all(row["changed_pixels"] == 1 for row in measurements))

    async def test_an_inspection_and_a_comparison_share_the_pdfs_measurements(self) -> None:
        runner = RangeRunner(pages=PAGES)
        async with pdf_job_scope(self.root / "job", runner, self.budget):
            await inspect_pdf(self.plain, self.root / "inspect", runner, budget=self.budget)
            await self.compare(runner, self.plain, self.first, "compare")
        properties = [argv for argv in self.commands(runner, "pdfinfo") if "-box" not in argv]
        self.assertEqual(len(properties), 2)
        self.assertEqual(len(self.commands(runner, "pdftotext")), 2)

    async def test_a_pipeline_run_does_not_release_the_jobs_session(self) -> None:
        runner = RangeRunner(pages=PAGES)
        async with pdf_job_scope(self.root / "job", runner, self.budget):
            await inspect_standalone_pdf(
                self.plain, self.root / "pipeline", runner, PdfCheckSettings(), self.budget
            )
            findings = await self.compare(runner, self.plain, self.first, "compare")
        self.assertFalse(any(item["rule"] == "compare.unavailable" for item in findings))

    async def test_a_registry_serves_only_the_runner_and_budget_it_was_made_for(self) -> None:
        runner = RangeRunner()
        async with pdf_job_scope(self.root / "job", runner, self.budget) as sessions:
            self.assertIs(job_pdf_sessions(runner, self.budget), sessions)
            self.assertIsNone(job_pdf_sessions(RangeRunner(), self.budget))
            self.assertIsNone(job_pdf_sessions(runner, ResourceBudget(4, 4096)))
            self.assertIsNone(job_pdf_sessions(runner, None))
        self.assertIsNone(job_pdf_sessions(runner, self.budget))

    async def test_the_scope_forgets_every_pdf_when_it_ends(self) -> None:
        runner = RangeRunner()
        async with pdf_job_scope(self.root / "job", runner, self.budget) as sessions:
            before = sessions.pdf(self.plain)
            self.assertIs(sessions.pdf(self.plain), before)
        self.assertIsNot(sessions.pdf(self.plain), before)

    async def test_a_replaced_file_is_not_served_from_its_predecessors_memory(self) -> None:
        runner = RangeRunner()
        async with pdf_job_scope(self.root / "job", runner, self.budget) as sessions:
            before = sessions.pdf(self.plain)
            self.plain.write_bytes(b"%PDF-plain, but longer than before")
            self.assertIsNot(sessions.pdf(self.plain), before)


class ToolProbeTests(unittest.IsolatedAsyncioTestCase):
    """The tool versions that a job probes while it still builds."""

    async def asyncSetUp(self) -> None:
        """Create a temporary directory and a budget."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.budget = ResourceBudget(4, 4096, render_jobs=2)

    async def settle(self, runner: ProbeRecordingRunner, count: int) -> None:
        """Wait until the runner has started ``count`` probes."""
        for _ in range(200):
            if len(runner.probes) >= count and runner.active == 0:
                return
            await asyncio.sleep(0.01)
        self.fail(f"Only {len(runner.probes)} of {count} probes ran")

    async def test_the_probes_run_concurrently_and_only_once(self) -> None:
        runner = ProbeRecordingRunner()
        async with pdf_job_scope(self.root / "job", runner, self.budget) as sessions:
            sessions.warm_tool_probes({"pdfinfo", "pdftotext", "pdftoppm"})
            sessions.warm_tool_probes({"pdfinfo", "pdftoppm"})
            await self.settle(runner, 3)
            self.assertEqual(sorted(runner.probes), ["pdfinfo", "pdftoppm", "pdftotext"])
            self.assertGreaterEqual(runner.peak, 2)
            first, second = self.root / "first.pdf", self.root / "second.pdf"
            first.write_bytes(b"%PDF-plain")
            second.write_bytes(b"%PDF-right")
            await compare_pdfs(first, second, self.root / "compare", runner, budget=self.budget)
        self.assertEqual(sorted(runner.probes), ["pdfinfo", "pdftoppm", "pdftotext"])

    async def test_a_tool_that_cannot_be_probed_is_left_to_the_command_that_needs_it(self) -> None:
        runner = ProbeRecordingRunner()
        runner.missing = "pdftoppm"
        async with pdf_job_scope(self.root / "job", runner, self.budget) as sessions:
            sessions.warm_tool_probes({"pdfinfo", "pdftoppm"})
            await self.settle(runner, 1)
            await asyncio.sleep(0.05)
        self.assertEqual(sorted(runner.tool_versions), ["pdfinfo"])
        with self.assertRaisesRegex(PreparationError, "Missing tool: pdftoppm"):
            await runner.tool_version(["pdftoppm", "-v"], self.root)

    async def test_probes_still_running_when_the_scope_ends_are_stopped(self) -> None:
        runner = ProbeRecordingRunner()
        async with pdf_job_scope(self.root / "job", runner, self.budget) as sessions:
            sessions.warm_tool_probes({"pdfinfo", "pdftotext"})
        self.assertEqual(runner.active, 0)
        await asyncio.sleep(0.05)
        self.assertEqual(runner.active, 0)
