"""Parallel formatting with a controlled formatter: overlap, isolation, failures, cancellation."""

from __future__ import annotations

import asyncio
import difflib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.command_runner import CommandResult, ToolVersion
from latexprep.findings import has_blockers
from latexprep.resource_budget import ResourceBudget
from tests.fakes import FakeCommandRunner
from tests.support import format_with


class ControlledFormatter(FakeCommandRunner):
    """A formatter double that answers each pass with a scripted result and records how it ran.

    Each file is formatted in passes; the double checks the workspace, the arguments and the
    budget lease of every pass, and lets a test hold, observe and cancel the passes.

    Args:
        blocked: The files whose first pass waits for its gate.

    Attributes:
        work: The working directory: passes run in workspaces below ``work / "files"``, the
            version probe in ``work / "version"``.
        outputs: The scripted results of each file by relative path, one per pass.
        budget: The resource budget whose current lease each pass checks, or ``None``.
        entered: An event per file, set when a pass of the file starts.
        finished: An event per file, set when its second pass starts.
        gates: An event per blocked file, which its first pass waits for.
        cleanup_gate: An event that a cancelled pass waits for before it counts as cleaned
            up, or ``None``.
        cancelled: Set when a pass is cancelled.
        cleaned: Set when a cancelled pass has finished its cleanup.
        passes: Each pass as its file and its number from 1, in the order they started.
        leases: The budget leases held by the passes of each file.
        workspaces: The workspace of the latest pass of each file.
        active: The number of passes running now.
        max_active: The largest number of passes that ran at once.
        version_calls: The number of version probes.
    """

    def __init__(self, work, outputs, *, budget=None, blocked=(), cleanup_gate=None):
        super().__init__(budget=budget)
        self.work = work
        self.outputs = outputs
        self.entered = {path: asyncio.Event() for path in outputs}
        self.finished = {path: asyncio.Event() for path in outputs}
        self.gates = {path: asyncio.Event() for path in blocked}
        self.cleanup_gate = cleanup_gate
        self.cancelled = asyncio.Event()
        self.cleaned = asyncio.Event()
        self.passes = []
        self.leases = {path: [] for path in outputs}
        self.workspaces = {}
        self.active = 0
        self.max_active = 0
        self.version_calls = 0

    async def tool_version(self, argv, workspace) -> ToolVersion:
        """Check that the probe runs in ``work / "version"``, count it, and return a version."""
        self.version_calls += 1
        assert workspace == self.work / "version"
        return ToolVersion(("test formatter",), argv[0], ("--version",))

    async def run(self, argv, cwd, workspace, *, readonly_inputs=(), memory_mb=None):
        """Return the next scripted result of the file in ``workspace`` and check how it runs.

        The pass asserts that it runs in its workspace with 128 MB and names its file, that a
        later pass reads the output of the previous one, that the tool state in the workspace
        belongs to this file alone and, with a budget, that it holds a lease larger than its
        memory. The first pass of a blocked file waits for its gate.

        Args:
            argv: The formatter command line; its last argument names the file.
            cwd: The working directory, which must be the workspace.
            workspace: The workspace of the file, below ``work / "files"``.
            readonly_inputs: The read-only inputs; they are not used.
            memory_mb: The memory limit of the command, which must be 128.

        Returns:
            The scripted result of this pass.

        Raises:
            asyncio.CancelledError: The pass was cancelled; it sets ``cancelled``, waits for
                ``cleanup_gate`` when one is given and sets ``cleaned`` first.
        """
        relative = workspace.relative_to(self.work / "files").as_posix()
        assert cwd == workspace
        assert memory_mb == 128
        assert argv[-1] == f"./{Path(relative).name}"
        pass_number = sum(path == relative for path, _pass in self.passes)
        target = workspace / argv[-1]
        if pass_number:
            previous = self.outputs[relative][pass_number - 1]
            assert target.read_bytes() == previous.stdout.encode("utf-8")
        state = workspace / ".tool-state"
        state.mkdir(exist_ok=True)
        marker = state / "owner"
        if marker.exists():
            assert marker.read_text() == relative
        marker.write_text(relative)
        self.workspaces[relative] = workspace
        if self.budget is not None:
            lease = self.budget.current_lease
            assert lease is not None
            assert lease.memory_mb > memory_mb
            self.leases[relative].append(lease)
        self.passes.append((relative, pass_number + 1))
        self.entered[relative].set()
        self.active += 1
        self.max_active = max(self.active, self.max_active)
        try:
            if pass_number == 0 and relative in self.gates:
                await self.gates[relative].wait()
            if pass_number == 1:
                self.finished[relative].set()
            return self.outputs[relative][pass_number]
        except asyncio.CancelledError:
            self.cancelled.set()
            if self.cleanup_gate is not None:
                await self.cleanup_gate.wait()
            self.cleaned.set()
            raise
        finally:
            self.active -= 1


