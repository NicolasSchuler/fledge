"""Behavioral contracts for dependency overlap and the final publication barrier."""

from __future__ import annotations

import asyncio
import json
import shutil
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from latexprep.build_result import BuildTools
from latexprep.check_selection import CheckSelection
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.defects import REPORT_DEFECT_SUGGESTION
from latexprep.errors import PreparationError
from latexprep.findings import Finding
from latexprep.job_context import JobRequest
from latexprep.job_execution import JobDeadlineExceeded
from latexprep.publication import VerifiedPackage
from latexprep.redaction import scrub_local_paths
from latexprep.report import Outcome, Report
from latexprep.staging import apply_contents
from tests.support import controlled_backends


def project(root: Path) -> None:
    root.mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\nHello.\\end{document}\n"
    )


async def build(tree, main, work, engine, runner, **_options):
    work.mkdir()
    pdf = work / "built.pdf"
    pdf.write_bytes(b"%PDF-1.4\ncontrolled orchestration fixture\n")
    return SimpleNamespace(
        success=True,
        pdf=pdf,
        findings=[],
        recorder_project_inputs={main},
        submission_inputs={main},
        tools=BuildTools(),
        command=[],
        generated_reads=frozenset(),
    )


class ParallelWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_cancellation_during_publication_removes_only_new_output(self):
        report = Report("prepare")
        copytree = shutil.copytree
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, output = base / "input", base / "output"
            project(source)

            async def ready(context, _report):
                prepared = context.work / "prepared"
                project(prepared)
                archive, pdf = context.work / "submission.zip", context.work / "built.pdf"
                archive.write_bytes(b"archive fixture")
                pdf.write_bytes(b"PDF fixture")
                report.outcome = Outcome.PASSED
                return VerifiedPackage(prepared, archive, pdf)

            def cancel_copy(src, dst, *args, **kwargs):
                result = copytree(src, dst, *args, **kwargs)
                if dst.resolve() == (output / "sources").resolve():
                    asyncio.current_task().cancel()
                return result

            with (
                patch("latexprep.core.run_project", side_effect=ready),
                patch("latexprep.publication.shutil.copytree", side_effect=cancel_copy),
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

    async def test_a_failed_rollback_leaves_a_finding_and_a_progress_line(self):
        report = Report("prepare")
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source, output = base / "input", base / "output"
            project(source)

            async def ready(context, _report):
                prepared = context.work / "prepared"
                project(prepared)
                archive, pdf = context.work / "submission.zip", context.work / "built.pdf"
                archive.write_bytes(b"archive fixture")
                pdf.write_bytes(b"PDF fixture")
                report.outcome = Outcome.PASSED
                return VerifiedPackage(prepared, archive, pdf)

            remove = shutil.rmtree

            def refuse(path, *args, **kwargs):
                # Only the rollback of the new output fails; the workspace is removed normally.
                if Path(path) == output.resolve():
                    raise PermissionError(13, "Permission denied", str(path))
                remove(path, *args, **kwargs)

            with (
                patch("latexprep.core.run_project", side_effect=ready),
                patch("latexprep.publication.shutil.copyfile", side_effect=RuntimeError("full")),
                patch("latexprep.publication.shutil.rmtree", side_effect=refuse),
                self.assertRaises(RuntimeError),
            ):
                await run_job(
                    JobRequest("prepare", source, Settings(), output), messages.append, report
                )
        [rollback] = [item for item in report.findings if item.rule == "execution.rollback"]
        self.assertIn(scrub_local_paths(str(output.resolve())), rollback.message)
        self.assertIn("manually", rollback.next_step)
        self.assertTrue(any("remove it manually" in message for message in messages))

    async def test_baseline_pdf_starts_before_independent_analysis_finishes(self):
        started, release = threading.Event(), threading.Event()
        inspected = asyncio.Event()

        def bibliography(_root, _main=None, **_options):
            started.set()
            if not release.wait(5):
                raise AssertionError("PDF branch did not release the independent analysis")
            return [
                Finding(
                    "bib.test_evidence",
                    "Completed independent analysis",
                    severity="info",
                    status="passed",
                )
            ]

        async def baseline(*args, **options):
            self.assertTrue(await asyncio.to_thread(started.wait, 5))
            return await build(*args, **options)

        async def inspect(*_args, **_kwargs):
            self.assertFalse(release.is_set())
            inspected.set()
            release.set()
            return []

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            project(source)
            try:
                backends = controlled_backends(build=baseline, inspect_pdf=inspect)
                with patch(
                    "latexprep.document_tasks.check_bibliography_integrity",
                    side_effect=bibliography,
                ):
                    report = await asyncio.wait_for(
                        run_job(JobRequest("check", source, Settings(jobs=3)), backends=backends), 8
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

            backends = controlled_backends(build=build, inspect_pdf=inspect, compare_pdfs=compare)
            task = asyncio.create_task(
                run_job(
                    JobRequest(
                        "prepare", source, Settings(jobs=4), output, reference_pdf=reference
                    ),
                    backends=backends,
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
            findings=[Finding("build.test", "Build diagnostic", severity="warning")],
            recorder_project_inputs=set(),
            tools=BuildTools(),
            command=[],
            generated_reads=frozenset(),
            log="",
        )
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            project(source)
            inspect = AsyncMock()
            backends = controlled_backends(build=failed, inspect_pdf=inspect)
            report = await run_job(JobRequest("check", source, Settings()), backends=backends)
        inspect.assert_not_called()
        self.assertEqual(report.outcome, "blocked")
        self.assertTrue(any(f.rule == "build.test" for f in report.findings))
        stages = {stage["name"]: stage["status"] for stage in report.stages}
        self.assertEqual(stages["baseline PDF inspection"], "blocked")
        self.assertEqual(stages["bibliography checks"], "succeeded")
        # Task statuses are recorded and serialized as plain strings, not enum members.
        self.assertTrue(all(type(stage["status"]) is str for stage in report.stages))
        serialized = {
            stage["name"]: stage["status"] for stage in json.loads(report.to_json())["stages"]
        }
        self.assertEqual(serialized["baseline PDF inspection"], "blocked")

    async def test_deadline_drains_tasks_preserves_evidence_and_never_publishes(self):
        stopped = asyncio.Event()

        async def hang(*_args, **_options):
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
            backends = controlled_backends(build=hang)
            report = await run_job(
                JobRequest("prepare", source, Settings(job_timeout_seconds=1), output),
                backends=backends,
            )
            self.assertFalse(output.exists())
        self.assertTrue(stopped.is_set())
        self.assertEqual(report.outcome, "blocked")
        self.assertTrue(any(f.rule == "execution.deadline" for f in report.findings))
        stages = {stage["name"]: stage["status"] for stage in report.stages}
        self.assertEqual(stages["source checks"], "cancelled")
        self.assertEqual(report.execution["resources"]["active_cpu"], 0)
        self.assertEqual(report.execution["resources"]["queued"], 0)

    async def test_deadline_report_still_follows_check_selection(self):
        async def hang(*_args, **_options):
            await asyncio.Event().wait()

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            project(source)
            (source / "main.tex").write_text(
                "\\documentclass{article}\n\\begin{document}\nTODO finish.\n\\end{document}\n"
            )
            settings = Settings(
                jobs=3, job_timeout_seconds=1, checks=CheckSelection(ignore=("TEX001",))
            )
            backends = controlled_backends(build=hang)
            report = await run_job(JobRequest("check", source, settings), backends=backends)
        self.assertTrue(any(f.rule == "execution.deadline" for f in report.findings))
        stages = {stage["name"]: stage["status"] for stage in report.stages}
        self.assertEqual(stages["source checks"], "succeeded")
        self.assertNotIn("source.edit_marker", {f.rule for f in report.findings})

    async def test_a_socket_timeout_is_not_reported_as_the_job_deadline(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            project(source)
            with (
                patch(
                    "latexprep.job_context.import_project", side_effect=TimeoutError("timed out")
                ),
                self.assertRaises(TimeoutError) as raised,
            ):
                await run_job(JobRequest("check", source, Settings()))
        self.assertNotIsInstance(raised.exception, JobDeadlineExceeded)

    async def test_a_crashed_check_is_reported_as_a_defect_without_paths(self):
        def crash(_root, _main=None, **_options):
            raise KeyError("unexpected")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            project(source)
            with patch("latexprep.document_tasks.check_bibliography_integrity", side_effect=crash):
                report = await run_job(JobRequest("inspect", source, Settings()))
        [finding] = [
            f
            for f in report.findings
            if f.rule == "execution.task" and f.details.get("task") == "bibliography checks"
        ]
        self.assertIn("KeyError", finding.message)
        self.assertEqual(finding.suggestion, REPORT_DEFECT_SUGGESTION)
        frames = finding.details["traceback"]
        self.assertTrue(frames)
        self.assertTrue(all("/" not in frame for frame in frames), frames)
        # A defect is an error (exit code 4), not a problem with the input (blocked, 3).
        self.assertEqual(report.outcome, Outcome.ERROR)
        self.assertEqual(report.outcome.exit_code, 4)
        # The other checks still ran and their results stay in the report.
        stages = {stage["name"]: stage["status"] for stage in report.stages}
        self.assertEqual(stages["source checks"], "succeeded")
        self.assertEqual(stages["bibliography checks"], "failed")

    async def test_an_expected_failure_in_a_check_stays_a_blocked_run(self):
        def refuse(_root, _main=None, **_options):
            raise PreparationError("the bibliography cannot be read")

        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "input"
            project(source)
            with patch("latexprep.document_tasks.check_bibliography_integrity", side_effect=refuse):
                report = await run_job(JobRequest("inspect", source, Settings()))
        [finding] = [
            f
            for f in report.findings
            if f.rule == "execution.task" and f.details.get("task") == "bibliography checks"
        ]
        self.assertNotIn("traceback", finding.details)
        self.assertEqual(report.outcome, Outcome.BLOCKED)

    async def test_repeated_cancellation_keeps_workspace_until_children_stop(self):
        started, cleaning, release, cleaned = (asyncio.Event() for _ in range(4))
        observed_work = []
        report = Report("prepare")

        async def delayed_build(_tree, _main, work, *_args, **_options):
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
            backends = controlled_backends(build=delayed_build)
            task = asyncio.create_task(
                run_job(
                    JobRequest("prepare", source, Settings(), output),
                    report=report,
                    backends=backends,
                )
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
            backends = controlled_backends(build=build)
            report = await run_job(
                JobRequest("prepare", source, Settings(max_temporary_bytes=1), output),
                backends=backends,
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
                apply_contents(
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
                apply_contents(root, {"a.tex": b"new"}, originals={})
            with self.assertRaises(PreparationError):
                apply_contents(root, {"a.tex": b"new", "missing.tex": b"new"})
            self.assertEqual((root / "a.tex").read_bytes(), b"original")
