"""Common command-line mistakes: read as what was meant, or refused with a clear message."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

from click.testing import CliRunner

from latexprep.cli import USAGE_EXIT_CODE, cli
from latexprep.cli_inputs import check_selection_note, choose_input
from latexprep.job_context import JobCommand
from latexprep.report import Outcome
from tests.support import ProjectTestCase


class InputTestCase(ProjectTestCase):
    """A project ``paper`` with one main file, and a way to run a command without a job."""

    def setUp(self) -> None:
        """Write ``paper/main.tex`` and ``paper/sections/intro.tex``."""
        super().setUp()
        self.project = self.root / "paper"
        self.write(
            "paper/main.tex", "\\documentclass{article}\n\\begin{document}\n\\end{document}\n"
        )
        self.write("paper/sections/intro.tex", "Intro.\n")

    def run_job_of(self, *words: str) -> tuple[Any, Any]:
        """Run ``fledge`` with ``words`` and a job that only records its request.

        Returns:
            The request the job received, or ``None`` when no job ran, and the Click result.
        """
        seen: dict[str, Any] = {}

        async def job(request, progress=None, report=None):
            """Record the request and pass."""
            seen["request"] = request
            report.outcome = Outcome.PASSED
            return report

        with patch("latexprep.cli.run_job", new=job):
            result = CliRunner().invoke(cli, list(words))
        return seen.get("request"), result


class TexFileInputTests(InputTestCase):
    """A ``.tex`` file given as INPUT stands for its folder and ``--main``."""

    def test_a_tex_file_as_input_is_its_folder_with_main(self) -> None:
        for command in (("inspect",), ("check",), ("bib", "check"), ("prepare",)):
            with self.subTest(command=" ".join(command)):
                extra = ["--dry-run"] if command == ("prepare",) else []
                request, result = self.run_job_of(
                    *command, str(self.project / "main.tex"), "--json", *extra
                )
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(request.source, self.project)
                self.assertEqual(request.settings.main, "main.tex")
                self.assertIn("is a .tex file, so INPUT is its folder", result.stderr)
                self.assertIn("--main is main.tex", result.stderr)

    def test_quiet_leaves_out_the_note(self) -> None:
        request, result = self.run_job_of("inspect", str(self.project / "main.tex"), "--quiet")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(request.settings.main, "main.tex")
        self.assertEqual(result.stderr, "")

    def test_a_tex_file_together_with_main_is_a_usage_error(self) -> None:
        request, result = self.run_job_of(
            "check", str(self.project / "main.tex"), "--main", "main.tex"
        )
        self.assertIsNone(request)
        self.assertEqual(result.exit_code, USAGE_EXIT_CODE, result.output)
        self.assertIn("give the project folder as INPUT", result.stderr)

    def test_a_tex_file_in_the_current_folder_is_read_from_there(self) -> None:
        previous = Path.cwd()
        os.chdir(self.project)
        self.addCleanup(os.chdir, previous)
        choice = choose_input(Path("main.tex"), None)
        self.assertEqual((choice.source, choice.main), (Path(), "main.tex"))

    def test_the_note_shows_escape_sequences_in_the_name_as_text(self) -> None:
        hostile = self.project / "evil\x1b]0;title\x07name.tex"
        hostile.write_text("\\documentclass{article}\n\\begin{document}\n\\end{document}\n")
        request, result = self.run_job_of("inspect", str(hostile))
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(request.settings.main, hostile.name)
        self.assertNotIn("\x1b", result.stderr)
        self.assertNotIn("\x07", result.stderr)
        self.assertIn("evil\\u001b]0;title\\u0007name.tex", result.stderr)
        request, result = self.run_job_of("check", str(hostile), "--main", "main.tex")
        self.assertEqual(result.exit_code, USAGE_EXIT_CODE, result.output)
        self.assertNotIn("\x1b", result.stderr)

    def test_submission_text_takes_a_tex_file_as_input(self) -> None:
        self.write(
            "paper/main.tex",
            "\\documentclass{article}\n\\title{A Title}\n\\begin{document}\n\\end{document}\n",
        )
        result = CliRunner().invoke(
            cli, ["submission-text", str(self.project / "main.tex"), "--json"]
        )
        self.assertIn(result.exit_code, {0, 3}, result.output)
        self.assertIn("A Title", json.loads(result.stdout)["fields"]["title"]["draft"])
        self.assertIn("is a .tex file", result.stderr)


class MainPathTests(InputTestCase):
    """``--main`` written as a path from the current folder or as an absolute path."""

    def test_main_from_the_current_folder_into_input_is_made_relative(self) -> None:
        previous = Path.cwd()
        os.chdir(self.root)
        self.addCleanup(os.chdir, previous)
        for main in ("paper/main.tex", str(self.project / "main.tex"), "main.tex"):
            with self.subTest(main=main):
                request, result = self.run_job_of("inspect", "paper", "--main", main, "-q")
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertEqual(request.settings.main, "main.tex")
        request, result = self.run_job_of("inspect", "paper", "-m", "paper/sections/intro.tex")
        self.assertEqual(request.settings.main, "sections/intro.tex")

    def test_a_main_outside_input_is_left_for_the_job_to_refuse(self) -> None:
        self.write("other/main.tex", "\\documentclass{article}\n")
        outside = str(self.root / "other" / "main.tex")
        self.assertEqual(choose_input(self.project, outside).main, outside)
        self.assertEqual(choose_input(self.project, "missing.tex").main, "missing.tex")


class PrepareDestinationTests(InputTestCase):
    """``prepare`` needs a destination, and report files stay out of output folders."""

    def test_prepare_without_output_or_dry_run_names_both(self) -> None:
        request, result = self.run_job_of("prepare", str(self.project))
        self.assertIsNone(request)
        self.assertEqual(result.exit_code, USAGE_EXIT_CODE, result.output)
        self.assertIn("prepare needs --output DIR", result.stderr)
        self.assertIn("--dry-run", result.stderr)

    def test_a_report_file_inside_the_output_folder_is_refused(self) -> None:
        output = self.root / "submission"
        output.mkdir()
        for option, name in (("--report", "r.json"), ("--html-report", "r.html")):
            with self.subTest(option=option):
                request, result = self.run_job_of(
                    "prepare",
                    str(self.project),
                    "-o",
                    str(output),
                    option,
                    str(output / name),
                    "--json",
                )
                self.assertIsNone(request)
                report = json.loads(result.stdout)
                self.assertEqual(report["outcome"], "error")
                [finding] = report["findings"]
                self.assertIn("outside the output and preview folders", finding["message"])
                self.assertFalse((output / name).exists())

    def test_an_existing_empty_folder_is_no_report_file(self) -> None:
        folder = self.root / "reports"
        folder.mkdir()
        request, result = self.run_job_of(
            "inspect", str(self.project), "--report", str(folder), "--json"
        )
        self.assertIsNone(request)
        [finding] = json.loads(result.stdout)["findings"]
        self.assertIn("Output already exists", finding["message"])


class CheckSelectionNoteTests(InputTestCase):
    """``--select`` choosing checks that the command cannot run."""

    def test_only_unrunnable_checks_are_a_usage_error_naming_the_right_command(self) -> None:
        for words, expected in (
            (("inspect", str(self.project), "--select", "PDF001"), "fledge check"),
            (("inspect", str(self.project), "--select", "bld"), "fledge check"),
            (("bib", "check", str(self.project), "--select", "MAN"), "fledge inspect"),
            (("bib", "check", str(self.project), "--select", "STY"), "fledge inspect"),
            (("pdf", "check", str(self.project), "--select", "STY3"), "fledge inspect"),
        ):
            with self.subTest(words=words):
                request, result = self.run_job_of(*words)
                self.assertIsNone(request)
                self.assertEqual(result.exit_code, USAGE_EXIT_CODE, result.output)
                self.assertIn("cannot run", result.stderr)
                self.assertIn(expected, result.stderr)

    def test_some_unrunnable_checks_draw_a_warning_and_the_run_goes_on(self) -> None:
        request, result = self.run_job_of(
            "inspect", str(self.project), "--select", "TEX001", "--select", "PDF001", "--quiet"
        )
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(request.settings.checks.select, ("TEX001", "PDF001"))
        self.assertIn("Warning: PDF001 will not run: inspect does not build the PDF", result.stderr)

    def test_checks_the_command_runs_draw_no_note(self) -> None:
        cases = (
            (JobCommand.INSPECT, ("PDF313",)),
            (JobCommand.INSPECT, ("ALL",)),
            (JobCommand.INSPECT, ("TEX", "BIB1")),
            (JobCommand.INSPECT, ("ALL", "STY")),
            (JobCommand.CHECK, ("PDF001",)),
            (JobCommand.PREPARE, ("FMT",)),
            (JobCommand.BIBLIOGRAPHY, ("BIB", "NET")),
            (JobCommand.PDF, ("PDF", "CMP")),
            (JobCommand.INSPECT, ("no-such-check",)),
        )
        for command, selectors in cases:
            with self.subTest(command=command, selectors=selectors):
                self.assertIsNone(check_selection_note(command, selectors))

    def test_a_prefix_names_the_first_unrunnable_checks_and_counts_the_rest(self) -> None:
        note = check_selection_note(JobCommand.PDF, ("TEX", "PDF001"))
        assert note is not None
        self.assertRegex(note, r"^Warning: TEX\d{3}, .* and \d+ more will not run")
        self.assertIn("pdf check reads only the PDF", note)
