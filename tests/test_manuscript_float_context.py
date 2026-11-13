r"""The float policies (MAN011 to MAN015) on the constructs real papers put inside floats.

Mathematics, table formatting, acronyms, TikZ drawings, the project's own text macros
and subfigures cannot hide a caption, label or reference, so a float that uses them is
checked instead of being inconclusive. A macro that may hide one still makes the float
inconclusive, and a float that nobody references still fails.
"""

from __future__ import annotations

import time

from latexprep.findings import Finding
from latexprep.manuscript_check_options import ManuscriptCheckOptions
from latexprep.manuscript_checks import check_manuscript_policies
from latexprep.tex_lexer import scan_source_commands
from latexprep.tex_spans import macro_definitions
from tests.support import ProjectTestCase

FLOATS = ManuscriptCheckOptions(
    require_float_captions=True,
    require_float_labels=True,
    check_float_label_order=True,
    require_float_references=True,
    check_float_reference_order=True,
)
FLOAT_RULES = (
    "float_captions",
    "float_labels",
    "float_label_order",
    "float_references",
    "float_reference_order",
)


class FloatContextTests(ProjectTestCase):
    """Float policies over neutral constructs, project macros, drawings and subfigures."""

    def statuses(
        self, body: str, preamble: str = "", options: ManuscriptCheckOptions = FLOATS
    ) -> dict[str, Finding]:
        """Check an ``article`` with ``body`` and return each float policy's finding by name."""
        self.write(
            "main.tex",
            "\\documentclass{article}\n"
            + preamble
            + "\\begin{document}\n"
            + body
            + "\n\\end{document}\n",
        )
        findings = check_manuscript_policies(self.root, "main.tex", options)
        return {item.rule.removeprefix("manuscript."): item for item in findings}

    def assert_passed(self, body: str, preamble: str = "") -> None:
        """Assert that every float policy passes for ``body``."""
        results = self.statuses(body, preamble)
        for rule in FLOAT_RULES:
            with self.subTest(rule=rule):
                self.assertEqual(results[rule].status, "passed", results[rule].details)

    def test_mathematics_tables_and_acronyms_are_read(self) -> None:
        self.assert_passed(
            "See Figure~\\ref{fig:a} and \\textbf{Table~\\ref{tab:a}}.\n"
            "\\begin{figure}\\centering $\\Delta \\langle x \\rangle \\triangleq \\big(y\\big)$"
            "\\caption{Gains of \\ac{LLM} repair \\checkmark}\\label{fig:a}\\end{figure}\n"
            "\\begin{table}\\caption{Results}\\label{tab:a}\\setlength{\\tabcolsep}{2pt}"
            "\\begin{tabular}{l>{\\raggedright\\arraybackslash}p{2cm}}\\toprule"
            "\\rowcolor{gray}\\makecell{A\\\\B} & \\cellcolor{red}\\num{3.5}\\\\ \\addlinespace"
            "\\bottomrule\\end{tabular}\\end{table}"
        )

    def test_text_macros_of_the_project_are_read(self) -> None:
        preamble = (
            "\\newcommand{\\capslot}{$\\langle$capability$\\rangle$}\n"
            "\\newcommand{\\tabgroup}[1]{\\textbf{#1}}\n\\newlength{\\colw}\n"
            "\\newcommand{\\both}{\\capslot{} and \\tabgroup{x}}\n"
        )
        self.assert_passed(
            "See \\ref{tab:a}.\\begin{table}\\caption{A \\capslot{} slot}\\label{tab:a}"
            "\\begin{tabular}{p{\\colw}}\\tabgroup{G} \\both\\\\\\end{tabular}\\end{table}",
            preamble,
        )

    def test_a_macro_that_may_hide_a_label_keeps_the_float_inconclusive(self) -> None:
        for definition in (
            "\\newcommand{\\mklabel}{\\label{tab:a}}",
            "\\newcommand{\\mklabel}{\\csname x\\endcsname}",
            "\\newcommand{\\mklabel}{\\mklabel}",
        ):
            with self.subTest(definition=definition):
                results = self.statuses(
                    "See \\ref{tab:a}.\\begin{table}\\caption{A}\\mklabel\\end{table}",
                    definition + "\n",
                )
                self.assertEqual(results["float_labels"].status, "inconclusive")

    def test_a_layout_macro_hides_no_label_but_stays_visible_to_the_layout_policy(self) -> None:
        preamble = "\\newcommand{\\cell}[1]{{\\footnotesize\\bfseries #1}}\n"
        body = "See \\ref{tab:a}.\\begin{table}\\caption{A}\\label{tab:a}\\cell{1}\\end{table}"
        self.assert_passed(body, preamble)
        layout = self.statuses(
            body, preamble, ManuscriptCheckOptions(check_layout_manipulation=True)
        )
        self.assertNotEqual(layout["layout_manipulation"].status, "passed")

    def test_drawing_commands_are_read_as_a_drawing(self) -> None:
        self.assert_passed(
            "See \\ref{fig:a}.\\begin{figure}\\begin{tikzpicture}\\draw (0,0) -- (1,1);"
            "\\node[draw] at (0,0) {\\foo};\\foreach \\x in {1,2} {\\fill (\\x,0) circle (1pt);}"
            "\\end{tikzpicture}\\caption{A}\\label{fig:a}\\end{figure}"
        )

    def test_subfigures_have_their_own_captions_and_labels(self) -> None:
        self.assert_passed(
            "See \\ref{fig:a-left}, \\subref{fig:b-right} and Figure~\\ref{fig:b}.\n"
            "\\begin{figure}\\begin{subfigure}{0.4\\linewidth}\\includegraphics{x}"
            "\\caption{Left}\\label{fig:a-left}\\end{subfigure}"
            "\\begin{subfigure}{0.4\\linewidth}\\caption{Right}\\label{fig:a-right}"
            "\\end{subfigure}\\caption{Both}\\label{fig:a}\\end{figure}\n"
            "\\begin{figure}\\subfloat[Left\\label{fig:b-left}]{\\includegraphics{x}}"
            "\\subfloat[Right]{\\includegraphics{y}\\label{fig:b-right}}"
            "\\caption{Both}\\label{fig:b}\\end{figure}"
        )

    def test_an_equation_label_inside_a_figure_is_not_the_figure_label(self) -> None:
        self.assert_passed(
            "See \\ref{fig:a} and \\eqref{eq:a}.\\begin{figure}"
            "\\begin{equation*}\\label{eq:a} x = y\\end{equation*}"
            "\\caption{A}\\label{fig:a}\\end{figure}"
        )

    def test_a_neutral_macro_in_a_caption_with_text_leaves_it_checked(self) -> None:
        preamble = "\\newcommand{\\capslot}{$\\langle$capability$\\rangle$}\n"
        body = "See \\ref{fig:a}.\\begin{figure}\\caption{%s}\\label{fig:a}\\end{figure}"
        self.assertEqual(
            self.statuses(body % "Gains \\capslot", preamble)["float_captions"].status, "passed"
        )
        # A caption of only a macro may be empty, and an unknown macro may hide anything.
        self.assertEqual(
            self.statuses(body % "\\capslot", preamble)["float_captions"].status, "inconclusive"
        )
        self.assertEqual(
            self.statuses(body % "Gains \\foo")["float_captions"].status, "inconclusive"
        )

    def test_a_figure_whose_subfigures_nobody_references_fails(self) -> None:
        results = self.statuses(
            "Text.\\begin{figure}\\subfloat[A\\label{fig:a-left}]{\\includegraphics{x}}"
            "\\caption{A}\\label{fig:a}\\end{figure}"
        )
        self.assertEqual(results["float_references"].status, "failed")

    def test_reading_definitions_stays_linear(self) -> None:
        text = "\\def\\x " * 50_000
        started = time.monotonic()
        definitions = macro_definitions(text, scan_source_commands(text))
        self.assertEqual(definitions, [])
        self.assertLess(time.monotonic() - started, 5)

    def test_a_float_inside_a_list_keeps_its_caption_and_label(self) -> None:
        for environment in ("enumerate", "itemize"):
            with self.subTest(environment=environment):
                self.assert_passed(
                    f"See Figure~\\ref{{fig:a}}.\\begin{{{environment}}}\\item x "
                    "\\begin{figure}\\includegraphics{x}\\caption{Main}\\label{fig:a}\\end{figure}"
                    f"\\end{{{environment}}}"
                )

    def test_a_subcaption_in_a_minipage_labels_the_subfigure(self) -> None:
        self.assert_passed(
            "See Figure~\\ref{fig:a}.\\begin{figure}\\begin{minipage}{.4\\linewidth}"
            "\\includegraphics{x}\\subcaption{Left}\\label{fig:left}\\end{minipage}"
            "\\caption{Main}\\label{fig:a}\\end{figure}"
        )

    def test_a_table_note_label_is_not_the_table_label(self) -> None:
        results = self.statuses(
            "See note \\ref{tn:1}.\\begin{table}\\begin{threeparttable}\\begin{tabular}{l}x"
            "\\end{tabular}\\begin{tablenotes}\\item\\label{tn:1} n\\end{tablenotes}"
            "\\caption{A}\\end{threeparttable}\\end{table}"
        )
        self.assertEqual(results["float_labels"].status, "failed")
        self.assertEqual(results["float_label_order"].status, "passed")

    def test_constructs_that_may_hide_a_caption_or_label_stay_inconclusive(self) -> None:
        cases = (
            # xparse: the second group is an argument specification, not the body.
            (
                "\\NewDocumentCommand{\\mk}{}{\\label{tab:a}}\n",
                "\\begin{table}\\caption{A}\\mk\\end{table}",
            ),
            # A macro with a parameter swallows the unbraced token after it.
            (
                "\\newcommand{\\gobble}[1]{}\n",
                "\\begin{table}\\gobble\\caption{A}\\label{tab:a}\\end{table}",
            ),
            # A project macro inside a drawing is still read as a project macro.
            (
                "\\newcommand{\\figcap}{\\caption{A}\\label{tab:a}}\n",
                "\\begin{table}\\begin{tikzpicture}\\node{\\figcap};\\end{tikzpicture}\\end{table}",
            ),
            # A definition name built with \csname may redefine any macro.
            (
                "\\newcommand{\\cell}{\\small}"
                "\\expandafter\\def\\csname cell\\endcsname{\\label{tab:a}}\n",
                "\\begin{table}\\caption{A}\\cell\\end{table}",
            ),
        )
        for preamble, body in cases:
            with self.subTest(preamble=preamble):
                results = self.statuses("See \\ref{tab:a}." + body, preamble)
                self.assertEqual(
                    results["float_labels"].status, "inconclusive", results["float_labels"].details
                )

    def test_an_unclosed_drawing_hides_nothing_after_it(self) -> None:
        results = self.statuses(
            "\\begin{tikzpicture}\\draw (0,0);\n"
            "See \\ref{fig:a}.\\begin{figure}\\figcap\\end{figure}",
            "\\newcommand{\\figcap}{\\caption{A}\\label{fig:a}}\n",
        )
        self.assertEqual(results["float_labels"].status, "inconclusive")

    def test_a_brace_group_after_a_subfloat_belongs_to_the_float(self) -> None:
        results = self.statuses(
            "See \\ref{fig:a}.\\begin{figure}\\subfloat[A]{\\includegraphics{x}}\n"
            "{\\centering\\caption{Main}\\label{fig:a}}\\end{figure}"
        )
        self.assertNotEqual(results["float_captions"].status, "failed")
        self.assertNotEqual(results["float_labels"].status, "failed")

    def test_reading_nested_definitions_stays_bounded(self) -> None:
        text = "".join(f"\\def\\m{index}{{x " for index in range(1500)) + "}" * 1500
        self.write(
            "main.tex", "\\documentclass{article}\n" + text + "\n\\begin{document}\\end{document}\n"
        )
        started = time.monotonic()
        check_manuscript_policies(self.root, "main.tex", FLOATS)
        self.assertLess(time.monotonic() - started, 20)

    def test_a_drawing_in_a_box_with_tikz_value_macros_is_read(self) -> None:
        self.assert_passed(
            "See \\ref{fig:a}.\\begin{figure}\\resizebox{\\linewidth}{!}{\\begin{tikzpicture}"
            "\\pgfmathsetlengthmacro{\\colw}{0.3\\linewidth}\\node at (\\colw,0) {A};"
            "\\path (0,0) coordinate (y1);\\pgfextra{\\pgfmathsetlengthmacro{\\rowthree}{2pt}"
            "\\global\\let\\rowthree\\rowthree};\\draw (0,\\rowthree) -- (1,0);"
            "\\end{tikzpicture}}\\caption{A}\\label{fig:a}\\end{figure}"
        )
