"""Tool versions, frozen inputs and the sandbox grants of a runner."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from latexprep.command_runner import CommandResult
from latexprep.errors import PreparationError
from latexprep.resource_budget import ResourceBudget
from latexprep.runtime import ToolRunner
from latexprep.sandbox import SeatbeltSandbox
from latexprep.tool_launch import create_tool_environment
from latexprep.toolchain import ToolContext
from tests.fakes import (
    FixtureToolchain,
)
from tests.support import live_test


class ProbeRunner(ToolRunner):
    """A runner whose every command waits for ``release`` and then returns ``response``.

    Version probes through it can be held open, so tests can observe concurrent and cancelled
    probes.

    Attributes:
        calls: The number of commands run.
        entered: Set once a command has started.
        release: Set by the test to let waiting commands return.
        response: The result every command returns.
        owner: The task that ran the latest command, or ``None`` before the first.
    """

    def __init__(self) -> None:
        super().__init__(source_date_epoch=123, toolchain=FixtureToolchain(Path("/tools")))
        self.calls = 0
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.response = CommandResult(
            0, "pdfinfo version test\n", "", resolved_command=["/tools/pdfinfo"]
        )
        self.owner: asyncio.Task[object] | None = None

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        """Count the command, record its task and return ``response`` once ``release`` is set.

        The arguments are those of :meth:`CommandRunner.run`; the double ignores them.
        """
        self.calls += 1
        self.owner = asyncio.current_task()
        self.entered.set()
        await self.release.wait()
        return self.response


class VersionAndInputTests(unittest.IsolatedAsyncioTestCase):
    """Version probes, the source date epoch, frozen inputs and the Seatbelt grants of a runner.

    A version probe runs once per command and flags, publishes only a successful result and
    returns detached metadata; a frozen input is an exact snapshot that the sandbox grants as a
    literal path, and the workspace is the only writable leaf.
    """

    async def asyncSetUp(self) -> None:
        """Create a temporary directory as ``self.root``."""
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name).resolve()

    async def asyncTearDown(self) -> None:
        """Remove the temporary directory."""
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
        with self.assertRaises(FrozenInstanceError):
            left.version = ("changed",)  # type: ignore[misc]
        tools = runner.tool_versions
        tools.pop("pdfinfo")
        report = left.to_report()
        report["version"].append("changed")
        current = await runner.tool_version(["pdfinfo", "-v"], self.root)
        self.assertEqual(current.version, ("pdfinfo version test",))
        self.assertEqual(current.to_report()["version"], ["pdfinfo version test"])
        self.assertEqual(runner.calls, 1)
        self.assertEqual(runner.statistics().version_probes, 1)

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
        self.assertEqual(version.version, ("pdfinfo version test",))
        self.assertEqual(runner.calls, 2)

    async def test_failed_limited_and_empty_version_probes_do_not_enter_cache(self) -> None:
        runner = ProbeRunner()
        runner.release.set()
        cases = (
            CommandResult(1, "partial", "error", resolved_command=["/tools/pdfinfo"]),
            CommandResult(0, "version", "", resolved_command=["/tools/pdfinfo"], timed_out=True),
            CommandResult(
                0, "version", "", resolved_command=["/tools/pdfinfo"], output_limited=True
            ),
            CommandResult(
                0, "version", "", resolved_command=["/tools/pdfinfo"], resource_exceeded="memory"
            ),
            CommandResult(0, "", "", resolved_command=["/tools/pdfinfo"]),
            CommandResult(
                0, "version", "Syntax Error: invalid", resolved_command=["/tools/pdfinfo"]
            ),
        )
        for response in cases:
            runner.response = response
            with self.assertRaises(PreparationError):
                await runner.tool_version(["pdfinfo", "-v"], self.root)
            self.assertEqual(runner.tool_versions, {})
        runner.response = CommandResult(0, "valid", "", resolved_command=["/tools/pdfinfo"])
        self.assertEqual(
            (await runner.tool_version(["pdfinfo", "-v"], self.root)).version, ("valid",)
        )
        self.assertEqual(runner.calls, len(cases) + 1)

    async def test_version_cache_keys_include_flags_and_detach_99_needs_its_prefix(self) -> None:
        runner = ProbeRunner()
        runner.release.set()
        runner.response = CommandResult(
            99, "", "other tool version 1", resolved_command=["/tools/pdfdetach"]
        )
        with self.assertRaises(PreparationError):
            await runner.tool_version(["pdfdetach", "-v"], self.root)
        runner.response = CommandResult(
            99, "", "pdfdetach version 25.1\n", resolved_command=["/tools/pdfdetach"]
        )
        value = await runner.tool_version(["pdfdetach", "-v"], self.root)
        self.assertEqual(value.version, ("pdfdetach version 25.1",))
        with self.assertRaises(PreparationError):
            await runner.tool_version(["pdfdetach", "--version"], self.root)
        with self.assertRaises(PreparationError):
            await runner.tool_version(["pdfinfo", "-v"], self.root)
        self.assertEqual(runner.calls, 4)

    def test_epoch_context_resolution_and_invocation_state_are_stable(self) -> None:
        runner = ToolRunner(source_date_epoch=17)
        with patch("latexprep.toolchain.tool_path", return_value=Path("/usr/bin/perl")) as resolve:
            self.assertEqual(runner._resolve_tool("perl"), Path("/usr/bin/perl"))
            self.assertEqual(runner._resolve_tool("perl"), Path("/usr/bin/perl"))
            resolve.assert_called_once()
        perl = Path("/usr/bin/perl")
        context = runner.toolchain.context(perl)
        epoch = runner.source_date_epoch
        first = create_tool_environment(self.root, context, source_date_epoch=epoch).variables
        with patch("latexprep.toolchain.shutil.which", return_value="/untrusted/new-tool"):
            self.assertIs(runner.toolchain.context(perl), context)
            second = create_tool_environment(
                self.root, runner.toolchain.context(perl), source_date_epoch=epoch
            ).variables
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
        self.assertEqual(len(runner._frozen._identities), 2)

    def test_readonly_grants_reject_arbitrary_paths_directories_and_changed_snapshots(self) -> None:
        runner = ToolRunner()
        source = self.root / "source.pdf"
        source.write_bytes(b"%PDF-input")
        frozen = runner.freeze_input(source, self.root / "artifacts")
        linked = self.root / "linked.pdf"
        linked.symlink_to(frozen)
        for invalid in (source, frozen.parent, linked):
            with self.assertRaises(PreparationError):
                runner._frozen.validate((invalid,))
        self.assertEqual(runner._frozen.validate((frozen,)), (frozen,))
        frozen.chmod(0o600)
        frozen.write_bytes(b"modified")
        with self.assertRaises(PreparationError):
            runner._frozen.validate((frozen,))
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
        # Generate the Seatbelt profile on any host without requiring macOS tools.
        context = ToolContext(
            Path("/usr/bin/perl"), frozenset({Path("/usr/bin/perl")}), frozenset(), ("/usr/bin",)
        )
        runner.sandbox = SeatbeltSandbox()
        with (
            patch("latexprep.sandbox.shutil.which", return_value="/usr/bin/sandbox-exec"),
            patch.object(runner.toolchain, "context", return_value=context),
        ):
            command = runner._sandbox_command(["/usr/bin/perl", "-v"], workspace, (frozen,))
        self.assertEqual(command[0], "/usr/bin/sandbox-exec")
        profile = command[2]
        self.assertIn(f'(literal "{frozen}")', profile)
        self.assertNotIn(f'(subpath "{frozen.parent}")', profile)
        self.assertIn(f'(allow file-write* (subpath "{workspace}"))', profile)
        self.assertNotIn(f'(subpath "{self.root}")', profile)

    @live_test(
        "opt-in live Seatbelt test (FLEDGE_RUN_INTEGRATION=1)",
        "LATEXPREP_RUN_SANDBOX_TESTS",
        platform="darwin",
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


if __name__ == "__main__":
    unittest.main()
