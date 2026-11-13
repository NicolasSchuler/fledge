r"""Where a command runs: typesetting arguments, named tokens and what a finding can hide.

The lexer records the command that takes each brace group and the tokens that a
definition or conditional test only names. The source checks, the citation graph and the
spliced manuscript use these facts so that ``\textbf{\cite{x}}``,
``\subfloat{\includegraphics{a}}`` and ``\ifx\citeauthoryear\undefined`` in a venue class
are not gaps, while a citation in an unknown macro or a redefined ``\cite`` still is.
"""

from __future__ import annotations

import time
import tracemalloc
import unittest

from latexprep.citation_graph import build_citation_graph
from latexprep.expanded_document import expand_document
from latexprep.source_analysis import analyze_sources
from latexprep.source_graph import SourceGraph
from latexprep.tex_lexer import scan_source_commands
from latexprep.tex_spans import (
    conditional_branches,
    conditional_spans,
    definition_spans,
    in_conditional,
    runs_in_place,
)
from tests.support import ProjectTestCase


def _document(body: str, *, preamble: str = "") -> str:
    """Return a main file with ``body`` in its document environment and one bibliography."""
    return (
        "\\documentclass{article}\n" + preamble + "\\begin{document}\n" + body + "\n"
        "\\bibliographystyle{plain}\n\\bibliography{refs}\n\\end{document}\n"
    )


class LexerExecutionContextTests(unittest.TestCase):
    """The groups around each command and the tokens that are named instead of run."""

    def test_each_brace_group_records_the_command_that_takes_it(self) -> None:
        commands = scan_source_commands(r"\textbf{\cite{a}} x {\cite{b}} \foo{\emph{\cite{c}}}")
        cites = [command.groups for command in commands if command.name == "cite"]
        self.assertEqual(cites, [("textbf",), (None,), ("foo", "emph")])

    def test_definitions_and_tests_name_tokens_without_running_them(self) -> None:
        text = (
            r"\ifx\citeauthoryear\undefined\fi"
            r"\renewcommand\setcitestyle[1]{x}\newcommand{\bibsection}{y}"
            r"\let\a=\b \def\c{z}\cite{k}"
        )
        named = {command.name: command.named_by for command in scan_source_commands(text)}
        self.assertEqual(named["citeauthoryear"], "ifx")
        self.assertEqual(named["undefined"], "ifx")
        self.assertEqual(named["setcitestyle"], "renewcommand")
        self.assertEqual(named["bibsection"], "newcommand")
        self.assertEqual((named["a"], named["b"]), ("let", "let"))
        self.assertEqual(named["c"], "def")
        self.assertIsNone(named["cite"])
        self.assertIsNone(named["fi"])

    def test_a_stringified_token_is_text_and_not_a_command(self) -> None:
        commands = scan_source_commands(
            r"\ClassError{acmart}{use \string\documentclass[languages]{acmart}}{}"
        )
        self.assertNotIn("documentclass", [command.name for command in commands])
        self.assertIn("string", [command.name for command in commands])

    def test_deep_nesting_costs_linear_time_and_memory(self) -> None:
        # Every group is linked to its parent, so no command copies the groups around it.
        text = "{\\a." * 20_000 + "}" * 20_000
        tracemalloc.start()
        started = time.monotonic()
        commands = scan_source_commands(text)
        elapsed = time.monotonic() - started
        peak = tracemalloc.get_traced_memory()[1]
        tracemalloc.stop()
        self.assertEqual(commands[-1].depth, 20_000)
        self.assertLess(peak, 64 * 1024 * 1024)
        self.assertLess(elapsed, 10)
        self.assertFalse(runs_in_place(commands[-1], []))

    def test_a_stringified_paragraph_break_leaves_the_next_command(self) -> None:
        names = [command.name for command in scan_source_commands("\\string\n\n\\cite{k}")]
        self.assertIn("cite", names)

    def test_a_conditional_that_a_test_only_names_opens_no_span(self) -> None:
        text = r"\ifx\iffoo\relax\input{a}\fi"
        commands = scan_source_commands(text)
        spans = conditional_spans(commands)
        self.assertEqual(spans, [(0, text.index(r"\fi"))])
        located = {command.name: command.start for command in commands}
        self.assertTrue(in_conditional(spans, located["input"]))


