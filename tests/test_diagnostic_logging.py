"""Diagnostic logging: silent by default, opt-in on stderr, and free of local paths.

The library only emits records; the command line attaches one stderr handler when
``--debug`` or ``FLEDGE_DEBUG=1`` asks for it, and removes it when the command ends.
"""

from __future__ import annotations

import io
import json
import logging
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from click.testing import CliRunner

from latexprep.analysed_source import AnalysedSourceCache
from latexprep.cli import cli
from latexprep.command_runner import CommandResult, ToolVersion, argv_summary
from latexprep.config_discovery import resolve_config_path
from latexprep.diagnostic_logging import PACKAGE_LOGGER_NAME, debug_logging
from latexprep.errors import PreparationError
from latexprep.online_http_wire import Response
from latexprep.pdf_tools import DOCUMENT, PdfToolSession
from latexprep.report import Report
from latexprep.resource_budget import ResourceBudget
from latexprep.runtime import ToolRunner
from latexprep.scheduler import Scheduler, Task

from .online_support import FakeTransport, options, public_resolver
from .support import write_project

PACKAGE_LOGGER = logging.getLogger(PACKAGE_LOGGER_NAME)


class RecordingRunner(ToolRunner):
    """A runner whose commands finish at once with a chosen result, without any process."""

    def __init__(self, result: CommandResult) -> None:
        super().__init__()
        self.result = result

    async def _run_command(self, argv, cwd, workspace, readonly_inputs, memory_mb):
        return self.result


def finished(returncode: int = 0, **flags: object) -> CommandResult:
    return CommandResult(returncode, "", "", resolved_command=["tool"], **flags)


class FontsRunner(ToolRunner):
    """Answers every PDF tool command with a fixed output, so no tool is needed."""

    async def tool_version(self, argv, workspace):
        return ToolVersion(("pdffonts 1.0",), "/usr/bin/pdffonts", ("-v",))

    async def run(self, argv, cwd, workspace, *, readonly_inputs=(), memory_mb=None):
        return CommandResult(0, "name type\n", "", resolved_command=argv)


class ArgvSummaryTests(unittest.TestCase):
    def test_keeps_the_tool_and_option_names_but_no_path_or_option_value(self) -> None:
        summary = argv_summary(
            ["/opt/tex/bin/pdftoppm", "-r", "300", "-png", "/home/ann/paper.pdf", "out"]
        )
        self.assertEqual(summary, "pdftoppm -r -png +3 more arguments")

    def test_a_command_without_arguments_is_just_its_name(self) -> None:
        self.assertEqual(argv_summary(["/usr/bin/pdfinfo"]), "pdfinfo")

    def test_path_like_and_valued_options_are_counted_not_shown(self) -> None:
        summary = argv_summary(["latexmk", "-output-directory=/tmp/x", "-pdf", "main.tex"])
        self.assertEqual(summary, "latexmk -pdf +2 more arguments")


class LibraryIsSilentTests(unittest.TestCase):
    def test_the_package_logger_only_has_a_null_handler(self) -> None:
        self.assertTrue(
            any(isinstance(handler, logging.NullHandler) for handler in PACKAGE_LOGGER.handlers)
        )
        self.assertNotIn(
            logging.StreamHandler,
            [type(handler) for handler in PACKAGE_LOGGER.handlers],
        )

    def test_records_are_dropped_without_the_debug_option(self) -> None:
        stream = io.StringIO()
        with patch("sys.stderr", stream):
            logging.getLogger("latexprep.runtime").debug("tool start: pdfinfo")
            logging.getLogger("latexprep.runtime").info("tool stopped")
            logging.getLogger("latexprep.runtime").warning("a warning")
        self.assertEqual(stream.getvalue(), "")


