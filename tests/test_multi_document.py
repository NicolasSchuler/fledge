"""Multiple-package release barriers and preservation controls exercise actual workflow."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from latexprep.config import Settings, load_settings
from latexprep.core import run_job
from latexprep.defects import REPORT_DEFECT_SUGGESTION
from latexprep.errors import PreparationError, RequestError
from latexprep.findings import Finding
from latexprep.job_context import JobRequest
from latexprep.main_selection import main_selection_error
from latexprep.pdf_comparison import compare_pdfs
from latexprep.staging import stage_document
from latexprep.workflow_options import ComparisonOptions, DocumentOptions, WorkflowOptions
from tests.pdf_fakes import FakePdfRunner
from tests.support import controlled_backends, fake_build, minimal_project


class MultiDocumentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "input"
        minimal_project(self.source / "paper")
        minimal_project(self.source / "supplement", "extra.tex")
        self.settings = Settings(
            workflow=WorkflowOptions(
                documents=(
                    DocumentOptions("paper", "main.tex", "paper"),
                    DocumentOptions("supplement", "extra.tex", "supplement"),
                )
            )
        )

    async def test_every_document_rebuilds_own_archive_and_releases_together(self):
        visits = []

        async def observed(tree, main, work, engine, runner, **_options):
            visits.append((main, work.name, sorted(path.name for path in tree.iterdir())))
            return await fake_build(tree, main, work, engine, runner)

        backends = controlled_backends(build=observed)
        report = await run_job(
            JobRequest("prepare", self.source, self.settings, self.base / "out"), backends=backends
        )
        self.assertEqual(report.outcome, "passed", report.to_dict())
        self.assertEqual(len(visits), 6)
        for name, main in (("paper", "main.tex"), ("supplement", "extra.tex")):
            with zipfile.ZipFile(self.base / "out" / name / "submission.zip") as archive:
                self.assertEqual(archive.namelist(), [main])
            self.assertEqual(
                sum(visit[0] == main and visit[1] == "archive-build" for visit in visits), 1
            )
        self.assertTrue(
            any(item.code == "PKG301" and item.status == "passed" for item in report.findings)
        )
        self.assertTrue((self.base / "out/report.json").is_file())

    async def test_one_failed_document_prevents_all_publication(self):
        async def failure(tree, main, work, engine, runner, **_options):
            result = await fake_build(tree, main, work, engine, runner)
            if main == "extra.tex" and work.name == "archive-build":
                result.success = False
                result.pdf = None
            return result

        backends = controlled_backends(build=failure)
        report = await run_job(
            JobRequest("prepare", self.source, self.settings, self.base / "out"), backends=backends
        )
        self.assertEqual(report.outcome, "blocked")
        self.assertFalse((self.base / "out").exists())
        self.assertTrue(
            any(item.code == "PKG301" and item.status == "failed" for item in report.findings)
        )

    async def test_single_purpose_commands_ignore_configured_documents(self):
        pdf = self.base / "paper.pdf"
        pdf.write_bytes(b"%PDF-fixture")
        formatted = SimpleNamespace(findings=[], tools={}, changes=[])
        with (
            patch(
                "latexprep.standalone_checks.inspect_standalone_pdf",
                new=AsyncMock(return_value=[]),
            ),
            patch(
                "latexprep.standalone_checks.format_project", new=AsyncMock(return_value=formatted)
            ),
        ):
            for command, source in (("bib", self.source), ("fmt", self.source), ("pdf", pdf)):
                with self.subTest(command=command):
                    report = await run_job(JobRequest(command, source, self.settings))
                    self.assertEqual(report.command, command)
                    self.assertNotIn(report.outcome, {"error", "blocked"})
                    self.assertFalse(
                        any(
                            item.rule == "package.independent_documents" for item in report.findings
                        )
                    )

    async def test_reference_pdf_with_documents_is_rejected_before_the_workspace_exists(self):
        reference = self.base / "reference.pdf"
        reference.write_bytes(b"%PDF-fixture")
        request = JobRequest("check", self.source, self.settings, reference_pdf=reference)
        with (
            patch("latexprep.core.JobExecution") as execution,
            self.assertRaises(RequestError) as raised,
        ):
            await run_job(request)
        execution.assert_not_called()
        self.assertIn("workflow.documents", raised.exception.suggestion)
        self.assertIn("reference_pdf", raised.exception.suggestion)

    async def test_failed_document_names_its_own_settings_not_the_command_line(self):
        failure = main_selection_error("main.tex", ["a.tex"])
        self.assertIn("--main", failure.suggestion)
        with patch("latexprep.documents.run_project", side_effect=failure):
            report = await run_job(JobRequest("check", self.source, self.settings))
        failed = [item for item in report.findings if item.rule == "package.document"]
        self.assertEqual({item.document for item in failed}, {"paper", "supplement"})
        for item in failed:
            self.assertIn("workflow.documents", item.suggestion)
            self.assertNotIn("--main", item.suggestion)
        self.assertEqual(report.outcome, "blocked")

    async def test_a_defect_in_one_document_is_reported_as_a_defect(self):
        with patch("latexprep.documents.run_project", side_effect=ValueError("unexpected value")):
            report = await run_job(JobRequest("check", self.source, self.settings))
        [item, *_] = [item for item in report.findings if item.rule == "package.document"]
        self.assertIn("ValueError: unexpected value", item.message)
        self.assertEqual(item.suggestion, REPORT_DEFECT_SUGGESTION)
        self.assertTrue(item.details["traceback"])
        self.assertTrue(all("/" not in frame for frame in item.details["traceback"]))
        self.assertEqual(report.outcome, "blocked")

    async def test_unrepresentable_multi_document_report_is_refused_without_output(self):
        output = self.base / "out?token=findings&"
        backends = controlled_backends()
        report = await run_job(
            JobRequest("prepare", self.source, self.settings, output), backends=backends
        )
        self.assertEqual(report.outcome, "error")
        self.assertFalse(output.exists())
        self.assertEqual(report.artifacts, {})
        self.assertIn("report.redaction_limit", {item.rule for item in report.findings})

    async def test_a_blocked_preparation_says_why_nothing_was_published(self):
        async def failure(tree, main, work, engine, runner, **_options):
            result = await fake_build(tree, main, work, engine, runner)
            if main == "extra.tex" and work.name == "archive-build":
                result.success = False
                result.pdf = None
            return result

        messages: list[str] = []
        backends = controlled_backends(build=failure)
        await run_job(
            JobRequest("prepare", self.source, self.settings, self.base / "out"),
            messages.append,
            backends=backends,
        )
        self.assertIn(
            "No output was published: the job did not produce a verified package", messages
        )

    async def test_explicit_selection_must_include_root_and_match_all_patterns(self):
        for patterns in (("*.bib",), ("main.tex", "absent.*")):
            with self.subTest(patterns=patterns), self.assertRaises(PreparationError):
                stage_document(
                    self.source,
                    self.base / "selected",
                    DocumentOptions("paper", "main.tex", "paper", patterns),
                )
        self.assertFalse((self.base / "selected").exists())
        staged = stage_document(
            self.source,
            self.base / "selected",
            DocumentOptions("paper", "main.tex", "paper", ("main.tex",)),
        )
        self.assertEqual([path.name for path in staged.iterdir()], ["main.tex"])

    async def test_configuration_rejects_ambiguous_names_or_root_selection(self):
        with self.assertRaises(PreparationError):
            WorkflowOptions(
                documents=(
                    DocumentOptions("paper", "main.tex"),
                    DocumentOptions("PAPER", "other.tex"),
                )
            )
        with self.assertRaises(PreparationError):
            load_settings(None, {"main": "main.tex", "workflow": self.settings.workflow})
        for path in ("../escape", "/etc", "dir\\nested"):
            with self.subTest(path=path), self.assertRaises(PreparationError):
                DocumentOptions("paper", "main.tex", path)


class BaselineStabilityTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "input"
        minimal_project(self.source)
        self.settings = Settings(
            workflow=WorkflowOptions(baseline_runs=2, comparison=ComparisonOptions(1, 0.01))
        )

    async def test_repeat_build_difference_blocks_publication(self):
        backends = controlled_backends(
            compare_pdfs=[Finding("compare.text", "Changed", severity="error")]
        )
        report = await run_job(
            JobRequest("prepare", self.source, self.settings, self.base / "out"), backends=backends
        )
        self.assertEqual(report.outcome, "blocked")
        self.assertFalse((self.base / "out").exists())
        self.assertTrue(
            any(item.code == "CMP101" and item.status == "failed" for item in report.findings)
        )

    async def test_identical_repeats_pass_and_do_not_relax_archive_comparison(self):
        compare = AsyncMock(return_value=[])
        backends = controlled_backends(compare_pdfs=compare)
        report = await run_job(
            JobRequest("prepare", self.source, self.settings, self.base / "out"), backends=backends
        )
        self.assertEqual(report.outcome, "passed", report.to_dict())
        self.assertEqual(compare.await_count, 3)
        self.assertIsNone(compare.call_args_list[0].kwargs.get("options"))
        self.assertEqual(
            compare.call_args_list[1].kwargs["options"], self.settings.workflow.comparison
        )
        self.assertIsNone(compare.call_args_list[2].kwargs.get("options"))
        self.assertTrue(
            any(item.code == "CMP101" and item.status == "passed" for item in report.findings)
        )

    async def test_every_build_stage_records_its_time(self):
        backends = controlled_backends()
        report = await run_job(
            JobRequest("prepare", self.source, self.settings, self.base / "out"), backends=backends
        )
        stages = {stage["name"]: stage for stage in report.stages}
        for name in ("baseline", "baseline repeat 2", "transformed", "archive"):
            with self.subTest(stage=name):
                self.assertGreaterEqual(stages[name]["elapsed_seconds"], 0)
                self.assertGreaterEqual(stages[name]["queue_seconds"], 0)

    async def test_a_crashed_baseline_build_keeps_the_baseline_stage_name(self):
        async def crash(*_args, **_options):
            raise RuntimeError("tool crashed")

        backends = controlled_backends(build=crash)
        report = await run_job(JobRequest("check", self.source, self.settings), backends=backends)
        stages = {stage["name"]: stage["status"] for stage in report.stages}
        self.assertEqual(stages["baseline"], "failed")
        self.assertNotIn("baseline build", stages)

    async def test_pixel_tolerance_boundary_preserves_text_check(self):
        left, right = self.base / "a.pdf", self.base / "b.pdf"
        left.write_bytes(b"%PDF-left")
        right.write_bytes(b"%PDF-right")
        for index, (options, status) in enumerate(
            (
                (ComparisonOptions(254, 0), "failed"),
                (ComparisonOptions(255, 0), "passed"),
                (ComparisonOptions(0, 1), "passed"),
            )
        ):
            findings = await compare_pdfs(
                left,
                right,
                self.base / f"comparison-{index}",
                FakePdfRunner(raster_changed=True, text_changed=True),
                options=options,
            )
            by_rule = {item.rule: item for item in findings}
            self.assertEqual(by_rule["compare.rendering"].status, status)
            self.assertEqual(by_rule["compare.text"].status, "failed")
            self.assertEqual(
                by_rule["compare.rendering"].details["channel_tolerance"], options.channel_tolerance
            )


if __name__ == "__main__":
    unittest.main()
