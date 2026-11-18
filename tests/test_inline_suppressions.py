"""Suggestions silenced in the sources by ``% fledge: ignore CODE`` comments."""

from __future__ import annotations

import unittest

from latexprep.check_selection import CheckSelection
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.document_tasks import TaskName
from latexprep.findings import Finding
from latexprep.inline_suppressions import silenced_codes, split_silenced_suggestions
from latexprep.job_context import JobRequest
from latexprep.report import Report
from latexprep.scheduler import TaskResult, TaskStatus
from latexprep.task_reporting import TaskReporter
from tests.support import AsyncProjectTestCase, ProjectTestCase, controlled_backends


def suggestion(line: int, path: str = "main.tex", rule: str = "source.edit_marker") -> Finding:
    """Return a suggestion of ``rule`` (TEX001 by default) at ``path`` and ``line``."""
    return Finding(rule, "Consider rewording.", severity="suggestion", path=path, line=line)


class SilencedCodeTests(unittest.TestCase):
    """Which codes a comment silences: on the line, or alone on the line before."""

    def test_a_comment_on_the_line_or_alone_on_the_line_before_silences(self):
        lines = [
            "% fledge: ignore TEX001",
            "Some text.",
            "Text. % fledge: ignore TEX002, TEX003 TEX004",
            "Text. % fledge: ignore TEX005",
            "Next line.",
            r"A 50\% share % fledge: ignore TEX006",
            r"A 50\% fledge: ignore TEX007",
        ]
        cases = (
            (2, {"TEX001"}),
            (3, {"TEX002", "TEX003", "TEX004"}),
            # A comment after text applies to its own line only, not to the next one.
            (5, set()),
            (6, {"TEX006"}),
            # An escaped percent sign is text, not the start of a comment.
            (7, set()),
            (99, set()),
        )
        for line, expected in cases:
            with self.subTest(line=line):
                self.assertEqual(silenced_codes(lines, line), expected)


class SplitTests(ProjectTestCase):
    """Only suggestions with a code, a readable file and a line can be silenced."""

    def test_a_silenced_suggestion_leaves_the_findings_and_is_returned(self):
        self.write("main.tex", "Text.\n% fledge: ignore TEX001\nMore text.\nUnrelated.\n")
        warning = Finding("source.edit_marker", "TODO", path="main.tex", line=3)
        kept, silenced = split_silenced_suggestions(
            [suggestion(3), suggestion(4), warning], self.root
        )
        self.assertEqual(kept, [suggestion(4), warning])
        self.assertEqual(
            [item.to_report() for item in silenced],
            [{"code": "TEX001", "path": "main.tex", "line": 3}],
        )

    def test_a_suggestion_that_cannot_be_matched_to_its_line_is_kept(self):
        self.write("main.tex", "% fledge: ignore TEX001\nText.\n")
        findings = [
            suggestion(2, path="missing.tex"),
            suggestion(2, path="../outside.tex"),
            Finding("source.edit_marker", "No line.", severity="suggestion", path="main.tex"),
            # An operational result has no code to name.
            Finding("pdf.pages", "1 page", severity="suggestion", path="main.tex", line=2),
        ]
        kept, silenced = split_silenced_suggestions(findings, self.root)
        self.assertEqual(kept, findings)
        self.assertEqual(silenced, [])


class TaskReporterTests(ProjectTestCase):
    """Suggestions of the checks of the original sources are silenced as they are collected."""

    def test_silenced_suggestions_are_counted_in_the_execution_record(self):
        self.write("main.tex", "\\documentclass{article}\nText. % fledge: ignore TEX001\n")
        report = Report("check")
        reporter = TaskReporter(
            report, Settings(), source_root=self.root, main="main.tex", document_name=None
        )
        reporter.collect(
            TaskResult(
                TaskName.MANUSCRIPT_CHECKS,
                TaskStatus.SUCCEEDED,
                value=[suggestion(2), suggestion(1)],
            )
        )
        self.assertEqual(report.findings, [suggestion(1)])
        self.assertEqual(
            report.execution["silenced_suggestions"],
            [{"code": "TEX001", "path": "main.tex", "line": 2}],
        )


class ArchivedSuppressionTests(AsyncProjectTestCase):
    """Final findings use source locations in the verified archive."""

    async def test_flat_layout_keeps_a_suggestion_suppressed_at_its_new_path(self):
        source = self.write_project(
            {
                "paper/main.tex": (
                    "\\documentclass{article}\n\\begin{document}\n"
                    "Fast... % fledge: ignore STY305\n\\end{document}\n"
                )
            }
        )
        output = self.root / "output"
        settings = Settings(
            main="paper/main.tex", layout="flat", checks=CheckSelection(select=("ALL", "STY305"))
        )
        report = await run_job(
            JobRequest("prepare", source, settings, output), backends=controlled_backends()
        )
        self.assertTrue(report.submission_written, report.to_dict())
        self.assertFalse(any(item.code == "STY305" for item in report.findings))
        self.assertEqual(
            report.execution["silenced_suggestions"],
            [
                {"code": "STY305", "path": "paper/main.tex", "line": 3},
                {"code": "STY305", "path": "main.tex", "line": 3},
            ],
        )


if __name__ == "__main__":
    unittest.main()
