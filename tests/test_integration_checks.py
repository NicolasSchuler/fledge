"""Opt-in configured-check workflow using real restricted LaTeX and PDF tools."""

from __future__ import annotations

import asyncio
import tempfile
import unittest
import zipfile
from pathlib import Path

from latexprep.build_checks import BuildCheckOptions
from latexprep.config import Settings
from latexprep.core import JobRequest, run_job
from latexprep.manuscript_checks import ManuscriptCheckOptions
from latexprep.pdf_checks import PdfCheckOptions, PdfSectionBudget, inspect_pdf_details
from latexprep.runtime import RuntimeLimits, ToolRunner
from latexprep.scheduler import ResourceBudget
from latexprep.submission_checks import SubmissionOptions
from tests.support import live_tests_enabled


@unittest.skipUnless(
    live_tests_enabled(),
    "Set FLEDGE_RUN_INTEGRATION=1 for the live configured-check preparation workflow",
)
class LiveConfiguredChecksTests(unittest.IsolatedAsyncioTestCase):
    async def test_configured_checks_survive_flattening_and_exact_archive_rebuild(self) -> None:
        with tempfile.TemporaryDirectory(prefix="latex-prep-configured-live-") as directory:
            base = Path(directory).resolve()
            source = base / "paper"
            (source / "sections").mkdir(parents=True)
            (source / "main.tex").write_text(
                r"""\documentclass[10pt]{article}
\usepackage[pdfusetitle]{hyperref}
\title{Configured Checks Study}
\author{Rosa Newton}
\date{}
\begin{document}
\maketitle
\input{sections/body}
\end{document}
""",
                encoding="utf-8",
            )
            (source / "sections/body.tex").write_text(
                r"""\section{Results}
Figure~\ref{fig:shape} shows a controlled rectangle.
The fixture contains literal metadata and a short declaration.
\begin{figure}[ht]
\centering
\rule{2cm}{1cm}
\caption{A solid rectangle used as a controlled figure.}
\label{fig:shape}
\end{figure}
\section*{Data Availability}
The complete fixture is included in this source package.
""",
                encoding="utf-8",
            )
            (source / "cover.txt").write_text(
                "Controlled accompanying text deliverable.\n", encoding="utf-8"
            )
            original_bytes = {
                path.relative_to(source).as_posix(): path.read_bytes()
                for path in source.rglob("*")
                if path.is_file()
            }
            settings = Settings(
                main="main.tex",
                layout="flat",
                jobs=2,
                build_jobs=1,
                render_jobs=1,
                memory_mb=1024,
                build_memory_mb=512,
                timeout_seconds=60,
                job_timeout_seconds=180,
                max_bytes=8_388_608,
                max_temporary_bytes=134_217_728,
                source_date_epoch=1_700_000_000,
                build_checks=BuildCheckOptions(
                    overfull_tolerance_pt=1,
                    inventory_loaded_packages=True,
                    required_loaded_packages=("article.cls",),
                    minimum_package_dates=(("article.cls", "1990-01-01"),),
                ),
                manuscript_checks=ManuscriptCheckOptions(
                    required_metadata=("title", "author"),
                    required_declarations=("Data Availability",),
                    require_float_captions=True,
                    require_float_labels=True,
                    check_float_label_order=True,
                    require_float_references=True,
                ),
                pdf_checks=PdfCheckOptions(
                    section_budgets=(PdfSectionBudget("Entire short fixture", 1, 1, 1),),
                    min_text_size_pt=8,
                    required_metadata=("Title", "Author"),
                ),
                match_source_pdf_metadata=("title",),
                submission_checks=SubmissionOptions(
                    identity_terms=("Private Institute Identifier",),
                    # Source-relative extras follow the flat filename map.
                    required_deliverables=(("sections/body.tex", "text"), ("cover.txt", "text")),
                    max_archive_bytes=65_536,
                ),
            )
            pending_before = asyncio.all_tasks()
            output = base / "prepared"
            report = await run_job(JobRequest("prepare", source, settings, output))
            problems = [
                (item.code, item.status, item.details.get("stage"), item.message)
                for item in report.findings
                if item.severity == "error" or item.status == "inconclusive"
            ]
            self.assertIn(report.outcome, {"passed", "passed_with_advisories"}, problems)
            self.assertEqual(asyncio.all_tasks() - pending_before, set())
            self.assertEqual(
                original_bytes,
                {
                    path.relative_to(source).as_posix(): path.read_bytes()
                    for path in source.rglob("*")
                    if path.is_file()
                },
            )

            def assert_code_stages(code: str, expected_stages: set[str]) -> None:
                findings = [item for item in report.findings if item.code == code]
                self.assertEqual(len(findings), len(expected_stages), (code, findings))
                self.assertEqual(
                    {item.details.get("stage") for item in findings}, expected_stages, code
                )
                self.assertTrue(all(item.status == "passed" for item in findings), findings)

            for code in ("BLD101", "BLD102", "BLD103"):
                assert_code_stages(code, {"baseline", "transformed", "prepared", "archive"})
            for code in ("MAN008", "MAN010", "MAN011", "MAN012", "MAN013", "MAN014"):
                assert_code_stages(code, {"manuscript details", "final source checks"})
            for code in ("PDF201", "PDF202", "PDF206", "PDF207", "PRV002"):
                assert_code_stages(
                    code, {"baseline configured PDF checks", "final configured PDF checks"}
                )
            assert_code_stages(
                "PRV001",
                {
                    "source privacy and bundle checks",
                    "prepared submission checks",
                    "final submission checks",
                },
            )
            assert_code_stages("PKG104", {"prepared submission checks", "final submission checks"})
            assert_code_stages("PKG105", {"archive"})
            for finding in report.findings:
                if finding.rule == "build.package_inventory":
                    records = finding.details["packages"]
                    assert isinstance(records, list)
                    self.assertTrue(any(record["name"] == "article.cls" for record in records))
                if finding.code == "PDF207":
                    self.assertEqual(finding.details["compared_fields"], ["Title"])
                if finding.code == "PDF201":
                    self.assertEqual(finding.details["observed_total_pages"], 1)
            for stage in ("baseline", "transformed", "prepared", "archive"):
                builds = [item for item in report.stages if item["name"] == stage]
                self.assertEqual(len(builds), 1)
                self.assertEqual(builds[0]["status"], "succeeded")
            comparisons = [item for item in report.findings if item.rule == "compare.rendering"]
            self.assertEqual(len(comparisons), 2)
            self.assertTrue(all(item.status == "passed" for item in comparisons))

            archive = output / "submission.zip"
            pdf = output / "manuscript.pdf"
            self.assertEqual(report.artifacts["archive"], str(archive))
            self.assertTrue(archive.is_file())
            maximum_archive_bytes = settings.submission_checks.max_archive_bytes
            assert isinstance(maximum_archive_bytes, int)
            self.assertLessEqual(archive.stat().st_size, maximum_archive_bytes)
            self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
            with zipfile.ZipFile(archive) as packaged:
                self.assertEqual(set(packaged.namelist()), {"main.tex", "body.tex", "cover.txt"})
                self.assertNotIn(b"sections/", packaged.read("main.tex"))
                self.assertEqual(packaged.read("body.tex"), original_bytes["sections/body.tex"])
                self.assertEqual(packaged.read("cover.txt"), original_bytes["cover.txt"])
                for name in packaged.namelist():
                    self.assertEqual(packaged.read(name), (output / "sources" / name).read_bytes())

            resources = report.execution["resources"]
            observed = report.execution["observed"]
            assert isinstance(resources, dict)
            assert isinstance(observed, dict)
            self.assertEqual(resources["active_cpu"], 0)
            self.assertEqual(resources["active_memory_mb"], 0)
            self.assertEqual(resources["queued"], 0)
            self.assertLessEqual(resources["peak_reserved_cpu"], settings.jobs)
            self.assertLessEqual(resources["peak_reserved_memory_mb"], settings.memory_mb)
            self.assertLessEqual(resources["peak_builds"], settings.build_jobs)
            self.assertLessEqual(resources["peak_renders"], settings.render_jobs)
            self.assertIsNone(observed["resource_exceeded"])
            self.assertGreater(observed["commands"], 0)
            self.assertGreater(observed["samples"], 0)
            self.assertLessEqual(observed["peak_temp_bytes"], settings.max_temporary_bytes)
            execution = report.tools["execution"]
            assert isinstance(execution, dict)
            self.assertEqual(execution["backend"], "macOS sandbox-exec")
            self.assertEqual(execution["network"], "denied")
            self.assertEqual(execution["shell_escape"], "disabled")
            for tool in ("latexmk", "pdflatex", "pdfinfo", "pdftohtml", "pdftoppm", "pdftotext"):
                tool_evidence = report.tools[tool]
                assert isinstance(tool_evidence, dict)
                self.assertTrue(tool_evidence["version"], tool)

            # A contradictory configured threshold must fail on the same published PDF.
            # This adds no TeX build or archive rewrite.
            strict = await inspect_pdf_details(
                pdf,
                base / "strict-pdf-check",
                ToolRunner(RuntimeLimits(timeout_seconds=30, memory_mb=256)),
                options=PdfCheckOptions(min_text_size_pt=40),
                budget=ResourceBudget(1, 256),
            )
            failure = next(item for item in strict if item.code == "PDF202")
            self.assertEqual(failure.status, "failed", failure)
            violation_count = failure.details["violation_count"]
            assert isinstance(violation_count, int)
            self.assertGreater(violation_count, 0)


if __name__ == "__main__":
    unittest.main()
