"""Exercise report data across preparation, CLI output, and scratch cleanup."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from latexprep.cli import cli
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.findings import Finding
from latexprep.html_report import render_html
from latexprep.job_context import JobRequest
from latexprep.pdf_previews import PdfPreviewStore, PreviewPage, PreviewSide
from latexprep.report import Change, ChangeKind, Outcome, Report
from latexprep.workflow_options import DocumentOptions, WorkflowOptions
from tests.support import (
    PNG,
    AsyncProjectTestCase,
    RecordedBuild,
    controlled_backends,
    directory_contents,
)


def captured_difference(store, stage="baseline versus prepared", document=None, *, collector=None):
    """Record one captured page pair whose text changed but whose rendering did not.

    Both sides are one-pixel PNG images of a letter-size page, and the collector is marked
    finished.

    Args:
        store: The preview store that makes the collector when ``collector`` is ``None``.
        stage: The comparison stage of a new collector.
        document: The document of a new collector, or ``None``.
        collector: The collector to record into, or ``None`` for a new one from ``store``.

    Returns:
        The collector.
    """
    collector = collector or store.for_comparison(stage, document)
    slot = collector.store.reserve_page()
    assert slot is not None
    sides = {
        side: PreviewSide(
            True,
            (612, 792),
            "captured",
            image_id=collector.store.add_image(slot, side, PNG),
            pixels=(1, 1),
        )
        for side in ("left", "right")
    }
    collector.pages.append(
        PreviewPage(
            1,
            text_changed=True,
            rendering_changed=False,
            text_only=True,
            page_dimensions_changed=False,
            page_count_changed=False,
            within_render_tolerance=None,
            left=sides["left"],
            right=sides["right"],
            status="captured",
        )
    )
    collector.finished = True
    return collector


class PreparationInventoryTests(AsyncProjectTestCase):
    """The package inventory and scratch cleanup of a preparation run.

    A verified inventory has exact ZIP sizes and flat names, a dry run claims neither the final
    layout nor a verified ZIP, a missing build trace releases nothing, documents have their own
    inventories, and a blocked comparison keeps its preview images after scratch cleanup.
    """

    async def asyncSetUp(self):
        """Write a paper with one included section and unused notes, and record its contents."""
        self.source = self.root / "paper"
        (self.source / "sections").mkdir(parents=True)
        (self.source / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}\n\\input{sections/body}\n\\end{document}\n"
        )
        (self.source / "sections/body.tex").write_text("A short paper.\n")
        (self.source / "notes.txt").write_text("Private unused notes.\n")
        self.original = directory_contents(self.source)

    async def prepare(
        self, *, settings=None, dry_run=False, trace=True, previews=None, compare=None
    ):
        """Prepare the test paper with a recorded build and return the report.

        The test fails if the preparation changed the source directory.

        Args:
            settings: The settings, or ``None`` for the defaults with ``main.tex`` as the main file.
            dry_run: Whether to prepare without an output directory.
            trace: Whether each build traces ``main.tex`` and ``sections/body.tex`` as its project
                inputs; without a trace a build reports none.
            previews: The PDF preview store, or ``None``.
            compare: The side effect of the PDF comparison, or ``None`` for a comparison without
                findings.

        Returns:
            The report of the job.
        """
        selected = {"main.tex", "sections/body.tex"} if trace else None
        build = RecordedBuild({"main.tex": (selected,) * 4})
        backends = controlled_backends(
            build=build.__call__, compare_pdfs=AsyncMock(side_effect=compare, return_value=[])
        )
        report = await run_job(
            JobRequest(
                "prepare",
                self.source,
                settings or Settings(main="main.tex"),
                None if dry_run else self.root / "out",
                dry_run=dry_run,
                pdf_previews=previews,
            ),
            backends=backends,
        )
        self.assertEqual(directory_contents(self.source), self.original)
        return report

    async def test_verified_inventory_uses_exact_zip_sizes_and_flat_names(self):
        report = await self.prepare(settings=Settings(main="main.tex", layout="flat"))
        self.assertIn(report.outcome, {"passed", "passed_with_advisories"}, report.findings)
        package = report.package_contents[0]
        self.assertEqual(package["status"], "verified")
        included = {row["path"]: row for row in package["files"] if row["status"] == "included"}
        archive_path = self.root / "out/submission.zip"
        with zipfile.ZipFile(archive_path) as archive:
            self.assertEqual(set(included), set(archive.namelist()))
            for member in archive.infolist():
                self.assertEqual(included[member.filename]["size_bytes"], member.file_size)
                self.assertEqual(
                    included[member.filename]["compressed_bytes"], member.compress_size
                )
            compressed = sum(member.compress_size for member in archive.infolist())
        self.assertEqual(package["archive_bytes"], archive_path.stat().st_size)
        self.assertEqual(package["overhead_bytes"], package["archive_bytes"] - compressed)
        body = next(row for row in included.values() if row["source"] == "sections/body.tex")
        self.assertNotIn("/", body["path"])
        notes = next(row for row in package["files"] if row["source"] == "notes.txt")
        self.assertEqual(notes["status"], "excluded")
        self.assertTrue(notes["reasons"])
        saved = json.loads((self.root / "out/report.json").read_text())
        self.assertEqual(saved["package_contents"], report.to_dict()["package_contents"])

    async def test_dry_run_inventory_does_not_imply_final_layout_or_zip_verification(self):
        report = await self.prepare(settings=Settings(main="main.tex", layout="flat"), dry_run=True)
        self.assertEqual(report.outcome, "planned", report.findings)
        package = report.package_contents[0]
        self.assertEqual(package["status"], "proposed")
        self.assertIsNone(package["archive_bytes"])
        self.assertTrue(all(row["compressed_bytes"] is None for row in package["files"]))
        self.assertFalse((self.root / "out").exists())

    async def test_missing_build_trace_is_incomplete_and_releases_nothing(self):
        report = await self.prepare(trace=False)
        self.assertEqual(report.outcome, "blocked")
        self.assertEqual(report.package_contents[0]["status"], "incomplete")
        self.assertEqual(report.artifacts, {})
        self.assertFalse((self.root / "out").exists())

    async def test_multiple_documents_have_independent_inventory_and_saved_reports(self):
        settings = Settings(
            workflow=WorkflowOptions(
                documents=(
                    DocumentOptions("paper", "main.tex"),
                    DocumentOptions("copy", "main.tex"),
                )
            )
        )
        # Six builds share this recorder, one initial/transformed/archive build per document.
        selected = {"main.tex", "sections/body.tex"}
        build = RecordedBuild({"main.tex": (selected,) * 6})
        backends = controlled_backends(build=build.__call__)
        report = await run_job(
            JobRequest("prepare", self.source, settings, self.root / "out"), backends=backends
        )
        self.assertEqual({row["document"] for row in report.package_contents}, {"paper", "copy"})
        for package in report.package_contents:
            self.assertEqual(package["status"], "verified")
            saved = json.loads(
                (self.root / "out" / package["document"] / "report.json").read_text()
            )
            self.assertEqual(saved["package_contents"], [package])
        self.assertEqual(directory_contents(self.source), self.original)

    async def test_blocked_comparison_keeps_images_after_scratch_cleanup_without_releasing_zip(
        self,
    ):
        store = PdfPreviewStore()
        scratch = []

        async def changed(_left, _right, work, _runner, **kwargs):
            scratch.append(work.parent)
            collector = kwargs["previews"]
            captured_difference(store, collector=collector)
            return [Finding("compare.text", "Extracted text differs", severity="error")]

        report = await self.prepare(previews=store, compare=changed)
        self.assertEqual(report.outcome, "blocked")
        self.assertTrue(scratch)
        self.assertTrue(all(not path.exists() for path in scratch))
        self.assertFalse((self.root / "out").exists())
        self.assertEqual(report.artifacts, {})
        self.assertEqual(report.execution["pdf_previews"]["captured_page_pairs"], 1)
        html = render_html(report, preview_images=store.images)
        self.assertIn("data:image/png;base64,", html)
        self.assertIn("Extracted text differs although the rendered pages match", html)
        self.assertIn("Difference overlay", html)
        self.assertNotIn("data:image", json.dumps(report.to_dict()))


class ReportOutputTests(unittest.TestCase):
    """Report output: credential redaction, HTML escaping and preview images, and clean CLI JSON."""

    def test_default_import_capacity_retains_inventory_and_redacts_late_credentials(self):
        secret = "ghp_" + "Ab12" * 10
        report = Report("prepare", outcome=Outcome.PASSED, scope=f"A prior occurrence: {secret}")
        files = []
        for number in range(10_000):
            name = f"notes-{number}.txt"
            report.changes.append(
                Change(name, kind=ChangeKind.REMOVE, reason="Not selected for the submission.")
            )
            files.append(
                {
                    "source": name,
                    "path": name,
                    "status": "excluded",
                    "reasons": ["Not selected for the submission."],
                    "size_bytes": 20,
                    "compressed_bytes": None,
                }
            )
        files[-1]["reasons"] = [f"Excluded source contains {secret}"]
        report.package_contents = [{"document": "main.tex", "status": "verified", "files": files}]
        public = report.to_dict()
        self.assertEqual(public["outcome"], "passed")
        self.assertEqual(len(public["package_contents"][0]["files"]), 10_000)
        self.assertNotIn(secret, json.dumps(public))
        self.assertIn("[REDACTED]", public["scope"])

    def test_html_escapes_descriptors_and_accepts_only_image_bytes_from_explicit_store(self):
        store = PdfPreviewStore()
        collector = captured_difference(store, "<script>alert(1)</script>", "paper&notes")
        report = Report("prepare", execution={"pdf_previews": store.summary()})
        html = render_html(report, preview_images=store.images)
        self.assertIn("&lt;script&gt;", html)
        self.assertNotIn("<script>", html)
        self.assertIn("img-src data:", html)
        self.assertIn('aria-label="Changed pages"', html)
        self.assertNotIn("<img ", render_html(report))
        invalid = dict.fromkeys(store.images, b"<svg onload='bad()'>")
        self.assertNotIn("<img ", render_html(report, preview_images=invalid))
        collector.pages[0].left = PreviewSide(False, None, "missing")
        collector.pages[0].page_dimensions_changed = True
        report.execution["pdf_previews"] = store.summary()
        html = render_html(report, preview_images=store.images)
        self.assertIn("This page does not exist", html)
        self.assertIn("Page dimensions differ", html)
        self.assertNotIn("<summary>Difference overlay", html)

    def test_cli_only_collects_previews_when_html_is_requested_and_keeps_json_clean(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "paper"
            source.mkdir()
            (source / "main.tex").write_text("\\documentclass{article}")
            observed = []

            async def job(request, _progress, report):
                observed.append(request.pdf_previews is not None)
                report.outcome = Outcome.BLOCKED
                if request.pdf_previews is not None:
                    captured_difference(request.pdf_previews)
                    report.execution["pdf_previews"] = request.pdf_previews.summary()
                return report

            runner = CliRunner()
            with patch("latexprep.cli.run_job", side_effect=job):
                ordinary = runner.invoke(cli, ["check", str(source), "--json", "--quiet"])
                enhanced = runner.invoke(
                    cli,
                    [
                        "check",
                        str(source),
                        "--json",
                        "--quiet",
                        "--html-report",
                        str(root / "report.html"),
                    ],
                )
            self.assertEqual(observed, [False, True])
            self.assertEqual(ordinary.exit_code, 3, ordinary.output)
            self.assertEqual(enhanced.exit_code, 3, enhanced.output)
            self.assertEqual(enhanced.stderr, "")
            self.assertEqual(json.loads(enhanced.stdout)["outcome"], "blocked")
            self.assertNotIn("data:image", enhanced.stdout)
            self.assertIn("data:image/png;base64,", (root / "report.html").read_text())


if __name__ == "__main__":
    unittest.main()
