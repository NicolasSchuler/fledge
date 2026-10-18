"""Resource accounting: storage walks, process listing and the shared job monitor."""

from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from latexprep.command_runner import CommandResult
from latexprep.defects import is_expected_failure
from latexprep.errors import PreparationError
from latexprep.process_control import kill_process_group
from latexprep.process_table import ProcessSample
from latexprep.resource_budget import ResourceBudget
from latexprep.resource_monitor import (
    JobScope,
    MemoryAccounting,
    ResourceMonitor,
    ResourceStatistics,
    sample_interval,
    workspace_violation,
)
from latexprep.runtime import ToolRunner
from latexprep.sandbox import SeatbeltSandbox
from tests.fakes import (
    LifecycleRunner,
    ScriptedJobMonitor,
    ScriptedProcessMonitor,
)
from tests.support import ProjectTestCase, live_test


class SampledRunner(LifecycleRunner):
    """A runner whose process table, and so its memory accounting, the test scripts."""

    def __init__(self, budget: ResourceBudget) -> None:
        self.allow_sample = asyncio.Event()
        self.parent_rss_mb = 20
        self.group_rss_mb = 30
        self.group_processes = 1
        self.sample_owners: set[asyncio.Task[object] | None] = set()
        self.unavailable = False
        super().__init__(
            budget=budget,
            monitor_factory=lambda processes, statistics: ScriptedProcessMonitor(
                processes, statistics, self.sample_processes
            ),
        )

    async def sample_processes(self) -> list[ProcessSample]:
        await self.allow_sample.wait()
        self.sample_owners.add(asyncio.current_task())
        if self.unavailable:
            raise PreparationError("synthetic process accounting unavailable")
        rows = [ProcessSample(os.getpid(), -1, self.parent_rss_mb * 1024)]
        for pid in self.monitor.active_commands:
            rows.append(ProcessSample(pid, pid, self.group_rss_mb * 1024))
            rows.extend(
                ProcessSample(pid + offset, pid, 0) for offset in range(1, self.group_processes)
            )
        return rows


class StorageWalkTests(ProjectTestCase):
    def test_one_walk_measures_the_tree_and_checks_every_workspace(self) -> None:
        quiet, noisy = self.root / "quiet", self.root / "noisy"
        (quiet / "nested").mkdir(parents=True)
        noisy.mkdir()
        (quiet / "nested" / "small.bin").write_bytes(b"x" * 10)
        (noisy / "huge.bin").write_bytes(b"y" * 200)
        (self.root / "outside.bin").write_bytes(b"z" * 5)
        monitor = ResourceMonitor(SeatbeltSandbox(), ResourceStatistics(1024, None))
        scope = JobScope(self.root, max_temp_bytes=10_000, max_file_bytes=100)
        with patch("latexprep.resource_monitor.os.walk", wraps=os.walk) as walk:
            violations, tree_bytes = monitor._job_storage(scope, (quiet, noisy))
        self.assertEqual(walk.call_count, 1)
        self.assertEqual(tree_bytes, 215)
        self.assertEqual(violations, (None, "A generated file exceeded the file size limit"))

    def test_workspace_limits_are_counted_per_workspace(self) -> None:
        workspace = self.root / "work"
        workspace.mkdir()
        for index in range(5):
            (workspace / f"part{index}.bin").write_bytes(b"x" * 30)
        self.assertIsNone(workspace_violation(workspace, 100))
        # Five files of 30 bytes exceed four times a 30-byte file ceiling in total.
        self.assertEqual(
            workspace_violation(workspace, 30), "Workspace exceeded the total disk limit"
        )
        with patch("latexprep.resource_monitor.MAX_WORKSPACE_ENTRIES", 3):
            self.assertEqual(
                workspace_violation(workspace, 100), "Workspace entry count exceeded 3"
            )


