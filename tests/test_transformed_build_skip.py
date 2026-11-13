"""The transformed rebuild is skipped when staging changed nothing, and only then.

A preparation rebuilds the staged copy of the sources after the selected transformations. When
the staged copy is byte for byte the tree it was copied from, that build would repeat the
baseline, so the baseline stands in: its evidence and PDF carry over, the stage is recorded as
skipped, and a preserved layout does not compare the baseline PDF with itself. The archive
rebuild always runs and always has to match.
"""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock

from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.findings import Finding
from latexprep.job_context import JobRequest
from latexprep.source_transform import SourceTransformOptions
from latexprep.staging import trees_identical
from latexprep.workflow_options import DocumentOptions, WorkflowOptions
from tests.support import controlled_backends, fake_build, minimal_project

COMMENTED = (
    "\\documentclass{article}\n% private TODO note\n\\begin{document}Hello.\\end{document}\n"
)


class TreeIdentityTests(unittest.TestCase):
    """When two trees count as identical."""

    def setUp(self) -> None:
        """Create two trees holding the same two files."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.first, self.second = (
            Path(self.temporary.name) / "first",
            Path(self.temporary.name) / "second",
        )
        for root in (self.first, self.second):
            (root / "sections").mkdir(parents=True)
            (root / "main.tex").write_text("main\n")
            (root / "sections/one.tex").write_text("one\n")

    def test_equal_trees_are_identical(self) -> None:
        self.assertTrue(trees_identical(self.first, self.second))

    def test_a_changed_byte_is_a_difference(self) -> None:
        (self.second / "sections/one.tex").write_text("One\n")
        self.assertFalse(trees_identical(self.first, self.second))

    def test_a_missing_or_extra_file_is_a_difference(self) -> None:
        (self.second / "extra.tex").write_text("extra\n")
        self.assertFalse(trees_identical(self.first, self.second))
        (self.second / "extra.tex").unlink()
        (self.second / "sections/one.tex").unlink()
        self.assertFalse(trees_identical(self.first, self.second))

    def test_an_extra_folder_is_a_difference(self) -> None:
        (self.second / "empty").mkdir()
        self.assertFalse(trees_identical(self.first, self.second))

    def test_a_link_in_either_tree_is_never_identical(self) -> None:
        (self.first / "link.tex").symlink_to(self.first / "main.tex")
        (self.second / "link.tex").symlink_to(self.second / "main.tex")
        self.assertFalse(trees_identical(self.first, self.second))
        self.assertFalse(trees_identical(self.second, self.first))

    def test_files_of_equal_size_are_compared_by_content(self) -> None:
        (self.second / "main.tex").write_text("mine\n")
        self.assertFalse(trees_identical(self.first, self.second))


class TransformedBuildSkipTests(unittest.IsolatedAsyncioTestCase):
    """Which builds a ``prepare`` job runs, and what its report says about the others."""

    async def asyncSetUp(self) -> None:
        """Write a minimal project to ``input`` in a temporary directory."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "input"
        minimal_project(self.source)
        self.builds: list[str] = []
        self.compared: list[str] = []

    async def observed_build(self, tree, main, work, engine, runner, **options):
        """Record the build's workspace name, then build as the controlled backend does."""
        self.builds.append(f"{main}:{work.name}")
        return await fake_build(tree, main, work, engine, runner, **options)

    async def observed_comparison(self, _left, _right, work, *_args, **_options):
        """Record the comparison's workspace name; it finds nothing."""
        self.compared.append(work.name)
        return []

    async def prepare(self, settings: Settings):
        """Run ``prepare`` with the recording backends and return the report."""
        backends = controlled_backends(
            build=self.observed_build, compare_pdfs=self.observed_comparison
        )
        return await run_job(
            JobRequest("prepare", self.source, settings, self.base / "out"), backends=backends
        )

    def stage(self, report, name: str) -> dict:
        """Return the report's stage called ``name``."""
        (stage,) = [item for item in report.stages if item["name"] == name]
        return stage

    async def test_unchanged_staging_reuses_the_baseline_build(self) -> None:
        report = await self.prepare(Settings())
        self.assertEqual(report.outcome, "passed", report.to_dict())
        self.assertEqual(self.builds, ["main.tex:baseline", "main.tex:archive-build"])
        self.assertEqual(
            self.stage(report, "transformed"),
            {
                "name": "transformed",
                "status": "skipped",
                "elapsed_seconds": 0.0,
                "queue_seconds": 0.0,
            },
        )
        self.assertEqual(self.stage(report, "baseline")["status"], "succeeded")
        self.assertEqual(self.stage(report, "archive")["status"], "succeeded")
        with zipfile.ZipFile(self.base / "out/submission.zip") as archive:
            self.assertEqual(archive.namelist(), ["main.tex"])

    async def test_the_baseline_pdf_is_not_compared_with_itself(self) -> None:
        await self.prepare(Settings())
        self.assertEqual(self.compared, ["compare-archive"])

    async def test_a_change_to_the_sources_builds_the_transformed_copy_and_compares_it(
        self,
    ) -> None:
        (self.source / "main.tex").write_text(COMMENTED)
        settings = Settings(source_transforms=SourceTransformOptions(comment_policy="private"))
        report = await self.prepare(settings)
        self.assertEqual(report.outcome, "passed", report.to_dict())
        self.assertEqual(
            self.builds,
            ["main.tex:baseline", "main.tex:transformed-build", "main.tex:archive-build"],
        )
        self.assertEqual(self.stage(report, "transformed")["status"], "succeeded")
        self.assertEqual(self.compared, ["compare-prepared", "compare-archive"])
        with zipfile.ZipFile(self.base / "out/submission.zip") as archive:
            self.assertNotIn("private TODO", archive.read("main.tex").decode())

    async def test_the_flat_layout_rebuilds_the_flattened_sources_without_a_transformed_build(
        self,
    ) -> None:
        report = await self.prepare(Settings(layout="flat"))
        self.assertEqual(report.outcome, "passed", report.to_dict())
        self.assertEqual(
            self.builds, ["main.tex:baseline", "main.tex:staged-build", "main.tex:archive-build"]
        )
        self.assertEqual(self.stage(report, "transformed")["status"], "skipped")
        self.assertEqual(self.stage(report, "prepared")["status"], "succeeded")
        self.assertEqual(self.compared, ["compare-prepared", "compare-archive"])

    async def test_the_archive_rebuild_still_decides_the_outcome(self) -> None:
        changed = Finding("compare.rendering", "Rendered pixels changed", severity="error")
        failure = AsyncMock(side_effect=[[changed]])
        backends = controlled_backends(compare_pdfs=failure)
        report = await run_job(
            JobRequest("prepare", self.source, Settings(), self.base / "out"), backends=backends
        )
        self.assertEqual(report.outcome, "blocked")
        self.assertEqual(failure.await_count, 1)
        self.assertFalse((self.base / "out").exists())

    async def test_a_dry_run_builds_nothing_beyond_the_baseline(self) -> None:
        backends = controlled_backends(build=self.observed_build)
        report = await run_job(
            JobRequest("prepare", self.source, Settings(), self.base / "out", dry_run=True),
            backends=backends,
        )
        self.assertEqual(report.outcome, "planned")
        self.assertEqual(self.builds, ["main.tex:baseline"])
        self.assertNotIn("transformed", {item["name"] for item in report.stages})

    async def test_each_document_decides_for_itself(self) -> None:
        documents = self.base / "documents"
        minimal_project(documents / "paper")
        (documents / "paper/main.tex").write_text(COMMENTED)
        minimal_project(documents / "supplement", "extra.tex")
        settings = Settings(
            source_transforms=SourceTransformOptions(comment_policy="private"),
            workflow=WorkflowOptions(
                documents=(
                    DocumentOptions("paper", "main.tex", "paper"),
                    DocumentOptions("supplement", "extra.tex", "supplement"),
                )
            ),
        )
        backends = controlled_backends(build=self.observed_build)
        report = await run_job(
            JobRequest("prepare", documents, settings, self.base / "documents-out"),
            backends=backends,
        )
        self.assertEqual(report.outcome, "passed", report.to_dict())
        stages = {item["name"]: item["status"] for item in report.stages}
        self.assertEqual(stages["paper: transformed"], "succeeded")
        self.assertEqual(stages["supplement: transformed"], "skipped")
        self.assertIn("main.tex:transformed-build", self.builds)
        self.assertNotIn("extra.tex:transformed-build", self.builds)
