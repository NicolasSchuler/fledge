"""Opt-in live checks of hostile and structured source projects.

Hostile constructs (a shell-escape command, an absolute input, an absolute write) must
neither run nor reach outside the project. Projects that use ``import`` and ``subimport``,
``subfiles``, and local classes, styles and bibliography styles in subdirectories are
built and flattened with real TeX, where the controlled flattening tests check only the
plan. An EPS figure shows how a build that needs shell escape fails.
"""

from __future__ import annotations

import re
import shutil
import struct
import subprocess
import tempfile
import unicodedata
import unittest
import zipfile
import zlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any, NamedTuple

from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.findings import Finding
from latexprep.job_context import JobRequest
from latexprep.report import Report
from latexprep.submission_options import SubmissionOptions
from tests.support import live_test, write_project

EPOCH = 1_700_000_000
# Builds on a loaded host can be slow; the job deadline only has to catch a hang.
JOB_TIMEOUT_SECONDS = 600
# The outcomes that publish a bundle (docs/reports.md, "Exit codes").
PUBLISHED_OUTCOMES = frozenset({"passed", "passed_with_advisories"})
SHELL_ESCAPE_PREFLIGHT = SubmissionOptions(check_shell_escape=True)
# How to install a package that BasicTeX and Ubuntu's base TeX Live packages lack.
LATEX_EXTRA = "install the TeX Live package {0} (tlmgr install {0}; texlive-latex-extra on Ubuntu)"
# Typesets the shell-escape state that pdfTeX itself reports (0 means disabled).
SHELL_ESCAPE_PROBE = "\\ifnum\\pdfshellescape=0 ShellEscapeOff\\else ShellEscapeOn\\fi"
# In the log of an EPS build: TeX refusing the epstopdf conversion command while shell escape
# is disabled (TeX Live names the restricted repstopdf), and pdftex.def's error for the
# converted PDF that was therefore never written.
EPSTOPDF_REFUSED = re.compile(r"runsystem\(r?epstopdf [^\n]*\)\.\.\.disabled")
CONVERTED_EPS_MISSING = re.compile(r"File `[^'\n]*-eps-converted-to\.pdf' not found")


def _document(body: str, preamble: str = "") -> str:
    return (
        "\\documentclass{article}\n"
        + preamble
        + "\\begin{document}\n"
        + body
        + "\n\\end{document}\n"
    )


def _png() -> bytes:
    """A valid 1 x 1 RGB PNG."""

    def chunk(kind: bytes, data: bytes) -> bytes:
        payload = kind + data
        return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(b"\0\xff\x00\x00"))
        + chunk(b"IEND", b"")
    )


# A hand-written EPS figure: a filled 20 x 20 pt square.
EPS_FIGURE = (
    "%!PS-Adobe-3.0 EPSF-3.0\n"
    "%%BoundingBox: 0 0 20 20\n"
    "%%EndComments\n"
    "0 0 1 setrgbcolor\n"
    "newpath 0 0 moveto 20 0 lineto 20 20 lineto 0 20 lineto closepath fill\n"
    "%%EOF\n"
)

IMPORT_PROJECT = {
    "main.tex": _document("\\import{sections/}{intro}", "\\usepackage{import}\n"),
    "sections/intro.tex": "Introduction.\n\\subimport{sub/}{detail}\n",
    "sections/sub/detail.tex": "Detail text.\n",
}

SUBFILES_MAIN = _document("\\subfile{sections/intro}", "\\usepackage{subfiles}\n")
SUBFILE = (
    "\\documentclass[../main.tex]{subfiles}\n"
    "\\begin{document}\nIntroduction text.\n\\end{document}\n"
)

