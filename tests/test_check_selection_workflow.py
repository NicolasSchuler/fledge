"""Check selection changes execution and outcomes without weakening release guards."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from latexprep.check_selection import CheckSelection
from latexprep.config import Settings
from latexprep.core import JobRequest, run_job
from latexprep.models import Finding, Report
from latexprep.online_checks import OnlineOptions
from latexprep.pdf_artwork import PdfArtworkOptions
from latexprep.presentation import render_compact, render_terminal
from latexprep.runtime import build_project
from latexprep.submission_checks import SubmissionOptions
from tests.test_multi_document import build as fixture_build
from tests.test_multi_document import project
from tests.test_runtime import FakeBuildRunner


async def build(*args, **_kwargs):
    return await fixture_build(*args)


class CheckSelectionWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.source = self.root / "source"
        project(self.source)

    async def test_selected_source_check_and_required_dependency_guard_both_run(self):
        main = self.source / "main.tex"
        main.write_text(
            "\\documentclass{article}\n\\begin{document}\n"
            "TODO finish.\\todo{edit}\\input{missing}\n\\end{document}\n"
        )
        report = await run_job(
            JobRequest(
                "inspect",
                self.source,
                Settings(checks=CheckSelection(select=("TEX001",)), require_abstract=True),
            )
        )
        codes = {item.code for item in report.findings}
        self.assertIn("TEX001", codes)
        self.assertIn("TEX005", codes)
        self.assertNotIn("TEX002", codes)
        self.assertNotIn("TEX104", codes)
        self.assertEqual(report.outcome, "blocked")

    async def test_ignored_page_policy_does_not_run_or_block_verified_output(self):
        seen_limits = []

        async def inspection(_pdf, _work, _runner, max_pages, **_kwargs):
            seen_limits.append(max_pages)
            return []

        settings = Settings(max_pages=1, checks=CheckSelection(ignore=("PDF001",)))
        supplied = Report("prepare", execution={"config_path": "latex-prep.toml"})
        with (
            patch("latexprep.core.build_project", side_effect=build),
            patch("latexprep.core.inspect_pdf", side_effect=inspection),
            patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[])),
        ):
            report = await run_job(
                JobRequest("prepare", self.source, settings, self.root / "output"),
                report=supplied,
            )
        self.assertEqual(seen_limits, [None, None])
        self.assertIn(report.outcome, {"passed", "passed_with_advisories"})
        saved = json.loads((self.root / "output/report.json").read_text())
        self.assertEqual(saved["settings"]["max_pages"], 1)
        self.assertEqual(saved["execution"]["config_path"], "latex-prep.toml")
        self.assertEqual(saved["execution"]["check_selection"]["ignore"], ["PDF001"])

    async def test_ignore_all_cannot_release_changed_archive(self):
        failure = Finding("compare.rendering", "Rendered pixels changed", "error")
        comparison = AsyncMock(side_effect=[[], [failure]])
        with (
            patch("latexprep.core.build_project", side_effect=build),
            patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
            patch("latexprep.core.compare_pdfs", new=comparison),
        ):
            report = await run_job(
                JobRequest(
                    "prepare",
                    self.source,
                    Settings(checks=CheckSelection(ignore=("ALL",))),
                    self.root / "output",
                )
            )
        self.assertEqual(report.outcome, "blocked")
        self.assertEqual(comparison.await_count, 2)
        self.assertTrue(any(item.code == "CMP003" for item in report.findings))
        self.assertFalse((self.root / "output").exists())

    async def test_online_selection_is_disabled_before_network_adapter(self):
        seen = []

        async def online(_root, _main, *, options, budget):
            seen.append(options)
            self.assertFalse(options.online_doi_resolution)
            self.assertFalse(options.online_metadata)
            return []

        with patch("latexprep.core.check_online_references", side_effect=online):
            report = await run_job(
                JobRequest(
                    "inspect",
                    self.source,
                    Settings(
                        checks=CheckSelection(select=("TEX",)),
                        online_checks=OnlineOptions(
                            online=True, online_doi_resolution=True, online_metadata=True
                        ),
                    ),
                )
            )
        self.assertEqual(len(seen), 1)
        self.assertEqual(report.outcome, "passed")

    async def test_ignored_shared_parser_advisory_is_absent_before_outcome(self):
        main = self.source / "main.tex"
        main.write_text("\\documentclass{article}\n\\begin{document}TODO.\\end{document}\n")
        report = await run_job(
            JobRequest("inspect", self.source, Settings(checks=CheckSelection(ignore=("TEX001",))))
        )
        self.assertFalse(any(item.code == "TEX001" for item in report.findings))
        self.assertEqual(report.outcome, "passed")
        for rendered in (render_compact(report), render_terminal(report)):
            self.assertIn("ignore TEX001", rendered)
            self.assertIn("required validation retained", rendered)

    async def test_source_and_pdf_identity_checks_have_independent_selection(self):
        (self.source / "main.tex").write_text(
            "\\documentclass{article}\n\\begin{document}Example Author.\\end{document}\n"
        )
        for selected in ("PRV001", "PRV002"):
            with self.subTest(selected=selected):
                extract = AsyncMock(return_value=("Example Author", {"Pages": "1"}))
                with (
                    patch("latexprep.core.build_project", side_effect=build),
                    patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
                    patch("latexprep.check_pipeline.extract_pdf_evidence", new=extract),
                ):
                    report = await run_job(
                        JobRequest(
                            "check",
                            self.source,
                            Settings(
                                checks=CheckSelection(select=(selected,)),
                                submission_checks=SubmissionOptions(
                                    identity_terms=("Example Author",)
                                ),
                            ),
                        )
                    )
                privacy = {
                    item.code for item in report.findings if item.code in {"PRV001", "PRV002"}
                }
                self.assertEqual(privacy, {selected})
                self.assertEqual(extract.await_count, int(selected == "PRV002"))

    async def test_build_artifact_success_uses_selection_without_hiding_raw_evidence(self):
        for code, log in (
            ("BLD001", "LaTeX Warning: Citation `missing' undefined."),
            ("BLD002", "LaTeX Warning: Reference `missing' undefined."),
        ):
            for ignored in (False, True):
                with self.subTest(code=code, ignored=ignored):
                    builds = []

                    async def measured(
                        tree, main, work, engine, _runner, *, _log=log, _builds=builds, **kwargs
                    ):
                        result = await build_project(
                            tree, main, work, engine, FakeBuildRunner(log=_log), **kwargs
                        )
                        _builds.append(result)
                        return result

                    inspection = AsyncMock(return_value=[])
                    with (
                        patch("latexprep.core.build_project", side_effect=measured),
                        patch("latexprep.core.inspect_pdf", new=inspection),
                    ):
                        report = await run_job(
                            JobRequest(
                                "check",
                                self.source,
                                Settings(checks=CheckSelection(ignore=(code,) if ignored else ())),
                            )
                        )
                    self.assertEqual(report.outcome, "passed" if ignored else "blocked")
                    self.assertEqual(builds[0].success, ignored)
                    self.assertTrue(any(item.code == code for item in builds[0].findings))
                    self.assertEqual(inspection.await_count, int(ignored))

    async def test_ignore_all_retains_tex_rerun_and_execution_failures(self):
        for log, returncode in (
            ("LaTeX Warning: Rerun to get cross-references right.", 0),
            ("! Undefined control sequence.", 0),
            ("", 1),
        ):
            with self.subTest(log=log, returncode=returncode):

                async def measured(
                    tree, main, work, engine, _runner, *, _log=log, _returncode=returncode, **kwargs
                ):
                    return await build_project(
                        tree,
                        main,
                        work,
                        engine,
                        FakeBuildRunner(log=_log, returncode=_returncode),
                        **kwargs,
                    )

                with patch("latexprep.core.build_project", side_effect=measured):
                    report = await run_job(
                        JobRequest(
                            "check", self.source, Settings(checks=CheckSelection(ignore=("ALL",)))
                        )
                    )
                self.assertEqual(report.outcome, "blocked")

    async def test_ignored_artwork_with_nondefault_tuning_has_no_scope_requirement(self):
        pdf = self.root / "paper.pdf"
        pdf.write_bytes(b"%PDF-test")
        settings = Settings(
            checks=CheckSelection(ignore=("PDF306",)),
            figure_artwork=PdfArtworkOptions(classify_artwork=True, raster_dpi=36),
        )
        for command, source in (("pdf", pdf), ("check", self.source)):
            with self.subTest(command=command):
                with (
                    patch("latexprep.core.build_project", side_effect=build),
                    patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
                    patch("latexprep.check_pipeline.inspect_pdf", new=AsyncMock(return_value=[])),
                    patch(
                        "latexprep.check_pipeline._figure_inputs",
                        side_effect=AssertionError("No active figure check"),
                    ),
                ):
                    report = await run_job(JobRequest(command, source, settings))
                self.assertEqual(report.outcome, "passed", report.to_dict())
                self.assertFalse(
                    any(item.rule == "pdf.artwork_input_scope" for item in report.findings)
                )


if __name__ == "__main__":
    unittest.main()
