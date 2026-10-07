"""Exercise Click commands and the human/agent reports against source fixtures."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from latexprep.cli import cli
from latexprep.models import Change, Finding, Report
from latexprep.presentation import render_compact, render_terminal
from latexprep.rules import RULES


class CommandTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "project"
        self.source.mkdir()
        self.original = (
            "\\documentclass{article}\n\\begin{document}\nTODO finish this.\n\\end{document}"
        )
        (self.source / "main.tex").write_text(self.original)
        self.runner = CliRunner()

    def test_click_compact_output_identifies_problem_location_and_next_step(self):
        result = self.runner.invoke(
            cli, ["inspect", str(self.source), "--quiet", "--output-format", "compact"]
        )
        self.assertEqual(result.exit_code, 1, result.output)
        self.assertIn("TEX001 warning/failed main.tex:3", result.stdout)
        self.assertIn("TODO", result.stdout)
        self.assertIn("next:", result.stdout)
        self.assertNotIn("\x1b", result.stdout)
        self.assertNotIn("Importing", result.stdout)
        self.assertEqual(len(result.stdout.splitlines()), 3)
        self.assertEqual((self.source / "main.tex").read_text(), self.original)

    def test_configured_manuscript_violation_reaches_cli_exit_and_json(self):
        config = self.root / "settings.toml"
        config.write_text('expected_document_class = "report"\nrequire_abstract = true\n')
        result = self.runner.invoke(
            cli, ["inspect", str(self.source), "--config", str(config), "--output-format", "json"]
        )
        self.assertEqual(result.exit_code, 3, result.output)
        report = json.loads(result.stdout)
        codes = {finding["code"] for finding in report["findings"]}
        self.assertTrue({"TEX101", "TEX104", "TEX001"} <= codes)
        self.assertEqual(report["outcome"], "blocked")
        self.assertIn("manuscript checks", {stage["name"] for stage in report["stages"]})
        for finding in report["findings"]:
            if finding["status"] == "failed" and finding["code"]:
                self.assertTrue(finding["suggestion"])

    def test_catalogue_and_explanation_are_available_without_a_project(self):
        result = self.runner.invoke(cli, ["rules", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            {row["code"] for row in json.loads(result.stdout)}, {rule.code for rule in RULES}
        )
        result = self.runner.invoke(cli, ["rule", "TEX001", "--json"])
        self.assertEqual(result.exit_code, 0)
        item = json.loads(result.stdout)
        self.assertEqual(item["name"], "source-edit-marker")
        self.assertTrue(item["tests"] and item["fix"])
        self.assertEqual(self.runner.invoke(cli, ["rule", "UNKNOWN"]).exit_code, 2)
        self.assertEqual(self.runner.invoke(cli, ["prepare", "--help"]).exit_code, 0)
        self.assertEqual(self.runner.invoke(cli, ["inspect"]).exit_code, 2)

    def test_parallel_cli_settings_reach_core_and_json_without_changing_output_mode(self):
        result = self.runner.invoke(
            cli,
            [
                "inspect",
                str(self.source),
                "--jobs",
                "4",
                "--build-jobs",
                "2",
                "--render-jobs",
                "2",
                "--job-timeout-seconds",
                "45",
                "--json",
                "--quiet",
            ],
        )
        self.assertEqual(result.exit_code, 1, result.output)
        report = json.loads(result.stdout)
        self.assertEqual(report["settings"]["jobs"], 4)
        self.assertEqual(report["settings"]["build_jobs"], 2)
        self.assertEqual(report["settings"]["render_jobs"], 2)
        self.assertEqual(report["settings"]["job_timeout_seconds"], 45)
        self.assertEqual(report["execution"]["resources"]["active_cpu"], 0)
        self.assertEqual(report["execution"]["resources"]["queued"], 0)
        self.assertNotIn("Importing", result.stdout)

    def test_boolean_options_preserve_toml_defaults_and_explicit_false_overrides(self):
        config = self.root / "settings.toml"
        config.write_text("format = true\nnormalize_doi = true\ncleanup = true\n")
        for flags, expected in (
            ([], True),
            (["--no-format", "--no-normalize-doi", "--no-cleanup"], False),
        ):
            with self.subTest(flags=flags):
                with patch(
                    "latexprep.cli.run_job",
                    new=AsyncMock(return_value=Report("prepare", "planned")),
                ) as run:
                    result = self.runner.invoke(
                        cli,
                        [
                            "prepare",
                            str(self.source),
                            "--config",
                            str(config),
                            "--dry-run",
                            "--json",
                            *flags,
                        ],
                    )
                self.assertEqual(result.exit_code, 0, result.output)
                settings = run.await_args.args[0].settings
                self.assertEqual(settings.format, expected)
                self.assertEqual(settings.normalize_doi, expected)
                self.assertEqual(settings.cleanup, expected)

    def test_conflicting_output_options_are_usage_errors(self):
        result = self.runner.invoke(
            cli, ["inspect", str(self.source), "--json", "--output-format", "compact"]
        )
        self.assertEqual(result.exit_code, 2)
        self.assertIn("conflicts", result.output)


class PresentationTests(unittest.TestCase):
    def test_terminal_prioritizes_failures_with_locations_and_clear_incomplete_status(self):
        report = Report(
            "check",
            "blocked",
            findings=[
                Finding(
                    "source-edit-marker", "TODO: [bold]literal[/bold]", path="main.tex", line=7
                ),
                Finding(
                    "pdf.font_embedding", "Font inventory unavailable", "error", "inconclusive"
                ),
                Finding(
                    "manuscript.abstract_words",
                    "120 words; maximum 100",
                    "error",
                    path="intro.tex",
                    line=3,
                ),
                Finding("pdf.pages", "1 page", "info", "passed"),
            ],
        )
        text = render_terminal(report)
        self.assertLess(text.index("TEX105"), text.index("PDF104"))
        self.assertLess(text.index("PDF104"), text.index("TEX001"))
        self.assertIn("Where: intro.tex:3", text)
        self.assertIn("Next:", text)
        self.assertIn("ERROR / inconclusive", text)
        self.assertIn("[bold]literal[/bold]", text)
        self.assertNotIn("pdf / pages", text)
        self.assertIn("pdf / pages", render_terminal(report, show_passed=True))
        self.assertNotIn("\x1b", text)

    def test_compact_preserves_complete_messages_on_one_line_and_no_terminal_escapes(self):
        message = "Unexpected [red]literal[/red]\nsecond line " + "long " * 100 + "THE END"
        report = Report(
            "inspect",
            "blocked",
            findings=[
                Finding(
                    "source-missing-dependency",
                    "\x1b[31m" + message,
                    "error",
                    path="chapter.tex",
                    line=19,
                )
            ],
        )
        text = render_compact(report)
        self.assertEqual(len(text.splitlines()), 2)
        self.assertIn("TEX005 error/failed chapter.tex:19", text)
        self.assertIn("[red]literal[/red]\\nsecond line", text)
        self.assertIn("THE END", text)
        self.assertNotIn("\x1b", text)

    def test_formatting_diff_is_shown_only_when_requested(self):
        report = Report(
            "fmt",
            "passed_with_advisories",
            changes=[
                Change(
                    "main.tex", "format", "Formatting", diff="--- main.tex\n+++ main.tex\n-a\n+b\n"
                )
            ],
        )
        self.assertNotIn("-a", render_terminal(report))
        self.assertIn("-a", render_terminal(report, show_diff=True))


if __name__ == "__main__":
    unittest.main()
