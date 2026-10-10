"""Exercise Click commands and the human/agent reports against source fixtures."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from latexprep.cli import USAGE_EXIT_CODE, cli
from latexprep.models import Change, Finding, Report
from latexprep.presentation import render_compact, render_terminal
from latexprep.rules import RULES
from latexprep.runtime import BuildResult


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
        self.assertEqual(self.runner.invoke(cli, ["rule", "UNKNOWN"]).exit_code, USAGE_EXIT_CODE)
        self.assertEqual(self.runner.invoke(cli, ["prepare", "--help"]).exit_code, 0)
        self.assertEqual(self.runner.invoke(cli, ["inspect"]).exit_code, USAGE_EXIT_CODE)

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

    def test_unexpected_internal_error_still_produces_a_json_error_report(self):
        with patch("latexprep.cli.run_job", new=AsyncMock(side_effect=KeyError("defect"))):
            result = self.runner.invoke(cli, ["inspect", str(self.source), "--json", "--quiet"])
        self.assertEqual(result.exit_code, 4, result.output)
        report = json.loads(result.stdout)
        self.assertEqual(report["outcome"], "error")
        self.assertEqual(report["findings"][0]["rule"], "execution.internal")
        self.assertIn("KeyError", report["findings"][0]["message"])

    def test_failed_html_report_is_reflected_in_saved_json(self):
        html = self.root / "missing-parent" / "report.html"
        saved = self.root / "report.json"
        with patch("latexprep.cli.validate_destination", side_effect=lambda _source, path: path):
            result = self.runner.invoke(
                cli,
                ["inspect", str(self.source), "--quiet", "--html-report", str(html)]
                + ["--report", str(saved)],
            )
        self.assertEqual(result.exit_code, 4, result.output)
        report = json.loads(saved.read_text())
        self.assertEqual(report["outcome"], "error")
        self.assertIn("report.write", {finding["rule"] for finding in report["findings"]})

    def test_conflicting_output_options_are_usage_errors(self):
        result = self.runner.invoke(
            cli, ["inspect", str(self.source), "--json", "--output-format", "compact"]
        )
        self.assertEqual(result.exit_code, USAGE_EXIT_CODE)
        self.assertIn("conflicts", result.output)


class RequestErrorTests(unittest.TestCase):
    """Rejected requests name the option or file at fault and a concrete next step."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "project"
        self.source.mkdir()
        (self.source / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\nText.\n\\end{document}\n"
        )
        self.runner = CliRunner()

    def request_error(self, *arguments: str) -> dict:
        result = self.runner.invoke(cli, [*arguments, "--json", "--quiet"])
        self.assertEqual(result.exit_code, 4, result.output)
        report = json.loads(result.stdout)
        self.assertEqual(report["outcome"], "error")
        self.assertEqual(report["scope"], "request validation")
        [finding] = [item for item in report["findings"] if item["rule"] == "execution.request"]
        self.assertNotIn("missing or ambiguous evidence", finding["suggestion"])
        return finding

    def test_existing_output_directory_names_the_option_to_change(self):
        existing = self.root / "existing"
        existing.mkdir()
        finding = self.request_error("prepare", str(self.source), "--output", str(existing))
        self.assertIn("Output already exists", finding["message"])
        self.assertIn("Choose a new --output path", finding["suggestion"])
        self.assertIn(str(existing), finding["suggestion"])

    def test_existing_report_file_names_the_report_option(self):
        existing = self.root / "report.json"
        existing.write_text("{}")
        result = self.runner.invoke(
            cli, ["inspect", str(self.source), "--report", str(existing), "--output-format", "json"]
        )
        self.assertEqual(result.exit_code, 4, result.output)
        [finding] = json.loads(result.stdout)["findings"]
        self.assertIn("Choose a new --report path", finding["suggestion"])

    def test_missing_input_suggests_checking_the_path(self):
        finding = self.request_error("inspect", str(self.root / "missing"))
        self.assertIn("Input does not exist", finding["message"])
        self.assertIn("LaTeX source folder or ZIP", finding["suggestion"])

    def test_prepare_without_output_suggests_output_or_dry_run(self):
        finding = self.request_error("prepare", str(self.source))
        self.assertIn("--output", finding["suggestion"])
        self.assertIn("--dry-run", finding["suggestion"])

    def test_unknown_main_lists_the_documents_found(self):
        finding = self.request_error("inspect", str(self.source), "--main", "nope.tex")
        self.assertIn("nope.tex", finding["message"])
        self.assertIn("--main", finding["suggestion"])
        self.assertIn("documents found in the input: main.tex", finding["suggestion"])

    def test_several_documents_suggest_choosing_one_with_main(self):
        (self.source / "other.tex").write_text((self.source / "main.tex").read_text())
        result = self.runner.invoke(cli, ["inspect", str(self.source), "--json", "--quiet"])
        report = json.loads(result.stdout)
        [finding] = [
            item for item in report["findings"] if item["rule"] == "project.main_selection"
        ]
        self.assertIn("Rerun with --main set to one of the candidate roots", finding["suggestion"])

    def test_invalid_command_line_value_names_the_flag(self):
        finding = self.request_error("check", str(self.source), "--max-pages", "0")
        self.assertIn("max_pages must be a positive integer", finding["message"])
        self.assertIn("--max-pages on the command line", finding["suggestion"])

    def test_invalid_configured_value_names_the_settings_file(self):
        config = self.source / ".fledge.toml"
        config.write_text("jobs = 100\n")
        finding = self.request_error("inspect", str(self.source))
        self.assertIn(f"jobs in {config}", finding["suggestion"])

    def test_unknown_setting_names_the_file_and_the_closest_valid_key(self):
        config = self.source / ".fledge.toml"
        config.write_text("max_page = 3\n")
        finding = self.request_error("inspect", str(self.source))
        self.assertIn(str(config), finding["message"])
        self.assertIn("max_page (did you mean max_pages?)", finding["message"])
        self.assertIn(f"Rename or remove it in {config}", finding["suggestion"])

    def test_unknown_setting_without_a_close_match_lists_valid_keys(self):
        config = self.source / ".fledge.toml"
        config.write_text("zzzz = 3\n")
        finding = self.request_error("inspect", str(self.source))
        self.assertIn("Valid top-level settings:", finding["suggestion"])
        self.assertIn("max_pages", finding["suggestion"])

    def test_terminal_report_is_headed_as_a_request_error(self):
        result = self.runner.invoke(cli, ["prepare", str(self.source), "--quiet"])
        self.assertEqual(result.exit_code, 4, result.output)
        self.assertTrue(result.stdout.startswith("Request error\n"), result.stdout)
        self.assertNotIn("source analysis", result.stdout)


