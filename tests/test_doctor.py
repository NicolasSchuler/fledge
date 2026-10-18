"""Setup diagnostics with controlled tools; these are not live toolchain tests."""

from __future__ import annotations

import asyncio
import json
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from unittest.mock import AsyncMock, patch

from click.testing import CliRunner

from latexprep.cli import cli
from latexprep.config import Settings
from latexprep.doctor import diagnostic_engines, run_doctor
from latexprep.job_execution import JobDeadlineExceeded
from latexprep.models import Finding, Outcome, PreparationError
from latexprep.resource_monitor import ResourceMonitor
from latexprep.runtime import CommandResult, ToolRunner
from latexprep.workflow_options import DocumentOptions, WorkflowOptions
from tests.support import fake_build_result, write_project
from tests.test_pdf import FakePdfRunner


class DoctorRunner(FakePdfRunner):
    """Reuse controlled Poppler output, with no external commands or monitor."""

    def __init__(self, *args, budget=None, source_date_epoch=None):
        super().__init__()
        self.budget = budget
        self.limits = args[0]
        self.cancelled = False

    @asynccontextmanager
    async def job_scope(self, root, *, max_temp_bytes):
        yield self

    async def run(self, argv, cwd, workspace, **options):
        if argv[-1] == "--version":
            return CommandResult(0, f"{argv[0]} version test\n", "", resolved_command=argv)
        return await super().run(argv, cwd, workspace, **options)


class DoctorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.runner = None
        self.builds = []

        def make_runner(*args, **kwargs):
            self.runner = DoctorRunner(*args, **kwargs)
            return self.runner

        self.stack = []
        for replacement in (
            patch("latexprep.job_execution.ToolRunner", side_effect=make_runner),
            patch("latexprep.doctor.shutil.which", side_effect=lambda name: f"/test-tools/{name}"),
            patch("latexprep.doctor.build_project", side_effect=self.build),
        ):
            self.stack.append(replacement.start())
            self.addCleanup(replacement.stop)

    async def build(self, source, main, work, engine, runner, **options):
        self.builds.append((engine, source, (source / main).read_text(), options))
        return fake_build_result(work, main)

    async def test_default_exercises_core_pipeline_and_keeps_inventory_distinct(self):
        report = await run_doctor()
        self.assertEqual(report.outcome, "passed", report.to_dict())
        self.assertEqual(report.command, "doctor")
        self.assertFalse(report.execution["manuscript_compiled"])
        self.assertTrue(report.execution["bibliography_exercised"])
        self.assertEqual(report.execution["engines"], ["pdflatex"])
        # The core fixture has no citations; each installed backend gets its own fixture.
        self.assertNotIn("\\cite", self.builds[0][2])
        self.assertEqual(
            [(row[0], row[3]["bibliography_backend"]) for row in self.builds[1:]],
            [("pdflatex", "bibtex"), ("pdflatex", "biber")],
        )
        self.assertTrue(all("\\cite{doctor}" in row[2] for row in self.builds[1:]))
        for name in ("bibtex", "biber"):
            self.assertFalse(report.tools[name]["required"])
            self.assertTrue(report.tools[name]["exercised"])
        for name in ("latexmk", "pdflatex", "pdfinfo", "pdftoppm", "pdftotext"):
            self.assertTrue(report.tools[name]["installed"])
            self.assertTrue(report.tools[name]["version_verified"])
            self.assertTrue(report.tools[name]["exercised"])
        self.assertNotIn("qpdf", report.tools)
        self.assertNotIn("tex-fmt", report.tools)
        self.assertFalse(self.builds[0][1].exists(), "Owned fixture is cleaned up")
        self.assertTrue(any(argv[0] == "pdftoppm" for argv in self.runner.commands))

    async def test_final_resource_sample_failure_blocks_successful_fixture(self):
        for failure in (
            "Job temporary files exceeded the configured limit",
            "Aggregate job memory exceeded the configured limit",
            "Job resource accounting failed: sampling unavailable",
        ):
            with self.subTest(failure=failure):

                async def final_sample(monitor, scope, resource_failure=failure):
                    monitor.fail_job(resource_failure)

                # Use the real scope exit path, with only its monitor and final
                # sample controlled. All built-in fixture operations succeed.
                with (
                    patch.object(DoctorRunner, "job_scope", ToolRunner.job_scope),
                    patch.object(ResourceMonitor, "_monitor_job", AsyncMock()),
                    patch.object(ResourceMonitor, "_sample_job", final_sample),
                ):
                    report = await run_doctor()
                self.assertEqual(report.outcome, "blocked")
                self.assertTrue(all(stage["status"] == "passed" for stage in report.stages))
                self.assertTrue(report.tools["pdflatex"]["exercised"])
                self.assertEqual(report.execution["observed"]["resource_exceeded"], failure)
                finding = next(
                    item for item in report.findings if item.rule == "execution.resources"
                )
                self.assertEqual(finding.severity, "error")
                self.assertEqual(finding.status, "inconclusive")
                self.assertIn(failure, finding.message)
                self.assertIn("rerun fledge doctor", finding.next_step)

    async def test_missing_required_tool_blocks_and_does_not_claim_exercise(self):
        with patch(
            "latexprep.doctor.shutil.which",
            side_effect=lambda name: None if name == "pdflatex" else f"/test-tools/{name}",
        ):
            report = await run_doctor()
        self.assertEqual(report.outcome, "blocked")
        self.assertFalse(report.tools["pdflatex"]["installed"])
        self.assertFalse(report.tools["latexmk"]["exercised"])
        self.assertEqual(self.builds, [])
        self.assertTrue(any(item.rule == "doctor.tool_missing" for item in report.findings))

    async def test_installed_tool_with_failed_version_is_not_exercised(self):
        original = DoctorRunner.tool_version

        async def failed(runner, argv, workspace):
            if argv[0] == "pdflatex":
                raise PreparationError("bad tool distribution")
            return await original(runner, argv, workspace)

        with patch.object(DoctorRunner, "tool_version", failed):
            report = await run_doctor()
        self.assertEqual(report.outcome, "blocked")
        self.assertTrue(report.tools["pdflatex"]["installed"])
        self.assertFalse(report.tools["pdflatex"]["version_verified"])
        self.assertFalse(report.tools["pdflatex"]["exercised"])

    async def test_unavailable_sandbox_reports_setup_failure_without_build(self):
        with patch.object(DoctorRunner, "run", side_effect=PreparationError("sandbox refused")):
            report = await run_doctor()
        self.assertEqual(report.outcome, "blocked")
        self.assertEqual(self.builds, [])
        self.assertTrue(report.tools["pdflatex"]["installed"])
        self.assertFalse(report.tools["pdflatex"]["version_verified"])
        self.assertIn("never falls back", report.findings[0].next_step)
        self.assertEqual(report.findings[0].rule, "doctor.sandbox_unavailable")
        self.assertEqual(report.findings[0].status, "inconclusive")

    async def test_timeout_preserves_installed_evidence_without_claiming_pass(self):
        with patch.object(DoctorRunner, "run", side_effect=JobDeadlineExceeded):
            report = await run_doctor()
        self.assertEqual(report.outcome, "blocked")
        self.assertTrue(report.tools["pdflatex"]["installed"])
        self.assertTrue(any(item.rule == "execution.deadline" for item in report.findings))
        self.assertEqual(self.builds, [])

    async def test_stray_timeout_is_not_reported_as_the_job_deadline(self):
        with patch.object(DoctorRunner, "run", side_effect=TimeoutError("timed out")):
            report = await run_doctor()
        rules = {item.rule for item in report.findings}
        self.assertNotIn("execution.deadline", rules)
        self.assertIn("doctor.execution_unavailable", rules)
        self.assertEqual(report.outcome, "blocked")

    async def test_failed_sandbox_probe_names_the_command_failure_in_plain_words(self):
        failed = CommandResult(1, "", "bwrap: no user namespaces", resolved_command=["true"])
        with patch.object(DoctorRunner, "run", return_value=failed):
            report = await run_doctor()
        message = report.findings[0].message
        self.assertEqual(report.findings[0].rule, "doctor.sandbox_unavailable")
        self.assertIn("the sandbox probe failed (1): bwrap: no user namespaces", message)
        self.assertNotIn("output_limit", message)

    async def test_job_deadline_cancels_active_build_and_releases_its_budget(self):
        cancelled = asyncio.Event()

        async def wait(*args, **kwargs):
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        with patch("latexprep.doctor.build_project", side_effect=wait):
            report = await run_doctor(Settings(job_timeout_seconds=1))
        self.assertEqual(report.outcome, "blocked")
        self.assertTrue(cancelled.is_set())
        self.assertEqual(report.execution["resources"]["active_cpu"], 0)
        self.assertTrue(any(item.rule == "execution.deadline" for item in report.findings))

    async def test_enabled_formatter_exercises_builtin_source_only(self):
        from latexprep.formatting import FormattingResult

        async def format_source(source, work, runner, options, **style):
            self.assertIn("Fledge setup test", (source / "main.tex").read_text())
            self.assertEqual(options.exclude, ())
            self.assertEqual(options.wrap_length, Settings().formatting_options.wrap_length)
            return FormattingResult(contents={"main.tex": (source / "main.tex").read_bytes()})

        with patch("latexprep.doctor.format_project", side_effect=format_source):
            report = await run_doctor(Settings(apply_formatting=True))
        self.assertEqual(report.outcome, "passed", report.findings)
        self.assertTrue(report.tools["tex-fmt"]["exercised"])

    async def test_failed_builtin_build_is_not_a_manuscript_finding(self):
        async def failed(source, main, work, engine, runner, **options):
            return fake_build_result(
                work,
                success=False,
                findings=[Finding("build.failed", "Tool failed.", severity="error")],
            )

        with patch("latexprep.doctor.build_project", side_effect=failed):
            report = await run_doctor()
        self.assertEqual(report.outcome, "blocked")
        failure = next(item for item in report.findings if item.rule == "doctor.fixture_failed")
        self.assertIn("Built-in setup fixture", failure.message)
        self.assertIn("does not diagnose your manuscript", failure.next_step)
        self.assertFalse(report.tools["pdflatex"]["exercised"])

    async def test_pdf_tool_failure_blocks_smoke_even_when_compilation_passes(self):
        original = DoctorRunner.run

        async def failing(runner, argv, cwd, workspace, **options):
            if argv[0] == "pdftoppm" and argv[-1] != "-v":
                return CommandResult(1, "", "cannot render", resolved_command=argv)
            return await original(runner, argv, cwd, workspace, **options)

        with patch.object(DoctorRunner, "run", failing):
            report = await run_doctor()
        self.assertEqual(report.outcome, "blocked")
        self.assertTrue(report.tools["pdflatex"]["exercised"])
        self.assertTrue(report.tools["pdftoppm"]["version_verified"])
        self.assertFalse(report.tools["pdftoppm"]["exercised"])

    async def test_every_distinct_document_engine_is_exercised(self):
        settings = Settings(
            workflow=WorkflowOptions(
                documents=(
                    DocumentOptions("paper", "main.tex", engine="xelatex"),
                    DocumentOptions("supplement", "supp.tex", engine="lualatex"),
                    DocumentOptions("extra", "extra.tex", engine="xelatex"),
                )
            )
        )
        report = await run_doctor(settings)
        self.assertEqual(report.outcome, "passed", report.to_dict())
        self.assertEqual(diagnostic_engines(settings), ("lualatex", "xelatex"))
        core = [row[0] for row in self.builds if "\\cite" not in row[2]]
        self.assertEqual(core, ["lualatex", "xelatex"])
        # Bibliography backends are engine-independent, so one engine exercises them.
        self.assertEqual({row[0] for row in self.builds if "\\cite" in row[2]}, {"lualatex"})
        self.assertNotIn("pdflatex", report.tools)
        self.assertTrue(report.tools["xdvipdfmx"]["exercised"])

    async def test_explicit_bibliography_backend_selects_builtin_citation_fixture(self):
        for backend in ("bibtex", "biber"):
            report = await run_doctor(Settings(bibliography_backend=backend))
            self.assertEqual(report.outcome, "passed", report.to_dict())
            self.assertTrue(report.tools[backend]["exercised"])
            self.assertTrue(report.execution["bibliography_exercised"])
            self.assertIn("\\cite{doctor}", self.builds[-1][2])
            self.assertEqual(self.builds[-1][3]["bibliography_backend"], backend)

    async def test_missing_or_failing_bibliography_backend_is_advisory_under_auto(self):
        with patch(
            "latexprep.doctor.shutil.which",
            side_effect=lambda name: None if name == "biber" else f"/test-tools/{name}",
        ):
            report = await run_doctor()
        self.assertEqual(report.outcome, "passed_with_advisories", report.to_dict())
        missing = next(item for item in report.findings if item.rule == "doctor.tool_missing")
        self.assertEqual((missing.severity, missing.details["tool"]), ("warning", "biber"))
        stages = {stage["name"]: stage["status"] for stage in report.stages}
        self.assertEqual(stages["pdflatex: biber bibliography"], "skipped")
        self.assertEqual(stages["pdflatex: bibtex bibliography"], "passed")
        self.assertTrue(report.tools["bibtex"]["exercised"])

        async def failing_biber(source, main, work, engine, runner, **options):
            if options["bibliography_backend"] == "biber":
                return fake_build_result(
                    work,
                    success=False,
                    findings=[Finding("build.failed", "biblatex.sty not found.", severity="error")],
                )
            return await self.build(source, main, work, engine, runner, **options)

        with patch("latexprep.doctor.build_project", side_effect=failing_biber):
            report = await run_doctor()
        self.assertEqual(report.outcome, "passed_with_advisories", report.to_dict())
        failure = next(item for item in report.findings if item.rule == "doctor.fixture_failed")
        self.assertEqual(failure.severity, "warning")
        self.assertIn("biber bibliography", failure.message)
        self.assertIn("Only papers that need", failure.next_step)
        self.assertFalse(report.tools["biber"]["exercised"])
        self.assertTrue(report.tools["pdflatex"]["exercised"])

    async def test_optional_tools_are_advisory_only_when_enabled(self):
        from latexprep.pdf_checks import PdfCheckOptions

        with patch(
            "latexprep.doctor.shutil.which",
            side_effect=lambda name: None if name == "qpdf" else f"/test-tools/{name}",
        ):
            report = await run_doctor(Settings(pdf_checks=PdfCheckOptions(require_language=True)))
        self.assertEqual(report.outcome, "passed_with_advisories", report.to_dict())
        self.assertFalse(report.tools["qpdf"]["required"])
        self.assertFalse(report.tools["qpdf"]["exercised"])

    async def test_configured_pdf_structure_checks_list_qpdf_as_an_advisory_tool(self):
        from latexprep.pdf_structure_checks import StructureOptions

        for field in (
            "check_table_structure",
            "check_link_structure",
            "require_figure_alt",
            "check_structure_references",
            "require_structure_tree",
        ):
            with self.subTest(setting=field):
                report = await run_doctor(
                    Settings(structure_checks=StructureOptions(**{field: True}))
                )
                self.assertTrue(report.tools["qpdf"]["installed"])
                self.assertFalse(report.tools["qpdf"]["required"])
        report = await run_doctor(Settings())
        self.assertNotIn("qpdf", report.tools)

    async def test_missing_qpdf_is_an_advisory_for_structure_checks(self):
        from latexprep.pdf_structure_checks import StructureOptions

        with patch(
            "latexprep.doctor.shutil.which",
            side_effect=lambda name: None if name == "qpdf" else f"/test-tools/{name}",
        ):
            report = await run_doctor(
                Settings(structure_checks=StructureOptions(require_figure_alt=True))
            )
        self.assertEqual(report.outcome, "passed_with_advisories", report.to_dict())
        self.assertFalse(report.tools["qpdf"]["installed"])

    async def test_required_color_space_alone_lists_mutool(self):
        from latexprep.pdf_artwork import PdfArtworkOptions

        for field in ("rendered_pdf_artwork", "included_figure_artwork"):
            with self.subTest(setting=field):
                report = await run_doctor(
                    Settings(**{field: PdfArtworkOptions(required_color_space="rgb")})
                )
                self.assertTrue(report.tools["mutool"]["installed"])
                self.assertFalse(report.tools["mutool"]["required"])
        report = await run_doctor(Settings())
        self.assertNotIn("mutool", report.tools)

    async def test_cancellation_propagates_to_the_active_work(self):
        started = asyncio.Event()
        cancelled = asyncio.Event()

        async def wait(*args, **kwargs):
            started.set()
            try:
                await asyncio.Future()
            finally:
                cancelled.set()

        with patch("latexprep.doctor.build_project", side_effect=wait):
            task = asyncio.create_task(run_doctor())
            await started.wait()
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertTrue(cancelled.is_set())


