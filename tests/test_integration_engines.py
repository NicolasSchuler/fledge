"""Opt-in live jobs with XeLaTeX and LuaLaTeX, mixed-engine packages and real PDF policies.

``test_integration_backends`` builds with each engine at the ``build_project`` level only.
These tests run whole ``prepare`` and ``check`` jobs with the real engines, Poppler and the
sandbox, and check what the job publishes and reports.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unicodedata
import unittest
import zipfile
from pathlib import Path
from typing import Any, NamedTuple

from latexprep.config import Settings, load_settings
from latexprep.core import run_job
from latexprep.job_context import JobRequest
from latexprep.pdf_inspection import PdfInspectionOptions
from latexprep.report import Report
from tests.support import live_test, write_project

EPOCH = 1_700_000_000
# Other live builds may share the host, and LuaLaTeX starts slowly; never assert timings.
JOB_TIMEOUT_SECONDS = 600
PASSING_OUTCOMES = frozenset({"passed", "passed_with_advisories"})
# Page count, extracted text and rendered pages: the three checks of every PDF comparison.
COMPARISON_CODES = ("CMP001", "CMP002", "CMP003")
ALL_COMPARISONS_PASSED = dict.fromkeys(COMPARISON_CODES, "passed")
BASELINE_VERSUS_PREPARED = "baseline versus prepared"
PREPARED_VERSUS_ARCHIVE = "prepared versus archive rebuild"
# The stages that compare a document with the reference PDF its settings supply.
SUPPLIED_VERSUS_BASELINE = "supplied PDF versus baseline"
SUPPLIED_VERSUS_ARCHIVE = "supplied PDF versus archive rebuild"
# The Type 3, forms and attachments policies of the final PDF.
POLICY_CODES = frozenset({"PDF105", "PDF107", "PDF109"})
# The Poppler tools that PDF inspection and comparison run, as docs/doctor.md lists them.
POPPLER_TOOLS = ("pdfinfo", "pdftotext", "pdftoppm", "pdffonts", "pdfdetach", "pdfimages")
# XeLaTeX writes XDV, which xdvipdfmx turns into the PDF (docs/doctor.md).
ENGINE_TOOLS = {"xelatex": ("xelatex", "xdvipdfmx")}
UNICODE_TEXT = "Ünïcödé, naïve café – “quotes”"
# What docs/preparation-options.md lists in each published package directory.
PACKAGE_ENTRIES = ["manuscript.pdf", "report.json", "sources", "submission.zip"]
# A settings file with two independent documents; the reference path is relative to it.
DOCUMENT_SETTINGS = """\
source_date_epoch = {epoch}
job_timeout_seconds = {timeout}

[[workflow.documents]]
name = "paper"
main = "main.tex"
source_directory = "paper"

