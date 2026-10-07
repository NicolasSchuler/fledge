"""Exercise Click commands and the human/agent reports against source fixtures."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from latexprep.cli import USAGE_EXIT_CODE, cli
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
