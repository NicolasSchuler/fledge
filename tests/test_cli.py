"""Exercise Click commands and the human/agent reports against source fixtures."""

from __future__ import annotations

import asyncio
import io
import json
import os
import sys
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from latexprep.cli import USAGE_EXIT_CODE, _settings_suggestion, cli, main
from latexprep.finding_view import RenderOptions
from latexprep.findings import Finding
from latexprep.latexmk_build import BuildResult
from latexprep.redaction import scrub_local_paths
from latexprep.report import Change, ChangeKind, Outcome, Report
from latexprep.rules import RULES
from latexprep.terminal_report import render_compact, render_terminal


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
                self.assertTrue(finding["next_step"])

    def test_catalogue_and_explanation_are_available_without_a_project(self):
        result = self.runner.invoke(cli, ["rules", "--json"])
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(
            {row["code"] for row in json.loads(result.stdout)}, {rule.code for rule in RULES}
        )
        result = self.runner.invoke(cli, ["rule", "TEX001", "--json"])
        self.assertEqual(result.exit_code, 0)
        item = json.loads(result.stdout)
        self.assertEqual(item["name"], "source.edit_marker")
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
        config.write_text("apply_formatting = true\nnormalize_doi = true\ncleanup = true\n")
        for flags, expected in (
            ([], True),
            (["--no-format", "--no-normalize-doi", "--no-cleanup"], False),
        ):
            with self.subTest(flags=flags):
                with patch(
                    "latexprep.cli.run_job",
                    new=AsyncMock(return_value=Report("prepare", outcome=Outcome.PLANNED)),
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
                self.assertEqual(settings.apply_formatting, expected)
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
        with patch(
            "latexprep.cli.validate_destination", side_effect=lambda _source, path, _option: path
        ):
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
        self.assertNotIn("missing or ambiguous evidence", finding["next_step"])
        return finding

    def test_existing_output_directory_names_the_option_to_change(self):
        existing = self.root / "existing"
        existing.mkdir()
        finding = self.request_error("prepare", str(self.source), "--output", str(existing))
        self.assertIn("Output already exists", finding["message"])
        self.assertIn("Choose a new --output path", finding["next_step"])
        # Reports show the home and temporary directories generically.
        self.assertIn(scrub_local_paths(str(existing)), finding["next_step"])

    def test_existing_report_file_names_the_report_option(self):
        existing = self.root / "report.json"
        existing.write_text("{}")
        result = self.runner.invoke(
            cli, ["inspect", str(self.source), "--report", str(existing), "--output-format", "json"]
        )
        self.assertEqual(result.exit_code, 4, result.output)
        [finding] = json.loads(result.stdout)["findings"]
        self.assertIn("Choose a new --report path", finding["next_step"])

    def test_missing_input_suggests_checking_the_path(self):
        finding = self.request_error("inspect", str(self.root / "missing"))
        self.assertIn("Input does not exist", finding["message"])
        self.assertIn("LaTeX source folder or ZIP", finding["next_step"])

    def test_prepare_without_output_suggests_output_or_dry_run(self):
        finding = self.request_error("prepare", str(self.source))
        self.assertIn("--output", finding["next_step"])
        self.assertIn("--dry-run", finding["next_step"])

    def test_unknown_main_lists_the_documents_found(self):
        finding = self.request_error("inspect", str(self.source), "--main", "nope.tex")
        self.assertIn("nope.tex", finding["message"])
        self.assertIn("--main", finding["next_step"])
        self.assertIn("documents found in the input: main.tex", finding["next_step"])

    def test_several_documents_suggest_choosing_one_with_main(self):
        (self.source / "other.tex").write_text((self.source / "main.tex").read_text())
        result = self.runner.invoke(cli, ["inspect", str(self.source), "--json", "--quiet"])
        report = json.loads(result.stdout)
        [finding] = [
            item for item in report["findings"] if item["rule"] == "project.main_selection"
        ]
        self.assertIn("Rerun with --main set to one of the candidate roots", finding["next_step"])

    def test_invalid_command_line_value_names_the_flag(self):
        finding = self.request_error("check", str(self.source), "--max-pages", "0")
        self.assertIn("max_pages must be a positive integer", finding["message"])
        self.assertIn("--max-pages on the command line", finding["next_step"])

    def test_invalid_configured_value_names_the_settings_file(self):
        config = self.source / ".fledge.toml"
        config.write_text("jobs = 100\n")
        finding = self.request_error("inspect", str(self.source))
        self.assertIn(f"jobs in {scrub_local_paths(str(config))}", finding["next_step"])

    def test_a_table_value_names_the_table_in_the_settings_file(self):
        config = self.source / ".fledge.toml"
        config.write_text("[online_checks]\nonline_timeout_seconds = 0\n")
        finding = self.request_error("inspect", str(self.source))
        self.assertIn("online_timeout_seconds", finding["message"])
        self.assertIn(f"online_checks in {scrub_local_paths(str(config))}", finding["next_step"])

    def test_every_setting_at_fault_is_named_in_the_order_of_the_message(self):
        config = self.source / ".fledge.toml"
        config.write_text("build_jobs = 100\n")
        finding = self.request_error("inspect", str(self.source))
        shown = scrub_local_paths(str(config))
        self.assertIn(
            f"Change build_jobs in {shown}; render_jobs in {shown}, then rerun",
            finding["next_step"],
        )

    def test_a_setting_name_in_a_file_path_is_not_taken_for_a_setting_at_fault(self):
        project = self.root / "jobs"
        project.mkdir()
        (project / "main.tex").write_text((self.source / "main.tex").read_text())
        config = project / ".fledge.toml"
        config.write_text("jobs = [")
        finding = self.request_error("inspect", str(project))
        self.assertIn("Cannot read settings", finding["message"])
        self.assertEqual(
            finding["next_step"],
            f"Correct the settings in {scrub_local_paths(str(config))}, then rerun the command.",
        )

    def test_unknown_setting_names_the_file_and_the_closest_valid_key(self):
        config = self.source / ".fledge.toml"
        config.write_text("max_page = 3\n")
        finding = self.request_error("inspect", str(self.source))
        shown = scrub_local_paths(str(config))
        self.assertIn(shown, finding["message"])
        self.assertIn("max_page (did you mean max_pages?)", finding["message"])
        self.assertIn(f"Rename or remove it in {shown}", finding["next_step"])

    def test_unknown_setting_without_a_close_match_lists_valid_keys(self):
        config = self.source / ".fledge.toml"
        config.write_text("zzzz = 3\n")
        finding = self.request_error("inspect", str(self.source))
        self.assertIn("Valid top-level settings:", finding["next_step"])
        self.assertIn("max_pages", finding["next_step"])

    def test_terminal_report_is_headed_as_a_request_error(self):
        result = self.runner.invoke(cli, ["prepare", str(self.source), "--quiet"])
        self.assertEqual(result.exit_code, 4, result.output)
        self.assertTrue(result.stdout.startswith("Request error\n"), result.stdout)
        self.assertNotIn("source analysis", result.stdout)


class SettingsSuggestionTests(unittest.TestCase):
    def test_each_named_setting_says_where_it_was_supplied(self):
        names = ("jobs", "apply_formatting", "engine", "layout")
        overrides = {"jobs": 0, "apply_formatting": True}
        config = Path("/project/fledge.toml")
        self.assertEqual(
            _settings_suggestion(names, overrides, config, "journal"),
            "Change --jobs on the command line; --format on the command line; "
            "engine in /project/fledge.toml or the journal preset; "
            "layout in /project/fledge.toml or the journal preset, then rerun the command.",
        )
        self.assertEqual(
            _settings_suggestion(("engine",), {}, config, None),
            "Change engine in /project/fledge.toml, then rerun the command.",
        )
        self.assertEqual(
            _settings_suggestion(("engine",), {}, None, "journal"),
            "Change engine in the journal preset, then rerun the command.",
        )

    def test_no_named_setting_gives_the_general_advice(self):
        self.assertEqual(
            _settings_suggestion((), {}, Path("/project/fledge.toml"), None),
            "Correct the settings in /project/fledge.toml, then rerun the command.",
        )
        self.assertEqual(
            _settings_suggestion((), {}, None, None),
            "Correct the options named above, then rerun the command.",
        )


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
                        severity="error",
                        status="inconclusive",
                        details={"tool": "latexmk"},
                    )
                ],
            )
            with (
                patch(
                    "latexprep.document_tasks.build_project",
                    new=AsyncMock(return_value=unavailable),
                ),
                patch("latexprep.toolchain_help.shutil.which", return_value=None),
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
        self.assertIn("nicolasschuler.github.io/fledge/installation.html", finding["next_step"])
        self.assertIn("fledge inspect", finding["next_step"])
        self.assertIn("basictex" if sys.platform == "darwin" else "TeX Live", finding["next_step"])


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
            outcome=Outcome.BLOCKED,
            findings=[
                Finding(
                    "source.edit_marker", "TODO: [bold]literal[/bold]", path="main.tex", line=7
                ),
                Finding(
                    "pdf.font_embedding",
                    "Font inventory unavailable",
                    severity="error",
                    status="inconclusive",
                ),
                Finding(
                    "manuscript.abstract_words",
                    "120 words; maximum 100",
                    severity="error",
                    path="intro.tex",
                    line=3,
                ),
                Finding("pdf.pages", "1 page", severity="info", status="passed"),
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
        self.assertIn(
            "pdf / pages", render_terminal(report, options=RenderOptions(show_passed=True))
        )
        self.assertNotIn("\x1b", text)

    def test_compact_preserves_complete_messages_on_one_line_and_no_terminal_escapes(self):
        message = "Unexpected [red]literal[/red]\nsecond line " + "long " * 100 + "THE END"
        report = Report(
            "inspect",
            outcome=Outcome.BLOCKED,
            findings=[
                Finding(
                    "source.missing_dependency",
                    "\x1b[31m" + message,
                    severity="error",
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
            outcome=Outcome.BLOCKED,
            findings=[
                Finding(
                    "manuscript.forbidden_packages",
                    "Forbidden package declarations: 5.",
                    severity="error",
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
                    severity="warning",
                    details={"nonembedded": ["Helv\nSecond", "Times"]},
                ),
                Finding(
                    "pdf.page_dimensions",
                    "Unexpected page size.",
                    severity="error",
                    details={"differing_pages": [2, 7]},
                ),
                Finding(
                    "pdf.text_overlap",
                    "Overlaps.",
                    severity="error",
                    details={
                        "violations": [{"page": 4, "bounds_pt": [1, 2, 3, 4]}],
                        "violation_count": 40,
                    },
                ),
                Finding(
                    "pdf.metadata",
                    "Metadata inventory",
                    severity="info",
                    status="passed",
                    details={"matches": [{"path": "never-shown.tex"}]},
                ),
            ],
        )
        text = render_terminal(report, options=RenderOptions(show_passed=True))
        self.assertIn("  Evidence: a.tex:3, b.tex:9, c.tex:1 (+2 more)", text)
        self.assertIn("  Evidence: Helv\\nSecond, Times", text)
        self.assertIn("  Evidence: page 2, page 7", text)
        self.assertIn("  Evidence: page 4 (+39 more)", text)
        self.assertNotIn("never-shown.tex", text)
        self.assertNotIn("\x1b", text)
        self.assertIn("(+2 more)\n  Next: ", text)
        compact = render_compact(report, options=RenderOptions(show_passed=True))
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
            outcome=Outcome.BLOCKED,
            settings={"password": "SyntheticCredential_1234"},
            findings=[
                Finding(
                    "manuscript.forbidden_packages",
                    "Forbidden.",
                    severity="error",
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
            outcome=Outcome.PASSED_WITH_ADVISORIES,
            findings=[
                Finding(
                    "online.metadata",
                    "Online checks were not requested.",
                    severity="info",
                    status="skipped",
                ),
                Finding(
                    "build.bibliography_backend",
                    "No bibliography backend is used.",
                    severity="info",
                    status="not_applicable",
                ),
                Finding("pdf.pages", "1 page", severity="info", status="passed"),
                Finding("pdf.fonts", "Fonts unavailable", severity="error", status="skipped"),
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
        shown = render_terminal(report, options=RenderOptions(show_passed=True))
        self.assertIn("Online checks were not requested", shown)
        self.assertIn("INFO / not checked", shown)
        self.assertIn("INFO / not applicable", shown)
        self.assertIn(
            "No bibliography backend",
            render_compact(report, options=RenderOptions(show_passed=True)),
        )
        only_notes = Report("check", outcome=Outcome.PASSED, findings=report.findings[:2])
        quiet = render_terminal(only_notes)
        self.assertIn("No issues found", quiet)
        self.assertNotIn("fledge rule", quiet)
        self.assertEqual(len(render_compact(only_notes).splitlines()), 1)
        self.assertIn("0 incomplete, 2 not applicable", render_compact(only_notes))

    def test_many_changes_are_summarized_by_kind_unless_diffs_are_requested(self):
        changes = (
            [
                Change(f"extra{i}.txt", kind=ChangeKind.EXCLUDE, reason="Unused", diff="-x\n+y\n")
                for i in range(37)
            ]
            + [Change(f"img{i}.png", kind=ChangeKind.MOVE, reason="Flatten") for i in range(12)]
            + [Change("main.tex", kind=ChangeKind.REWRITE, reason="Paths")]
        )
        report = Report("prepare", outcome=Outcome.PLANNED, changes=changes)
        text = render_terminal(report)
        self.assertIn(
            "50 proposed/applied changes (original input unchanged): "
            "exclude: 37, move: 12, rewrite: 1",
            text,
        )
        self.assertNotIn("extra0.txt", text)
        self.assertIn("use --json", text)
        full = render_terminal(report, options=RenderOptions(show_diff=True))
        self.assertIn("extra36.txt", full)
        self.assertIn("img11.png", full)
        self.assertIn("-x", full)
        small = render_terminal(Report("prepare", outcome=Outcome.PLANNED, changes=changes[:20]))
        self.assertIn("extra19.txt", small)
        self.assertIn("exclude: 20", small)
        self.assertNotIn("use --json", small)
        self.assertNotIn(
            "extra19.txt",
            render_terminal(Report("p", outcome=Outcome.PLANNED, changes=changes[:21])),
        )
        compact = render_compact(report)
        self.assertIn("changes: 50 (exclude: 37, move: 12, rewrite: 1; use --json", compact)
        self.assertEqual(len(compact.splitlines()), 2)

    def test_formatting_diff_is_shown_only_when_requested(self):
        report = Report(
            "fmt",
            outcome=Outcome.PASSED_WITH_ADVISORIES,
            changes=[
                Change(
                    "main.tex",
                    kind=ChangeKind.FORMAT,
                    reason="Formatting",
                    diff="--- main.tex\n+++ main.tex\n-a\n+b\n",
                )
            ],
        )
        self.assertNotIn("-a", render_terminal(report))
        self.assertIn("-a", render_terminal(report, options=RenderOptions(show_diff=True)))


class UnexpectedFailureTests(unittest.TestCase):
    """A defect ends with the error exit code, never with a pass or an advisory code."""

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "project"
        self.source.mkdir()
        (self.source / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\nTODO finish this.\n\\end{document}\n"
        )

    def run_main(self, *arguments: str) -> tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = main(list(arguments))
        return code, out.getvalue(), err.getvalue()

    def inspect_json(self, *extra: str) -> tuple[int, dict, str]:
        code, out, err = self.run_main("inspect", str(self.source), "--json", "--quiet", *extra)
        return code, json.loads(out), err

    def test_crash_in_the_job_exits_4_and_keeps_the_error_type_and_frames(self):
        with patch("latexprep.cli.run_job", new=AsyncMock(side_effect=RuntimeError("defect"))):
            code, report, err = self.inspect_json()
        self.assertEqual(code, 4)
        self.assertEqual(report["outcome"], "error")
        self.assertEqual(err, "")
        [finding] = [item for item in report["findings"] if item["rule"] == "execution.internal"]
        self.assertEqual(finding["details"]["error_type"], "RuntimeError")
        frames = finding["details"]["frames"]
        self.assertTrue(frames)
        # file name, line and function; never a directory.
        for frame in frames:
            self.assertRegex(frame, r"^[\w.-]+\.py:\d+ in \S+$")
        self.assertIn("--debug", finding["next_step"])

    def test_internal_error_is_not_labelled_as_request_validation(self):
        with patch("latexprep.cli.run_job", new=AsyncMock(side_effect=KeyError("defect"))):
            _, report, _ = self.inspect_json()
        self.assertEqual(report["scope"], "run did not finish")
        self.assertNotIn("execution.request", {item["rule"] for item in report["findings"]})

    def test_value_error_from_the_job_is_a_defect_not_a_request_error(self):
        with patch("latexprep.cli.run_job", new=AsyncMock(side_effect=ValueError("bad value"))):
            code, report, _ = self.inspect_json()
        self.assertEqual(code, 4)
        rules = {item["rule"] for item in report["findings"]}
        self.assertIn("execution.internal", rules)
        self.assertNotIn("execution.request", rules)

    def test_genuine_input_output_errors_stay_request_errors(self):
        with patch("latexprep.cli.run_job", new=AsyncMock(side_effect=OSError("disk full"))):
            code, report, _ = self.inspect_json()
        self.assertEqual(code, 4)
        self.assertEqual(report["scope"], "request validation")
        self.assertIn("execution.request", {item["rule"] for item in report["findings"]})

    def test_cancellation_is_consistent_for_interrupt_and_asyncio_cancellation(self):
        for failure in (KeyboardInterrupt(), asyncio.CancelledError()):
            with self.subTest(failure=type(failure).__name__):
                with patch("latexprep.cli.run_job", new=AsyncMock(side_effect=failure)):
                    code, report, _ = self.inspect_json()
                self.assertEqual(code, 130)
                self.assertEqual(report["outcome"], "cancelled")
                self.assertEqual(report["scope"], "run did not finish")

    def test_crash_while_writing_the_report_exits_4_with_one_plain_line(self):
        with patch("latexprep.cli.write_report", side_effect=RuntimeError("renderer defect")):
            code, out, err = self.run_main("inspect", str(self.source), "--quiet")
        self.assertEqual(code, 4)
        self.assertEqual(out, "")
        self.assertEqual(len(err.strip().splitlines()), 1, err)
        self.assertIn("Internal error (RuntimeError) while writing the report", err)
        self.assertNotIn("Traceback", err)
        self.assertNotIn("renderer defect", err)

    def test_crash_while_saving_an_optional_report_file_is_recorded_in_the_report(self):
        html = self.root / "report.html"
        with patch("latexprep.cli.render_html", side_effect=RuntimeError("html defect")):
            code, report, _ = self.inspect_json("--html-report", str(html))
        self.assertEqual(code, 4)
        self.assertEqual(report["outcome"], "error")
        self.assertIn("execution.internal", {item["rule"] for item in report["findings"]})

    def test_debug_flag_and_environment_variable_print_the_traceback_to_stderr(self):
        for environment, flags in (({"FLEDGE_DEBUG": "1"}, []), ({}, ["--debug"])):
            with self.subTest(environment=environment, flags=flags):
                with (
                    patch.dict(os.environ, environment),
                    patch(
                        "latexprep.cli.write_report", side_effect=RuntimeError("renderer defect")
                    ),
                ):
                    code, _, err = self.run_main("inspect", str(self.source), "--quiet", *flags)
                self.assertEqual(code, 4)
                self.assertIn("Traceback", err)
                self.assertIn("renderer defect", err)

    def test_debug_traceback_keeps_stdout_a_clean_report(self):
        with patch("latexprep.cli.run_job", new=AsyncMock(side_effect=RuntimeError("defect"))):
            code, report, err = self.inspect_json("--debug")
        self.assertEqual(code, 4)
        self.assertEqual(report["outcome"], "error")
        self.assertIn("Traceback", err)

    def test_commands_without_their_own_guard_exit_4_instead_of_status_1(self):
        (self.root / "empty").mkdir()
        cases = (
            ("rules", "latexprep.cli.print_rule_table", ["rules"]),
            (
                "init",
                "latexprep.init_command.parse_settings_text",
                ["init", str(self.root / "empty")],
            ),
            (
                "submission-text",
                "latexprep.submission_text_command.read_submission_text",
                ["submission-text", str(self.source)],
            ),
        )
        for name, target, arguments in cases:
            with self.subTest(command=name):
                with patch(target, side_effect=RuntimeError("defect")):
                    code, _, err = self.run_main(*arguments)
                self.assertEqual(code, 4)
                self.assertIn("Internal error (RuntimeError)", err)
                self.assertNotIn("Traceback", err)

    def test_doctor_treats_value_error_as_a_defect_and_cancellation_like_other_commands(self):
        for failure, expected_code, expected_rule in (
            (ValueError("bad"), 4, "execution.internal"),
            (asyncio.CancelledError(), 130, "execution.cancelled"),
        ):
            with self.subTest(failure=type(failure).__name__):
                with patch("latexprep.doctor_command.run_doctor", side_effect=failure):
                    code, out, _ = self.run_main("doctor", "--json", "--quiet")
                self.assertEqual(code, expected_code)
                report = json.loads(out)
                self.assertIn(expected_rule, {item["rule"] for item in report["findings"]})
                self.assertNotEqual(report["scope"], "request validation")

    def test_closed_output_pipe_keeps_the_verdict_and_prints_nothing(self):
        with patch("latexprep.cli.write_report", side_effect=BrokenPipeError):
            code, _, err = self.run_main("inspect", str(self.source), "--quiet")
        # The TODO marker is an advisory: exit code 1, not an error.
        self.assertEqual(code, 1)
        self.assertEqual(err, "")
        with patch("latexprep.cli.print_rule_table", side_effect=BrokenPipeError):
            code, _, err = self.run_main("rules")
        self.assertEqual((code, err), (0, ""))

    def test_missing_settings_file_says_it_does_not_exist_and_how_to_fix_it(self):
        missing = self.root / "missing.toml"
        code, report, _ = self.inspect_json("--config", str(missing))
        self.assertEqual(code, 4)
        [finding] = [item for item in report["findings"] if item["rule"] == "execution.request"]
        self.assertIn("does not exist", finding["message"])
        self.assertIn("missing.toml", finding["message"])
        self.assertNotIn("Errno", finding["message"])
        self.assertIn("--config", finding["next_step"])

    def test_report_write_failures_name_the_option_and_a_next_step(self):
        for option, directory in (("--html-report", "html"), ("--diagnostics", "bundle")):
            with self.subTest(option=option):
                destination = self.root / "no-such-folder" / directory
                with patch(
                    "latexprep.cli.validate_destination",
                    side_effect=lambda _source, path, _option: path,
                ):
                    code, report, _ = self.inspect_json(option, str(destination))
                self.assertEqual(code, 4)
                [finding] = [item for item in report["findings"] if item["rule"] == "report.write"]
                self.assertIn(option, finding["message"])
                self.assertNotIn("Errno", finding["message"])
                self.assertIn(option, finding["next_step"])


if __name__ == "__main__":
    unittest.main()
