"""Multiple-package release barriers and preservation controls exercise actual workflow."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from latexprep.config import Settings, load_settings
from latexprep.core import JobRequest, run_job
from latexprep.models import Finding, PreparationError
from latexprep.pdf import compare_pdfs
from latexprep.workflow_options import (
    ComparisonOptions,
    DocumentOptions,
    WorkflowOptions,
    stage_document,
)
from tests.test_pdf import FakePdfRunner


def project(root: Path, main: str = "main.tex") -> None:
    root.mkdir(parents=True, exist_ok=True)
    (root / main).write_text("\\documentclass{article}\n\\begin{document}Hello.\\end{document}\n")


async def build(tree, main, work, engine, runner):
    work.mkdir(parents=True)
    pdf = work / "built.pdf"
    pdf.write_bytes(b"%PDF-controlled-test\n")
    return SimpleNamespace(
        success=True,
        pdf=pdf,
        findings=[],
        dependencies={main},
        submission_inputs={main},
        tools={},
        command=[],
    )


class MultiDocumentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "input"
        project(self.source / "paper")
        project(self.source / "supplement", "extra.tex")
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

        async def observed(tree, main, work, engine, runner):
            visits.append((main, work.name, sorted(path.name for path in tree.iterdir())))
            return await build(tree, main, work, engine, runner)

        with (
            patch("latexprep.core.build_project", side_effect=observed),
            patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
            patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[])),
        ):
            report = await run_job(
                JobRequest("prepare", self.source, self.settings, self.base / "out")
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
        async def failure(tree, main, work, engine, runner):
            result = await build(tree, main, work, engine, runner)
            if main == "extra.tex" and work.name == "archive-build":
                result.success = False
                result.pdf = None
            return result

        with (
            patch("latexprep.core.build_project", side_effect=failure),
            patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
            patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[])),
        ):
            report = await run_job(
                JobRequest("prepare", self.source, self.settings, self.base / "out")
            )
        self.assertEqual(report.outcome, "blocked")
        self.assertFalse((self.base / "out").exists())
        self.assertTrue(
            any(item.code == "PKG301" and item.status == "failed" for item in report.findings)
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
        project(self.source)
        self.settings = Settings(
            workflow=WorkflowOptions(baseline_runs=2, comparison=ComparisonOptions(1, 0.01))
        )

    async def test_repeat_build_difference_blocks_publication(self):
        with (
            patch("latexprep.core.build_project", side_effect=build),
            patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
            patch(
                "latexprep.core.compare_pdfs",
                new=AsyncMock(return_value=[Finding("compare.text", "Changed", "error")]),
            ),
        ):
            report = await run_job(
                JobRequest("prepare", self.source, self.settings, self.base / "out")
            )
        self.assertEqual(report.outcome, "blocked")
        self.assertFalse((self.base / "out").exists())
        self.assertTrue(
            any(item.code == "CMP101" and item.status == "failed" for item in report.findings)
        )

    async def test_identical_repeats_pass_and_do_not_relax_archive_comparison(self):
        compare = AsyncMock(return_value=[])
        with (
            patch("latexprep.core.build_project", side_effect=build),
            patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
            patch("latexprep.core.compare_pdfs", new=compare),
        ):
            report = await run_job(
                JobRequest("prepare", self.source, self.settings, self.base / "out")
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
