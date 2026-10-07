"""Review decisions never erase evidence; exported reports are inert and sanitized."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
import zipfile
from dataclasses import FrozenInstanceError, replace
from html.parser import HTMLParser
from pathlib import Path
from unittest.mock import patch

from latexprep.models import Change, Finding, PreparationError, Report
from latexprep.option_config import read_options
from latexprep.reporting import (
    ReportingOptions,
    ReviewRule,
    apply_reviews,
    export_diagnostic_bundle,
    has_unaccepted_blockers,
    render_ci_annotations,
    render_html,
    review_marker,
    reviewed_findings,
)
from latexprep.submission_checks import SubmissionOptions, check_pdf_identity, check_submission
from latexprep.submission_checks import _read as read_submission_file


class ReviewTests(unittest.TestCase):
    def finding(self, **changes):
        finding = Finding(
            "pdf.rendered_text_size",
            "Caption text is small",
            "error",
            "failed",
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
            "manuscript.abstract_citations", "Citation", "error", path="main.tex", line=12
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
            "source-external-path",
            "flatten-identity-collision",
            "pdf.javascript_policy",
        ):
            finding = Finding(rule, "Original evidence", "error")
            options = ReportingOptions(accepted_exceptions=(ReviewRule(finding.code, "Reviewed"),))
            self.assertTrue(has_unaccepted_blockers([finding], options))
            result = reviewed_findings([finding], options)
            self.assertEqual(result[0], finding)
            self.assertEqual(result[-1].rule, "report.review_ineligible")
        options = ReportingOptions(accepted_exceptions=(ReviewRule("PDF202", "Reviewed"),))
        for finding in (
            self.finding(status="inconclusive"),
            self.finding(status="skipped"),
            self.finding(details={"incomplete": True}),
            self.finding(details={"unsupported_pages": [2]}),
            self.finding(details={"nested": {"unmeasured": ["figure"]}}),
        ):
            self.assertTrue(has_unaccepted_blockers([finding], options))

    def test_comparison_exceptions_cannot_accept_archive_changes(self):
        options = ReportingOptions(accepted_exceptions=(ReviewRule("CMP003", "Intended edit"),))
        finding = Finding(
            "compare.rendering",
            "Changed",
            "error",
            details={
                "stage": "baseline versus prepared",
                "changed_pages": [1],
            },
        )
        self.assertFalse(has_unaccepted_blockers([finding], options))
        for stage in ("prepared versus archive rebuild", "archive", None):
            modified = replace(finding, details={**finding.details, "stage": stage})
            self.assertTrue(has_unaccepted_blockers([modified], options))
        unavailable = Finding("compare.unavailable", "Renderer failed", "error", "inconclusive")
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
        with patch("latexprep.submission_checks.MAX_SCAN_MATCHES", 1):
            limited = check_pdf_identity("Alice", {"Author": "Alice"}, scan)[0]
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
                return read_submission_file(path, *args, **kwargs)

            with patch("latexprep.submission_checks._read", side_effect=read):
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
        finding = Finding("source-edit-marker", "TODO quotation", path="main.tex", line=2)
        options = ReportingOptions(
            suppressions=(
                ReviewRule("TEX001", "Intentional example", path="main.tex", line_start=2),
            )
        )
        report = apply_reviews(
            Report("inspect", "passed_with_advisories", findings=[finding]), options
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
            report = apply_reviews(Report("prepare", outcome, findings=[finding]), options)
            self.assertEqual(report.outcome, outcome)
        complete = apply_reviews(Report("check", "passed", findings=[finding]), options)
        self.assertEqual(complete.outcome, "accepted_exceptions")
        self.assertEqual(complete.findings[0].status, "failed")
        self.assertEqual(apply_reviews(complete, options).to_dict(), complete.to_dict())

    def test_unmatched_review_is_visible(self):
        options = ReportingOptions(
            accepted_exceptions=(ReviewRule("PDF202", "Reviewed", page_start=99),)
        )
        report = apply_reviews(Report("check", "blocked", findings=[self.finding()]), options)
        self.assertEqual(report.outcome, "blocked")
        self.assertEqual(report.findings[-1].rule, "report.review_unmatched")
        self.assertEqual(report.findings[-1].details["requested_review"]["reason"], "Reviewed")
        no_findings = apply_reviews(Report("check", "passed"), options)
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
            "blocked",
            payload,
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
                    "format",
                    payload,
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
                    "info",
                    "passed",
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
                    "source-edit-marker",
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
        literal = Report("check", findings=[Finding("source-edit-marker", "literal %0A")])
        self.assertIn("%250A", render_ci_annotations(literal))
        report.findings[0] = replace(report.findings[0], path="../../outside.tex")
        self.assertNotIn("file=", render_ci_annotations(report))

    def test_review_reasons_are_visible_and_redacted_in_html_and_ci(self):
        report = Report(
            "check",
            "passed",
            findings=[
                Finding(
                    "manuscript.abstract_citations",
                    "Citation",
                    "error",
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


class DiagnosticBundleTests(unittest.TestCase):
    def test_bundle_is_unverified_and_cross_redacted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "tool.log").write_text("password=CrossFileSecret\n", encoding="utf-8")
            report = Report(
                "prepare",
                "passed",
                "Verified workflow scope",
                findings=[Finding("source-edit-marker", "CrossFileSecret")],
                changes=[Change("main.tex", "edit", "Reason", diff="-CrossFileSecret\n+value")],
                stages=[{"name": "baseline", "log": "CrossFileSecret"}],
            )
            destination = root / "diagnostics.zip"
            self.assertEqual(
                export_diagnostic_bundle(
                    report,
                    destination,
                    scratch_root=root,
                    artifacts=("tool.log",),
                    logs={"tool": "CrossFileSecret"},
                ),
                destination,
            )
            with zipfile.ZipFile(destination) as archive:
                self.assertEqual(
                    set(archive.namelist()),
                    {
                        "README.txt",
                        "report.json",
                        "report.html",
                        "logs/stages.json",
                        "logs/explicit.json",
                        "diffs/0001.diff",
                        "scratch/0001.log",
                    },
                )
                for name in archive.namelist():
                    self.assertNotIn(b"CrossFileSecret", archive.read(name))
                values = json.loads(archive.read("report.json"))
                self.assertEqual(values["outcome"], "blocked")
                self.assertIn("not a submission package", values["scope"])
                self.assertEqual(
                    values["execution"]["diagnostic_export"]["source_outcome"], "passed"
                )
                self.assertIn(b"UNVERIFIED DIAGNOSTIC BUNDLE", archive.read("README.txt"))
            self.assertEqual(report.outcome, "passed")
            self.assertNotIn("diagnostic_export", report.execution)

    def test_unsafe_scratch_inputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scratch = root / "scratch"
            scratch.mkdir()
            (root / "outside.log").write_text("outside")
            (scratch / "linked.log").symlink_to(root / "outside.log")
            (scratch / "linked-dir").symlink_to(root, target_is_directory=True)
            (scratch / "binary.log").write_bytes(b"binary\0content")
            (scratch / "source.pdf").write_bytes(b"%PDF-1.4")
            (scratch / "invalid.log").write_bytes(b"\xff")
            os.mkfifo(scratch / "pipe.log")
            for path in (
                "../outside.log",
                "linked.log",
                "linked-dir/outside.log",
                "binary.log",
                "source.pdf",
                "invalid.log",
                "pipe.log",
                "/outside.log",
                "a\\b.log",
            ):
                with self.subTest(path=path), self.assertRaises(PreparationError):
                    export_diagnostic_bundle(
                        Report("check"), root / "bad.zip", scratch_root=scratch, artifacts=(path,)
                    )
                self.assertFalse((root / "bad.zip").exists())

    def test_export_refuses_overwrite_and_artifact_limits(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            destination = root / "existing.zip"
            destination.write_bytes(b"user data")
            with self.assertRaises(PreparationError):
                export_diagnostic_bundle(Report("check"), destination)
            self.assertEqual(destination.read_bytes(), b"user data")
            (root / "big.log").write_text("oversized")
            with patch("latexprep.reporting._MAX_ARTIFACT_BYTES", 2):
                with self.assertRaises(PreparationError):
                    export_diagnostic_bundle(
                        Report("check"), root / "new.zip", scratch_root=root, artifacts=("big.log",)
                    )
            self.assertFalse((root / "new.zip").exists())

    def test_artifact_names_never_disclose_source_credentials_or_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            secret_name = "ghp_" + "a" * 25 + ".log"
            (root / secret_name).write_text("tool output")
            export_diagnostic_bundle(
                Report("check"), root / "bundle.zip", scratch_root=root, artifacts=(secret_name,)
            )
            with zipfile.ZipFile(root / "bundle.zip") as archive:
                self.assertIn("scratch/0001.log", archive.namelist())
                for name in archive.namelist():
                    self.assertNotIn(secret_name.encode(), archive.read(name))
                    self.assertNotIn(secret_name, name)


if __name__ == "__main__":
    unittest.main()
