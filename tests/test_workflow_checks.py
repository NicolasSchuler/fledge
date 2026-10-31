"""Check policy wiring, output gates, source scope, and CLI consent with real source fixtures."""

from __future__ import annotations

import json
import string
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from latexprep.build_checks import BuildCheckOptions
from latexprep.cli import cli
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.findings import Finding
from latexprep.job_context import JobRequest, pdf_check_settings
from latexprep.manuscript import ManuscriptOptions
from latexprep.manuscript_check_options import ManuscriptCheckOptions
from latexprep.online_checks import OnlineOptions
from latexprep.pdf_check_options import PdfCheckOptions
from latexprep.pdf_check_pipeline import inspect_configured_pdf_checks
from latexprep.report import Outcome, Report
from latexprep.resource_budget import ResourceBudget
from latexprep.runtime import ToolRunner
from latexprep.submission_options import SubmissionOptions
from tests.support import (
    AsyncProjectTestCase,
    build_with_loaded_class,
    controlled_backends,
    single_file_project,
)


class WorkflowCheckTests(AsyncProjectTestCase):
    """Configured checks through whole jobs on real source fixtures.

    The cases cover identical results at any parallelism, the citation scope of the ``bib``
    command, offline runs that never resolve or connect, output policies applied after cleanup
    and flattening, constraints and PDF checks that block publication, and source policies and
    template references that follow the flattened files.
    """

    async def asyncSetUp(self):
        """Set ``self.source`` to the ``input`` directory under ``self.root``; tests create it."""
        self.source = self.root / "input"

    async def test_inspect_runs_new_source_checks_with_deterministic_parallel_results(self):
        single_file_project(
            self.source,
            "Named Institution \\cite{missing}\\bibliography{refs}",
            "\\title{Paper}",
        )
        (self.source / "refs.bib").write_text("@article{unused,title={A study}}\n")
        original = {p.name: p.read_bytes() for p in self.source.iterdir()}
        settings = Settings(
            manuscript_checks=ManuscriptCheckOptions(required_metadata=("author",)),
            submission_checks=SubmissionOptions(identity_terms=("Named Institution",)),
        )
        build = AsyncMock()
        backends = controlled_backends(build=build)
        reports = [
            await run_job(
                JobRequest("inspect", self.source, replace(settings, jobs=jobs)),
                backends=backends,
            )
            for jobs in (1, 4)
        ]
        build.assert_not_called()
        self.assertEqual(reports[0].to_dict()["findings"], reports[1].to_dict()["findings"])
        for report in reports:
            self.assertEqual(report.outcome, "blocked")
            failed = {item.code for item in report.findings if item.status == "failed"}
            self.assertTrue({"BIB101", "BIB102", "MAN008", "PRV001"} <= failed)
            self.assertEqual(report.execution["resources"]["active_cpu"], 0)
        self.assertEqual(original, {p.name: p.read_bytes() for p in self.source.iterdir()})

    async def test_bibliography_command_uses_selected_citation_scope(self):
        single_file_project(self.source, "\\cite{missing}\\bibliography{refs}")
        (self.source / "refs.bib").write_text("@article{unused,title={A study}}\n")
        report = await run_job(JobRequest("bib", self.source, Settings()))
        self.assertEqual(report.main, "main.tex")
        self.assertEqual(report.outcome, "blocked")
        self.assertIn("BIB101", {item.code for item in report.findings})

    async def test_offline_configured_checks_never_resolve_or_connect(self):
        single_file_project(self.source, "\\cite{one}\\bibliography{refs}")
        (self.source / "refs.bib").write_text("@article{one,doi={10.1234/example}}\n")
        with (
            patch(
                "latexprep.pinned_dns.start_process",
                new=AsyncMock(side_effect=AssertionError("DNS")),
            ) as dns,
            patch(
                "asyncio.open_connection", new=AsyncMock(side_effect=AssertionError("network"))
            ) as network,
        ):
            report = await run_job(
                JobRequest(
                    "inspect",
                    self.source,
                    Settings(online_checks=OnlineOptions(online_doi_resolution=True)),
                )
            )
        dns.assert_not_called()
        network.assert_not_called()
        finding = next(item for item in report.findings if item.code == "NET001")
        self.assertEqual(finding.status, "skipped")
        self.assertEqual(finding.details["requests"], 0)

    async def test_output_policies_apply_after_cleanup_and_flattening(self):
        single_file_project(self.source)
        (self.source / "extra notes.txt").write_text("Additional note.\n")
        (self.source / "main.aux").write_text("Generated artifact.\n")
        options = SubmissionOptions(
            filename_allowed_characters=string.ascii_letters + string.digits + "._-",
            allowed_extensions=(".tex", ".txt"),
            required_deliverables=(("main.tex", "text"),),
            max_archive_bytes=10_000,
        )
        settings = Settings(layout="flat", submission_checks=options)
        backends = controlled_backends(build=build_with_loaded_class)
        report = await run_job(
            JobRequest("prepare", self.source, settings, self.root / "out"), backends=backends
        )
        self.assertIn(report.outcome, {"passed", "passed_with_advisories"}, report.to_dict())
        self.assertTrue((self.root / "out/submission.zip").is_file())
        self.assertTrue((self.source / "extra notes.txt").is_file())
        self.assertTrue((self.source / "main.aux").is_file())
        for code in ("PKG102", "PKG103", "PKG104", "PKG105"):
            findings = [item for item in report.findings if item.code == code]
            self.assertTrue(findings, code)
            self.assertTrue(all(item.status == "passed" for item in findings), findings)
            self.assertTrue(
                all(
                    item.details["stage"] != "source privacy and bundle checks" for item in findings
                )
            )

    async def test_archive_and_build_constraints_block_publication(self):
        single_file_project(self.source)
        for name, settings, code in (
            (
                "archive",
                Settings(submission_checks=SubmissionOptions(max_archive_bytes=1)),
                "PKG105",
            ),
            (
                "build",
                Settings(
                    build_checks=BuildCheckOptions(forbidden_loaded_packages=("article.cls",))
                ),
                "BLD102",
            ),
        ):
            backends = controlled_backends(build=build_with_loaded_class)
            with self.subTest(policy=name):
                report = await run_job(
                    JobRequest("prepare", self.source, settings, self.root / name),
                    backends=backends,
                )
            self.assertEqual(report.outcome, "blocked", report.to_dict())
            self.assertFalse((self.root / name).exists())
            self.assertEqual(report.artifacts, {})
            self.assertTrue(
                any(item.code == code and item.status == "failed" for item in report.findings)
            )

    async def test_failed_final_configured_pdf_check_prevents_publication(self):
        single_file_project(self.source)
        stages = []

        async def configured(_pdf, root, _main, work, _runner, _settings, _budget, *, analysed):
            self.assertTrue(analysed.covers(root, _main))
            stages.append((root, work.name))
            final = work.name == "additional-final"
            return [
                Finding(
                    "pdf.required_metadata",
                    "Author missing" if final else "Present",
                    severity="error" if final else "info",
                    status="failed" if final else "passed",
                )
            ]

        backends = controlled_backends(build=build_with_loaded_class)
        with patch(
            "latexprep.document_tasks.inspect_configured_pdf_checks", side_effect=configured
        ):
            report = await run_job(
                JobRequest("prepare", self.source, Settings(), self.root / "out"), backends=backends
            )
        self.assertEqual(report.outcome, "blocked")
        self.assertFalse((self.root / "out").exists())
        self.assertEqual([name for _, name in stages], ["additional-baseline", "additional-final"])
        self.assertNotEqual(stages[0][0], stages[1][0])

    async def test_final_source_policy_catches_package_name_changed_by_flattening(self):
        single_file_project(self.source, preamble="\\usepackage{styles/helper}")
        (self.source / "styles").mkdir()
        (self.source / "styles/helper.sty").write_text("\\ProvidesPackage{helper}\n")
        backends = controlled_backends(build=build_with_loaded_class)
        report = await run_job(
            JobRequest(
                "prepare",
                self.source,
                Settings(
                    layout="flat", manuscript=ManuscriptOptions(forbidden_packages=("helper",))
                ),
                self.root / "out",
            ),
            backends=backends,
        )
        violation = next(
            item for item in report.findings if item.code == "TEX103" and item.status == "failed"
        )
        self.assertEqual(violation.details["stage"], "final source checks")
        self.assertEqual(report.outcome, "blocked")
        self.assertFalse((self.root / "out").exists())

    async def test_template_reference_follows_the_flattened_file(self):
        single_file_project(self.source, preamble="\\usepackage{styles/helper}")
        (self.source / "styles").mkdir()
        content = b"\\ProvidesPackage{helper}[2025/01/01 v1]\n"
        (self.source / "styles/helper.sty").write_bytes(content)
        reference = self.root / "reference.sty"
        reference.write_bytes(content)
        backends = controlled_backends(build=build_with_loaded_class)
        report = await run_job(
            JobRequest(
                "prepare",
                self.source,
                Settings(
                    layout="flat",
                    submission_checks=SubmissionOptions(
                        template_references=(("styles/helper.sty", str(reference)),),
                    ),
                ),
                self.root / "out",
            ),
            backends=backends,
        )
        matches = [item for item in report.findings if item.code == "PKG107"]
        self.assertEqual(len(matches), 3, report.to_dict())
        self.assertTrue(all(item.status == "passed" for item in matches), matches)
        self.assertIn(report.outcome, {"passed", "passed_with_advisories"}, report.to_dict())
        self.assertEqual((self.root / "out/sources/helper.sty").read_bytes(), content)