class MissingToolchainTests(unittest.TestCase):
    def test_missing_tex_is_reported_once_with_installation_steps(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "project"
            source.mkdir()
            (source / "main.tex").write_text(
                "\\documentclass{article}\n\\begin{document}\nText.\n\\end{document}\n"
            )
            unavailable = BuildResult(
                False,
                findings=[
                    Finding(
                        "build.execution_unavailable",
                        "Required tool is unavailable: latexmk. Install it explicitly.",
                        "error",
                        "inconclusive",
                    )
                ],
            )
            with (
                patch("latexprep.core.build_project", new=AsyncMock(return_value=unavailable)),
                patch("latexprep.core.shutil.which", return_value=None),
            ):
                result = CliRunner().invoke(cli, ["check", str(source), "--json", "--quiet"])
        self.assertEqual(result.exit_code, 3, result.output)
        report = json.loads(result.stdout)
        rules = [finding["rule"] for finding in report["findings"]]
        self.assertNotIn("build.baseline", rules)
        # The missing toolchain already explains why no TeX version was recorded.
        self.assertNotIn("build.tex_distribution", rules)
        [finding] = [
            item for item in report["findings"] if item["rule"] == "build.execution_unavailable"
        ]
        self.assertIn("latexmk and pdflatex were not found", finding["message"])
        self.assertEqual(finding["details"]["missing_tools"], ["latexmk", "pdflatex"])
        self.assertIn("nicolasschuler.github.io/fledge/installation.html", finding["suggestion"])
        self.assertIn("fledge inspect", finding["suggestion"])
        self.assertIn("basictex" if sys.platform == "darwin" else "TeX Live", finding["suggestion"])


class HelpTests(unittest.TestCase):
    def help_text(self, *command: str) -> str:
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
            "--report",
        ):
            self.assertIn(option, advanced)
            self.assertNotIn(f"{option} ", common)

    def test_non_interactive_is_hidden_but_still_accepted(self):
        self.assertNotIn("--non-interactive", self.help_text("prepare"))
        with tempfile.TemporaryDirectory() as directory:
            result = CliRunner().invoke(
                cli, ["inspect", directory, "--non-interactive", "--quiet", "--json"]
            )
        self.assertNotEqual(result.exit_code, USAGE_EXIT_CODE, result.output)

    def test_commands_without_advanced_options_keep_one_options_section(self):
        text = self.help_text("rules")
        self.assertIn("Options:", text)
        self.assertNotIn("Advanced options:", text)

    def test_help_uses_checks_codes_and_findings_consistently(self):
        overview = self.help_text()
        self.assertIn("'fledge rules' lists the checks", overview)
        self.assertIn("Reports list findings", overview)
        self.assertIn("List every check with its stable code", overview)
        self.assertIn("Explain the check with code CODE", self.help_text("rule"))


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

    def test_evidence_excerpt_is_shown_for_unresolved_findings_only(self):
        report = Report(
            "check",
            "blocked",
            findings=[
                Finding(
                    "manuscript.forbidden_packages",
                    "Forbidden package declarations: 5.",
                    "error",
                    details={
                        "matches": [
                            {"path": "a.tex", "line": 3},
                            {"path": "b.tex", "line": 9},
                            {"path": "c.\x1b[31mtex", "line": 1},
                            {"path": "d.tex", "line": 2},
                            {"path": "e.tex"},
                        ],
                        "violations": [{"page": 9}],
                    },
                ),
                Finding(
                    "pdf.fonts",
                    "Inspected 9 font resources.",
                    "warning",
                    details={"nonembedded": ["Helv\nSecond", "Times"]},
                ),
                Finding(
                    "pdf.page_dimensions",
                    "Unexpected page size.",
                    "error",
                    details={"differing_pages": [2, 7]},
                ),
                Finding(
                    "pdf.text_overlap",
                    "Overlaps.",
                    "error",
                    details={
                        "violations": [{"page": 4, "bounds_pt": [1, 2, 3, 4]}],
                        "violation_count": 40,
                    },
                ),
                Finding(
                    "pdf.metadata",
                    "Metadata inventory",
                    "info",
                    "passed",
                    details={"matches": [{"path": "never-shown.tex"}]},
                ),
            ],
        )
        text = render_terminal(report, show_passed=True)
        self.assertIn("  Evidence: a.tex:3, b.tex:9, c.tex:1 (+2 more)", text)
        self.assertIn("  Evidence: Helv\\nSecond, Times", text)
        self.assertIn("  Evidence: page 2, page 7", text)
        self.assertIn("  Evidence: page 4 (+39 more)", text)
        self.assertNotIn("never-shown.tex", text)
        self.assertNotIn("\x1b", text)
        self.assertIn("(+2 more)\n  Next: ", text)
        compact = render_compact(report, show_passed=True)
        self.assertNotIn("never-shown.tex", compact)
        self.assertNotIn("\x1b", compact)
        self.assertEqual(len(compact.splitlines()), 1 + len(report.findings))
        line = next(row for row in compact.splitlines() if row.startswith("TEX"))
        self.assertIn(" | evidence: a.tex:3, b.tex:9, c.tex:1 (+2 more) | ", line)
        self.assertLess(line.index("evidence:"), line.index("next:"))
        self.assertIn("evidence: Helv\\nSecond, Times", compact)

    def test_evidence_excerpt_is_redacted_with_the_rest_of_the_report(self):
        report = Report(
            "check",
            "blocked",
            settings={"password": "SyntheticCredential_1234"},
            findings=[
                Finding(
                    "manuscript.forbidden_packages",
                    "Forbidden.",
                    "error",
                    details={"matches": [{"path": "SyntheticCredential_1234.tex", "line": 4}]},
                )
            ],
        )
        for rendered in (render_terminal(report), render_compact(report)):
            self.assertNotIn("SyntheticCredential_1234", rendered)
            self.assertIn("REDACTED", rendered)

    def test_scope_disclaimers_are_hidden_unless_passed_results_are_requested(self):
        report = Report(
            "check",
            "passed_with_advisories",
            findings=[
                Finding(
                    "online.metadata",
                    "Online checks were not requested.",
                    "info",
                    "skipped",
                ),
                Finding(
                    "build.bibliography_backend",
                    "No bibliography backend is used.",
                    "info",
                    "not_applicable",
                ),
                Finding("pdf.pages", "1 page", "info", "passed"),
                Finding("pdf.fonts", "Fonts unavailable", "error", "skipped"),
            ],
        )
        text = render_terminal(report)
        self.assertNotIn("Online checks were not requested", text)
        self.assertNotIn("No bibliography backend", text)
        self.assertIn("Fonts unavailable", text)
        self.assertIn("1 incomplete, 2 not applicable, 1 passed result", text)
        self.assertEqual(
            render_compact(report).splitlines()[0].split(" | ")[1],
            "1 error, 0 warnings, 1 incomplete, 2 not applicable, 1 passed result",
        )
        shown = render_terminal(report, show_passed=True)
        self.assertIn("Online checks were not requested", shown)
        self.assertIn("INFO / not checked", shown)
        self.assertIn("INFO / not applicable", shown)
        self.assertIn("No bibliography backend", render_compact(report, show_passed=True))
        only_notes = Report("check", "passed", findings=report.findings[:2])
        quiet = render_terminal(only_notes)
        self.assertIn("No issues found", quiet)
        self.assertNotIn("fledge rule", quiet)
        self.assertEqual(len(render_compact(only_notes).splitlines()), 1)
        self.assertIn("0 incomplete, 2 not applicable", render_compact(only_notes))

    def test_many_changes_are_summarized_by_kind_unless_diffs_are_requested(self):
        changes = (
            [Change(f"extra{i}.txt", "exclude", "Unused", diff="-x\n+y\n") for i in range(37)]
            + [Change(f"img{i}.png", "move", "Flatten") for i in range(12)]
            + [Change("main.tex", "rewrite", "Paths")]
        )
        report = Report("prepare", "planned", changes=changes)
        text = render_terminal(report)
        self.assertIn(
            "50 proposed/applied changes (original input unchanged): "
            "exclude: 37, move: 12, rewrite: 1",
            text,
        )
        self.assertNotIn("extra0.txt", text)
        self.assertIn("use --json", text)
        full = render_terminal(report, show_diff=True)
        self.assertIn("extra36.txt", full)
        self.assertIn("img11.png", full)
        self.assertIn("-x", full)
        small = render_terminal(Report("prepare", "planned", changes=changes[:20]))
        self.assertIn("extra19.txt", small)
        self.assertIn("exclude: 20", small)
        self.assertNotIn("use --json", small)
        self.assertNotIn(
            "extra19.txt", render_terminal(Report("p", "planned", changes=changes[:21]))
        )
        compact = render_compact(report)
        self.assertIn("changes: 50 (exclude: 37, move: 12, rewrite: 1; use --json", compact)
        self.assertEqual(len(compact.splitlines()), 2)

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
