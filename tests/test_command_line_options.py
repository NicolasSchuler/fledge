"""Every option of every job command reaches the settings, the request or the report.

One table names, for each job command, every option it accepts, the value to pass and
where the effect must show: a setting, a field of the job request, the rendering of the
final report, a saved report file, the progress display or the debug switch. A second test
fails when a command gains an option that the table does not cover, so an option can no
longer be accepted and silently ignored.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import patch

import click
from click.testing import CliRunner

from latexprep.cli import CommandOptions, cli
from latexprep.command_options import debug_enabled
from latexprep.job_context import JobRequest
from latexprep.report import Outcome, Report
from latexprep.report_formats import OutputFormat
from tests.support import ProjectTestCase

# The job commands, by their name path below ``fledge``.
JOB_COMMANDS = (
    ("inspect",),
    ("check",),
    ("prepare",),
    ("bib", "check"),
    ("fmt",),
    ("pdf", "check"),
)


@dataclasses.dataclass
class Observed:
    """What one invocation handed to the job and to the final report.

    Attributes:
        request: The job request.
        report: The report the job filled in.
        output_format: The format the final report was printed in.
        render: The rendering options of the final report.
        quiet: Whether progress was suppressed.
        debug: Whether debugging was on while the job ran.
        stderr: What the command wrote to standard error.
    """

    request: JobRequest
    report: Report
    output_format: OutputFormat
    render: Any
    quiet: bool
    debug: bool
    stderr: str


# An expectation: a description of the effect and a function that checks it.
Check = Callable[["OptionTableTests", Observed], None]


def setting(name: str, expected: object) -> Check:
    """Expect the setting ``name`` (a dotted path below ``Settings``) to be ``expected``."""

    def check(test: OptionTableTests, observed: Observed) -> None:
        """Compare the setting with the expectation."""
        value: object = observed.request.settings
        for part in name.split("."):
            value = getattr(value, part)
        test.assertEqual(value, expected, name)

    return check


def request_field(name: str, expected: object) -> Check:
    """Expect the request field ``name`` to be ``expected``; a path is compared resolved."""

    def check(test: OptionTableTests, observed: Observed) -> None:
        """Compare the request field with the expectation."""
        value = getattr(observed.request, name)
        if isinstance(expected, Path):
            test.assertIsInstance(value, Path, name)
            assert isinstance(value, Path)
            test.assertEqual(value.resolve(), expected.resolve(), name)
        else:
            test.assertEqual(value, expected, name)

    return check


def render_option(name: str) -> Check:
    """Expect the rendering option ``name`` of the final report to be on."""

    def check(test: OptionTableTests, observed: Observed) -> None:
        """Check that the rendering option is on."""
        test.assertTrue(getattr(observed.render, name), name)

    return check


def printed_as(output_format: OutputFormat) -> Check:
    """Expect the final report to be printed in ``output_format``."""

    def check(test: OptionTableTests, observed: Observed) -> None:
        """Compare the output format with the expectation."""
        test.assertIs(observed.output_format, output_format)

    return check


def saved_file(name: str) -> Check:
    """Expect a report file named ``name`` in the temporary root."""

    def check(test: OptionTableTests, observed: Observed) -> None:
        """Check that the file was saved."""
        test.assertTrue((test.root / name).is_file(), name)

    return check


def recorded_preset(name: str) -> Check:
    """Expect the report to record the preset ``name`` beneath the settings."""

    def check(test: OptionTableTests, observed: Observed) -> None:
        """Compare the recorded preset with the expectation."""
        test.assertEqual(observed.report.execution.get("preset"), name)

    return check


def quiet(test: OptionTableTests, observed: Observed) -> None:
    """Expect progress to be suppressed."""
    test.assertTrue(observed.quiet)


def debugging(test: OptionTableTests, observed: Observed) -> None:
    """Expect debugging to be on while the job ran."""
    test.assertTrue(observed.debug)


def unchanged(test: OptionTableTests, observed: Observed) -> None:
    """Expect the request of the formatting check to equal one made without the option."""
    plain = test.invoke(("fmt",), ())
    test.assertEqual(observed.request, plain.request)


class OptionTableTests(ProjectTestCase):
    """The table of every option of every job command, and its completeness.

    Attributes:
        maxDiff: Show whole differences.
    """

    maxDiff = None

    def setUp(self) -> None:
        """Write a project with a settings file, a PDF in it, and a separate settings file."""
        super().setUp()
        self.project = self.root / "paper"
        self.project.mkdir()
        (self.project / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\nText.\n\\end{document}\n"
        )
        (self.project / "paper.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")
        (self.project / ".fledge.toml").write_text("max_pages = 9\n")
        (self.root / "explicit.toml").write_text("max_pages = 7\n")
        (self.root / "reference.pdf").write_bytes(b"%PDF-1.4\n%%EOF\n")

    def table(self) -> dict[tuple[str, ...], dict[str, tuple[tuple[str, ...], Check]]]:
        """Return, for each job command, each parameter with its arguments and expectation.

        Returns:
            For each command path, the parameter names mapped to the command-line words
            that set them and the check of their effect.
        """
        root = self.root
        reports: dict[str, tuple[tuple[str, ...], Check]] = {
            "config": (("--config", str(root / "explicit.toml")), setting("max_pages", 7)),
            "isolated": (("--isolated",), setting("max_pages", None)),
            "json_output": (("--json",), printed_as(OutputFormat.JSON)),
            "output_format": (("--output-format", "compact"), printed_as(OutputFormat.COMPACT)),
            "quiet": (("-q",), quiet),
            "show_passed": (("--show-passed",), render_option("show_passed")),
            "html_report": (("--html-report", str(root / "r.html")), saved_file("r.html")),
            "diagnostics": (("--diagnostics", str(root / "d.zip")), saved_file("d.zip")),
            "report": (("--report", str(root / "r.json")), saved_file("r.json")),
            "jobs": (("--jobs", "3"), setting("jobs", 3)),
            "job_timeout_seconds": (
                ("--job-timeout-seconds", "45"),
                setting("job_timeout_seconds", 45),
            ),
            "timeout_seconds": (("--timeout-seconds", "30"), setting("timeout_seconds", 30)),
        }
        selection = {
            "preset": (("--preset", "arxiv"), recorded_preset("arxiv")),
            "select_checks": (("--select", "tex001"), setting("checks.select", ("TEX001",))),
            "ignore_checks": (("--ignore", "TEX002"), setting("checks.ignore", ("TEX002",))),
        }
        document = {
            "main": (("-m", "main.tex"), setting("main", "main.tex")),
            "online": (("--online",), setting("online_checks.online", True)),
        }
        builds = {
            "engine": (("--engine", "lualatex"), setting("engine", "lualatex")),
            "bibliography_backend": (
                ("--bibliography-backend", "biber"),
                setting("bibliography_backend", "biber"),
            ),
            "reference_pdf": (
                ("--reference-pdf", str(root / "reference.pdf")),
                request_field("reference_pdf", root / "reference.pdf"),
            ),
            "max_pages": (("--max-pages", "3"), setting("max_pages", 3)),
            "preview_output": (
                ("--preview-output", str(root / "previews")),
                request_field("preview_output", root / "previews"),
            ),
            "force": (("--force",), request_field("force", True)),
            "build_jobs": (("--build-jobs", "2"), setting("build_jobs", 2)),
            "render_jobs": (("--render-jobs", "2"), setting("render_jobs", 2)),
        }
        preparation = {
            "output": (("-o", str(root / "out")), request_field("output", root / "out")),
            "layout": (("--layout", "flat"), setting("layout", "flat")),
            "apply_formatting": (("--format",), setting("apply_formatting", True)),
            "normalize_doi": (("--normalize-doi",), setting("normalize_doi", True)),
            "cleanup": (("--cleanup",), setting("cleanup", True)),
            "dry_run": (("--dry-run",), request_field("dry_run", True)),
            "diff": (("--diff",), render_option("show_diff")),
            "show_files": (("--show-files",), render_option("show_files")),
        }
        pdf = {
            **{name: value for name, value in builds.items() if name not in {"engine"}},
            "select_checks": (("--select", "pdf001"), setting("checks.select", ("PDF001",))),
            "ignore_checks": (("--ignore", "PDF101"), setting("checks.ignore", ("PDF101",))),
        }
        for name in ("bibliography_backend", "build_jobs"):
            pdf.pop(name)
        return {
            ("inspect",): {**reports, **selection, **document},
            ("check",): {**reports, **selection, **document, **builds},
            ("prepare",): {**reports, **selection, **document, **builds, **preparation},
            ("bib", "check"): {
                **reports,
                **selection,
                **document,
                "select_checks": (("--select", "BIB"), setting("checks.select", ("BIB",))),
            },
            ("fmt",): {
                **reports,
                "check": (("--check",), unchanged),
                "diff": (("--diff",), render_option("show_diff")),
            },
            ("pdf", "check"): {
                **reports,
                "preset": selection["preset"],
                **pdf,
            },
        }

    def invoke(self, path: tuple[str, ...], words: tuple[str, ...]) -> Observed:
        """Run the command at ``path`` with ``words`` and observe what reached the job.

        The job and the report printer are replaced, so nothing is built or printed; the
        report files are saved as usual. ``prepare`` gets ``--dry-run`` unless the words
        name an output.

        Args:
            path: The command path.
            words: The options and their values.

        Returns:
            What the job and the printer received.
        """
        seen: dict[str, Any] = {}

        async def job(request, progress=None, report=None):
            """Record the request and the debug switch, and pass."""
            seen["request"], seen["debug"], seen["report"] = request, debug_enabled(), report
            report.outcome = Outcome.PASSED
            return report

        def printer(_report, output_format, **keywords):
            """Record how the final report would have been printed."""
            seen["format"], seen["render"] = output_format, keywords["options"]

        source = self.project / "paper.pdf" if path == ("pdf", "check") else self.project
        extra: tuple[str, ...] = ()
        if path == ("prepare",) and not {"-o", "--dry-run"} & set(words):
            extra = ("--dry-run",)
        with (
            patch("latexprep.cli.run_job", new=job),
            patch("latexprep.cli.write_redacted_report", new=printer),
            patch("latexprep.cli.TerminalProgress.__enter__", autospec=True) as progress,
        ):
            progress.side_effect = lambda instance: seen.setdefault("quiet", instance.quiet)
            result = CliRunner().invoke(cli, [*path, str(source), *words, *extra])
        self.assertEqual(result.exit_code, 0, result.output)
        return Observed(
            seen["request"],
            seen["report"],
            seen["format"],
            seen["render"],
            seen["quiet"],
            seen["debug"],
            result.stderr,
        )

    def test_every_option_of_every_job_command_reaches_its_target(self) -> None:
        for path, options in self.table().items():
            for name, (words, check) in options.items():
                with self.subTest(command=" ".join(path), option=name):
                    check(self, self.invoke(path, words))
                    for leftover in ("r.html", "d.zip", "r.json"):
                        (self.root / leftover).unlink(missing_ok=True)

    def test_the_debug_option_turns_debugging_on_for_every_job_command(self) -> None:
        for path in JOB_COMMANDS:
            with self.subTest(command=" ".join(path)):
                debugging(self, self.invoke(path, ("--debug",)))

    def test_the_table_covers_every_parameter_of_every_job_command(self) -> None:
        table = self.table()
        fields = {item.name for item in dataclasses.fields(CommandOptions)}
        self.assertEqual(set(table), set(JOB_COMMANDS))
        for path in JOB_COMMANDS:
            command: click.Command = cli
            for name in path:
                assert isinstance(command, click.Group)
                command = command.commands[name]
            names = {
                parameter.name
                for parameter in command.params
                if not isinstance(parameter, click.Argument) and parameter.name != "debug"
            }
            with self.subTest(command=" ".join(path)):
                self.assertEqual(names, set(table[path]))
                exposed = {item.name for item in command.params if item.expose_value}
                self.assertLessEqual(exposed, fields)


class CommandShapeTests(ProjectTestCase):
    """Which options each command offers, how its help is laid out and named."""

    def help_text(self, *path: str) -> str:
        """Return the help of the command at ``path`` at width 100."""
        result = CliRunner().invoke(cli, [*path, "--help"], terminal_width=100)
        self.assertEqual(result.exit_code, 0, result.output)
        return result.output

    def test_options_that_change_nothing_are_not_offered(self) -> None:
        self.write("paper/main.tex", "\\documentclass{article}\n")
        source = str(self.root / "paper")
        cases = {
            ("inspect",): ("--show-files", "--preview-output", "--build-jobs", "--render-jobs"),
            ("check",): ("--show-files",),
            ("bib", "check"): ("--show-files", "--preview-output", "--build-jobs"),
            ("fmt",): ("--show-files", "--preview-output", "--render-jobs", "--force"),
            ("pdf", "check"): ("--show-files", "--build-jobs", "--engine", "--main"),
        }
        for path, removed in cases.items():
            for option in removed:
                with self.subTest(command=" ".join(path), option=option):
                    words = [option] if option in {"--show-files", "--force"} else [option, "2"]
                    result = CliRunner().invoke(cli, [*path, source, *words])
                    self.assertEqual(result.exit_code, 64, result.output)
                    self.assertIn("No such option", result.stderr)

    def test_commands_are_listed_in_workflow_order(self) -> None:
        overview = self.help_text().split("Commands:")[1]
        names = [line.split()[0] for line in overview.strip().splitlines()]
        self.assertEqual(
            names,
            [
                "inspect",
                "check",
                "prepare",
                "bib",
                "pdf",
                "fmt",
                "submission-text",
                "rules",
                "rule",
                "doctor",
                "init",
            ],
        )

    def test_arguments_are_named_input_or_pdf(self) -> None:
        for path in (("inspect",), ("check",), ("prepare",), ("bib", "check"), ("fmt",)):
            with self.subTest(command=" ".join(path)):
                self.assertIn(
                    f"Usage: fledge {' '.join(path)} [OPTIONS] INPUT\n", self.help_text(*path)
                )
        self.assertIn("Usage: fledge pdf check [OPTIONS] PDF\n", self.help_text("pdf", "check"))
        self.assertIn("Usage: fledge doctor [OPTIONS] [INPUT]\n", self.help_text("doctor"))

    def option_rows(self, *path: str) -> list[str]:
        """Return the options of the common section of a command's help, in order."""
        section = self.help_text(*path).split("Common options:\n")[1].split("\n\n")[0]
        return [line.split()[0] for line in section.splitlines() if line.startswith("  -")]

    def test_main_and_output_come_first_and_have_short_flags(self) -> None:
        self.assertEqual(self.option_rows("prepare")[:2], ["-o,", "-m,"])
        for path in (("inspect",), ("check",), ("bib", "check")):
            with self.subTest(command=" ".join(path)):
                self.assertEqual(self.option_rows(*path)[0], "-m,")
        for path in (("inspect",), ("doctor",)):
            with self.subTest(command=" ".join(path)):
                self.assertIn("-q, --quiet", self.help_text(*path))
        self.assertIn("-m, --main FILE", self.help_text("submission-text"))

    def test_the_main_commands_end_their_help_with_examples(self) -> None:
        for path in (
            ("inspect",),
            ("check",),
            ("prepare",),
            ("bib", "check"),
            ("pdf", "check"),
            ("fmt",),
            ("init",),
            ("doctor",),
            ("submission-text",),
            ("rules",),
        ):
            with self.subTest(command=" ".join(path)):
                examples = self.help_text(*path).split("Examples:\n")
                self.assertEqual(len(examples), 2)
                self.assertTrue(examples[1].lstrip().startswith("fledge "), examples[1])

    def test_numeric_options_show_their_ranges(self) -> None:
        text = self.help_text("check")
        self.assertIn("[1<=x<=64]", text)
        self.assertIn("[x>=1]", text)

    def test_help_avoids_the_jargon_the_review_named(self) -> None:
        everything = " ".join(
            self.help_text(*path) for path in (("inspect",), ("check",), ("prepare",))
        )
        for jargon in ("escaped offline HTML", "explicitly unverified", "Most operations"):
            self.assertNotIn(jargon, everything)
