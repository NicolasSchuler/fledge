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
from latexprep.tex_spans import definition_spans, macro_definitions
from tests.support import ProjectTestCase, clean_case

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
# The policies take about 3.5 times as long as one reading of the definition spans of this
# many nested definitions, with and without coverage tracing; reading every body again
# for each definition takes several hundred times as long.
NESTED_DEFINITIONS = 800
MAX_READINGS_OF_NESTED_DEFINITIONS = 12


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
        # A caption of only a macro may be empty; literal text beside an unknown macro is
        # a caption whatever the macro expands to.
        self.assertEqual(
            self.statuses(body % "\\capslot", preamble)["float_captions"].status, "inconclusive"
        )
        self.assertEqual(self.statuses(body % "Gains \\foo")["float_captions"].status, "passed")

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

    def test_many_floats_and_many_unread_references_stay_fast(self) -> None:
        # Review: every float was matched against every reference that was not read, which
        # took over a minute for a 1 MB file. The references are looked up by label.
        floats, references = 2_000, 6_000
        body = (
            "\\iffalse\n"
            + "".join(f"\\ref{{x:{index}}}\n" for index in range(references))
            + "\\fi\n"
            + "".join(
                f"\\begin{{figure}}\\caption{{C}}\\label{{f:{index}}}\\end{{figure}}\n"
                for index in range(floats)
            )
        )
        started = time.monotonic()
        results = self.statuses(body)
        self.assertLess(time.monotonic() - started, 10)
        # No reference that was read or may be one names a float: all are unreferenced.
        self.assertEqual(results["float_references"].status, "failed")
        # A reference that was not read still keeps the float it may name undecided.
        results = self.statuses(body.replace("\\ref{x:0}", "\\ref{f:0}", 1))
        self.assertIn("main.tex:", "".join(results["float_references"].details["uncertainty"]))

    def test_a_float_inside_a_list_keeps_its_caption_and_label(self) -> None:
        for environment in ("enumerate", "itemize"):
            with self.subTest(environment=environment):
                self.assert_passed(
                    f"See Figure~\\ref{{fig:a}}.\\begin{{{environment}}}\\item x "
                    "\\begin{figure}\\includegraphics{x}\\caption{Main}\\label{fig:a}\\end{figure}"
                    f"\\end{{{environment}}}"
                )

    def test_a_label_in_a_subfigure_command_names_the_part_not_the_float(self) -> None:
        # Held-out paper (subfigure package): \subfigure[caption]{\label{…}…} in front of
        # the float's caption was reported as a label placed before the caption.
        body = (
            "See Figure~\\ref{fig:both}%s.\n\\begin{figure}\\centering\n"
            "\\subfigure[First]{\\label{fig:both:a}\\includegraphics{a}}\n"
            "\\subfigure[Second]{\\label{fig:both:b}\\includegraphics{b}}\n"
            "%s\\caption{Both.}\\label{fig:both}\\end{figure}"
        )
        self.assert_passed(body % ("", ""))
        # A reference to a part refers to the figure.
        self.assert_passed((body % ("", "")).replace("\\ref{fig:both}", "\\ref{fig:both:a}"))
        # Counter-test: a label of the float itself in front of its caption is still misplaced.
        results = self.statuses(body % (" and \\ref{fig:early}", "\\label{fig:early}"))
        self.assertEqual(results["float_label_order"].status, "failed")

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
                "\\begin{table}\\caption{A}\\gobble\\label{tab:a}\\end{table}",
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
        # Each nested definition spans the rest of the text, so finding where all of them
        # end takes time quadratic in their number, however the policies read them. The
        # policies may cost a small multiple of one such reading; reading every body
        # again for each definition would cost hundreds of them. Both are measured here,
        # so the limit holds on a slow computer and under coverage tracing.
        text = (
            "".join(f"\\def\\m{index}{{x " for index in range(NESTED_DEFINITIONS))
            + "}" * NESTED_DEFINITIONS
        )
        source = "\\documentclass{article}\n" + text + "\n\\begin{document}\\end{document}\n"
        self.write("main.tex", source)
        started = time.process_time()
        definition_spans(source, scan_source_commands(source))
        one_reading = time.process_time() - started
        started = time.process_time()
        check_manuscript_policies(self.root, "main.tex", FLOATS)
        whole = time.process_time() - started
        self.assertLess(whole, MAX_READINGS_OF_NESTED_DEFINITIONS * one_reading)

    def test_a_drawing_in_a_box_with_tikz_value_macros_is_read(self) -> None:
        self.assert_passed(
            "See \\ref{fig:a}.\\begin{figure}\\resizebox{\\linewidth}{!}{\\begin{tikzpicture}"
            "\\pgfmathsetlengthmacro{\\colw}{0.3\\linewidth}\\node at (\\colw,0) {A};"
            "\\path (0,0) coordinate (y1);\\pgfextra{\\pgfmathsetlengthmacro{\\rowthree}{2pt}"
            "\\global\\let\\rowthree\\rowthree};\\draw (0,\\rowthree) -- (1,0);"
            "\\end{tikzpicture}}\\caption{A}\\label{fig:a}\\end{figure}"
        )


