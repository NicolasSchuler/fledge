r"""Style suggestions (``STY``): obsolete constructs, floats, references, typography, wording.

Every check is run through :func:`check_manuscript_policies` with an explicit selection, as
a job runs it, on a document that triggers it and on a clean twin that does not. The
complexity tests feed hostile text to every new pattern and expect a linear scan.
"""

from __future__ import annotations

import time

from latexprep.check_selection import CheckSelection
from latexprep.config import Settings
from latexprep.core import run_job
from latexprep.findings import Finding
from latexprep.inline_suppressions import split_silenced_suggestions
from latexprep.job_context import JobRequest
from latexprep.manuscript_check_options import ManuscriptCheckOptions
from latexprep.manuscript_checks import check_manuscript_policies
from latexprep.rules import BY_CODE
from latexprep.style_suggestions import STYLE_CODES
from latexprep.style_support import MAX_FINDINGS_PER_RULE
from tests.support import AsyncProjectTestCase, ProjectTestCase, clean_case

# Characters of hostile text; a pattern that backtracks quadratically takes many seconds.
HOSTILE_CHARACTERS = 200_000
LINEAR_TIME_LIMIT_SECONDS = 2.0
DOCUMENT_START = "\\documentclass{article}\n\\begin{document}\n"


class StyleTestCase(ProjectTestCase):
    """Writes a one-file document and runs the selected style suggestions on it."""

    def suggestions(
        self,
        body: str,
        code: str,
        *,
        preamble: str = "",
        options: ManuscriptCheckOptions | None = None,
    ) -> list[Finding]:
        r"""Return the findings of the suggestions ``code`` selects for a document with ``body``.

        Args:
            body: The text between ``\begin{document}`` and ``\end{document}``.
            code: The style code, or code prefix, to select beside ``ALL``.
            preamble: Lines between the class and ``\begin{document}``.
            options: The manuscript policies that run as well.

        Returns:
            The findings whose code starts with ``code``.
        """
        self.write(
            "main.tex",
            "\\documentclass{article}\n" + preamble + "\\begin{document}\n" + body + "\n"
            "\\end{document}\n",
        )
        findings = check_manuscript_policies(
            self.root,
            "main.tex",
            options or ManuscriptCheckOptions(),
            selection=CheckSelection(select=("ALL", code)),
        )
        return [finding for finding in findings if (finding.code or "").startswith(code)]

    def assert_suggested(self, findings: list[Finding], lines: list[int]) -> None:
        """Assert that ``findings`` are suggestions that failed on exactly ``lines``."""
        self.assertEqual([finding.line for finding in findings], lines, findings)
        for finding in findings:
            self.assertEqual((finding.severity, finding.status), ("suggestion", "failed"))
            self.assertEqual(finding.path, "main.tex")
            self.assertEqual(finding.evidence_kind, "heuristic")


class SelectionTests(StyleTestCase):
    """Suggestions run only for explicit selectors and never with the default selection."""

    def test_the_default_selection_runs_no_suggestion_and_a_family_runs_all(self) -> None:
        self.write("main.tex", DOCUMENT_START + "{\\bf Bold} text...\n\\end{document}\n")
        default = check_manuscript_policies(self.root, "main.tex", selection=CheckSelection())
        self.assertEqual(default, [])
        family = check_manuscript_policies(
            self.root, "main.tex", selection=CheckSelection(select=("ALL", "STY"))
        )
        self.assertEqual({finding.code for finding in family}, {"STY102", "STY305", "STY501"})
        ignored = check_manuscript_policies(
            self.root,
            "main.tex",
            selection=CheckSelection(select=("ALL", "STY"), ignore=("STY3",)),
        )
        self.assertEqual({finding.code for finding in ignored}, {"STY102", "STY501"})

    def test_every_style_code_is_a_catalogued_suggestion_with_a_reference(self) -> None:
        for code in STYLE_CODES:
            with self.subTest(code=code):
                rule = BY_CODE[code]
                self.assertTrue(rule.suggestion)
                self.assertTrue(rule.reference_url and rule.reference_url.startswith("https://"))

    def test_a_comment_in_the_sources_silences_one_occurrence(self) -> None:
        findings = self.suggestions(
            "Fast... % fledge: ignore STY305\nSlow...", "STY305", preamble=""
        )
        kept, silenced = split_silenced_suggestions(findings, self.root)
        self.assertEqual([item.line for item in kept], [4])
        self.assertEqual(
            [item.to_report() for item in silenced],
            [{"code": "STY305", "path": "main.tex", "line": 3}],
        )

    def test_occurrences_beyond_the_limit_are_counted_in_one_more_finding(self) -> None:
        findings = self.suggestions("Text... " * (MAX_FINDINGS_PER_RULE + 3), "STY305")
        self.assertEqual(len(findings), MAX_FINDINGS_PER_RULE + 1)
        self.assertEqual(findings[-1].details, {"not_listed": 3})
        self.assertIsNone(findings[-1].line)


