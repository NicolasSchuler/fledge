"""Transformations and online evidence go through the same verified preparation workflow."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

from latexprep.bibliography_transform import BibliographyTransformOptions
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.findings import Finding
from latexprep.job_context import JobRequest
from latexprep.online_checks import OnlineOptions
from latexprep.source_transform import SourceTransformOptions
from tests.support import controlled_backends, minimal_project


class WorkflowTransformationTests(unittest.IsolatedAsyncioTestCase):
    """Bibliography and source transformations in ``prepare`` jobs, and online reference checks.

    Transformations reach the archive but never the input; online checks see the final
    references once, or the initial snapshot in a dry run.
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
        backends = controlled_backends()
        result = await run_job(
            JobRequest("prepare", self.source, settings, self.base / "out"), backends=backends
        )
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
                    analysed,
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
                            severity="info" if finding_status == "passed" else "warning",
                            status=finding_status,
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
                backends = controlled_backends()
                with patch("latexprep.document_tasks.check_online_references", side_effect=online):
                    result = await run_job(
                        JobRequest("prepare", self.source, settings, output), backends=backends
                    )
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
        backends = controlled_backends()
        with patch("latexprep.document_tasks.check_online_references", side_effect=online):
            result = await run_job(
                JobRequest("prepare", self.source, settings, self.base / "out", dry_run=True),
                backends=backends,
            )
        self.assertEqual(result.outcome, "planned", result.to_dict())
        self.assertEqual(observed, [original])
        self.assertFalse((self.base / "out").exists())


if __name__ == "__main__":
    unittest.main()
