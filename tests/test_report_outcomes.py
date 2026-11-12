"""How findings become an outcome and an exit code, and why suggestions never take part.

Exit code 3 means that the paper needs a fix and 4 that Fledge could not check it; 70 is a
defect in Fledge. Suggestions are optional advice that the check selection must switch on
explicitly and that never change the outcome.
"""

from __future__ import annotations

import unittest
from dataclasses import replace
from io import StringIO
from unittest.mock import patch

from rich.console import Console

from latexprep.check_policy import selected_findings, suggestion_selected
from latexprep.check_selection import CheckSelection
from latexprep.compact_report import render_compact
from latexprep.config import Settings
from latexprep.findings import Finding
from latexprep.job_report import job_outcome, settled_outcome
from latexprep.operational_titles import OPERATIONAL_TITLES
from latexprep.report import Outcome, Report, blocked_outcome, outcome_from_findings
from latexprep.review_policy import ReportingOptions, ReviewRule, apply_reviews
from latexprep.rule_catalogue_view import print_rule_explanation
from latexprep.rules import BY_CODE, BY_NAME, OPERATIONAL_RULE_NAMES
from latexprep.terminal_report import render_terminal

# A source error that the paper must fix.
MISSING_FILE = Finding(
    "source.missing_dependency", "Missing figures/plot.", severity="error", path="a.tex", line=2
)
# A missing tool, which keeps Fledge from checking.
NO_TEX = Finding(
    "build.execution_unavailable", "latexmk is missing.", severity="error", status="inconclusive"
)
# A citation in the abstract, a reviewable failure that an accepted exception may cover.
ABSTRACT_CITATION = Finding(
    "manuscript.abstract_citations", "Citation in the abstract.", severity="error", path="a.tex"
)


def baseline(rule: str, message: str, **fields: object) -> Finding:
    """Return an error of the baseline build stage with ``rule`` and ``message``."""
    return Finding(rule, message, severity="error", details={"stage": "baseline"}, **fields)


def suggestion(**fields: object) -> Finding:
    """Return a suggestion of the unfinished-text check (TEX001) at ``main.tex`` line 3."""
    values: dict[str, object] = {
        "severity": "suggestion",
        "path": "main.tex",
        "line": 3,
        **fields,
    }
    return Finding("source.edit_marker", "Consider rewording.", **values)


class ExitCodeTests(unittest.TestCase):
    """Every outcome has its documented exit code."""

    def test_every_outcome_has_its_documented_exit_code(self):
        expected = {
            Outcome.PASSED: 0,
            Outcome.PLANNED: 0,
            Outcome.PASSED_WITH_ADVISORIES: 1,
            Outcome.ACCEPTED_EXCEPTIONS: 2,
            Outcome.BLOCKED: 3,
            Outcome.ERROR: 4,
            Outcome.INTERNAL_ERROR: 70,
            Outcome.CANCELLED: 130,
        }
        self.assertEqual({outcome: outcome.exit_code for outcome in Outcome}, expected)

    def test_only_the_three_passing_outcomes_count_as_passed(self):
        self.assertEqual(
            {outcome for outcome in Outcome if outcome.passed},
            {Outcome.PASSED, Outcome.PASSED_WITH_ADVISORIES, Outcome.ACCEPTED_EXCEPTIONS},
        )


