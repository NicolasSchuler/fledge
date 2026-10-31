"""Opt-in live preparation with bibliography transformations under real TeX, Biber and Poppler.

The controlled tests in ``tests.test_bibliography_transform`` and ``tests.test_source_transform``
establish what each transformation proposes. These tests apply the proposals in a real
``prepare`` job and check that the shipped package holds the transformed sources and still
renders exactly like the unchanged baseline: key renames, a merge, field removal and cited-only
output under Biber, and an inlined BibTeX ``.bbl`` under pdfLaTeX.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import subprocess
import tempfile
import unittest
import zipfile
from collections.abc import Iterable
from pathlib import Path

from latexprep.bibliography_transform import BibliographyTransformOptions
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.job_context import JobRequest
from latexprep.report import Outcome, Report
from latexprep.source_transform import SourceTransformOptions
from tests.support import directory_contents, live_test, write_project

EPOCH = 1_700_000_000
# Other live builds may share the host, and Biber preparation alone takes about ten seconds.
JOB_TIMEOUT_SECONDS = 600
PASSING_OUTCOMES = frozenset({Outcome.PASSED, Outcome.PASSED_WITH_ADVISORIES})
# The Poppler tools of the PDF inspection and of the baseline-versus-prepared comparison.
POPPLER_TOOLS = ("pdfinfo", "pdftotext", "pdftoppm")

# Stage names as reports show them. Review rules in settings files name stages literally
# (stage = "baseline versus prepared"), so these are not imported: a rename must fail here.
BASELINE_VERSUS_PREPARED_STAGE = "baseline versus prepared"
FINAL_SOURCE_CHECKS_STAGE = "final source checks"
# The builds of a prepare job with the preserved layout: the unchanged sources, the
# transformed copy, and the rebuild of the extracted archive.
BASELINE_STAGE = "baseline"
TRANSFORMED_STAGE = "transformed"
ARCHIVE_STAGE = "archive"

BIBLATEX_MAIN = r"""\documentclass{article}
\usepackage[backend=biber,style=numeric]{biblatex}
\addbibresource{refs.bib}
\begin{document}
The first study \cite{first} and the second study \cite{second}.
\printbibliography
\end{document}
"""

# ``duplicate`` is an uncited copy of ``first`` with a private file path, and ``unused`` is
# uncited too. ``first`` also has a file field, so the removal changes a printed entry as well.
BIBLATEX_REFERENCES = """@article{first,
  author = {Ada Alpha},
  title = {A First Study},
  journal = {Example Journal},
  year = {2020},
  file = {private/first.pdf}
}
@article{second,
  author = {Bob Beta},
  title = {A Second Study},
  journal = {Example Journal},
  year = {2021}
}
@article{duplicate,
  author = {Ada Alpha},
  title = {A First Study},
  journal = {Example Journal},
  year = {2020},
  file = {private/path}
}
@article{unused,
  author = {Uma Unused},
  title = {An Uncited Study},
  journal = {Example Journal},
  year = {2019}
}
"""

# The shipped refs.bib in order: the cited entries under their final keys, every value as
# written and no file field.
SHIPPED_ENTRIES = [
    (
        "first",
        {
            "author": "Ada Alpha",
            "title": "A First Study",
            "journal": "Example Journal",
            "year": "2020",
        },
    ),
    (
        "renamed_second",
        {
            "author": "Bob Beta",
            "title": "A Second Study",
            "journal": "Example Journal",
            "year": "2021",
        },
    ),
]

BIBTEX_MAIN = r"""\documentclass{article}
\begin{document}
The first study \cite{first} and the second study \cite{second}.
\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""

BIBTEX_REFERENCES = """@article{first,
  author = {Ada Alpha},
  title = {A First Study},
  journal = {Example Journal},
  year = {2020}
}
@article{second,
  author = {Bob Beta},
  title = {A Second Study},
  journal = {Example Journal},
  year = {2021}
}
"""

# A field with a brace-delimited value without nested braces, as every fixture field is.
_FIELD = re.compile(r"(\w+)\s*=\s*\{([^{}]*)\}")


def _biber_settings() -> Settings:
    """Return Biber settings that format entries, drop ``file``, rename one key and merge two.

    Only cited entries are kept, and the fixed epoch and the live job deadline apply.
    """
    return Settings(
        main="main.tex",
        bibliography_backend="biber",
        source_date_epoch=EPOCH,
        job_timeout_seconds=JOB_TIMEOUT_SECONDS,
        bibliography_transform=BibliographyTransformOptions(
            format_entries=True,
            remove_fields=("file",),
            cited_only=True,
            key_renames=(("second", "renamed_second"),),
            # The uncited duplicate is the donor and ``first`` the retained target, so the
            # merge redirects no citation and the rendered reference list cannot change.
            merge_keys=(("duplicate", "first"),),
        ),
    )