class SpanExecutionContextTests(unittest.TestCase):
    """Whether a command runs in place, and the body of an unbraced definition."""

    def test_typesetting_arguments_and_bare_groups_run_in_place(self) -> None:
        text = (
            r"\caption{\cite{a}}\resizebox{1}{!}{\subfloat[x]{\includegraphics{f}}}"
            r"{\small\cite{b}}\myfig{\includegraphics{g}}\def\x#1{\cite{c}}"
        )
        commands = scan_source_commands(text)
        definitions = definition_spans(text, commands)
        placed = {
            (command.name, command.arguments[0].value if command.arguments else ""): runs_in_place(
                command, definitions
            )
            for command in commands
            if command.name in {"cite", "includegraphics"}
        }
        self.assertEqual(
            placed,
            {
                ("cite", "a"): True,
                ("includegraphics", "f"): True,
                ("cite", "b"): True,
                ("includegraphics", "g"): False,
                ("cite", "c"): False,
            },
        )

    def test_an_unbraced_command_definition_spans_its_body(self) -> None:
        text = r"\renewcommand\setcitestyle[1]{\NAT@set{#1}} \cite{k}"
        commands = scan_source_commands(text)
        spans = definition_spans(text, commands)
        self.assertEqual(spans, [(0, text.index(" \\cite"))])
        # The body is the first group; a later group is text.
        text = r"\renewcommand\x{A}{\cite{b}}"
        spans = definition_spans(text, scan_source_commands(text))
        self.assertEqual(spans, [(0, text.index("{\\cite"))])

    def test_a_branch_after_a_character_comparison_has_no_known_start(self) -> None:
        commands = scan_source_commands(r"\ifx\a X\else X\fi")
        self.assertIsNone(conditional_branches(commands, 0))
        commands = scan_source_commands(r"\ifx\a\b X\else Y\fi")
        self.assertIsNotNone(conditional_branches(commands, 0))


class CitationExecutionContextTests(ProjectTestCase):
    """Citation gaps: none in typesetting arguments; unknown macros and redefinitions stay."""

    def setUp(self) -> None:
        super().setUp()
        self.write(
            "refs.bib", "@misc{a, title={A}, year={2026}}\n@misc{b, title={B}, year={2026}}\n"
        )

    def citation_gaps(self, main: str) -> list[str]:
        """Return the citation evidence gaps of ``main`` written as the project's main file."""
        self.write("main.tex", main)
        graph = build_citation_graph(SourceGraph.build(self.root, "main.tex"))
        return [item.message for item in graph.uncertainties]

    def test_citations_in_typesetting_arguments_leave_no_gap(self) -> None:
        body = "\n".join(
            (
                r"\textbf{See \cite{a}.} \footnote{See \cite{b}.}",
                r"\begin{figure}\caption{Cap \cite{a}}\end{figure}",
                r"\resizebox{\linewidth}{!}{\begin{tabular}{l}\cite{b}\\\end{tabular}}",
                r"{\small See \cite{a}.}",
            )
        )
        self.assertEqual(self.citation_gaps(_document(body)), [])

    def test_citations_in_unknown_macros_and_definitions_stay_gaps(self) -> None:
        for body in (r"\foo{\cite{a}}", r"\def\mycite#1{\cite{a}}\mycite{x}"):
            with self.subTest(body=body):
                gaps = self.citation_gaps(_document(body))
                self.assertTrue(any("grouped citation" in gap for gap in gaps), gaps)

    def test_citations_where_the_project_may_drop_them_stay_gaps(self) -> None:
        cases = (
            # A definition body on the line after its parameters.
            "\\def\\everything#1\n{\\nocite{*}}",
            "\\def\\addfig#1#2%\n{\\caption{#2 \\cite{a}}}",
            # A typesetting command or environment that the project redefines.
            r"\renewcommand{\sout}[1]{}\sout{\cite{a}}",
            r"\renewcommand\textbf[1]{}\textbf{\cite{a}}",
            r"\newenvironment{hideme}[1]{}{}\begin{hideme}{\cite{a}}\end{hideme}",
            # A seventh argument, and arguments after <…> or (…) delimiters.
            r"\foo{1}{2}{3}{4}{5}{6}{\cite{a}}",
            r"\only<2>{\cite{a}}",
        )
        for body in cases:
            with self.subTest(body=body):
                self.assertNotEqual(self.citation_gaps(_document(body)), [])

    def test_a_venue_class_that_redefines_a_typesetting_command_keeps_it_typeset(self) -> None:
        self.write(
            "venue.cls",
            "\\ProvidesClass{venue}\n\\LoadClass{article}\n\\renewcommand\\footnote[1]{}\n",
        )
        main = _document(r"See\footnote{\cite{a}}").replace("{article}", "{venue}", 1)
        self.assertEqual(self.citation_gaps(main), [])

    def test_redefining_a_citation_command_stays_a_gap(self) -> None:
        for preamble in (r"\def\cite#1{}", r"\let\cite\relax", r"\renewcommand\cite[1]{}"):
            with self.subTest(preamble=preamble):
                gaps = self.citation_gaps(_document(r"\cite{a}", preamble=preamble + "\n"))
                self.assertTrue(any("macro definition" in gap for gap in gaps), gaps)

    def test_an_input_that_runs_if_a_file_exists_is_a_gap(self) -> None:
        self.write("extra.tex", "\\cite{b}\n")
        gaps = self.citation_gaps(_document(r"\InputIfFileExists{extra.tex}{}{}\cite{a}"))
        self.assertTrue(any("InputIfFileExists" in gap for gap in gaps), gaps)

    def test_style_switches_and_named_tokens_in_a_venue_class_leave_no_gap(self) -> None:
        self.write(
            "venue.cls",
            "\n".join(
                (
                    r"\ProvidesClass{venue}",
                    r"\LoadClass{article}",
                    r"\ifx\citeauthoryear\undefined\def\venue@x{}\fi",
                    r"\if@venue@natbib",
                    r"  \renewcommand\setcitestyle[1]{\NAT@set{#1}\NAT@@setcites}",
                    r"\fi",
                    r"\if@venue@natbib\citestyle{venuenumeric}\fi",
                    r"\ClassError{venue}{Use \string\documentclass[x]{venue}}{}",
                    "",
                )
            ),
        )
        self.write("main.tex", _document(r"\cite{a}").replace("{article}", "{venue}", 1))
        inspection = SourceGraph.build(self.root, "main.tex")
        self.assertEqual(build_citation_graph(inspection).uncertainties, [])
        self.assertNotIn("source.input_cycle", [item.rule for item in inspection.findings])