class BlockedOrNotCheckedTests(unittest.TestCase):
    """A blocked run is an error when only problems that prevented checking remain."""

    def test_a_paper_problem_blocks_and_a_tool_problem_could_not_check(self):
        cases = (
            ("a source error", [MISSING_FILE], Outcome.BLOCKED),
            ("a missing tool", [NO_TEX], Outcome.ERROR),
            ("both: the paper needs a fix", [NO_TEX, MISSING_FILE], Outcome.BLOCKED),
            (
                "no single main file",
                [Finding("project.main_selection", "Two roots.", severity="error")],
                Outcome.ERROR,
            ),
            (
                "an unavailable PDF measurement",
                [Finding("pdf.fonts_unavailable", "No pdffonts.", severity="error")],
                Outcome.ERROR,
            ),
            (
                "a failed setup diagnostic",
                [Finding("doctor.fixture_failed", "Fixture failed.", severity="error")],
                Outcome.ERROR,
            ),
            ("a failed build alone", [baseline("build.failed", "Exit 12.")], Outcome.BLOCKED),
            ("nothing that decides", [], Outcome.BLOCKED),
        )
        for name, findings, expected in cases:
            with self.subTest(name):
                self.assertEqual(blocked_outcome(findings), expected)
                self.assertEqual(outcome_from_findings(findings, blocked=True), expected)

    def test_the_truncated_log_of_a_stopped_build_decides_nothing(self):
        findings = [
            baseline("build.timeout", "Stopped after 1 s."),
            baseline("build.missing_pdf", "No PDF."),
            baseline("build.undefined_citation", "Citation `knuth' undefined."),
            Finding("build.baseline", "Required baseline build did not succeed", severity="error"),
        ]
        self.assertEqual(blocked_outcome(findings), Outcome.ERROR)
        # A paper problem outside the stopped build still decides.
        self.assertEqual(blocked_outcome([*findings, MISSING_FILE]), Outcome.BLOCKED)

    def test_the_summary_of_several_document_packages_decides_nothing(self):
        # It fails whenever a document package failed, for the reasons that document's own
        # findings give.
        summary = Finding(
            "package.independent_documents", "A document package failed.", severity="error"
        )
        timeout = baseline("build.timeout", "Stopped after 1 s.")
        self.assertEqual(blocked_outcome([timeout, summary]), Outcome.ERROR)
        self.assertEqual(blocked_outcome([timeout, summary, MISSING_FILE]), Outcome.BLOCKED)
        self.assertEqual(blocked_outcome([summary]), Outcome.BLOCKED)

    def test_an_accepted_paper_problem_leaves_the_problem_that_prevented_checking(self):
        settings = Settings(
            reporting=ReportingOptions(
                accepted_exceptions=(ReviewRule("MAN003", "The venue allows it."),)
            )
        )
        report = Report("inspect", findings=[ABSTRACT_CITATION, NO_TEX])
        self.assertEqual(job_outcome(report, settings), Outcome.ERROR)
        report = Report("inspect", findings=[ABSTRACT_CITATION])
        self.assertEqual(job_outcome(report, settings), Outcome.PASSED)
        # The workflow sets the outcome first; the review then labels the accepted exception.
        reviewed = apply_reviews(replace(report, outcome=Outcome.PASSED), settings.reporting)
        self.assertEqual(reviewed.outcome, Outcome.ACCEPTED_EXCEPTIONS)


class SettledOutcomeTests(unittest.TestCase):
    """The outcome a finished job ends with: defects, problems that prevented checking."""

    def settle(self, outcome: Outcome, *findings: Finding) -> Outcome:
        """Return the settled outcome of a report with ``outcome`` and ``findings``."""
        return settled_outcome(
            Report("check", outcome=outcome, findings=list(findings)), Settings()
        )

    def test_a_defect_is_an_internal_error_whatever_else_was_found(self):
        task_defect = Finding(
            "execution.task",
            "source checks: KeyError",
            severity="error",
            status="inconclusive",
            details={"task": "source checks", "traceback": ["checks.py:1 in run"]},
        )
        document_defect = replace(
            task_defect, rule="package.document", details={"traceback": ["x.py:1 in f"]}
        )
        internal = Finding("execution.internal", "Internal error", severity="error")
        for name, finding in (
            ("a task", task_defect),
            ("a document", document_defect),
            ("the run", internal),
        ):
            with self.subTest(name):
                self.assertEqual(
                    self.settle(Outcome.ERROR, finding, MISSING_FILE), Outcome.INTERNAL_ERROR
                )
                self.assertEqual(self.settle(Outcome.BLOCKED, finding), Outcome.INTERNAL_ERROR)

    def test_a_blocked_run_that_could_not_check_becomes_an_error(self):
        self.assertEqual(self.settle(Outcome.BLOCKED, NO_TEX), Outcome.ERROR)
        self.assertEqual(self.settle(Outcome.BLOCKED, NO_TEX, MISSING_FILE), Outcome.BLOCKED)
        self.assertEqual(
            self.settle(
                Outcome.BLOCKED,
                Finding("execution.deadline", "Over time.", severity="error"),
            ),
            Outcome.ERROR,
        )

    def test_other_outcomes_are_kept(self):
        for outcome in (Outcome.PASSED, Outcome.PLANNED, Outcome.CANCELLED, Outcome.ERROR):
            with self.subTest(outcome=outcome):
                self.assertEqual(self.settle(outcome, NO_TEX), outcome)


