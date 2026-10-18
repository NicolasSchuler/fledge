"""Golden outputs: the help of every command and the report of a controlled check run.

The expected texts live in ``tests/golden``. After an intended change of an output, regenerate
them with ``FLEDGE_UPDATE_GOLDEN=1 python -m pytest tests/test_golden_outputs.py`` and review
the diff; see "Golden outputs" in ``docs/development.md``.
"""

from __future__ import annotations

import asyncio
import json
import os
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

import click
from click.testing import CliRunner
from rich.console import Console

from latexprep import __version__
from latexprep.cli import cli
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.findings import Finding
from latexprep.job_context import JobRequest
from latexprep.report import Report
from latexprep.terminal_report import print_report
from tests.support import (
    GOLDEN_DIRECTORY,
    ProjectTestCase,
    assert_matches_golden,
    controlled_backends,
    normalise_report_data,
    schema_violations,
)

HELP_WIDTH = 100
SCHEMA = Path(__file__).resolve().parents[1] / "docs" / "_static" / "report.schema.json"
# Resource accounting depends on timing and the host, so only its keys are compared.
MEASURED_SECTIONS = frozenset({"observed", "resources"})


def command_paths(command: click.Command, prefix: tuple[str, ...] = ()) -> list[tuple[str, ...]]:
    """The name path of ``command`` and of every command below it, depth first."""
    paths = [prefix]
    if isinstance(command, click.Group):
        for name, child in sorted(command.commands.items()):
            paths.extend(command_paths(child, (*prefix, name)))
    return paths


def help_golden_name(path: tuple[str, ...]) -> str:
    return "help/" + "-".join(path or ("fledge",)) + ".txt"


class HelpGoldenTests(unittest.TestCase):
    def help_text(self, path: tuple[str, ...]) -> str:
        environment = {"COLUMNS": str(HELP_WIDTH), "NO_COLOR": "1"}
        result = CliRunner(env=environment).invoke(
            cli, [*path, "--help"], terminal_width=HELP_WIDTH, color=False
        )
        self.assertEqual(result.exit_code, 0, result.output)
        return result.output

    def test_the_help_of_every_command_matches_its_golden_file(self) -> None:
        paths = command_paths(cli)
        self.assertGreater(len(paths), 10)
        for path in paths:
            with self.subTest(command=" ".join(path) or "fledge"):
                assert_matches_golden(self, help_golden_name(path), self.help_text(path))

    def test_no_golden_help_file_outlives_its_command(self) -> None:
        if os.environ.get("FLEDGE_UPDATE_GOLDEN") == "1":
            self.skipTest("goldens are being regenerated")
        expected = {help_golden_name(path) for path in command_paths(cli)}
        present = {
            path.relative_to(GOLDEN_DIRECTORY).as_posix()
            for path in (GOLDEN_DIRECTORY / "help").glob("*.txt")
        }
        self.assertEqual(present - expected, set(), "Delete the golden files of removed commands.")


class GoldenHelperTests(ProjectTestCase):
    """The comparison fails with a way forward, and regeneration rewrites the file."""

    def compare(self, actual: str) -> None:
        with patch("tests.support.GOLDEN_DIRECTORY", self.root):
            assert_matches_golden(self, "nested/sample.txt", actual)

    def test_a_matching_file_passes_and_a_different_one_names_the_regeneration_variable(
        self,
    ) -> None:
        self.write("nested/sample.txt", "expected\n")
        with patch.dict(os.environ, {"FLEDGE_UPDATE_GOLDEN": "0"}):
            self.compare("expected\n")
            with self.assertRaises(AssertionError) as raised:
                self.compare("changed\n")
        self.assertIn("FLEDGE_UPDATE_GOLDEN=1", str(raised.exception))

    def test_a_missing_file_fails_instead_of_passing_silently(self) -> None:
        with patch.dict(os.environ, {"FLEDGE_UPDATE_GOLDEN": "0"}):
            with self.assertRaises(AssertionError) as raised:
                self.compare("anything\n")
        self.assertIn("Missing golden file", str(raised.exception))

    def test_regeneration_writes_the_file_and_creates_its_directory(self) -> None:
        with patch.dict(os.environ, {"FLEDGE_UPDATE_GOLDEN": "1"}):
            self.compare("new text\n")
        self.assertEqual((self.root / "nested" / "sample.txt").read_text(), "new text\n")


class ControlledCheckGoldenTests(ProjectTestCase):
    """One ``check`` run with controlled build and PDF backends, and what it produces."""

    def setUp(self) -> None:
        super().setUp()
        self.project = self.write_project(
            {
                "main.tex": (
                    "\\documentclass{article}\n\\begin{document}\n"
                    "Knuth~\\cite{knuth1984} wrote this. % TODO: check the claim\n"
                    "\\bibliographystyle{plain}\n\\bibliography{references}\n"
                    "\\end{document}\n"
                ),
                "references.bib": (
                    "@book{knuth1984, author={Donald Knuth}, title={The TeXbook},"
                    " year={1984}, publisher={Addison-Wesley}}\n"
                    "@misc{unused2020, title={Never cited}, year={2020}}\n"
                ),
            },
            "paper",
        )
        passed_inspection = Finding(
            "pdf.page_count", "The PDF has 1 page.", severity="info", status="passed"
        )
        self.report: Report = asyncio.run(
            run_job(
                JobRequest("check", self.project, Settings()),
                backends=controlled_backends(inspect_pdf=[passed_inspection]),
            )
        )

    def replacements(self) -> dict[str, str]:
        return {
            str(self.project): "<project>",
            str(self.project.resolve()): "<project>",
            str(self.root): "<temporary>",
            __version__: "<version>",
        }

    def normalised_json(self) -> str:
        data = normalise_report_data(
            json.loads(self.report.to_json()), self.replacements(), measured=MEASURED_SECTIONS
        )
        return json.dumps(data, indent=1, ensure_ascii=False) + "\n"

    def terminal_text(self) -> str:
        output = StringIO()
        console = Console(
            file=output, width=HELP_WIDTH, color_system=None, no_color=True, highlight=False
        )
        print_report(self.report, console)
        text = output.getvalue()
        for old, new in self.replacements().items():
            text = text.replace(old, new)
        # Rich pads wrapped lines; trailing spaces are not part of what a reader sees.
        return "".join(line.rstrip() + "\n" for line in text.splitlines())

    def test_the_json_report_matches_its_golden_file(self) -> None:
        assert_matches_golden(self, "check-report.json", self.normalised_json())

    def test_the_terminal_summary_matches_its_golden_file(self) -> None:
        assert_matches_golden(self, "check-terminal.txt", self.terminal_text())

    def test_the_report_conforms_to_the_published_schema(self) -> None:
        schema = json.loads(SCHEMA.read_text(encoding="utf-8"))
        self.assertEqual(schema_violations(json.loads(self.report.to_json()), schema), [])

    def test_the_normalised_report_hides_nothing_but_paths_versions_and_measurements(self) -> None:
        normalised = json.loads(self.normalised_json())
        original = json.loads(self.report.to_json())
        self.assertEqual(list(normalised), list(original))
        self.assertEqual(normalised["findings"], original["findings"])
        self.assertEqual(normalised["settings"], original["settings"])


if __name__ == "__main__":
    unittest.main()
