"""Workflow contract tests; tool execution is replaced explicitly in these tests."""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from latexprep.build_result import BuildTools
from latexprep.cli import main
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.errors import PreparationError, RequestError
from latexprep.findings import Finding
from latexprep.job_context import JobRequest
from latexprep.publication import (
    DocumentResult,
    OutputTransaction,
    plan_previews,
    publish_documents,
    validate_destination,
)
from latexprep.redaction import scrub_local_paths
from latexprep.report import Report
from tests.support import controlled_backends, sectioned_project


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_flat_bundle_from_exact_extraction_and_serial_equivalence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "input"
            sectioned_project(source)
            (source / "data.txt").write_text("0" * 100_000)
            original = {p.relative_to(source): p.read_bytes() for p in source.rglob("*.tex")}
            build_inputs = []

            async def build(tree, main, work, engine, runner, **_options):
                build_inputs.append((tree, main, sorted(p.name for p in tree.rglob("*.tex"))))
                work.mkdir()
                pdf = work / "built.pdf"
                pdf.write_bytes(b"controlled fake PDF; these tests exercise orchestration only")
                return SimpleNamespace(
                    success=True,
                    pdf=pdf,
                    findings=[],
                    recorder_project_inputs={main},
                    submission_inputs={main},
                    tools=BuildTools(),
                    command=["test-build"],
                    generated_reads=frozenset(),
                    log="",
                )

            passed = [
                Finding("pdf.comparison", "Controlled comparison", severity="info", status="passed")
            ]
            backends = controlled_backends(build=build, compare_pdfs=passed)
            first = await run_job(
                JobRequest(
                    "prepare",
                    source,
                    Settings(main="./main.tex", layout="flat", jobs=1),
                    base / "serial",
                ),
                backends=backends,
            )
            second = await run_job(
                JobRequest("prepare", source, Settings(layout="flat", jobs=3), base / "parallel"),
                backends=backends,
            )
            self.assertIn(first.outcome, {"passed", "passed_with_advisories"})
            self.assertEqual(first.outcome, second.outcome)
            self.assertEqual(
                (base / "serial/submission.zip").read_bytes(),
                (base / "parallel/submission.zip").read_bytes(),
            )
            self.assertEqual(
                original, {p.relative_to(source): p.read_bytes() for p in source.rglob("*.tex")}
            )
            self.assertEqual(len(build_inputs), 8)
            self.assertNotEqual(build_inputs[0][0], build_inputs[1][0])
            self.assertNotEqual(build_inputs[1][0], build_inputs[2][0])
            self.assertEqual(build_inputs[2][2], build_inputs[3][2])
            with zipfile.ZipFile(base / "serial/submission.zip") as archive:
                self.assertTrue(all("/" not in name for name in archive.namelist()))
                self.assertEqual(len(archive.namelist()), 3)
                self.assertNotIn("data.txt", archive.namelist())
            self.assertTrue((base / "serial/report.json").is_file())

    async def test_failed_final_build_never_releases_output(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "input"
            sectioned_project(source)
            count = 0

            async def build(tree, main, work, engine, runner, **_options):
                nonlocal count
                count += 1
                work.mkdir()
                pdf = work / "built.pdf"
                pdf.write_bytes(b"fake")
                return SimpleNamespace(
                    success=count < 3,
                    pdf=pdf if count < 3 else None,
                    findings=[],
                    recorder_project_inputs={main},
                    submission_inputs={main},
                    tools=BuildTools(),
                    command=["test-build"],
                    generated_reads=frozenset(),
                    log="",
                )

            backends = controlled_backends(build=build)
            result = await run_job(
                JobRequest("prepare", source, Settings(layout="flat"), base / "output"),
                backends=backends,
            )
            self.assertEqual(count, 3)
            self.assertEqual(result.outcome, "blocked")
            self.assertFalse((base / "output").exists())
            self.assertEqual(result.artifacts, {})

    async def test_baseline_failure_keeps_source_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            sectioned_project(base / "input")
            (base / "input/sections/results.tex").write_text("TODO unfinished.\n")
            failure = SimpleNamespace(
                success=False,
                pdf=None,
                findings=[],
                recorder_project_inputs=set(),
                tools=BuildTools(),
                command=[],
                generated_reads=frozenset(),
                log="failure",
            )
            backends = controlled_backends(build=failure)
            result = await run_job(
                JobRequest("check", base / "input", Settings()), backends=backends
            )
            self.assertEqual(result.outcome, "blocked")
            self.assertTrue(any(item.rule == "source.edit_marker" for item in result.findings))

    async def test_bibliography_command_skips_root_discovery_and_the_reference_pdf(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            sectioned_project(base / "input")
            reference = base / "reference.pdf"
            reference.write_bytes(b"%PDF-fixture")
            with (
                patch(
                    "latexprep.main_selection.discover_roots", side_effect=AssertionError("scan")
                ),
                patch(
                    "latexprep.runtime.ToolRunner.freeze_input",
                    side_effect=AssertionError("reference PDF snapshot"),
                ),
            ):
                result = await run_job(
                    JobRequest(
                        "bib",
                        base / "input",
                        Settings(main="main.tex"),
                        reference_pdf=reference,
                    )
                )
        self.assertEqual(result.main, "main.tex")
        self.assertNotEqual(result.outcome, "error")

    async def test_input_without_a_document_says_no_main_file_was_found(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "input").mkdir()
            (base / "input/notes.txt").write_text("not a document\n")
            build = AsyncMock()
            backends = controlled_backends(build=build)
            result = await run_job(
                JobRequest("check", base / "input", Settings()), backends=backends
            )
            build.assert_not_called()
        [finding] = [item for item in result.findings if item.rule == "project.main_selection"]
        self.assertIn("no file with \\documentclass was found", finding.message)
        self.assertNotIn("0 candidate roots", finding.message)

    async def test_ambiguous_root_blocks_without_running_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            sectioned_project(base / "input")
            (base / "input/other.tex").write_bytes((base / "input/main.tex").read_bytes())
            build = AsyncMock()
            backends = controlled_backends(build=build)
            result = await run_job(
                JobRequest("check", base / "input", Settings()), backends=backends
            )
            self.assertEqual(result.outcome, "blocked")
            build.assert_not_called()


class PublicationLimitTests(unittest.TestCase):
    def preview_report(self, *artifacts: Path) -> Report:
        return Report(
            "check",
            findings=[
                Finding(
                    "pdf.grayscale_preview",
                    "Generated",
                    severity="info",
                    status="passed",
                    details={"artifacts": [str(path) for path in artifacts]},
                )
            ],
        )

    def test_repeated_previews_are_numbered_once_in_order(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            first, second = work / "a.png", work / "b.png"
            first.write_bytes(b"one")
            second.write_bytes(b"two")
            plan = plan_previews(
                self.preview_report(first, second, first), work, work / "out" / "previews"
            )
        self.assertEqual(
            plan,
            (
                (first, work / "out/previews/page-0001.png"),
                (second, work / "out/previews/page-0002.png"),
            ),
        )

    def test_a_preview_over_the_size_limit_names_the_file_and_the_way_out(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            image = work / "large.png"
            image.write_bytes(b"x" * 8)
            with (
                patch("latexprep.publication._MAX_PREVIEW_IMAGE_BYTES", 4),
                self.assertRaisesRegex(
                    PreparationError, r"large\.png is larger than .* grayscale preview"
                ),
            ):
                plan_previews(self.preview_report(image), work, work / "previews")

    def test_a_preview_outside_the_workspace_is_refused_as_a_defect(self):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve() / "work"
            work.mkdir()
            outside = Path(directory).resolve() / "elsewhere.png"
            outside.write_bytes(b"png")
            with self.assertRaisesRegex(
                PreparationError, "not a PNG image inside the job's workspace"
            ):
                plan_previews(self.preview_report(outside), work, work / "previews")


class OutputTransactionTests(unittest.TestCase):
    def test_rollback_removes_every_directory_even_when_one_cannot_be_removed(self):
        remove = shutil.rmtree
        with tempfile.TemporaryDirectory() as directory:
            first, second = Path(directory) / "first", Path(directory) / "second"

            def refuse_second(path, *args, **kwargs):
                if Path(path) == second:
                    raise PermissionError(13, "Permission denied", str(path))
                remove(path, *args, **kwargs)

            with (
                patch("latexprep.publication.shutil.rmtree", side_effect=refuse_second),
                self.assertRaises(RuntimeError) as raised,
            ):
                with OutputTransaction() as transaction:
                    transaction.create(first)
                    transaction.create(second)
                    raise RuntimeError("publication failed")
            # The cause stays primary; the directory that stayed behind is named.
            self.assertEqual(str(raised.exception), "publication failed")
            notes = "\n".join(raised.exception.__notes__)
            self.assertIn(str(second), notes)
            self.assertIn("Permission denied", notes)
            self.assertIn("remove it manually", notes)
            self.assertFalse(first.exists())
            self.assertTrue(second.exists())

    def test_a_directory_that_stays_behind_is_announced_and_recorded_in_the_report(self):
        messages: list[str] = []
        report = Report("prepare")
        with tempfile.TemporaryDirectory() as directory:
            leftover = Path(directory) / "output"

            def refuse(path, *_args, **_kwargs):
                raise PermissionError(13, "Permission denied", str(path))

            with (
                patch("latexprep.publication.shutil.rmtree", side_effect=refuse),
                self.assertRaises(RuntimeError),
                OutputTransaction(messages.append, report) as transaction,
            ):
                transaction.create(leftover)
                raise RuntimeError("publication failed")
            [finding] = report.findings
            self.assertEqual(finding.rule, "execution.rollback")
            self.assertEqual(finding.severity, "error")
            # The path is generalized as in every report, and the advice says what to do.
            shown = scrub_local_paths(str(leftover))
            self.assertIn(shown, finding.message)
            self.assertIn(f"Remove {shown} manually", finding.next_step)
            self.assertIn("remove it manually", messages[-1])
            self.assertIn(str(leftover), messages[-1])

    def test_rollback_announces_the_removal_only_when_something_was_created(self):
        messages: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            created = Path(directory) / "output"
            with self.assertRaises(RuntimeError), OutputTransaction(messages.append) as transaction:
                raise RuntimeError("before anything was created")
            self.assertEqual(messages, [])
            with self.assertRaises(RuntimeError), OutputTransaction(messages.append) as transaction:
                transaction.create(created)
                raise RuntimeError("publication failed")
            self.assertFalse(created.exists())
        self.assertEqual(messages, ["Publication failed; removing the partial output"])

    def test_successful_transaction_keeps_what_it_created(self):
        with tempfile.TemporaryDirectory() as directory:
            created = Path(directory) / "output"
            with OutputTransaction() as transaction:
                transaction.create(created)
            self.assertTrue(created.is_dir())

    def test_unverified_document_is_refused_before_any_directory_is_created(self):
        with tempfile.TemporaryDirectory() as directory:
            output = Path(directory) / "output"
            documents = [DocumentResult("paper", Report("prepare"), None)]
            with OutputTransaction() as transaction:
                with self.assertRaisesRegex(PreparationError, "paper did not verify"):
                    publish_documents(
                        transaction, output, documents, Report("prepare"), lambda: None
                    )
            self.assertFalse(output.exists())


class CliTests(unittest.TestCase):
    def test_json_has_no_progress_and_original_input_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            sectioned_project(base / "input")
            stdout, stderr = StringIO(), StringIO()
            with redirect_stdout(stdout), redirect_stderr(stderr):
                code = main(["inspect", str(base / "input"), "--json"])
            data = json.loads(stdout.getvalue())
            self.assertIn(code, {0, 1})
            self.assertEqual(data["command"], "inspect")
            self.assertIn("Importing", stderr.getvalue())
            self.assertEqual(sorted(p.name for p in base.iterdir()), ["input"])

    def test_invalid_config_returns_json_error(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            sectioned_project(base / "input")
            (base / "invalid.toml").write_text('publisher = "unsupported"\n')
            output = StringIO()
            with redirect_stdout(output):
                code = main(
                    [
                        "inspect",
                        str(base / "input"),
                        "--config",
                        str(base / "invalid.toml"),
                        "--json",
                    ]
                )
            self.assertEqual(code, 4)
            self.assertEqual(json.loads(output.getvalue())["outcome"], "error")

    def test_existing_and_contained_outputs_are_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            sectioned_project(base / "input")
            for output in (base / "input", base / "input/new"):
                with self.subTest(output=output), self.assertRaises(PreparationError):
                    validate_destination(base / "input", output, "--output")

    def test_advice_reads_correctly_for_an_option_that_starts_with_a_vowel(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            sectioned_project(base / "input")
            for output in (base / "input" / "new", base / "absent" / "new"):
                with self.subTest(output=output), self.assertRaises(RequestError) as raised:
                    validate_destination(base / "input", output, "output")
                self.assertNotRegex(raised.exception.suggestion, r"\ba [aeiou]")
                self.assertIn("different output path", raised.exception.suggestion)


if __name__ == "__main__":
    unittest.main()
