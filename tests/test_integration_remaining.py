"""Opt-in real TeX/Poppler verification of the extended preparation workflow."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path

from latexprep.bibliography_transform import BibliographyTransformOptions
from latexprep.config import Settings
from latexprep.core import JobRequest, run_job
from latexprep.source_transform import SourceTransformOptions
from latexprep.workflow_options import DocumentOptions, WorkflowOptions
from tests.support import live_tests_enabled


@unittest.skipUnless(
    live_tests_enabled(),
    "Enable FLEDGE_RUN_INTEGRATION for real restricted TeX tools",
)
class LiveRemainingWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_independent_flat_packages_with_bibliography_and_source_cleanup(self):
        with tempfile.TemporaryDirectory(prefix="latex-prep-remaining-") as directory:
            base = Path(directory).resolve()
            source = base / "input"
            for name in ("paper", "supplement"):
                root = source / name
                (root / "sections").mkdir(parents=True)
                (root / "main.tex").write_text(
                    "\\documentclass{article}\n% private TODO note\n"
                    "\\begin{document}\n\\input{sections/body}\n"
                    "\\bibliographystyle{plain}\n\\bibliography{refs}\n\\end{document}\n"
                )
                (root / "sections/body.tex").write_text(f"{name.title()} cites \\cite{{used}}.\n")
                (root / "refs.bib").write_text(
                    "@article{used,author={Ada Example},title={A {GPU} study},"
                    "journal={Examples},year={2020},file={private/path}}\n"
                    "@article{unused,author={Unused Author},title={Unused},year={2021}}\n"
                )
            originals = {
                path.relative_to(source): path.read_bytes()
                for path in source.rglob("*")
                if path.is_file()
            }
            settings = Settings(
                bibliography_backend="bibtex",
                layout="flat",
                workflow=WorkflowOptions(
                    documents=(
                        DocumentOptions("paper", "main.tex", "paper"),
                        DocumentOptions("supplement", "main.tex", "supplement"),
                    ),
                    baseline_runs=2,
                ),
                bibliography_transform=BibliographyTransformOptions(
                    format_entries=True, remove_fields=("file",), cited_only=True
                ),
                source_transforms=SourceTransformOptions(
                    comment_policy="private", merge_inputs=True
                ),
                job_timeout_seconds=300,
            )
            report = await run_job(JobRequest("prepare", source, settings, base / "output"))
            self.assertIn(report.outcome, {"passed", "passed_with_advisories"}, report.to_dict())
            self.assertEqual(
                originals,
                {
                    path.relative_to(source): path.read_bytes()
                    for path in source.rglob("*")
                    if path.is_file()
                },
            )
            for name in ("paper", "supplement"):
                with zipfile.ZipFile(base / "output" / name / "submission.zip") as archive:
                    self.assertTrue(all("/" not in member for member in archive.namelist()))
                    self.assertNotIn("unused", archive.read("refs.bib").decode())
                    self.assertNotIn("private", archive.read("refs.bib").decode())
                    self.assertNotIn("private TODO", archive.read("main.tex").decode())
                    self.assertNotIn("body.tex", archive.namelist())
                self.assertTrue(
                    (base / "output" / name / "manuscript.pdf").read_bytes().startswith(b"%PDF-")
                )
            for code in (
                "PKG301",
                "CMP101",
                "BLD201",
                "BIB201",
                "BIB202",
                "BIB203",
                "TEX201",
                "TEX202",
            ):
                self.assertTrue(
                    any(item.code == code and item.status == "passed" for item in report.findings),
                    code,
                )


if __name__ == "__main__":
    unittest.main()
