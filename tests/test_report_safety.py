"""Credentials must not escape through another report section or output format."""

from __future__ import annotations

import io
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from latexprep.ci_annotations import render_ci_annotations
from latexprep.cli import cli
from latexprep.compact_report import render_compact
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.diagnostic_bundle import export_diagnostic_bundle
from latexprep.finding_view import RenderOptions
from latexprep.findings import Finding
from latexprep.html_report import render_html
from latexprep.job_context import JobRequest
from latexprep.online_checks import OnlineOptions
from latexprep.pdf_artwork_options import PdfArtworkOptions
from latexprep.report import Change, ChangeKind, Outcome, Report
from latexprep.terminal_report import render_terminal
from tests.test_workflow_checks import controlled_build, source_project


class ReportSafetyTests(unittest.TestCase):
    def report(self):
        credential = "SyntheticCredentialValue_1234"
        return credential, Report(
            "check",
            outcome=Outcome.PASSED_WITH_ADVISORIES,
            scope="fixture",
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
                    severity="info",
                    status="passed",
                    details={"Title": credential},
                ),
                Finding(
                    "source.edit_marker",
                    "Review " + credential,
                    path="main.tex",
                    suggestion="Remove " + credential,
                ),
            ],
            changes=[
                Change("main.tex", kind=ChangeKind.FORMAT, reason="Layout", diff="-" + credential)
            ],
            tools={"diagnostic": credential},
        )

    def test_json_rich_and_compact_redact_repeated_credential_values(self):
        credential, report = self.report()
        for rendered in (
            json.dumps(report.to_dict()),
            render_terminal(report, options=RenderOptions(show_diff=True, show_passed=True)),
            render_compact(report, options=RenderOptions(show_passed=True)),
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
        with patch("latexprep.report.redact_data", return_value="redaction budget reached"):
            result = report.to_dict()
        self.assertEqual(result["outcome"], "error")
        self.assertEqual(result["settings"], {})
        self.assertEqual(result["tools"], {})
        self.assertEqual(result["findings"][0]["rule"], "report.redaction_limit")

    def test_common_credential_text_never_corrupts_operational_status_or_check_codes(self):
        report = Report(
            "check",
            outcome=Outcome.PASSED_WITH_ADVISORIES,
            scope="fixture",
            settings={"password": "passed"},
            findings=[
                Finding("pdf.metadata_agreement", "passed", severity="info", status="passed")
            ],
        )
        result = report.to_dict()
        self.assertEqual(result["outcome"], "passed_with_advisories")
        self.assertEqual(result["findings"][0]["code"], "PDF207")
        self.assertEqual(result["findings"][0]["status"], "passed")
        self.assertEqual(result["settings"]["password"], "[REDACTED]")
        self.assertEqual(result["findings"][0]["message"], "[REDACTED]")
        # If a credential collides with schema keys, return a safe error report.
        with patch("latexprep.report.redact_data", return_value={"[REDACTED]": []}):
            fallback = report.to_dict()
        self.assertEqual(fallback["outcome"], "error")

    def test_nested_stage_key_collision_returns_a_safe_error_report(self):
        report = Report(
            "prepare",
            outcome=Outcome.PASSED,
            scope="fixture",
            settings={"password": "name"},
            stages=[{"name": "baseline", "status": "succeeded"}],
        )
        public = report.to_dict()
        self.assertEqual(public["outcome"], "error")
        self.assertEqual(public["stages"], [])
        self.assertIn("raw details were withheld", public["findings"][0]["message"])
        self.assertIn("report.redaction_limit", render_compact(report))


class WithheldReportTests(unittest.TestCase):
    """A withheld report says whether it was too large or sanitizing failed, never why in detail."""

    def report(self) -> Report:
        return Report("check", outcome=Outcome.PASSED, scope="fixture")

    def test_oversized_report_records_the_redaction_limit(self):
        with patch("latexprep.report.redact_data", return_value="redaction budget reached"):
            result = self.report().to_dict()
        [finding] = result["findings"]
        self.assertEqual(finding["details"], {"cause": "redaction_limit"})
        self.assertIn("within redaction limits", finding["message"])

    def test_structural_failure_records_only_the_error_type(self):
        with patch("latexprep.report.redact_data", return_value={"[REDACTED]": []}):
            result = self.report().to_dict()
        [finding] = result["findings"]
        self.assertEqual(finding["details"], {"cause": "KeyError"})
        self.assertIn("cause: KeyError", finding["message"])
        self.assertNotIn("within redaction limits", finding["message"])

    def test_withheld_report_keeps_the_command_so_it_matches_the_schema(self):
        with patch("latexprep.report.redact_data", return_value="redaction budget reached"):
            self.assertEqual(self.report().to_dict()["command"], "check")


class LocalPathTests(unittest.TestCase):
    """Shared reports do not reveal the user name or the directory layout of a machine."""

    def setUp(self):
        self.home = str(Path.home().resolve())
        self.temporary = str(Path(tempfile.gettempdir()).resolve())
        self.report = Report(
            "check",
            outcome=Outcome.PASSED_WITH_ADVISORIES,
            scope="fixture",
            settings={"reference_pdf": f"{self.home}/paper/reference.pdf"},
            tools={"latexmk": {"executable": f"{self.home}/.local/bin/latexmk"}},
            findings=[
                Finding(
                    "source.edit_marker",
                    f"Cannot read {self.home}/paper/extra.tex and {self.temporary}/scratch/a.log",
                    path="main.tex",
                    suggestion=f"Copy {self.home}/paper/fledge.toml",
                )
            ],
            artifacts={"report": f"{self.home}/out/report.json"},
            execution={"config_path": f"{self.home}/paper/fledge.toml"},
        )

    def test_json_shows_home_and_temporary_directories_generically(self):
        result = self.report.to_dict()
        self.assertEqual(result["execution"]["config_path"], "~/paper/fledge.toml")
        self.assertEqual(result["settings"]["reference_pdf"], "~/paper/reference.pdf")
        self.assertEqual(result["tools"]["latexmk"]["executable"], "~/.local/bin/latexmk")
        [finding] = result["findings"]
        self.assertEqual(
            finding["message"], "Cannot read ~/paper/extra.tex and <tmp>/scratch/a.log"
        )
        self.assertEqual(finding["next_step"], "Copy ~/paper/fledge.toml")
        self.assertEqual(finding["path"], "main.tex")

    def test_published_artifact_paths_keep_their_location(self):
        self.assertEqual(
            self.report.to_dict()["artifacts"], {"report": f"{self.home}/out/report.json"}
        )
        self.assertEqual(
            self.report.redacted_copy().artifacts, {"report": f"{self.home}/out/report.json"}
        )

    def test_every_other_output_format_is_scrubbed(self):
        outputs = {
            "terminal": render_terminal(self.report, options=RenderOptions(show_passed=True)),
            "compact": render_compact(self.report),
            "html": render_html(self.report),
            "ci": render_ci_annotations(self.report),
        }
        for name, text in outputs.items():
            with self.subTest(format=name):
                self.assertNotIn(f"{self.home}/paper", text)
                self.assertNotIn(f"{self.home}/.local", text)
                self.assertNotIn(self.temporary, text)
                self.assertIn("~/paper", text)

    def test_diagnostic_bundle_is_scrubbed(self):
        with tempfile.TemporaryDirectory() as directory:
            destination = Path(directory) / "bundle.zip"
            export_diagnostic_bundle(self.report, destination)
            with zipfile.ZipFile(io.BytesIO(destination.read_bytes())) as archive:
                for name in ("report.json", "report.html"):
                    with self.subTest(entry=name):
                        text = archive.read(name).decode("utf-8")
                        self.assertNotIn(f"{self.home}/paper", text)
                        self.assertNotIn(f"{self.home}/.local", text)
                        self.assertIn("~/paper/fledge.toml", text)


class ReportPublicationSafetyTests(unittest.IsolatedAsyncioTestCase):
    async def test_artifact_path_redaction_failure_blocks_before_creating_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "input"
            source_project(source)
            output = root / "out?token=findings&"
            with (
                patch("latexprep.document_tasks.build_project", side_effect=controlled_build),
                patch("latexprep.document_tasks.inspect_pdf", new=AsyncMock(return_value=[])),
                patch("latexprep.document_tasks.compare_pdfs", new=AsyncMock(return_value=[])),
            ):
                report = await run_job(JobRequest("prepare", source, Settings(), output))
            self.assertEqual(report.outcome, "error")
            self.assertFalse(output.exists())
            self.assertEqual(report.artifacts, {})
            self.assertTrue((source / "main.tex").exists())
            self.assertIn("report.redaction_limit", {item.rule for item in report.findings})

    async def test_redaction_refusal_leaves_no_references_to_unpublished_previews(self):
        async def measured(_pdf, _root, _main, work, *_args):
            work.mkdir(parents=True)
            preview = work / "gray.png"
            preview.write_bytes(b"PNG fixture")
            return [
                Finding(
                    "pdf.grayscale_preview",
                    "Generated",
                    severity="info",
                    status="passed",
                    details={"artifacts": [str(preview)]},
                )
            ]

        settings = Settings(rendered_pdf_artwork=PdfArtworkOptions(grayscale_preview=True))
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "input"
            source_project(source)
            output = root / "out?token=findings&"
            with (
                patch("latexprep.document_tasks.build_project", side_effect=controlled_build),
                patch("latexprep.document_tasks.inspect_pdf", new=AsyncMock(return_value=[])),
                patch("latexprep.document_tasks.compare_pdfs", new=AsyncMock(return_value=[])),
                patch(
                    "latexprep.document_tasks.inspect_configured_pdf_checks", side_effect=measured
                ),
            ):
                report = await run_job(
                    JobRequest(
                        "prepare", source, settings, output, preview_output=root / "previews"
                    )
                )
            self.assertFalse(output.exists())
            self.assertFalse((root / "previews").exists())
        self.assertEqual(report.outcome, "error")
        self.assertEqual(report.artifacts, {})
        previews = [item for item in report.findings if item.rule == "pdf.grayscale_preview"]
        self.assertTrue(previews)
        for item in previews:
            self.assertEqual(item.details["artifacts"], [])
            self.assertEqual(item.details["previews_exported"], 0)

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
                patch("latexprep.document_tasks.build_project", side_effect=controlled_build),
                patch("latexprep.document_tasks.inspect_pdf", new=AsyncMock(return_value=[])),
                patch("latexprep.document_tasks.compare_pdfs", new=AsyncMock(return_value=[])),
            ):
                report = await run_job(JobRequest("prepare", source, settings, root / "out"))
            self.assertEqual(report.outcome, "passed", report.to_dict())
            self.assertNotIn(credential, str(report.settings))
            self.assertNotIn(credential, (root / "out/report.json").read_text())
            self.assertTrue((root / "out/submission.zip").is_file())