STYLED_MAIN = r"""\documentclass{styles/journal}
\usepackage{styles/macros}
\graphicspath{{figures/}}
\begin{document}
Published in \journalname.
\includegraphics[width=1cm]{plot}
\include{chapters/one}
See \cite{knuth1984}.
\bibliographystyle{styles/custom}
\bibliography{refs}
\end{document}
"""
JOURNAL_CLASS = (
    "\\NeedsTeXFormat{LaTeX2e}\n\\ProvidesClass{journal}\n"
    "\\LoadClass{article}\n\\RequirePackage{graphicx}\n"
)
MACROS_PACKAGE = (
    "\\NeedsTeXFormat{LaTeX2e}\n\\ProvidesPackage{macros}\n"
    "\\newcommand{\\journalname}{the Journal of Examples}\n"
)
BIBLIOGRAPHY = """@book{knuth1984,
  author = {Donald E. Knuth},
  title = {The {TeXbook}},
  year = {1984},
  publisher = {Addison-Wesley}
}
"""
# The flat names of the styled project's files, and the commands that must name them.
STYLED_FLAT_NAMES = [
    "custom.bst",
    "journal.cls",
    "macros.sty",
    "main.tex",
    "one.tex",
    "plot.png",
    "refs.bib",
]
STYLED_FLAT_COMMANDS = (
    "\\documentclass{journal}",
    "\\usepackage{macros}",
    "\\includegraphics[width=1cm]{plot",
    "\\include{one}",
    "\\bibliographystyle{custom}",
    "\\bibliography{refs}",
)


class Prepared(NamedTuple):
    """One preparation: its report, the input and output folders and the input's bytes."""

    report: Report
    source: Path
    output: Path
    originals: dict[str, bytes]


def _settings(**options: Any) -> Settings:
    return Settings(
        main="main.tex",
        source_date_epoch=EPOCH,
        job_timeout_seconds=JOB_TIMEOUT_SECONDS,
        **options,
    )


def _snapshot(root: Path) -> dict[str, bytes]:
    return {
        path.relative_to(root).as_posix(): path.read_bytes()
        for path in root.rglob("*")
        if path.is_file()
    }


def _archive(output: Path) -> dict[str, bytes]:
    """Every entry of the published ZIP by name; a directory entry has empty content."""
    with zipfile.ZipFile(output / "submission.zip") as archive:
        return {name: archive.read(name) for name in archive.namelist()}


def _with_code(report: Report, code: str) -> list[Finding]:
    return [item for item in report.findings if item.code == code]


def _with_rule(report: Report, rule: str) -> list[Finding]:
    return [item for item in report.findings if item.rule == rule]


def _results(findings: list[Finding]) -> set[tuple[str, str, str | None]]:
    """The distinct (severity, status, path) of ``findings``; empty when there are none."""
    return {(item.severity, item.status, item.path) for item in findings}


def _problems(report: Report) -> list[tuple[str | None, str, str, str]]:
    """The blocking findings, which explain an unexpected outcome."""
    return [
        (item.code, item.rule, item.status, item.message)
        for item in report.findings
        if item.is_blocking
    ]


def _pdf_text(pdftotext: str, pdf: Path) -> str:
    """The text pdftotext extracts from ``pdf``.

    NFKC normalization turns a ligature back into its letters, in case a Poppler version
    extracts the ff of "Off" as the single character U+FB00.
    """
    extracted = subprocess.run(
        [pdftotext, "-enc", "UTF-8", str(pdf), "-"], capture_output=True, check=True, timeout=60
    )
    return unicodedata.normalize("NFKC", extracted.stdout.decode("utf-8"))


def _installed_tex_file(name: str) -> Path | None:
    """Where kpsewhich finds the TeX file ``name``, or ``None``."""
    kpsewhich = shutil.which("kpsewhich")
    if kpsewhich is None:
        return None
    found = subprocess.run(
        [kpsewhich, name], capture_output=True, text=True, check=False, timeout=60
    )
    location = found.stdout.strip()
    return Path(location) if found.returncode == 0 and location else None