class PdfCheckCompositionTests(unittest.IsolatedAsyncioTestCase):
    """What :func:`inspect_configured_pdf_checks` passes to the PDF adapters it calls.

    Source metadata becomes the expected PDF metadata unless it is ambiguous, privacy terms
    reach the check of the extracted PDF text and metadata, a failed extraction stays
    inconclusive, and only the figures the source includes are inspected and reported by their
    source path.
    """

    async def test_title_only_matching_ignores_unrelated_author_ambiguity(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "input"
            single_file_project(source, preamble="\\title{A paper}\\author{Alice}\\author{Bob}")
            details = AsyncMock(return_value=[])
            with patch("latexprep.pdf_check_pipeline.inspect_pdf_details", new=details):
                findings = await inspect_configured_pdf_checks(
                    root / "paper.pdf",
                    source,
                    "main.tex",
                    root / "work",
                    ToolRunner(),
                    pdf_check_settings(Settings(match_source_pdf_metadata=("title",))),
                    ResourceBudget(1, 1024),
                )
            self.assertEqual(findings, [])
            self.assertEqual(
                details.await_args.kwargs["options"].expected_metadata, (("Title", "A paper"),)
            )

    async def test_source_metadata_and_pdf_privacy_reach_their_adapters(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "input"
            single_file_project(source, preamble="\\title{Example Paper}\\author{Example Author}")
            budget = ResourceBudget(2, 1024)
            settings = Settings(
                match_source_pdf_metadata=("title", "author"),
                submission_checks=SubmissionOptions(identity_terms=("Example Author",)),
            )
            details = AsyncMock(return_value=[])
            with (
                patch("latexprep.pdf_check_pipeline.inspect_pdf_details", new=details),
                patch(
                    "latexprep.pdf_check_pipeline.extract_pdf_evidence",
                    new=AsyncMock(return_value=("Body text.", {"Author": "Example Author"})),
                ),
            ):
                findings = await inspect_configured_pdf_checks(
                    root / "paper.pdf",
                    source,
                    "main.tex",
                    root / "work",
                    ToolRunner(),
                    pdf_check_settings(settings),
                    budget,
                )
            expected = dict(details.await_args.kwargs["options"].expected_metadata)
            self.assertEqual(expected, {"Title": "Example Paper", "Author": "Example Author"})
            self.assertTrue(
                any(item.code == "PRV002" and item.status == "failed" for item in findings)
            )
            self.assertEqual(budget.statistics()["active_cpu"], 0)

    async def test_ambiguous_metadata_and_missing_extraction_cannot_pass(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "input"
            single_file_project(source, preamble="\\author{One}\\author{Two}")
            details = AsyncMock(return_value=[])
            with (
                patch("latexprep.pdf_check_pipeline.inspect_pdf_details", new=details),
                patch(
                    "latexprep.pdf_check_pipeline.extract_pdf_evidence",
                    new=AsyncMock(side_effect=OSError("sensitive tool fragment")),
                ),
            ):
                findings = await inspect_configured_pdf_checks(
                    root / "paper.pdf",
                    source,
                    "main.tex",
                    root / "work",
                    ToolRunner(),
                    pdf_check_settings(
                        Settings(
                            match_source_pdf_metadata=("author",),
                            submission_checks=SubmissionOptions(identity_terms=("One",)),
                        )
                    ),
                    ResourceBudget(1, 1024),
                )
            self.assertEqual(details.await_args.kwargs["options"].expected_metadata, ())
            self.assertTrue({"PDF207", "PRV002"} <= {item.code for item in findings})
            self.assertTrue(all(item.status == "inconclusive" for item in findings))
            self.assertNotIn("sensitive tool fragment", str(findings))

    async def test_figure_inputs_are_selected_and_reported_with_persistent_locations(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory).resolve()
            source = root / "input"
            single_file_project(source, "\\includegraphics{figure.pdf}")
            (source / "figure.pdf").write_bytes(b"%PDF-fixture")
            (source / "unused.pdf").write_bytes(b"%PDF-fixture")

            async def figure_check(paths, *_args, **_kwargs):
                self.assertEqual(paths, (source / "figure.pdf",))
                return [
                    Finding(
                        "pdf.included_figure_fonts",
                        "Font is missing",
                        severity="error",
                        path=str(paths[0]),
                    )
                ]

            with (
                patch(
                    "latexprep.pdf_check_pipeline.inspect_included_pdf_figures",
                    side_effect=figure_check,
                ),
                patch(
                    "latexprep.pdf_check_pipeline.inspect_pdf_details",
                    new=AsyncMock(return_value=[]),
                ),
            ):
                findings = await inspect_configured_pdf_checks(
                    root / "paper.pdf",
                    source,
                    "main.tex",
                    root / "work",
                    ToolRunner(),
                    pdf_check_settings(
                        Settings(pdf_checks=PdfCheckOptions(require_embedded_figure_fonts=True))
                    ),
                    ResourceBudget(1, 1024),
                )
            self.assertEqual(len(findings), 1, findings)
            self.assertEqual((findings[0].code, findings[0].path), ("PDF212", "figure.pdf"))


class OnlineCliTests(unittest.TestCase):
    """``--online`` and ``--offline`` change only the consent of the configured online checks."""

    def test_online_flags_override_only_consent_and_preserve_selected_checks(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings_file = root / "checks.toml"
            settings_file.write_text("[online_checks]\nonline = true\nonline_metadata = true\n")
            for flags, expected in (([], True), (["--offline"], False), (["--online"], True)):
                with (
                    self.subTest(flags=flags),
                    patch(
                        "latexprep.cli.run_job",
                        new=AsyncMock(return_value=Report("inspect", outcome=Outcome.PASSED)),
                    ) as job,
                ):
                    response = CliRunner().invoke(
                        cli,
                        ["inspect", str(root), "--config", str(settings_file), "--json", *flags],
                    )
                self.assertEqual(response.exit_code, 0, response.output)
                options = job.await_args.args[0].settings.online_checks
                self.assertEqual(options.online, expected)
                self.assertTrue(options.online_metadata)
                self.assertEqual(json.loads(response.stdout)["outcome"], "passed")
