"""Smoke-test preparing and rebuilding a small article with Linux system tools."""

from __future__ import annotations

import zipfile

from tests.live_support import LiveProjectTestCase
from tests.support import live_test, minimal_project


@live_test("Set FLEDGE_RUN_INTEGRATION=1 for the Bubblewrap preparation smoke", platform="linux")
class LinuxPrepareSmokeTests(LiveProjectTestCase):
    """Exercise a real TeX build, PDF comparison, ZIP rebuild and publication."""

    def test_prepare_publishes_the_verified_archive_and_pdf(self) -> None:
        source = self.base / "paper"
        minimal_project(source)

        report = self.prepare(source)
        execution = report.tools["execution"]
        assert isinstance(execution, dict)
        self.assertEqual(execution["backend"], "Linux Bubblewrap")
        self.assertEqual(
            [stage["status"] for stage in report.stages if stage["name"] == "archive"],
            ["succeeded"],
        )
        self.assertEqual(
            [
                finding.status
                for finding in report.findings
                if finding.rule == "compare.rendering"
                and finding.stage == "prepared versus archive rebuild"
            ],
            ["passed"],
        )

        output = self.base / "prepared"
        with zipfile.ZipFile(output / "submission.zip") as archive:
            self.assertEqual(archive.namelist(), ["main.tex"])
            self.assertEqual(archive.read("main.tex"), (output / "sources/main.tex").read_bytes())
        self.assertTrue((output / "manuscript.pdf").read_bytes().startswith(b"%PDF-"))
        self.assertTrue((output / "report.json").is_file())
