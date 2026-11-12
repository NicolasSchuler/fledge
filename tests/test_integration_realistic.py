"""Opt-in live checks with constructs found in ordinary papers and journal templates.

Each project once blocked inspection or preparation although TeX built it cleanly.
"""

from __future__ import annotations

import unittest
import zipfile

from latexprep.core import run_job
from latexprep.job_context import JobRequest
from latexprep.report import Outcome
from tests.live_support import AsyncLiveProjectTestCase, live_settings, png_bytes
from tests.support import live_test, write_project


def _pdf() -> bytes:
    """Return a minimal one-page vector PDF with a correct cross-reference table."""
    stream = b"0 0 1 rg 0 0 20 20 re f"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 20 20] /Contents 4 0 R >>",
        b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream),
    ]
    output = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(output))
        output += b"%d 0 obj\n%s\nendobj\n" % (number, body)
    xref = len(output)
    output += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    output += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    output += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(output)


TEMPLATE_CLASS = r"""\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{journal}[2024/01/01 v1.0 Example journal template]
\newif\if@review
\DeclareOption{review}{\@reviewtrue}
\ProcessOptions\relax
\LoadClass{article}
\RequirePackage{graphicx}
\if@review\RequirePackage{lineno}\fi
"""

MAIN = r"""\documentclass{journal}
\newif\ifanonymous
\anonymoustrue
\title{A realistic submission}
\ifanonymous\author{Anonymous}\else\author{Ada Example}\fi
\date{}
\begin{document}
\maketitle
\begin{abstract}
We study $t \in [0, 1)$ and report results.
\end{abstract}
\input{sections/intro}
\begin{figure}[h]
\centering
\includegraphics[width=1cm]{figures/plot}
\caption{A plot.}\label{fig:plot}
\end{figure}
See Figure~\ref{fig:plot} and \cite{knuth1984}.
\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""

BIBLIOGRAPHY = """@book{knuth1984,
  author = {Donald E. Knuth},
  title = {The {TeXbook}},
  year = {1984},
  publisher = {Addison-Wesley}
}
"""


@live_test("Set FLEDGE_RUN_INTEGRATION=1 for live TeX checks")
class RealisticProjectTests(AsyncLiveProjectTestCase):
    """``check`` and ``prepare`` of a paper with a template class and ordinary constructs."""

    def setUp(self) -> None:
        """Write the paper: a local class, an interval, matching figures and a spare ``.bib``.

        The figures are a PDF and a PNG of the same name; ``old/`` holds a backup copy of the
        bibliography, and ``figures`` holds a macOS ``Icon`` file whose name ends in a
        carriage return.
        """
        self.source = write_project(
            self.base / "paper",
            {
                "main.tex": MAIN,
                "journal.cls": TEMPLATE_CLASS,
                "sections/intro.tex": "\\section{Introduction}\nAn interval $x \\in (0, 1]$.\n",
                "figures/plot.pdf": _pdf(),
                "figures/plot.png": png_bytes(b"\x00\x80\xff"),
                "refs.bib": BIBLIOGRAPHY,
                "old/refs-backup.bib": BIBLIOGRAPHY,
                "figures/Icon\r": b"",
            },
        )

    async def test_check_is_not_blocked_by_ordinary_constructs(self) -> None:
        report = await run_job(JobRequest("check", self.source, live_settings()))
        self.assert_outcome(report)

    async def test_preserve_and_flat_preparation_complete(self) -> None:
        for layout in ("preserve", "flat"):
            with self.subTest(layout=layout):
                output = self.base / f"prepared-{layout}"
                report = await run_job(
                    JobRequest(
                        "prepare",
                        self.source,
                        live_settings(layout=layout),
                        output,
                    )
                )
                self.assert_outcome(report)
                with zipfile.ZipFile(output / "submission.zip") as archive:
                    names = set(archive.namelist())
                self.assertNotIn("old/refs-backup.bib", names)
                self.assertTrue(any(name.endswith("plot.pdf") for name in names), names)
                self.assertFalse(any(name.endswith("plot.png") for name in names), names)

    async def test_clean_paper_check_passes_without_advisories(self) -> None:
        clean = write_project(
            self.base / "clean",
            {"main.tex": "\\documentclass{article}\n\\begin{document}\nHello.\n\\end{document}\n"},
        )
        report = await run_job(JobRequest("check", clean, live_settings()))
        self.assert_outcome(report, Outcome.PASSED)

    async def test_biblatex_with_biber_prepares(self) -> None:
        project = write_project(
            self.base / "biblatex",
            {
                "main.tex": "\\documentclass{article}\n"
                "\\usepackage[backend=biber]{biblatex}\n\\addbibresource{refs.bib}\n"
                "\\begin{document}\nSee \\cite{knuth1984}.\n\\printbibliography\n"
                "\\end{document}\n",
                "refs.bib": BIBLIOGRAPHY,
            },
        )
        report = await run_job(
            JobRequest(
                "prepare",
                project,
                live_settings(bibliography_backend="biber"),
                self.base / "prepared-biblatex",
            )
        )
        self.assert_outcome(report)


if __name__ == "__main__":
    unittest.main()
