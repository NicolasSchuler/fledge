"""One PDF tool session shared by the check groups of a pipeline run."""

from __future__ import annotations

import asyncio
import unittest
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from latexprep.command_runner import CommandResult
from latexprep.errors import PreparationError
from latexprep.findings import Finding
from latexprep.pdf_artwork import inspect_pdf_artwork
from latexprep.pdf_artwork_options import PdfArtworkOptions
from latexprep.pdf_check_options import PdfCheckOptions
from latexprep.pdf_check_pipeline import PdfCheckSettings, inspect_standalone_pdf
from latexprep.pdf_checks import extract_pdf_evidence, inspect_pdf_details
from latexprep.pdf_inspection import PdfInspectionOptions, inspect_pdf
from latexprep.pdf_structure_checks import (
    HeadingExpectation,
    StructureOptions,
    inspect_pdf_structure,
)
from latexprep.pdf_tools import (
    DOCUMENT,
    SharedPdfSession,
    font_inventory_argv,
    layout_text_argv,
    mupdf_trace_argv,
    page_boxes_argv,
    pdfinfo_argv,
    qpdf_objects_argv,
)
from latexprep.resource_budget import ResourceBudget
from latexprep.submission_checks import check_pdf_identity
from latexprep.submission_options import SubmissionOptions
from tests.pdf_fakes import DetailRunner
from tests.support import AsyncProjectTestCase

_VERSION_FLAGS = {"-v", "--version"}
_FROZEN = "@pdf@"
# Every group of a standalone PDF check that measures the checked PDF, each with a setting
# that makes it run its tools.
_ALL_GROUPS = PdfCheckSettings(
    max_pages=10,
    inspection=PdfInspectionOptions(require_embedded_fonts=True, forbid_attachments=True),
    details=PdfCheckOptions(
        required_metadata=("Title",),
        min_text_size_pt=8,
        min_page_ink_ratio=0.01,
        forbidden_annotation_types=("Link",),
        min_stroke_width_pt=0.1,
    ),
    rendered_artwork=PdfArtworkOptions(
        classify_artwork=True, classify_type3_glyphs=True, required_color_space="rgb"
    ),
    structure=StructureOptions(
        heading_expectations=(HeadingExpectation(1, "Body", ""),), require_structure_tree=True
    ),
    privacy=SubmissionOptions(identity_terms=("Nobody Atall",)),
)


def normalized(argv: list[str]) -> tuple[str, ...]:
    """A command line with the frozen PDF, as the runner or a session names it, as ``_FROZEN``."""
    return tuple(_FROZEN if item == DOCUMENT or item.endswith(".pdf") else item for item in argv)


class SharedRunner(DetailRunner):
    """Every tool the groups use, recording which commands ran and failing on request."""

    def __init__(self, **outputs: object) -> None:
        # DetailRunner applies the keyword outputs last, so these defaults come first.
        self.freezes = 0
        self.freeze_failure: BaseException | None = None
        self.fail_once: str | None = None
        super().__init__(**outputs)

    def freeze_input(self, source: Path, storage: Path) -> Path:
        self.freezes += 1
        if self.freeze_failure is not None:
            failure, self.freeze_failure = self.freeze_failure, None
            raise failure
        return super().freeze_input(source, storage)

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        probe = argv[-1] in _VERSION_FLAGS
        if argv[0] == self.fail_once and not probe:
            self.fail_once = None
            raise PreparationError("The tool failed this once")
        result = await super().run(
            argv, cwd, workspace, readonly_inputs=readonly_inputs, memory_mb=memory_mb
        )
        listings = {
            "pdfdetach": "0 embedded files\n",
            "pdfimages": "page num type width height color comp bpc enc interp object ID "
            "x-ppi y-ppi size ratio\n---\n",
        }
        if argv[0] in listings and not probe:
            return CommandResult(0, listings[argv[0]], "", resolved_command=argv)
        return result

    def commands(self) -> Counter[tuple[str, ...]]:
        """How often each command ran, apart from the version probes."""
        return Counter(
            normalized(argv) for argv, _, _ in self.calls if argv[-1] not in _VERSION_FLAGS
        )

    def runs_of(self, argv: list[str]) -> int:
        return self.commands()[normalized(argv)]

    def probes(self) -> Counter[str]:
        return Counter(argv[0] for argv, _, _ in self.calls if argv[-1] in _VERSION_FLAGS)


