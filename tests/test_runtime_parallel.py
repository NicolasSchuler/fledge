from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from latexprep.models import PreparationError
from latexprep.runtime import CommandResult, ToolRunner, _kill_group, _sample_interval
from latexprep.scheduler import ResourceBudget
from tests.support import live_tests_enabled


class ProbeRunner(ToolRunner):
    def __init__(self) -> None:
        super().__init__(source_date_epoch=123)
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.response = CommandResult(0, "pdfinfo version test\n", "", False, ["/tools/pdfinfo"])
        self.owner: asyncio.Task[object] | None = None

    def _resolve_tool(self, name: str) -> Path:
        return Path("/tools") / Path(name).name

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        self.calls += 1
        self.owner = asyncio.current_task()
        self.entered.set()
        await self.release.wait()
        return self.response


class UnrestrictedLifecycleRunner(ToolRunner):
    """Exercise owned real child processes; no production sandbox bypass exists."""

    def _sandbox_command(
        self,
        command: list[str],
        workspace: Path,
        readonly_inputs: tuple[Path, ...] = (),
    ) -> list[str]:
        return command

    async def _resource_monitor(self, pid: int, workspace: Path) -> str:
        await asyncio.Future()
        raise AssertionError("unreachable")


class SampledRunner(UnrestrictedLifecycleRunner):
    def __init__(self, budget: ResourceBudget) -> None:
        super().__init__(budget=budget)
        self.allow_sample = asyncio.Event()
        self.parent_rss_mb = 20
        self.group_rss_mb = 30
        self.group_processes = 1
        self.sample_owners: set[asyncio.Task[object] | None] = set()
        self.unavailable = False

    async def _process_sample(self) -> list[tuple[int, int, int]]:
        await self.allow_sample.wait()
        self.sample_owners.add(asyncio.current_task())
        if self.unavailable:
            raise PreparationError("synthetic process accounting unavailable")
        rows = [(os.getpid(), -1, self.parent_rss_mb * 1024)]
        for pid in self._groups:
            rows.append((pid, pid, self.group_rss_mb * 1024))
            rows.extend((pid + offset, pid, 0) for offset in range(1, self.group_processes))
        return rows


class VersionAndInputTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_concurrent_version_probe_runs_once_and_returns_detached_metadata(self) -> None:
        runner = ProbeRunner()
        first = asyncio.create_task(runner.tool_version(["pdfinfo", "-v"], self.root))
        await runner.entered.wait()
        second = asyncio.create_task(runner.tool_version(["pdfinfo", "-v"], self.root))
        await asyncio.sleep(0)
        self.assertEqual(runner.calls, 1)
        self.assertIs(runner.owner, first)
        runner.release.set()
        left, right = await asyncio.gather(first, second)
        self.assertEqual(left, right)
        self.assertIsNot(left, right)
        left["version"] = ["changed"]
        tools = runner.tool_versions
        tools["pdfinfo"] = "changed"
        current = await runner.tool_version(["pdfinfo", "-v"], self.root)
        self.assertEqual(current["version"], ["pdfinfo version test"])
        self.assertEqual(runner.calls, 1)
        self.assertEqual(runner.statistics()["version_probes"], 1)

    async def test_unleased_probe_does_not_hold_lock_while_waiting_for_admission(self) -> None:
        runner = ProbeRunner()
        runner.budget = ResourceBudget(1, 100)
        start = asyncio.Event()

        async def unleased_probe() -> dict[str, object]:
            await start.wait()
            return await runner.tool_version(["pdfinfo", "-v"], self.root)

        waiting = asyncio.create_task(unleased_probe())
        async with runner.budget.lease("existing leaf", memory_mb=64):
            start.set()
            await asyncio.sleep(0)
            runner.release.set()
            async with asyncio.timeout(3):
                owned = await runner.tool_version(["pdfinfo", "-v"], self.root)
        async with asyncio.timeout(3):
            self.assertEqual(await waiting, owned)
        self.assertEqual(runner.calls, 1)

    async def test_cancelled_version_probe_can_retry_without_publishing_success(self) -> None:
        runner = ProbeRunner()
        first = asyncio.create_task(runner.tool_version(["pdfinfo", "-v"], self.root))
        await runner.entered.wait()
        first.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await first
        self.assertEqual(runner.tool_versions, {})
        runner.release.set()
        version = await runner.tool_version(["pdfinfo", "-v"], self.root)
        self.assertEqual(version["version"], ["pdfinfo version test"])
        self.assertEqual(runner.calls, 2)

    async def test_failed_limited_and_empty_version_probes_do_not_enter_cache(self) -> None:
        runner = ProbeRunner()
        runner.release.set()
        cases = (
            CommandResult(1, "partial", "error", False, ["/tools/pdfinfo"]),
            CommandResult(0, "version", "", True, ["/tools/pdfinfo"]),
            CommandResult(0, "version", "", False, ["/tools/pdfinfo"], output_limited=True),
            CommandResult(0, "version", "", False, ["/tools/pdfinfo"], resource_exceeded="memory"),
            CommandResult(0, "", "", False, ["/tools/pdfinfo"]),
            CommandResult(0, "version", "Syntax Error: invalid", False, ["/tools/pdfinfo"]),
        )
        for response in cases:
            runner.response = response
            with self.assertRaises(PreparationError):
                await runner.tool_version(["pdfinfo", "-v"], self.root)
            self.assertEqual(runner.tool_versions, {})
        runner.response = CommandResult(0, "valid", "", False, ["/tools/pdfinfo"])
        self.assertEqual(
            (await runner.tool_version(["pdfinfo", "-v"], self.root))["version"], ["valid"]
        )
        self.assertEqual(runner.calls, len(cases) + 1)

    async def test_version_cache_keys_include_flags_and_detach_99_needs_its_prefix(self) -> None:
        runner = ProbeRunner()
        runner.release.set()
        runner.response = CommandResult(99, "", "other tool version 1", False, ["/tools/pdfdetach"])
        with self.assertRaises(PreparationError):
            await runner.tool_version(["pdfdetach", "-v"], self.root)
        runner.response = CommandResult(
            99, "", "pdfdetach version 25.1\n", False, ["/tools/pdfdetach"]
        )
        value = await runner.tool_version(["pdfdetach", "-v"], self.root)
        self.assertEqual(value["version"], ["pdfdetach version 25.1"])
        with self.assertRaises(PreparationError):
            await runner.tool_version(["pdfdetach", "--version"], self.root)
        with self.assertRaises(PreparationError):
            await runner.tool_version(["pdfinfo", "-v"], self.root)
        self.assertEqual(runner.calls, 4)

    def test_epoch_context_resolution_and_invocation_state_are_stable(self) -> None:
        runner = ToolRunner(source_date_epoch=17)
        with patch("latexprep.runtime._tool_path", return_value=Path("/usr/bin/perl")) as resolve:
            self.assertEqual(runner._resolve_tool("perl"), Path("/usr/bin/perl"))
            self.assertEqual(runner._resolve_tool("perl"), Path("/usr/bin/perl"))
            resolve.assert_called_once()
        context = runner._tool_context(Path("/usr/bin/perl"))
        first = runner._environment(self.root, Path("/usr/bin/perl"))
        with patch("latexprep.runtime.shutil.which", return_value="/untrusted/new-tool"):
            second = runner._environment(self.root, Path("/usr/bin/perl"))
            self.assertIs(runner._tool_context(Path("/usr/bin/perl")), context)
        self.assertEqual(first["SOURCE_DATE_EPOCH"], "17")
        self.assertEqual(first["SOURCE_DATE_EPOCH"], second["SOURCE_DATE_EPOCH"])
        self.assertEqual(first["PATH"], second["PATH"])
        self.assertNotEqual(first["TMPDIR"], second["TMPDIR"])
        self.assertIsInstance(context.resource_roots, frozenset)
        with self.assertRaises(AttributeError):
            attribute = "source_date_epoch"
            setattr(runner, attribute, "changed")
        for bad in (-1, "1.5", "１２３", True):
            with self.assertRaises(PreparationError):
                ToolRunner(source_date_epoch=bad)

    def test_freeze_reuses_snapshot_and_handles_same_names(self) -> None:
        runner = ToolRunner()
        left, right = self.root / "left", self.root / "right"
        left.mkdir()
        right.mkdir()
        first, second = left / "main.pdf", right / "main.pdf"
        first.write_bytes(b"%PDF-first")
        second.write_bytes(b"%PDF-second")
        frozen_first = runner.freeze_input(first, self.root / "artifacts")
        frozen_second = runner.freeze_input(second, self.root / "artifacts")
        self.assertNotEqual(frozen_first, frozen_second)
        self.assertEqual(frozen_first.read_bytes(), b"%PDF-first")
        self.assertEqual(frozen_second.read_bytes(), b"%PDF-second")
        first.unlink()
        self.assertEqual(runner.freeze_input(first, self.root / "other"), frozen_first)
        self.assertEqual(runner.freeze_input(frozen_first, self.root / "other"), frozen_first)
        self.assertFalse((self.root / "other").exists())
        self.assertEqual(len(runner._frozen_files), 2)

    def test_readonly_grants_reject_arbitrary_paths_directories_and_changed_snapshots(self) -> None:
        runner = ToolRunner()
        source = self.root / "source.pdf"
        source.write_bytes(b"%PDF-input")
        frozen = runner.freeze_input(source, self.root / "artifacts")
        linked = self.root / "linked.pdf"
        linked.symlink_to(frozen)
        for invalid in (source, frozen.parent, linked):
            with self.assertRaises(PreparationError):
                runner._validate_readonly_inputs((invalid,))
        self.assertEqual(runner._validate_readonly_inputs((frozen,)), (frozen,))
        frozen.chmod(0o600)
        frozen.write_bytes(b"modified")
        with self.assertRaises(PreparationError):
            runner._validate_readonly_inputs((frozen,))
        with self.assertRaises(PreparationError):
            runner.freeze_input(linked, self.root / "another")
        existing = self.root / "existing"
        existing.mkdir()
        with self.assertRaises(PreparationError):
            ToolRunner().freeze_input(source, existing)

    def test_sandbox_grants_literal_input_reads_and_only_leaf_writes(self) -> None:
        runner = ToolRunner()
        source = self.root / "input.pdf"
        source.write_bytes(b"%PDF-input")
        frozen = runner.freeze_input(source, self.root / "artifacts")
        workspace = self.root / "leaf"
        workspace.mkdir()
        with patch("latexprep.runtime.sys.platform", "darwin"):
            command = runner._sandbox_command(["/usr/bin/perl", "-v"], workspace, (frozen,))
        profile = command[2]
        self.assertIn(f'(literal "{frozen}")', profile)
        self.assertNotIn(f'(subpath "{frozen.parent}")', profile)
        self.assertIn(f'(allow file-write* (subpath "{workspace}"))', profile)
        self.assertNotIn(f'(subpath "{self.root}")', profile)

    @unittest.skipUnless(
        live_tests_enabled("LATEXPREP_RUN_SANDBOX_TESTS") and sys.platform == "darwin",
        "opt-in live Seatbelt test (LATEXPREP_RUN_SANDBOX_TESTS=1)",
    )
    async def test_live_sandbox_denies_sibling_writes_and_undeclared_reads(self) -> None:
        runner = ToolRunner()
        source = self.root / "source.pdf"
        source.write_bytes(b"%PDF-input")
        frozen = runner.freeze_input(source, self.root / "artifacts")
        workspace, sibling = self.root / "leaf", self.root / "sibling"
        workspace.mkdir()
        sibling.mkdir()
        program = (
            'my $f; print(open($f,"<",$ARGV[0]) ? "READ" : "FAIL"); '
            'print(open($f,"<",$ARGV[1]) ? " LEAK" : " DENIED"); '
            'print(open($f,">",$ARGV[2]) ? " SIBLING" : " BLOCKED"); '
            'print(open($f,">",$ARGV[0]) ? " MODIFIED" : " READONLY");'
        )
        try:
            result = await runner.run(
                ["perl", "-e", program, str(frozen), str(source), str(sibling / "marker")],
                workspace,
                workspace,
                readonly_inputs=(frozen,),
            )
        except PreparationError as error:
            self.skipTest(str(error))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "READ DENIED BLOCKED READONLY", result.stderr)
        self.assertFalse((sibling / "marker").exists())


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
            while len(runner._groups) != count:
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

    @unittest.skipUnless(
        live_tests_enabled("LATEXPREP_RUN_SANDBOX_TESTS") and sys.platform == "darwin",
        "opt-in live job-monitor test (LATEXPREP_RUN_SANDBOX_TESTS=1)",
    )
    async def test_live_job_monitor_samples_parent_and_real_tool_group(self) -> None:
        runner = ToolRunner(budget=ResourceBudget(1, 1024, parent_memory_mb=64))
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
        self.assertIsNone(stats["resource_exceeded"])
        parent_rss, tool_rss = stats["peak_parent_rss_mb"], stats["peak_tool_rss_mb"]
        assert isinstance(parent_rss, float) and isinstance(tool_rss, float)
        self.assertGreater(parent_rss, 0)
        self.assertGreater(tool_rss, 0)
        self.assertEqual(stats["memory_accounting"], "sampled parent and registered process groups")

    async def test_shared_job_memory_includes_parent_and_stops_all_groups(self) -> None:
        runner = SampledRunner(ResourceBudget(2, 75, parent_memory_mb=5))
        async with runner.job_scope(self.root, max_temp_bytes=1_000_000):
            first = asyncio.create_task(self.sleeping_command(runner, "left", 30))
            second = asyncio.create_task(self.sleeping_command(runner, "right", 30))
            await self.wait_groups(runner, 2)
            monitor = runner._job_monitor
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
        self.assertEqual(stats["peak_parent_rss_mb"], 20)
        self.assertEqual(stats["peak_tool_rss_mb"], 60)
        self.assertEqual(stats["peak_total_rss_mb"], 80)
        self.assertEqual(stats["peak_command_memory_reservation_mb"], 60)
        self.assertEqual(stats["peak_active_commands"], 2)
        self.assertEqual(runner._groups, {})
        self.assertIsNone(runner._job_monitor)

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
        self.assertEqual(runner.statistics()["peak_command_memory_reservation_mb"], 32)
        self.assertIsNone(runner.statistics()["resource_exceeded"])

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
        self.assertEqual(stats["peak_command_memory_reservation_mb"], 320)
        self.assertEqual(stats["peak_command_memory_limit_mb"], 128)
        self.assertIsNone(stats["resource_exceeded"])

    async def test_command_memory_cap_must_fit_lease_and_runtime_limits(self) -> None:
        runner = UnrestrictedLifecycleRunner(budget=ResourceBudget(1, 1024))
        assert runner.budget is not None
        async with runner.budget.lease("page", memory_mb=128):
            with self.assertRaisesRegex(PreparationError, "cannot exceed"):
                await runner.run([self.perl, "-e", "exit 0"], self.root, self.root, memory_mb=129)
        for cap in (0, -1, True):
            with self.assertRaisesRegex(PreparationError, "positive integer"):
                await runner.run([self.perl, "-e", "exit 0"], self.root, self.root, memory_mb=cap)
        self.assertEqual(runner.statistics()["commands"], 0)

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
        original = ToolRunner._job_storage

        def recorded(self: ToolRunner, workspaces: tuple[Path, ...]) -> object:
            threads.add(threading.get_ident())
            return original(self, workspaces)

        with patch.object(ToolRunner, "_job_storage", recorded):
            async with runner.job_scope(self.root, max_temp_bytes=1_000_000):
                task = asyncio.create_task(self.sleeping_command(runner, "command", 64))
                await self.wait_groups(runner, 1)
                await asyncio.sleep(0.25)
                _kill_group(next(iter(runner._groups)))
                async with asyncio.timeout(3):
                    await task
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)
        self.assertEqual(runner.statistics()["sample_interval_seconds"], 0.1)
        self.assertEqual(_sample_interval(time.monotonic() - 10), 0.5)
        self.assertEqual(_sample_interval(time.monotonic()), 0.1)

    async def test_job_disk_ceiling_applies_even_without_active_tools(self) -> None:
        runner = SampledRunner(ResourceBudget(1, 1024))
        runner.allow_sample.set()
        async with runner.job_scope(self.root, max_temp_bytes=1024):
            (self.root / "queued-artifact").write_bytes(b"x" * 1025)
        stats = runner.statistics()
        self.assertEqual(stats["peak_temp_bytes"], 1025)
        self.assertEqual(stats["resource_exceeded"], "Job temporary storage exceeded 1024 bytes")

    async def test_source_only_unavailable_accounting_is_explicit_and_active_tools_fail_closed(
        self,
    ) -> None:
        runner = SampledRunner(ResourceBudget(1, 1024))
        runner.unavailable = True
        runner.allow_sample.set()
        async with runner.job_scope(self.root, max_temp_bytes=1_000_000):
            pass
        self.assertIsNone(runner.statistics()["resource_exceeded"])
        self.assertIn("unavailable", str(runner.statistics()["memory_accounting"]))
        runner.allow_sample.clear()
        async with runner.job_scope(self.root, max_temp_bytes=1_000_000):
            task = asyncio.create_task(self.sleeping_command(runner, "command", 64))
            await self.wait_groups(runner, 1)
            runner.allow_sample.set()
            async with asyncio.timeout(3):
                result = await task
            self.assertIn("Job resource accounting failed", result.resource_exceeded or "")

    async def test_unleased_command_is_admitted_and_held_lease_is_reused(self) -> None:
        budget = ResourceBudget(1, 100, parent_memory_mb=20)
        runner = UnrestrictedLifecycleRunner(budget=budget)
        result = await runner.run([self.perl, "-e", 'print "first";'], self.root, self.root)
        self.assertEqual(result.stdout, "first")
        self.assertEqual(runner.statistics()["peak_command_memory_reservation_mb"], 80)
        async with budget.lease("build", memory_mb=32):
            async with asyncio.timeout(3):
                second = await runner.run(
                    [self.perl, "-e", 'print "second";'], self.root, self.root
                )
        self.assertEqual(second.stdout, "second")
        self.assertIsNone(budget.current_lease)

    async def test_writable_workspace_cannot_include_frozen_inputs(self) -> None:
        runner = UnrestrictedLifecycleRunner()
        source = self.root / "source.pdf"
        source.write_bytes(b"%PDF-input")
        runner.freeze_input(source, self.root / "artifacts")
        with self.assertRaisesRegex(PreparationError, "cannot contain frozen inputs"):
            await runner.run([self.perl, "-e", "exit 0"], self.root, self.root)

    async def test_scope_exit_reaps_unawaited_active_commands(self) -> None:
        runner = SampledRunner(ResourceBudget(1, 1024))
        runner.allow_sample.set()
        async with runner.job_scope(self.root, max_temp_bytes=1_000_000):
            task = asyncio.create_task(self.sleeping_command(runner, "command", 64))
            await self.wait_groups(runner, 1)
            process = next(iter(runner._groups.values())).process
        result = await task
        self.assertEqual(result.resource_exceeded, "Job scope closed while a command was active")
        self.assertIsNotNone(process.returncode)
        self.assertEqual(runner._groups, {})
        self.assertIsNone(runner._job_monitor)

    async def test_repeated_cancellation_during_launch_still_reaps_new_child(self) -> None:
        runner = UnrestrictedLifecycleRunner()
        original = asyncio.create_subprocess_exec
        launched, release = asyncio.Event(), asyncio.Event()
        children: list[asyncio.subprocess.Process] = []

        async def delayed_launch(*args: str, **kwargs: Any) -> asyncio.subprocess.Process:
            child = await original(*args, **kwargs)
            children.append(child)
            launched.set()
            await release.wait()
            return child

        with patch("latexprep.runtime.asyncio.create_subprocess_exec", delayed_launch):
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
        runner = UnrestrictedLifecycleRunner(budget=ResourceBudget(1, 1024))
        cleanup_started, cleanup_release, cleanup_done = (
            asyncio.Event(),
            asyncio.Event(),
            asyncio.Event(),
        )

        async def monitor(pid: int, workspace: Path) -> str:
            try:
                await asyncio.Future()
            finally:
                cleanup_started.set()
                await cleanup_release.wait()
                cleanup_done.set()
            raise AssertionError("unreachable")

        with patch.object(runner, "_resource_monitor", monitor):
            task = asyncio.create_task(
                runner.run([self.perl, "-e", "sleep 30"], self.root, self.root)
            )
            await self.wait_groups(runner, 1)
            process = next(iter(runner._groups.values())).process
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
        self.assertEqual(runner._groups, {})
        self.assertEqual(runner._workspace_claims, set())


if __name__ == "__main__":
    unittest.main()