class DebugLoggingTests(unittest.TestCase):
    def test_records_use_relative_time_and_the_module_name_without_the_package(self) -> None:
        stream = io.StringIO()
        with debug_logging(stream):
            logging.getLogger("latexprep.runtime").debug("tool start: %s", "pdfinfo")
            logging.getLogger("latexprep.runtime").info("tool stopped by its %s", "timeout")
        first, second = stream.getvalue().splitlines()
        self.assertRegex(first, r"^\+\d+\.\d{3}s runtime: tool start: pdfinfo$")
        self.assertRegex(second, r"^\+\d+\.\d{3}s runtime \[info\]: tool stopped by its timeout$")

    def test_the_handler_and_level_are_restored_afterwards(self) -> None:
        handlers, level = list(PACKAGE_LOGGER.handlers), PACKAGE_LOGGER.level
        with debug_logging(io.StringIO()):
            self.assertEqual(PACKAGE_LOGGER.level, logging.DEBUG)
            self.assertEqual(len(PACKAGE_LOGGER.handlers), len(handlers) + 1)
        self.assertEqual(PACKAGE_LOGGER.handlers, handlers)
        self.assertEqual(PACKAGE_LOGGER.level, level)

    def test_a_nested_use_attaches_no_second_handler(self) -> None:
        stream = io.StringIO()
        with debug_logging(stream), debug_logging(io.StringIO()):
            logging.getLogger("latexprep.runtime").debug("once")
        self.assertEqual(stream.getvalue().count("once"), 1)

    def test_home_and_temporary_directories_are_shown_generically(self) -> None:
        stream = io.StringIO()
        with patch.dict(os.environ, {"HOME": "/home/ann"}), debug_logging(stream):
            logging.getLogger("latexprep.config_discovery").debug(
                "read %s and %s", "/home/ann/papers/fledge.toml", "/home/ann"
            )
        text = stream.getvalue()
        self.assertNotIn("/home/ann", text)
        self.assertIn("read ~/papers/fledge.toml and ~", text)

    def test_a_logged_exception_prints_its_traceback_without_paths_or_credentials(self) -> None:
        stream = io.StringIO()
        with patch.dict(os.environ, {"HOME": "/home/ann"}), debug_logging(stream):
            try:
                raise RuntimeError("cannot read /home/ann/paper.tex?token=SECRETVALUE\x1b[31m")
            except RuntimeError as error:
                logging.getLogger("latexprep.scheduler").debug("task x: raised", exc_info=error)
        text = stream.getvalue()
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn("RuntimeError: cannot read ~/paper.tex?token=", text)
        self.assertNotIn("/home/ann", text)
        self.assertNotIn("SECRETVALUE", text)
        self.assertNotIn("\x1b", text)

    def test_other_handlers_of_the_same_record_see_it_unchanged(self) -> None:
        seen: list[str] = []

        class Collecting(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                seen.append(record.getMessage())

        with patch.dict(os.environ, {"HOME": "/home/ann"}), debug_logging(io.StringIO()):
            PACKAGE_LOGGER.addHandler(Collecting())
            try:
                logging.getLogger("latexprep.config_discovery").debug("read %s", "/home/ann/x.toml")
            finally:
                PACKAGE_LOGGER.handlers = [
                    item for item in PACKAGE_LOGGER.handlers if not isinstance(item, Collecting)
                ]
        self.assertEqual(seen, ["read /home/ann/x.toml"])

    def test_a_message_with_percent_signs_is_not_formatted_a_second_time(self) -> None:
        stream = io.StringIO()
        with debug_logging(stream):
            logging.getLogger("latexprep.runtime").debug("progress %s", "100%s done")
        self.assertIn("progress 100%s done", stream.getvalue())


class ToolRunnerLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.work = Path(self.temporary.name).resolve()

    async def asyncTearDown(self) -> None:
        self.temporary.cleanup()

    async def test_a_command_logs_its_start_and_end_without_paths(self) -> None:
        runner = RecordingRunner(finished(0))
        argv = ["/opt/tex/bin/pdflatex", "-halt-on-error", str(self.work / "paper.tex")]
        with self.assertLogs("latexprep.runtime", level="DEBUG") as logs:
            await runner.run(argv, self.work, self.work)
        start, end = logs.output
        self.assertIn("tool start: pdflatex -halt-on-error +1 more argument", start)
        self.assertIn("tool end: pdflatex -halt-on-error +1 more argument (exit 0,", end)
        self.assertNotIn(str(self.work), "\n".join(logs.output))
        self.assertTrue(all(record.levelno == logging.DEBUG for record in logs.records))

    async def test_a_nonzero_exit_is_named_in_the_end_line(self) -> None:
        runner = RecordingRunner(finished(2))
        with self.assertLogs("latexprep.runtime", level="DEBUG") as logs:
            await runner.run(["pdfinfo"], self.work, self.work)
        self.assertIn("exit 2", logs.output[-1])
        self.assertIn("exit status", logs.output[-1])

    async def test_a_limit_that_stopped_the_tool_is_logged_at_info(self) -> None:
        for flags, kind in (
            ({"timed_out": True}, "timeout"),
            ({"output_limited": True}, "output limit"),
            ({"resource_exceeded": "The memory limit was exceeded"}, "resource limit"),
        ):
            with self.subTest(kind=kind):
                runner = RecordingRunner(finished(137, **flags))
                with self.assertLogs("latexprep.runtime", level="DEBUG") as logs:
                    await runner.run(["pdflatex"], self.work, self.work)
                stopped = [record for record in logs.records if record.levelno == logging.INFO]
                self.assertEqual(len(stopped), 1)
                self.assertIn(f"tool stopped by its {kind}: pdflatex", stopped[0].getMessage())
                # The resource message may name places; the log line carries only the kind.
                self.assertNotIn("memory limit was", stopped[0].getMessage())

    async def test_an_error_before_the_tool_finished_is_logged_and_raised(self) -> None:
        class Failing(ToolRunner):
            async def _run_command(self, argv, cwd, workspace, readonly_inputs, memory_mb):
                raise RuntimeError("boom with /home/ann/secret")

        with self.assertLogs("latexprep.runtime", level="DEBUG") as logs:
            with self.assertRaises(RuntimeError):
                await Failing().run(["pdfinfo"], self.work, self.work)
        self.assertIn("tool did not finish: pdfinfo (RuntimeError after", logs.output[-1])
        self.assertNotIn("secret", "\n".join(logs.output))


class OnlineClientLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_requests_log_the_host_and_outcome_but_never_the_url_or_contact(self) -> None:
        from latexprep.online_http_session import OnlineClient

        transport = FakeTransport([Response(200), Response(404)])
        selected = options(online_contact_email="ann@example.org")
        client = OnlineClient(selected, None, resolver=public_resolver, transport=transport)
        plain_url = "https://example.org/path/with/segments"
        secret_url = "https://example.org/path/with?token=SECRETVALUE"
        with self.assertLogs("latexprep.online_http_session", level="DEBUG") as logs:
            await client.fetch(plain_url)
            await client.fetch(plain_url)  # answered from the cache
            await client.fetch(secret_url)  # refused before any request
            await client.fetch("https://api.crossref.org/works/10.1/x", want_body=True)
        text = "\n".join(logs.output)
        self.assertIn("online HEAD example.org: HTTP 200", text)
        self.assertIn("answered from the cache", text)
        self.assertIn("online GET api.crossref.org: HTTP 404", text)
        self.assertIn("fetch failed as unsafe_target", text)
        for private in ("SECRETVALUE", "/path/with", "segments", "ann@example.org"):
            self.assertNotIn(private, text)

    async def test_a_throttling_provider_is_logged_at_info(self) -> None:
        from latexprep.online_http_session import OnlineClient

        client = OnlineClient(
            options(), None, resolver=public_resolver, transport=FakeTransport(Response(429))
        )
        with self.assertLogs("latexprep.online_http_session", level="DEBUG") as logs:
            await client.fetch("https://example.org/")
        self.assertTrue(
            any(
                record.levelno == logging.INFO and "throttled" in record.getMessage()
                for record in logs.records
            )
        )


class PdfMemoLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_a_remembered_measurement_is_logged_as_reused(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            pdf = root / "paper.pdf"
            pdf.write_bytes(b"%PDF-fixture")
            session = await PdfToolSession.open(
                root / "work", {"document": pdf}, FontsRunner(), None
            )
            with self.assertLogs("latexprep.pdf_tools", level="DEBUG") as logs:
                for name in ("fonts", "fonts-again"):
                    await session.measure(
                        name, ["pdffonts", DOCUMENT], lambda result: result.stdout
                    )
        first, second = logs.output
        self.assertIn("PDF measurement fonts: not remembered, running pdffonts", first)
        self.assertIn("PDF measurement fonts-again: reused the remembered pdffonts output", second)
        self.assertNotIn(temporary, "\n".join(logs.output))


class SchedulerLoggingTests(unittest.IsolatedAsyncioTestCase):
    async def test_start_end_and_the_reason_a_task_is_blocked_are_logged(self) -> None:
        async def fine() -> str:
            return "ok"

        async def broken() -> str:
            raise RuntimeError("no")

        tasks = [
            Task("build", broken),
            Task("compare", fine, needs_success_of=("build",)),
            Task("lint", fine),
        ]
        with self.assertLogs("latexprep.scheduler", level="DEBUG") as logs:
            await Scheduler(ResourceBudget(2, 1024)).execute(tasks)
        text = "\n".join(logs.output)
        self.assertIn("task lint: start", text)
        self.assertIn("task lint: succeeded in", text)
        self.assertIn("task build: raised RuntimeError", text)
        self.assertIn("task build: failed in", text)
        self.assertIn("task compare: blocked by failed build", text)

    async def test_a_defect_logs_its_traceback_but_an_expected_failure_does_not(self) -> None:
        async def defect() -> None:
            raise RuntimeError("defect inside the task")

        async def expected() -> None:
            raise PreparationError("the input is wrong")

        with self.assertLogs("latexprep.scheduler", level="DEBUG") as logs:
            await Scheduler(ResourceBudget(2, 1024)).execute(
                [Task("defective", defect), Task("expected", expected)]
            )
        by_task = {
            record.getMessage().split(":")[0]: record
            for record in logs.records
            if "raised" in record.getMessage()
        }
        defective = by_task["task defective"].exc_info
        self.assertIsNotNone(defective)
        self.assertIs(defective[0], RuntimeError)
        self.assertIsNone(by_task["task expected"].exc_info)


class ConfigDiscoveryLoggingTests(unittest.TestCase):
    def discover(self, files: dict[str, str], **keywords: object):
        with tempfile.TemporaryDirectory() as temporary:
            project = write_project(Path(temporary) / "project", files)
            (project / ".git").mkdir()
            with self.assertLogs("latexprep.config_discovery", level="DEBUG") as logs:
                resolve_config_path(project, **keywords)
        return logs

    def test_the_chosen_file_is_named_without_its_directory(self) -> None:
        logs = self.discover({"fledge.toml": "", "main.tex": ""})
        self.assertEqual(
            [record.getMessage() for record in logs.records],
            ["settings: discovered fledge.toml beside the input"],
        )

    def test_a_legacy_name_is_reported_at_info(self) -> None:
        logs = self.discover({"latex-prep.toml": "", "main.tex": ""})
        legacy = [record for record in logs.records if record.levelno == logging.INFO]
        self.assertEqual(len(legacy), 1)
        self.assertIn("latex-prep.toml is a legacy name", legacy[0].getMessage())

    def test_a_legacy_pyproject_table_is_reported_at_info(self) -> None:
        logs = self.discover({"pyproject.toml": "[tool.latex-prep]\nengine = 'xelatex'\n"})
        self.assertTrue(
            any(
                record.levelno == logging.INFO and "[tool.latex-prep]" in record.getMessage()
                for record in logs.records
            )
        )

    def test_isolated_and_missing_files_are_logged(self) -> None:
        self.assertIn("--isolated", self.discover({"main.tex": ""}, isolated=True).output[0])
        self.assertIn("no settings file found", self.discover({"main.tex": ""}).output[0])


class SourceAnalysisLoggingTests(unittest.TestCase):
    def test_cache_misses_hits_and_builds_are_logged(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = write_project(Path(temporary) / "project", {"main.tex": "\\input{part}\n"})
            write_project(root, {"part.tex": "text\n"})
            cache = AnalysedSourceCache()
            with self.assertLogs("latexprep.analysed_source", level="DEBUG") as logs:
                analysis = cache.get(root, "main.tex")
                cache.get(root, "main.tex")
                self.assertEqual(len(analysis.graph.sources), 2)
                self.assertTrue(analysis.expanded.text)
        text = "\n".join(logs.output)
        self.assertIn("cache miss for main main.tex", text)
        self.assertIn("cache hit for main main.tex", text)
        self.assertIn("source graph built: 2 files", text)
        self.assertIn("expanded document built:", text)
        self.assertNotIn(temporary, text)


class CommandLineLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.project = write_project(
            Path(self.temporary.name) / "project",
            {"main.tex": "\\documentclass{article}\\begin{document}Hi\\end{document}\n"},
        )

    def invoke(self, *arguments: str, environment: dict[str, str] | None = None):
        return CliRunner().invoke(
            cli,
            ["inspect", str(self.project), "--main", "main.tex", "--json", "--quiet", *arguments],
            env=environment,
        )

    def test_debug_prints_log_lines_to_stderr_and_leaves_stdout_parseable(self) -> None:
        result = self.invoke("--debug")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(json.loads(result.stdout)["command"], "inspect")
        self.assertIn("scheduler: task", result.stderr)
        self.assertIn("config_discovery: settings: no settings file found", result.stderr)
        self.assertNotIn("scheduler:", result.stdout)

    def test_without_debug_nothing_is_logged(self) -> None:
        result = self.invoke()
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(result.stderr, "")
        json.loads(result.stdout)

    def test_the_environment_variable_asks_for_the_same_lines(self) -> None:
        result = self.invoke(environment={"FLEDGE_DEBUG": "1"})
        self.assertIn("scheduler: task", result.stderr)
        json.loads(result.stdout)

    def test_a_later_command_without_debug_prints_nothing(self) -> None:
        self.assertIn("scheduler:", self.invoke("--debug").stderr)
        self.assertEqual(self.invoke().stderr, "")

    def test_the_debug_flag_before_the_command_also_starts_the_lines(self) -> None:
        result = CliRunner().invoke(
            cli,
            ["--debug", "inspect", str(self.project), "--main", "main.tex", "--json", "--quiet"],
        )
        self.assertIn("scheduler: task", result.stderr)
        json.loads(result.stdout)

    def test_the_project_location_never_appears_in_the_log(self) -> None:
        result = self.invoke("--debug")
        self.assertNotIn(self.temporary.name, result.stderr)
        self.assertNotIn(str(Path(self.temporary.name).resolve()), result.stderr)

    def test_a_tool_command_shows_its_start_and_end_lines_on_stderr(self) -> None:
        runner = RecordingRunner(finished(0))

        async def fake_doctor(settings, *, progress):
            await runner.run(
                ["pdflatex", "-version", str(self.project / "main.tex")], self.project, self.project
            )
            return Report("doctor")

        with patch("latexprep.doctor_command.run_doctor", fake_doctor):
            logged = CliRunner().invoke(cli, ["doctor", "--json", "--quiet", "--debug"])
            silent = CliRunner().invoke(cli, ["doctor", "--json", "--quiet"])
        self.assertIn("runtime: tool start: pdflatex -version +1 more argument", logged.stderr)
        self.assertIn("runtime: tool end: pdflatex -version +1 more argument", logged.stderr)
        self.assertNotIn(self.temporary.name, logged.stderr)
        self.assertEqual(json.loads(logged.stdout)["command"], "doctor")
        self.assertEqual(silent.stderr, "")


if __name__ == "__main__":
    unittest.main()
