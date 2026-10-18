"""Findings that name a limit or an unsupported input: formatter encoding and size, archive size."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from latexprep.build_result import BuildTools
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.findings import Finding
from latexprep.formatting import MAX_SOURCE_BYTES
from latexprep.job_context import JobRequest
from latexprep.rules import MANDATORY_CODES
from tests.fakes import fake_formatter
from tests.support import controlled_backends, format_with, sectioned_project


class FormatterLimitTests(unittest.IsolatedAsyncioTestCase):
    async def unformattable(
        self, content: bytes, rule: str, limit: int = MAX_SOURCE_BYTES
    ) -> Finding:
        """The finding of ``rule`` for a ``main.tex`` that the formatter cannot take."""
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            (base / "source").mkdir()
            (base / "source" / "main.tex").write_bytes(content)
            with (
                patch("latexprep.formatting.shutil.which", return_value="/fake/tex-fmt"),
                patch("latexprep.formatting.MAX_SOURCE_BYTES", limit),
            ):
                result = await format_with(
                    base / "source", base / "work", fake_formatter([]), Settings()
                )
            self.assertEqual((base / "source" / "main.tex").read_bytes(), content)
        (finding,) = [item for item in result.findings if item.rule == rule]
        return finding

    async def test_a_source_that_is_not_utf8_is_reported_unformattable(self) -> None:
        finding = await self.unformattable(b"caf\xe9\n", "source.format_encoding")
        self.assertEqual((finding.path, finding.severity), ("main.tex", "error"))
        self.assertEqual(finding.code, "FMT003")
        self.assertIn(finding.code, MANDATORY_CODES)

    async def test_a_source_over_the_size_limit_is_reported_unformattable(self) -> None:
        finding = await self.unformattable(
            b"Longer than the limit.\n", "source.format_size", limit=4
        )
        self.assertEqual((finding.path, finding.severity), ("main.tex", "error"))
        self.assertEqual(finding.code, "FMT004")
        self.assertIn(finding.code, MANDATORY_CODES)


class ArchiveLimitTests(unittest.IsolatedAsyncioTestCase):
    async def test_an_archive_over_max_bytes_blocks_verification(self) -> None:
        async def build(tree, main, work, engine, runner, **_options):
            work.mkdir()
            pdf = work / "built.pdf"
            pdf.write_bytes(b"controlled fake PDF")
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

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            sectioned_project(base / "input")
            backends = controlled_backends(build=build)
            result = await run_job(
                JobRequest(
                    "prepare",
                    base / "input",
                    Settings(layout="flat", max_bytes=300),
                    base / "output",
                ),
                backends=backends,
            )
            self.assertFalse((base / "output").exists())
        (finding,) = [item for item in result.findings if item.rule == "package.archive_limit"]
        self.assertEqual(
            (finding.code, finding.severity, finding.status), ("PKG303", "error", "failed")
        )
        self.assertIn("max_import_bytes", finding.message)
        self.assertIn("is 300", finding.message)
        self.assertEqual(result.outcome, "blocked")


if __name__ == "__main__":
    unittest.main()