class DoctorCommandTests(unittest.TestCase):
    def test_json_stdout_is_clean_and_quiet_suppresses_only_stderr_progress(self):
        from latexprep.models import Report

        async def respond(settings, *, progress):
            if progress is not None:
                progress("Testing controlled setup")
            return Report("doctor", outcome=Outcome.PASSED, scope="setup diagnostics")

        for quiet in (False, True):
            with patch("latexprep.doctor_command.run_doctor", side_effect=respond):
                arguments = ["doctor", "--json", *(["--quiet"] if quiet else [])]
                result = CliRunner().invoke(cli, arguments)
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(json.loads(result.stdout)["outcome"], "passed")
            if quiet:
                self.assertEqual(result.stderr, "")
            else:
                self.assertIn("Testing controlled setup", result.stderr)

    def test_project_config_is_selected_without_reading_or_building_manuscript(self):
        from latexprep.models import Report

        with tempfile.TemporaryDirectory() as temporary:
            root = write_project(
                Path(temporary),
                {
                    "fledge.toml": 'engine = "xelatex"\n',
                    "main.tex": "private manuscript which must not be compiled",
                },
            )
            response = Report(
                "doctor", outcome=Outcome.PASSED, scope="setup diagnostics with a built-in fixture"
            )
            with patch(
                "latexprep.doctor_command.run_doctor", AsyncMock(return_value=response)
            ) as run:
                result = CliRunner().invoke(cli, ["doctor", str(root), "--json", "--quiet"])
            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(run.call_args.args[0].engine, "xelatex")
            self.assertEqual(json.loads(result.output)["command"], "doctor")
            self.assertEqual(
                (root / "main.tex").read_text(), "private manuscript which must not be compiled"
            )

    def test_no_project_uses_defaults_and_quiet_retains_exit_code(self):
        from latexprep.models import Report

        with patch("latexprep.doctor_command.resolve_config_path") as discover:
            with patch(
                "latexprep.doctor_command.run_doctor",
                AsyncMock(return_value=Report("doctor", outcome=Outcome.BLOCKED)),
            ) as run:
                result = CliRunner().invoke(cli, ["doctor", "--quiet"])
        self.assertEqual(result.exit_code, 3, result.output)
        self.assertIn("Blocked", result.output)
        self.assertEqual(run.call_args.args[0].engine, "pdflatex")
        discover.assert_not_called()

    def test_invalid_config_is_json_request_error(self):
        with tempfile.TemporaryDirectory() as temporary:
            config = Path(temporary) / "bad.toml"
            config.write_text('engine = "invalid"\n')
            result = CliRunner().invoke(cli, ["doctor", "--config", str(config), "--json"])
        self.assertEqual(result.exit_code, 4, result.output)
        report = json.loads(result.output)
        self.assertEqual(report["scope"], "request validation")
        self.assertEqual(report["outcome"], "error")