def output(text, **flags):
    """Build a successful formatter result.

    Args:
        text: The standard output of the formatter.
        **flags: Further fields of the :class:`CommandResult`.

    Returns:
        The result, with exit code 0 and no standard error.
    """
    return CommandResult(0, text, "", resolved_command=[], **flags)


def source_tree(base, paths):
    """Create a ``source`` directory in ``base`` with a file at each path.

    Args:
        base: The directory in which ``source`` is created.
        paths: The relative paths of the files; each holds the line ``original``.

    Returns:
        The new ``source`` directory.
    """
    source = base / "source"
    source.mkdir()
    for relative in paths:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original\n")
    return source


class ParallelFormattingTests(unittest.IsolatedAsyncioTestCase):
    """Formatting several files at once under a budget, and serial formatting without one."""

    async def wait_for_events(self, *events):
        """Wait until every one of ``events`` is set.

        Raises:
            TimeoutError: The events were not all set within two seconds.
        """
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in events)), 2)

    async def finish_task(self, task, runner):
        """Release every gate of ``runner``, cancel ``task`` if it is running and await its end.

        An exception of ``task`` is not raised again.

        Args:
            task: The task that runs the formatting.
            runner: The controlled formatter whose gates and cleanup gate are set.
        """
        for gate in runner.gates.values():
            gate.set()
        if runner.cleanup_gate is not None:
            runner.cleanup_gate.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)

    async def test_files_overlap_with_isolated_state_and_ordered_passes(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = ["left/main.tex", "right/main.tex"]
            source = source_tree(base, paths)
            work = base / "work"
            budget = ResourceBudget(cpu_slots=2, memory_mb=1024)
            runner = ControlledFormatter(
                work,
                {path: [output(path + "\n")] * 2 for path in paths},
                budget=budget,
                blocked=paths,
            )
            diff_leases = {}
            unified_diff = difflib.unified_diff

            def checked_diff(*args, **kwargs):
                diff_leases[kwargs["fromfile"]] = budget.current_lease
                return unified_diff(*args, **kwargs)

            with (
                patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"),
                patch("latexprep.text_edits.difflib.unified_diff", side_effect=checked_diff),
            ):
                task = asyncio.create_task(format_with(source, work, runner, budget=budget))
                try:
                    await self.wait_for_events(*(runner.entered[path] for path in paths))
                    self.assertEqual(runner.active, 2)
                    runner.gates[paths[1]].set()
                    await self.wait_for_events(runner.finished[paths[1]])
                    self.assertFalse(task.done())
                    self.assertFalse(runner.finished[paths[0]].is_set())
                    runner.gates[paths[0]].set()
                    result = await asyncio.wait_for(task, 2)
                finally:
                    await self.finish_task(task, runner)
            self.assertEqual(runner.version_calls, 1)
            self.assertEqual(runner.max_active, 2)
            self.assertEqual(list(result.contents), paths)
            self.assertEqual(list(result.originals), paths)
            self.assertEqual([change.path for change in result.changes], paths)
            self.assertEqual([finding.path for finding in result.findings], paths)
            self.assertEqual(budget.statistics()["active_memory_mb"], 0)
            for relative in paths:
                self.assertEqual(result.originals[relative], b"original\n")
                self.assertEqual((source / relative).read_bytes(), b"original\n")
                self.assertEqual(
                    [number for path, number in runner.passes if path == relative], [1, 2]
                )
                self.assertIs(runner.leases[relative][0], runner.leases[relative][1])
                self.assertIs(runner.leases[relative][0], diff_leases[relative])
                self.assertEqual(runner.workspaces[relative], work / "files" / relative)
                self.assertEqual(
                    (runner.workspaces[relative] / ".tool-state" / "owner").read_text(),
                    relative,
                )

    async def test_no_budget_and_one_job_keep_files_serial(self):
        for jobs in (None, 1):
            with self.subTest(jobs=jobs), tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                paths = ["a.tex", "b.tex"]
                source = source_tree(base, paths)
                work = base / "work"
                budget = ResourceBudget(cpu_slots=jobs, memory_mb=1024) if jobs else None
                runner = ControlledFormatter(
                    work,
                    {path: [output("formatted\n")] * 2 for path in paths},
                    budget=budget,
                    blocked=paths,
                )
                with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                    task = asyncio.create_task(format_with(source, work, runner, budget=budget))
                    try:
                        await self.wait_for_events(runner.entered["a.tex"])
                        self.assertFalse(runner.entered["b.tex"].is_set())
                        runner.gates["a.tex"].set()
                        await self.wait_for_events(runner.entered["b.tex"])
                        self.assertTrue(runner.finished["a.tex"].is_set())
                        runner.gates["b.tex"].set()
                        result = await asyncio.wait_for(task, 2)
                    finally:
                        await self.finish_task(task, runner)
                self.assertFalse(has_blockers(result.findings))
                self.assertEqual(runner.max_active, 1)
                self.assertEqual(
                    runner.passes, [("a.tex", 1), ("a.tex", 2), ("b.tex", 1), ("b.tex", 2)]
                )

    async def test_serial_and_parallel_results_are_identical(self):
        results = []
        for jobs in (1, 3):
            with tempfile.TemporaryDirectory() as directory:
                base = Path(directory)
                outputs = {
                    "z.tex": [output("Z\n")] * 2,
                    "a/section.ltx": [output("A\n")] * 2,
                    "same.latex": [output("original\n")] * 2,
                }
                source = source_tree(base, outputs)
                work = base / "work"
                budget = ResourceBudget(cpu_slots=jobs, memory_mb=1024)
                runner = ControlledFormatter(work, outputs, budget=budget)
                with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                    results.append(await format_with(source, work, runner, budget=budget))
        self.assertEqual(results[0], results[1])
        self.assertEqual(list(results[0].contents), ["a/section.ltx", "z.tex"])
        self.assertEqual(
            [(item.code, item.status, item.severity, item.path) for item in results[0].findings],
            [
                ("FMT001", "failed", "warning", "a/section.ltx"),
                ("FMT001", "failed", "warning", "z.tex"),
            ],
        )

    async def test_worker_failures_discard_all_proposals_but_preserve_diagnostics(self):
        for pass_number in (1, 2):
            serial_result = None
            for jobs in (1, 3):
                with (
                    self.subTest(pass_number=pass_number, jobs=jobs),
                    tempfile.TemporaryDirectory() as directory,
                ):
                    base = Path(directory)
                    good = output("formatted\n")
                    bad = output("formatted\n", resource_exceeded="memory")
                    outputs = {
                        "a-good.tex": [good, good],
                        "b-unstable.tex": [good, output("changed again\n")],
                        "c-error.tex": [bad] if pass_number == 1 else [good, bad],
                    }
                    source = source_tree(base, outputs)
                    work = base / "work"
                    budget = ResourceBudget(cpu_slots=jobs, memory_mb=1024)
                    runner = ControlledFormatter(work, outputs, budget=budget)
                    with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                        result = await format_with(source, work, runner, budget=budget)
                    self.assertTrue(has_blockers(result.findings))
                    self.assertEqual(result.contents, {})
                    self.assertEqual(result.originals, {})
                    self.assertEqual(result.changes, [])
                    self.assertIn(
                        ("FMT001", "failed", "warning", "a-good.tex"),
                        [
                            (item.code, item.status, item.severity, item.path)
                            for item in result.findings
                        ],
                    )
                    self.assertIn(
                        ("FMT002", "failed", "error", "b-unstable.tex"),
                        [
                            (item.code, item.status, item.severity, item.path)
                            for item in result.findings
                        ],
                    )
                    summary = next(item for item in result.findings if item.path is None)
                    self.assertEqual(
                        (summary.code, summary.status, summary.severity),
                        ("FMT001", "inconclusive", "error"),
                    )
                    failure = next(item for item in result.findings if item.path == "c-error.tex")
                    self.assertEqual(failure.rule, "source.format_execution")
                    self.assertEqual(failure.status, "inconclusive")
                    for relative in outputs:
                        self.assertEqual((source / relative).read_bytes(), b"original\n")
                    if serial_result is None:
                        serial_result = result
                    else:
                        self.assertEqual(serial_result, result)

    async def test_cancellation_drains_active_work_and_never_starts_queued_files(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = ["a.tex", "b.tex", "c.tex"]
            source = source_tree(base, paths)
            work = base / "work"
            budget = ResourceBudget(cpu_slots=2, memory_mb=200)
            cleanup_gate = asyncio.Event()
            runner = ControlledFormatter(
                work,
                {path: [output("formatted\n")] * 2 for path in paths},
                budget=budget,
                blocked=paths,
                cleanup_gate=cleanup_gate,
            )
            with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                task = asyncio.create_task(format_with(source, work, runner, budget=budget))
                try:
                    await self.wait_for_events(runner.entered["a.tex"])
                    self.assertNotEqual(budget.statistics()["active_memory_mb"], 0)
                    task.cancel()
                    await self.wait_for_events(runner.cancelled)
                    self.assertFalse(task.done())
                    self.assertFalse(runner.cleaned.is_set())
                    cleanup_gate.set()
                    with self.assertRaises(asyncio.CancelledError):
                        await asyncio.wait_for(task, 2)
                finally:
                    await self.finish_task(task, runner)
            self.assertTrue(runner.cleaned.is_set())
            self.assertEqual(runner.active, 0)
            self.assertEqual(budget.statistics()["active_memory_mb"], 0)
            self.assertEqual(budget.statistics()["queued"], 0)
            self.assertEqual(runner.passes, [("a.tex", 1)])
            for relative in paths[1:]:
                self.assertFalse((work / "files" / relative).exists())
            for relative in paths:
                self.assertEqual((source / relative).read_bytes(), b"original\n")

    async def test_insufficient_memory_rejects_files_before_formatter_execution(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            paths = ["a.tex", "b.tex"]
            source = source_tree(base, paths)
            work = base / "work"
            budget = ResourceBudget(cpu_slots=2, memory_mb=128)
            runner = ControlledFormatter(
                work,
                {path: [output("formatted\n")] * 2 for path in paths},
                budget=budget,
            )
            with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                result = await format_with(source, work, runner, budget=budget)
            self.assertTrue(has_blockers(result.findings))
            self.assertEqual(runner.passes, [])
            self.assertEqual(result.contents, {})
            self.assertEqual(result.originals, {})
            self.assertEqual(result.changes, [])
            summary = next(item for item in result.findings if item.path is None)
            self.assertEqual(
                (summary.code, summary.status, summary.severity),
                ("FMT001", "inconclusive", "error"),
            )
            for relative in paths:
                finding = next(item for item in result.findings if item.path == relative)
                self.assertEqual(finding.rule, "source.format_execution")
                self.assertEqual(finding.status, "inconclusive")
                self.assertIn("capacity", finding.message)
                self.assertEqual((source / relative).read_bytes(), b"original\n")
                self.assertFalse((work / "files" / relative).exists())

    async def test_proposal_retains_original_bytes_when_source_changes_during_work(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = source_tree(base, ["main.tex"])
            work = base / "work"
            budget = ResourceBudget(cpu_slots=2, memory_mb=1024)
            runner = ControlledFormatter(
                work,
                {"main.tex": [output("formatted\n")] * 2},
                budget=budget,
                blocked=["main.tex"],
            )
            with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                task = asyncio.create_task(format_with(source, work, runner, budget=budget))
                try:
                    await self.wait_for_events(runner.entered["main.tex"])
                    (source / "main.tex").write_bytes(b"new independent edit\n")
                    runner.gates["main.tex"].set()
                    result = await asyncio.wait_for(task, 2)
                finally:
                    await self.finish_task(task, runner)
            self.assertEqual(result.contents, {"main.tex": b"formatted\n"})
            self.assertEqual(result.originals, {"main.tex": b"original\n"})
            self.assertEqual((source / "main.tex").read_bytes(), b"new independent edit\n")


if __name__ == "__main__":
    unittest.main()
