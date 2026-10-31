"""The help text of the commands."""

from __future__ import annotations

import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

import click
from click.testing import CliRunner

from latexprep.cli import (
    ADVANCED_OPTIONS,
    USAGE_EXIT_CODE,
    CommandOptions,
    cli,
)
from latexprep.config import Settings


class HelpTests(unittest.TestCase):
    """Tests of the ``--help`` text of the commands: option sections, defaults and wording."""

    def help_text(self, *command: str) -> str:
        """Return the ``--help`` output of ``command`` at width 200, asserting that it succeeds."""
        result = CliRunner().invoke(cli, [*command, "--help"], terminal_width=200)
        self.assertEqual(result.exit_code, 0, result.output)
        return result.output

    def test_prepare_help_separates_common_and_advanced_options(self):
        text = self.help_text("prepare")
        common, advanced = text.split("Advanced options:")
        self.assertIn("Common options:", common)
        for option in ("--output", "--main", "--select", "--ignore", "--dry-run", "--json"):
            self.assertIn(option, common)
        for option in (
            "--jobs",
            "--build-jobs",
            "--render-jobs",
            "--job-timeout-seconds",
            "--timeout-seconds",
            "--preview-output",
            "--diagnostics",
            "--html-report",
            "--json-report",
            "--report",
        ):
            self.assertIn(option, advanced)
            self.assertNotIn(f"{option} ", common)

    def test_pdf_check_offers_only_the_options_that_apply_to_a_pdf_that_is_not_built(self):
        text = self.help_text("pdf", "check")
        for option in ("--engine", "--bibliography-backend"):
            self.assertNotIn(option, text)
        self.assertNotIn("build", text.split("--reference-pdf")[1].split("--max-pages")[0])
        self.assertIn("--reference-pdf", text)
        # check and prepare still build the PDF, so they keep both options and their wording.
        for command in ("check", "prepare"):
            built = self.help_text(command)
            self.assertIn("--engine", built)
            self.assertIn("PDF that the build must match", built)
        result = CliRunner().invoke(cli, ["pdf", "check", "paper.pdf", "--engine", "xelatex"])
        self.assertEqual(result.exit_code, USAGE_EXIT_CODE, result.output)

    def test_formatting_check_flag_stays_accepted_as_documented_no_op(self):
        with tempfile.TemporaryDirectory() as directory:
            result = CliRunner().invoke(cli, ["fmt", directory, "--check", "--quiet", "--json"])
        self.assertNotEqual(result.exit_code, USAGE_EXIT_CODE, result.output)

    def test_removed_non_interactive_flag_is_a_usage_error(self):
        result = CliRunner().invoke(cli, ["inspect", ".", "--non-interactive"])
        self.assertEqual(result.exit_code, USAGE_EXIT_CODE, result.output)

    def test_every_parameter_of_a_job_command_is_a_command_options_field(self):
        fields_ = {item.name for item in dataclasses.fields(CommandOptions)}
        seen = set()
        for path in (
            ("inspect",),
            ("check",),
            ("prepare",),
            ("bib", "check"),
            ("fmt",),
            ("pdf", "check"),
        ):
            command: click.Command = cli
            for name in path:
                assert isinstance(command, click.Group)
                command = command.commands[name]
            for parameter in command.params:
                if parameter.expose_value:
                    self.assertIn(parameter.name, fields_, (path, parameter.name))
                    seen.add(parameter.name)
        self.assertEqual(ADVANCED_OPTIONS - seen, set(), "ADVANCED_OPTIONS names a missing option")

    def test_help_quotes_defaults_from_the_settings(self):
        settings = Settings()
        text = self.help_text("prepare")
        for expected in (
            f"(default {settings.jobs})",
            f"(default {settings.build_jobs})",
            f"(default {settings.render_jobs})",
            f"(default {settings.timeout_seconds})",
            f"(default {settings.job_timeout_seconds})",
            f"(default {settings.engine})",
            f"default {settings.layout}.",
        ):
            self.assertIn(expected, text)

    def test_main_option_has_one_help_everywhere(self):
        helps = set()
        for path in (("inspect",), ("check",), ("prepare",), ("bib", "check")):
            command: click.Command = cli
            for name in path:
                assert isinstance(command, click.Group)
                command = command.commands[name]
            [main_parameter] = [item for item in command.params if item.name == "main"]
            helps.add((main_parameter.help, main_parameter.metavar))
        submission = cli.commands["submission-text"]
        [main_parameter] = [item for item in submission.params if item.name == "main"]
        helps.add((main_parameter.help, main_parameter.metavar))
        self.assertEqual(len(helps), 1, helps)

    def test_quiet_option_has_one_help_everywhere(self):
        helps = set()
        for command in (cli.commands["inspect"], cli.commands["doctor"]):
            [quiet] = [item for item in command.params if item.name == "quiet"]
            helps.add(quiet.help)
        self.assertEqual(len(helps), 1, helps)

    def test_options_without_obvious_meaning_have_help(self):
        for command, option in (("doctor", "--engine"), ("submission-text", "--field")):
            with self.subTest(option=option):
                self.assertRegex(self.help_text(command), rf"{option}[^\n]*\n?\s+\S")
                [parameter] = [item for item in cli.commands[command].params if option in item.opts]
                self.assertTrue(parameter.help)

    def test_command_list_is_not_truncated(self):
        overview = CliRunner().invoke(cli, ["--help"], terminal_width=80).output
        self.assertNotIn("...", overview.split("Commands:")[1])
        self.assertIn("Build the PDF and check the sources and the PDF.", overview)

    def test_help_avoids_internal_jargon(self):
        everything = " ".join(
            self.help_text(*path)
            for path in (("check",), ("prepare",), ("pdf", "check"), ("doctor",), ("rule",))
        )
        for jargon in ("CPU slots", "page-pair", "isolated build", "in isolation"):
            self.assertNotIn(jargon, everything)

    def test_json_report_and_its_former_name_both_save_the_report(self):
        for flag in ("--json-report", "--report"):
            with self.subTest(flag=flag), tempfile.TemporaryDirectory() as directory:
                saved = Path(directory) / "report.json"
                project = Path(directory) / "project"
                project.mkdir()
                (project / "main.tex").write_text("\\documentclass{article}\n")
                result = CliRunner().invoke(
                    cli, ["inspect", str(project), flag, str(saved), "--quiet", "--isolated"]
                )
                self.assertNotEqual(result.exit_code, USAGE_EXIT_CODE, result.output)
                self.assertEqual(json.loads(saved.read_text())["command"], "inspect")

    def test_rule_explanation_leaves_test_ids_to_the_json_form(self):
        result = CliRunner().invoke(cli, ["rule", "TEX001"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("tests.", result.output)
        self.assertNotIn("Behavioral tests", result.output)
        self.assertNotIn("tests.", CliRunner().invoke(cli, ["rules"]).output)

    def test_commands_without_advanced_options_keep_one_options_section(self):
        text = self.help_text("rules")
        self.assertIn("Options:", text)
        self.assertNotIn("Advanced options:", text)

    def test_help_uses_checks_codes_and_findings_consistently(self):
        overview = self.help_text()
        self.assertIn("'fledge rules' lists the checks", overview)
        self.assertIn("Reports list findings", overview)
        self.assertIn("List every check with its code and title", overview)
        self.assertIn("Explain the check with code CODE", self.help_text("rule"))


if __name__ == "__main__":
    unittest.main()
