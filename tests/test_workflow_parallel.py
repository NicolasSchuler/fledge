"""Behavioral contracts for dependency overlap and the final publication barrier."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from latexprep.config import Settings
from latexprep.core import JobRequest, _apply_contents, run_job
from latexprep.models import Finding, PreparationError, Report


def project(root: Path) -> None:
    root.mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nHello.\\end{document}\n"
    )


async def build(tree, main, work, engine, runner):
    work.mkdir()
    pdf = work / "built.pdf"
    pdf.write_bytes(b"%PDF-1.4\ncontrolled orchestration fixture\n")
    return SimpleNamespace(
        success=True, pdf=pdf, findings=[], dependencies={"main.tex"}, tools={}, command=[]
    )


class ParallelWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_during_publication_removes_only_new_output(self):
        report = Report("prepare")
        copytree = shutil.copytree
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, output = base / "input", base / "output"
            project(source)

            async def ready(_request, _settings, work, *_args):
                prepared = work / "prepared"
                project(prepared)
                archive, pdf = work / "submission.zip", work / "built.pdf"
                archive.write_bytes(b"archive fixture")
                pdf.write_bytes(b"PDF fixture")
                report.outcome = "passed"
                return prepared, archive, pdf

            def cancel_copy(src, dst, *args, **kwargs):
                result = copytree(src, dst, *args, **kwargs)
                if dst.resolve() == (output / "sources").resolve():
                    asyncio.current_task().cancel()
                return result

            with (
                patch("latexprep.core._run_in_workspace", side_effect=ready),
                patch("latexprep.core.shutil.copytree", side_effect=cancel_copy),
            ):
                task = asyncio.create_task(
                    run_job(JobRequest("prepare", source, Settings(), output), report=report)
                )
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertFalse(output.exists())
            self.assertTrue((source / "main.tex").is_file())
        self.assertEqual(report.outcome, "cancelled")
        self.assertEqual(report.artifacts, {})

    async def test_baseline_pdf_starts_before_independent_analysis_finishes(self):
        started, release = threading.Event(), threading.Event()
        inspected = asyncio.Event()

        def bibliography(_root):
            started.set()
            if not release.wait(5):
                raise AssertionError("PDF branch did not release the independent analysis")
            return [
                Finding("bib.test_evidence", "Completed independent analysis", "info", "passed")
            ]

        async def baseline(*args):
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            return await build(*args)

        async def inspect(*_args, **_kwargs):
            self.assertFalse(release.is_set())
            inspected.set()
            release.set()
            return []

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            project(source)
            try:
                with (
                    patch("latexprep.core.check_bibliography", side_effect=bibliography),
                    patch("latexprep.core.build_project", side_effect=baseline),
                    patch("latexprep.core.inspect_pdf", side_effect=inspect),
                ):
                    report = await asyncio.wait_for(
                        run_job(JobRequest("check", source, Settings(jobs=3))), 8
                    )
            finally:
                release.set()
        self.assertTrue(inspected.is_set())
        self.assertEqual(report.outcome, "passed")
        self.assertTrue(any(f.rule == "bib.test_evidence" for f in report.findings))
        self.assertEqual(report.execution["resources"]["active_cpu"], 0)

    async def test_final_branches_overlap_and_all_finish_before_publication(self):
        final_inspect, archive_compare, reference_compare = (asyncio.Event() for _ in range(3))
        release = asyncio.Event()
        reference_paths = []
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, output = base / "input", base / "output"
            project(source)
            reference = base / "reference.pdf"
            reference.write_bytes(b"original reference")

            async def inspect(_pdf, work, *_args, **_kwargs):
                if work.name == "inspect-final":
                    final_inspect.set()
                    await release.wait()
                return []

            async def compare(left, _right, work, *_args, **_kwargs):
                if work.name.startswith("reference-"):
                    reference_paths.append(left)
                    self.assertNotEqual(left, reference)
                    self.assertEqual(left.read_bytes(), b"original reference")
                    reference.write_bytes(b"changed by user during job")
                if work.name == "compare-archive":
                    archive_compare.set()
                    await release.wait()
                elif work.name == "reference-final":
                    reference_compare.set()
                    await release.wait()
                return []

            with (
                patch("latexprep.core.build_project", side_effect=build),
                patch("latexprep.core.inspect_pdf", side_effect=inspect),
                patch("latexprep.core.compare_pdfs", side_effect=compare),
            ):
                task = asyncio.create_task(
                    run_job(
                        JobRequest(
                            "prepare", source, Settings(jobs=4), output, reference_pdf=reference
                        )
                    )
                )
                try:
                    await asyncio.wait_for(
                        asyncio.gather(
                            final_inspect.wait(), archive_compare.wait(), reference_compare.wait()
                        ),
                        5,
                    )
                    self.assertFalse(output.exists())
                    self.assertFalse(task.done())
                    release.set()
                    report = await task
                finally:
                    release.set()
                    if not task.done():
                        task.cancel()
                        await asyncio.gather(task, return_exceptions=True)
            self.assertEqual(report.outcome, "passed")
            self.assertTrue((output / "submission.zip").is_file())
            self.assertEqual(len(reference_paths), 2)
            self.assertEqual(reference_paths[0], reference_paths[1])
            self.assertEqual(reference.read_bytes(), b"changed by user during job")

    async def test_failed_artifact_blocks_pdf_consumers_and_retains_build_evidence(self):
        failed = SimpleNamespace(
            success=False,
            pdf=None,
            findings=[Finding("build.test", "Build diagnostic", "warning")],
            dependencies=set(),
            tools={},
            command=[],
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            project(source)
            with (
                patch("latexprep.core.build_project", new=AsyncMock(return_value=failed)),
                patch("latexprep.core.inspect_pdf", new=AsyncMock()) as inspect,
            ):
                report = await run_job(JobRequest("check", source, Settings()))
        inspect.assert_not_called()
        self.assertEqual(report.outcome, "blocked")
        self.assertTrue(any(f.rule == "build.test" for f in report.findings))
        stages = {stage["name"]: stage["status"] for stage in report.stages}
        self.assertEqual(stages["baseline PDF inspection"], "blocked")
        self.assertEqual(stages["bibliography checks"], "succeeded")

    async def test_deadline_drains_tasks_preserves_evidence_and_never_publishes(self):
        stopped = asyncio.Event()

        async def hang(*_args):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, output = base / "input", base / "output"
            project(source)
            (source / "main.tex").write_text(
                (source / "main.tex").read_text().replace("Hello.", "TODO finish.")
            )
            with patch("latexprep.core.build_project", side_effect=hang):
                report = await run_job(
                    JobRequest("prepare", source, Settings(job_timeout_seconds=1), output)
                )
            self.assertFalse(output.exists())
        self.assertTrue(stopped.is_set())
        self.assertEqual(report.outcome, "blocked")
        self.assertTrue(any(f.rule == "execution.deadline" for f in report.findings))
        self.assertTrue(any(f.code == "TEX001" for f in report.findings))
        self.assertEqual(report.execution["resources"]["active_cpu"], 0)
        self.assertEqual(report.execution["resources"]["queued"], 0)

    async def test_repeated_cancellation_keeps_workspace_until_children_stop(self):
        started, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
        observed_work = []
        report = Report("prepare")

        async def delayed_build(_tree, _main, work, *_args):
            observed_work.append(work.parent)
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaning.set()
                await release.wait()
                self.assertTrue(work.parent.is_dir())
                cleaned.set()

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, output = base / "input", base / "output"
            project(source)
            with patch("latexprep.core.build_project", side_effect=delayed_build):
                task = asyncio.create_task(
                    run_job(JobRequest("prepare", source, Settings(), output), report=report)
                )
                await asyncio.wait_for(started.wait(), 5)
                task.cancel()
                await asyncio.wait_for(cleaning.wait(), 5)
                task.cancel()
                self.assertTrue(observed_work[0].is_dir())
                release.set()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            self.assertFalse(output.exists())
        self.assertTrue(cleaned.is_set())
        self.assertFalse(observed_work[0].exists())
        self.assertEqual(report.outcome, "cancelled")
        self.assertEqual(report.execution["resources"]["active_cpu"], 0)

    async def test_job_disk_limit_blocks_publication_even_without_real_tool_commands(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, output = base / "input", base / "output"
            project(source)
            with (
                patch("latexprep.core.build_project", side_effect=build),
                patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
                patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[])),
            ):
                report = await run_job(
                    JobRequest("prepare", source, Settings(max_temporary_bytes=1), output)
                )
            self.assertFalse(output.exists())
        self.assertEqual(report.outcome, "blocked")
        self.assertTrue(any(f.rule == "execution.resources" for f in report.findings))


class FormattingApplicationTests(unittest.TestCase):
    def test_all_preconditions_checked_before_first_source_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.tex").write_bytes(b"original a")
            (root / "b.tex").write_bytes(b"changed b")
            with self.assertRaisesRegex(PreparationError, "Source changed"):
                _apply_contents(
                    root,
                    {"a.tex": b"formatted a", "b.tex": b"formatted b"},
                    originals={"a.tex": b"original a", "b.tex": b"original b"},
                )
            self.assertEqual((root / "a.tex").read_bytes(), b"original a")
            self.assertEqual((root / "b.tex").read_bytes(), b"changed b")

    def test_missing_originals_or_invalid_targets_never_apply_partial_proposals(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.tex").write_bytes(b"original")
            with self.assertRaises(PreparationError):
                _apply_contents(root, {"a.tex": b"new"}, originals={})
            with self.assertRaises(PreparationError):
                _apply_contents(root, {"a.tex": b"new", "missing.tex": b"new"})
            self.assertEqual((root / "a.tex").read_bytes(), b"original")
