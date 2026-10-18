"""A job takes its build, PDF inspection and PDF comparison from injectable backends."""

from __future__ import annotations

import unittest
from dataclasses import FrozenInstanceError
from unittest.mock import AsyncMock

from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.findings import Finding
from latexprep.job_context import DocumentBackends, JobRequest
from latexprep.latexmk_build import build_project
from latexprep.pdf_comparison import compare_pdfs
from latexprep.pdf_inspection import inspect_pdf
from latexprep.workflow_options import DocumentOptions, WorkflowOptions
from tests.support import (
    AsyncProjectTestCase,
    controlled_backends,
    fake_build,
    minimal_project,
    write_file,
)


class DocumentBackendsTests(unittest.TestCase):
    def test_the_defaults_are_the_operations_that_drive_the_real_tools(self) -> None:
        backends = DocumentBackends()
        self.assertIs(backends.build, build_project)
        self.assertIs(backends.inspect_pdf, inspect_pdf)
        self.assertIs(backends.compare_pdfs, compare_pdfs)

    def test_backends_are_immutable_so_a_job_cannot_swap_them_midway(self) -> None:
        with self.assertRaises(FrozenInstanceError):
            DocumentBackends().build = fake_build


class ControlledBackendsTests(AsyncProjectTestCase):
    async def test_defaults_build_successfully_and_find_nothing(self) -> None:
        backends = controlled_backends()
        result = await backends.build(
            self.root / "tree", "main.tex", self.root / "work", "pdflatex", None
        )
        self.assertTrue(result.success)
        self.assertEqual(await backends.inspect_pdf(self.root / "a.pdf"), [])
        self.assertEqual(await backends.compare_pdfs(self.root / "a.pdf", self.root / "b.pdf"), [])

    async def test_values_exceptions_and_functions_are_each_accepted(self) -> None:
        finding = Finding("pdf.example", "Example", severity="info", status="passed")
        calls: list[int] = []

        async def counting(*_arguments: object, **_options: object) -> list[Finding]:
            calls.append(1)
            return [finding]

        backends = controlled_backends(
            inspect_pdf=[finding], compare_pdfs=counting, build=RuntimeError("no build")
        )
        self.assertEqual(await backends.inspect_pdf(), [finding])
        # A returned list is copied, so one job cannot change what the next one receives.
        first = await backends.inspect_pdf()
        first.append(finding)
        self.assertEqual(await backends.inspect_pdf(), [finding])
        self.assertEqual(await backends.compare_pdfs(), [finding])
        self.assertEqual(calls, [1])
        with self.assertRaisesRegex(RuntimeError, "no build"):
            await backends.build()


class JobBackendsTests(AsyncProjectTestCase):
    async def test_a_check_builds_once_and_inspects_the_pdf_it_built(self) -> None:
        source = self.root / "input"
        minimal_project(source)
        build, inspect = AsyncMock(side_effect=fake_build), AsyncMock(return_value=[])
        compare = AsyncMock(return_value=[])
        report = await run_job(
            JobRequest("check", source, Settings()),
            backends=DocumentBackends(build=build, inspect_pdf=inspect, compare_pdfs=compare),
        )
        self.assertEqual(report.outcome, "passed")
        build.assert_awaited_once()
        tree, main, work, engine, _runner = build.await_args.args
        self.assertEqual((main, engine), ("main.tex", "pdflatex"))
        self.assertEqual(work.name, "baseline")
        self.assertNotEqual(tree, source, "the job builds its own snapshot, not the input")
        inspect.assert_awaited_once()
        self.assertEqual(inspect.await_args.args[0].name, "built.pdf")
        compare.assert_not_awaited()

    async def test_a_reference_pdf_is_compared_with_the_baseline_by_the_backend(self) -> None:
        source = self.root / "input"
        minimal_project(source)
        reference = write_file(self.root, "reference.pdf", b"%PDF-reference")
        compared: list[tuple[bytes, str]] = []

        async def compare(left, right, work, runner, **options):
            # The job compares copies it imported into its own workspace.
            compared.append((left.read_bytes(), right.name))
            return []

        await run_job(
            JobRequest("check", source, Settings(), reference_pdf=reference),
            backends=controlled_backends(compare_pdfs=compare),
        )
        self.assertEqual(compared, [(b"%PDF-reference", "built.pdf")])

    async def test_every_document_of_a_multi_document_job_uses_the_same_backends(self) -> None:
        source = self.root / "input"
        minimal_project(source / "paper")
        minimal_project(source / "supplement", "extra.tex")
        settings = Settings(
            workflow=WorkflowOptions(
                documents=(
                    DocumentOptions("paper", "main.tex", "paper"),
                    DocumentOptions("supplement", "extra.tex", "supplement"),
                )
            )
        )
        mains: list[str] = []

        async def build(tree, main, work, engine, runner, **options):
            mains.append(main)
            return await fake_build(tree, main, work, engine, runner, **options)

        await run_job(
            JobRequest("check", source, settings), backends=controlled_backends(build=build)
        )
        self.assertEqual(sorted(mains), ["extra.tex", "main.tex"])


if __name__ == "__main__":
    unittest.main()