class FigureExecutionContextTests(ProjectTestCase):
    """Figures in subfigures run in place; figures elsewhere hide no text or citation."""

    def setUp(self) -> None:
        super().setUp()
        self.write("refs.bib", "@misc{a, title={A}, year={2026}}\n")
        self.write("f.pdf", b"%PDF-1.4\n")

    def test_a_figure_in_a_subfloat_is_not_a_nested_file_command(self) -> None:
        self.write(
            "main.tex",
            _document(r"\begin{figure}\subfloat[A]{\includegraphics{f}\label{fig:a}}\end{figure}"),
        )
        rules = [item.rule for item in analyze_sources(self.root, "main.tex").findings]
        self.assertNotIn("source.nested_file_command", rules)

    def test_a_figure_in_an_unknown_macro_is_nested_but_hides_no_text(self) -> None:
        self.write(
            "main.tex",
            _document(
                r"\myfig{\includegraphics{f}} \cite{a}", preamble="\\newcommand\\myfig[1]{#1}\n"
            ),
        )
        nested = [
            item
            for item in analyze_sources(self.root, "main.tex").findings
            if item.rule == "source.nested_file_command"
        ]
        self.assertEqual([item.details["command"] for item in nested], ["includegraphics"])
        self.assertEqual(expand_document(self.root, "main.tex").graph_uncertainty, [])
        graph = build_citation_graph(SourceGraph.build(self.root, "main.tex"))
        self.assertEqual(graph.uncertainties, [])

    def test_merged_findings_keep_what_each_kind_can_hide(self) -> None:
        # A figure and an \input in macros of one style file: the input is still a gap.
        self.write("part.tex", "Text.\n")
        self.write("defs.sty", "\\keep{\\includegraphics{f}}\n\\keep{\\input{part}}\n")
        self.write("main.tex", _document(r"\cite{a}", preamble="\\input{defs.sty}\n"))
        nested = [
            item
            for item in SourceGraph.build(self.root, "main.tex").findings
            if item.rule == "source.nested_file_command"
        ]
        self.assertEqual(
            sorted(item.details.get("command") for item in nested), ["includegraphics", "input"]
        )
        self.assertTrue(expand_document(self.root, "main.tex").graph_uncertainty)

    def test_an_unknown_command_with_one_source_file_in_its_list_may_hide_text(self) -> None:
        self.write("main.tex", _document(r"\loadparts{abstract.tex, logo.png}"))
        self.assertTrue(expand_document(self.root, "main.tex").graph_uncertainty)
        self.write("main.tex", _document(r"\loadparts{data.csv, logo.png}"))
        self.assertEqual(expand_document(self.root, "main.tex").graph_uncertainty, [])

    def test_findings_of_one_rule_in_a_class_file_are_merged(self) -> None:
        self.write(
            "venue.cls",
            "\\ProvidesClass{venue}\n\\LoadClass{article}\n"
            + "".join(
                f"\\def\\venue@logo{index}{{\\includegraphics{{\\venue@f}}}}\n"
                for index in range(3)
            ),
        )
        self.write("main.tex", _document(r"\cite{a}").replace("{article}", "{venue}", 1))
        dynamic = [
            item
            for item in SourceGraph.build(self.root, "main.tex").findings
            if item.rule == "source.dynamic_path"
        ]
        self.assertEqual(len(dynamic), 1, dynamic)
        self.assertEqual(dynamic[0].details["occurrences"], 3)
        self.assertEqual(dynamic[0].details["lines"], [3, 4, 5])
        self.assertIn("3 such findings (\\includegraphics)", dynamic[0].message)


if __name__ == "__main__":
    unittest.main()
