"""New interfaces and transformations use the same verified preparation workflow."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from latexprep.bibliography_transform import BibliographyTransformOptions
from latexprep.check_pipeline import inspect_additional_pdf_checks
from latexprep.cli import cli
from latexprep.config import Settings, load_settings
from latexprep.core import JobRequest, run_job
from latexprep.models import Finding
from latexprep.online_checks import OnlineOptions
from latexprep.pdf_artwork import PdfArtworkOptions
from latexprep.reporting import ReportingOptions, ReviewRule
from latexprep.runtime import ToolRunner
from latexprep.scheduler import ResourceBudget
from latexprep.source_transform import SourceTransformOptions
from latexprep.workflow_options import DocumentOptions, WorkflowOptions
from tests.test_multi_document import build, project


class WorkflowExtensionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "input"
        project(self.source)

    async def test_selected_transformations_apply_to_archive_without_touching_input(self):
        text = (
            "\\documentclass{article}\n% private TODO note\n\\begin{document}\n"
            "\\cite{used}\n\\bibliography{refs}\n\\end{document}\n"
        )
        (self.source / "main.tex").write_text(text)
        bib = (
            "@article{used,title={A {GPU} study},note={private},year={2020}}\n"
            "@article{unused,title={Unused}}\n"
        )
        (self.source / "refs.bib").write_text(bib)
        settings = Settings(
            bibliography_transform=BibliographyTransformOptions(
                format_entries=True, remove_fields=("note",), cited_only=True
            ),
            source_transforms=SourceTransformOptions(comment_policy="private"),
        )
        with (
            patch("latexprep.core.build_project", side_effect=build),
            patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
            patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[])),
        ):
            result = await run_job(JobRequest("prepare", self.source, settings, self.base / "out"))
        self.assertIn(result.outcome, {"passed", "passed_with_advisories"}, result.to_dict())
        with zipfile.ZipFile(self.base / "out/submission.zip") as archive:
            prepared = archive.read("refs.bib").decode()
            self.assertNotIn("unused", prepared)
            self.assertNotIn("note", prepared)
            self.assertIn("{GPU}", prepared)
            self.assertNotIn("private TODO", archive.read("main.tex").decode())
        self.assertEqual((self.source / "main.tex").read_text(), text)
        self.assertEqual((self.source / "refs.bib").read_text(), bib)

    async def test_online_evidence_uses_final_references_once_and_preserves_input(self):
        (self.source / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\cite{entry}\\bibliography{refs}\n\\end{document}\n"
        )
        original = "@misc{entry,title={An example},doi={10.1234/old}}\n"
        (self.source / "refs.bib").write_text(original)
        for replacement, status in (
            ("10.1234/old", "passed"),
            ("10.1234/new", "failed"),
            ("10.1234/new", "inconclusive"),
        ):
            with self.subTest(replacement=replacement, status=status):
                observed = []
                output = self.base / status

                async def online(
                    root,
                    main,
                    *,
                    options,
                    budget,
                    seen=observed,
                    finding_status=status,
                    doi=replacement,
                ):
                    self.assertEqual(main, "main.tex")
                    self.assertEqual(options.online_max_requests, 1)
                    seen.append((root / "refs.bib").read_text())
                    return [
                        Finding(
                            "online.doi_resolution",
                            "Controlled online result for the inspected DOI",
                            "info" if finding_status == "passed" else "warning",
                            finding_status,
                            details={"doi": doi},
                        )
                    ]

                settings = Settings(
                    bibliography_transform=BibliographyTransformOptions(
                        field_edits=(("entry", "doi", "10.1234/old", replacement),)
                    ),
                    online_checks=OnlineOptions(
                        online=True, online_doi_resolution=True, online_max_requests=1
                    ),
                )
                with (
                    patch("latexprep.core.build_project", side_effect=build),
                    patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
                    patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[])),
                    patch("latexprep.core.check_online_references", side_effect=online),
                ):
                    result = await run_job(JobRequest("prepare", self.source, settings, output))
                self.assertEqual(
                    result.outcome,
                    "passed" if status == "passed" else "passed_with_advisories",
                    result.to_dict(),
                )
                with zipfile.ZipFile(output / "submission.zip") as archive:
                    prepared = archive.read("refs.bib").decode()
                self.assertEqual(observed, [prepared])
                self.assertIn(replacement, prepared)
                self.assertEqual((self.source / "refs.bib").read_text(), original)
                findings = [item for item in result.findings if item.code == "NET001"]
                self.assertEqual(len(findings), 1)
                self.assertEqual(findings[0].status, status)
                self.assertEqual(findings[0].details["stage"], "final online reference checks")

    async def test_preparation_dry_run_keeps_online_evidence_on_initial_snapshot(self):
        (self.source / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "\\cite{entry}\\bibliography{refs}\n\\end{document}\n"
        )
        original = "@misc{entry,title={An example},doi={10.1234/old}}\n"
        (self.source / "refs.bib").write_text(original)
        observed = []

        async def online(root, _main, **_kwargs):
            observed.append((root / "refs.bib").read_text())
            return []

        settings = Settings(
            bibliography_transform=BibliographyTransformOptions(
                field_edits=(("entry", "doi", "10.1234/old", "10.1234/new"),)
            ),
            online_checks=OnlineOptions(online=True, online_doi_resolution=True),
        )
        with (
            patch("latexprep.core.build_project", side_effect=build),
            patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
            patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[])),
            patch("latexprep.core.check_online_references", side_effect=online),
        ):
            result = await run_job(
                JobRequest("prepare", self.source, settings, self.base / "out", dry_run=True)
            )
        self.assertEqual(result.outcome, "planned", result.to_dict())
        self.assertEqual(observed, [original])
        self.assertFalse((self.base / "out").exists())

    async def test_reviewed_page_policy_keeps_failure_visible_and_marks_output(self):
        settings = Settings(
            reporting=ReportingOptions(
                accepted_exceptions=(ReviewRule("PDF001", "Approved extra page"),)
            )
        )
        failure = Finding("pdf.page_limit", "Too many pages", "error")
        with (
            patch("latexprep.core.build_project", side_effect=build),
            patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[failure])),
            patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[])),
        ):
            result = await run_job(JobRequest("prepare", self.source, settings, self.base / "out"))
        self.assertEqual(result.outcome, "accepted_exceptions", result.to_dict())
        findings = [item for item in result.findings if item.code == "PDF001"]
        self.assertTrue(findings)
        self.assertTrue(
            all(item.status == "failed" and item.details.get("review") for item in findings)
        )
        saved = json.loads((self.base / "out/report.json").read_text())
        self.assertEqual(saved["outcome"], "accepted_exceptions")

    async def test_archive_mismatch_cannot_be_waived(self):
        settings = Settings(
            reporting=ReportingOptions(
                accepted_exceptions=(
                    ReviewRule(
                        "CMP003", "Intended new appearance", stage="baseline versus prepared"
                    ),
                )
            )
        )
        changed = Finding("compare.rendering", "Pixels differ", "error")
        with (
            patch("latexprep.core.build_project", side_effect=build),
            patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
            patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[changed])),
        ):
            result = await run_job(JobRequest("prepare", self.source, settings, self.base / "out"))
        self.assertEqual(result.outcome, "blocked")
        self.assertFalse((self.base / "out").exists())
        self.assertTrue(
            any(
                item.details.get("stage") == "prepared versus archive rebuild"
                and item.status == "failed"
                for item in result.findings
            )
        )

    async def test_standalone_pdf_does_not_import_or_build_sources(self):
        pdf = self.base / "paper.pdf"
        pdf.write_bytes(b"%PDF-fixture")
        with (
            patch("latexprep.core.import_project", side_effect=AssertionError("source import")),
            patch("latexprep.core.build_project", side_effect=AssertionError("build")),
            patch(
                "latexprep.core.inspect_standalone_pdf",
                new=AsyncMock(
                    return_value=[Finding("pdf.page_limit", "Count okay", "info", "passed")]
                ),
            ),
        ):
            result = await run_job(JobRequest("pdf", pdf, Settings()))
        self.assertEqual(result.outcome, "passed")
        self.assertIn("source and submission bundle were not verified", result.scope)

    async def test_preview_only_needs_no_figure_source_inventory(self):
        (self.source / "main.tex").write_text(
            r"\documentclass{article}\begin{document}\includegraphics{\dynamic}\end{document}"
        )
        with (
            patch(
                "latexprep.check_pipeline._figure_inputs",
                side_effect=AssertionError("unneeded figure graph"),
            ),
            patch("latexprep.check_pipeline.inspect_pdf_artwork", new=AsyncMock(return_value=[])),
        ):
            result = await inspect_additional_pdf_checks(
                self.base / "paper.pdf",
                self.source,
                "main.tex",
                self.base / "work",
                ToolRunner(),
                Settings(pdf_artwork=PdfArtworkOptions(grayscale_preview=True)),
                ResourceBudget(2, 2048),
            )
        self.assertFalse(any(item.rule == "pdf.artwork_input_scope" for item in result))

    async def test_standalone_previews_survive_temporary_workspace(self):
        pdf = self.base / "paper.pdf"
        pdf.write_bytes(b"%PDF-fixture")

        async def measured(_pdf, work, *_args):
            work.mkdir(parents=True)
            preview = work / "gray.png"
            preview.write_bytes(b"PNG fixture")
            return [
                Finding(
                    "pdf.grayscale_preview",
                    "Generated",
                    "info",
                    "passed",
                    details={"artifacts": [str(preview)]},
                )
            ]

        settings = Settings(pdf_artwork=PdfArtworkOptions(grayscale_preview=True))
        with patch("latexprep.core.inspect_standalone_pdf", side_effect=measured):
            result = await run_job(
                JobRequest("pdf", pdf, settings, preview_output=self.base / "previews")
            )
        self.assertEqual(result.outcome, "passed")
        preview = Path(result.artifacts["preview.1"])
        self.assertEqual(preview.read_bytes(), b"PNG fixture")
        self.assertEqual(result.findings[0].details["artifacts"], [str(preview)])

    async def test_preview_export_timeout_rolls_back_single_and_multiple_packages(self):
        async def measured(_pdf, _root, _main, work, *_args):
            work.mkdir(parents=True)
            preview = work / "gray.png"
            preview.write_bytes(b"PNG fixture")
            return [
                Finding(
                    "pdf.grayscale_preview",
                    "Generated",
                    "info",
                    "passed",
                    details={"artifacts": [str(preview)]},
                )
            ]

        def fail_preview(previews, _deadline):
            if previews:
                raise TimeoutError

        for multi in (False, True):
            settings = Settings(
                pdf_artwork=PdfArtworkOptions(grayscale_preview=True),
                workflow=WorkflowOptions(documents=(DocumentOptions("paper", "main.tex"),))
                if multi
                else WorkflowOptions(),
            )
            output, previews = self.base / f"out-{multi}", self.base / f"previews-{multi}"
            with (
                patch("latexprep.core.build_project", side_effect=build),
                patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
                patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[])),
                patch("latexprep.core.inspect_additional_pdf_checks", side_effect=measured),
                patch("latexprep.core._copy_previews", side_effect=fail_preview),
            ):
                result = await run_job(
                    JobRequest("prepare", self.source, settings, output, preview_output=previews)
                )
            self.assertEqual(result.outcome, "blocked")
            self.assertFalse(output.exists())
            self.assertFalse(previews.exists())
            self.assertEqual(result.artifacts, {})


class InterfaceExtensionTests(unittest.TestCase):
    def test_toml_field_edit_tables_can_add_or_remove_with_explicit_preconditions(self):
        with tempfile.TemporaryDirectory() as directory:
            config = Path(directory) / "settings.toml"
            config.write_text("""[[bibliography_transform.field_edits]]
