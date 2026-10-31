"""The command line as a separate process: exit codes, output streams and the example page.

Most command-line tests call ``latexprep.cli.main`` inside the test process. These start
``python -m latexprep`` as a child process, as a shell, a script or a CI job does, and
observe only what such a caller observes: the exit status, standard output and standard
error. No test here needs TeX, Poppler or a network: every report run passes ``--offline``,
and ``--isolated`` or ``--config`` decides which settings apply.
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping, Sequence
from contextlib import redirect_stdout
from itertools import pairwise
from pathlib import Path
from types import ModuleType

from tests import SOURCE_ROOT
from tests.support import ProjectTestCase, directory_contents, write_project

REPOSITORY = SOURCE_ROOT.parent
EXAMPLE_PAGE = REPOSITORY / "docs" / "examples.md"
RELEASE_CHECKS = REPOSITORY / "scripts" / "release_checks.py"
FLEDGE = (sys.executable, "-m", "latexprep")
# Generous for a slow runner that starts Python; a command that hangs fails instead of waiting.
TIMEOUT_SECONDS = 120
# The report as JSON on stdout, no progress on stderr and no network access.
JSON_REPORT = ("--json", "--quiet", "--offline")
# Variables that change what the child writes to its streams: forced colour or terminal
# behaviour, NO_COLOR (which a test sets itself) and the diagnostic log on stderr.
STREAM_VARIABLES = frozenset(
    {"FORCE_COLOR", "TTY_COMPATIBLE", "TTY_INTERACTIVE", "NO_COLOR", "FLEDGE_DEBUG"}
)
CLEAN_ARTICLE = "\\documentclass{article}\n\\begin{document}\nHello.\n\\end{document}\n"
UNFINISHED_ARTICLE = (
    "\\documentclass{article}\n\\begin{document}\nTODO finish this.\n\\end{document}\n"
)
# A fenced ``sh`` block of a Markdown page.
SHELL_BLOCK = re.compile(r"^```sh\n(.*?)^```$", re.DOTALL | re.MULTILINE)
# A Select Graphic Rendition sequence, which sets colours and text attributes.
GRAPHIC_RENDITION = re.compile(rb"\x1b\[([0-9;]*)m")


def child_environment(**overrides: str) -> dict[str, str]:
    """Return the environment of a child that runs this checkout's ``latexprep``.

    The inherited environment loses the ``STREAM_VARIABLES`` and gains a ``PYTHONPATH`` that
    points at the source root.

    Args:
        **overrides: Variables that are added last and so replace any inherited value.

    Returns:
        The environment variables of the child.
    """
    inherited = {name: value for name, value in os.environ.items() if name not in STREAM_VARIABLES}
    return {**inherited, "PYTHONPATH": str(SOURCE_ROOT), **overrides}


def run_fledge(
    arguments: Sequence[str | Path],
    *,
    cwd: Path,
    environment: Mapping[str, str] | None = None,
) -> subprocess.CompletedProcess[bytes]:
    """Run ``python -m latexprep`` with ``arguments``: no input, both outputs kept as bytes.

    Args:
        arguments: The command-line arguments after ``python -m latexprep``.
        cwd: The working directory of the child.
        environment: The environment of the child, or ``None`` for :func:`child_environment`
            without overrides.

    Returns:
        The finished child process; a non-zero exit status does not raise.

    Raises:
        subprocess.TimeoutExpired: The child ran longer than ``TIMEOUT_SECONDS``.
    """
    return subprocess.run(
        [*FLEDGE, *map(str, arguments)],
        stdin=subprocess.DEVNULL,
        capture_output=True,
        cwd=cwd,
        env=dict(child_environment() if environment is None else environment),
        timeout=TIMEOUT_SECONDS,
        check=False,
    )


def here_document(page: str, delimiter: str) -> str:
    """Return the body of the quoted here-document ``<<'DELIMITER'`` in ``page``.

    The body is returned as ``cat`` writes it.

    Args:
        page: The Markdown text of a documentation page.
        delimiter: The word that opens and closes the here-document.

    Returns:
        The lines between the opening line and the closing delimiter.

    Raises:
        AssertionError: The page has no such here-document.
    """
    match = re.search(rf"<<'{delimiter}'\n(.*?)^{delimiter}$", page, flags=re.DOTALL | re.MULTILINE)
    if match is None:
        raise AssertionError(f"{EXAMPLE_PAGE.name} has no here-document <<'{delimiter}'")
    return match.group(1)


def fledge_command_lines(page: str) -> list[str]:
    """Return each ``fledge`` command in the ``sh`` blocks of ``page``.

    Continuation lines are joined before the commands are picked out.
    """
    return [
        line
        for block in SHELL_BLOCK.findall(page)
        for line in block.replace("\\\n", "").splitlines()
        if line.startswith("fledge ")
    ]


def prints_json(words: Sequence[str]) -> bool:
    """Return whether the command line ``words`` asks for the JSON report on stdout."""
    return "--json" in words or ("--output-format", "json") in pairwise(words)


def colour_parameters(output: bytes) -> set[int]:
    """Return the graphic-rendition parameters in ``output`` that set a colour.

    A colour parameter sets a foreground or background colour. Bold (1) and dim (2) are
    attributes, not colours; 30-49 and 90-107 are colours, including 38 and 48, which
    introduce 256-colour and true-colour values.
    """
    values = {
        int(value)
        for match in GRAPHIC_RENDITION.finditer(output)
        for value in match.group(1).split(b";")
        if value
    }
    return {value for value in values if 30 <= value <= 49 or 90 <= value <= 107}


def release_checks() -> ModuleType:
    """Load ``scripts/release_checks.py`` as a module; the script is not part of the package.

    Returns:
        The executed module.
    """
    spec = importlib.util.spec_from_file_location("release_checks", RELEASE_CHECKS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ExitCodeAndStreamTests(ProjectTestCase):
    """What a calling shell sees: the exit status, the report on stdout, progress on stderr."""

    def setUp(self) -> None:
        """Write a clean and an unfinished project and a settings file that expects ``report``."""
        super().setUp()
        self.clean = self.write_project({"main.tex": CLEAN_ARTICLE}, "clean")
        self.unfinished = self.write_project({"main.tex": UNFINISHED_ARTICLE}, "unfinished")
        self.settings = self.write("settings.toml", 'expected_document_class = "report"\n')

    def test_each_outcome_reaches_the_shell_as_its_documented_exit_code(self) -> None:
        """Check that each outcome reaches the shell as its documented exit code.

        docs/reports.md, "Exit codes": 0 passed, 1 passed with advisories, 3 blocked and 4
        error, with the JSON report still printed, and 64 for invalid syntax, with no report;
        ``rules --json`` and ``rule CODE --json`` print JSON and exit with 0.
        """
        cases: tuple[tuple[str, list[str | Path], int, dict[str, object] | None], ...] = (
            (
                "a clean article passes",
                ["inspect", self.clean, "--isolated", *JSON_REPORT],
                0,
                {"command": "inspect", "outcome": "passed"},
            ),
            (
                "an unfinished-text marker is an advisory",
                ["inspect", self.unfinished, "--isolated", *JSON_REPORT],
                1,
                {"outcome": "passed_with_advisories"},
            ),
            (
                "a violated setting from --config blocks",
                ["inspect", self.clean, "--config", self.settings, *JSON_REPORT],
                3,
                {"outcome": "blocked"},
            ),
            (
                "a missing input is an error with a report",
                ["inspect", self.root / "absent", "--isolated", *JSON_REPORT],
                4,
                {"command": "inspect", "outcome": "error"},
            ),
            ("an unknown option", ["inspect", self.clean, "--no-such-flag"], 64, None),
            ("inspect without its input", ["inspect"], 64, None),
            ("the catalogue as JSON", ["rules", "--json"], 0, {"schema_version": 1}),
            ("one check as JSON", ["rule", "TEX001", "--json"], 0, {"code": "TEX001"}),
        )
        for name, arguments, exit_code, expected in cases:
            with self.subTest(name):
                completed = run_fledge(arguments, cwd=self.root)
                self.assertEqual(completed.returncode, exit_code, completed.stderr)
                if expected is None:
                    self.assert_usage_error(completed)
                else:
                    self.assert_json_on_stdout_alone(completed, expected)

    def assert_usage_error(self, completed: subprocess.CompletedProcess[bytes]) -> None:
        """Assert that ``completed`` ended as invalid syntax does.

        Invalid syntax prints no report, only an explanation on stderr.
        """
        self.assertEqual(completed.stdout, b"")
        self.assertNotEqual(completed.stderr.strip(), b"")

    def assert_json_on_stdout_alone(
        self, completed: subprocess.CompletedProcess[bytes], expected: Mapping[str, object]
    ) -> None:
        """Assert that stdout is one JSON document holding the ``expected`` values.

        Stderr must stay empty.

        Args:
            completed: The finished child process.
            expected: The values the document must hold, by key.
        """
        self.assertEqual(completed.stderr, b"")
        document = json.loads(completed.stdout)
        self.assertEqual({key: document.get(key) for key in expected}, dict(expected))

    def test_progress_goes_to_stderr_and_a_piped_report_has_no_escape_sequences(self) -> None:
        """Check that progress goes to stderr and a piped report has no escape sequences.

        docs/reports.md, "The terminal summary": progress goes to stderr, as plain stage
        lines when redirected, and ``--quiet`` suppresses it. With stdout piped, the terminal
        report holds no escape byte, with or without ``NO_COLOR``, and it is the report that
        ``--quiet`` prints, so no progress line reaches stdout.
        """
        arguments = ["inspect", self.unfinished, "--isolated", "--offline"]
        quiet = run_fledge([*arguments, "--quiet"], cwd=self.root)
        self.assertEqual((quiet.returncode, quiet.stderr), (1, b""))
        self.assertNotEqual(quiet.stdout.strip(), b"")
        for name, environment in (
            ("NO_COLOR unset", child_environment()),
            ("NO_COLOR=1", child_environment(NO_COLOR="1")),
        ):
            with self.subTest(name):
                completed = run_fledge(arguments, cwd=self.root, environment=environment)
                self.assertEqual(completed.returncode, 1, completed.stderr)
                self.assertNotEqual(completed.stderr.strip(), b"", "no progress lines on stderr")
                self.assertNotIn(b"\x1b", completed.stderr)
                self.assertNotIn(b"\x1b", completed.stdout)
                self.assertEqual(completed.stdout, quiet.stdout)

    def test_no_color_removes_the_colours_of_a_report_on_a_terminal(self) -> None:
        """Check that ``NO_COLOR`` removes the colours of a report on a terminal.

        docs/reports.md, "The terminal summary": ``NO_COLOR`` disables color. A pipe has no
        colour either way, so ``FORCE_COLOR=1`` makes Rich treat it as a terminal: the report is
        coloured, and with ``NO_COLOR`` also set it has no colour, though attributes such as
        bold may remain.
        """
        arguments = ["inspect", self.unfinished, "--isolated", "--offline", "--quiet"]
        terminal = child_environment(FORCE_COLOR="1", TERM="xterm-256color")
        coloured = run_fledge(arguments, cwd=self.root, environment=terminal)
        plain = run_fledge(arguments, cwd=self.root, environment={**terminal, "NO_COLOR": "1"})
        self.assertEqual((coloured.returncode, plain.returncode), (1, 1), plain.stderr)
        self.assertNotEqual(colour_parameters(coloured.stdout), set(), "no colour to remove")
        self.assertEqual(colour_parameters(plain.stdout), set())


@unittest.skipIf(
    not hasattr(os, "geteuid") or os.geteuid() == 0,
    "permission bits do not stop this user from reading a file",
)
class UnreadableSourceTests(ProjectTestCase):
    """An included source file without read permission, inspected through the command line."""

    def setUp(self) -> None:
        """Write a project whose included section has no read permission, and record it."""
        super().setUp()
        self.project = self.write_project(
            {
                "main.tex": (
                    "\\documentclass{article}\n\\begin{document}\n"
                    "\\input{sections/intro}\n\\end{document}\n"
                ),
                "sections/intro.tex": "The introduction.\n",
            },
            "paper",
        )
        self.originals = directory_contents(self.project)
        self.unreadable = self.project / "sections" / "intro.tex"
        self.unreadable.chmod(0)
        # Runs before the temporary directory is removed: cleanups run last in, first out.
        self.addCleanup(self.unreadable.chmod, 0o644)

    def inspect(self) -> subprocess.CompletedProcess[bytes]:
        """Inspect the project through the command line, with the JSON report on stdout.

        Returns:
            The finished child process.
        """
        return run_fledge(
            ["inspect", self.project, "--main", "main.tex", "--isolated", *JSON_REPORT],
            cwd=self.root,
        )

    def test_the_run_ends_with_a_report_and_no_traceback_and_leaves_the_originals_alone(
        self,
    ) -> None:
        """Check that the run ends with a report, no traceback and unchanged originals.

        docs/reports.md, "Exit codes": an input problem exits with 4 (``error``) and a check
        that cannot read a file blocks with 3; the JSON report is still printed and stderr
        holds no traceback. docs/quickstart.md: the original project is never changed.

        Observed: 4. The import copies every file of the project before any check runs and
        cannot read this one, so the run stops with ``execution.request`` and the scope
        "run did not finish".
        """
        completed = self.inspect()
        self.assertIn(completed.returncode, (3, 4), completed.stderr)
        report = json.loads(completed.stdout)
        self.assertEqual(report["outcome"], {3: "blocked", 4: "error"}[completed.returncode])
        self.assertNotIn(b"Traceback", completed.stderr)
        self.assertEqual(stat.S_IMODE(self.unreadable.stat().st_mode), 0)
        self.unreadable.chmod(0o644)
        self.assertEqual(directory_contents(self.project), self.originals)

    def test_the_report_names_the_unreadable_file_and_holds_no_traceback(self) -> None:
        """Check that the report names the unreadable file and holds no traceback.

        docs/reports.md, "Exit codes": a problem with the input is reported for the person
        who must fix it, and a traceback is printed only with ``--debug``; the report names the
        unreadable file by its path in the project, and neither output holds a traceback.
        """
        completed = self.inspect()
        findings = json.loads(completed.stdout)["findings"]
        messages = [finding["message"] for finding in findings]
        self.assertTrue(any("sections/intro.tex" in message for message in messages), messages)
        self.assertNotIn(b"Traceback", completed.stdout)
        self.assertNotIn(b"Traceback", completed.stderr)


class OfflineExamplePageTests(unittest.TestCase):
    """docs/examples.md works as written: its files, its commands and what it says they do."""

    def test_the_offline_example_runs_as_the_page_says(self) -> None:
        """Check that the offline example runs as the page says.

        docs/examples.md: in a fresh directory, ``inspect`` and ``bib check`` of the demo exit
        with 0 and outcome ``passed``; the saved report passes too, holds the ``TEX009``
        not-applicable note and is the JSON printed on stdout; every other command on the page,
        including the inspection of examples/nested-paper, exits with 0.
        """
        page = EXAMPLE_PAGE.read_text(encoding="utf-8")
        commands = fledge_command_lines(page)
        self.assertGreater(len(commands), 3, "no fledge commands found; is the pattern stale?")
        with tempfile.TemporaryDirectory(prefix="fledge-examples-") as temporary:
            demo = Path(temporary).resolve()
            files = {"main.tex": here_document(page, "TEX")}
            files["references.bib"] = here_document(page, "BIB")
            write_project(demo / "paper", files)
            for line in commands:
                with self.subTest(line):
                    self.run_documented_command(line, demo)
            saved = json.loads((demo / "inspection.json").read_text(encoding="utf-8"))
        self.assertEqual(saved["outcome"], "passed")
        notes = {(finding["code"], finding["status"]) for finding in saved["findings"]}
        self.assertIn(("TEX009", "not_applicable"), notes)

    def run_documented_command(self, line: str, demo: Path) -> None:
        """Run one command of the page from the source root, with ``$demo_dir`` set to ``demo``.

        The command must exit with 0, and an ``inspect`` or ``bib`` command must report
        ``passed``, in the file that ``--report`` names too. A report command that prints the
        terminal summary is repeated with ``--json`` to read its outcome.

        Args:
            line: A ``fledge`` command line of the page.
            demo: The directory that ``$demo_dir`` stands for.
        """
        expanded = line.replace("$demo_dir", str(demo))
        self.assertNotIn("$", expanded, "the page uses a shell variable this test does not set")
        words = shlex.split(expanded)
        completed = run_fledge(words[1:], cwd=REPOSITORY)
        self.assertEqual(completed.returncode, 0, completed.stderr)
        if words[1] not in {"inspect", "bib"}:
            return
        if not prints_json(words):
            completed = run_fledge([*words[1:], "--json", "--quiet"], cwd=REPOSITORY)
        report = json.loads(completed.stdout)
        self.assertEqual(report["outcome"], "passed")
        if "--report" in words:
            saved = Path(words[words.index("--report") + 1])
            self.assertEqual(json.loads(saved.read_text(encoding="utf-8")), report)


class WheelCheckTests(unittest.TestCase):
    """The wheel check of scripts/release_checks.py inspects the documented offline example."""

    def test_the_wheel_check_carries_the_files_of_the_offline_example_page(self) -> None:
        """Check that the wheel check carries the files of the offline example page.

        scripts/release_checks.py: the installed wheel inspects the example of
        docs/examples.md, so the files embedded in the script are the files of that page.
        """
        page = EXAMPLE_PAGE.read_text(encoding="utf-8")
        checks = release_checks()
        self.assertEqual(checks.DEMO_MAIN_TEX, here_document(page, "TEX"))
        self.assertEqual(checks.DEMO_REFERENCES_BIB, here_document(page, "BIB"))

    def test_the_offline_inspection_fails_unless_it_exits_with_0_and_passes(self) -> None:
        """Check that the offline inspection fails unless it exits with 0 and passes.

        scripts/release_checks.py: the wheel check fails unless the inspection of the
        example exits with 0 and its JSON report says ``passed``.
        """
        checks = release_checks()
        cases = (
            ("passed", 0, '{"outcome": "passed"}', 0),
            ("exit code 1 although the report passed", 1, '{"outcome": "passed"}', 1),
            ("advisories", 1, '{"outcome": "passed_with_advisories"}', 1),
            ("blocked", 3, '{"outcome": "blocked"}', 1),
            ("no report", 0, "not a report", 1),
        )
        with tempfile.TemporaryDirectory(prefix="fledge-wheel-check-") as temporary:
            for name, exit_code, stdout, expected in cases:
                with self.subTest(name):
                    script = f"import sys; print({stdout!r}); sys.exit({exit_code})"
                    with redirect_stdout(io.StringIO()):
                        result = checks.inspect_offline_demo(
                            [sys.executable, "-c", script], Path(temporary).resolve() / name
                        )
                    self.assertEqual(result, expected)

    def test_a_failed_inspection_shows_what_the_child_printed_also_when_it_hangs(self) -> None:
        """Check that a failed inspection shows what the child printed, also when it hangs.

        scripts/release_checks.py: a failure shows the end of the child's stdout when it
        holds no report, and the end of stderr; a child that does not finish within the time
        limit is stopped and fails the check with what it printed so far.
        """
        checks = release_checks()
        # Long enough for the child to start and print on a busy runner, short for a test.
        checks.DEMO_TIMEOUT_SECONDS = 2
        no_report = "print('not a report', flush=True); print('child warning', file=sys.stderr)"
        cases = (
            ("no report", no_report),
            ("hangs", f"{no_report}; sys.stderr.flush(); time.sleep(60)"),
        )
        with tempfile.TemporaryDirectory(prefix="fledge-wheel-check-") as temporary:
            for name, body in cases:
                with self.subTest(name):
                    printed = io.StringIO()
                    with redirect_stdout(printed):
                        result = checks.inspect_offline_demo(
                            [sys.executable, "-c", f"import sys, time; {body}"],
                            Path(temporary).resolve() / name,
                        )
                    self.assertEqual(result, 1)
                    self.assertIn("stdout: not a report", printed.getvalue())
                    self.assertIn("stderr: child warning", printed.getvalue())


if __name__ == "__main__":
    unittest.main()
