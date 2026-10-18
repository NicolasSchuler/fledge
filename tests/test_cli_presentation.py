"""How the terminal and compact reports present findings and results."""

from __future__ import annotations

import unittest

from latexprep.compact_report import render_compact
from latexprep.finding_view import RenderOptions
from latexprep.findings import Finding
from latexprep.report import Change, ChangeKind, Outcome, Report
from latexprep.terminal_report import render_terminal


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


if __name__ == "__main__":
    unittest.main()
