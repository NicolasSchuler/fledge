"""``fledge doctor`` on the command line: the tool versions after the setup report."""

from __future__ import annotations

import json
import tempfile
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner
from rich.console import Console

from latexprep.cli import cli
from latexprep.doctor_command import print_tool_versions
from latexprep.report import Outcome, Report

TOOLS = {
    "latexmk": {
        "installed": True,
        "required": True,
        "version": ["Latexmk, John Collins, 9 March 2026. Version 4.88"],
    },
    "biber": {"installed": False, "required": False},
    "pdffonts": {"installed": True, "required": True, "version_verified": False},
    "odd\x1b[31m": {"installed": True, "required": True, "version": ["v1\x1b[0m"]},
}


class ToolVersionTests(unittest.TestCase):
    """The list of tools and versions that follows the terminal setup report."""

    def test_each_tool_shows_its_version_or_why_there_is_none(self) -> None:
        output = StringIO()
        print_tool_versions(TOOLS, Console(file=output, width=120, color_system=None))
        lines = [line.rstrip() for line in output.getvalue().splitlines() if line.strip()]
        self.assertEqual(lines[0].strip(), "Tools")
        rows = {line.split()[0]: " ".join(line.split()[1:]) for line in lines[1:]}
        self.assertEqual(rows["latexmk"], "Latexmk, John Collins, 9 March 2026. Version 4.88")
        self.assertEqual(rows["biber"], "not found (optional)")
        self.assertEqual(rows["pdffonts"], "version not verified")
        self.assertNotIn("\x1b", output.getvalue())

    def test_no_tools_print_nothing(self) -> None:
        output = StringIO()
        print_tool_versions({}, Console(file=output))
        self.assertEqual(output.getvalue(), "")

    def test_the_terminal_report_lists_the_tools_and_json_stays_clean(self) -> None:
        report = Report("doctor", outcome=Outcome.PASSED, tools=dict(TOOLS))
        with patch("latexprep.doctor_command.run_doctor", new=AsyncMock(return_value=report)):
            terminal = CliRunner().invoke(cli, ["doctor", "--quiet"], terminal_width=120)
            structured = CliRunner().invoke(cli, ["doctor", "--json", "-q"])
        self.assertEqual(terminal.exit_code, 0, terminal.output)
        self.assertIn("Tools", terminal.stdout)
        self.assertIn("Version 4.88", terminal.stdout)
        self.assertEqual(json.loads(structured.stdout)["tools"]["latexmk"]["installed"], True)

    def test_the_tool_list_and_the_report_show_the_same_redacted_copy(self) -> None:
        home_path = str(Path.home() / "private" / "chktexrc")
        tools = {
            "chktex": {"installed": True, "required": False, "version": [f"ChkTeX {home_path}"]}
        }
        with tempfile.TemporaryDirectory() as folder:
            for words in (["doctor", folder, "--quiet"], ["doctor", folder, "--json", "-q"]):
                report = Report("doctor", outcome=Outcome.PASSED, tools=dict(tools))
                with (
                    self.subTest(words=words),
                    patch(
                        "latexprep.doctor_command.run_doctor",
                        new=AsyncMock(return_value=report),
                    ),
                ):
                    result = CliRunner().invoke(cli, words, terminal_width=200)
                    self.assertEqual(result.exit_code, 0, result.output)
                    self.assertNotIn(str(Path.home()), result.stdout)
                    self.assertNotIn(folder, result.stdout)
                    self.assertIn("ChkTeX ~/private/chktexrc", result.stdout)


if __name__ == "__main__":
    unittest.main()
