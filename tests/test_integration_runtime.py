"""Opt-in live runtime checks: time limits, interruption, ``fmt``, ``doctor`` and the CI recipe.

Every test drives the real tools through a public entry point: ``run_job``, the Click command
in process, or ``python -m latexprep`` as a separate process. A document that loops forever
stands in for a runaway build. Its main file has a unique name, so ``pgrep -f`` finds any TeX
process still working on it after the job has ended.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import secrets
import selectors
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Collection, Iterable
from pathlib import Path
from typing import IO, Any, NamedTuple

from click.testing import CliRunner, Result

from latexprep.cli import cli
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.job_context import JobRequest
from latexprep.report import Report
from tests import SOURCE_ROOT
from tests.support import directory_contents, live_test, write_project

EPOCH = 1_700_000_000
REPOSITORY = SOURCE_ROOT.parent
# \loop\iftrue\repeat calls itself in tail position: TeX grows no stack and writes no output,
# so only a time limit or a signal ends the build.
ENDLESS_DOCUMENT = (
    "\\documentclass{article}\n\\begin{document}\n\\loop\\iftrue\\repeat\n\\end{document}\n"
)
# Inconsistent indentation, a line far beyond the wrap length and trailing spaces.
BADLY_FORMATTED = (
    "\\documentclass{article}   \n"
    "\\begin{document}\n"
    "      \\begin{itemize}\n"
    "  \\item First point.   \n"
    "\\item " + " ".join(["A very long line of words"] * 12) + ".\n"
    "            \\end{itemize}\n"
    "\\end{document}\n"
)
WELL_FORMATTED = (
    "\\documentclass{article}\n\\begin{document}\n"
    "\\begin{itemize}\n  \\item First point.\n\\end{itemize}\n\\end{document}\n"
)
TEX_TOOLS = ("latexmk", "pdflatex")
POPPLER_TOOLS = ("pdfinfo", "pdftotext", "pdftoppm", "pdffonts", "pdfdetach", "pdfimages")
# The stage name of the baseline build, as reports and review decisions in settings files
# spell it, and the progress line on stderr when that build starts. Literals on purpose: a
# rename must fail these tests rather than follow silently.
BASELINE_STAGE = "baseline"
BASELINE_ANNOUNCEMENT = "Starting baseline build"
CANCELLED_MESSAGE = "Cancelled; no verified output was published"
# Rich draws a transient progress display instead of plain stage lines when either is set.
FORCED_TERMINAL_VARIABLES = frozenset({"FORCE_COLOR", "TTY_COMPATIBLE"})
# Generous waits: other builds may share the machine, and no test asserts how long a step took.
ORPHAN_WAIT_SECONDS = 30
STARTUP_WAIT_SECONDS = 60
INTERRUPT_WAIT_SECONDS = 120
COMMAND_TIMEOUT_SECONDS = 600
POLL_SECONDS = 0.2
# GitHub annotations that name a file and a line, as ci_annotations renders them.
ANNOTATION = re.compile(
    r"::(?P<level>\w+) title=(?P<title>[^,:]+),file=(?P<file>[^,:]+),line=(?P<line>\d+)::"
)
# The TODO marker the CI recipe test adds to sections/results.tex: once in a comment, which
# TEX001 ignores, and once in active text on the next line, which it reports.
TODO_FILE = "sections/results.tex"
COMMENTED_TODO_LINE = 3
ACTIVE_TODO_LINE = 4


def _unique_main(topic: str) -> str:
    """Return a main file name that no other process on the machine uses.

    The name is ``topic`` with a random suffix, such as ``deadline_1a2b3c4d.tex``.
    """
    return f"{topic}_{secrets.token_hex(4)}.tex"


def _cli(*arguments: str) -> list[str]:
    """Return the command line of Fledge from this checkout, run as a separate process."""
    return [sys.executable, "-m", "latexprep", *arguments]


def _environment(**overrides: str) -> dict[str, str]:
    """Return the environment of such a process: this checkout's sources, plain progress lines.

    The variables that force terminal output are left out of the inherited environment.

    Args:
        **overrides: Variables that replace inherited ones.

    Returns:
        The environment variables of the process.
    """
    inherited = {
        name: value for name, value in os.environ.items() if name not in FORCED_TERMINAL_VARIABLES
    }
    return {**inherited, "PYTHONPATH": str(SOURCE_ROOT), **overrides}


def _matching_processes(pattern: str, exclude: Collection[int] = ()) -> list[int]:
    """List the processes whose command line matches ``pattern``, by ``pgrep -f``.

    Args:
        pattern: The pattern given to ``pgrep -f``.
        exclude: Process IDs to leave out.

    Returns:
        The IDs of the matching processes.

    Raises:
        RuntimeError: ``pgrep`` failed.
    """
    found = subprocess.run(
        ["pgrep", "-f", pattern], capture_output=True, text=True, check=False, timeout=60
    )
    if found.returncode not in (0, 1):  # 1: no process matched
        raise RuntimeError(f"pgrep failed with status {found.returncode}: {found.stderr.strip()}")
    return [pid for pid in map(int, found.stdout.split()) if pid not in exclude]


def _survivors(pattern: str) -> list[int]:
    """List the processes that still match ``pattern`` after waiting for them to end.

    The wait lasts at most ``ORPHAN_WAIT_SECONDS``.
    """
    deadline = time.monotonic() + ORPHAN_WAIT_SECONDS
    while (found := _matching_processes(pattern)) and time.monotonic() < deadline:
        time.sleep(POLL_SECONDS)
    return found


def _process_appears(pattern: str, exclude: Collection[int]) -> bool:
    """Return whether a process other than ``exclude`` comes to match ``pattern``.

    Args:
        pattern: The pattern given to ``pgrep -f``.
        exclude: Process IDs to leave out.

    Returns:
        Whether such a process appeared within ``STARTUP_WAIT_SECONDS``.
    """
    deadline = time.monotonic() + STARTUP_WAIT_SECONDS
    while not _matching_processes(pattern, exclude):
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)
    return True


def _kill_matching(pattern: str) -> None:
    """Kill whatever a failed test left running on its uniquely named document, as cleanup."""
    for pid in _matching_processes(pattern):
        with contextlib.suppress(ProcessLookupError):
            os.kill(pid, signal.SIGKILL)


def _stop(process: subprocess.Popen[bytes]) -> None:
    """End the command if a failed test left it running, and close its pipes, as cleanup."""
    if process.poll() is None:
        process.kill()
        process.wait(timeout=STARTUP_WAIT_SECONDS)
    for stream in (process.stdout, process.stderr):
        if stream is not None:
            stream.close()


class _ProcessWatch:
    """A context manager that polls ``pgrep -f`` in a thread and records any matching process.

    An error of ``pgrep`` ends the polling and is raised again when the watch ends, so a broken
    process search fails the test instead of reading as "no process seen".

    Attributes:
        pattern: The pattern given to ``pgrep -f``.
        seen: Whether any process matched the pattern while the watch ran.
    """

    def __init__(self, pattern: str) -> None:
        self.pattern = pattern
        self.seen = False
        self._error: Exception | None = None
        self._stopped = threading.Event()
        self._thread = threading.Thread(target=self._poll, daemon=True)

    def __enter__(self) -> _ProcessWatch:
        self._thread.start()
        return self

    def __exit__(self, *_exception: object) -> None:
        self._stopped.set()
        self._thread.join()
        if self._error is not None:
            raise self._error

    def _poll(self) -> None:
        """Poll until the watch ends or ``pgrep`` fails, keeping the error for ``__exit__``."""
        try:
            while not self._stopped.is_set():
                self.seen = self.seen or bool(_matching_processes(self.pattern))
                self._stopped.wait(POLL_SECONDS)
        except Exception as error:  # raised again in the test's thread by __exit__
            self._error = error


def _read_until_line(stream: IO[bytes], expected: str) -> tuple[bool, bytes]:
    """Read ``stream`` until a line equal to ``expected`` arrives, it ends, or the wait runs out.

    The descriptor is read directly, as ``Popen.communicate`` does, so ``communicate``
    collects the rest afterwards. The wait lasts at most ``STARTUP_WAIT_SECONDS``.

    Args:
        stream: The output pipe of a process.
        expected: The line to wait for, compared without surrounding whitespace.

    Returns:
        Whether the line arrived, and every byte read.
    """
    received = b""
    deadline = time.monotonic() + STARTUP_WAIT_SECONDS
    with selectors.DefaultSelector() as selector:
        selector.register(stream, selectors.EVENT_READ)
        while (remaining := deadline - time.monotonic()) > 0 and selector.select(remaining):
            chunk = os.read(stream.fileno(), 4096)
            if not chunk:
                break
            received += chunk
            lines = received.decode(errors="replace").splitlines()
            if expected in (line.strip() for line in lines):
                return True, received
    return False, received


def _changed_lines(text: str, sign: str) -> list[str]:
    """List the removed (``-``) or added (``+``) lines of a unified diff, without file headers.

    Args:
        text: The unified diff.
        sign: ``-`` for removed lines or ``+`` for added ones.

    Returns:
        The matching lines in diff order, each with its sign.
    """
    return [
        line
        for line in text.splitlines()
        if line.startswith(sign) and not line.startswith(sign * 3)
    ]


def _insert_lines(path: Path, *, after: int, lines: Iterable[str]) -> None:
    """Insert ``lines`` into the text file at ``path``.

    Args:
        path: The file to edit.
        after: The number of existing lines that stay before the inserted ones.
        lines: The lines to insert, without line ends.
    """
    text = path.read_text(encoding="utf-8").splitlines(keepends=True)
    text[after:after] = [f"{line}\n" for line in lines]
    path.write_text("".join(text), encoding="utf-8")


def _annotations(stdout: str) -> set[tuple[str, str, str, int]]:
    """Collect the level, title, file and line of each GitHub annotation with a file and line."""
    return {
        (match["level"], match["title"], match["file"], int(match["line"]))
        for match in map(ANNOTATION.match, stdout.splitlines())
        if match
    }


def _error_annotations(stdout: str) -> list[str]:
    """List the lines of ``stdout`` that are GitHub error annotations."""
    return [line for line in stdout.splitlines() if line.startswith("::error")]


def _unresolved(report: dict[str, Any]) -> list[tuple[str, str, str]]:
    """List the findings of a JSON report that did not pass, for assertion messages.

    Returns:
        The rule, status and message of each such finding.
    """
    return [
        (finding["rule"], finding["status"], finding["message"])
        for finding in report["findings"]
        if finding["status"] != "passed"
    ]


def _path_without(tool: str) -> str:
    """Return ``PATH`` without each directory that provides ``tool``; other entries are kept."""
    return os.pathsep.join(
        entry
        for entry in os.environ.get("PATH", "").split(os.pathsep)
        if shutil.which(tool, path=entry) is None
    )


def _run_command(
    *arguments: str, cwd: Path | None = None, **environment: str
) -> subprocess.CompletedProcess[str]:
    """Run Fledge as a separate process without input, with ``environment`` added to its own.

    Args:
        *arguments: The command-line arguments.
        cwd: The working directory, or ``None`` for the current one.
        **environment: Environment variables to set for the process.

    Returns:
        The completed process with its text output, also when the command failed.
    """
    return subprocess.run(
        _cli(*arguments),
        cwd=cwd,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        check=False,
        env=_environment(**environment),
        timeout=COMMAND_TIMEOUT_SECONDS,
    )


class _EndlessRun(NamedTuple):
    """What a ``prepare`` job of the endless document left behind.

    Attributes:
        report: The report of the job.
        main: The unique name of the main file.
        output: The output directory given to the job.
        tex_seen: Whether a process working on the document was seen while the job ran.
    """

    report: Report
    main: str
    output: Path
    tex_seen: bool


@live_test("Set FLEDGE_RUN_INTEGRATION=1 for live runtime tests with TeX, Poppler and tex-fmt")
class RuntimeIntegrationTests(unittest.TestCase):
    """Time limits, interruption, ``fmt``, ``doctor`` and the CI recipe with the real tools."""

    def _directory(self, topic: str) -> Path:
        """Create a temporary directory named after ``topic``, removed after the test."""
        temporary = tempfile.TemporaryDirectory(prefix=f"fledge-{topic}-")
        self.addCleanup(temporary.cleanup)
        return Path(temporary.name).resolve()

    def _require_tools(self, *names: str) -> None:
        """Skip the test unless every one of ``names`` is on ``PATH``."""
        if missing := [name for name in names if shutil.which(name) is None]:
            self.skipTest(f"Not on PATH: {', '.join(missing)} (see docs/installation.md)")

    @staticmethod
    def _invoke(*arguments: str) -> Result:
        """Run the command line ``arguments`` in this process and return the Click result."""
        return CliRunner().invoke(cli, list(arguments))

    def _json_report(self, process: subprocess.CompletedProcess[str]) -> dict[str, Any]:
        """Parse the JSON report that ``process`` printed, failing the test if there is none."""
        try:
            return json.loads(process.stdout)
        except json.JSONDecodeError:
            self.fail(f"No JSON report (exit {process.returncode}): {process.stderr[-2000:]}")

    # Time limits on a real build.

    def _prepare_endless_document(
        self, topic: str, *, tool_timeout: int, job_timeout: int
    ) -> _EndlessRun:
        """Prepare the endless document under the given limits, then wait for TeX to be gone.

        Skips unless TeX and ``pgrep`` are installed, and asserts that no TeX process outlives
        the job.

        Args:
            topic: The topic that names the directory and the main file.
            tool_timeout: The limit of one tool command, ``timeout_seconds``.
            job_timeout: The limit of the whole job, ``job_timeout_seconds``.

        Returns:
            What the job left behind.
        """
        self._require_tools(*TEX_TOOLS, "pgrep")
        base = self._directory(topic)
        main = _unique_main(topic)
        pattern = main.removesuffix(".tex")
        self.addCleanup(_kill_matching, pattern)
        source = write_project(base / "paper", {main: ENDLESS_DOCUMENT})
        output = base / "submission"
        settings = Settings(
            main=main,
            timeout_seconds=tool_timeout,
            job_timeout_seconds=job_timeout,
            source_date_epoch=EPOCH,
        )
        with _ProcessWatch(pattern) as watch:
            report = asyncio.run(run_job(JobRequest("prepare", source, settings, output)))
        self.assertEqual(_survivors(pattern), [], "A TeX process outlived the job")
        return _EndlessRun(report, main, output, watch.seen)

    def test_job_deadline_stops_the_running_build_and_publishes_nothing(self) -> None:
        """Check that the job deadline stops the running build and publishes nothing.

        A missed ``job_timeout_seconds`` deadline blocks the run with ``execution.deadline``.

        Verifies docs/configuration.md (the deadline bounds the whole job, so the build is
        cancelled rather than timed out), docs/workflow-reference.md (cancellation drains
        running tools: no TeX process survives) and docs/reports.md (a blocked run writes no
        bundle).
        """
        run = self._prepare_endless_document("deadline", tool_timeout=600, job_timeout=5)
        findings = run.report.findings
        self.assertIn("execution.deadline", {finding.rule for finding in findings})
        self.assertNotIn("build.timeout", {finding.rule for finding in findings})
        self.assertEqual(run.report.outcome, "blocked")
        stages = {stage["name"]: stage["status"] for stage in run.report.stages}
        self.assertEqual(stages.get(BASELINE_STAGE), "cancelled", stages)
        self.assertFalse(run.output.exists())
        if not run.tex_seen:
            self.skipTest("The deadline passed before TeX started, so no TeX process was stopped")

    def test_tool_timeout_stops_the_build_and_records_a_build_timeout(self) -> None:
        """Check that the tool timeout stops the build and records a build timeout.

        A build longer than ``timeout_seconds`` is stopped and reported as ``build.timeout``.

        Verifies docs/configuration.md (the limit bounds one tool command, not the job: no
        ``execution.deadline``), docs/reports.md (blocked, no bundle) and that the stopped
        build leaves no TeX process behind.
        """
        run = self._prepare_endless_document("tooltimeout", tool_timeout=5, job_timeout=600)
        self.assertTrue(run.tex_seen, "No TeX process was seen working on the document")
        findings = run.report.findings
        self.assertNotIn("execution.deadline", {finding.rule for finding in findings})
        [timeout] = [finding for finding in findings if finding.rule == "build.timeout"]
        self.assertEqual(
            (timeout.stage, timeout.path, timeout.severity, timeout.status),
            (BASELINE_STAGE, run.main, "error", "failed"),
        )
        self.assertEqual(run.report.outcome, "blocked")
        self.assertFalse(run.output.exists())

    # fmt with the real formatter.

    def test_fmt_diff_shows_the_formatter_patch_without_editing_the_project(self) -> None:
        """Check that ``fmt --diff`` shows the formatter's patch without editing the project.

        ``fmt --diff`` reports FMT001 for main.tex and shows tex-fmt's patch, read-only.

        Verifies the ``fmt`` and ``--diff`` help (read-only check; the patch appears in the
        terminal report), docs/checks.md FMT001 (a warning, exit 1) and
        docs/workflow-reference.md (compact output summarizes the change instead of printing
        the patch).
        """
        self._require_tools("tex-fmt")
        project = write_project(self._directory("fmt") / "messy", {"main.tex": BADLY_FORMATTED})
        before = directory_contents(project)
        options = ("--diff", "--quiet", "--isolated")
        terminal = self._invoke("fmt", str(project), *options)
        compact = self._invoke("fmt", str(project), *options, "--output-format", "compact")
        self.assertEqual(terminal.exit_code, 1, terminal.output)
        self.assertEqual(compact.exit_code, 1, compact.output)
        lines = terminal.stdout.splitlines()
        self.assertIn("--- main.tex", lines)
        self.assertIn("+++ main.tex", lines)
        self.assertTrue(_changed_lines(terminal.stdout, "-"), terminal.stdout)
        self.assertTrue(_changed_lines(terminal.stdout, "+"), terminal.stdout)
        records = compact.stdout.splitlines()
        [record] = [line for line in records if line.startswith("FMT001 ")]
        self.assertEqual(record.split(" | ")[0].split(), ["FMT001", "warning/failed", "main.tex"])
        [summary] = [line for line in records if line.startswith("changes: ")]
        self.assertTrue(summary.startswith("changes: 1 "), summary)
        self.assertNotIn("--- main.tex", records)
        self.assertEqual(directory_contents(project), before)

    def test_fmt_passes_a_project_that_already_matches_the_formatter(self) -> None:
        """Check that a source tex-fmt leaves unchanged passes ``fmt``.

        FMT001 passes and the exit code is 0.
        """
        self._require_tools("tex-fmt")
        project = write_project(self._directory("fmt") / "clean", {"main.tex": WELL_FORMATTED})
        before = directory_contents(project)
        options = ("--diff", "--quiet", "--isolated", "--show-passed", "--output-format", "compact")
        result = self._invoke("fmt", str(project), *options)
        self.assertEqual(result.exit_code, 0, result.output)
        [record] = [line for line in result.stdout.splitlines() if line.startswith("FMT001 ")]
        self.assertEqual(record.split()[1], "info/passed", record)
        self.assertEqual(directory_contents(project), before)

    # doctor with the real tools.

    def test_doctor_verifies_and_exercises_the_core_tools(self) -> None:
        """Check that ``doctor`` verifies and exercises the core tools.

        ``doctor --json`` passes on a complete setup and records each core tool's version.

        Verifies docs/doctor.md: the outcome is passed or passed with advisories, with exit
        code 0 or 1 to match, and the JSON report records latexmk, pdflatex and pdfinfo as
        found on PATH, version verified under isolation and exercised by the built-in fixture.
        """
        self._require_tools(*TEX_TOOLS, *POPPLER_TOOLS)
        result = self._invoke("doctor", "--json", "--quiet")
        report = json.loads(result.stdout)
        exit_codes = {"passed": 0, "passed_with_advisories": 1}
        self.assertIn(report["outcome"], exit_codes, _unresolved(report))
        self.assertEqual(result.exit_code, exit_codes[report["outcome"]])
        for name in ("latexmk", "pdflatex", "pdfinfo"):
            with self.subTest(tool=name):
                tool = report["tools"][name]
                self.assertTrue(tool.get("version") and tool["version"][0].strip(), tool)
                self.assertTrue(tool["installed"], tool)
                self.assertTrue(tool["version_verified"], tool)
                self.assertTrue(tool["exercised"], tool)

    def _path_without_poppler(self) -> str:
        """Return ``PATH`` without Poppler; skip where TeX shares its directory, as on Ubuntu."""
        self._require_tools(*TEX_TOOLS, "pdfinfo")
        path = _path_without("pdfinfo")
        if hidden := [name for name in TEX_TOOLS if shutil.which(name, path=path) is None]:
            self.skipTest(
                f"{', '.join(hidden)} shares a directory with pdfinfo, so Poppler cannot be "
                "removed from PATH without removing TeX"
            )
        return path

    def test_doctor_blocks_when_poppler_is_missing(self) -> None:
        """Check that a missing required tool blocks the diagnostic result with exit code 3.

        Verifies docs/doctor.md: Poppler's ``pdfinfo`` is a core tool, a missing required tool
        blocks, and the report records the tool as not installed.
        """
        process = _run_command("doctor", "--json", "--quiet", PATH=self._path_without_poppler())
        report = self._json_report(process)
        self.assertEqual(process.returncode, 3, _unresolved(report))
        self.assertEqual(report["outcome"], "blocked")
        self.assertFalse(report["tools"]["pdfinfo"]["installed"])
        self.assertFalse(report["tools"]["pdfinfo"]["exercised"])
        # The finding names the missing tool and the package that provides it (Poppler),
        # as the pdf.inspection_unavailable finding of check and prepare does.
        missing = {
            finding.get("details", {}).get("tool"): finding
            for finding in report["findings"]
            if finding["rule"] == "doctor.tool_missing" and finding["severity"] == "error"
        }
        self.assertIn("pdfinfo", missing)
        self.assertIn("Poppler", missing["pdfinfo"]["next_step"], missing["pdfinfo"])

    # Interrupting a live build.

    def _await_running_build(self, process: subprocess.Popen[bytes], pattern: str) -> bytes:
        """Wait until stderr announces the baseline build and a TeX process works on it.

        The command itself is excluded from the search: its own command line names the main
        file.

        Args:
            process: The running command, whose standard error is a pipe.
            pattern: The pattern that matches a process working on the document.

        Returns:
            The standard error read so far.
        """
        assert process.stderr is not None
        announced, received = _read_until_line(process.stderr, BASELINE_ANNOUNCEMENT)
        self.assertTrue(announced, f"stderr never announced the baseline build: {received!r}")
        self.assertTrue(
            _process_appears(pattern, exclude={process.pid}),
            "No TeX process started for the document",
        )
        return received

    def test_interrupt_during_the_build_exits_130_without_output_or_tex_process(self) -> None:
        """Check that SIGINT during the baseline build cancels ``prepare`` with exit code 130.

        Verifies docs/reports.md (130 means interrupted; no bundle is written), the
        cancellation message of the final report, and docs/workflow-reference.md
        (cancellation drains running tools, so no TeX process survives). The message is
        asserted in the final report on stdout.
        """
        self._require_tools(*TEX_TOOLS, "pgrep")
        base = self._directory("interrupt")
        main = _unique_main("interrupt")
        pattern = main.removesuffix(".tex")
        source = write_project(base / "paper", {main: ENDLESS_DOCUMENT})
        output = base / "submission"
        self.addCleanup(_kill_matching, pattern)
        process = subprocess.Popen(
            _cli(
                *("prepare", str(source), "--main", main, "--output", str(output)),
                *("--job-timeout-seconds", "600", "--timeout-seconds", "600"),
            ),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=_environment(),
        )
        self.addCleanup(_stop, process)
        progress = self._await_running_build(process, pattern)
        process.send_signal(signal.SIGINT)
        stdout, rest = process.communicate(timeout=INTERRUPT_WAIT_SECONDS)
        diagnostics = (stdout + progress + rest).decode(errors="replace")
        self.assertEqual(process.returncode, 130, diagnostics)
        self.assertIn(CANCELLED_MESSAGE, stdout.decode(), diagnostics)
        self.assertFalse(output.exists())
        self.assertEqual(_survivors(pattern), [], "A TeX process outlived the interrupted job")

    # The documented CI recipe.

    def _assert_check_step(self, project: Path, reports: Path) -> None:
        """Run the recipe's ``check`` step in ``project`` and assert its annotations and reports.

        Args:
            project: The project directory, the working directory of the step.
            reports: The directory for the JSON and HTML reports.
        """
        check = _run_command(
            *("check", ".", "--main", "main.tex", "--output-format", "ci", "--quiet"),
            *("--json-report", str(reports / "check.json")),
            *("--html-report", str(reports / "check.html")),
            cwd=project,
        )
        self.assertLessEqual(check.returncode, 2, check.stdout + check.stderr)
        annotations = _annotations(check.stdout)
        self.assertIn(("warning", "TEX001", TODO_FILE, ACTIVE_TODO_LINE), annotations, check.stdout)
        annotated = {line for _, _, file, line in annotations if file == TODO_FILE}
        self.assertNotIn(COMMENTED_TODO_LINE, annotated, check.stdout)
        self.assertEqual(_error_annotations(check.stdout), [])
        saved = json.loads((reports / "check.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["outcome"], "passed_with_advisories", _unresolved(saved))
        self.assertGreater((reports / "check.html").stat().st_size, 0)

    def _assert_prepare_step(self, project: Path, reports: Path) -> None:
        """Run the recipe's ``prepare`` step in ``project`` and assert what it publishes.

        Args:
            project: The project directory, the working directory of the step.
            reports: The directory for the submission and the HTML report.
        """
        submission = reports / "submission"
        prepare = _run_command(
            *("prepare", ".", "--main", "main.tex", "--output", str(submission)),
            *("--output-format", "ci", "--quiet", "--html-report", str(reports / "prepare.html")),
            cwd=project,
        )
        self.assertLessEqual(prepare.returncode, 2, prepare.stdout + prepare.stderr)
        annotation = ("warning", "TEX001", TODO_FILE, ACTIVE_TODO_LINE)
        self.assertIn(annotation, _annotations(prepare.stdout), prepare.stdout)
        self.assertEqual(_error_annotations(prepare.stdout), [])
        self.assertTrue((submission / "sources").is_dir())
        for name in ("submission.zip", "manuscript.pdf", "report.json"):
            with self.subTest(artifact=name):
                self.assertTrue((submission / name).is_file())
        self.assertGreater((reports / "prepare.html").stat().st_size, 0)

    def test_documented_ci_recipe_annotates_advisories_and_publishes_the_submission(self) -> None:
        """Check the workflow of docs/continuous-integration.md, run from the project directory.

        ``check`` and ``prepare`` with ``--output-format ci --quiet`` pass the recipe's exit
        code gate (at most 2) for an advisory, print it as a GitHub warning annotation with
        file and line, and write the reports and the submission outside the project, which
        stays unchanged. TEX001 ignores comments (docs/checks.md), so only the TODO in active
        text is annotated.
        """
        self._require_tools(*TEX_TOOLS, "bibtex", *POPPLER_TOOLS)
        base = self._directory("ci")
        project = base / "paper"
        shutil.copytree(REPOSITORY / "examples" / "nested-paper", project)
        _insert_lines(
            project / TODO_FILE,
            after=COMMENTED_TODO_LINE - 1,
            lines=("% TODO: tighten this", "TODO: tighten this paragraph."),
        )
        before = directory_contents(project)
        reports = base / "fledge"
        reports.mkdir()
        self._assert_check_step(project, reports)
        self._assert_prepare_step(project, reports)
        self.assertEqual(directory_contents(project), before)


if __name__ == "__main__":
    unittest.main()
