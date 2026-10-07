from __future__ import annotations

import asyncio
import difflib
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.config import Settings
from latexprep.formatting import format_project
from latexprep.models import has_blockers
from latexprep.runtime import CommandResult, ToolRunner
from latexprep.scheduler import ResourceBudget


class ControlledFormatter(ToolRunner):
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
        self.calls = []
        self.leases = {path: [] for path in outputs}
        self.workspaces = {}
        self.active = 0
        self.max_active = 0
        self.version_calls = 0

    async def tool_version(self, argv, workspace) -> dict[str, object]:
        self.version_calls += 1
        assert workspace == self.work / "version"
        return {"version": "test formatter", "executable": argv[0]}

    async def run(self, argv, cwd, workspace, *, readonly_inputs=(), memory_mb=None):
        relative = workspace.relative_to(self.work / "files").as_posix()
        assert cwd == workspace
        assert memory_mb == 128
        assert argv[-1] == f"./{Path(relative).name}"
        pass_number = sum(path == relative for path, _pass in self.calls)
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
        self.calls.append((relative, pass_number + 1))
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
    return CommandResult(0, text, "", False, [], **flags)


def source_tree(base, paths):
    source = base / "source"
    source.mkdir()
    for relative in paths:
        target = source / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("original\n")
    return source


class ParallelFormattingTests(unittest.IsolatedAsyncioTestCase):
    async def wait_for_events(self, *events):
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in events)), 2)

    async def finish_task(self, task, runner):
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
            budget = ResourceBudget(jobs=2, memory_mb=1024)
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
                patch("latexprep.formatting.difflib.unified_diff", side_effect=checked_diff),
            ):
                task = asyncio.create_task(
                    format_project(source, work, runner, Settings(), budget=budget)
                )
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
                    [number for path, number in runner.calls if path == relative], [1, 2]
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
                budget = ResourceBudget(jobs=jobs, memory_mb=1024) if jobs else None
                runner = ControlledFormatter(
                    work,
                    {path: [output("formatted\n")] * 2 for path in paths},
                    budget=budget,
                    blocked=paths,
                )
                with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                    task = asyncio.create_task(
                        format_project(source, work, runner, Settings(), budget=budget)
                    )
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
                    runner.calls, [("a.tex", 1), ("a.tex", 2), ("b.tex", 1), ("b.tex", 2)]
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
                budget = ResourceBudget(jobs=jobs, memory_mb=1024)
                runner = ControlledFormatter(work, outputs, budget=budget)
                with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                    results.append(
                        await format_project(source, work, runner, Settings(), budget=budget)
                    )
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
                    budget = ResourceBudget(jobs=jobs, memory_mb=1024)
                    runner = ControlledFormatter(work, outputs, budget=budget)
                    with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                        result = await format_project(
                            source, work, runner, Settings(), budget=budget
                        )
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
            budget = ResourceBudget(jobs=2, memory_mb=200)
            cleanup_gate = asyncio.Event()
            runner = ControlledFormatter(
                work,
                {path: [output("formatted\n")] * 2 for path in paths},
                budget=budget,
                blocked=paths,
                cleanup_gate=cleanup_gate,
            )
            with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                task = asyncio.create_task(
                    format_project(source, work, runner, Settings(), budget=budget)
                )
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
            self.assertEqual(runner.calls, [("a.tex", 1)])
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
            budget = ResourceBudget(jobs=2, memory_mb=128)
            runner = ControlledFormatter(
                work,
                {path: [output("formatted\n")] * 2 for path in paths},
                budget=budget,
            )
            with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                result = await format_project(source, work, runner, Settings(), budget=budget)
            self.assertTrue(has_blockers(result.findings))
            self.assertEqual(runner.calls, [])
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
            budget = ResourceBudget(jobs=2, memory_mb=1024)
            runner = ControlledFormatter(
                work,
                {"main.tex": [output("formatted\n")] * 2},
                budget=budget,
                blocked=["main.tex"],
            )
            with patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"):
                task = asyncio.create_task(
                    format_project(source, work, runner, Settings(), budget=budget)
                )
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