class StyleJobTests(AsyncProjectTestCase):
    """A whole inspect job: suggestions reach the report only when selected, never the outcome."""

    async def test_inspect_reports_selected_suggestions_without_changing_the_outcome(self) -> None:
        source = self.root / "paper"
        self.write("paper/main.tex", DOCUMENT_START + "{\\bf Bold} text...\n\\end{document}\n")
        outcomes = {}
        for name, select in (("default", ("ALL",)), ("style", ("ALL", "STY"))):
            settings = Settings(main="main.tex", checks=CheckSelection(select=select))
            report = await run_job(
                JobRequest("inspect", source, settings, self.root / f"work-{name}")
            )
            outcomes[name] = report.outcome
            codes = {item.code for item in report.findings if item.severity == "suggestion"}
            expected = {"STY102", "STY305", "STY501"} if name == "style" else set()
            self.assertEqual(codes, expected, name)
        self.assertEqual(outcomes["default"], outcomes["style"])


class ObsoleteConstructTests(StyleTestCase):
    """``STY101`` and ``STY102``: obsolete packages, driver options, commands, environments."""

    def test_obsolete_packages_and_driver_options(self) -> None:
        preamble = (
            "\\usepackage{times}\n"
            "\\usepackage{subfigure,amsmath}\n"
            "\\usepackage[pdftex]{graphicx}\n"
            "\\usepackage{fixltx2e}\n"
        )
        findings = self.suggestions("Text.", "STY101", preamble=preamble)
        self.assert_suggested(findings, [2, 3, 4, 5])
        self.assertIn("newtxtext", findings[0].message)
        self.assertEqual(findings[2].details["option"], "pdftex")
        classes = self.suggestions("Text.", "STY101", preamble="\\documentclass[dvips]{article}\n")
        self.assertEqual([finding.details["option"] for finding in classes], ["dvips"])
        # When the output driver policy runs, it reports driver options and psfig itself.
        policy = ManuscriptCheckOptions(forbid_output_drivers=True)
        moved = self.suggestions(
            "Text.",
            "STY101",
            preamble="\\usepackage[pdftex]{graphicx}\\usepackage{psfig}\n",
            options=policy,
        )
        self.assertEqual(moved, [])
        clean = self.suggestions(
            "Text.",
            "STY101",
            preamble="\\usepackage{newtxtext,newtxmath}\n\\usepackage[final]{graphicx}\n"
            "% \\usepackage{times}\n",
        )
        self.assertEqual(clean, [])

    def test_obsolete_commands_environments_and_dollars(self) -> None:
        body = (
            "Prior work {\\bf matters} and {\\it so} does {\\tt code}.\n"
            "$$ E = mc^2 $$\n"
            "\\begin{eqnarray}\na &=& {b \\over c}\n\\end{eqnarray}\n"
            "\\centerline{Centred}\n"
        )
        findings = self.suggestions(body, "STY102")
        self.assert_suggested(findings, [3, 3, 3, 4, 5, 6, 8])
        self.assertEqual(
            [finding.details["construct"] for finding in findings],
            ["bf", "it", "tt", "$$", "eqnarray", "over", "centerline"],
        )
        clean = self.suggestions(
            "\\textbf{Bold}, \\[ E = mc^2 \\] and \\frac{b}{c}, a \\$5 fee, "
            "\\verb|$$ \\bf| and % \\it\n\\begin{align}a &= b\\end{align}",
            "STY102",
        )
        self.assertEqual(clean, [])