class SharedSessionCase(AsyncProjectTestCase):
    async def asyncSetUp(self) -> None:
        self.pdf = self.root / "document.pdf"
        self.pdf.write_bytes(b"%PDF-fixture")
        self.counter = 0

    def work(self, name: str = "work") -> Path:
        self.counter += 1
        return self.root / f"{name}-{self.counter}"

    async def properties(self, view) -> str:
        return await view.measure("properties", pdfinfo_argv(), lambda result: result.stdout)


class PipelineSharingTests(SharedSessionCase):
    async def run_groups_alone(self, runner: SharedRunner) -> list[Finding]:
        """Each group of ``_ALL_GROUPS`` run on its own, so each freezes the PDF itself."""
        settings, budget = _ALL_GROUPS, ResourceBudget(2, 1024)
        findings = await inspect_pdf(
            self.pdf,
            self.work(),
            runner,
            settings.max_pages,
            options=settings.inspection,
            budget=budget,
        )
        findings += await inspect_pdf_details(
            self.pdf, self.work(), runner, options=settings.details, budget=budget
        )
        text, metadata = await extract_pdf_evidence(self.pdf, self.work(), runner, budget=budget)
        findings += check_pdf_identity(text, metadata, settings.privacy)
        findings += await inspect_pdf_artwork(
            self.pdf, self.work(), runner, options=settings.rendered_artwork, budget=budget
        )
        findings += await inspect_pdf_structure(
            self.pdf, self.work(), runner, settings.structure, budget
        )
        return findings

    async def run_pipeline(self, runner: SharedRunner) -> list[Finding]:
        return await inspect_standalone_pdf(
            self.pdf, self.work(), runner, _ALL_GROUPS, ResourceBudget(2, 1024)
        )

    async def test_each_read_only_measurement_runs_once_for_the_whole_pipeline(self) -> None:
        alone, shared = SharedRunner(), SharedRunner()
        await self.run_groups_alone(alone)
        await self.run_pipeline(shared)
        measurements = {
            "properties": pdfinfo_argv(),
            "page boxes": page_boxes_argv(1),
            "text": layout_text_argv(),
            "qpdf objects": qpdf_objects_argv(),
            "MuPDF trace": mupdf_trace_argv(1),
            "fonts": font_inventory_argv(),
        }
        # Run alone, the groups repeat what they have in common.
        self.assertEqual(
            {name: alone.runs_of(argv) for name, argv in measurements.items()},
            {
                "properties": 5,
                "page boxes": 3,
                "text": 3,
                "qpdf objects": 3,
                "MuPDF trace": 2,
                "fonts": 1,
            },
        )
        # Shared, no read-only command runs twice; only the renders stay per group.
        repeated = {argv: count for argv, count in shared.commands().items() if count > 1}
        self.assertEqual(repeated, {})
        self.assertEqual(
            {name: shared.runs_of(argv) for name, argv in measurements.items()},
            dict.fromkeys(measurements, 1),
        )
        self.assertEqual(shared.freezes, 1)
        self.assertEqual(alone.freezes, 5)
        self.assertEqual(
            {"pdftoppm"}, {argv[0] for argv in shared.commands() if argv[0] == "pdftoppm"}
        )

    async def test_shared_findings_equal_the_findings_of_groups_run_alone(self) -> None:
        expected = await self.run_groups_alone(SharedRunner())
        found = await self.run_pipeline(SharedRunner())
        self.assertTrue(found)
        self.assertEqual([asdict(item) for item in found], [asdict(item) for item in expected])

    async def test_each_group_reports_the_tools_it_relied_on_even_when_it_reused_output(
        self,
    ) -> None:
        runner = SharedRunner()
        findings = await self.run_pipeline(runner)
        [tools] = [item for item in findings if item.rule == "pdf.tools"]
        # The inspection never relied on qpdf or mutool, which other groups ran for it.
        self.assertEqual(
            set(tools.details["tools"]),
            {"pdfinfo", "pdftotext", "pdffonts", "pdfdetach", "pdfimages"},
        )
        self.assertEqual(max(runner.probes().values()), 1)

    async def test_an_inspection_that_reuses_remembered_output_still_lists_its_tools(self) -> None:
        settings, budget = _ALL_GROUPS, ResourceBudget(2, 1024)
        runner = SharedRunner()
        with SharedPdfSession(self.pdf, self.work("shared"), runner, budget) as shared:
            # These groups run pdfinfo and pdftotext first, so the inspection reuses both.
            await inspect_pdf_details(
                self.pdf,
                self.work(),
                runner,
                options=settings.details,
                budget=budget,
                session=shared,
            )
            await inspect_pdf_structure(
                self.pdf, self.work(), runner, settings.structure, budget, session=shared
            )
            before = runner.commands()
            reused = await inspect_pdf(
                self.pdf,
                self.work(),
                runner,
                settings.max_pages,
                options=settings.inspection,
                budget=budget,
                session=shared,
            )
            ran = runner.commands() - before
        self.assertEqual({argv[0] for argv in ran}, {"pdffonts", "pdfdetach", "pdfimages"})
        alone = await inspect_pdf(
            self.pdf,
            self.work(),
            SharedRunner(),
            settings.max_pages,
            options=settings.inspection,
            budget=budget,
        )
        self.assertEqual([asdict(item) for item in reused], [asdict(item) for item in alone])
        [tools] = [item for item in reused if item.rule == "pdf.tools"]
        self.assertEqual(
            set(tools.details["tools"]),
            {"pdfinfo", "pdftotext", "pdffonts", "pdfdetach", "pdfimages"},
        )

    async def test_the_budget_and_the_workspace_are_clean_after_the_run(self) -> None:
        budget, work = ResourceBudget(2, 1024), self.work()
        await inspect_standalone_pdf(self.pdf, work, SharedRunner(), _ALL_GROUPS, budget)
        statistics = budget.statistics()
        self.assertEqual((statistics["active_cpu"], statistics["queued"]), (0, 0))
        self.assertFalse(list(work.rglob("*.pgm")))

    async def test_an_unreadable_pdf_is_frozen_once_and_every_group_reports_the_failure(
        self,
    ) -> None:
        self.pdf.write_bytes(b"not a pdf")
        runner = SharedRunner()
        findings = await self.run_pipeline(runner)
        unavailable = [item for item in findings if item.incomplete]
        self.assertGreater(len(unavailable), 10)
        # The privacy scan words its own cause; every other group names the missing header.
        self.assertEqual(
            {item.message for item in unavailable if "pdf_identity" not in item.rule},
            {"PDF input has no PDF header"},
        )
        self.assertEqual(runner.freezes, 1)
        self.assertEqual(runner.calls, [])

    async def test_a_failed_measurement_of_one_group_is_not_remembered_for_the_others(self) -> None:
        runner = SharedRunner(fail_once="qpdf")
        findings = await self.run_pipeline(runner)
        clean = await self.run_pipeline(SharedRunner())
        lost = [item for item in findings if item.incomplete]
        # Only the group that asked first lost the qpdf objects; the next group ran qpdf
        # again (the failed attempt is not in the record) and the third reused that output.
        self.assertEqual({item.message for item in lost}, {"The tool failed this once"})
        self.assertEqual(len(lost), 1)
        self.assertEqual(
            sum(count for argv, count in runner.commands().items() if argv[0] == "qpdf"), 1
        )
        self.assertEqual(
            [(item.rule, item.status) for item in findings if not item.incomplete],
            [(item.rule, item.status) for item in clean if item.rule != lost[0].rule],
        )

    async def test_cancelling_the_run_releases_the_budget_and_does_not_leave_the_pdf_open(
        self,
    ) -> None:
        budget = ResourceBudget(2, 1024)
        runner = SharedRunner(block=asyncio.Event())
        run = asyncio.create_task(
            inspect_standalone_pdf(self.pdf, self.work(), runner, _ALL_GROUPS, budget)
        )
        await asyncio.wait_for(runner.started.wait(), 1)
        run.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await run
        statistics = budget.statistics()
        self.assertEqual((statistics["active_cpu"], statistics["queued"]), (0, 0))
        # A later run of the same PDF is unaffected by the cancelled one.
        again = await inspect_standalone_pdf(
            self.pdf, self.work(), SharedRunner(), _ALL_GROUPS, ResourceBudget(2, 1024)
        )
        self.assertTrue(again)