def _inlining_settings() -> Settings:
    """Return BibTeX settings that inline the supplied ``main.bbl``, with the fixed epoch."""
    return Settings(
        main="main.tex",
        bibliography_backend="bibtex",
        source_date_epoch=EPOCH,
        job_timeout_seconds=JOB_TIMEOUT_SECONDS,
        source_transforms=SourceTransformOptions(inline_bibliography="main.bbl"),
    )


def _entries(text: str) -> list[tuple[str, dict[str, str]]]:
    """Split a .bib text into its entries.

    Args:
        text: The .bib text, whose fields have brace-delimited values without nested braces.

    Returns:
        Each entry in order: its key and its fields with their values.
    """
    entries = []
    for record in text.split("@")[1:]:
        head, _, body = record.partition(",")
        entries.append((head.partition("{")[2].strip(), dict(_FIELD.findall(body))))
    return entries


def _shipped_files(output: Path) -> dict[str, str]:
    """Read the text of every file in the published submission.zip.

    Args:
        output: The output directory that holds ``submission.zip``.

    Returns:
        The UTF-8 text of each entry by archive name.
    """
    with zipfile.ZipFile(output / "submission.zip") as archive:
        return {name: archive.read(name).decode("utf-8") for name in archive.namelist()}


def _problems(report: Report) -> list[tuple[str | None, str, str, str | None, str]]:
    """List the findings that block or advise, shown when an outcome is not the expected one.

    Args:
        report: The report of the job.

    Returns:
        The code, rule, status, stage and message of each finding that is not informational
        and did not pass.
    """
    return [
        (item.code, item.rule, item.status, item.stage, item.message)
        for item in report.findings
        if item.severity != "info" and item.status != "passed"
    ]


def _statuses(report: Report, code: str, stage: str | None = None) -> list[str]:
    """List the status of every finding with ``code``; only those of ``stage`` when one is given.

    Args:
        report: The report of the job.
        code: The finding code.
        stage: The stage to select, or ``None`` for every stage.

    Returns:
        The statuses in report order.
    """
    return [
        item.status
        for item in report.findings
        if item.code == code and (stage is None or item.stage == stage)
    ]


def _backend_checks(report: Report) -> dict[str | None, tuple[str, object]]:
    """Map each build stage to its BLD201 status and the backends it required.

    The backends are those that the stage's fresh control files required, as the
    ``observed_backends`` detail records them.

    Args:
        report: The report of the job.

    Returns:
        The status and the backends of the BLD201 finding of each stage.
    """
    return {
        item.stage: (item.status, item.details.get("observed_backends"))
        for item in report.findings
        if item.code == "BLD201"
    }


def _run_latexmk(project: Path, scratch: Path) -> subprocess.CompletedProcess[str]:
    """Build a copy of ``project`` in ``scratch`` with the host's latexmk, writing main.bbl.

    ``-norc`` keeps a personal latexmkrc from moving the output elsewhere.

    Args:
        project: The project to copy.
        scratch: The new directory for the copy, which latexmk builds in.

    Returns:
        The completed latexmk process with its text output, also when latexmk failed.
    """
    shutil.copytree(project, scratch)
    return subprocess.run(
        ["latexmk", "-norc", "-pdf", "-interaction=nonstopmode", "main.tex"],
        cwd=scratch,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        timeout=JOB_TIMEOUT_SECONDS,
        check=False,
    )


