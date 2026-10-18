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

from latexprep import build_workspace, latexmk_build, project
from latexprep.build_log import log_findings
from latexprep.command_runner import (
    CommandFailure,
    CommandResult,
    RuntimeLimits,
    ToolVersion,
)
from latexprep.errors import PreparationError, SandboxRefusedError
from latexprep.latexmk_build import build_project
from latexprep.resource_monitor import ResourceMonitor
from latexprep.runtime import ToolRunner
from latexprep.tool_launch import create_tool_environment, sandbox_refusal, with_ulimits
from tests.fakes import (
    FakeBuildRunner,
    FixtureToolchain,
    LifecycleRunner,
    UnconfinedSandbox,
    UnwatchedMonitor,
)


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

        runner = ContextRunner(
            trace=f"INPUT {self.source / 'main.tex'}\n", resource_roots=[self.source]
        )
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
        scan = project.folder_entries

        def scan_then_swap(*args, **kwargs):
            entries = scan(*args, **kwargs)
            victim.unlink()
            victim.symlink_to(secret)
            return entries

        with patch.object(project, "folder_entries", scan_then_swap):
            with self.assertRaisesRegex(PreparationError, "replaced by a link"):
                await build_project(
                    self.source, "main.tex", self.root / "build", "pdflatex", FakeBuildRunner()
                )
        copied = [path for path in (self.root / "build").rglob("*") if path.is_file()]
        self.assertFalse([path for path in copied if path.read_text() == "outside the project"])

    async def test_report_records_how_long_the_build_command_took(self) -> None:
        class TimedBuildRunner(FakeBuildRunner):
            async def run(self, argv, cwd, workspace, **kwargs):
                result = await super().run(argv, cwd, workspace, **kwargs)
                if argv[-1] in {"-v", "--version"}:
                    return result
                return replace(result, duration_seconds=2.5)

        result = await build_project(
            self.source, "main.tex", self.root / "timed", "pdflatex", TimedBuildRunner()
        )
        self.assertTrue(result.success, result.findings)
        execution = result.tools.to_report()["execution"]
        assert isinstance(execution, dict)
        self.assertEqual(execution["duration_seconds"], 2.5)

    async def test_oversized_build_input_names_the_limit_and_its_value(self) -> None:
        (self.source / "extra.tex").write_text("more")
        with patch.object(build_workspace, "MAX_WORKSPACE_ENTRIES", 1):
            with self.assertRaisesRegex(PreparationError, "exceeds the 1 entry limit"):
                await build_project(
                    self.source, "main.tex", self.root / "entries", "pdflatex", FakeBuildRunner()
                )
        (self.source / "extra.tex").write_bytes(b"x" * 2048)
        small = FakeBuildRunner()
        small.limits = RuntimeLimits(max_copy_bytes=1024)
        with self.assertRaisesRegex(
            PreparationError, "exceeds the 1 KiB size limit at 'extra.tex'"
        ):
            await build_project(self.source, "main.tex", self.root / "bytes", "pdflatex", small)

    async def test_the_build_copy_is_bounded_by_its_own_limit_not_the_per_file_tool_limit(
        self,
    ) -> None:
        (self.source / "extra.tex").write_bytes(b"x" * 2048)
        runner = FakeBuildRunner()
        runner.limits = RuntimeLimits(max_file_bytes=1024)
        result = await build_project(
            self.source, "main.tex", self.root / "bounded", "pdflatex", runner
        )
        self.assertTrue(result.success)

    async def test_copying_the_project_does_not_block_the_event_loop(self) -> None:
        threads: list[int] = []
        prepare = latexmk_build.prepare_layout

        def recorded(*args, **kwargs):
            threads.append(threading.get_ident())
            return prepare(*args, **kwargs)

        with patch.object(latexmk_build, "prepare_layout", recorded):
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

    async def test_a_version_probe_stopped_by_a_limit_names_the_setting_to_change(self) -> None:
        cases = (
            (
                "Workspace exceeded the total disk limit",
                ("max_generated_file_bytes", "max_import_bytes"),
            ),
            ("A generated file exceeded the file size limit", ("max_generated_file_bytes",)),
            ("Job temporary storage exceeded 99 bytes", ("max_temporary_bytes",)),
            ("Aggregate job memory including the parent exceeded 9 MiB", ("memory_mb",)),
            ("Aggregate tool memory exceeded 9 MiB", ("build_memory_mb",)),
        )
        for violation, settings in cases:
            runner = LifecycleRunner()

            async def stopped(argv, *_args, _violation=violation, **_options):
                return CommandResult(
                    -9, "", "", resolved_command=argv, resource_exceeded=_violation
                )

            with self.subTest(violation=violation), patch.object(runner, "run", stopped):
                with self.assertRaises(PreparationError) as raised:
                    await runner.tool_version([self.perl, "-v"], self.work)
            message = str(raised.exception)
            self.assertIn("Cannot verify tool perl: it exceeded a resource limit", message)
            self.assertIn(violation, message)
            for setting in settings:
                self.assertIn(setting, message)
            self.assertNotIn("reinstall", message)

    async def test_tool_state_exists_during_the_command_and_is_removed_after_it(self) -> None:
        runner = LifecycleRunner()
        result = await runner.run(
            [self.perl, "-e", 'print $ENV{TMPDIR}; print -d $ENV{TMPDIR} ? " exists" : " missing"'],
            self.work,
            self.work,
        )
        temporary, state = result.stdout.rsplit(" ", 1)[0], result.stdout.rsplit(" ", 1)[1]
        self.assertEqual(state, "exists")
        self.assertTrue(Path(temporary).is_relative_to(self.work / ".tool-state"))
        self.assertFalse(Path(temporary).exists())
        self.assertEqual(list((self.work / ".tool-state").iterdir()), [])

    async def test_tool_state_is_removed_even_when_the_command_is_refused(self) -> None:
        runner = LifecycleRunner(RuntimeLimits(max_memory_mb=64))
        with self.assertRaises(ValueError):
            await runner.run([self.perl, "-e", "exit 0"], self.work, self.work, memory_mb=128)
        self.assertEqual(list((self.work / ".tool-state").glob("invocation-*")), [])

    async def test_command_duration_is_measured(self) -> None:
        runner = LifecycleRunner()
        result = await runner.run(
            [self.perl, "-e", "select(undef, undef, undef, 0.3)"], self.work, self.work
        )
        self.assertGreaterEqual(result.duration_seconds, 0.25)
        self.assertLess(result.duration_seconds, 10)

    def test_failure_names_the_cause_and_the_value_that_was_exceeded(self) -> None:
        limits = RuntimeLimits(timeout_seconds=7, max_output_bytes=3 * 1024 * 1024)
        cases = (
            (dict(timed_out=True, returncode=-9), CommandFailure.TIMEOUT, "stopped after 7 s"),
            (dict(output_limited=True, returncode=-9), CommandFailure.OUTPUT_LIMIT, "3 MiB"),
            (
                dict(resource_exceeded="Aggregate tool memory exceeded 9 MiB", returncode=-9),
                CommandFailure.RESOURCE_LIMIT,
                "exceeded 9 MiB",
            ),
            (dict(returncode=2, stderr="boom"), CommandFailure.EXIT_STATUS, "failed (2): boom"),
        )
        for fields in cases:
            arguments, failure, expected = fields
            with self.subTest(failure=failure):
                result = CommandResult(
                    arguments.pop("returncode"),
                    "",
                    arguments.pop("stderr", ""),
                    resolved_command=["tool"],
                    **arguments,
                )
                self.assertIs(result.failure, failure)
                self.assertIn(expected, result.failure_reason("tool", limits) or "")
        clean = CommandResult(0, "", "", resolved_command=["tool"])
        self.assertIsNone(clean.failure)
        self.assertIsNone(clean.failure_reason("tool", limits))
        # Without limits the older, value-free wording remains.
        timed_out = CommandResult(-9, "", "", resolved_command=["tool"], timed_out=True)
        self.assertEqual(
            timed_out.failure_reason("tool"), "tool exceeded the configured time limit"
        )

    async def test_resource_accounting_failure_is_closed(self) -> None:
        runner = LifecycleRunner()

        async def unavailable(pid: int, workspace: Path, *, max_file_bytes: int) -> str:
            return "accounting unavailable"

        with patch.object(runner.monitor, "watch_command", unavailable):
            result = await runner.run([self.perl, "-e", "sleep 30"], self.work, self.work)
        self.assertEqual(result.resource_exceeded, "accounting unavailable")
        self.assertNotEqual(result.returncode, 0)

    async def test_unexpected_accounting_error_names_its_type(self) -> None:
        runner = LifecycleRunner()

        async def broken(pid: int, workspace: Path, *, max_file_bytes: int) -> str:
            raise ValueError("bad row")

        with patch.object(runner.monitor, "watch_command", broken):
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
            environment = create_tool_environment(
                self.work,
                runner.toolchain.context(Path(self.perl)),
                source_date_epoch=runner.source_date_epoch,
            ).variables
        self.assertEqual(environment["TEXINPUTS"], ".:")
        # latexmk warns when USER is unset; a neutral name keeps the real one private.
        self.assertEqual(environment["USER"], "fledge")
        for key in ("HOME", "CODEX_HOME", "PERL5OPT", "OPENAI_API_KEY"):
            self.assertNotIn(key, environment)
        self.assertTrue(environment["TEXMFHOME"].startswith(str(self.work)))

    async def test_environment_names_the_state_directory_it_created(self) -> None:
        runner = ToolRunner()
        environment = create_tool_environment(
            self.work,
            runner.toolchain.context(Path(self.perl)),
            source_date_epoch=runner.source_date_epoch,
        )
        state = environment.state_directory
        self.assertEqual(state.parent, self.work / ".tool-state")
        for name in ("tmp", "config", "cache", "texmf", "texmf-config", "texmf-var"):
            self.assertTrue((state / name).is_dir(), name)
        self.assertEqual(environment.variables["TMPDIR"], str(state / "tmp"))

    async def test_environment_keeps_long_log_lines_unwrapped(self) -> None:
        runner = ToolRunner()
        environment = create_tool_environment(
            self.work,
            runner.toolchain.context(Path(self.perl)),
            source_date_epoch=runner.source_date_epoch,
        ).variables
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
        environment = create_tool_environment(
            self.work,
            runner.toolchain.context(Path(pdflatex).resolve()),
            source_date_epoch=runner.source_date_epoch,
        ).variables
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
        self.assertEqual(wrapped[3:7], ["fledge-limits", "4", "120", "256"])
        self.assertEqual(wrapped[-1], "/usr/bin/true")
        # Dash counts 512-byte blocks and would halve the limit, so bash wins
        # wherever it exists; macOS /bin/sh is bash and agrees either way.
        self.assertEqual(wrapped[0], "/bin/bash" if Path("/bin/bash").is_file() else "/bin/sh")
        with patch.object(Path, "is_file", return_value=False):
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