class SharedSessionTests(SharedSessionCase):
    def shared(self, runner: SharedRunner | None = None) -> SharedPdfSession:
        return SharedPdfSession(self.pdf, self.work("shared"), runner or SharedRunner(), None)

    async def test_views_share_results_but_keep_their_own_directories_and_tool_records(
        self,
    ) -> None:
        runner = SharedRunner()
        text = ["pdftotext", DOCUMENT, "-"]
        with self.shared(runner) as shared:
            first = await shared.view(self.pdf, self.work("group"))
            second = await shared.view(self.pdf, self.work("group"))
            await first.measure("text", text, lambda result: result.stdout)
            await first.measure("same-name", pdfinfo_argv(), lambda result: result.stdout)
            # The same command name in another view cannot collide with the first view's.
            await second.measure("same-name", ["pdffonts", DOCUMENT], lambda result: result.stdout)
            await second.measure("text", text, lambda result: result.stdout)
            self.assertEqual(sum(1 for argv in runner.commands() if argv[0] == "pdftotext"), 1)
            self.assertEqual(sorted(first.tool_versions()), ["pdfinfo", "pdftotext"])
            self.assertEqual(sorted(second.tool_versions()), ["pdffonts", "pdftotext"])
            self.assertEqual(
                first.tool_versions()["pdftotext"], second.tool_versions()["pdftotext"]
            )
            self.assertTrue((first.work / "same-name").is_dir())
            self.assertTrue((second.work / "same-name").is_dir())
            self.assertEqual(runner.freezes, 1)

    async def test_a_view_needs_a_new_directory_and_the_pdf_it_was_opened_for(self) -> None:
        with self.shared() as shared:
            existing = self.work("existing")
            existing.mkdir()
            with self.assertRaisesRegex(PreparationError, "new, separate workspace"):
                await shared.view(self.pdf, existing)
            with self.assertRaisesRegex(ValueError, "different PDF"):
                await shared.view(self.root / "other.pdf", self.work())

    async def test_the_open_failure_is_remembered_and_raised_to_every_group(self) -> None:
        self.pdf.write_bytes(b"not a pdf")
        runner = SharedRunner()
        with self.shared(runner) as shared:
            messages = []
            for _ in range(3):
                with self.assertRaises(PreparationError) as raised:
                    await shared.view(self.pdf, self.work())
                messages.append(str(raised.exception))
        self.assertEqual(messages, ["PDF input has no PDF header"] * 3)
        self.assertEqual(runner.freezes, 1)

    async def test_a_cancelled_open_is_retried_by_the_next_group(self) -> None:
        runner = SharedRunner()
        runner.freeze_failure = asyncio.CancelledError()
        work = self.work("shared")
        shared = SharedPdfSession(self.pdf, work, runner, None)
        with self.assertRaises(asyncio.CancelledError):
            await shared.view(self.pdf, self.work())
        self.assertFalse(work.exists())
        view = await shared.view(self.pdf, self.work())
        self.assertEqual(await self.properties(view), runner.info)
        self.assertEqual(runner.freezes, 2)

    async def test_concurrent_groups_open_the_pdf_once(self) -> None:
        runner = SharedRunner()
        with self.shared(runner) as shared:
            views = await asyncio.gather(*(shared.view(self.pdf, self.work()) for _ in range(4)))
            self.assertEqual(len({view.inputs["document"] for view in views}), 1)
        self.assertEqual(runner.freezes, 1)

    async def test_a_cancelled_measurement_leaves_the_next_group_to_run_it(self) -> None:
        block = asyncio.Event()
        runner = SharedRunner(block=block)
        with self.shared(runner) as shared:
            first = await shared.view(self.pdf, self.work())
            second = await shared.view(self.pdf, self.work())
            cancelled = asyncio.create_task(self.properties(first))
            await asyncio.wait_for(runner.started.wait(), 1)
            waiting = asyncio.create_task(self.properties(second))
            await asyncio.sleep(0.01)
            cancelled.cancel()
            block.set()
            with self.assertRaises(asyncio.CancelledError):
                await cancelled
            self.assertEqual(await waiting, runner.info)
            self.assertEqual(sorted(second.tool_versions()), ["pdfinfo"])

    async def test_release_forgets_every_remembered_measurement(self) -> None:
        shared = self.shared()
        view = await shared.view(self.pdf, self.work())
        await self.properties(view)
        self.assertTrue(view.memo.results)
        shared.release()
        shared.release()
        self.assertFalse(view.memo.results)
        with self.assertRaisesRegex(RuntimeError, "released"):
            await shared.view(self.pdf, self.work())

    async def test_leaving_the_context_releases_even_after_a_failure(self) -> None:
        shared = self.shared()
        with self.assertRaises(ZeroDivisionError), shared:
            view = await shared.view(self.pdf, self.work())
            await self.properties(view)
            raise ZeroDivisionError
        self.assertFalse(view.memo.results)


if __name__ == "__main__":
    unittest.main()