class SuggestionTests(unittest.TestCase):
    """Suggestions are off unless selected explicitly and never change the outcome."""

    def test_only_a_selector_other_than_all_switches_a_suggestion_on(self):
        cases = (
            ("the default selection", CheckSelection(), False),
            ("its code", CheckSelection(select=("TEX001",)), True),
            ("its family beside ALL", CheckSelection(select=("ALL", "TEX")), True),
            (
                "an ignored code wins",
                CheckSelection(select=("ALL", "TEX"), ignore=("TEX001",)),
                False,
            ),
        )
        for name, selection, expected in cases:
            with self.subTest(name):
                self.assertEqual(suggestion_selected(selection, "TEX001"), expected)
                kept = selected_findings([suggestion()], selection)
                self.assertEqual(kept == [suggestion()], expected)

    def test_a_finding_of_a_suggestion_rule_always_has_the_suggestion_severity(self):
        rule = replace(BY_NAME["source.edit_marker"], suggestion=True)
        catalogue = {**BY_NAME, "source.edit_marker": rule}
        warning = Finding("source.edit_marker", "TODO", path="main.tex", line=3)
        with patch("latexprep.check_policy.BY_NAME", catalogue):
            self.assertEqual(selected_findings([warning], CheckSelection()), [])
            [kept] = selected_findings([warning], CheckSelection(select=("TEX001",)))
        self.assertEqual(kept.severity, "suggestion")

    def test_suggestions_never_change_the_outcome_or_the_exit_code(self):
        findings = [suggestion(), suggestion(status="inconclusive", line=9)]
        self.assertEqual(outcome_from_findings(findings, blocked=False), Outcome.PASSED)
        report = Report("inspect", outcome=Outcome.PASSED, findings=findings)
        self.assertEqual(apply_reviews(report, ReportingOptions()).outcome, Outcome.PASSED)
        settings = Settings(checks=CheckSelection(select=("TEX001",)))
        self.assertEqual(job_outcome(report, settings), Outcome.PASSED)
        self.assertEqual(blocked_outcome(findings), Outcome.BLOCKED)

    def test_suggestions_have_a_report_section_of_their_own_and_their_own_count(self):
        report = Report(
            "inspect",
            outcome=Outcome.PASSED,
            findings=[suggestion()],
            execution={"silenced_suggestions": [{"code": "TEX001", "path": "a.tex", "line": 1}]},
        )
        terminal = render_terminal(report)
        self.assertIn("1 suggestion", terminal.splitlines()[2])
        self.assertIn("Suggestions (they never change the result or the exit code)", terminal)
        self.assertIn("1 suggestion silenced by % fledge: ignore comments", terminal)
        self.assertIn("No issues found", terminal)
        compact = render_compact(report).splitlines()
        self.assertIn("suggestions: 1 (they never change the outcome or the exit code)", compact)
        self.assertTrue(
            any(line.startswith("TEX001 suggestion/failed main.tex:3") for line in compact)
        )


class OperationalTitleTests(unittest.TestCase):
    """Operational results are shown with titles, not with their dotted rule names."""

    def test_every_operational_result_has_a_title(self):
        self.assertEqual(set(OPERATIONAL_TITLES), {str(name) for name in OPERATIONAL_RULE_NAMES})

    def test_reports_show_the_titles(self):
        report = Report(
            "check",
            findings=[
                baseline("build.failed", "Build tool exited with status 12."),
                Finding("build.baseline", "Required baseline build did not succeed"),
            ],
        )
        text = render_terminal(report)
        self.assertIn("build.failed  Build failed  [ERROR / failed]", text)
        self.assertIn("Baseline build did not succeed", text)
        self.assertNotIn("build / failed", text)


class RuleReferenceTests(unittest.TestCase):
    """A rule can carry a reference link, which its JSON and its explanation show."""

    def test_the_reference_link_and_the_suggestion_flag_are_part_of_the_rule(self):
        rule = replace(BY_CODE["TEX001"], reference_url="https://example.org/style")
        self.assertEqual(rule.to_dict()["reference_url"], "https://example.org/style")
        self.assertIs(BY_CODE["TEX001"].to_dict()["suggestion"], False)
        output = StringIO()
        print_rule_explanation(
            rule, Console(file=output, width=100, color_system=None), defaults=Settings()
        )
        self.assertIn("Reference: https://example.org/style", output.getvalue())
        output = StringIO()
        print_rule_explanation(
            BY_CODE["TEX001"], Console(file=output, color_system=None), defaults=Settings()
        )
        self.assertNotIn("Reference:", output.getvalue())


if __name__ == "__main__":
    unittest.main()