class FloatEvidenceTests(ProjectTestCase):
    r"""A gap decides a float policy only where the float lacks the evidence it requires.

    Each relaxation is paired with a counter-test in which the same kind of construct
    really hides or carries a violation, which must still fail or stay inconclusive.
    """

    def statuses(self, body: str, preamble: str = "") -> dict[str, Finding]:
        """Check an ``article`` with ``body`` and return each float policy's finding by name."""
        self.write(
            "main.tex",
            "\\documentclass{article}\n" + preamble + "\\begin{document}\n" + body + "\n"
            "\\end{document}\n",
        )
        findings = check_manuscript_policies(self.root, "main.tex", FLOATS)
        return {item.rule.removeprefix("manuscript."): item for item in findings}

    def assert_statuses(self, body: str, preamble: str = "", **expected: str) -> None:
        """Assert the status of each float policy: ``passed`` unless ``expected`` names another.

        Args:
            body: The document body.
            preamble: The preamble after the class line.
            **expected: The status of a policy, by rule name without ``manuscript.``.
        """
        results = self.statuses(body, preamble)
        for rule in FLOAT_RULES:
            with self.subTest(rule=rule):
                self.assertEqual(
                    results[rule].status, expected.get(rule, "passed"), results[rule].message
                )

    @clean_case("MAN011", "MAN012", "MAN013", "MAN014", "MAN015")
    def test_an_unknown_command_beside_the_evidence_leaves_every_float_check_decided(
        self,
    ) -> None:
        # Defect: \rho, \fbox or a project macro inside a float made all five inconclusive.
        body = (
            "See Figure~\\ref{fig:a} and Table~\\ref{tab:a}.\n"
            "\\begin{figure}\\centering\\fbox{\\rule{1cm}{1cm}}%s"
            "\\caption{Spearman $\\rho=0.8$.}\\label{fig:a}\\end{figure}\n"
            "\\begin{table}\\caption{Data \\gdiff}\\label{tab:a}\\colrule\\end{table}"
        )
        self.assert_statuses(body % "")
        for preamble in (
            "\\newcommand{\\mymark}{\\ding{51}}\n",
            # A switch made by \newif, and names built with \csname under another prefix.
            "\\newif\\ifshowrev\\newcommand{\\mymark}{\\ifshowrev\\color{blue}\\fi}\n",
            "\\newcommand{\\mymark}{\\ding{51}}"
            "\\newcommand{\\mk}[1]{\\expandafter\\xdef\\csname tabfn@#1\\endcsname{x}}\n",
        ):
            with self.subTest(preamble=preamble):
                self.assert_statuses(body % "\\mymark ", preamble)
        # Counter-tests: a macro of the project that holds a label, or one that a name built
        # with \csname may redefine, may put a label in front of the caption.
        for preamble in (
            "\\newcommand{\\mymark}{\\ding{51}\\label{fig:b}}\n",
            "\\newcommand{\\mymark}{\\ding{51}}"
            "\\expandafter\\def\\csname my#1\\endcsname{\\label{fig:b}}\n",
        ):
            with self.subTest(preamble=preamble):
                self.assert_statuses(body % "\\mymark ", preamble, float_label_order="inconclusive")

    def test_a_label_macro_defined_in_any_way_keeps_the_label_placement_open(self) -> None:
        # Review: a macro of the paper that holds a label passed as a class command when
        # its definition used a command the scan did not know, or a name built at run time.
        body = (
            "See Figure~\\ref{fig:a}.\n"
            "\\begin{figure}\\centering\\figlabel{fig:a}\\caption{A caption}\\end{figure}"
        )
        for preamble in (
            "\\newrobustcmd\\figlabel[1]{\\label{#1}}",
            "\\NewCommandCopy\\figlabel\\label",
            "\\NewCommandCopy{\\figlabel}{\\label}",
            "\\NewExpandableDocumentCommand\\figlabel{m}{\\label{#1}}",
            "\\makeatletter\\@namedef{figlabel}#1{\\label{#1}}\\makeatother",
            "\\expandafter\\def\\csname figlabel\\endcsname#1{\\label{#1}}",
            "\\csdef{figlabel}#1{\\label{#1}}",
            "\\cslet{figlabel}{\\label}",
            "\\newcommand\\mk[1]{\\expandafter\\def\\csname #1\\endcsname##1{\\label{##1}}}"
            "\\mk{figlabel}",
        ):
            with self.subTest(preamble=preamble):
                result = self.statuses(body, preamble + "\n")["float_label_order"]
                self.assertEqual(result.status, "inconclusive", result.message)
                self.assertIn("\\figlabel", result.message)
        # The same in a package file of the project.
        self.write(
            "mylabels.sty", "\\ProvidesPackage{mylabels}\n\\csdef{figlabel}#1{\\label{#1}}\n"
        )
        result = self.statuses(body, "\\usepackage{mylabels}\n")["float_label_order"]
        self.assertEqual(result.status, "inconclusive", result.message)
        # A definition by a built name whose text holds no label leaves the commands of
        # the class and of packages as they are.
        self.assert_statuses(
            "See Figure~\\ref{fig:a}.\n"
            "\\begin{figure}\\fbox{x}\\caption{A caption}\\label{fig:a}\\end{figure}",
            "\\newcommand\\mk[1]{\\expandafter\\def\\csname #1\\endcsname{\\textsuperscript{a}}}"
            "\\mk{x}\n",
        )

    def test_an_unknown_macro_still_counts_where_the_evidence_is_missing(self) -> None:
        # Counter-tests: the only content is a project macro, or the caption exists only in
        # the definition of the macro the float uses.
        self.assert_statuses(
            "See \\ref{fig:a}.\\begin{figure}\\mymark\\label{fig:a}\\end{figure}",
            "\\newcommand{\\mymark}{\\foo}\n",
            float_captions="inconclusive",
            float_label_order="inconclusive",
        )
        self.assert_statuses(
            "See \\ref{fig:a}.\\begin{figure}\\mycap\\label{fig:a}\\end{figure}",
            "\\newcommand{\\mycap}{\\caption{Hidden}}\n",
            float_captions="inconclusive",
            float_label_order="inconclusive",
        )
        # A float with neither a caption nor an unknown construct still fails, and so does
        # one whose only other command typesets its argument.
        self.assert_statuses(
            "See \\ref{fig:a}.\\begin{figure}\\rule{1cm}{1cm}\\label{fig:a}\\end{figure}",
            float_captions="failed",
            float_label_order="failed",
        )
        self.assert_statuses(
            "\\begin{figure}\\fbox{\\rule{1cm}{1cm}}\\caption{A}\\end{figure}",
            float_labels="failed",
            float_references="failed",
        )

    @clean_case("MAN011", "MAN012", "MAN013", "MAN014", "MAN015")
    def test_a_gap_outside_every_float_leaves_their_checks_decided(self) -> None:
        # Defect: a source-graph gap anywhere made every float check inconclusive.
        self.write("sec.tex", "See Figure~\\ref{fig:b}.\n")
        floats = (
            "\\begin{figure}\\centering\\rule{1cm}{1cm}\\caption{Plain.}\\label{fig:b}\\end{figure}"
        )
        for preamble, body in (
            ("", "\\input{sec}\n{\\small Closing text.}\n" + floats),
            (
                "\\newcommand{\\mk}[1]{\\expandafter\\xdef\\csname m@#1\\endcsname{x}}\n",
                "See \\ref{fig:b}." + floats,
            ),
            (
                "",
                "\\begin{itemize}\\item {natbib.sty} for citations.\\end{itemize}"
                "See \\ref{fig:b}." + floats,
            ),
        ):
            with self.subTest(preamble=preamble, body=body):
                self.assert_statuses(body, preamble)

    def test_text_that_was_not_read_still_counts(self) -> None:
        # Counter-tests: the caption is in a file that cannot be read; a file that cannot be
        # read may hold a whole float; a dynamic inclusion as well.
        self.assert_statuses(
            "See \\ref{fig:a}.\\begin{figure}\\input{missing}\\label{fig:a}\\end{figure}",
            float_captions="inconclusive",
            float_label_order="inconclusive",
        )
        undecided = dict.fromkeys(FLOAT_RULES, "inconclusive")
        self.assert_statuses(
            "See \\ref{fig:a}.\\begin{figure}\\caption{A}\\label{fig:a}\\end{figure}"
            "\\input{missing}",
            **undecided,
        )
        self.assert_statuses(
            "See \\ref{fig:a}.\\begin{figure}\\caption{A}\\label{fig:a}\\end{figure}"
            "\\input{\\figdir/more}",
            "\\newcommand{\\figdir}{figs}\n",
            **undecided,
        )

    def test_a_float_without_label_is_left_out_of_the_reference_order(self) -> None:
        # Defect: one unlabelled float made the order check inconclusive for every float.
        # The label and reference checks report that float; the order check compares the
        # floats that have a first reference.
        self.assert_statuses(
            "See Figure~\\ref{fig:d}.\n"
            "\\begin{figure}\\centering\\rule{1cm}{1cm}\\caption{Labelled.}\\label{fig:d}"
            "\\end{figure}\n"
            "\\begin{figure}\\centering\\rule{1cm}{1cm}\\caption{No label.}\\end{figure}",
            float_labels="failed",
            float_references="failed",
        )

    def test_a_float_whose_first_reference_may_lie_elsewhere_keeps_the_order_open(self) -> None:
        # Counter-test: the second figure's only reference is inside \iffalse.
        self.assert_statuses(
            "See \\ref{fig:a}.\\iffalse See \\ref{fig:b}.\\fi\n"
            "\\begin{figure}\\caption{A}\\label{fig:a}\\end{figure}"
            "\\begin{figure}\\caption{B}\\label{fig:b}\\end{figure}",
            float_references="inconclusive",
            float_reference_order="inconclusive",
        )

    @clean_case("MAN013")
    def test_two_minipages_each_with_caption_and_label(self) -> None:
        # Defect: read as "multiple captions within one float".
        self.assert_statuses(
            "See Figures~\\ref{fig:l} and~\\ref{fig:r}.\\begin{figure}"
            "\\begin{minipage}{0.45\\linewidth}\\rule{1cm}{1cm}\\caption{Left.}\\label{fig:l}"
            "\\end{minipage}\\hfill\\begin{minipage}{0.45\\linewidth}\\rule{1cm}{1cm}"
            "\\caption{Right.}\\label{fig:r}\\end{minipage}\\end{figure}"
        )

    def test_labels_that_may_belong_to_another_caption_stay_open(self) -> None:
        # Counter-tests: a second label between two captions may be the second caption's;
        # a label before both captions precedes its caption.
        self.assert_statuses(
            "See \\ref{fig:a} and \\ref{fig:b}.\\begin{figure}\\caption{A}\\label{fig:a}"
            "\\label{fig:b}\\caption{B}\\end{figure}",
            float_label_order="inconclusive",
        )
        self.assert_statuses(
            "See \\ref{fig:a}.\\begin{figure}\\label{fig:a}\\caption{A}\\caption{B}\\end{figure}",
            float_label_order="failed",
        )

    @clean_case("MAN013")
    def test_a_label_inside_its_caption_follows_it(self) -> None:
        # Defect: \caption{...\label{x}} and REVTeX's \caption{\label{x}...} were reported as
        # labels placed before their caption.
        for caption in ("\\caption{Plain caption.\\label{fig:c}}", "\\caption{\\label{fig:c}A.}"):
            with self.subTest(caption=caption):
                self.assert_statuses(
                    "See Figure~\\ref{fig:c}.\\begin{figure}\\centering\\rule{1cm}{1cm}"
                    + caption
                    + "\\end{figure}"
                )

    def test_a_label_before_its_caption_still_fails(self) -> None:
        results = self.statuses(
            "See Figure~\\ref{fig:early}.\\begin{figure}\\centering\\rule{1cm}{1cm}\n"
            "\\label{fig:early}\n\\caption{The label stands before this caption.}\\end{figure}"
        )
        self.assertEqual(results["float_label_order"].status, "failed")
        self.assertEqual(results["float_label_order"].details["matches"][0]["line"], 4)

    @clean_case("MAN011", "MAN012", "MAN013", "MAN014", "MAN015")
    def test_accents_and_nested_graphics_pass(self) -> None:
        # Regressions of the saved TeX Live reproductions accent and nested.
        self.assert_statuses(
            "See Figure~\\ref{fig:a}.\\begin{figure}\\centering\\rule{1cm}{1cm}"
            "\\caption{Schr\\\"odinger's cat costs \\$5.}\\label{fig:a}\\end{figure}"
        )
        self.assert_statuses(
            "See Figure~\\ref{fig:a}.\\begin{figure}\\centering"
            "\\resizebox{0.5\\linewidth}{!}{\\includegraphics{a}}\\caption{A caption.}"
            "\\label{fig:a}\\end{figure}",
            "\\usepackage{graphicx}\n",
        )

    def test_a_reference_inside_iffalse_does_not_count(self) -> None:
        self.assert_statuses(
            "\\begin{figure}\\caption{A}\\label{fig:a}\\end{figure}"
            "\\iffalse See Figure~\\ref{fig:a}.\\fi",
            float_references="inconclusive",
        )

    @clean_case("MAN014", "MAN015")
    def test_a_reference_macro_of_the_project_is_read_at_each_use(self) -> None:
        # Triage: \newcommand\Pic[1]{Figure~\ref{fig:#1}} was not expanded.
        self.assert_statuses(
            "Our results appear in \\Pic{alpha}, then \\Pic{beta}.\n"
            "\\begin{figure}\\caption{A.}\\label{fig:alpha}\\end{figure}"
            "\\begin{figure}\\caption{B.}\\label{fig:beta}\\end{figure}",
            "\\newcommand\\Pic[1]{Figure~\\ref{fig:#1}}\n",
        )

    def test_a_reference_macro_that_names_another_float_or_hides_its_reference(self) -> None:
        # Counter-tests: the macro names another label, or hides its reference in a
        # conditional, which the reader cannot expand.
        self.assert_statuses(
            "See \\Pic{beta}.\\begin{figure}\\caption{A.}\\label{fig:alpha}\\end{figure}",
            "\\newcommand\\Pic[1]{Figure~\\ref{fig:#1}}\n",
            float_references="failed",
        )
        self.assert_statuses(
            "See \\Pic{alpha}.\\begin{figure}\\caption{A.}\\label{fig:alpha}\\end{figure}",
            "\\newcommand\\Pic[1]{\\iffalse Figure~\\ref{fig:#1}\\fi}\n",
            float_references="inconclusive",
        )

    @clean_case("MAN014", "MAN015")
    def test_a_reference_in_the_argument_of_a_wrapper_macro_runs_in_place(self) -> None:
        # Triage: \changed{... \ref{tab:one}} was skipped, so tab:one looked unreferenced or
        # first referenced late.
        self.assert_statuses(
            "\\changed{The first is in Table~\\ref{tab:one}.} The second is in "
            "Table~\\ref{tab:two}. Table~\\ref{tab:one} again."
            "\\begin{table}\\caption{First.}\\label{tab:one}\\end{table}"
            "\\begin{table}\\caption{Second.}\\label{tab:two}\\end{table}",
            "\\newcommand{\\changed}[1]{\\textcolor{blue}{#1}}\n",
        )

    def test_a_reference_in_a_macro_that_drops_its_argument_does_not_count(self) -> None:
        # Counter-test: \hide drops its argument, so the reference never runs.
        self.assert_statuses(
            "\\hide{See Table~\\ref{tab:one}.}"
            "\\begin{table}\\caption{First.}\\label{tab:one}\\end{table}",
            "\\newcommand{\\hide}[1]{}\n",
            float_references="inconclusive",
        )

    def test_a_caption_inside_an_environment_that_may_not_typeset_it_is_not_evidence(
        self,
    ) -> None:
        # Review: a caption and label saved in a box, or inside an environment of the paper
        # that throws its content away, satisfied the caption and label checks.
        for float_body, preamble in (
            (
                "\\begin{lrbox}{\\mybox}\\caption{A}\\label{fig:a}\\end{lrbox}",
                "\\newsavebox\\mybox\n",
            ),
            (
                "\\begin{hidebox}\\caption{A}\\label{fig:a}\\end{hidebox}",
                "\\newenvironment{hidebox}{\\setbox0\\vbox\\bgroup}{\\egroup}\n",
            ),
        ):
            with self.subTest(float_body=float_body):
                self.assert_statuses(
                    "See \\ref{fig:a}.\n\\begin{figure}" + float_body + "\\end{figure}",
                    preamble,
                    float_captions="inconclusive",
                    float_labels="inconclusive",
                    float_label_order="inconclusive",
                )
        # An environment of the paper that only arranges its content keeps the evidence.
        self.assert_statuses(
            "See \\ref{fig:a}.\n\\begin{figure}\\begin{tight}\\caption{A}\\label{fig:a}"
            "\\end{tight}\\end{figure}",
            "\\newenvironment{tight}{\\begin{center}\\small}{\\end{center}}\n",
        )

    def test_a_reference_command_the_document_defines_anew_is_not_read_as_one(self) -> None:
        # Review: \renewcommand\ref[1]{} prints nothing, yet its uses counted as references.
        body = "See \\ref{fig:a}.\n\\begin{figure}\\caption{A}\\label{fig:a}\\end{figure}"
        for preamble in (
            "\\renewcommand\\ref[1]{}",
            "\\def\\ref#1{}",
            "\\makeatletter\\let\\ref\\@gobble\\makeatother",
        ):
            with self.subTest(preamble=preamble):
                result = self.statuses(body, preamble + "\n")["float_references"]
                self.assertEqual(result.status, "inconclusive", result.message)
                self.assertIn("the document defines \\ref", result.message)
        # A definition that hands its argument to a reference command is read at each use,
        # and naming \ref in a definition refers to nothing.
        self.assert_statuses(
            "See \\eqref{fig:a}.\n\\begin{figure}\\caption{A}\\label{fig:a}\\end{figure}",
            "\\def\\eqref#1{equation~\\ref{#1}}\n",
        )
        self.assert_statuses(
            "\\begin{figure}\\caption{A}\\label{fig:a}\\end{figure}",
            "\\let\\plainref\\ref\n",
            float_references="failed",
        )

    @clean_case("MAN014")
    def test_a_reference_from_another_floats_caption_counts(self) -> None:
        # Triage: references inside every float were ignored.
        self.assert_statuses(
            "The headline numbers are in Table~\\ref{tab:head}.\n"
            "\\begin{table}\\caption{Headline; spreads are in Table~\\ref{tab:spread}.}"
            "\\label{tab:head}\\end{table}"
            "\\begin{table}\\caption{Spreads.}\\label{tab:spread}\\end{table}"
        )
        # Counter-test: a float that only refers to itself is still unreferenced.
        self.assert_statuses(
            "\\begin{table}\\caption{See Table~\\ref{tab:self}.}\\label{tab:self}\\end{table}",
            float_references="failed",
        )

    @clean_case("MAN011", "MAN012", "MAN013", "MAN014", "MAN015")
    def test_the_float_macro_and_references_of_the_jmlr_class_are_read(self) -> None:
        # PMLR template: \floatconts{label}{caption}{content} made every float check
        # inconclusive, and \tableref and \figureref were not read as references.
        body = (
            "See \\tableref{tab:scores} and \\figureref{fig:curve,fig:setup}.\n"
            "\\begin{table}\\floatconts{tab:scores}%%\n  {\\caption{Scores}}%%\n"
            "  {\\begin{tabular}{ll}a & b\\end{tabular}}\\end{table}\n"
            "\\begin{figure}\\floatconts{fig:curve}{\\caption{%s}}"
            "{\\includegraphics{curve}}\\end{figure}\n"
            "\\begin{figure}\\floatconts{%s}{\\caption{Setup}}{\\rule{1cm}{1cm}}\\end{figure}"
        )
        self.assert_statuses(body % ("Curve", "fig:setup"))
        # Counter-tests: an empty caption, and a label that nothing refers to.
        self.assert_statuses(body % ("", "fig:setup"), float_captions="failed")
        self.assert_statuses(body % ("Curve", "fig:other"), float_references="failed")
        # Counter-test: a paper that defines the macro itself may drop what it is given.
        self.assert_statuses(
            body % ("Curve", "fig:setup"),
            "\\newcommand{\\floatconts}[3]{#3}\n",
            float_captions="inconclusive",
            float_labels="inconclusive",
            float_label_order="inconclusive",
            float_references="inconclusive",
            float_reference_order="inconclusive",
        )

    @clean_case("MAN015")
    def test_the_order_of_citations_follows_the_running_text(self) -> None:
        # Held-out paper: the caption of the first figure names the second, and the text
        # then cites the first before the second. A caption is printed where LaTeX places
        # its float, so it does not cite the second figure early.
        self.assert_statuses(
            "\\begin{figure}\\caption{Masses; examples are in Fig.~\\ref{fig:examples}.}"
            "\\label{fig:masses}\\end{figure}\n"
            "\\begin{figure}\\caption{Examples.}\\label{fig:examples}\\end{figure}\n"
            "Fig.~\\ref{fig:masses} shows the masses, Fig.~\\ref{fig:examples} the examples."
        )
        # A float cited only from another float is placed where that float stands.
        self.assert_statuses(
            "See Fig.~\\ref{fig:a}.\n"
            "\\begin{figure}\\caption{A; details in Fig.~\\ref{fig:b}.}\\label{fig:a}"
            "\\end{figure}\n"
            "\\begin{figure}\\caption{B.}\\label{fig:b}\\end{figure}"
        )
        # Counter-test: a caption that names a figure early does not put right a text that
        # cites the figures in the wrong order.
        result = self.statuses(
            "\\begin{table}\\caption{Setup; see Fig.~\\ref{fig:first}.}\\label{tab:setup}"
            "\\end{table}\n"
            "Table~\\ref{tab:setup}. Fig.~\\ref{fig:second} comes before Fig.~\\ref{fig:first}.\n"
            "\\begin{figure}\\caption{First.}\\label{fig:first}\\end{figure}\n"
            "\\begin{figure}\\caption{Second.}\\label{fig:second}\\end{figure}"
        )["float_reference_order"]
        self.assertEqual(result.status, "failed")
        self.assertEqual([match["labels"] for match in result.details["matches"]], [["fig:first"]])
        # Counter-test: a range in running text still cites the floats between its ends.
        tables = "".join(
            f"\\begin{{table}}\\caption{{{name}.}}\\label{{tab:{name}}}\\end{{table}}\n"
            for name in ("one", "two", "three", "four")
        )
        self.assert_statuses(
            "Table~\\ref{tab:four} first, then Tables~\\ref{tab:one}--\\ref{tab:three}.\n" + tables,
            float_reference_order="failed",
        )

    @clean_case("MAN014", "MAN015")
    def test_a_range_refers_to_the_floats_between_its_ends(self) -> None:
        # Triage: \crefrange{a}{c} and \ref{a}--\ref{c} recorded only the two ends.
        tables = "".join(
            f"\\begin{{table}}\\caption{{{name}.}}\\label{{tab:{name}}}\\end{{table}}"
            for name in ("first", "second", "third")
        )
        for reference in (
            "\\crefrange{tab:first}{tab:third}",
            "Tables~\\ref{tab:first}--\\ref{tab:third}",
        ):
            with self.subTest(reference=reference):
                self.assert_statuses("Scores are in " + reference + ".\n" + tables)
        # Counter-test: two references joined by a comma are no range.
        self.assert_statuses(
            "Scores are in Tables~\\ref{tab:first}, \\ref{tab:third}.\n" + tables,
            float_references="failed",
        )

    def test_the_order_check_names_the_float_cited_late(self) -> None:
        # Triage: every float after the late one was named instead of the late one.
        figures = "".join(
            f"\\begin{{figure}}\\caption{{{name}.}}\\label{{fig:{name}}}\\end{{figure}}\n"
            for name in ("method", "result")
        )
        result = self.statuses(
            "\\begin{figure}\\caption{Overview.}\\label{fig:overview}\\end{figure}\n"
            "Method in \\ref{fig:method}, result in \\ref{fig:result}.\n"
            + figures
            + "Only now \\ref{fig:overview}."
        )["float_reference_order"]
        self.assertEqual(result.status, "failed")
        self.assertEqual([match["line"] for match in result.details["matches"]], [3])
        self.assertIn("1 of 3 floats first referenced out of source order", result.message)

    def test_a_failure_says_how_many_floats_could_not_be_judged(self) -> None:
        # Triage: counts read as complete although undecided floats were left out.
        result = self.statuses(
            "\\begin{figure}\\caption{A}\\end{figure}\\begin{figure}\\caption{B}\\mymark"
            "\\end{figure}\\begin{figure}\\caption{C}\\label{fig:c}\\end{figure}See \\ref{fig:c}.",
            "\\newcommand{\\mymark}{\\foo}\n",
        )["float_labels"]
        self.assertEqual(result.status, "failed")
        self.assertTrue(
            result.message.startswith(
                "1 of 3 floats without a label (1 float could not be judged): main.tex:4."
            ),
            result.message,
        )
        self.assertIn("figure without a literal label", str(result.details["uncertainty"]))
