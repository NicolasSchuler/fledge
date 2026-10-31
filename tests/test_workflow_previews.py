"""Standalone PDF checks and grayscale previews through the workflow."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.findings import Finding
from latexprep.job_context import JobRequest, pdf_check_settings
from latexprep.job_execution import JobDeadlineExceeded
from latexprep.pdf_artwork_options import PdfArtworkOptions
from latexprep.pdf_check_pipeline import inspect_configured_pdf_checks
from latexprep.redaction import scrub_local_paths
from latexprep.resource_budget import ResourceBudget
from latexprep.runtime import ToolRunner
from latexprep.workflow_options import DocumentOptions, WorkflowOptions
from tests.support import controlled_backends, minimal_project


class WorkflowPreviewTests(unittest.IsolatedAsyncioTestCase):
    """Standalone ``pdf`` jobs and grayscale previews: what they skip, keep and roll back.

    A standalone PDF check neither imports nor builds sources, a preview needs no inventory of
    figure sources, the previews of a standalone check outlive its temporary workspace, and a
    timeout while previews are exported rolls back the output of single and multiple packages.
    """

    async def asyncSetUp(self):
        """Write a minimal project to ``input`` in a temporary directory removed after the test.

        ``self.base`` is the temporary directory and ``self.source`` the project in it.
        """
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.base = Path(self.temporary.name)
        self.source = self.base / "input"
        minimal_project(self.source)

    async def test_standalone_pdf_does_not_import_or_build_sources(self):
        pdf = self.base / "paper.pdf"
        pdf.write_bytes(b"%PDF-fixture")
        backends = controlled_backends(build=AssertionError("build"))
        with (
            patch(
                "latexprep.job_context.import_project", side_effect=AssertionError("source import")
            ),
            patch(
                "latexprep.standalone_checks.inspect_standalone_pdf",
                new=AsyncMock(
                    return_value=[
                        Finding("pdf.page_limit", "Count okay", severity="info", status="passed")
                    ]
                ),
            ),
        ):
            result = await run_job(JobRequest("pdf", pdf, Settings()), backends=backends)
        self.assertEqual(result.outcome, "passed")
        self.assertIn("source and submission bundle were not verified", result.scope)

    async def test_preview_only_needs_no_figure_source_inventory(self):
        (self.source / "main.tex").write_text(
            r"\documentclass{article}\begin{document}\includegraphics{\dynamic}\end{document}"
        )
        with (
            patch(
                "latexprep.pdf_check_pipeline._figure_inputs",
                side_effect=AssertionError("unneeded figure graph"),
            ),
            patch(
                "latexprep.pdf_check_pipeline.inspect_pdf_artwork", new=AsyncMock(return_value=[])
            ),
        ):
            result = await inspect_configured_pdf_checks(
                self.base / "paper.pdf",
                self.source,
                "main.tex",
                self.base / "work",
                ToolRunner(),
                pdf_check_settings(
                    Settings(rendered_pdf_artwork=PdfArtworkOptions(grayscale_preview=True))
                ),
                ResourceBudget(2, 2048),
            )
        self.assertFalse(any(item.rule == "pdf.artwork_input_scope" for item in result))

    async def test_standalone_previews_survive_temporary_workspace(self):
        pdf = self.base / "paper.pdf"
        pdf.write_bytes(b"%PDF-fixture")

        async def measured(_pdf, work, *_args, **_kwargs):
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
        with patch("latexprep.standalone_checks.inspect_standalone_pdf", side_effect=measured):
            result = await run_job(
                JobRequest("pdf", pdf, settings, preview_output=self.base / "previews")
            )
        self.assertEqual(result.outcome, "passed")
        preview = Path(result.artifacts["preview.1"])
        self.assertEqual(preview.read_bytes(), b"PNG fixture")
        # Findings are part of the shareable report, so the preview's location is shown
        # generically (<tmp>/...); only report.artifacts keeps the real path.
        self.assertEqual(result.findings[0].details["artifacts"], [scrub_local_paths(str(preview))])

    async def test_preview_export_timeout_rolls_back_single_and_multiple_packages(self):
        async def measured(_pdf, _root, _main, work, *_args, **_options):
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

        def fail_preview(previews, _deadline):
            if previews:
                raise JobDeadlineExceeded

        for multi in (False, True):
            settings = Settings(
                rendered_pdf_artwork=PdfArtworkOptions(grayscale_preview=True),
                workflow=WorkflowOptions(documents=(DocumentOptions("paper", "main.tex"),))
                if multi
                else WorkflowOptions(),
            )
            output, previews = self.base / f"out-{multi}", self.base / f"previews-{multi}"
            backends = controlled_backends()
            with (
                patch(
                    "latexprep.document_tasks.inspect_configured_pdf_checks", side_effect=measured
                ),
                patch("latexprep.publication._copy_previews", side_effect=fail_preview),
            ):
                result = await run_job(
                    JobRequest("prepare", self.source, settings, output, preview_output=previews),
                    backends=backends,
                )
            self.assertEqual(result.outcome, "blocked")
            self.assertFalse(output.exists())
            self.assertFalse(previews.exists())
            self.assertEqual(result.artifacts, {})


if __name__ == "__main__":
    unittest.main()
