"""Credentials must not escape through another report section or output format."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from latexprep.cli import cli
from latexprep.config import Settings
from latexprep.core import JobRequest, run_job
from latexprep.models import Change, Finding, Report
from latexprep.online_checks import OnlineOptions
from latexprep.presentation import render_compact, render_terminal
from tests.test_workflow_checks import controlled_build, source_project


class ReportSafetyTests(unittest.TestCase):
    def report(self):
        credential = "SyntheticCredentialValue_1234"
        return credential, Report(
            "check",
            "passed_with_advisories",
            "fixture",
            settings={
                "online_checks": {
                    "online_replication_urls": (
                        "https://example.org/artifact?access_token=" + credential,
                    )
                }
            },
            findings=[
                Finding(
                    "pdf.metadata",
                    "Metadata inventory",
                    "info",
                    "passed",
                    details={"Title": credential},
                ),
                Finding(
                    "source-edit-marker",
                    "Review " + credential,
                    path="main.tex",
                    suggestion="Remove " + credential,
                ),
            ],
            changes=[Change("main.tex", "format", "Layout", diff="-" + credential)],
            tools={"diagnostic": credential},
        )

    def test_json_rich_and_compact_redact_repeated_credential_values(self):
        credential, report = self.report()
        for rendered in (
            json.dumps(report.to_dict()),
            render_terminal(report, show_diff=True, show_passed=True),
            render_compact(report, show_passed=True),
        ):
            self.assertNotIn(credential, rendered)
            self.assertIn("REDACTED", rendered)
            self.assertIn("TEX001", rendered)
        self.assertEqual(report.findings[0].details["Title"], credential)
        self.assertNotIn(credential, str(report.redacted_copy()))

    def test_cli_stdout_and_saved_report_use_the_same_sanitized_evidence(self):
        credential, report = self.report()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "input"
            source.mkdir()
            for output_format in ("terminal", "compact", "json"):
                destination = root / (output_format + ".json")
                with patch("latexprep.cli.run_job", new=AsyncMock(return_value=report)):
                    response = CliRunner().invoke(
                        cli,
                        [
                            "inspect",
                            str(source),
                            "--output-format",
                            output_format,
                            "--quiet",
                            "--report",
                            str(destination),
                            "--show-passed",
                        ],
                    )
                self.assertEqual(response.exit_code, 1, response.output)
                self.assertNotIn(credential, response.output)
                self.assertNotIn(credential, destination.read_text())
                self.assertEqual(
                    json.loads(destination.read_text())["outcome"], "passed_with_advisories"
                )

    def test_redaction_limits_do_not_fall_back_to_raw_report_details(self):
        _, report = self.report()
        with patch("latexprep.models.redact_data", return_value="redaction budget reached"):
            result = report.to_dict()
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["settings"], {})
        self.assertEqual(result["tools"], {})
        self.assertEqual(result["findings"][0]["rule"], "report.redaction_limit")

    def test_common_credential_text_never_corrupts_operational_status_or_check_codes(self):
        report = Report(
            "check",
            "passed_with_advisories",
            "fixture",
            settings={"password": "passed"},
            findings=[Finding("pdf.metadata_agreement", "passed", "info", "passed")],
        )
        result = report.to_dict()
        self.assertEqual(result["outcome"], "passed_with_advisories")
        self.assertEqual(result["findings"][0]["code"], "PDF207")
        self.assertEqual(result["findings"][0]["status"], "passed")
        self.assertEqual(result["settings"]["password"], "[REDACTED]")
        self.assertEqual(result["findings"][0]["message"], "[REDACTED]")
        # If a credential collides with schema keys, return a safe error report.
        with patch("latexprep.models.redact_data", return_value={"[REDACTED]": []}):
            fallback = report.to_dict()
        self.assertEqual(fallback["outcome"], "error")

    def test_nested_stage_key_collision_returns_a_safe_error_report(self):
        report = Report(
            "prepare",
            "passed",
            "fixture",
            settings={"password": "name"},
            stages=[{"name": "baseline", "status": "succeeded"}],
        )
        public = report.to_dict()
        self.assertEqual(public["outcome"], "error")
        self.assertEqual(public["stages"], [])
        self.assertIn("raw details were withheld", public["findings"][0]["message"])
        self.assertIn("report.redaction_limit", render_compact(report))


class ReportPublicationSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_artifact_path_redaction_failure_blocks_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "input"
            source_project(source)
            output = root / "out?token=findings&"
            with (
                patch("latexprep.core.build_project", side_effect=controlled_build),
                patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
                patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[])),
            ):
                report = await run_job(JobRequest("prepare", source, Settings(), output))
            self.assertEqual(report.outcome, "error")
            self.assertFalse(output.exists())
            self.assertEqual(report.artifacts, {})
            self.assertTrue((source / "main.tex").exists())
            self.assertIn("report.redaction_limit", {item.rule for item in report.findings})

    async def test_api_and_published_report_redact_configured_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "input"
            source_project(source)
            credential = "SyntheticCredentialValue_1234"
            settings = Settings(
                online_checks=OnlineOptions(
                    online_replication_urls=("https://example.org/?token=" + credential,),
                )
            )
            with (
                patch("latexprep.core.build_project", side_effect=controlled_build),
                patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
                patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[])),
            ):
                report = await run_job(JobRequest("prepare", source, settings, root / "out"))
            self.assertEqual(report.outcome, "passed", report.to_dict())
            self.assertNotIn(credential, str(report.settings))
            self.assertNotIn(credential, (root / "out/report.json").read_text())
            self.assertTrue((root / "out/submission.zip").is_file())