class ProcessListingTests(unittest.IsolatedAsyncioTestCase):
    def monitor(self) -> ResourceMonitor:
        return ResourceMonitor(SeatbeltSandbox(), ResourceStatistics(1024, None))

    async def test_process_listing_runs_detached_and_without_input(self) -> None:
        listing = MagicMock(returncode=0)
        listing.communicate = AsyncMock(return_value=(b"1 1 10\n", b""))
        listing.wait = AsyncMock(return_value=0)
        start = AsyncMock(return_value=listing)
        with patch("latexprep.process_control.asyncio.create_subprocess_exec", start):
            samples = await self.monitor().sample_processes()
        self.assertEqual(samples, [ProcessSample(1, 1, 10)])
        options = start.call_args.kwargs
        self.assertEqual(options["stdin"], asyncio.subprocess.DEVNULL)
        self.assertTrue(options["start_new_session"])

    async def test_missing_process_listing_names_the_package_to_install(self) -> None:
        missing = AsyncMock(side_effect=FileNotFoundError(2, "No such file", "/bin/ps"))
        with patch("latexprep.process_control.asyncio.create_subprocess_exec", missing):
            with self.assertRaises(PreparationError) as raised:
                await self.monitor().sample_processes()
        self.assertIn("/bin/ps", str(raised.exception))
        self.assertIn("procps", str(raised.exception))

    async def test_unexpected_monitor_failure_names_the_exception_type(self) -> None:
        async def broken(scope: JobScope) -> None:
            if asyncio.current_task() is scope.monitor:
                raise KeyError("scope")

        runner = LifecycleRunner(
            budget=ResourceBudget(1, 1024),
            monitor_factory=lambda processes, statistics: ScriptedJobMonitor(
                processes, statistics, broken
            ),
        )
        with tempfile.TemporaryDirectory() as directory:
            async with runner.job_scope(Path(directory), max_temp_bytes=1_000_000):
                async with asyncio.timeout(3):
                    while runner.statistics().resource_exceeded is None:
                        await asyncio.sleep(0.005)
        self.assertEqual(
            runner.statistics().resource_exceeded,
            "Job resource accounting failed: KeyError: 'scope'",
        )


class SharedMonitorTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()
        perl = shutil.which("perl")
        if perl is None:
            self.skipTest("Perl is unavailable for owned child-process tests")
        self.perl = perl

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def wait_groups(self, runner: ToolRunner, count: int) -> None:
        async with asyncio.timeout(3):
            while len(runner.monitor.active_commands) != count:
                await asyncio.sleep(0.005)

    async def sleeping_command(
        self, runner: ToolRunner, name: str, memory_mb: int, command_memory_mb: int | None = None
    ) -> CommandResult:
        workspace = self.root / name
        workspace.mkdir()
        assert runner.budget is not None
        async with runner.budget.lease(name, memory_mb=memory_mb):
            return await runner.run(
                [self.perl, "-e", "sleep 30;"],
                workspace,
                workspace,
                memory_mb=command_memory_mb,
            )

    @live_test(
        "opt-in live job-monitor test (FLEDGE_RUN_INTEGRATION=1)",
        "LATEXPREP_RUN_SANDBOX_TESTS",
        platform="darwin",
    )
    async def test_live_job_monitor_samples_parent_and_real_tool_group(self) -> None:
        runner = ToolRunner(budget=ResourceBudget(1, 1024, coordinator_memory_mb=64))
        leaf = self.root / "live-command"
        leaf.mkdir()
        async with runner.job_scope(self.root, max_temp_bytes=1_000_000):
            result = await runner.run(
                [self.perl, "-e", 'select(undef,undef,undef,.4); print "completed"'],
                leaf,
                leaf,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(result.resource_exceeded)
        stats = runner.statistics()
        self.assertIsNone(stats.resource_exceeded)
        parent_rss, tool_rss = stats.peak_parent_rss_mb, stats.peak_tool_rss_mb
        assert isinstance(parent_rss, float) and isinstance(tool_rss, float)
        self.assertGreater(parent_rss, 0)
        self.assertGreater(tool_rss, 0)
        self.assertIs(stats.memory_accounting, MemoryAccounting.SAMPLED)
        self.assertIsNone(stats.memory_accounting_problem)

    async def test_shared_job_memory_includes_parent_and_stops_all_groups(self) -> None:
        runner = SampledRunner(ResourceBudget(2, 75, coordinator_memory_mb=5))
        async with runner.job_scope(self.root, max_temp_bytes=1_000_000):
            first = asyncio.create_task(self.sleeping_command(runner, "left", 30))
            second = asyncio.create_task(self.sleeping_command(runner, "right", 30))
            await self.wait_groups(runner, 2)
            assert runner.monitor.scope is not None
            monitor = runner.monitor.scope.monitor
            runner.allow_sample.set()
            async with asyncio.timeout(3):
                results = await asyncio.gather(first, second)
            self.assertEqual(runner.sample_owners, {monitor})
            for result in results:
                self.assertIn(
                    "including the parent exceeded 75 MiB", result.resource_exceeded or ""
                )
                self.assertNotEqual(result.returncode, 0)
        stats = runner.statistics()
        self.assertEqual(stats.peak_parent_rss_mb, 20)
        self.assertEqual(stats.peak_tool_rss_mb, 60)
        self.assertEqual(stats.peak_total_rss_mb, 80)
        self.assertEqual(stats.peak_command_memory_reservation_mb, 60)
        self.assertEqual(stats.peak_active_commands, 2)
        self.assertEqual(runner.monitor.active_commands, {})
        self.assertIsNone(runner.monitor.scope)

    async def test_command_memory_cap_never_exceeds_its_leaf_lease(self) -> None:
        runner = SampledRunner(ResourceBudget(1, 1024))
        runner.group_rss_mb = 33
        async with runner.job_scope(self.root, max_temp_bytes=1_000_000):
            task = asyncio.create_task(self.sleeping_command(runner, "command", 32))
            await self.wait_groups(runner, 1)
            runner.allow_sample.set()
            async with asyncio.timeout(3):
                result = await task
            self.assertEqual(result.resource_exceeded, "Aggregate tool memory exceeded 32 MiB")
        self.assertEqual(runner.statistics().peak_command_memory_reservation_mb, 32)
        self.assertIsNone(runner.statistics().resource_exceeded)

    async def test_command_headroom_leaves_the_remaining_leaf_memory_for_buffers(self) -> None:
        runner = SampledRunner(ResourceBudget(1, 1024))
        runner.group_rss_mb = 129
        async with runner.job_scope(self.root, max_temp_bytes=1_000_000):
            task = asyncio.create_task(self.sleeping_command(runner, "page", 320, 128))
            await self.wait_groups(runner, 1)
            runner.allow_sample.set()
            async with asyncio.timeout(3):
                result = await task
            self.assertEqual(result.resource_exceeded, "Aggregate tool memory exceeded 128 MiB")
        stats = runner.statistics()
        self.assertEqual(stats.peak_command_memory_reservation_mb, 320)
        self.assertEqual(stats.peak_command_memory_limit_mb, 128)
        self.assertIsNone(stats.resource_exceeded)

    async def test_command_memory_cap_must_fit_lease_and_runtime_limits(self) -> None:
        runner = LifecycleRunner(budget=ResourceBudget(1, 1024))
        assert runner.budget is not None
        async with runner.budget.lease("page", memory_mb=128):
            with self.assertRaisesRegex(ValueError, "cannot exceed"):
                await runner.run([self.perl, "-e", "exit 0"], self.root, self.root, memory_mb=129)
        for cap in (0, -1, True):
            with self.assertRaisesRegex(ValueError, "positive integer"):
                await runner.run([self.perl, "-e", "exit 0"], self.root, self.root, memory_mb=cap)
        self.assertEqual(runner.statistics().commands, 0)

    async def test_shared_monitor_retains_per_group_process_count_limit(self) -> None:
        runner = SampledRunner(ResourceBudget(1, 1024))
        runner.group_processes = 33
        async with runner.job_scope(self.root, max_temp_bytes=1_000_000):
            task = asyncio.create_task(self.sleeping_command(runner, "command", 64))
            await self.wait_groups(runner, 1)
            runner.allow_sample.set()
            async with asyncio.timeout(3):
                result = await task
            self.assertEqual(result.resource_exceeded, "Tool process count exceeded 32")

    async def test_storage_walks_run_off_the_event_loop_and_relax_their_interval(self) -> None:
        runner = SampledRunner(ResourceBudget(1, 1024))
        runner.allow_sample.set()
        threads: set[int] = set()
        original = ResourceMonitor._job_storage

        def recorded(
            self: ResourceMonitor, scope: JobScope, workspaces: tuple[Path, ...]
        ) -> object:
            threads.add(threading.get_ident())
            return original(self, scope, workspaces)

        with patch.object(ResourceMonitor, "_job_storage", recorded):
            async with runner.job_scope(self.root, max_temp_bytes=1_000_000):
                task = asyncio.create_task(self.sleeping_command(runner, "command", 64))
                await self.wait_groups(runner, 1)
                await asyncio.sleep(0.25)
                kill_process_group(next(iter(runner.monitor.active_commands)))
                async with asyncio.timeout(3):
                    await task
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)
        self.assertEqual(runner.statistics().sample_interval_seconds, 0.1)
        self.assertEqual(sample_interval(time.monotonic() - 10), 0.5)
        self.assertEqual(sample_interval(time.monotonic()), 0.1)

    async def test_job_disk_ceiling_applies_even_without_active_tools(self) -> None:
        runner = SampledRunner(ResourceBudget(1, 1024))
        runner.allow_sample.set()
        async with runner.job_scope(self.root, max_temp_bytes=1024):
            (self.root / "queued-artifact").write_bytes(b"x" * 1025)
        stats = runner.statistics()
        self.assertEqual(stats.peak_temp_bytes, 1025)
        self.assertEqual(stats.resource_exceeded, "Job temporary storage exceeded 1024 bytes")

    async def test_source_only_unavailable_accounting_is_explicit_and_active_tools_fail_closed(
        self,
    ) -> None:
        runner = SampledRunner(ResourceBudget(1, 1024))
        runner.unavailable = True
        runner.allow_sample.set()
        async with runner.job_scope(self.root, max_temp_bytes=1_000_000):
            pass
        self.assertIsNone(runner.statistics().resource_exceeded)
        self.assertIs(runner.statistics().memory_accounting, MemoryAccounting.UNAVAILABLE)
        self.assertEqual(
            runner.statistics().memory_accounting_problem,
            "synthetic process accounting unavailable",
        )
        runner.allow_sample.clear()
        async with runner.job_scope(self.root, max_temp_bytes=1_000_000):
            task = asyncio.create_task(self.sleeping_command(runner, "command", 64))
            await self.wait_groups(runner, 1)
            runner.allow_sample.set()
            async with asyncio.timeout(3):
                result = await task
            self.assertIn("Job resource accounting failed", result.resource_exceeded or "")

    async def test_unleased_command_is_admitted_and_held_lease_is_reused(self) -> None:
        budget = ResourceBudget(1, 100, coordinator_memory_mb=20)
        runner = LifecycleRunner(budget=budget)
        result = await runner.run([self.perl, "-e", 'print "first";'], self.root, self.root)
        self.assertEqual(result.stdout, "first")
        self.assertEqual(runner.statistics().peak_command_memory_reservation_mb, 80)
        async with budget.lease("build", memory_mb=32):
            async with asyncio.timeout(3):
                second = await runner.run(
                    [self.perl, "-e", 'print "second";'], self.root, self.root
                )
        self.assertEqual(second.stdout, "second")
        self.assertIsNone(budget.current_lease)

    async def test_writable_workspace_cannot_include_frozen_inputs(self) -> None:
        runner = LifecycleRunner()
        source = self.root / "source.pdf"
        source.write_bytes(b"%PDF-input")
        runner.freeze_input(source, self.root / "artifacts")
        with self.assertRaisesRegex(ValueError, "cannot contain frozen inputs"):
            await runner.run([self.perl, "-e", "exit 0"], self.root, self.root)

    async def test_contract_violations_are_defects_not_expected_failures(self) -> None:
        runner = LifecycleRunner()
        other = self.root / "other"
        other.mkdir()
        command = [self.perl, "-e", "exit 0"]
        runner._workspace_claims.add(self.root)
        violations = (
            (ValueError, "inside its own workspace", self.root, other, None),
            (ValueError, "positive integer", self.root, self.root, 0),
            (RuntimeError, "non-overlapping writable workspaces", self.root, self.root, None),
        )
        for error_type, message, cwd, workspace, memory_mb in violations:
            with self.subTest(message=message):
                with self.assertRaisesRegex(error_type, message) as raised:
                    await runner.run(command, cwd, workspace, memory_mb=memory_mb)
                # Reported as a defect, not as an unavailable tool or a researcher's problem.
                self.assertFalse(isinstance(raised.exception, PreparationError))
                self.assertFalse(is_expected_failure(raised.exception))

    async def test_scope_exit_reaps_unawaited_active_commands(self) -> None:
        runner = SampledRunner(ResourceBudget(1, 1024))
        runner.allow_sample.set()
        async with runner.job_scope(self.root, max_temp_bytes=1_000_000):
            task = asyncio.create_task(self.sleeping_command(runner, "command", 64))
            await self.wait_groups(runner, 1)
            process = next(iter(runner.monitor.active_commands.values())).process
        result = await task
        self.assertEqual(result.resource_exceeded, "Job scope closed while a command was active")
        self.assertIsNotNone(process.returncode)
        self.assertEqual(runner.monitor.active_commands, {})
        self.assertIsNone(runner.monitor.scope)

    async def test_repeated_cancellation_during_launch_still_reaps_new_child(self) -> None:
        runner = LifecycleRunner()
        original = asyncio.create_subprocess_exec
        launched, release = asyncio.Event(), asyncio.Event()
        children: list[asyncio.subprocess.Process] = []

        async def delayed_launch(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
            child = await original(*args, **kwargs)
            children.append(child)
            launched.set()
            await release.wait()
            return child

        with patch("latexprep.process_control.asyncio.create_subprocess_exec", delayed_launch):
            task = asyncio.create_task(
                runner.run([self.perl, "-e", "sleep 30"], self.root, self.root)
            )
            await launched.wait()
            task.cancel()
            await asyncio.sleep(0)
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(len(children), 1)
        self.assertIsNotNone(children[0].returncode)
        self.assertEqual(runner._workspace_claims, set())

    async def test_repeated_cancellation_waits_for_monitor_cleanup_and_child_reaping(self) -> None:
        runner = LifecycleRunner(budget=ResourceBudget(1, 1024))
        cleanup_started, cleanup_release, cleanup_done = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def monitor(pid: int, workspace: Path, *, max_file_bytes: int) -> str:
            try:
                await asyncio.Future()
            finally:
                cleanup_started.set()
                await cleanup_release.wait()
                cleanup_done.set()
            raise AssertionError("unreachable")

        with patch.object(runner.monitor, "watch_command", monitor):
            task = asyncio.create_task(
                runner.run([self.perl, "-e", "sleep 30"], self.root, self.root)
            )
            await self.wait_groups(runner, 1)
            process = next(iter(runner.monitor.active_commands.values())).process
            task.cancel()
            await cleanup_started.wait()
            task.cancel()
            await asyncio.sleep(0)
            self.assertFalse(task.done())
            self.assertFalse(cleanup_done.is_set())
            cleanup_release.set()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(cleanup_done.is_set())
        self.assertIsNotNone(process.returncode)
        self.assertEqual(runner.monitor.active_commands, {})
        self.assertEqual(runner._workspace_claims, set())


if __name__ == "__main__":
    unittest.main()
