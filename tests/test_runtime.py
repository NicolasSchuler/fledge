from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from latexprep.models import PreparationError
from latexprep.runtime import (
    CommandResult,
    RuntimeLimits,
    ToolRunner,
    _log_findings,
    build_project,
)


class LifecycleRunner(ToolRunner):
    """Test process lifetimes without Seatbelt or host process accounting.

    The enclosing sandbox blocks both sandbox-exec and ps. Production has no
    bypass option. Isolation and accounting failures have independent tests.
    """

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


class FakeBuildRunner(ToolRunner):
    def __init__(self, returncode: int = 0, log: str = "", trace: str | None = None) -> None:
        super().__init__()
        self._test_resource_roots: set[Path] = set()
        self.returncode = returncode
        self.log = log
        self.trace = trace
        self.commands: list[list[str]] = []

    @property
    def resource_roots(self) -> frozenset[Path]:
        return frozenset(self._test_resource_roots)

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
        self.commands.append(argv)
        if argv[-1] in {"-v", "--version"}:
            return CommandResult(0, "test tool version\n", "", False, argv)
        name = Path(argv[-1]).stem
        output = workspace / "output"
        (output / f"{name}.pdf").write_bytes(b"%PDF-1.7\npartial or successful")
        (output / f"{name}.log").write_text(self.log)
        (output / f"{name}.fls").write_text(self.trace or f"INPUT {cwd / argv[-1]}\n")
        return CommandResult(self.returncode, "", "", False, argv)


class BuildTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        self.source.mkdir()
        (self.source / "main.tex").write_text("source content")

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_failed_tool_never_accepts_partial_pdf(self) -> None:
        result = await build_project(
            self.source, "main.tex", self.root / "build", "pdflatex", FakeBuildRunner(returncode=1)
        )
        self.assertFalse(result.success)
        self.assertIsNone(result.pdf)
        self.assertIn("build.failed", {item.rule for item in result.findings})

    async def test_final_rerun_request_blocks_success(self) -> None:
        result = await build_project(
            self.source,
            "main.tex",
            self.root / "build",
            "pdflatex",
            FakeBuildRunner(log="LaTeX Warning: Rerun to get cross-references right."),
        )
        self.assertFalse(result.success)
        self.assertIn("build.rerun_required", {item.rule for item in result.findings})
        self.assertIn("BLD008", {item.code for item in result.findings})

    async def test_build_copy_has_fresh_output_and_safe_main(self) -> None:
        (self.source / "-main.tex").write_text("test source")
        (self.source / "-main.pdf").write_bytes(b"%PDF-old")
        (self.source / ".latexmkrc").write_text("die 'must not run'\n")
        runner = FakeBuildRunner()
        result = await build_project(
            self.source, "-main.tex", self.root / "build", "pdflatex", runner
        )
        self.assertTrue(result.success)
        self.assertIn("-main.tex", result.dependencies)
        command = runner.commands[-1]
        self.assertEqual(command[-1], "./-main.tex")
        self.assertIn("-norc", command)
        self.assertIn("-no-shell-escape", command)
        self.assertIn("-use-make-", command)
        self.assertEqual((self.source / "-main.pdf").read_bytes(), b"%PDF-old")
        self.assertEqual((self.source / "-main.tex").read_text(), "test source")

    async def test_reused_workspace_and_escaping_main_are_rejected(self) -> None:
        work = self.root / "build"
        work.mkdir()
        with self.assertRaises(PreparationError):
            await build_project(self.source, "main.tex", work, "pdflatex", FakeBuildRunner())
        with self.assertRaises(PreparationError):
            await build_project(
                self.source, "../main.tex", self.root / "unused", "pdflatex", FakeBuildRunner()
            )
        with self.assertRaises(PreparationError):
            await build_project(
                self.source, "main;touch.tex", self.root / "unused", "pdflatex", FakeBuildRunner()
            )

    async def test_external_recorder_dependency_blocks_success(self) -> None:
        result = await build_project(
            self.source,
            "main.tex",
            self.root / "build",
            "pdflatex",
            FakeBuildRunner(trace=f"INPUT {self.source / 'main.tex'}\n"),
        )
        self.assertFalse(result.success)
        self.assertIn("build.external_dependency", {item.rule for item in result.findings})

    async def test_recorder_uses_command_roots_and_tool_metadata_is_a_snapshot(self) -> None:
        metadata: dict[str, object] = {"version": ["build tool test"], "executable": "/tools/build"}
        declared = self.root / "toolchain"
        declared.mkdir()

        class ContextRunner(FakeBuildRunner):
            async def tool_version(self, argv: list[str], workspace: Path) -> dict[str, object]:
                return metadata

            async def run(
                self,
                argv: list[str],
                cwd: Path,
                workspace: Path,
                *,
                readonly_inputs: tuple[Path, ...] = (),
                memory_mb: int | None = None,
            ) -> CommandResult:
                result = await super().run(argv, cwd, workspace)
                return replace(result, resource_roots=(str(declared),))

        runner = ContextRunner(trace=f"INPUT {self.source / 'main.tex'}\n")
        runner._test_resource_roots.add(self.source)
        result = await build_project(
            self.source, "main.tex", self.root / "build", "pdflatex", runner
        )
        self.assertFalse(result.success)
        external = next(
            item for item in result.findings if item.rule == "build.external_dependency"
        )
        self.assertEqual(external.path, "main.tex")
        self.assertEqual(external.status, "failed")
        self.assertEqual(external.severity, "error")
        self.assertEqual(result.tools["resource_roots"], [str(declared)])
        metadata["version"] = ["changed later"]
        self.assertEqual(
            result.tools["latexmk"], {"version": ["build tool test"], "executable": "/tools/build"}
        )

    async def test_symlink_source_is_rejected(self) -> None:
        (self.source / "external.tex").symlink_to(self.root / "unrelated")
        with self.assertRaises(PreparationError):
            await build_project(
                self.source, "main.tex", self.root / "build", "pdflatex", FakeBuildRunner()
            )

    def test_diagnostics_do_not_invent_source_locations(self) -> None:
        log = (
            "Overfull \\hbox (4.2pt too wide) in paragraph at lines 12--14\n"
            "LaTeX Warning: Citation `missing' on page 1 undefined on input line 20.\n"
            "! Undefined control sequence.\n"
        )
        findings = _log_findings(log, "main.tex")
        overflow = next(item for item in findings if item.rule == "build.overfull_box")
        self.assertEqual(overflow.details["overflow_pt"], 4.2)
        self.assertEqual(overflow.code, "BLD006")
        self.assertIsNone(overflow.line)
        self.assertIsNone(overflow.path)
        self.assertEqual(overflow.details["input_line"], 12)
        self.assertEqual(overflow.details["document"], "main.tex")
        self.assertIn("build.undefined_citation", {item.rule for item in findings})
        self.assertIn("build.tex_error", {item.rule for item in findings})

    def test_build_log_check_codes_match_diagnostics(self) -> None:
        cases = (
            ("BLD001", "LaTeX Warning: Citation `missing' on page 1 undefined.", "error"),
            ("BLD002", "LaTeX Warning: Reference `missing' on page 1 undefined.", "error"),
            ("BLD003", "LaTeX Warning: Label `same' multiply defined.", "warning"),
            ("BLD004", "Missing character: There is no 你 in font cmr10!", "warning"),
            ("BLD005", "LaTeX Font Warning: Font shape unavailable, using substitute.", "warning"),
            ("BLD006", "Overfull \\hbox (4.2pt too wide) at lines 12--14", "warning"),
            ("BLD007", "Underfull \\vbox (badness 10000) while output is active", "warning"),
            ("BLD008", "LaTeX Warning: Rerun to get cross-references right.", "error"),
            ("BLD009", "! Undefined control sequence.", "error"),
        )
        for code, message, severity in cases:
            with self.subTest(code=code):
                findings = _log_findings(message, "main.tex")
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0].code, code)
                self.assertEqual(findings[0].message, message)
                self.assertEqual(findings[0].severity, severity)
                self.assertEqual(findings[0].status, "failed")
                self.assertIsNone(findings[0].path)
                self.assertIsNone(findings[0].line)
                self.assertEqual(findings[0].details["document"], "main.tex")
        self.assertEqual(
            _log_findings(
                "Citation and reference resolved.\nOutput written on main.pdf (1 page).", "main.tex"
            ),
            [],
        )

    def test_explicit_diagnostic_filename_identifies_included_source(self) -> None:
        findings = _log_findings(
            "./sections/method.tex:21: Undefined control sequence.",
            "article/main.tex",
        )
        self.assertEqual(findings[0].path, "article/sections/method.tex")
        self.assertEqual(findings[0].line, 21)

    def test_explicit_absolute_filename_is_mapped_only_within_build_source(self) -> None:
        findings = _log_findings(
            f"{self.source}/sections/method.tex:21: Undefined control sequence.",
            "main.tex",
            self.source,
            self.source,
        )
        self.assertEqual(findings[0].path, "sections/method.tex")
        outside = _log_findings(
            f"{self.root}/elsewhere.tex:22: Undefined control sequence.",
            "main.tex",
            self.source,
            self.source,
        )
        self.assertIsNone(outside[0].path)
        self.assertIsNone(outside[0].line)


class RuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name).resolve()
        perl = shutil.which("perl")
        if perl is None:
            self.skipTest("Perl is unavailable for subprocess lifecycle integration tests")
        self.perl = perl

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_output_limit_stops_real_process(self) -> None:
        runner = LifecycleRunner(RuntimeLimits(max_output_bytes=4096))
        result = await runner.run(
            [self.perl, "-e", 'print "a" x 2000000; sleep 10;'], self.work, self.work
        )
        self.assertTrue(result.output_limited)
        self.assertLessEqual(len(result.stdout) + len(result.stderr), 4096)
        self.assertNotEqual(result.returncode, 0)

    async def test_timeout_terminates_child_group(self) -> None:
        runner = LifecycleRunner(RuntimeLimits(timeout_seconds=1))
        program = (
            '$|=1; if (fork()==0) { open(my $f, ">", "heartbeat"); $f->autoflush(1); '
            'while(1) { print $f "x"; select(undef,undef,undef,.05); }} sleep 30;'
        )
        result = await runner.run([self.perl, "-MIO::Handle", "-e", program], self.work, self.work)
        self.assertTrue(result.timed_out, result)
        heartbeat = self.work / "heartbeat"
        size = heartbeat.stat().st_size
        await asyncio.sleep(0.15)
        self.assertEqual(heartbeat.stat().st_size, size)

    async def test_cancellation_terminates_child_group(self) -> None:
        runner = LifecycleRunner()
        program = (
            'if (fork()==0) { open(my $f, ">", "heartbeat"); $f->autoflush(1); '
            'while(1) { print $f "x"; select(undef,undef,undef,.05); }} sleep 30;'
        )
        task = asyncio.create_task(
            runner.run([self.perl, "-MIO::Handle", "-e", program], self.work, self.work)
        )
        heartbeat = self.work / "heartbeat"
        async with asyncio.timeout(3):
            while not heartbeat.exists():
                await asyncio.sleep(0.01)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        size = heartbeat.stat().st_size
        await asyncio.sleep(0.15)
        self.assertEqual(heartbeat.stat().st_size, size)

    async def test_resource_accounting_failure_is_closed(self) -> None:
        runner = LifecycleRunner()

        async def unavailable(pid: int, workspace: Path) -> str:
            return "accounting unavailable"

        with patch.object(runner, "_resource_monitor", unavailable):
            result = await runner.run([self.perl, "-e", "sleep 30"], self.work, self.work)
        self.assertEqual(result.resource_exceeded, "accounting unavailable")
        self.assertNotEqual(result.returncode, 0)

    async def test_environment_discards_injection_and_private_variables(self) -> None:
        with patch.dict(
            os.environ,
            {"TEXINPUTS": "/private/source//", "PERL5OPT": "danger", "OPENAI_API_KEY": "private"},
        ):
            environment = ToolRunner()._environment(self.work, Path(self.perl))
        self.assertEqual(environment["TEXINPUTS"], ".:")
        for key in ("HOME", "CODEX_HOME", "PERL5OPT", "OPENAI_API_KEY"):
            self.assertNotIn(key, environment)
        self.assertTrue(environment["TEXMFHOME"].startswith(str(self.work)))

    async def test_missing_backend_never_runs_command(self) -> None:
        with (
            patch("latexprep.runtime.sys.platform", "unsupported"),
            self.assertRaises(PreparationError),
        ):
            await ToolRunner().run([self.perl, "-e", 'open(F, ">marker")'], self.work, self.work)
        self.assertFalse((self.work / "marker").exists())

    async def test_real_sandbox_denies_external_file_and_network(self) -> None:
        runner = ToolRunner()
        try:
            probe = await runner.run([self.perl, "-e", 'print "ready"'], self.work, self.work)
        except PreparationError as error:
            self.skipTest(str(error))
        self.assertEqual(probe.returncode, 0, probe.stderr)
        outside = self.work.parent / f"latexprep-secret-{self.work.name}"
        outside.write_text("secret")
        accepted = False

        def connection(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            nonlocal accepted
            accepted = True
            writer.close()

        server = await asyncio.start_server(connection, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        try:
            program = (
                'use IO::Socket::INET; my $f; print(open($f,"<",$ARGV[0]) ? "READ" : "DENIED"); '
                'my $s=IO::Socket::INET->new(PeerAddr=>"127.0.0.1",PeerPort=>$ARGV[1],Timeout=>1); '
                'print($s ? "NETWORK" : " BLOCKED");'
            )
            result = await runner.run(
                [self.perl, "-e", program, str(outside), str(port)], self.work, self.work
            )
            self.assertEqual(result.stdout, "DENIED BLOCKED", result.stderr)
            self.assertFalse(accepted)
        finally:
            server.close()
            await server.wait_closed()
            outside.unlink()


if __name__ == "__main__":
    unittest.main()
