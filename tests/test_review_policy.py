"""Review decisions: suppressions, accepted exceptions and what they may not waive."""

from __future__ import annotations

import tempfile
import unittest
from dataclasses import FrozenInstanceError, replace
from pathlib import Path
from unittest.mock import patch

from latexprep.errors import PreparationError
from latexprep.file_safety import read_regular_file
from latexprep.findings import Finding
from latexprep.measurement_findings import heuristic_finding, measurement_finding
from latexprep.option_config import read_options
from latexprep.report import Outcome, Report
from latexprep.review_policy import (
    ReportingOptions,
    ReviewRule,
    apply_reviews,
    has_unaccepted_blockers,
    review_marker,
    reviewed_findings,
)
from latexprep.submission_checks import check_pdf_identity, check_submission
from latexprep.submission_options import SubmissionOptions
from latexprep.submission_scan_findings import ScanLimits


class ReviewTests(unittest.TestCase):
    """Review rules: their validation, their scopes and the findings they may not waive."""

    def finding(self, **changes):
        """Return a failed ``pdf.rendered_text_size`` error on page 4 of ``paper.pdf``.

        The finding belongs to the ``final`` stage of ``main.tex``.

        Args:
            **changes: Fields of the :class:`Finding` that replace those described above.

        Returns:
            The finding.
        """
        finding = Finding(
            "pdf.rendered_text_size",
            "Caption text is small",
            severity="error",
            status="failed",
            path="paper.pdf",
            details={"stage": "final", "document": "main.tex", "page": 4},
        )
        return replace(finding, **changes)

    def test_reason_code_and_scopes_are_validated_and_frozen(self):
        for options in (
            {"reason": " "},
            {"code": "PDF999"},
            {"path": "../paper.pdf"},
            {"path": "C:\\paper.pdf"},
            {"line_start": 4},
            {"page_end": 4},
            {"page_start": True},
            {"page_start": 4, "page_end": 3},
            {"stage": "a\nb"},
        ):
            with self.subTest(options=options), self.assertRaises(PreparationError):
                ReviewRule(**{"code": "PDF202", "reason": "Reviewed caption", **options})
        review = ReviewRule("PDF202", "Reviewed caption", path="paper.pdf", page_start=4)
        with self.assertRaises(FrozenInstanceError):
            review.reason = "Changed"
        with self.assertRaises(PreparationError):
            ReportingOptions(accepted_exceptions=[review])
        loaded = read_options(
            ReportingOptions,
            {
                "accepted_exceptions": [
                    {
                        "code": "PDF202",
                        "reason": "Reviewed caption",
                        "path": "paper.pdf",
                        "page_start": 4,
                    }
                ]
            },
            "reporting",
        )
        self.assertEqual(loaded.accepted_exceptions, (review,))

    def test_scope_is_conjunctive_and_aggregate_pages_must_all_match(self):
        review = ReviewRule(
            "PDF202",
            "Reviewed caption",
            path="paper.pdf",
            page_start=4,
            stage="final",
            document="main.tex",
        )
        options = ReportingOptions(accepted_exceptions=(review,))
        finding = self.finding()
        self.assertFalse(has_unaccepted_blockers([finding], options))
        for modified in (
            replace(finding, path="other.pdf"),
            replace(finding, details={**finding.details, "page": 5}),
            replace(finding, details={**finding.details, "stage": "baseline"}),
            replace(finding, details={**finding.details, "document": "supplement.tex"}),
            replace(finding, details={}),
        ):
            with self.subTest(modified=modified):
                self.assertTrue(has_unaccepted_blockers([modified], options))
        violations = [{"page": 4}, {"page": 5}]
        aggregate = replace(
            finding,
            details={
                "stage": "final",
                "document": "main.tex",
                "violations": violations,
                "violation_count": 2,
            },
        )
        self.assertTrue(has_unaccepted_blockers([aggregate], options))
        both = ReportingOptions(accepted_exceptions=(replace(review, page_end=5),))
        self.assertFalse(has_unaccepted_blockers([aggregate], both))
        sampled = replace(aggregate, details={**aggregate.details, "violation_count": 3})
        self.assertTrue(has_unaccepted_blockers([sampled], both))

    def test_source_line_scope_is_literal_and_inclusive(self):
        finding = Finding(
            "manuscript.abstract_citations", "Citation", severity="error", path="main.tex", line=12
        )
        options = ReportingOptions(
            accepted_exceptions=(
                ReviewRule(
                    "MAN003", "Explicit exception", path="main.tex", line_start=10, line_end=12
                ),
            )
        )
        self.assertFalse(has_unaccepted_blockers([finding], options))
        self.assertTrue(has_unaccepted_blockers([replace(finding, line=13)], options))
        self.assertTrue(has_unaccepted_blockers([replace(finding, line=None)], options))
        glob = ReportingOptions(
            accepted_exceptions=(ReviewRule("MAN003", "Literal filename", path="*.tex"),)
        )
        self.assertTrue(has_unaccepted_blockers([finding], glob))

    def test_incomplete_and_safety_findings_cannot_be_waived(self):
        for rule in (
            "build.tex_error",
            "build.rerun_required",
            "source.external_path",
            "flatten.identity_collision",
            "pdf.javascript_policy",
        ):
            finding = Finding(rule, "Original evidence", severity="error")
            options = ReportingOptions(accepted_exceptions=(ReviewRule(finding.code, "Reviewed"),))
            self.assertTrue(has_unaccepted_blockers([finding], options))
            result = reviewed_findings([finding], options)
            self.assertEqual(result[0], finding)
            self.assertEqual(result[-1].rule, "report.review_ineligible")
        options = ReportingOptions(accepted_exceptions=(ReviewRule("PDF202", "Reviewed"),))
        for finding in (
            self.finding(status="inconclusive"),
            self.finding(status="skipped"),
            self.finding(incomplete=True, details={"incomplete": True}),
            self.finding(incomplete=True, details={"unsupported_pages": [2]}),
            self.finding(incomplete=True, details={"nested": {"unmeasured": ["figure"]}}),
        ):
            self.assertTrue(has_unaccepted_blockers([finding], options))

    def test_explicitly_incomplete_failures_cannot_be_accepted_or_suppressed(self):
        accept = ReportingOptions(accepted_exceptions=(ReviewRule("PDF202", "Reviewed size"),))
        suppress = ReportingOptions(suppressions=(ReviewRule("PDF204", "Reviewed overlay"),))
        for incomplete in (False, True):
            with self.subTest(incomplete=incomplete):
                error = measurement_finding(
                    "pdf.rendered_text_size", "Small text", [{"page": 4}], incomplete=incomplete
                )
                advisory = heuristic_finding(
                    "pdf.text_overlap", "Overlapping text", [{"page": 4}], incomplete=incomplete
                )
                self.assertEqual((error.status, error.incomplete), ("failed", incomplete))
                self.assertEqual((advisory.status, advisory.severity), ("failed", "warning"))
                self.assertEqual(has_unaccepted_blockers([error], accept), incomplete)
                for finding, options, label in (
                    (error, accept, "Accepted exception: Reviewed size"),
                    (advisory, suppress, "Suppressed advisory: Reviewed overlay"),
                ):
                    reviewed = reviewed_findings([finding], options)
                    if incomplete:
                        self.assertEqual(reviewed[0], finding)
                        self.assertEqual(reviewed[-1].rule, "report.review_ineligible")
                    else:
                        self.assertEqual(review_marker(reviewed[0]), label)

    def test_comparison_exceptions_cannot_accept_archive_changes(self):
        options = ReportingOptions(accepted_exceptions=(ReviewRule("CMP003", "Intended edit"),))
        finding = Finding(
            "compare.rendering",
            "Changed",
            severity="error",
            details={
                "stage": "baseline versus prepared",
                "changed_pages": [1],
            },
        )
        self.assertFalse(has_unaccepted_blockers([finding], options))
        for stage in ("prepared versus archive rebuild", "archive", None):
            modified = replace(finding, details={**finding.details, "stage": stage})
            self.assertTrue(has_unaccepted_blockers([modified], options))
        unavailable = Finding(
            "compare.unavailable", "Renderer failed", severity="error", status="inconclusive"
        )
        self.assertTrue(has_unaccepted_blockers([finding, unavailable], options))

    def test_real_privacy_aggregates_with_missing_channels_cannot_be_waived(self):
        scan = SubmissionOptions(identity_terms=("Alice",))
        review = ReportingOptions(accepted_exceptions=(ReviewRule("PRV002", "Reviewed match"),))
        for text, metadata in (("Alice", {}), ("", {"Author": "Alice"})):
            with self.subTest(text=text, metadata=metadata):
                finding = check_pdf_identity(text, metadata, scan)[0]
                self.assertEqual(finding.status, "failed")
                self.assertTrue(finding.details["issues"])
                self.assertTrue(has_unaccepted_blockers([finding], review))
                annotated = reviewed_findings([finding], review)
                self.assertEqual(annotated[0], finding)
                self.assertEqual(annotated[-1].rule, "report.review_ineligible")
        complete = check_pdf_identity("Alice", {"Author": "Anonymous"}, scan)[0]
        self.assertFalse(has_unaccepted_blockers([complete], review))
        limited = check_pdf_identity(
            "Alice", {"Author": "Alice"}, scan, limits=ScanLimits(max_matches=1)
        )[0]
        self.assertTrue(limited.details["match_output_limited"])
        self.assertTrue(has_unaccepted_blockers([limited], review))

    def test_unreadable_source_aggregate_retains_coverage_blocker(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tex").write_text("% Alice\n\\documentclass{article}\n")
            (root / "unreadable.tex").write_text("Uninspected source")

            def read(path, *args, **kwargs):
                if path.name == "unreadable.tex":
                    raise OSError("Unreadable fixture")
                return read_regular_file(path, *args, **kwargs)

            with patch("latexprep.file_scan.read_regular_file", side_effect=read):
                findings = check_submission(
                    root, "main.tex", SubmissionOptions(identity_terms=("Alice",))
                )
            finding = next(item for item in findings if item.code == "PRV001")
            self.assertEqual(finding.status, "failed")
            self.assertTrue(finding.details["matches"])
            self.assertTrue(finding.details["issues"])
            options = ReportingOptions(accepted_exceptions=(ReviewRule("PRV001", "Reviewed"),))
            self.assertTrue(has_unaccepted_blockers([finding], options))
            self.assertEqual(reviewed_findings([finding], options)[0], finding)

    def test_suppression_retains_original_evidence_and_cannot_suppress_errors(self):
        finding = Finding("source.edit_marker", "TODO quotation", path="main.tex", line=2)
        options = ReportingOptions(
            suppressions=(
                ReviewRule("TEX001", "Intentional example", path="main.tex", line_start=2),
            )
        )
        report = apply_reviews(
            Report("inspect", outcome=Outcome.PASSED_WITH_ADVISORIES, findings=[finding]), options
        )
        reviewed = report.findings[0]
        self.assertEqual(reviewed.status, "failed")
        self.assertEqual(reviewed.severity, "warning")
        self.assertEqual(reviewed.message, finding.message)
        self.assertIn("Suppressed advisory: Intentional example", review_marker(reviewed))
        self.assertNotIn("review", finding.details)
        error = replace(finding, severity="error")
        self.assertTrue(has_unaccepted_blockers([error], options))
        self.assertEqual(reviewed_findings([error], options)[-1].rule, "report.review_ineligible")

    def test_decisions_never_auto_accept_or_upgrade_incomplete_workflows(self):
        finding = self.finding(
            details={
                "review": [
                    {
                        "action": "accepted_exception",
                        "reason": "Forged metadata",
                    }
                ]
            }
        )
        self.assertTrue(has_unaccepted_blockers([finding], ReportingOptions()))
        self.assertNotIn(
            "review",
            apply_reviews(Report("check", findings=[finding]), ReportingOptions())
            .findings[0]
            .details,
        )
        options = ReportingOptions(accepted_exceptions=(ReviewRule("PDF202", "Reviewed"),))
        for outcome in ("blocked", "error", "cancelled", "planned"):
            report = apply_reviews(Report("prepare", outcome=outcome, findings=[finding]), options)
            self.assertEqual(report.outcome, outcome)
        complete = apply_reviews(
            Report("check", outcome=Outcome.PASSED, findings=[finding]), options
        )
        self.assertEqual(complete.outcome, "accepted_exceptions")
        self.assertEqual(complete.findings[0].status, "failed")
        self.assertEqual(apply_reviews(complete, options).to_dict(), complete.to_dict())

    def test_unmatched_review_is_visible(self):
        options = ReportingOptions(
            accepted_exceptions=(ReviewRule("PDF202", "Reviewed", page_start=99),)
        )
        report = apply_reviews(
            Report("check", outcome=Outcome.BLOCKED, findings=[self.finding()]), options
        )
        self.assertEqual(report.outcome, "blocked")
        self.assertEqual(report.findings[-1].rule, "report.review_unmatched")
        self.assertEqual(report.findings[-1].details["requested_review"]["reason"], "Reviewed")
        no_findings = apply_reviews(Report("check", outcome=Outcome.PASSED), options)
        self.assertEqual(no_findings.outcome, "passed_with_advisories")


if __name__ == "__main__":
    unittest.main()