class RunnerCompositionTests(unittest.IsolatedAsyncioTestCase):
    """What a runner is built from can be supplied, so tests need not reach into it."""

    def test_the_sandbox_the_toolchain_and_the_monitor_come_from_the_constructor(self) -> None:
        sandbox, toolchain = UnconfinedSandbox(), FixtureToolchain(Path("/fixture"))
        monitors = []

        def monitor_factory(processes, statistics):
            monitors.append((processes, statistics))
            return UnwatchedMonitor(processes, statistics)

        runner = ToolRunner(
            RuntimeLimits(max_memory_mb=256),
            sandbox=sandbox,
            toolchain=toolchain,
            monitor_factory=monitor_factory,
        )
        self.assertIs(runner.sandbox, sandbox)
        self.assertIs(runner.toolchain, toolchain)
        self.assertIsInstance(runner.monitor, UnwatchedMonitor)
        [(processes, statistics)] = monitors
        self.assertIs(processes, sandbox)
        self.assertEqual(statistics.memory_limit_mb, 256)
        self.assertEqual(runner.resource_roots, toolchain.resource_roots)

    def test_without_arguments_the_host_sandbox_is_selected_and_tools_are_found_lazily(
        self,
    ) -> None:
        with patch("latexprep.runtime.Toolchain.discover") as discover:
            runner = ToolRunner()
            discover.assert_not_called()
            self.assertIs(runner.toolchain, discover.return_value)
            self.assertIs(runner.toolchain, discover.return_value)
        discover.assert_called_once_with(runner.sandbox)
        self.assertIsInstance(runner.monitor, ResourceMonitor)

    async def test_commands_run_unwrapped_under_an_unconfined_sandbox(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            result = await LifecycleRunner().run(["/bin/echo", "hello"], work, work)
        self.assertEqual((result.returncode, result.stdout), (0, "hello\n"))


if __name__ == "__main__":
    unittest.main()