[[workflow.documents]]
name = "supplement"
main = "main.tex"
source_directory = "supplement"
engine = "{engine}"
reference_pdf = "refs/supplement.pdf"
"""


class PolicyCase(NamedTuple):
    """A document whose built PDF passes or fails the configured PDF policies.

    Attributes:
        name: The name of the case, which names its project directory and subtest.
        preamble: The preamble of the article.
        body: The body of the article.
        options: The PDF inspection options of the check.
        expected: The status each configured policy code must have.
        required_file: A TeX file that ``kpsewhich`` must find, or ``None``.
        missing_reason: The skip reason when ``kpsewhich`` does not find ``required_file``.
        extra_files: Further project files, each as its path and its text.
    """

    name: str
    preamble: str
    body: str
    options: PdfInspectionOptions
    expected: dict[str, str]
    required_file: str | None = None
    missing_reason: str = ""
    extra_files: tuple[tuple[str, str], ...] = ()


POLICY_CASES = (
    PolicyCase(
        "forms",
        "\\usepackage{hyperref}\n",
        "\\begin{Form}\\TextField{Name}\\end{Form}",
        PdfInspectionOptions(forbid_forms=True),
        {"PDF107": "failed"},
        "hyperref.sty",
        "kpsewhich did not find hyperref.sty (TeX Live package hyperref), "
        "which provides the form field",
    ),
    PolicyCase(
        "attachments",
        "\\usepackage{embedfile}\n\\embedfile{data.csv}\n",
        "Measurements are attached.",
        PdfInspectionOptions(forbid_attachments=True),
        {"PDF109": "failed"},
        "embedfile.sty",
        "kpsewhich did not find embedfile.sty (TeX Live package embedfile: "
        "tlmgr install embedfile, or texlive-latex-extra on Debian and Ubuntu)",
        (("data.csv", "x,y\n1,2\n"),),
    ),
    # Fledge runs TeX with MKTEXPK=0, so it never generates a bitmap font: T1 text without
    # cm-super fails the build (BLD009, font not found) instead of embedding Type 3 fonts.
    # TeX Live's cm package ships 600 dpi bitmaps of some Computer Modern fonts; without
    # its Type 1 map entry, pdfTeX embeds the bitmap of cmbx10 as a Type 3 font.
    PolicyCase(
        "type3",
        "\\pdfpkresolution=600\n\\pdfmapline{-cmbx10}\n",
        "Outline text and \\textbf{bitmap bold text}.",
        PdfInspectionOptions(forbid_type3_fonts=True),
        {"PDF105": "failed"},
        "cmbx10.600pk",
        "kpsewhich did not find cmbx10.600pk, the 600 dpi bitmap font of TeX Live "
        "package cm; Fledge never generates bitmap fonts, so no Type 3 font can be built",
    ),
    PolicyCase(
        "control",
        "",
        "Plain text in outline fonts.",
        PdfInspectionOptions(forbid_type3_fonts=True, forbid_forms=True, forbid_attachments=True),
        {"PDF105": "passed", "PDF107": "passed", "PDF109": "passed"},
    ),
)


def _settings(**values: Any) -> Settings:
    """Return settings with the fixed build date and the deadline these live jobs need.

    Args:
        **values: Further settings.

    Returns:
        The settings.
    """
    return Settings(source_date_epoch=EPOCH, job_timeout_seconds=JOB_TIMEOUT_SECONDS, **values)


def _article(body: str, preamble: str = "") -> str:
    """Return the text of an article with ``preamble`` and ``body``."""
    return f"\\documentclass{{article}}\n{preamble}\\begin{{document}}\n{body}\n\\end{{document}}\n"


def _tex_file_found(name: str) -> bool:
    """Return whether ``kpsewhich`` finds the TeX file ``name``: a package, font or bitmap font.

    Returns:
        Whether ``kpsewhich`` is installed and found the file.
    """
    kpsewhich = shutil.which("kpsewhich")
    if kpsewhich is None:
        return False
    probe = subprocess.run([kpsewhich, name], capture_output=True, check=False, timeout=60)
    return probe.returncode == 0


def _pdf_text(pdf: Path) -> str:
    """Return the text Poppler extracts from ``pdf``, composed (NFC) so letters compare equal.

    Returns:
        The text that ``pdftotext`` extracts, in NFC.

    Raises:
        subprocess.CalledProcessError: ``pdftotext`` failed.
    """
    extracted = subprocess.run(
        [shutil.which("pdftotext") or "pdftotext", "-enc", "UTF-8", str(pdf), "-"],
        capture_output=True,
        check=True,
        timeout=120,
    )
    return unicodedata.normalize("NFC", extracted.stdout.decode("utf-8"))


def _blocking(report: Report) -> list[tuple[str | None, str, str, str | None, str]]:
    """List the blocking findings of ``report``, to explain a failed assertion.

    Returns:
        The code, rule, status, stage and message of each blocking finding.
    """
    return [
        (item.code, item.rule, item.status, item.stage, item.message)
        for item in report.findings
        if item.is_blocking
    ]


def _comparison_statuses(
    findings: list[dict[str, Any]], stage: str, document: str | None = None
) -> dict[str, str]:
    """Collect the status of each PDF comparison check that ``stage`` recorded for ``document``.

    A single-document report names no document; the combined report of several documents
    names each finding's document.

    Args:
        findings: The findings in the JSON layout of a report.
        stage: The comparison stage.
        document: The document name, or ``None`` for a single-document report.

    Returns:
        The status of each comparison code.
    """
    return {
        item["code"]: item["status"]
        for item in findings
        if item["code"] in COMPARISON_CODES
        and item["details"].get("stage") == stage
        and item["details"].get("document") == document
    }


def _read_json(path: Path) -> dict[str, Any]:
    """Read the UTF-8 JSON file at ``path``."""
    return json.loads(path.read_text(encoding="utf-8"))


@live_test("Set FLEDGE_RUN_INTEGRATION=1 for live XeLaTeX, LuaLaTeX and PDF policy jobs")
class LiveEngineWorkflowTests(unittest.IsolatedAsyncioTestCase):
    """Real ``prepare`` and ``check`` jobs with XeLaTeX, LuaLaTeX and mixed-engine packages."""

    def setUp(self) -> None:
        """Create a temporary base directory."""
        temporary = tempfile.TemporaryDirectory(prefix="fledge-engines-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def require_tools(self, *names: str) -> None:
        """Skip the test unless every one of ``names`` is installed."""
        missing = [name for name in names if shutil.which(name) is None]
        if missing:
            self.skipTest("Required installed tools are absent: " + ", ".join(missing))

    def require_build_tools(self, *engines: str) -> None:
        """Skip unless latexmk, the engines and the Poppler tools of the checks are installed."""
        tools = [name for engine in engines for name in ENGINE_TOOLS.get(engine, (engine,))]
        self.require_tools("latexmk", *tools, *POPPLER_TOOLS)

    def assert_engine_version(self, report: Report, engine: str) -> None:
        """Assert that ``report`` records a non-blank version of ``engine``."""
        version = report.tools[engine]["version"]
        self.assertTrue(any(line.strip() for line in version), report.tools[engine])

    def write_unicode_article(self, engine: str) -> Path:
        """Write an article whose UTF-8 text is in an input file, loading fontspec when installed.

        Without fontspec.sty the article relies on the kernel's Unicode default. Both use
        Latin Modern, which TeX Live ships, so no system font is needed. That variant is
        reported as a skipped ``fontspec`` subtest, so the log shows which one ran.

        Args:
            engine: The engine, which names the project directory.

        Returns:
            The project directory.
        """
        fontspec = _tex_file_found("fontspec.sty")
        if not fontspec:
            with self.subTest(variant="fontspec"):
                self.skipTest(
                    "kpsewhich did not find fontspec.sty: the article was built without "
                    "\\usepackage{fontspec}, with the kernel's Unicode default font setup"
                )
        preamble = "\\usepackage{fontspec}\n" if fontspec else ""
        return write_project(
            self.base / f"{engine}-article",
            {
                "main.tex": _article("\\input{sections/body}", preamble),
                "sections/body.tex": f"{UNICODE_TEXT}.\n",
            },
        )

    async def test_xelatex_prepares_a_unicode_article(self) -> None:
        """Check that a XeLaTeX article with UTF-8 text goes through every step of prepare.

        docs/workflow.md: the prepared copy is compared with the baseline, and the rebuild
        of the fresh archive extraction with the prepared PDF, by page count, extracted text
        and renders; the output is published only when every step passed. The report
        records the engine's version. Without fontspec.sty the article is built with the
        kernel's Unicode default, and a skipped ``fontspec`` subtest says so.
        """
        await self.assert_engine_prepares_a_unicode_article("xelatex")

    async def test_lualatex_prepares_a_unicode_article(self) -> None:
        """Check that a LuaLaTeX article with UTF-8 text goes through every step of prepare.

        docs/preparation-options.md documents ``engine = "lualatex"``. luaotfload writes its
        font cache under the build's tool state (``TEXMFVAR``) and reads it back, and the TeX
        recorder lists that read. Dependency selection counts the cache as toolchain
        evidence, so the evidence stays complete and the package is published. Before that,
        the cache was an input outside the declared paths, the evidence was incomplete and
        every LuaLaTeX prepare was blocked with PKG302.
        """
        await self.assert_engine_prepares_a_unicode_article("lualatex")

    async def assert_engine_prepares_a_unicode_article(self, engine: str) -> None:
        """Prepare the Unicode article with ``engine`` and assert that every step passed."""
        self.require_build_tools(engine)
        source = self.write_unicode_article(engine)
        output = self.base / f"{engine}-prepared"
        settings = _settings(engine=engine, layout="flat", main="main.tex")
        report = await run_job(JobRequest("prepare", source, settings, output))
        self.assertIn(report.outcome, PASSING_OUTCOMES, _blocking(report))
        self.assert_engine_version(report, engine)
        # Every build established the inputs the submission needs; none was incomplete.
        self.assertTrue(report.tools["submission_dependencies"]["complete"], report.tools)
        findings = report.to_dict()["findings"]
        self.assertNotIn("build.submission_dependencies", {item["rule"] for item in findings})
        for stage in (BASELINE_VERSUS_PREPARED, PREPARED_VERSUS_ARCHIVE):
            self.assertEqual(_comparison_statuses(findings, stage), ALL_COMPARISONS_PASSED, stage)
        pdf = output / "manuscript.pdf"
        self.assertTrue(pdf.read_bytes().startswith(b"%PDF-"))
        self.assertIn(UNICODE_TEXT, _pdf_text(pdf))
        with zipfile.ZipFile(output / "submission.zip") as archive:
            names = archive.namelist()
            sources = [
                archive.read(name).decode("utf-8") for name in names if name.endswith(".tex")
            ]
        self.assertTrue(names, "the archive is empty")
        self.assertTrue(all("/" not in name for name in names), names)
        self.assertTrue(any(UNICODE_TEXT in text for text in sources), sources)

    async def test_xelatex_supplement_and_pdflatex_paper_release_together(self) -> None:
        """Check that a XeLaTeX supplement and a pdfLaTeX paper release together.

        Independent packages, a pdfLaTeX paper and a XeLaTeX supplement, release together.

        docs/preparation-options.md, independent packages: each document builds with its
        own engine and is compared with its reference PDF, which resolves relative to the
        settings file; all packages verify before any is released, in the documented
        layout, and a failed supplement blocks the entire release. The documented example
        gives the supplement ``engine = "lualatex"``; the supplement uses XeLaTeX here because
        every LuaLaTeX build rebuilds its font database in a fresh tool state, and
        test_lualatex_prepares_a_unicode_article covers prepare with LuaLaTeX.
        """
        engine = "xelatex"
        self.require_build_tools("pdflatex", engine)
        source = self.base / "input"
        for name in ("paper", "supplement"):
            write_project(source / name, {"main.tex": _article(f"{name.title()} body text.")})
        reference = await self.prepare_reference(source / "supplement", engine)
        settings = self.load_document_settings(reference, engine)
        output = self.base / "output"
        report = await run_job(JobRequest("prepare", source, settings, output))
        self.assertIn(report.outcome, PASSING_OUTCOMES, _blocking(report))
        self.assert_published_packages(output, engine)
        # The supplement no longer matches its reference, so neither package is released.
        shutil.copyfile(output / "paper" / "manuscript.pdf", reference)
        await self.assert_mismatched_reference_blocks_release(source, settings)

    async def prepare_reference(self, supplement: Path, engine: str) -> Path:
        """Prepare the supplement alone; copy its PDF to refs/ beside the future settings.

        Args:
            supplement: The supplement project.
            engine: The engine of the supplement.

        Returns:
            The copied reference PDF, ``settings/refs/supplement.pdf``.
        """
        output = self.base / "supplement-alone"
        settings = _settings(engine=engine, main="main.tex")
        report = await run_job(JobRequest("prepare", supplement, settings, output))
        self.assertIn(report.outcome, PASSING_OUTCOMES, _blocking(report))
        reference = self.base / "settings" / "refs" / "supplement.pdf"
        reference.parent.mkdir(parents=True)
        shutil.copyfile(output / "manuscript.pdf", reference)
        return reference

    def load_document_settings(self, reference: Path, engine: str) -> Settings:
        """Write and load the settings of the paper and the supplement beside ``refs/``.

        Asserts that the supplement's reference PDF is resolved from the settings file's
        directory and that only the supplement sets an engine.

        Args:
            reference: The reference PDF of the supplement.
            engine: The engine of the supplement.

        Returns:
            The loaded settings.
        """
        path = reference.parent.parent / "fledge.toml"
        path.write_text(
            DOCUMENT_SETTINGS.format(epoch=EPOCH, timeout=JOB_TIMEOUT_SECONDS, engine=engine),
            encoding="utf-8",
        )
        settings = load_settings(path, {})
        documents = {document.name: document for document in settings.workflow.documents}
        # Resolved from the settings file's directory, not from the working directory.
        self.assertEqual(documents["supplement"].reference_pdf, str(reference))
        self.assertEqual(documents["supplement"].engine, engine)
        self.assertIsNone(documents["paper"].engine)
        return settings

    def assert_published_packages(self, output: Path, engine: str) -> None:
        """Assert that each package has its own directory and report, beside the combined report.

        Args:
            output: The output directory of the job.
            engine: The engine of the supplement; the paper uses pdfLaTeX.
        """
        self.assertEqual(
            sorted(path.name for path in output.iterdir()), ["paper", "report.json", "supplement"]
        )
        engines = {"paper": "pdflatex", "supplement": engine}
        for name, document_engine in engines.items():
            package = output / name
            self.assertEqual(sorted(path.name for path in package.iterdir()), PACKAGE_ENTRIES)
            self.assertTrue((package / "sources").is_dir())
            self.assertTrue((package / "manuscript.pdf").read_bytes().startswith(b"%PDF-"))
            with zipfile.ZipFile(package / "submission.zip") as archive:
                self.assertIn(f"{name.title()} body text.", archive.read("main.tex").decode())
            report = _read_json(package / "report.json")
            self.assertEqual(report["settings"]["engine"], document_engine, name)
            # Each document's builds ran with its own engine only.
            self.assertEqual(set(engines.values()) & set(report["tools"]), {document_engine})
        supplement = _read_json(output / "supplement" / "report.json")["findings"]
        for stage in (SUPPLIED_VERSUS_BASELINE, SUPPLIED_VERSUS_ARCHIVE):
            self.assertEqual(_comparison_statuses(supplement, stage), ALL_COMPARISONS_PASSED, stage)
        combined = _read_json(output / "report.json")["findings"]
        self.assertEqual(
            [item["status"] for item in combined if item["code"] == "PKG301"], ["passed"]
        )

    async def assert_mismatched_reference_blocks_release(
        self, source: Path, settings: Settings
    ) -> None:
        """Assert that a supplement differing from its reference keeps both packages back.

        The paper still verifies and keeps its findings, although nothing is released.

        Args:
            source: The project with both documents.
            settings: The settings of both documents.
        """
        output = self.base / "output-mismatched"
        report = await run_job(JobRequest("prepare", source, settings, output))
        self.assertEqual(report.outcome, "blocked", _blocking(report))
        self.assertFalse(output.exists())
        findings = report.to_dict()["findings"]
        supplied = _comparison_statuses(findings, SUPPLIED_VERSUS_BASELINE, "supplement")
        self.assertEqual(supplied.get("CMP002"), "failed", supplied)
        releases = [item for item in findings if item["code"] == "PKG301"]
        self.assertEqual([item["status"] for item in releases], ["failed"], releases)
        verified = {item["name"]: item["verified"] for item in releases[0]["details"]["documents"]}
        self.assertEqual(verified, {"paper": True, "supplement": False})
        # The paper verified and keeps its findings, although nothing was released.
        paper = _comparison_statuses(findings, PREPARED_VERSUS_ARCHIVE, "paper")
        self.assertEqual(paper, ALL_COMPARISONS_PASSED)

    async def test_real_pdf_policy_violations_block_a_check(self) -> None:
        """Check that forms, attachments and Type 3 fonts in a really built PDF fail their policies.

        docs/checks.md: PDF107 rejects forms that pdfinfo reports, PDF109 embedded files
        that pdfdetach reports and PDF105 fonts that pdffonts reports as Type 3, each when
        its policy is enabled; a failed policy is an error that blocks the run, and a
        plain document passes all three.
        """
        self.require_build_tools("pdflatex")
        for case in POLICY_CASES:
            with self.subTest(case=case.name):
                await self.check_policy_case(case)

    async def check_policy_case(self, case: PolicyCase) -> None:
        """Check the document of ``case`` and assert the expected policy statuses.

        Skips when ``kpsewhich`` does not find the case's required file. Only the failed
        policies may block the run.
        """
        if case.required_file is not None and not _tex_file_found(case.required_file):
            self.skipTest(case.missing_reason)
        files = {"main.tex": _article(case.body, case.preamble), **dict(case.extra_files)}
        source = write_project(self.base / f"policy-{case.name}", files)
        settings = _settings(main="main.tex", pdf=case.options)
        report = await run_job(JobRequest("check", source, settings))
        statuses = {item.code: item.status for item in report.findings if item.code in POLICY_CODES}
        self.assertEqual(statuses, case.expected, _blocking(report))
        failed = {code for code, status in case.expected.items() if status == "failed"}
        # The policy itself blocks the run, not a build or tool problem.
        blocking = {item.code for item in report.findings if item.is_blocking}
        self.assertEqual(blocking, failed, _blocking(report))
        self.assertIn(report.outcome, {"blocked"} if failed else PASSING_OUTCOMES)


if __name__ == "__main__":
    unittest.main()
