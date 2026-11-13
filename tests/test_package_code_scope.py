r"""A class or package file in the project is package code, not manuscript content.

Venue class files such as ``IEEEtran.cls`` are often shipped inside the project. Their
macro code (``\catcode``, ``\csname``, packages loaded inside macros) is outside the
literal subset, which matters for flat layout and source transformations but says
nothing about the manuscript's text, citations, floats or declarations.
"""

from __future__ import annotations

import unittest

from latexprep.citation_graph import build_citation_graph
from latexprep.expanded_document import expand_document
from latexprep.flat_layout import plan_flatten
from latexprep.manuscript import ManuscriptOptions, check_manuscript
from latexprep.manuscript_check_options import ManuscriptCheckOptions
from latexprep.manuscript_checks import check_manuscript_policies
from latexprep.source_analysis import analyze_sources
from latexprep.source_graph import SourceGraph
from tests.support import ProjectTestCase

# A venue class written the way real ones are: run-time commands, packages loaded inside
# macros and conditionals, a definition of \thanks, and a final \endinput.
VENUE_CLASS = r"""\NeedsTeXFormat{LaTeX2e}
\ProvidesClass{venue}[2026/01/01]
\LoadClass{article}
\catcode`\@=11
\expandafter\def\csname venue@size\endcsname{10pt}
\def\venue@mathfonts{\RequirePackage{newtxmath}}
\ifx\venue@x\relax\RequirePackage{mathptmx}\fi
\IfFileExists{venue.cfg}{}{}
\expandafter\let\csname venue@thanks\endcsname\thanks
\long\def\thanks#1{\footnotemark}
\endinput
% End of venue.cls
"""
MAIN = r"""\documentclass{venue}
\begin{document}
\begin{figure}\caption{Results}\label{fig:results}\end{figure}
See \ref{fig:results} and \cite{key}.
\bibliographystyle{plain}
\bibliography{refs}
\end{document}
"""


class PackageCodeScopeTests(ProjectTestCase):
    """Uncertainty inside a project class file stays with the dependency graph."""

    def setUp(self) -> None:
        super().setUp()
        self.write("venue.cls", VENUE_CLASS)
        self.write("main.tex", MAIN)
        self.write("refs.bib", "@misc{key, title={T}, year={2026}}\n")

    def test_run_time_commands_of_a_class_file_are_one_finding(self) -> None:
        findings = analyze_sources(self.root, "main.tex").findings
        runtime = [item for item in findings if item.rule == "source.unsupported_command"]
        self.assertEqual(len(runtime), 1, runtime)
        self.assertEqual((runtime[0].path, runtime[0].line), ("venue.cls", 4))
        self.assertEqual(runtime[0].code, "TEX312")
        self.assertEqual(runtime[0].details["commands"], ["IfFileExists", "catcode", "csname"])
        self.assertEqual(runtime[0].details["occurrences"], 4)

    def test_toolchain_packages_loaded_inside_class_macros_are_not_nested_findings(self) -> None:
        findings = analyze_sources(self.root, "main.tex").findings
        self.assertFalse([item for item in findings if item.rule == "source.nested_file_command"])

    def test_a_nested_load_of_a_project_package_is_still_reported(self) -> None:
        self.write("local.sty", "\\ProvidesPackage{local}\n")
        self.write("venue.cls", VENUE_CLASS.replace("{newtxmath}", "{local}"))
        nested = [
            item
            for item in analyze_sources(self.root, "main.tex").findings
            if item.rule == "source.nested_file_command"
        ]
        self.assertEqual([(item.path, item.line) for item in nested], [("venue.cls", 6)])

    def test_run_time_commands_in_the_manuscript_are_still_reported_one_by_one(self) -> None:
        self.write("main.tex", MAIN.replace("\\begin{document}", "\\csname a\\endcsname\n" * 2))
        runtime = [
            item
            for item in analyze_sources(self.root, "main.tex").findings
            if item.rule == "source.unsupported_command"
        ]
        self.assertEqual(
            sorted((item.path, item.line) for item in runtime)[:2],
            [
                ("main.tex", 2),
                ("main.tex", 3),
            ],
        )

    def test_flat_layout_still_refuses_the_class_file_it_cannot_follow(self) -> None:
        refused = [
            item
            for item in plan_flatten(self.root, "main.tex").findings
            if item.rule == "flatten.unsupported_source"
        ]
        self.assertEqual([item.path for item in refused], ["venue.cls"])

    def test_the_spliced_manuscript_has_no_gaps(self) -> None:
        self.assertEqual(expand_document(self.root, "main.tex").graph_uncertainty, [])

    def test_citation_evidence_has_no_gaps_and_keeps_the_resource(self) -> None:
        graph = build_citation_graph(SourceGraph.build(self.root, "main.tex"))
        self.assertEqual(graph.uncertainties, [])
        self.assertEqual(graph.resources, {"refs.bib"})
        self.assertEqual([item.key for item in graph.citations], ["key"])

    def test_class_options_and_forbidden_packages_are_conclusive(self) -> None:
        options = ManuscriptOptions(
            forbidden_class_options=("draft",), forbidden_packages=("showframe",)
        )
        results = {item.rule: item for item in check_manuscript(self.root, "main.tex", options)}
        self.assertEqual(results["manuscript.class_options"].status, "passed")
        self.assertEqual(results["manuscript.forbidden_packages"].status, "passed")

    def test_a_final_endinput_in_an_open_group_hides_nothing(self) -> None:
        # Catcode changes can leave the literal scan inside a group at the end of a class.
        options = ManuscriptOptions(forbidden_packages=("showframe",))
        opened = VENUE_CLASS.replace("\\endinput", "{\n\\endinput")
        for text, status in (
            (opened, "passed"),
            (opened + "\\RequirePackage{xcolor}\n", "inconclusive"),
        ):
            with self.subTest(status=status):
                self.write("venue.cls", text)
                results = {
                    item.rule: item for item in check_manuscript(self.root, "main.tex", options)
                }
                self.assertEqual(results["manuscript.forbidden_packages"].status, status)

    def test_an_unclear_declaration_of_a_forbidden_package_stays_inconclusive(self) -> None:
        self.write("venue.cls", VENUE_CLASS.replace("{newtxmath}", "{showframe}"))
        options = ManuscriptOptions(forbidden_packages=("showframe",))
        results = {item.rule: item for item in check_manuscript(self.root, "main.tex", options)}
        self.assertEqual(results["manuscript.forbidden_packages"].status, "inconclusive")

    def test_float_checks_are_conclusive(self) -> None:
        options = ManuscriptCheckOptions(
            require_float_captions=True, require_float_labels=True, require_float_references=True
        )
        results = check_manuscript_policies(self.root, "main.tex", options)
        statuses = {
            item.rule: item.status for item in results if item.rule.startswith("manuscript.float")
        }
        self.assertEqual(
            statuses,
            {
                "manuscript.float_captions": "passed",
                "manuscript.float_labels": "passed",
                "manuscript.float_references": "passed",
            },
        )


if __name__ == "__main__":
    unittest.main()