@live_test("Set FLEDGE_RUN_INTEGRATION=1 for live TeX checks of hostile and structured sources")
class LiveSourceProjectTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory(prefix="fledge-sources-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    async def prepare(
        self, name: str, files: Mapping[str, str | bytes], **options: Any
    ) -> Prepared:
        """Write ``files`` as the project ``name`` and prepare it into a new folder."""
        source = write_project(self.base / name, files)
        originals = _snapshot(source)
        output = self.base / f"{name}-prepared"
        report = await run_job(JobRequest("prepare", source, _settings(**options), output))
        return Prepared(report, source, output, originals)

    def installed(self, name: str, install: str) -> Path:
        """The TeX file ``name``; skip the test, saying how to install it, when it is absent."""
        location = _installed_tex_file(name)
        if location is None:
            self.skipTest(f"kpsewhich does not find {name}: {install}")
        return location

    def assert_unchanged(self, prepared: Prepared) -> None:
        """The project is byte for byte what it was before the job."""
        self.assertEqual(_snapshot(prepared.source), prepared.originals)

    def assert_published(self, prepared: Prepared) -> dict[str, bytes]:
        """A passing outcome; return the entries of the published ZIP."""
        self.assertIn(prepared.report.outcome, PUBLISHED_OUTCOMES, _problems(prepared.report))
        return _archive(prepared.output)

    def assert_blocked(self, prepared: Prepared) -> None:
        """The blocked outcome, which publishes nothing (docs/workflow.md, step "Publish")."""
        self.assertEqual(prepared.report.outcome, "blocked")
        self.assertEqual(prepared.report.artifacts, {})
        self.assertFalse(prepared.output.exists())

    def assert_rendering_unchanged(self, report: Report) -> None:
        """There is a CMP003 comparison of rendered pages, and every one passed."""
        self.assertEqual({item.status for item in _with_code(report, "CMP003")}, {"passed"})

    async def test_shell_escape_command_is_flagged_and_never_runs(self) -> None:
        """PRV006 flags a literal write18; TeX has shell escape off and nothing is written.

        Documented claims: PRV006 is an optional advisory preflight for literal write18
        (docs/checks.md), and the build runs in a sandbox with shell escape disabled
        (docs/workflow.md, step "Baseline build"). The document typesets pdfTeX's own
        shell-escape state into the published PDF, which shows that shell escape was off;
        the absent marker is the sandbox check: nothing wrote outside the workspace.
        Observed outcome: passed_with_advisories. TeX skips the disabled command, so the
        build succeeds, and PRV006 is a warning.
        """
        pdftotext = shutil.which("pdftotext")
        if pdftotext is None:
            self.skipTest("pdftotext (Poppler) is not on PATH to read the typeset shell escape")
        marker = self.base / "shell-escape-marker"
        body = f"\\immediate\\write18{{touch {marker}}}\n{SHELL_ESCAPE_PROBE}"
        prepared = await self.prepare(
            "shell-escape",
            {"main.tex": _document(body)},
            submission_checks=SHELL_ESCAPE_PREFLIGHT,
        )
        self.assertFalse(marker.exists())
        self.assert_unchanged(prepared)
        report = prepared.report
        self.assertEqual(report.outcome, "passed_with_advisories", _problems(report))
        text = _pdf_text(pdftotext, prepared.output / "manuscript.pdf")
        self.assertIn("ShellEscapeOff", text)
        self.assertNotIn("ShellEscapeOn", text)
        preflight = _with_code(report, "PRV006")
        self.assertEqual(
            {(item.severity, item.status) for item in preflight}, {("warning", "failed")}
        )
        named = {match["path"] for item in preflight for match in item.details["matches"]}
        self.assertEqual(named, {"main.tex"})

    async def test_absolute_input_outside_the_project_blocks(self) -> None:
        """TEX006 rejects a literal absolute input outside the project and blocks the job.

        Documented claims: TEX006 rejects absolute and escaping literal references and is
        mandatory (docs/checks.md, docs/check-backlog.md); a blocked run publishes nothing
        (docs/workflow.md, step "Publish").
        """
        prepared = await self.prepare(
            "absolute-input",
            {"main.tex": _document("\\input{/etc/hosts}\nText.")},
            submission_checks=SHELL_ESCAPE_PREFLIGHT,
        )
        self.assert_unchanged(prepared)
        self.assert_blocked(prepared)
        external = _with_code(prepared.report, "TEX006")
        self.assertEqual(_results(external), {("error", "failed", "main.tex")})

    async def test_absolute_write_outside_the_workspace_is_refused(self) -> None:
        """A TeX write to an absolute path outside the build workspace never happens.

        Documented claim: the build runs in a sandbox, with no unsandboxed fallback
        (docs/workflow.md, docs/limitations.md). Fledge's tool environment also sets TeX's
        openout_any=p, which refuses absolute output paths. Observed: TeX refuses the file
        ("I can't write on file" in the log tail of build.failed) and stops, so the
        baseline build fails with BLD009 and build.failed, and the job is blocked.
        """
        target = self.base / "absolute-write.txt"
        body = (
            "\\newwrite\\out\n"
            f"\\immediate\\openout\\out={target}\n"
            "\\immediate\\write\\out{x}\n"
            "\\immediate\\closeout\\out\nText."
        )
        prepared = await self.prepare(
            "absolute-write",
            {"main.tex": _document(body)},
            submission_checks=SHELL_ESCAPE_PREFLIGHT,
        )
        self.assertFalse(target.exists())
        self.assert_unchanged(prepared)
        self.assert_blocked(prepared)
        report = prepared.report
        self.assertIn("failed", {item.status for item in _with_code(report, "BLD009")})
        logs = [
            str(item.details.get("log_tail", "")) for item in _with_rule(report, "build.failed")
        ]
        refusal = re.compile(rf"I can't write on file `[^'\n]*{re.escape(target.name)}'")
        self.assertTrue(any(refusal.search(log) for log in logs), logs)

    async def test_import_and_subimport_flatten_to_top_level_names(self) -> None:
        """Flat layout rewrites literal import and subimport paths and renders the same.

        Documented claims: flattening supports literal import/subimport
        (docs/workflow-reference.md, "Supported source syntax"), and the prepared PDF must
        match the baseline's rendered pages (CMP003, docs/checks.md).
        """
        self.installed("import.sty", LATEX_EXTRA.format("import"))
        prepared = await self.prepare("import", IMPORT_PROJECT, layout="flat")
        archive = self.assert_published(prepared)
        self.assertEqual(sorted(archive), ["detail.tex", "intro.tex", "main.tex"])
        main, intro = archive["main.tex"].decode(), archive["intro.tex"].decode()
        self.assertIn("\\import{", main)
        self.assertNotIn("sections/", main)
        self.assertIn("\\subimport{", intro)
        self.assertNotIn("sub/", intro)
        self.assert_rendering_unchanged(prepared.report)
        self.assert_unchanged(prepared)

    async def test_subfiles_flatten_with_the_parent_option_rewritten_to_main(self) -> None:
        """Flat layout supports a constrained subfile, and inspect resolves the subfile.

        Documented claims: flattening supports constrained subfile bodies
        (docs/workflow-reference.md, "Supported source syntax"), and TEX005 reports a
        missing literal dependency (docs/checks.md). Observed outcome:
        passed_with_advisories, from the inconclusive bibliography checks of subfile
        content that docs/check-backlog.md lists as unsupported.
        """
        self.installed("subfiles.cls", LATEX_EXTRA.format("subfiles"))
        self.installed("subfiles.sty", LATEX_EXTRA.format("subfiles"))
        files = {"main.tex": SUBFILES_MAIN, "sections/intro.tex": SUBFILE}
        prepared = await self.prepare("subfiles", files, layout="flat")
        archive = self.assert_published(prepared)
        self.assertEqual(sorted(archive), ["intro.tex", "main.tex"])
        self.assertIn("\\documentclass[main.tex]{subfiles}", archive["intro.tex"].decode())
        self.assertNotIn("sections/", archive["main.tex"].decode())
        self.assert_rendering_unchanged(prepared.report)
        self.assert_unchanged(prepared)
        original = await run_job(JobRequest("inspect", prepared.source, _settings()))
        unresolved = [item for item in _with_code(original, "TEX005") if item.status != "passed"]
        self.assertEqual(unresolved, [])
        # Without the subfile the same check fails, so its silence above is not vacuous.
        missing = write_project(self.base / "subfile-missing", {"main.tex": SUBFILES_MAIN})
        control = await run_job(JobRequest("inspect", missing, _settings()))
        self.assertEqual(_results(_with_code(control, "TEX005")), {("error", "failed", "main.tex")})

    async def test_local_class_style_and_bibliography_style_in_subdirectories(self) -> None:
        """Local files in subdirectories build in both layouts; flat moves them to the top.

        Documented claims: flattening rewrites literal include, includegraphics,
        graphicspath, bibliography and bibliographystyle references, and local class and
        style identities constrain renaming (docs/workflow-reference.md, "Supported source
        syntax"); the preserve layout keeps the project's folders.
        """
        bst = self.installed("plain.bst", "it ships with BibTeX (TeX Live package bibtex)")
        files = {
            "main.tex": STYLED_MAIN,
            "styles/journal.cls": JOURNAL_CLASS,
            "styles/macros.sty": MACROS_PACKAGE,
            "styles/custom.bst": bst.read_bytes(),
            "figures/plot.png": _png(),
            "chapters/one.tex": "\\section{One}\nChapter text.\n",
            "refs.bib": BIBLIOGRAPHY,
        }
        for layout in ("preserve", "flat"):
            with self.subTest(layout=layout):
                prepared = await self.prepare(f"styled-{layout}", files, layout=layout)
                archive = self.assert_published(prepared)
                if layout == "flat":
                    self.assert_flat_styled_project(archive)
                else:
                    self.assert_preserved_styled_project(archive, files)
                self.assert_rendering_unchanged(prepared.report)
                self.assert_unchanged(prepared)

    def assert_preserved_styled_project(
        self, archive: dict[str, bytes], files: Mapping[str, str | bytes]
    ) -> None:
        """Every file keeps its folder, and main.tex is shipped unchanged."""
        self.assertEqual({name for name in archive if not name.endswith("/")}, set(files))
        self.assertEqual(archive["main.tex"].decode(), STYLED_MAIN)

    def assert_flat_styled_project(self, archive: dict[str, bytes]) -> None:
        """Every file is at the top level, and main.tex names the flat files."""
        self.assertEqual(sorted(archive), STYLED_FLAT_NAMES)
        main = archive["main.tex"].decode()
        for command in STYLED_FLAT_COMMANDS:
            self.assertIn(command, main)
        for folder in ("styles/", "figures/", "chapters/"):
            self.assertNotIn(folder, main)

    async def test_eps_figure_fails_the_build_instead_of_hanging(self) -> None:
        """An EPS figure needs epstopdf, which needs shell escape; the build fails, not hangs.

        Documented claim: pdfLaTeX converts EPS only through epstopdf with shell escape,
        which the isolated build disables, and TEX011 says so (docs/checks.md). Observed:
        the log tail of build.failed shows TeX refusing the epstopdf command
        ("runsystem(repstopdf ...)...disabled") and pdftex.def's error for the converted
        "-eps-converted-to.pdf" file that was never written; there is no timeout or job
        deadline, and the job is blocked.
        """
        figure = _document("\\includegraphics{figures/plot.eps}", "\\usepackage{graphicx}\n")
        prepared = await self.prepare(
            "eps-figure", {"main.tex": figure, "figures/plot.eps": EPS_FIGURE}
        )
        self.assert_blocked(prepared)
        report = prepared.report
        self.assertEqual(_with_rule(report, "execution.deadline"), [])
        self.assertEqual(_with_rule(report, "build.timeout"), [])
        logs = [
            str(item.details.get("log_tail", "")) for item in _with_rule(report, "build.failed")
        ]
        refused = [
            log
            for log in logs
            if EPSTOPDF_REFUSED.search(log) and CONVERTED_EPS_MISSING.search(log)
        ]
        self.assertTrue(refused, logs)
        advisory = _with_code(report, "TEX011")
        self.assertEqual(_results(advisory), {("info", "failed", "figures/plot.eps")})
        self.assert_unchanged(prepared)


if __name__ == "__main__":
    unittest.main()
