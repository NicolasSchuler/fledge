"""The diagnostic bundle: sanitized reports and diffs in one ZIP."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from latexprep.diagnostic_bundle import (
    export_diagnostic_bundle,
    export_redacted_diagnostic_bundle,
)
from latexprep.errors import PreparationError
from latexprep.findings import Finding
from latexprep.report import Change, ChangeKind, Outcome, Report, redact_report


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
