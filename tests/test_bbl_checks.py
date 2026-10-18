"""Keys read out of a generated .bbl, in BibTeX's and biblatex's forms."""

from __future__ import annotations

import time
import unittest

from latexprep.bbl_checks import Backend, bbl_keys


class BblKeysTests(unittest.TestCase):
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

    def test_a_large_bbl_is_read_in_linear_time(self):
        text = "".join(f"\\bibitem{{key{index}}} Entry {index}.\n" for index in range(20_000))
        started = time.monotonic()
        keys = bbl_keys(text)
        self.assertEqual(len(keys.keys), 20_000)
        # Copying the rest of the text for every \bibitem would take minutes here.
        self.assertLess(time.monotonic() - started, 5)


if __name__ == "__main__":
    unittest.main()
