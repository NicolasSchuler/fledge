"""Review decisions never erase evidence; exported reports are inert and sanitized."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from dataclasses import FrozenInstanceError, replace
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from latexprep.ci_annotations import render_ci_annotations
from latexprep.diagnostic_bundle import (
    export_diagnostic_bundle,
    export_redacted_diagnostic_bundle,
)
from latexprep.errors import PreparationError
from latexprep.file_safety import read_regular_file
from latexprep.finding_view import RenderOptions, evidence_excerpt
from latexprep.findings import Finding
from latexprep.html_report import render_html
from latexprep.measurement_findings import heuristic_finding, measurement_finding
from latexprep.option_config import read_options
from latexprep.report import Change, ChangeKind, Outcome, Report, redact_report
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
    def finding(self, **changes):
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


class _Elements(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tags = []
        self.attributes = []

    def handle_starttag(self, tag, attrs):
        self.tags.append(tag)
        self.attributes.extend(attrs)


class RenderTests(unittest.TestCase):
    def report(self):
        secret = "SyntheticCredential_1234"
        payload = '</pre><script>alert(1)</script><img src="https://attacker.invalid" onerror="x">'
        report = Report(
            "check",
            outcome=Outcome.BLOCKED,
            scope=payload,
            main=payload,
            settings={"password": secret},
            findings=[
                Finding(
                    "pdf.printable_text_bounds",
                    payload + secret,
                    path='"/><script>alert(2)</script>.pdf',
                    suggestion=payload,
                    details={"page": 2, "bounds_pt": [1, 2, 30, 40]},
                )
            ],
            changes=[
                Change(
                    payload,
                    kind=ChangeKind.FORMAT,
                    reason=payload,
                    diff=f"--- a\n+++ b\n@@ -1 +1 @@\n-{payload}\n+{secret}\n",
                )
            ],
            artifacts={"pdf": "https://attacker.invalid/document.pdf"},
        )
        return secret, payload, report

    def test_html_escapes_every_surface_and_contains_no_remote_or_executable_resources(self):
        secret, payload, report = self.report()
        rendered = render_html(report)
        self.assertNotIn(secret, rendered)
        self.assertNotIn(payload, rendered)
        self.assertIn("&lt;script&gt;", rendered)
        self.assertIn("Before", rendered)
        self.assertIn("After", rendered)
        parser = _Elements()
        parser.feed(rendered)
        self.assertNotIn("script", parser.tags)
        self.assertNotIn("img", parser.tags)
        self.assertNotIn("iframe", parser.tags)
        for name, value in parser.attributes:
            self.assertFalse(name.startswith("on"))
            self.assertNotIn(name, {"src", "srcdoc"})
            if name == "href":
                self.assertFalse(value.startswith(("http:", "https:", "javascript:", "//")))
        self.assertIn("Content-Security-Policy", rendered)
        self.assertIn("Next action:", rendered)

    def test_pdf_region_diagrams_use_recorded_geometry_and_safe_local_links(self):
        report = Report(
            "check",
            findings=[
                Finding(
                    "pdf.geometry",
                    "Dimensions",
                    severity="info",
                    status="passed",
                    details={"dimensions_pt": [[100, 200], [300, 400]], "stage": "final"},
                ),
                Finding(
                    "pdf.text_overlap",
                    "Overlap",
                    details={
                        "stage": "final",
                        "violations": [{"page": 2, "bounds_pt": [[1, 2, 3, 4], [2, 3, 4, 5]]}],
                    },
                ),
            ],
            artifacts={"pdf": "/tmp/paper #1.pdf"},
        )
        rendered = render_html(report)
        self.assertIn('viewBox="0 0 300 400"', rendered)
        self.assertIn("/tmp/paper%20%231.pdf#page=2", rendered)
        self.assertIn("does not embed or modify the PDF", rendered)
        report.artifacts["pdf"] = "javascript:bad.pdf"
        self.assertNotIn('href="javascript', render_html(report))
        report.findings[-1] = replace(
            report.findings[-1],
            details={
                "page": 2,
                "bounds_pt": [0, 0, float("nan"), 20],
            },
        )
        self.assertNotIn("<svg", render_html(report))

    def test_ci_annotations_escape_injected_commands_paths_and_credentials(self):
        report = Report(
            "check",
            settings={"password": "Credential_1"},
            findings=[
                Finding(
                    "source.edit_marker",
                    "Credential_1\n::error::injected\r\x1b[2J %0A",
                    path="main,part.tex",
                    line=5,
                )
            ],
        )
        annotation = render_ci_annotations(report)
        self.assertEqual(len(annotation.splitlines()), 1)
        self.assertNotIn("Credential_1", annotation)
        self.assertNotIn("\x1b", annotation)
        self.assertIn("%0A::error::injected%0D%1B", annotation)
        self.assertIn("file=main%2Cpart.tex,line=5", annotation)
        literal = Report("check", findings=[Finding("source.edit_marker", "literal %0A")])
        self.assertIn("%250A", render_ci_annotations(literal))
        report.findings[0] = replace(report.findings[0], path="../../outside.tex")
        self.assertNotIn("file=", render_ci_annotations(report))

    def test_ci_hides_scope_disclaimers_unless_passed_results_are_requested(self):
        report = Report(
            "check",
            findings=[
                Finding("online.metadata", "Offline run.", severity="info", status="skipped"),
                Finding(
                    "build.bibliography_backend",
                    "No backend.",
                    severity="info",
                    status="not_applicable",
                ),
                Finding("pdf.pages", "1 page", severity="info", status="passed"),
                Finding("pdf.fonts", "Fonts unavailable", severity="error", status="skipped"),
                Finding("source.edit_marker", "Marker", path="main.tex", line=2),
            ],
        )
        annotations = render_ci_annotations(report)
        self.assertEqual(len(annotations.splitlines()), 2)
        for hidden in ("Offline run", "No backend", "1 page"):
            self.assertNotIn(hidden, annotations)
        everything = render_ci_annotations(report, options=RenderOptions(show_passed=True))
        self.assertEqual(len(everything.splitlines()), 5)
        self.assertIn("info/not applicable: No backend.", everything)
        self.assertTrue(report.findings[0].is_scope_disclaimer)
        self.assertFalse(report.findings[3].is_scope_disclaimer)
        self.assertFalse(replace(report.findings[0], status="failed").is_scope_disclaimer)

    def test_evidence_excerpt_uses_known_lists_in_order_and_counts_hidden_items(self):
        def excerpt(**details):
            return evidence_excerpt(Finding("pdf.fonts", "Generic.", details=details))

        self.assertEqual(excerpt(), "")
        self.assertEqual(excerpt(matches=[], violations=[{"page": 5}]), "page 5")
        self.assertEqual(
            excerpt(violations=[{"page": 5}], matches=[{"path": "a.tex"}, {"path": "b.tex"}]),
            "a.tex, b.tex",
        )
        self.assertEqual(excerpt(unrelated=["x"]), "")
        self.assertEqual(
            excerpt(
                matches=[{"path": f"{n}.tex", "line": n} for n in range(1, 8)],
            ),
            "1.tex:1, 2.tex:2, 3.tex:3 (+4 more)",
        )
        self.assertEqual(
            excerpt(violations=[{"page": 2}, {"page": 3}], violation_count=12),
            "page 2, page 3 (+10 more)",
        )
        self.assertEqual(excerpt(changed_pages=[4, 6]), "page 4, page 6")
        self.assertEqual(
            excerpt(nonembedded=["Helvetica", "Times", "Symbol", "Zapf"]),
            "Helvetica, Times, Symbol (+1 more)",
        )
        self.assertEqual(excerpt(candidates=["a.tex", "b.tex"]), "a.tex, b.tex")
        self.assertEqual(
            excerpt(candidates=[{"doi": "10.1/a", "title": "A"}, {"doi": "10.1/b", "title": "B"}]),
            "10.1/a, 10.1/b",
        )
        self.assertEqual(
            excerpt(issues=["Unreadable source: a.tex", "Unreadable source: b.tex"]),
            "Unreadable source: a.tex, Unreadable source: b.tex",
        )
        self.assertEqual(excerpt(matches=[], issues=[{"path": "a.png", "reason": "x"}]), "a.png")
        self.assertEqual(excerpt(matches=["a.tex"], issues=["unreadable"]), "a.tex")
        self.assertEqual(
            excerpt(violations=[{"page": 4, "reason": "Duplicate marked-content reference"}]),
            "Duplicate marked-content reference (page 4)",
        )
        clipped = excerpt(violations=[{"page": 4, "reason": "x" * 500}])
        self.assertEqual((len(clipped), clipped[-10:]), (120, "… (page 4)"))
        self.assertEqual(excerpt(missing=["twocolumn"]), "twocolumn")
        self.assertEqual(excerpt(entries=[{"name": "key1"}, {"key": "key2"}]), "key1, key2")
        self.assertEqual(excerpt(below_minimum=[{"page": 1, "number": 2, "x_dpi": 72}]), "page 1")
        self.assertEqual(
            excerpt(matches=[{"path": "a.pdf", "page": 3}, {"path": "a.pdf", "line": 4}]),
            "a.pdf (page 3), a.pdf:4",
        )
        # Repeated locations are shown once; unreadable items are counted as not shown.
        self.assertEqual(
            excerpt(matches=[{"path": "a.tex", "line": 1}] * 3 + [{"unknown": 1}]),
            "a.tex:1 (+1 more)",
        )
        self.assertEqual(excerpt(matches=[{"unknown": 1}], violations=[{"page": 2}]), "page 2")
        self.assertEqual(excerpt(matches=[True, None, 1.5, [1]]), "")
        self.assertEqual(len(excerpt(matches=["x" * 500])), 120)
        passed = Finding(
            "pdf.fonts", "ok", severity="info", status="passed", details={"matches": ["a.tex"]}
        )
        self.assertEqual(evidence_excerpt(passed), "")

    def test_ci_and_html_show_sanitized_evidence_excerpts(self):
        secret = "SyntheticCredential_1234"
        report = Report(
            "check",
            settings={"password": secret},
            findings=[
                Finding(
                    "manuscript.forbidden_packages",
                    "Generic message.",
                    details={
                        "matches": [
                            {"path": f"{secret}.tex", "line": 2},
                            {"path": "b\n::error::injected.tex", "line": 3},
                            {"path": "<script>.tex"},
                        ]
                    },
                )
            ],
        )
        annotation = render_ci_annotations(report)
        self.assertEqual(len(annotation.splitlines()), 1)
        self.assertIn("| Evidence: ", annotation)
        self.assertIn("b%0A::error::injected.tex:3", annotation)
        self.assertNotIn(secret, annotation)
        rendered = render_html(report)
        self.assertIn("<p>Evidence: ", rendered)
        self.assertIn("&lt;script&gt;.tex", rendered)
        self.assertNotIn("<script>.tex", rendered)

    def test_html_retains_distinct_violation_evidence_across_stages(self):
        baseline_violation = {"page": 1, "reported_size_pt": 8}
        for final_violation in (
            {"page": 2, "reported_size_pt": 8},
            {"page": 1, "reported_size_pt": 9},
        ):
            with self.subTest(final_violation=final_violation):
                report = Report(
                    "prepare",
                    findings=[
                        measurement_finding(
                            "pdf.rendered_text_size",
                            "Compared reported text sizes with configured minima.",
                            [violation],
                            stage=stage,
                        )
                        for stage, violation in (
                            ("baseline", baseline_violation),
                            ("final verification", final_violation),
                        )
                    ],
                )
                rendered = render_html(report)
                self.assertEqual(rendered.count("<summary>Recorded evidence</summary>"), 2)
                self.assertIn("[baseline]", rendered)
                self.assertIn("[final verification]", rendered)
                for violation in (baseline_violation, final_violation):
                    for key, value in violation.items():
                        self.assertIn(f"&quot;{key}&quot;: {value}", rendered)

    def test_html_merges_identical_stage_evidence_regardless_of_key_order(self):
        report = Report(
            "prepare",
            findings=[
                measurement_finding(
                    "pdf.rendered_text_size",
                    "Compared reported text sizes with configured minima.",
                    [violation],
                    stage=stage,
                )
                for stage, violation in (
                    ("baseline", {"page": 1, "reported_size_pt": 8}),
                    ("final verification", {"reported_size_pt": 8, "page": 1}),
                )
            ],
        )
        rendered = render_html(report)
        self.assertEqual(rendered.count("<summary>Recorded evidence</summary>"), 1)
        self.assertIn("[baseline, final verification]", rendered)

    def test_review_reasons_are_visible_and_redacted_in_html_and_ci(self):
        report = Report(
            "check",
            outcome=Outcome.PASSED,
            findings=[
                Finding(
                    "manuscript.abstract_citations",
                    "Citation",
                    severity="error",
                    path="main.tex",
                )
            ],
            settings={"password": "SensitiveExample"},
        )
        options = ReportingOptions(
            accepted_exceptions=(ReviewRule("MAN003", "Reviewed SensitiveExample <example>"),)
        )
        report = apply_reviews(report, options)
        for output in (render_html(report), render_ci_annotations(report)):
            self.assertIn("Accepted exception", output)
            self.assertNotIn("SensitiveExample", output)
            self.assertIn("failed", output)
        self.assertTrue(render_ci_annotations(report).startswith("::notice "))

    def test_accepted_stage_copy_does_not_hide_an_unaccepted_copy(self):
        copies = [
            Finding(
                "manuscript.abstract_citations",
                "Citation",
                severity="error",
                path="main.tex",
                details={"stage": stage},
            )
            for stage in ("baseline", "final source checks")
        ]
        options = ReportingOptions(
            accepted_exceptions=(ReviewRule("MAN003", "Reviewed baseline", stage="baseline"),)
        )
        report = apply_reviews(Report("check", findings=copies), options)
        self.assertEqual(report.outcome, Outcome.BLOCKED)
        annotations = render_ci_annotations(report).splitlines()
        self.assertEqual(len(annotations), 2)
        self.assertTrue(any(line.startswith("::error ") for line in annotations))
        self.assertTrue(any("Accepted exception" in line for line in annotations))
        self.assertEqual(render_html(report).count("Citation"), 2)


class _Structure(HTMLParser):
    """The landmark, title, heading and table structure of a rendered page."""

    def __init__(self):
        super().__init__()
        self.title = ""
        self.mains = 0
        self.captions = 0
        self.table_headers: list[dict[str, str | None]] = []
        self.tables = 0
        self._in_title = False

    def handle_starttag(self, tag, attrs):
        if tag == "title":
            self._in_title = True
        elif tag == "main":
            self.mains += 1
        elif tag == "table":
            self.tables += 1
        elif tag == "caption":
            self.captions += 1
        elif tag == "th":
            self.table_headers.append(dict(attrs))

    def handle_endtag(self, tag):
        if tag == "title":
            self._in_title = False

    def handle_data(self, data):
        if self._in_title:
            self.title += data


class HtmlPageStructureTests(unittest.TestCase):
    def report(self, **fields):
        return Report(
            "prepare",
            outcome=Outcome.PASSED_WITH_ADVISORIES,
            scope="selected checks",
            changes=[
                Change(
                    "sections/intro.tex",
                    kind=ChangeKind.REWRITE,
                    reason="Removed comments",
                    diff="--- a\n+++ b\n@@ -1 +1 @@\n-old\n+new\n",
                )
            ],
            **fields,
        )

    def structure(self, report):
        parser = _Structure()
        parser.feed(render_html(report))
        return parser

    def test_page_has_one_main_landmark_and_a_title_naming_command_and_document(self):
        page = self.structure(self.report(main="paper/main.tex"))
        self.assertEqual(page.mains, 1)
        self.assertEqual(page.title, "Fledge prepare report: paper/main.tex")
        self.assertEqual(self.structure(self.report()).title, "Fledge prepare report")

    def test_title_escapes_the_document_name(self):
        rendered = render_html(self.report(main="</title><script>alert(1)</script>"))
        self.assertNotIn("<script>", rendered)
        self.assertIn("&lt;/title&gt;", rendered.split("</title>")[0])

    def test_every_table_has_a_caption_and_scoped_column_headers(self):
        page = self.structure(
            self.report(
                package_contents=[
                    {
                        "document": "main",
                        "status": "verified",
                        "files": [{"source": "a.tex", "path": "a.tex", "status": "included"}],
                    }
                ]
            )
        )
        self.assertEqual(page.tables, 2)
        self.assertEqual(page.captions, page.tables)
        self.assertTrue(page.table_headers)
        self.assertTrue(all(header.get("scope") == "col" for header in page.table_headers))

    def test_diff_caption_names_the_changed_file(self):
        self.assertIn(
            "<caption>Changes to sections/intro.tex</caption>", render_html(self.report())
        )

    def test_stylesheet_ships_as_package_data_with_dark_mode_and_print_rules(self):
        from importlib import resources

        css = resources.files("latexprep").joinpath("report.css").read_text(encoding="utf-8")
        self.assertIn("@media(prefers-color-scheme:dark)", css)
        self.assertIn("@media print", css)
        self.assertNotIn("</style", css)
        self.assertIn(css, render_html(self.report()))
        manifest = (Path(__file__).resolve().parents[1] / "MANIFEST.in").read_text()
        self.assertIn("include src/latexprep/report.css", manifest.splitlines())


class DiagnosticBundleTests(unittest.TestCase):
    def test_bundle_is_unverified_and_cross_redacted(self):
        secret = "CrossFieldSecret"
        report = Report(
            "prepare",
            outcome=Outcome.PASSED,
            scope="Verified workflow scope",
            settings={"password": secret},
            findings=[Finding("source.edit_marker", f"found {secret}")],
            changes=[
                Change(
                    "main.tex",
                    kind=ChangeKind.REWRITE,
                    reason="Reason",
                    diff=f"-{secret}\n+value",
                )
            ],
            stages=[{"name": "baseline", "status": "succeeded", "log": secret}],
        )
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "diagnostics.zip"
            self.assertEqual(export_diagnostic_bundle(report, destination), destination)
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        "README.txt",
                        "report.json",
                        "report.html",
                        "logs/stages.json",
                        "diffs/0001.diff",
                    },
                )
                for name in archive.namelist():
                    self.assertNotIn(secret.encode(), archive.read(name))
                values = json.loads(archive.read("report.json"))
                self.assertEqual(values["outcome"], "blocked")
                self.assertIn("not a submission package", values["scope"])
                self.assertEqual(
                    values["execution"]["diagnostic_export"]["source_outcome"], "passed"
                )
                self.assertIn("report.diagnostic_bundle", {f["rule"] for f in values["findings"]})
                self.assertIn(b"UNVERIFIED DIAGNOSTIC BUNDLE", archive.read("README.txt"))
        self.assertEqual(report.outcome, "passed")
        self.assertNotIn("diagnostic_export", report.execution)

    def test_a_redacted_report_is_exported_without_being_redacted_again(self):
        report = Report("check", outcome=Outcome.PASSED)
        public = redact_report(report)
        with tempfile.TemporaryDirectory() as directory:
            with patch("latexprep.report.redact_data") as redact:
                export_redacted_diagnostic_bundle(public, Path(directory) / "bundle.zip")
        redact.assert_not_called()

    def test_export_refuses_to_overwrite_an_existing_file(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "existing.zip"
            destination.write_bytes(b"user data")
            with self.assertRaises(PreparationError):
                export_diagnostic_bundle(Report("check"), destination)
            self.assertEqual(destination.read_bytes(), b"user data")

    def test_unwritable_destination_is_reported_not_raised_as_oserror(self):
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(PreparationError):
                export_diagnostic_bundle(Report("check"), Path(directory) / "missing" / "b.zip")


if __name__ == "__main__":
    unittest.main()