class FloatAdviceTests(StyleTestCase):
    """``STY103`` and ``STY104``: placement with ``[h]`` alone and center inside floats."""

    def test_here_only_placement(self) -> None:
        body = (
            "\\begin{figure}[h]\\centering Figure.\\end{figure}\n"
            "\\begin{table*}[h]Table.\\end{table*}"
        )
        self.assert_suggested(self.suggestions(body, "STY103"), [3, 4])
        clean = (
            "\\begin{figure}[htbp]Figure.\\end{figure}\\begin{table}Table.\\end{table}"
            "\\begin{figure}[!h]Figure.\\end{figure}"
        )
        self.assertEqual(self.suggestions(clean, "STY103"), [])

    def test_center_environment_in_floats(self) -> None:
        body = "\\begin{figure}\n\\begin{center}\nFigure.\n\\end{center}\n\\end{figure}"
        self.assert_suggested(self.suggestions(body, "STY104"), [4])
        clean = (
            "\\begin{figure}\\centering Figure.\\end{figure}\n"
            "\\begin{center}A centred line in the text.\\end{center}"
        )
        self.assertEqual(self.suggestions(clean, "STY104"), [])


class ReferenceTests(StyleTestCase):
    r"""``STY201`` to ``STY203``: unused labels, doubled names, ``\ref`` to equations."""

    def test_unused_labels(self) -> None:
        body = (
            "\\section{Intro}\\label{sec:intro}\n"
            "\\section{Method}\\label{sec:method}\n"
            "See \\cref{sec:method,sec:other} and \\crefrange{eq:a}{eq:b}.\n"
            "\\label{sec:other}\\label{eq:a}\\label{eq:b}\\label{custom}\\mything{custom}\n"
            "\\iffalse\\label{hidden}\\fi"
        )
        findings = self.suggestions(body, "STY201")
        self.assert_suggested(findings, [3])
        self.assertEqual(findings[0].details["label"], "sec:intro")
        # A reference built from a parameter covers its literal prefix, or every label.
        prefixed = self.suggestions(
            "\\newcommand{\\figref}[1]{Figure~\\ref{fig:#1}}\\label{fig:a}\\label{tab:b}",
            "STY201",
        )
        self.assertEqual([finding.details["label"] for finding in prefixed], ["tab:b"])
        anything = self.suggestions(
            "\\newcommand{\\myref}[1]{\\cref{#1}}\\label{fig:a}\\label{tab:b}", "STY201"
        )
        self.assertEqual(anything, [])
        clean = self.suggestions("\\label{a}\\ref{a}\\label{b}\\pageref{b}", "STY201")
        self.assertEqual(clean, [])

    def test_doubled_reference_names(self) -> None:
        body = (
            "As Figure~\\cref{fig:a} and table \\autoref{tab:b} show,\n"
            "Sec.\n\\Cref{sec:c} and Equations~\\zcref{eq:d} agree."
        )
        findings = self.suggestions(body, "STY202")
        self.assert_suggested(findings, [3, 3, 4, 5])
        self.assertEqual(findings[0].details, {"name": "Figure", "command": "cref"})
        clean = self.suggestions(
            "As Figure~\\ref{fig:a}, \\cref{tab:b}, Figure~\\labelcref{fig:c} and "
            "\\Cref{sec:c} show; \\label{figure}.",
            "STY202",
        )
        self.assertEqual(clean, [])

    def test_ref_to_equation_labels(self) -> None:
        body = (
            "\\begin{equation}x = y\\label{eq:one}\\end{equation}\n"
            "\\begin{eqnarray}a &=& b \\label{eq:two}\\end{eqnarray}\n"
            "See \\ref{eq:one}, \\ref{eq:two} and (\\ref{eq:one}).\n"
            "\\section{S}\\label{sec:s} See \\ref{sec:s} and \\eqref{eq:one}."
        )
        findings = self.suggestions(body, "STY203")
        self.assert_suggested(findings, [5, 5])
        self.assertEqual([finding.details["label"] for finding in findings], ["eq:one", "eq:two"])
        self.assertEqual(self.suggestions("\\section{A}\\label{a} See \\ref{a}.", "STY203"), [])


