"""Opt-in real Biber and alternate-engine builds under the application sandbox."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path

from latexprep.runtime import ToolRunner, build_project


class DiagnosticRunner(ToolRunner):
    """Keep bounded fixture diagnostics without changing actual confinement."""

    def __init__(self):
        super().__init__(source_date_epoch=1_700_000_000)
        self.diagnostics = []

    async def run(self, argv, cwd, workspace, **kwargs):
        result = await super().run(argv, cwd, workspace, **kwargs)
        if result.returncode or result.resource_exceeded or result.timed_out:
            self.diagnostics.append(
                {
                    "tool": argv[0],
                    "returncode": result.returncode,
                    "stderr": result.stderr[-6000:],
                    "stdout": result.stdout[-6000:],
                    "resource_exceeded": result.resource_exceeded,
                }
            )
        return result


@unittest.skipUnless(
    os.environ.get("LATEX_PREP_RUN_INTEGRATION") == "1",
    "Enable LATEX_PREP_RUN_INTEGRATION for real restricted TeX/Biber tools",
)
class LiveBackendIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="latex-prep-backends-")
        self.root = Path(self.temporary.name).resolve()
        self.source = self.root / "source"
        self.source.mkdir()
        self.runner = DiagnosticRunner()

    async def asyncTearDown(self):
        self.temporary.cleanup()

    def require_tools(self, *names):
        missing = [name for name in names if shutil.which(name) is None]
        if missing:
            self.skipTest("Required installed tools are absent: " + ", ".join(missing))

    async def test_biber_produces_real_bibliography_and_backend_evidence(self):
        self.require_tools("latexmk", "pdflatex", "biber")
        original = (
            "\\documentclass{article}\n"
            "\\usepackage[backend=biber,style=numeric]{biblatex}\n"
            "\\addbibresource{refs.bib}\n"
            "\\begin{document}A citation \\cite{used}.\\printbibliography\\end{document}\n"
        )
        (self.source / "main.tex").write_text(original)
        (self.source / "refs.bib").write_text(
            "@article{used,author={Example, Ada},title={Restricted backend fixture},"
            "journaltitle={Example Journal},year={2025}}\n"
        )
        build = self.root / "biber"
        result = await build_project(
            self.source,
            "main.tex",
            build,
            "pdflatex",
            self.runner,
            bibliography_backend="biber",
        )
        self.assertTrue(result.success, (result.findings, self.runner.diagnostics))
        self.assertTrue(result.pdf.read_bytes().startswith(b"%PDF-"))
        bcf = ET.parse(build / "output/main.bcf").getroot()
        self.assertEqual(bcf.tag.rsplit("}", 1)[-1], "controlfile")
        self.assertRegex((build / "output/main.bbl").read_text(), r"\\entry\{used\}\{article\}")
        self.assertIn("Biber", (build / "output/main.blg").read_text())
        self.assertEqual(result.tools["bibliography"]["observed_backends"], ["biber"])
        self.assertTrue(result.tools["biber"]["version"])
        self.assertEqual(result.tools["biber"]["version_arguments"], ["--noconf", "--version"])
        for code in ("BLD201", "BLD202"):
            self.assertTrue(
                any(item.code == code and item.status == "passed" for item in result.findings),
                result.findings,
            )
        self.assertEqual((self.source / "main.tex").read_text(), original)

    async def engine_smoke(self, engine):
        self.require_tools("latexmk", engine)
        original = (
            "\\documentclass[11pt,twoside]{article}\n"
            "\\begin{document}Alternate engine fixture.\\end{document}\n"
        )
        (self.source / "main.tex").write_text(original)
        result = await build_project(
            self.source,
            "main.tex",
            self.root / engine,
            engine,
            self.runner,
            inventory_loaded_options=True,
        )
        self.assertTrue(result.success, (result.findings, self.runner.diagnostics))
        self.assertTrue(result.pdf.read_bytes().startswith(b"%PDF-"))
        self.assertTrue(result.tools[engine]["version"])
        inventory = next(item for item in result.findings if item.code == "BLD301")
        self.assertEqual(inventory.status, "passed")
        self.assertEqual(inventory.details["engine"], engine)
        article = next(
            item for item in inventory.details["packages"] if item["name"] == "article.cls"
        )
        self.assertEqual(article["options"], "11pt,twoside")
        self.assertEqual((self.source / "main.tex").read_text(), original)

    async def test_xelatex_real_build_and_kernel_options(self):
        await self.engine_smoke("xelatex")

    async def test_lualatex_real_build_and_kernel_options(self):
        await self.engine_smoke("lualatex")


if __name__ == "__main__":
    unittest.main()
