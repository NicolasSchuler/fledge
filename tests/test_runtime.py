from __future__ import annotations

import asyncio
import os
import shutil
import tempfile
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from latexprep import latexmk_build, project
from latexprep.build_log import log_findings
from latexprep.errors import PreparationError, SandboxRefusedError
from latexprep.latexmk_build import build_project
from latexprep.runtime import CommandResult, RuntimeLimits, ToolRunner, ToolVersion
from latexprep.sandbox import sandbox_refusal, tool_environment, with_ulimits


async def never_reports(pid: int, workspace: Path, *, max_file_bytes: int) -> str:
    """A standalone command watcher that never finds a resource failure."""
    await asyncio.Future()
    raise AssertionError("unreachable")


class LifecycleRunner(ToolRunner):
    """Test process lifetimes without Seatbelt or host process accounting.

    The enclosing sandbox blocks both sandbox-exec and ps. Production has no
    bypass option. Isolation and accounting failures have independent tests.
    """

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._monitor.watch_command = never_reports

    def _sandbox_command(
        self,
        command: list[str],
        workspace: Path,
        readonly_inputs: tuple[Path, ...] = (),
    ) -> list[str]:
        return command


class FakeBuildRunner(ToolRunner):
    # Fakes never run Biber, so automatic builds behave alike on every host.
    biber_needs_preparation = False

    def __init__(self, returncode: int = 0, log: str = "", trace: str | None = None) -> None:
        super().__init__()
        self._test_resource_roots: set[Path] = set()
        self.returncode = returncode
        self.log = log
        self.trace = trace
        self.commands: list[list[str]] = []
        self.biber_preparations = 0

    @property
    def resource_roots(self) -> frozenset[Path]:
        return frozenset(self._test_resource_roots)

    def _resolve_tool(self, name: str) -> Path:
        return Path("/tools") / Path(name).name

    async def prepare_biber(self, root: Path) -> Path | None:
        """Fake builds never launch a real tool, so no toolchain copy is prepared."""
        self.biber_preparations += 1
        return None

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
            return CommandResult(0, "test tool version\n", "", resolved_command=argv)
        name = Path(argv[-1]).stem
        output = workspace / "output"
        (output / f"{name}.pdf").write_bytes(b"%PDF-1.7\npartial or successful")
        (output / f"{name}.log").write_text(self.log)
        engine = (
            "xelatex" if "-xelatex" in argv else "lualatex" if "-lualatex" in argv else "pdflatex"
        )
        primary = output / (name + (".xdv" if engine == "xelatex" else ".pdf"))
        (output / f"{name}.fls").write_text(
            self.trace if self.trace is not None else f"INPUT {cwd / argv[-1]}\nOUTPUT {primary}\n"
        )
        (output / f"{name}.fdb_latexmk").write_text(
            f'# Fdb version 4\n["{engine}"] 1 "{cwd / argv[-1]}" "{primary}" "{name}" 1 0\n'
            f'  "{cwd / argv[-1]}" 1 1 {"0" * 32} ""\n'
            f'  (generated)\n  "{primary}"\n  (rewritten before read)\n'
        )
        return CommandResult(self.returncode, "", "", resolved_command=argv)


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

    async def test_failure_findings_carry_a_bounded_tail_of_the_final_log(self) -> None:
        log = "\n".join(f"log line {number}" for number in range(400)) + "\n! Emergency stop.\n"
        result = await build_project(
            self.source,
            "main.tex",
            self.root / "build",
            "pdflatex",
            FakeBuildRunner(returncode=1, log=log),
        )
        self.assertFalse(result.success)
        failed = next(item for item in result.findings if item.rule == "build.failed")
        tail = failed.details["log_tail"]
        self.assertIsInstance(tail, str)
        self.assertLessEqual(len(tail), 8000)
        self.assertLessEqual(len(tail.splitlines()), 60)
        self.assertIn("! Emergency stop.", tail)
        self.assertIn("log line 399", tail)
        self.assertNotIn("log line 300", tail)
        # Successful builds must not carry build output into the report.
        passed = await build_project(
            self.source, "main.tex", self.root / "passing", "pdflatex", FakeBuildRunner(log="fine")
        )
        self.assertTrue(passed.success)
        self.assertFalse(any("log_tail" in item.details for item in passed.findings))

    async def test_a_limit_that_stopped_the_build_is_reported_once_with_its_value(self) -> None:
        class StoppedBuildRunner(FakeBuildRunner):
            """latexmk was killed by a limit and so left a signal status behind."""

            def __init__(self, **stopped) -> None:
                super().__init__()
                self.limits = RuntimeLimits(timeout_seconds=7, max_output_bytes=3 * 1024 * 1024)
                self.stopped = stopped

            async def run(self, argv, cwd, workspace, **kwargs):
                result = await super().run(argv, cwd, workspace, **kwargs)
                if argv[-1] in {"-v", "--version"}:
                    return result
                return CommandResult(-9, "", "", resolved_command=argv, **self.stopped)

        cases = (
            ({"timed_out": True}, "build.timeout", ("7 s",), "timeout_seconds"),
            ({"output_limited": True}, "build.output_limit", ("3 MiB",), "not configurable"),
            (
                {"resource_exceeded": "Aggregate tool memory exceeded 1024 MiB"},
                "build.resource_limit",
                ("1024 MiB",),
                "build_memory_mb",
            ),
        )
        for index, (stopped, rule, mentioned, setting) in enumerate(cases):
            with self.subTest(rule=rule):
                result = await build_project(
                    self.source,
                    "main.tex",
                    self.root / f"stopped-{index}",
                    "pdflatex",
                    StoppedBuildRunner(**stopped),
                )
                self.assertFalse(result.success)
                errors = [item for item in result.findings if item.severity == "error"]
                self.assertEqual([item.rule for item in errors], [rule])
                for text in mentioned:
                    self.assertIn(text, errors[0].message)
                self.assertIn(setting, errors[0].suggestion or "")

    async def test_missing_pdf_reports_the_log_tail_without_a_tool_failure(self) -> None:
        class EmptyOutputRunner(FakeBuildRunner):
            async def run(self, argv, cwd, workspace, **kwargs):
                result = await super().run(argv, cwd, workspace, **kwargs)
                if argv[-1] not in {"-v", "--version"}:
                    (workspace / "output/main.pdf").write_bytes(b"not a PDF")
                return result

        result = await build_project(
            self.source,
            "main.tex",
            self.root / "build",
            "pdflatex",
            EmptyOutputRunner(log="! LaTeX Error: File `missing.sty' not found."),
        )
        self.assertFalse(result.success)
        missing = next(item for item in result.findings if item.rule == "build.missing_pdf")
        self.assertIn("missing.sty", str(missing.details["log_tail"]))

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
        self.assertIn("-main.tex", result.recorder_project_inputs)
        self.assertEqual(result.submission_inputs, {"-main.tex"})
        command = runner.commands[-1]
        self.assertEqual(command[-1], "./-main.tex")
        self.assertIn("-norc", command)
        self.assertIn("-no-shell-escape", command)
        self.assertIn("-use-make-", command)
        self.assertEqual((self.source / "-main.pdf").read_bytes(), b"%PDF-old")
        self.assertEqual((self.source / "-main.tex").read_text(), "test source")

    async def test_missing_backend_trace_does_not_relabel_pdf_success(self) -> None:
        class MissingDatabaseRunner(FakeBuildRunner):
            async def run(self, argv, cwd, workspace, **kwargs):
                result = await super().run(argv, cwd, workspace, **kwargs)
                if argv[-1] not in {"-v", "--version"}:
                    (workspace / "output/main.fdb_latexmk").unlink()
                return result

        result = await build_project(
            self.source, "main.tex", self.root / "build", "pdflatex", MissingDatabaseRunner()
        )
        self.assertTrue(result.success)
        self.assertTrue(result.recorder_complete)
        self.assertIsNone(result.submission_inputs)
        finding = next(
            item for item in result.findings if item.rule == "build.submission_dependencies"
        )
        self.assertEqual((finding.status, finding.severity), ("inconclusive", "warning"))
        self.assertIn("missing", str(finding.details["uncertainty"]))

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
        metadata = ToolVersion(("build tool test",), "/tools/build", ())
        declared = self.root / "toolchain"
        declared.mkdir()

        class ContextRunner(FakeBuildRunner):
            async def tool_version(self, argv: list[str], workspace: Path) -> ToolVersion:
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
        self.assertEqual(result.tools.resource_roots, (str(declared),))
        report = result.tools.to_report()
        self.assertEqual(report["resource_roots"], [str(declared)])
        report["latexmk"]["version"].append("changed later")
        self.assertEqual(
            result.tools.to_report()["latexmk"],
            {"version": ["build tool test"], "executable": "/tools/build", "version_arguments": []},
        )

    async def test_symlink_source_is_rejected(self) -> None:
        (self.source / "external.tex").symlink_to(self.root / "unrelated")
        with self.assertRaises(PreparationError):
            await build_project(
                self.source, "main.tex", self.root / "build", "pdflatex", FakeBuildRunner()
            )

    async def test_rejected_link_is_named_with_a_remedy(self) -> None:
        (self.source / "external.tex").symlink_to(self.root / "unrelated")
        with self.assertRaises(PreparationError) as raised:
            await build_project(
                self.source, "main.tex", self.root / "build", "pdflatex", FakeBuildRunner()
            )
        self.assertIn("'external.tex'", str(raised.exception))
        self.assertIn("Replace it with a real copy", str(raised.exception))

    async def test_link_that_appears_while_copying_is_refused_not_followed(self) -> None:
        secret = self.root / "secret.txt"
        secret.write_text("outside the project")
        victim = self.source / "data.tex"
        victim.write_text("inside the project")
        scan = project._folder_entries

        def scan_then_swap(*args, **kwargs):
            entries = scan(*args, **kwargs)
            victim.unlink()
            victim.symlink_to(secret)
            return entries

        with patch.object(project, "_folder_entries", scan_then_swap):
            with self.assertRaisesRegex(PreparationError, "replaced by a link"):
                await build_project(
                    self.source, "main.tex", self.root / "build", "pdflatex", FakeBuildRunner()
                )
        copied = [path for path in (self.root / "build").rglob("*") if path.is_file()]
        self.assertFalse([path for path in copied if path.read_text() == "outside the project"])

    async def test_oversized_build_input_names_the_limit_and_its_value(self) -> None:
        (self.source / "extra.tex").write_text("more")
        with patch.object(latexmk_build, "_MAX_IMPORTED_ENTRIES", 1):
            with self.assertRaisesRegex(PreparationError, "exceeds the 1 entry limit"):
                await build_project(
                    self.source, "main.tex", self.root / "entries", "pdflatex", FakeBuildRunner()
                )
        (self.source / "extra.tex").write_bytes(b"x" * 2048)
        small = FakeBuildRunner()
        small.limits = RuntimeLimits(max_file_bytes=1024)
        with self.assertRaisesRegex(
            PreparationError, "exceeds the 1 KiB size limit at 'extra.tex'"
        ):
            await build_project(self.source, "main.tex", self.root / "bytes", "pdflatex", small)

    async def test_copying_the_project_does_not_block_the_event_loop(self) -> None:
        threads: list[int] = []
        prepare = latexmk_build._prepare_tree

        def recorded(*args, **kwargs):
            threads.append(threading.get_ident())
            return prepare(*args, **kwargs)

        with patch.object(latexmk_build, "_prepare_tree", recorded):
            await build_project(
                self.source, "main.tex", self.root / "build", "pdflatex", FakeBuildRunner()
            )
        self.assertEqual(len(threads), 1)
        self.assertNotEqual(threads[0], threading.get_ident())

    def test_diagnostics_do_not_invent_source_locations(self) -> None:
        log = (
            "Overfull \\hbox (4.2pt too wide) in paragraph at lines 12--14\n"
            "LaTeX Warning: Citation `missing' on page 1 undefined on input line 20.\n"
            "! Undefined control sequence.\n"
        )
        findings = log_findings(log, "main.tex")
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
                findings = log_findings(message, "main.tex")
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0].code, code)
                self.assertEqual(findings[0].message, message)
                self.assertEqual(findings[0].severity, severity)
                self.assertEqual(findings[0].status, "failed")
                self.assertIsNone(findings[0].path)
                self.assertIsNone(findings[0].line)
                self.assertEqual(findings[0].details["document"], "main.tex")
        self.assertEqual(
            log_findings(
                "Citation and reference resolved.\nOutput written on main.pdf (1 page).", "main.tex"
            ),
            [],
        )

    def test_explicit_diagnostic_filename_identifies_included_source(self) -> None:
        findings = log_findings(
            "./sections/method.tex:21: Undefined control sequence.",
            "article/main.tex",
        )
        self.assertEqual(findings[0].path, "article/sections/method.tex")
        self.assertEqual(findings[0].line, 21)

    def test_explicit_absolute_filename_is_mapped_only_within_build_source(self) -> None:
        findings = log_findings(
            f"{self.source}/sections/method.tex:21: Undefined control sequence.",
            "main.tex",
            self.source,
            self.source,
        )
        self.assertEqual(findings[0].path, "sections/method.tex")
        outside = log_findings(
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

    def test_both_backends_report_refused_isolation_by_their_own_markers(self) -> None:
        for stderr in (
            "sandbox-exec: sandbox_apply: Operation not permitted",
            "bwrap: No permissions to create new namespace, likely because the kernel does "
            "not allow non-privileged user namespaces.\n",
            "\nbwrap: setting up uid map: Permission denied",
        ):
            with self.subTest(stderr=stderr):
                self.assertTrue(sandbox_refusal(stderr))
        for stderr in ("", "! LaTeX Error: File `x.sty' not found.", "see bwrap: in the manual"):
            with self.subTest(stderr=stderr):
                self.assertFalse(sandbox_refusal(stderr))

    async def test_bubblewrap_refusal_is_not_reported_as_a_tool_failure(self) -> None:
        runner = LifecycleRunner()
        script = 'print STDERR "bwrap: No permissions to create new namespace\\n"; exit 1;'
        with self.assertRaisesRegex(
            SandboxRefusedError, "Restricted execution was refused: bwrap:"
        ):
            await runner.tool_version([self.perl, "-e", script], self.work)

    async def test_unverifiable_tool_is_named_without_its_path_or_diagnostics(self) -> None:
        runner = LifecycleRunner()
        script = 'print STDERR "Error: leaked detail\\n"; exit 3;'
        with self.assertRaises(PreparationError) as raised:
            await runner.tool_version([self.perl, "-e", script], self.work)
        message = str(raised.exception)
        self.assertTrue(message.startswith("Cannot verify tool perl: "), message)
        self.assertIn("exited with status 3", message)
        self.assertNotIn(self.perl, message)
        self.assertNotIn("leaked detail", message)
        with self.assertRaisesRegex(PreparationError, "it printed no version"):
            await runner.tool_version([self.perl, "-e", "exit 0;"], self.work)

    async def test_resource_accounting_failure_is_closed(self) -> None:
        runner = LifecycleRunner()

        async def unavailable(pid: int, workspace: Path, *, max_file_bytes: int) -> str:
            return "accounting unavailable"

        with patch.object(runner._monitor, "watch_command", unavailable):
            result = await runner.run([self.perl, "-e", "sleep 30"], self.work, self.work)
        self.assertEqual(result.resource_exceeded, "accounting unavailable")
        self.assertNotEqual(result.returncode, 0)

    async def test_unexpected_accounting_error_names_its_type(self) -> None:
        runner = LifecycleRunner()

        async def broken(pid: int, workspace: Path, *, max_file_bytes: int) -> str:
            raise ValueError("bad row")

        with patch.object(runner._monitor, "watch_command", broken):
            result = await runner.run([self.perl, "-e", "sleep 30"], self.work, self.work)
        self.assertEqual(
            result.resource_exceeded, "Resource accounting failed: ValueError: bad row"
        )

    async def test_environment_discards_injection_and_private_variables(self) -> None:
        with patch.dict(
            os.environ,
            {
                "TEXINPUTS": "/private/source//",
                "PERL5OPT": "danger",
                "OPENAI_API_KEY": "private",
                "USER": "private-account",
            },
        ):
            runner = ToolRunner()
            environment = tool_environment(
                self.work,
                runner.toolchain.context(Path(self.perl)),
                source_date_epoch=runner.source_date_epoch,
            )
        self.assertEqual(environment["TEXINPUTS"], ".:")
        # latexmk warns when USER is unset; a neutral name keeps the real one private.
        self.assertEqual(environment["USER"], "fledge")
        for key in ("HOME", "CODEX_HOME", "PERL5OPT", "OPENAI_API_KEY"):
            self.assertNotIn(key, environment)
        self.assertTrue(environment["TEXMFHOME"].startswith(str(self.work)))

    async def test_environment_keeps_long_log_lines_unwrapped(self) -> None:
        runner = ToolRunner()
        environment = tool_environment(
            self.work,
            runner.toolchain.context(Path(self.perl)),
            source_date_epoch=runner.source_date_epoch,
        )
        self.assertEqual(environment["max_print_line"], "1000")
        self.assertEqual(environment["error_line"], "254")
        self.assertEqual(environment["half_error_line"], "238")

    async def test_long_citation_keys_reach_the_findings_whole(self) -> None:
        pdflatex = shutil.which("pdflatex")
        if pdflatex is None:
            self.skipTest("pdflatex is unavailable")
        key = "averyveryverylongcitationkeyforsomethingdeep2020"
        (self.work / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\\cite{" + key + "}\\end{document}\n"
        )
        runner = ToolRunner()
        environment = tool_environment(
            self.work,
            runner.toolchain.context(Path(pdflatex).resolve()),
            source_date_epoch=runner.source_date_epoch,
        )
        process = await asyncio.create_subprocess_exec(
            pdflatex,
            "-interaction=nonstopmode",
            "main.tex",
            cwd=self.work,
            env=environment,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await process.wait()
        findings = log_findings((self.work / "main.log").read_text(), "main.tex")
        citations = [item for item in findings if item.rule == "build.undefined_citation"]
        self.assertTrue(any(key in item.message for item in citations), citations)

    def test_file_size_limit_uses_a_shell_that_counts_kibibyte_units(self) -> None:
        wrapped = with_ulimits(["/usr/bin/true"], max_file_bytes=4096, timeout_seconds=120)
        self.assertEqual(wrapped[3:6], ["latexprep-limits", "4", "120"])
        self.assertEqual(wrapped[-1], "/usr/bin/true")
        # Dash counts 512-byte blocks and would halve the limit, so bash wins
        # wherever it exists; macOS /bin/sh is bash and agrees either way.
        self.assertEqual(wrapped[0], "/bin/bash" if Path("/bin/bash").is_file() else "/bin/sh")
        with patch("latexprep.sandbox.os.path.isfile", return_value=False):
            limited = with_ulimits(["/usr/bin/true"], max_file_bytes=4096, timeout_seconds=120)
            self.assertEqual(limited[0], "/bin/sh")

    async def test_missing_backend_never_runs_command(self) -> None:
        with patch("latexprep.sandbox.sys.platform", "unsupported"):
            runner = ToolRunner()
        self.assertEqual(runner.sandbox.description, "unavailable")
        with self.assertRaises(PreparationError):
            await runner.run([self.perl, "-e", 'open(F, ">marker")'], self.work, self.work)
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