class TypographyTests(StyleTestCase):
    """``STY301`` to ``STY307`` on prose; math, arguments and comments are left out."""

    def test_untied_references(self) -> None:
        body = "As Figure \\ref{f} and see\n\\cite{k} show, in \\cref{t} and Eq. \\eqref{e}."
        findings = self.suggestions(body, "STY301")
        self.assert_suggested(findings, [3, 4, 4])
        self.assertEqual([finding.details["word"] for finding in findings], ["Figure", "see", "in"])
        clean = (
            "As Figure~\\ref{f}, see~\\cite{k}, Figure%\n\\ref{g}, \\item \\cite{x}, "
            "(\\ref{a}), $x \\ref{b}$ and 2020 \\cite{y}."
        )
        self.assertEqual(self.suggestions(clean, "STY301"), [])

    def test_et_al_spacing(self) -> None:
        body = "Smith et al. showed it, and Lee et~al. \\cite{k} too."
        self.assert_suggested(self.suggestions(body, "STY302"), [3, 3])
        clean = "Smith et al.\\ showed, et al.~\\cite{k}, and so did Lee et al. The end."
        self.assertEqual(self.suggestions(clean, "STY302"), [])
        self.assertEqual(
            self.suggestions("Smith et al. showed it.", "STY302", preamble="\\frenchspacing\n"),
            [],
        )

    def test_hyphen_ranges(self) -> None:
        findings = self.suggestions("See pages 10-20 and 1-5.", "STY303")
        self.assert_suggested(findings, [3, 3])
        self.assertEqual(findings[0].suggestion, "Write 10--20.")
        clean = (
            "Pages 10--20, on 2020-01-01, version 1.2-3, GPT-4, \\label{s:1-2}, "
            "$x = 3-2$, \\cite{a1-2} and \\url{http://x/1-2}."
        )
        self.assertEqual(self.suggestions(clean, "STY303"), [])

    def test_straight_quotes(self) -> None:
        self.assert_suggested(self.suggestions('We call this "robust" here.', "STY304"), [3])
        clean = 'We call this ``robust\'\' here, write \\"o and 5" of rain, and \\verb|"x"|.'
        self.assertEqual(self.suggestions(clean, "STY304"), [])
        csquotes = self.suggestions(
            'We call this "robust".', "STY304", preamble='\\MakeOuterQuote{"}\n'
        )
        self.assertEqual(csquotes, [])

    def test_ellipses(self) -> None:
        self.assert_suggested(self.suggestions("It is fast...\nand", "STY305"), [3])
        clean = "It is fast\\dots, see $1, \\ldots, n$ and \\verb|...| or a.b.c. and ...."
        self.assertEqual(self.suggestions(clean, "STY305"), [])

    def test_spaced_footnotes(self) -> None:
        body = "A note \\footnote{Like this.} and\n\\footnote{Here.}"
        self.assert_suggested(self.suggestions(body, "STY306"), [3, 4])
        clean = "A note\\footnote{Like this.} and a note%\n\\footnote{Here.}\\footnotemark"
        self.assertEqual(self.suggestions(clean, "STY306"), [])

    def test_consecutive_citations(self) -> None:
        findings = self.suggestions("See \\cite{a}\\cite{b} and \\citep{c} \\citep{d}.", "STY307")
        self.assert_suggested(findings, [3, 3])
        self.assertEqual(findings[0].suggestion, "Write \\cite{a,b}.")
        clean = (
            "See \\cite{a,b}, \\cite[p.~2]{c}\\cite{d}, \\cite{e} and \\cite{f}, "
            "\\citet{g}\\cite{h}."
        )
        self.assertEqual(self.suggestions(clean, "STY307"), [])

    def test_hostile_prose_is_scanned_in_linear_time(self) -> None:
        size = HOSTILE_CHARACTERS
        bodies = {
            "long word before a reference": "a" * size + " x",
            "digit runs": "1" * size + "-" + "2" * size + "x",
            "unclosed quotes": '"a ' * (size // 3),
            "periods": "." * size,
            "spaces before footnote": "a" + " " * size + "\\footnotemark",
            "et al runs": "et al. " * (size // 7),
            "reference names": "Figure " * (size // 7),
            "unclosed math": "$a " + "\\( b " * (size // 5),
            "spelling fragments": "data " * (size // 5) + "modell " * (size // 6),
            "unclosed environments": "\\begin{equation} " * (size // 17),
        }
        for label, body in bodies.items():
            with self.subTest(body=label):
                started = time.perf_counter()
                self.suggestions(body, "STY")
                self.assertLess(time.perf_counter() - started, LINEAR_TIME_LIMIT_SECONDS)


class WordingTests(StyleTestCase):
    """``STY401``: one term spelled in two ways in one document."""

    def test_spelling_variants(self) -> None:
        body = (
            "Our modelling choices, modelling again and later modeling.\n"
            "The data set and the dataset; the behaviour is fine."
        )
        findings = self.suggestions(body, "STY401")
        self.assert_suggested(findings, [3, 4])
        self.assertEqual(findings[0].details, {"modelling": 2, "modeling": 1})
        clean = (
            "Our modeling, the dataset, \\begin{center}text\\end{center} the color center, "
            "\\centering and \\label{modelling}; the analyses are analyzed."
        )
        self.assertEqual(self.suggestions(clean, "STY401"), [])


class AccessibilityTests(StyleTestCase):
    r"""``STY501``: a ``\DocumentMetadata`` declaration with a language and tagging."""

    def test_accessibility_metadata_suggestion(self) -> None:
        missing = self.suggestions("Text.", "STY501")
        self.assert_suggested(missing, [1])
        self.assertEqual(missing[0].details["missing"], ["\\DocumentMetadata declaration"])
        self.write(
            "main.tex",
            "\\DocumentMetadata{pdfversion=2.0}\n" + DOCUMENT_START + "Text.\n\\end{document}\n",
        )
        partial = check_manuscript_policies(
            self.root, "main.tex", selection=CheckSelection(select=("ALL", "STY501"))
        )
        self.assertEqual(
            partial[0].details["missing"], ["lang key", "tagging=on or a PDF/UA pdfstandard"]
        )
        for declaration in (
            "\\DocumentMetadata{lang=en, tagging=on}",
            "\\DocumentMetadata{lang=de,\n pdfstandard=ua-2, pdfstandard=a-4f}",
        ):
            with self.subTest(declaration=declaration):
                self.write(
                    "main.tex", declaration + "\n" + DOCUMENT_START + "Text.\n\\end{document}\n"
                )
                complete = check_manuscript_policies(
                    self.root, "main.tex", selection=CheckSelection(select=("ALL", "STY501"))
                )
                self.assertEqual(complete, [])
        # When the accessibility policy runs, it reports the gap instead.
        policy = self.suggestions(
            "Text.", "STY501", options=ManuscriptCheckOptions(require_document_metadata=True)
        )
        self.assertEqual(policy, [])


class StyleCleanCaseTests(StyleTestCase):
    """Allowed input for each style suggestion: the clean twins, as the catalogue check reads them.

    Each test runs one suggestion on text that follows the advice and asserts that it reports
    nothing; ``tests/test_rules.py`` finds these tests through ``@clean_case``.
    """

    def assert_clean(self, code: str, body: str, *, preamble: str = "") -> None:
        """Assert that the suggestion ``code`` reports nothing for a document with ``body``."""
        self.assertEqual(self.suggestions(body, code, preamble=preamble), [])

    @clean_case("STY101")
    def test_current_packages_and_driver_free_options(self) -> None:
        self.assert_clean(
            "STY101",
            "Text.",
            preamble="\\usepackage{newtxtext,newtxmath}\n\\usepackage[final]{graphicx}\n"
            "% \\usepackage{times}\n",
        )

    @clean_case("STY102")
    def test_current_commands_and_display_math(self) -> None:
        self.assert_clean(
            "STY102",
            "\\textbf{Bold}, \\[ E = mc^2 \\] and \\frac{b}{c}, a \\$5 fee, "
            "\\verb|$$ \\bf| and % \\it\n\\begin{align}a &= b\\end{align}",
        )

    @clean_case("STY103")
    def test_placements_with_alternatives(self) -> None:
        self.assert_clean(
            "STY103",
            "\\begin{figure}[htbp]Figure.\\end{figure}\\begin{table}Table.\\end{table}"
            "\\begin{figure}[!h]Figure.\\end{figure}",
        )

    @clean_case("STY104")
    def test_centering_inside_floats(self) -> None:
        self.assert_clean(
            "STY104",
            "\\begin{figure}\\centering Figure.\\end{figure}\n"
            "\\begin{center}A centred line in the text.\\end{center}",
        )

    @clean_case("STY201")
    def test_every_label_referenced(self) -> None:
        self.assert_clean("STY201", "\\label{a}\\ref{a}\\label{b}\\pageref{b}")

    @clean_case("STY202")
    def test_reference_names_printed_once(self) -> None:
        self.assert_clean(
            "STY202",
            "As Figure~\\ref{fig:a}, \\cref{tab:b}, Figure~\\labelcref{fig:c} and "
            "\\Cref{sec:c} show; \\label{figure}.",
        )

    @clean_case("STY203")
    def test_ref_to_a_section(self) -> None:
        self.assert_clean("STY203", "\\section{A}\\label{a} See \\ref{a}.")

    @clean_case("STY301")
    def test_tied_references(self) -> None:
        self.assert_clean(
            "STY301",
            "As Figure~\\ref{f}, see~\\cite{k}, Figure%\n\\ref{g}, \\item \\cite{x}, "
            "(\\ref{a}), $x \\ref{b}$ and 2020 \\cite{y}.",
        )

    @clean_case("STY302")
    def test_et_al_spaced_correctly(self) -> None:
        self.assert_clean(
            "STY302", "Smith et al.\\ showed, et al.~\\cite{k}, and so did Lee et al. The end."
        )

    @clean_case("STY303")
    def test_ranges_with_en_dashes(self) -> None:
        self.assert_clean(
            "STY303",
            "Pages 10--20, on 2020-01-01, version 1.2-3, GPT-4, \\label{s:1-2}, "
            "$x = 3-2$, \\cite{a1-2} and \\url{http://x/1-2}.",
        )

    @clean_case("STY304")
    def test_typographic_quotes(self) -> None:
        self.assert_clean(
            "STY304", 'We call this ``robust\'\' here, write \\"o and 5" of rain, and \\verb|"x"|.'
        )

    @clean_case("STY305")
    def test_dots_commands(self) -> None:
        self.assert_clean(
            "STY305", "It is fast\\dots, see $1, \\ldots, n$ and \\verb|...| or a.b.c. and ...."
        )

    @clean_case("STY306")
    def test_footnotes_without_a_space(self) -> None:
        self.assert_clean(
            "STY306", "A note\\footnote{Like this.} and a note%\n\\footnote{Here.}\\footnotemark"
        )

    @clean_case("STY307")
    def test_combined_citations(self) -> None:
        self.assert_clean(
            "STY307",
            "See \\cite{a,b}, \\cite[p.~2]{c}\\cite{d}, \\cite{e} and \\cite{f}, "
            "\\citet{g}\\cite{h}.",
        )

    @clean_case("STY401")
    def test_one_spelling_per_term(self) -> None:
        self.assert_clean(
            "STY401",
            "Our modeling, the dataset, \\begin{center}text\\end{center} the color center, "
            "\\centering and \\label{modelling}; the analyses are analyzed.",
        )

    @clean_case("STY501")
    def test_metadata_with_language_and_tagging(self) -> None:
        self.write(
            "main.tex",
            "\\DocumentMetadata{lang=en, tagging=on}\n"
            + DOCUMENT_START
            + "Text.\n\\end{document}\n",
        )
        findings = check_manuscript_policies(
            self.root, "main.tex", selection=CheckSelection(select=("ALL", "STY501"))
        )
        self.assertEqual(findings, [])