@live_test("Set FLEDGE_RUN_INTEGRATION=1 for live TeX, Biber and Poppler preparation checks")
class LiveBibliographyPreparationTests(unittest.IsolatedAsyncioTestCase):
    """Real ``prepare`` jobs that transform the bibliography and must render like the baseline."""

    def setUp(self) -> None:
        """Create a temporary base directory."""
        temporary = tempfile.TemporaryDirectory(prefix="fledge-bibliography-")
        self.addCleanup(temporary.cleanup)
        self.base = Path(temporary.name).resolve()

    def require_tools(self, *names: str) -> None:
        """Skip the test unless every one of ``names`` is installed."""
        missing = [name for name in names if shutil.which(name) is None]
        if missing:
            self.skipTest("Required installed tools are absent: " + ", ".join(missing))

    async def prepare(self, source: Path, settings: Settings) -> tuple[Report, dict[str, str]]:
        """Prepare ``source`` into ``prepared`` and require a passing outcome.

        Args:
            source: The project to prepare.
            settings: The settings of the job.

        Returns:
            The report and the text of each shipped file by archive name.
        """
        output = self.base / "prepared"
        report = await run_job(JobRequest("prepare", source, settings, output))
        self.assertIn(report.outcome, PASSING_OUTCOMES, _problems(report))
        return report, _shipped_files(output)

    def assert_passed(self, report: Report, codes: Iterable[str], stage: str | None = None) -> None:
        """Assert that each code has exactly one finding (in ``stage`` when given) and it passed.

        Args:
            report: The report of the job.
            codes: The finding codes.
            stage: The stage to select, or ``None`` for every stage.
        """
        codes = tuple(codes)
        self.assertEqual(
            {code: _statuses(report, code, stage) for code in codes},
            {code: ["passed"] for code in codes},
            f"stage: {stage}",
        )

    async def generate_bbl(self, source: Path) -> str:
        """Run BibTeX through latexmk on a copy of ``source``; put its main.bbl beside main.tex.

        Asserts that latexmk succeeded and wrote main.bbl.

        Args:
            source: The project; it receives the generated main.bbl.

        Returns:
            The text of the generated main.bbl.
        """
        scratch = self.base / "scratch"
        built = await asyncio.to_thread(_run_latexmk, source, scratch)
        # latexmk reports its own errors on stderr and the TeX run on stdout.
        output = built.stdout[-3000:] + built.stderr[-3000:]
        self.assertEqual(built.returncode, 0, output)
        generated = scratch / "main.bbl"
        self.assertTrue(generated.is_file(), output)
        shutil.copyfile(generated, source / "main.bbl")
        return generated.read_text(encoding="utf-8")

    async def test_biber_key_mapping_and_cited_only_output_render_like_the_baseline(self) -> None:
        """Check that Biber key mapping and cited-only output render like the baseline.

        Key renames, a merge, field removal and cited-only output keep the Biber rendering.

        Verifies docs/preparation-options.md, "Bibliography and source transformations":
        renames update the literal citations and the resource together, a merge keeps the
        target's metadata and drops the donor, cited-only output removes unreachable entries,
        selected fields are removed and values are preserved, every edit is made to the separate
        copy only, and that copy must still match the baseline PDF (CMP002, CMP003).
        """
        self.require_tools("latexmk", "pdflatex", "biber", *POPPLER_TOOLS)
        source = write_project(
            self.base / "biblatex", {"main.tex": BIBLATEX_MAIN, "refs.bib": BIBLATEX_REFERENCES}
        )
        originals = directory_contents(source)
        report, shipped = await self.prepare(source, _biber_settings())
        self.assertEqual(sorted(shipped), ["main.tex", "refs.bib"])
        self.assertEqual(_entries(shipped["refs.bib"]), SHIPPED_ENTRIES)
        self.assertNotIn("private/", shipped["refs.bib"])
        self.assertEqual(
            shipped["main.tex"], BIBLATEX_MAIN.replace(r"\cite{second}", r"\cite{renamed_second}")
        )
        # Layout, field removal, cited-only output, and the key mapping of renames and merges.
        self.assert_passed(report, ("BIB201", "BIB202", "BIB203", "BIB204"))
        self.assert_passed(report, ("BIB102",), FINAL_SOURCE_CHECKS_STAGE)
        self.assert_passed(report, ("CMP002", "CMP003"), BASELINE_VERSUS_PREPARED_STAGE)
        # Every build ran Biber: the baseline, the transformed copy and the archive rebuild.
        biber = ("passed", ["biber"])
        self.assertEqual(
            _backend_checks(report),
            {BASELINE_STAGE: biber, TRANSFORMED_STAGE: biber, ARCHIVE_STAGE: biber},
        )
        self.assertEqual(directory_contents(source), originals)

    async def test_inlined_bibtex_bibliography_replaces_the_bibliography_files(self) -> None:
        """Check that an inlined BibTeX bibliography replaces the bibliography files.

        A supplied BibTeX .bbl replaces the bibliography command and renders like the baseline.

        Verifies docs/preparation-options.md, "Bibliography and source transformations":
        bibliography inlining accepts an explicitly supplied literal BibTeX thebibliography
        file; and docs/workflow-reference.md, "How package contents are selected": the final
        input set is recomputed after the transformations, so the copy, whose rebuild reads
        neither the .bib nor the .bbl, ships neither of them.
        """
        self.require_tools("latexmk", "pdflatex", "bibtex", *POPPLER_TOOLS)
        source = write_project(
            self.base / "bibtex", {"main.tex": BIBTEX_MAIN, "refs.bib": BIBTEX_REFERENCES}
        )
        bibliography = await self.generate_bbl(source)
        originals = directory_contents(source)
        report, shipped = await self.prepare(source, _inlining_settings())
        self.assertEqual(sorted(shipped), ["main.tex"])
        main = shipped["main.tex"]
        self.assertIn(r"\begin{thebibliography}", main)
        self.assertIn(bibliography.rstrip("\n"), main)
        self.assertNotIn(r"\bibliography{", main)
        self.assert_passed(report, ("TEX203",))
        self.assert_passed(report, ("CMP002", "CMP003"), BASELINE_VERSUS_PREPARED_STAGE)
        # The baseline ran BibTeX, so it rendered the bibliography that the copy inlines; the
        # builds of the inlined copy need no bibliography backend at all.
        self.assertEqual(
            _backend_checks(report),
            {
                BASELINE_STAGE: ("passed", ["bibtex"]),
                TRANSFORMED_STAGE: ("not_applicable", []),
                ARCHIVE_STAGE: ("not_applicable", []),
            },
        )
        self.assertEqual(directory_contents(source), originals)


if __name__ == "__main__":
    unittest.main()
