"""Workflow contract tests; tool execution is replaced explicitly in these tests."""

from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from latexprep.cli import main
from latexprep.config import Settings
from latexprep.core import JobRequest, run_job, validate_destination
from latexprep.models import Finding, PreparationError


def project(root: Path) -> None:
    (root / "sections").mkdir(parents=True)
    (root / "appendix").mkdir()
    (root / "main.tex").write_text(
        "\\documentclass{article}\n\\begin{document}\n"
        "\\input{sections/results}\n\\input{appendix/results}\n\\end{document}\n"
    )
    (root / "sections/results.tex").write_text("Main results.\n")
    (root / "appendix/results.tex").write_text("Additional results.\n")


class WorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_flat_bundle_from_exact_extraction_and_serial_equivalence(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "input"
            project(source)
            (source / "data.txt").write_text("0" * 100_000)
            original = {p.relative_to(source): p.read_bytes() for p in source.rglob("*.tex")}
            build_inputs = []

            async def build(tree, main, work, engine, runner):
                build_inputs.append((tree, main, sorted(p.name for p in tree.rglob("*.tex"))))
                work.mkdir()
                pdf = work / "built.pdf"
                pdf.write_bytes(b"controlled fake PDF; these tests exercise orchestration only")
                return SimpleNamespace(
                    success=True,
                    pdf=pdf,
                    findings=[],
                    dependencies={"main.tex"},
                    tools={},
                    command=["test-build"],
                    log="",
                )

            passed = [Finding("pdf.comparison", "Controlled comparison", "info", "passed")]
            with (
                patch("latexprep.core.build_project", side_effect=build),
                patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
                patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=passed)),
            ):
                first = await run_job(
                    JobRequest(
                        "prepare",
                        source,
                        Settings(main="./main.tex", layout="flat", jobs=1),
                        base / "serial",
                    )
                )
                second = await run_job(
                    JobRequest(
                        "prepare", source, Settings(layout="flat", jobs=3), base / "parallel"
                    )
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
            self.assertEqual(len(build_inputs), 6)
            self.assertNotEqual(build_inputs[0][0], build_inputs[1][0])
            self.assertNotEqual(build_inputs[1][0], build_inputs[2][0])
            self.assertEqual(build_inputs[1][2], build_inputs[2][2])
            with zipfile.ZipFile(base / "serial/submission.zip") as archive:
                self.assertTrue(all("/" not in name for name in archive.namelist()))
                self.assertEqual(len(archive.namelist()), 4)
            self.assertTrue((base / "serial/report.json").is_file())

    async def test_failed_final_build_never_releases_output(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            source = base / "input"
            project(source)
            count = 0

            async def build(tree, main, work, engine, runner):
                nonlocal count
                count += 1
                work.mkdir()
                pdf = work / "built.pdf"
                pdf.write_bytes(b"fake")
                return SimpleNamespace(
                    success=count < 3,
                    pdf=pdf if count < 3 else None,
                    findings=[],
                    dependencies=set(),
                    tools={},
                    command=["test-build"],
                    log="",
                )

            with (
                patch("latexprep.core.build_project", side_effect=build),
                patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
                patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[])),
            ):
                result = await run_job(
                    JobRequest("prepare", source, Settings(layout="flat"), base / "output")
                )
            self.assertEqual(count, 3)
            self.assertEqual(result.outcome, "blocked")
            self.assertFalse((base / "output").exists())
            self.assertEqual(result.artifacts, {})

    async def test_baseline_failure_keeps_source_diagnostics(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project(base / "input")
            (base / "input/sections/results.tex").write_text("TODO unfinished.\n")
            failure = SimpleNamespace(
                success=False,
                pdf=None,
                findings=[],
                dependencies=set(),
                tools={},
                command=[],
                log="failure",
            )
            with patch("latexprep.core.build_project", new=AsyncMock(return_value=failure)):
                result = await run_job(JobRequest("check", base / "input", Settings()))
            self.assertEqual(result.outcome, "blocked")
            self.assertTrue(any(item.rule == "source-edit-marker" for item in result.findings))

    async def test_ambiguous_root_blocks_without_running_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project(base / "input")
            (base / "input/other.tex").write_bytes((base / "input/main.tex").read_bytes())
            with patch("latexprep.core.build_project", new=AsyncMock()) as build:
                result = await run_job(JobRequest("check", base / "input", Settings()))
            self.assertEqual(result.outcome, "blocked")
            build.assert_not_called()


class CliTests(unittest.TestCase):
    def test_json_has_no_progress_and_original_input_is_unchanged(self):
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            project(base / "input")
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
            project(base / "input")
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
            project(base / "input")
            for output in (base / "input", base / "input/new"):
                with self.subTest(output=output), self.assertRaises(PreparationError):
                    validate_destination(base / "input", output)


if __name__ == "__main__":
    unittest.main()