key = "paper"
field = "doi"
expected_absent = true
replacement = "10.1234/example"
[[bibliography_transform.field_edits]]
key = "paper"
field = "note"
expected = "private"
remove = true
""")
            options = load_settings(config, {}).bibliography_transform
            self.assertEqual(
                options.field_edits,
                (("paper", "doi", None, "10.1234/example"), ("paper", "note", "private", None)),
            )
            config.write_text("""[[bibliography_transform.field_edits]]
key = "paper"
field = "doi"
replacement = "10.1234/example"
""")
            from latexprep.models import PreparationError

            with self.assertRaises(PreparationError):
                load_settings(config, {})

    def test_offline_html_ci_and_diagnostics_are_accessible_from_cli(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project(base / "input")
            with (base / "input/main.tex").open("a") as stream:
                stream.write("\n% comment only\n")
            result = CliRunner().invoke(
                cli,
                [
                    "inspect",
                    str(base / "input"),
                    "--quiet",
                    "--output-format",
                    "html",
                    "--html-report",
                    str(base / "report.html"),
                    "--diagnostics",
                    str(base / "debug.zip"),
                ],
            )
            self.assertIn(result.exit_code, {0, 1}, result.output)
            self.assertIn("<!doctype html>", result.output.lower())
            self.assertTrue((base / "report.html").is_file())
            with zipfile.ZipFile(base / "debug.zip") as archive:
                self.assertNotIn("submission.zip", archive.namelist())
                self.assertTrue(
                    any(
                        "unverified" in archive.read(name).decode().lower()
                        for name in archive.namelist()
                        if name.endswith(".json")
                    )
                )
            ci = CliRunner().invoke(
                cli, ["inspect", str(base / "input"), "--quiet", "--output-format", "ci"]
            )
            self.assertIn(ci.exit_code, {0, 1}, ci.output)

    def test_nested_transform_document_and_review_configuration_round_trips(self):
        settings = load_settings(
            None,
            {
                "workflow": {
                    "documents": [
                        {"name": "paper", "main": "main.tex", "include": ["*.tex", "*.bib"]}
                    ],
                    "baseline_runs": 2,
                    "comparison": {"channel_tolerance": 1},
                },
                "bibliography_transform": {
                    "remove_fields": ["file"],
                    "key_renames": {"old": "new"},
                },
                "source_transforms": {"comment_policy": "private"},
                "reporting": {
                    "accepted_exceptions": [
                        {"code": "PDF001", "reason": "Approved", "document": "paper"}
                    ]
                },
                "formatting_options": {"exclude": ["generated/*.tex"]},
            },
        )
        self.assertEqual(load_settings(None, settings.to_dict()), settings)
        self.assertEqual(settings.bibliography_transform.key_renames, (("old", "new"),))


if __name__ == "__main__":
    unittest.main()
