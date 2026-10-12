from __future__ import annotations

import asyncio
import base64
import json
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path

from latexprep.models import PreparationError
from latexprep.pdf_comparison import compare_pdfs
from latexprep.pdf_previews import PdfPreviewStore
from latexprep.runtime import CommandResult
from latexprep.scheduler import ResourceBudget
from latexprep.workflow_options import ComparisonOptions
from tests.test_pdf import FakePdfRunner

PNG = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+ap1kAAAAASUVORK5CYII="
)


class PreviewRunner(FakePdfRunner):
    def __init__(self, *, pages_left=1, png=PNG, png_error=False, resize=False, **kwargs):
        super().__init__(**kwargs)
        self.pages_left = pages_left
        self.png = png
        self.png_error = png_error
        self.resize = resize
        self.png_hook = None

    async def run(
        self, argv, cwd, workspace, *, readonly_inputs=(), memory_mb=None
    ) -> CommandResult:
        if argv[0] == "pdftoppm" and "-png" in argv:
            self._frozen.validate(readonly_inputs)
            self.commands.append(argv)
            (workspace / "page.png").write_bytes(self.png)
            if self.png_hook:
                await self.png_hook()
            if self.png_error or self.missing == "pdftoppm":
                raise PreparationError(f"Preview failure at {workspace}")
            return CommandResult(0, "", "", resolved_command=argv)
        result = await super().run(
            argv,
            cwd,
            workspace,
            readonly_inputs=readonly_inputs,
            memory_mb=memory_mb,
        )
        if argv[-1] == "-v":
            return result
        right = any(b"right" in path.read_bytes() for path in readonly_inputs)
        pages = self.pages_right if right else self.pages_left
        if argv[0] == "pdfinfo":
            output = f"Pages: {pages}\n"
            if "-box" in argv:
                height = 790 if right and self.resize else 792
                output += "".join(
                    f"Page {page} size: 612 x {height} pts\n" for page in range(1, pages + 1)
                )
            result = replace(result, stdout=output)
        elif argv[0] == "pdftotext":
            text = "Changed" if right and self.text_changed else "Text"
            result = replace(result, stdout=f"{text}\n\f" * pages)
        return result


class PdfPreviewTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.left, self.right = self.root / "left.pdf", self.root / "right.pdf"
        self.left.write_bytes(b"%PDF-left")
        self.right.write_bytes(b"%PDF-right")

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def compare(self, runner, store=None, name="compare", budget=None):
        return await compare_pdfs(
            self.left,
            self.right,
            self.root / name,
            runner,
            previews=store.for_comparison("prepared", "main.tex") if store is not None else None,
            budget=budget,
        )

    async def test_visual_difference_captured_after_all_page_measurements(self) -> None:
        store = PdfPreviewStore()
        runner = PreviewRunner(raster_changed=True, pages_left=3, pages_right=3)
        findings = await self.compare(runner, store, budget=ResourceBudget(4, 1024, render_jobs=2))
        self.assertEqual(
            next(f.status for f in findings if f.rule == "compare.rendering"), "failed"
        )
        pages = store.summary()["comparisons"][0]["pages"]
        self.assertEqual([page["page"] for page in pages], [1, 2, 3])
        self.assertTrue(all(page["rendering_changed"] for page in pages))
        self.assertTrue(all(not page["text_only"] for page in pages))
        render_commands = [
            argv for argv in runner.commands if argv[0] == "pdftoppm" and argv[-1] != "-v"
        ]
        self.assertEqual(sum("-png" not in argv for argv in render_commands), 6)
        self.assertEqual(sum("-png" in argv for argv in render_commands), 6)
        first_png = next(i for i, argv in enumerate(render_commands) if "-png" in argv)
        self.assertEqual(first_png, 6)
        for page in pages:
            for side in ("left", "right"):
                self.assertEqual(store.images[page[side]["image_id"]], PNG)
                self.assertEqual(page[side]["pixels"], [1, 1])
        self.assertFalse(list((self.root / "compare").glob("preview-*")))
        self.assertFalse(list((self.root / "compare").rglob("*.ppm")))
        shutil.rmtree(self.root / "compare")
        summary = json.dumps(store.summary())
        self.assertNotIn(str(self.root), summary)
        self.assertEqual(len(store.images), 6)

    async def test_text_only_difference_uses_matching_visuals_and_keeps_failed_verdict(self):
        store = PdfPreviewStore()
        findings = await self.compare(PreviewRunner(text_changed=True), store)
        by_rule = {finding.rule: finding for finding in findings}
        self.assertEqual(by_rule["compare.text"].status, "failed")
        self.assertEqual(by_rule["compare.rendering"].status, "passed")
        page = store.summary()["comparisons"][0]["pages"][0]
        self.assertTrue(page["text_only"])
        self.assertFalse(page["rendering_changed"])
        self.assertEqual(store.images[page["left"]["image_id"]], PNG)
        self.assertEqual(store.images[page["right"]["image_id"]], PNG)

    async def test_page_count_difference_marks_absent_side_and_captures_present_page(self):
        store = PdfPreviewStore()
        findings = await self.compare(PreviewRunner(pages_right=2), store)
        self.assertEqual(
            next(f.status for f in findings if f.rule == "compare.page_count"), "failed"
        )
        page = store.summary()["comparisons"][0]["pages"][0]
        self.assertEqual(page["page"], 2)
        self.assertTrue(page["page_count_changed"])
        self.assertEqual(page["left"]["status"], "missing")
        self.assertFalse(page["left"]["present"])
        self.assertIsNone(page["left"]["dimensions_pt"])
        self.assertEqual(page["right"]["status"], "captured")
        self.assertEqual(len(store.images), 1)

    async def test_page_dimensions_are_preserved(self):
        store = PdfPreviewStore()
        await self.compare(PreviewRunner(resize=True), store)
        page = store.summary()["comparisons"][0]["pages"][0]
        self.assertTrue(page["page_dimensions_changed"])
        self.assertEqual(page["left"]["dimensions_pt"], [612, 792])
        self.assertEqual(page["right"]["dimensions_pt"], [612, 790])

    async def test_default_path_and_unchanged_comparison_do_not_capture(self):
        for index, store in enumerate((None, PdfPreviewStore())):
            runner = PreviewRunner()
            findings = await self.compare(runner, store, name=f"compare-{index}")
            self.assertTrue(all(finding.status == "passed" for finding in findings))
            self.assertFalse(any("-png" in argv for argv in runner.commands))
            if store is not None:
                self.assertEqual(store.summary()["comparisons"][0]["status"], "unchanged")
                self.assertFalse(store.images)

    async def test_preview_page_limit_does_not_truncate_comparison_verification(self):
        runner = PreviewRunner(pages_left=10, pages_right=10, raster_changed=True)
        store = PdfPreviewStore()
        findings = await self.compare(runner, store)
        rendered = next(f for f in findings if f.rule == "compare.rendering")
        self.assertEqual(rendered.details["changed_pages"], list(range(1, 11)))
        self.assertEqual(sum("-png" in argv for argv in runner.commands), 16)
        summary = store.summary()
        self.assertEqual(summary["captured_page_pairs"], 8)
        self.assertEqual(summary["omitted_page_pairs"], 2)
        self.assertEqual(len(store.images), 16)

    async def test_global_budget_allocates_in_coordinator_order(self):
        store = PdfPreviewStore(max_page_pairs=1)
        await self.compare(PreviewRunner(text_changed=True), store, name="first")
        await self.compare(PreviewRunner(text_changed=True), store, name="second")
        comparisons = store.summary()["comparisons"]
        self.assertEqual([item["status"] for item in comparisons], ["captured", "omitted"])
        self.assertEqual(list(store.images), ["pdf-preview-0001-left", "pdf-preview-0001-right"])

    async def test_missing_renderer_and_preview_only_failure_preserve_findings(self):
        store = PdfPreviewStore()
        failed = await self.compare(PreviewRunner(text_changed=True, missing="pdftoppm"), store)
        self.assertEqual(next(f.status for f in failed if f.rule == "compare.text"), "failed")
        self.assertEqual(store.summary()["unavailable_page_pairs"], 1)
        baseline = await self.compare(PreviewRunner(text_changed=True), name="baseline")
        second_store = PdfPreviewStore()
        with_previews = await self.compare(
            PreviewRunner(text_changed=True, png_error=True), second_store, name="png-error"
        )
        self.assertEqual(baseline, with_previews)
        self.assertEqual(second_store.summary()["unavailable_page_pairs"], 1)
        self.assertNotIn(str(self.root), json.dumps(second_store.summary()))

    async def test_unavailable_preview_does_not_replace_a_passing_tolerant_verdict(self):
        store = PdfPreviewStore()
        runner = PreviewRunner(raster_changed=True, png_error=True)
        findings = await compare_pdfs(
            self.left,
            self.right,
            self.root / "tolerant",
            runner,
            options=ComparisonOptions(max_changed_pixel_ratio=1),
            previews=store.for_comparison("prepared"),
        )
        self.assertTrue(all(finding.status == "passed" for finding in findings))
        page = store.summary()["comparisons"][0]["pages"][0]
        self.assertTrue(page["within_render_tolerance"])
        self.assertEqual(page["status"], "unavailable")
        self.assertFalse(store.images)

    async def test_byte_limit_retains_one_side_and_omits_remaining_pages(self):
        store = PdfPreviewStore(max_total_bytes=len(PNG))
        runner = PreviewRunner(text_changed=True, pages_left=2, pages_right=2)
        findings = await self.compare(runner, store)
        self.assertEqual(next(f.status for f in findings if f.rule == "compare.text"), "failed")
        summary = store.summary()
        self.assertEqual(summary["image_bytes"], len(PNG))
        self.assertEqual(summary["partial_page_pairs"], 1)
        self.assertEqual(summary["omitted_page_pairs"], 1)
        self.assertEqual(len(store.images), 1)

    async def test_file_byte_limit_and_invalid_png_are_separate_preview_failures(self):
        store = PdfPreviewStore()
        runner = PreviewRunner(text_changed=True)
        runner.limits = replace(runner.limits, max_file_bytes=32)
        findings = await self.compare(runner, store)
        self.assertEqual(next(f.status for f in findings if f.rule == "compare.text"), "failed")
        self.assertEqual(store.summary()["omitted_page_pairs"], 1)
        invalid_store = PdfPreviewStore()
        await self.compare(
            PreviewRunner(text_changed=True, png=b"not a PNG"), invalid_store, name="invalid"
        )
        self.assertEqual(invalid_store.summary()["unavailable_page_pairs"], 1)
        self.assertFalse(invalid_store.images)

    async def test_cancelled_preview_renderer_releases_scratch_and_resource_lease(self):
        started = asyncio.Event()
        runner = PreviewRunner(text_changed=True)

        async def wait():
            started.set()
            await asyncio.Event().wait()

        runner.png_hook = wait
        budget = ResourceBudget(2, 1024, render_jobs=1)
        store = PdfPreviewStore()
        execution = asyncio.create_task(self.compare(runner, store, budget=budget))
        await asyncio.wait_for(started.wait(), 2)
        execution.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await execution
        self.assertFalse(list((self.root / "compare").glob("preview-*")))
        self.assertEqual(budget.statistics()["active_cpu"], 0)
        self.assertEqual(budget.statistics()["active_memory_mb"], 0)
        self.assertEqual(store.summary()["comparisons"][0]["status"], "incomplete")

    async def test_preflight_failure_records_unavailable_previews(self):
        store = PdfPreviewStore()
        await self.compare(PreviewRunner(missing="pdfinfo"), store)
        self.assertEqual(store.summary()["comparisons"][0]["status"], "unavailable")
        self.assertFalse(store.images)
