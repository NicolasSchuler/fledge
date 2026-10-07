"""Explicit package contents are packaging choices, independent of check selection."""

from __future__ import annotations

import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import AsyncMock, patch

from latexprep.check_selection import CheckSelection
from latexprep.config import Settings, load_settings
from latexprep.core import JobRequest, run_job
from latexprep.models import PreparationError
from latexprep.workflow_options import PackageOptions
from tests.support import MINIMAL_DOCUMENT, fake_build, write_project


class PackageOptionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name)
        self.source = write_project(
            self.base / "input",
            {
                "main.tex": MINIMAL_DOCUMENT,
                "data/results.csv": "a,b\n1,2\n",
                "notes/unrelated.txt": "draft\n",
            },
        )

    async def prepare(self, settings: Settings) -> zipfile.ZipFile:
        with (
            patch("latexprep.core.build_project", side_effect=fake_build),
            patch("latexprep.core.inspect_pdf", new=AsyncMock(return_value=[])),
            patch("latexprep.core.compare_pdfs", new=AsyncMock(return_value=[])),
        ):
            report = await run_job(JobRequest("prepare", self.source, settings, self.base / "out"))
        self.assertIn(report.outcome, {"passed", "passed_with_advisories"}, report.to_dict())
        archive = zipfile.ZipFile(self.base / "out" / "submission.zip")
        self.addCleanup(archive.close)
        return archive

    async def test_included_file_ships_even_when_deliverable_checks_are_ignored(self) -> None:
        settings = Settings(
            package=PackageOptions(include=("data/results.csv",)),
            checks=CheckSelection(ignore=("PKG104",)),
        )
        archive = await self.prepare(settings)
        self.assertEqual(sorted(archive.namelist()), ["data/", "data/results.csv", "main.tex"])

    async def test_included_file_follows_the_flat_filename_map(self) -> None:
        archive = await self.prepare(
            Settings(layout="flat", package=PackageOptions(include=("data/results.csv",)))
        )
        self.assertEqual(sorted(archive.namelist()), ["main.tex", "results.csv"])

    def test_include_paths_are_exact_relative_paths(self) -> None:
        for paths in (("../escape.txt",), ("/abs.txt",), ("data/*.csv",), ("a", "a")):
            with self.subTest(paths=paths), self.assertRaises(PreparationError):
                PackageOptions(include=paths)
        path = self.base / "settings.toml"
        path.write_text('[package]\ninclude = ["data/results.csv"]\n')
        self.assertEqual(load_settings(path, {}).package.include, ("data/results.csv",))


if __name__ == "__main__":
    unittest.main()
