"""Keys read out of a generated .bbl, in BibTeX's and biblatex's forms."""

from __future__ import annotations

import time
import unittest

from latexprep.bbl_checks import Backend, BblOptions, bbl_keys, check_bbl
from latexprep.expanded_document import reachable_sources
from latexprep.source_graph import SourceGraph

from .support import ProjectTestCase


class BblKeysTests(unittest.TestCase):
    """Tests of ``bbl_keys`` and ``Backend`` on BibTeX and biblatex ``.bbl`` text."""

    def test_bibtex_bibitems_give_their_keys_with_or_without_a_label(self):
        text = (
            "\\begin{thebibliography}{9}\n"
            "\\bibitem{plain} A.\n"
            "\\bibitem [Smith {[}2020{]}] {spaced} B.\n"
            "\\bibitem[{Doe(2021)}]{braced} C.\n"
            "\\bibitem[Label]{ labelled } D.\n"
            "\\bibitemize is not a bibitem\n"
            "\\end{thebibliography}\n"
        )
        keys = bbl_keys(text)
        self.assertEqual(keys.keys, {"plain", "spaced", "braced", "labelled"})
        self.assertIs(keys.backend, Backend.BIBTEX)

    def test_biblatex_entries_and_aliases_give_their_keys(self):
        text = (
            "% $ biblatex auxiliary file $\n\\datalist[entry]{nty/global//global/global}\n"
            "  \\entry{knuth}{book}{}\n  \\entry {lamport}{book}{}\n"
            "  \\keyalias{old}{knuth}\n"
        )
        keys = bbl_keys(text)
        self.assertEqual(keys.keys, {"knuth", "lamport", "old"})
        self.assertIs(keys.backend, Backend.BIBLATEX)

    def test_the_backend_values_are_the_reported_strings_and_display_names(self):
        self.assertEqual((Backend.BIBTEX.value, Backend.BIBLATEX.value), ("bibtex", "biblatex"))
        self.assertEqual(
            (Backend.BIBTEX.display_name, Backend.BIBLATEX.display_name), ("BibTeX", "biblatex")
        )

    def test_commented_and_literal_commands_do_not_define_bbl_keys(self):
        bibtex = (
            "% \\datalist{} \\entry{comment}{article}{}\n"
            "\\bibitem[Label]{actual} \\verb|\\bibitem{verbatim}|\n"
            "\\\\bibitem{escaped}\n"
            "% \\bibitem{commented}\n"
        )
        self.assertEqual(bbl_keys(bibtex), ({"actual"}, Backend.BIBTEX))
        biblatex = (
            "\\datalist[entry]{test}\n"
            "\\entry{actual}{article}{}\n"
            "  \\verb{url}\n"
            "  \\entry{fieldtext}{article}{}\n"
            "  \\endverb\n"
            "% \\entry{commented}{article}{}\n"
            "% \\keyalias{commentedalias}{actual}\n"
            "\\entry{next}{article}{}\n"
            "\\keyalias{previousname}{actual}\n"
        )
        self.assertEqual(bbl_keys(biblatex), ({"actual", "next", "previousname"}, Backend.BIBLATEX))

    def test_a_large_bbl_is_read_in_linear_time(self):
        # Copying the rest of the text for every \bibitem would make four times the items
        # cost sixteen times as much, where a linear read costs four. The test compares the
        # processor time of two sizes instead of timing one, so a loaded machine slows both.
        def processor_seconds(items: int) -> float:
            text = "".join(f"\\bibitem{{key{index}}} Entry {index}.\n" for index in range(items))
            best = float("inf")
            for _ in range(3):
                started = time.process_time()
                keys = bbl_keys(text)
                best = min(best, time.process_time() - started)
                self.assertEqual(len(keys.keys), items)
            return best

        small = processor_seconds(5_000)
        large = processor_seconds(20_000)
        self.assertLess(large, 10 * max(small, 2e-4))


class ShippedBblTests(ProjectTestCase):
    """The readiness checks when the generated bibliography is shipped alone."""

    def test_a_shipped_bbl_is_checked_without_the_declared_bib_file(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\usepackage{biblatex}\n"
            "\\addbibresource{references.bib}\n"
            "\\begin{document}\\cite{actual,other}\\printbibliography\\end{document}",
        )
        header = "% $ biblatex auxiliary file $\n% $ biblatex bbl format version 3.2 $\n"
        settings = BblOptions(check_bbl_coverage=True, biblatex_bbl_format_version="3.3")
        for second_entry, expected in (
            ("% \\entry{other}{article}{}\n", "failed"),
            ("\\entry{other}{article}{}\n", "passed"),
        ):
            with self.subTest(coverage=expected):
                self.write("main.bbl", header + "\\entry{actual}{article}{}\n" + second_entry)
                graph = SourceGraph.build(self.root, "main.tex")
                findings = check_bbl(self.root, graph, reachable_sources(graph), settings)
                self.assertEqual(
                    {item.code: item.status for item in findings},
                    {"TEX009": "passed", "BIB109": expected, "BIB110": "failed"},
                )
                self.assertEqual(findings[2].details["found_version"], "3.2")


if __name__ == "__main__":
    unittest.main()
