from __future__ import annotations

import asyncio
import tempfile
import unittest
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, replace
from pathlib import Path

from latexprep.models import PreparationError
from latexprep.pdf import PdfOptions, _pair_memory_mb, _read_raster, compare_pdfs, inspect_pdf
from latexprep.runtime import CommandResult
from latexprep.scheduler import ResourceBudget
from tests.test_pdf import FakePdfRunner


class RecordingPdfRunner(FakePdfRunner):
    """Fake tool output with the runtime's actual freeze and version-cache contracts."""

    def __init__(self, *, pages: int = 3, changed_pages: tuple[int, ...] = ()) -> None:
        super().__init__()
        self.pages = pages
        self.changed_pages = changed_pages
        self.hook: Callable[[list[str], Path, tuple[Path, ...]], Awaitable[None]] | None = None
        self.records: list[tuple[list[str], Path, tuple[Path, ...]]] = []
        self.active = self.peak = self.active_renders = self.peak_renders = 0
        self.render_completions: list[int] = []
        self.render_memory_limits: list[int | None] = []

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        self.records.append((argv, workspace, readonly_inputs))
        self.active += 1
        self.peak = max(self.peak, self.active)
        rendering = argv[0] == "pdftoppm" and argv[-1] != "-v"
        if rendering:
            self.render_memory_limits.append(memory_mb)
            self.active_renders += 1
            self.peak_renders = max(self.peak_renders, self.active_renders)
        try:
            if self.hook is not None and argv[-1] != "-v":
                await self.hook(argv, workspace, readonly_inputs)
            # A scheduling point also exercises concurrent version-cache misses.
            await asyncio.sleep(0)
            result = await super().run(
                argv, cwd, workspace, readonly_inputs=readonly_inputs, memory_mb=memory_mb
            )
            if argv[-1] == "-v":
                return result
            if argv[0] == "pdfinfo":
                output = f"Pages: {self.pages}\nEncrypted: no\nForm: none\nJavaScript: no\n"
                if "-box" in argv:
                    output += "".join(
                        f"Page {page} size: 612 x 792 pts\n" for page in range(1, self.pages + 1)
                    )
                return replace(result, stdout=output)
            if argv[0] == "pdftotext":
                return replace(result, stdout="Text\n\f" * self.pages)
            if rendering:
                page = int(argv[argv.index("-f") + 1])
                right = any(b"right" in source.read_bytes() for source in readonly_inputs)
                pixel = b"\0\0\0" if right and page in self.changed_pages else b"\xff\xff\xff"
                (workspace / "page.ppm").write_bytes(b"P6\n1 1\n255\n" + pixel)
                if right:
                    self.render_completions.append(page)
            return result
        finally:
            self.active -= 1
            if rendering:
                self.active_renders -= 1


class ParallelPdfTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "left").mkdir()
        (self.root / "right").mkdir()
        # Identical basenames must be safe in the shared frozen-input store.
        self.left = self.root / "left/document.pdf"
        self.right = self.root / "right/document.pdf"
        self.left.write_bytes(b"%PDF-left")
        self.right.write_bytes(b"%PDF-right")

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_serial_parallel_and_standalone_findings_are_identical(self) -> None:
        results = []
        options = PdfOptions(
            expected_page_width_pt=612,
            expected_page_height_pt=792,
            require_embedded_fonts=True,
            forbid_attachments=True,
            min_image_dpi=300,
        )
        for index, budget in enumerate(
            (None, ResourceBudget(1, 1024), ResourceBudget(4, 1024, render_jobs=2))
        ):
            runner = RecordingPdfRunner(changed_pages=(1, 3))
            inspected = await inspect_pdf(
                self.left, self.root / f"inspect-{index}", runner, 2, options=options, budget=budget
            )
            compared = await compare_pdfs(
                self.left, self.right, self.root / f"compare-{index}", runner, budget=budget
            )
            results.append(
                ([asdict(item) for item in inspected], [asdict(item) for item in compared])
            )
            if budget is None or budget.jobs == 1:
                self.assertEqual(runner.peak, 1)
        self.assertEqual(results[0], results[1])
        self.assertEqual(results[1], results[2])

    async def test_inventory_operations_overlap_only_after_page_preflight(self) -> None:
        runner = RecordingPdfRunner()
        ready, release = asyncio.Event(), asyncio.Event()
        started: set[str] = set()
        properties_seen = False

        async def hook(argv, _workspace, _inputs):
            nonlocal properties_seen
            if argv[0] == "pdfinfo" and "-box" not in argv:
                properties_seen = True
                return
            self.assertTrue(properties_seen)
            started.add(argv[0])
            if len(started) == 4:
                ready.set()
            await release.wait()

        runner.hook = hook
        execution = asyncio.create_task(
            inspect_pdf(self.left, self.root / "inspect", runner, budget=ResourceBudget(4, 1024))
        )
        try:
            await asyncio.wait_for(ready.wait(), 2)
            self.assertEqual(started, {"pdfinfo", "pdffonts", "pdftotext", "pdfdetach"})
            self.assertEqual(runner.active, 4)
        finally:
            release.set()
        findings = await execution
        self.assertEqual(
            next(item for item in findings if item.rule == "pdf.images").status, "passed"
        )
        self.assertEqual(runner.active, 0)

    async def test_comparison_side_preflights_overlap(self) -> None:
        runner = RecordingPdfRunner()
        ready, release = asyncio.Event(), asyncio.Event()
        sides: set[bytes] = set()

        async def hook(argv, _workspace, inputs):
            if argv[0] == "pdfinfo" and "-box" not in argv:
                sides.add(inputs[0].read_bytes())
                if len(sides) == 2:
                    ready.set()
                await release.wait()

        runner.hook = hook
        execution = asyncio.create_task(
            compare_pdfs(
                self.left, self.right, self.root / "compare", runner, budget=ResourceBudget(4, 1024)
            )
        )
        try:
            await asyncio.wait_for(ready.wait(), 2)
            self.assertEqual(sides, {b"%PDF-left", b"%PDF-right"})
        finally:
            release.set()
        self.assertTrue(all(item.status == "passed" for item in await execution))

    async def test_render_cap_overlap_ordering_and_prompt_raster_release(self) -> None:
        runner = RecordingPdfRunner(changed_pages=(1, 2, 3))
        budget = ResourceBudget(4, 1024, render_jobs=2)
        first_two, third_started = asyncio.Event(), asyncio.Event()
        release_first, release_second = asyncio.Event(), asyncio.Event()
        started: set[int] = set()

        async def hook(argv, _workspace, inputs):
            if argv[0] != "pdftoppm" or b"right" in inputs[0].read_bytes():
                return
            page = int(argv[argv.index("-f") + 1])
            started.add(page)
            if {1, 2} <= started:
                first_two.set()
            if page == 1:
                await release_first.wait()
            elif page == 2:
                await release_second.wait()
            elif page == 3:
                third_started.set()

        runner.hook = hook
        work = self.root / "compare"
        execution = asyncio.create_task(
            compare_pdfs(self.left, self.right, work, runner, budget=budget)
        )
        try:
            await asyncio.wait_for(first_two.wait(), 2)
            self.assertEqual(runner.active_renders, 2)
            self.assertFalse(third_started.is_set())
            release_second.set()
            await asyncio.wait_for(third_started.wait(), 2)
            self.assertFalse((work / "page-0002").exists())
        finally:
            release_first.set()
            release_second.set()
        findings = await execution
        rendered = next(item for item in findings if item.rule == "compare.rendering")
        self.assertEqual(rendered.details["changed_pages"], [1, 2, 3])
        self.assertEqual([item["page"] for item in rendered.details["measurements"]], [1, 2, 3])
        self.assertEqual(runner.render_completions[0], 2)
        self.assertEqual(runner.peak_renders, 2)
        self.assertEqual(runner.render_memory_limits, [128] * 6)
        self.assertEqual(budget.statistics()["peak_renders"], 2)
        self.assertFalse(list(work.glob("page-*")))
        self.assertFalse(list(work.rglob("*.ppm")))

    async def test_concurrent_comparisons_reuse_frozen_inputs_versions_and_isolate_outputs(
        self,
    ) -> None:
        runner = RecordingPdfRunner()
        budget = ResourceBudget(4, 1024, render_jobs=2)
        first, second, inspection = await asyncio.gather(
            compare_pdfs(self.left, self.right, self.root / "first", runner, budget=budget),
            compare_pdfs(self.left, self.right, self.root / "second", runner, budget=budget),
            inspect_pdf(self.left, self.root / "inspection", runner, budget=budget),
        )
        self.assertEqual(first, second)
        frozen = {source for _argv, _work, sources in runner.records for source in sources}
        self.assertEqual(len(frozen), 2)
        self.assertEqual({source.read_bytes() for source in frozen}, {b"%PDF-left", b"%PDF-right"})
        self.assertTrue(all(source not in (self.left, self.right) for source in frozen))
        workspaces = [work for _argv, work, _sources in runner.records]
        self.assertEqual(len(workspaces), len(set(workspaces)))
        for _argv, work, sources in runner.records:
            self.assertTrue(all(work not in source.parents for source in sources))
        probes = Counter(argv[0] for argv, _work, _sources in runner.records if argv[-1] == "-v")
        self.assertEqual(
            probes,
            Counter(
                {
                    name: 1
                    for name in (
                        "pdfinfo",
                        "pdftotext",
                        "pdftoppm",
                        "pdffonts",
                        "pdfdetach",
                        "pdfimages",
                    )
                }
            ),
        )
        tools = next(item.details["tools"] for item in first if item.rule == "compare.tools")
        self.assertEqual(set(tools), {"pdfinfo", "pdftotext", "pdftoppm"})
        inspection_tools = next(
            item.details["tools"] for item in inspection if item.rule == "pdf.tools"
        )
        self.assertNotIn("pdftoppm", inspection_tools)
        tools["pdfinfo"]["version"].append("edited report")
        self.assertNotIn("edited report", runner.tool_versions["pdfinfo"]["version"])
        other_tools = next(item.details["tools"] for item in second if item.rule == "compare.tools")
        self.assertNotIn("edited report", other_tools["pdfinfo"]["version"])

    async def test_failed_inventory_waits_for_siblings_and_preserves_their_findings(self) -> None:
        runner = RecordingPdfRunner()
        text_started, font_failed, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def hook(argv, _workspace, _inputs):
            if argv[0] == "pdftotext":
                text_started.set()
                await release.wait()
            elif argv[0] == "pdffonts":
                font_failed.set()
                raise PreparationError("Font inventory unavailable")

        runner.hook = hook
        execution = asyncio.create_task(
            inspect_pdf(
                self.left,
                self.root / "inspect",
                runner,
                options=PdfOptions(require_embedded_fonts=True),
                budget=ResourceBudget(4, 1024),
            )
        )
        try:
            await asyncio.wait_for(asyncio.gather(text_started.wait(), font_failed.wait()), 2)
            self.assertFalse(execution.done())
        finally:
            release.set()
        by_rule = {item.rule: item for item in await execution}
        self.assertEqual(by_rule["pdf.font_embedding"].status, "inconclusive")
        self.assertEqual(by_rule["pdf.text"].status, "passed")
        self.assertEqual(by_rule["pdf.images"].status, "passed")
        self.assertEqual(runner.active, 0)

    async def test_failed_page_preserves_completed_evidence_and_drains_other_pages(self) -> None:
        runner = RecordingPdfRunner(changed_pages=(2,))
        failed, sibling_started, release = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def hook(argv, workspace, _inputs):
            if argv[0] != "pdftoppm":
                return
            page = int(argv[argv.index("-f") + 1])
            if page == 1:
                (workspace / "page.ppm").write_bytes(b"partial")
                failed.set()
                raise PreparationError("Page renderer failed")
            if page == 2:
                sibling_started.set()
                await release.wait()

        runner.hook = hook
        work = self.root / "compare"
        execution = asyncio.create_task(
            compare_pdfs(
                self.left, self.right, work, runner, budget=ResourceBudget(4, 1024, render_jobs=2)
            )
        )
        try:
            await asyncio.wait_for(asyncio.gather(failed.wait(), sibling_started.wait()), 2)
            self.assertFalse(execution.done())
        finally:
            release.set()
        by_rule = {item.rule: item for item in await execution}
        self.assertEqual(by_rule["compare.page_count"].status, "passed")
        self.assertEqual(by_rule["compare.text"].status, "passed")
        self.assertNotIn("compare.rendering", by_rule)
        unavailable = by_rule["compare.unavailable"]
        self.assertEqual(unavailable.status, "inconclusive")
        self.assertEqual(unavailable.severity, "error")
        self.assertEqual(unavailable.details["completed_pages"], [2, 3])
        self.assertEqual(unavailable.details["unavailable_pages"], [1])
        self.assertEqual(unavailable.details["known_changed_pages"], [2])
        self.assertEqual(runner.active, 0)
        self.assertFalse(list(work.rglob("*.ppm")))

    async def test_text_and_geometry_failures_preserve_independent_measurements(self) -> None:
        for index, failed_tool in enumerate(("pdftotext", "geometry")):
            runner = RecordingPdfRunner()

            async def hook(argv, _workspace, inputs, failed_tool=failed_tool):
                if b"right" not in inputs[0].read_bytes():
                    return
                if argv[0] == failed_tool or (failed_tool == "geometry" and "-box" in argv):
                    raise PreparationError("Required measurement unavailable")

            runner.hook = hook
            findings = await compare_pdfs(
                self.left,
                self.right,
                self.root / str(index),
                runner,
                budget=ResourceBudget(4, 1024, render_jobs=2),
            )
            by_rule = {item.rule: item for item in findings}
            self.assertEqual(by_rule["compare.page_count"].status, "passed")
            self.assertEqual(by_rule["compare.unavailable"].status, "inconclusive")
            if failed_tool == "pdftotext":
                self.assertNotIn("compare.text", by_rule)
                self.assertEqual(by_rule["compare.rendering"].status, "passed")
            else:
                self.assertNotIn("compare.rendering", by_rule)
                self.assertEqual(by_rule["compare.text"].status, "passed")
                self.assertFalse(any(argv[0] == "pdftoppm" for argv in runner.commands))

    async def test_repeated_cancellation_reaps_workers_before_releasing_rasters_and_leases(
        self,
    ) -> None:
        runner = RecordingPdfRunner()
        budget = ResourceBudget(4, 1024, render_jobs=2)
        both_started, both_cleaning, cleanup_release = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )
        started: set[int] = set()
        cleaning: set[int] = set()

        async def hook(argv, workspace, _inputs):
            if argv[0] != "pdftoppm":
                return
            page = int(argv[argv.index("-f") + 1])
            (workspace / "page.ppm").write_bytes(b"partial")
            started.add(page)
            if len(started) == 2:
                both_started.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                cleaning.add(page)
                if len(cleaning) == 2:
                    both_cleaning.set()
                cleanup = asyncio.create_task(cleanup_release.wait())
                while not cleanup.done():
                    try:
                        await asyncio.shield(cleanup)
                    except asyncio.CancelledError:
                        continue
                raise

        runner.hook = hook
        work = self.root / "compare"
        execution = asyncio.create_task(
            compare_pdfs(self.left, self.right, work, runner, budget=budget)
        )
        try:
            await asyncio.wait_for(both_started.wait(), 2)
            execution.cancel()
            await asyncio.wait_for(both_cleaning.wait(), 2)
            execution.cancel()
            self.assertFalse(execution.done())
            self.assertEqual(len(list(work.rglob("*.ppm"))), 2)
        finally:
            cleanup_release.set()
        with self.assertRaises(asyncio.CancelledError):
            await asyncio.wait_for(execution, 2)
        self.assertEqual(started, {1, 2})
        self.assertEqual(runner.active, 0)
        self.assertFalse(list(work.rglob("*.ppm")))
        self.assertEqual(budget.statistics()["active_cpu"], 0)
        self.assertEqual(budget.statistics()["active_memory_mb"], 0)
        self.assertEqual(budget.statistics()["queued"], 0)

    async def test_geometry_memory_request_rejects_rendering_before_launch(self) -> None:
        runner = RecordingPdfRunner(pages=1)
        required = _pair_memory_mb((612, 792), (612, 792))
        self.assertGreater(required, 128)
        budget = ResourceBudget(4, required - 1, render_jobs=4)
        findings = await compare_pdfs(
            self.left, self.right, self.root / "compare", runner, budget=budget
        )
        by_rule = {item.rule: item for item in findings}
        self.assertEqual(by_rule["compare.text"].status, "passed")
        self.assertNotIn("compare.rendering", by_rule)
        self.assertIn(f"{required} MiB", by_rule["compare.unavailable"].message)
        self.assertFalse(any(argv[0] == "pdftoppm" for argv in runner.commands))

    def test_raster_pixels_cannot_exceed_geometry_reservation(self) -> None:
        raster = self.root / "oversized.ppm"
        raster.write_bytes(b"P6\n2 2\n255\n" + b"\0" * 12)
        with self.assertRaisesRegex(PreparationError, "oversized raster dimensions"):
            _read_raster(raster, max_pixels=1)


if __name__ == "__main__":
    unittest.main()
