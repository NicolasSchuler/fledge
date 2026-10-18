"""Which entries and links the online checks would send, and the gaps in what they could read."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from latexprep.bibtex_syntax import BibFileReader, mebibytes
from latexprep.errors import PreparationError
from latexprep.limits import MIB
from latexprep.online_inputs import MAX_BIB_BYTES, MAX_INPUT_BYTES, collect_inputs

from .online_support import options


def manuscript_text(body: str) -> str:
    return (
        "\\documentclass{article}\n\\begin{document}\n"
        + body
        + "\n\\bibliography{references}\n\\end{document}\n"
    )


class CollectInputsTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.bib = self.root / "references.bib"
        self.manuscript()

    def manuscript(self, body: str = ""):
        (self.root / "main.tex").write_text(manuscript_text(body))

    def collect(self, **changes):
        return collect_inputs(self.root, "main.tex", options(online_metadata=True, **changes))

    def test_entries_keep_only_the_literal_values_a_lookup_needs(self):
        self.bib.write_text(
            "@article{key, title={A {Title}}, author={Smith, Ada}, doi={10.1234/x}, year=2024,"
            " note=macro, journal={Name} # { Two}}\n"
        )
        (reference,) = self.collect().references
        self.assertEqual(
            (reference.key, reference.path, reference.line), ("key", "references.bib", 1)
        )
        self.assertEqual(reference.values["title"], "A Title")
        self.assertEqual(reference.author_text, "Smith, Ada")
        self.assertEqual(reference.doi, "10.1234/x")
        # A macro or concatenation is not a literal; its value is unknown, not guessed.
        self.assertIsNone(reference.values["note"])
        self.assertIsNone(reference.values["journal"])

    def test_a_repeated_field_becomes_unknown(self):
        self.bib.write_text(
            "@misc{key, doi={10.1234/a}, doi={10.1234/b}, author={A}, author={B}}\n"
        )
        (reference,) = self.collect().references
        self.assertEqual(
            (reference.doi, reference.values["doi"], reference.author_text), (None, None, None)
        )

    def test_links_come_from_configured_urls_and_literal_commands_not_comments(self):
        self.manuscript(
            "\\url{https://a.example.org/} \\nolinkurl{https://b.example.org/} "
            "\\href{https://c.example.org/}{text}\n"
            "% \\url{https://hidden.example.org/}\n"
            "\\url{\\indirect} \\verb|\\url{https://verb.example.org/}|"
        )
        inputs = collect_inputs(
            self.root,
            "main.tex",
            options(
                online_replication_links=True, online_replication_urls=("https://d.example.org/",)
            ),
        )
        self.assertEqual(
            [link.values["url"] for link in inputs.links],
            [
                "https://d.example.org/",
                "https://a.example.org/",
                "https://b.example.org/",
                "https://c.example.org/",
            ],
        )
        self.assertEqual(
            [(link.key, link.path) for link in inputs.links[:2]],
            [("configured link 1", None), ("source link", "main.tex")],
        )
        self.assertEqual(
            inputs.gaps, ["main.tex: an indirect or grouped URL command was not requested"]
        )

    def test_source_link_scan_reports_sorted_distinct_gaps_when_the_bound_is_hit(self):
        self.manuscript(
            "\\url{\\first}\n\\url{\\second}\n\\url{https://a.example.org/}\n"
            "\\url{https://b.example.org/}\n\\url{https://c.example.org/}"
        )
        with patch("latexprep.online_inputs.MAX_REFERENCES", 2):
            _, links, gaps = collect_inputs(
                self.root, "main.tex", options(online_replication_links=True)
            )
        self.assertEqual(len(links), 2)
        self.assertIn("Source-link scan exceeded its 2-link bound", gaps)
        self.assertEqual(gaps, sorted(set(gaps)))

    def test_limit_messages_are_built_from_the_limit_constants(self):
        (self.root / "references.bib").write_bytes(b"%" * (MAX_BIB_BYTES + 1))
        _, _, gaps = self.collect()
        self.assertEqual(
            gaps,
            [
                "references.bib exceeds the bibliography byte budget "
                f"({mebibytes(MAX_BIB_BYTES)} per file, {mebibytes(MAX_INPUT_BYTES)} in total)"
            ],
        )
        self.assertEqual((MAX_BIB_BYTES, MAX_INPUT_BYTES), (2 * MIB, 16 * MIB))
        self.bib.write_text("@misc{one, doi={10.1234/a}}\n@misc{two, doi={10.1234/b}}\n")
        with patch("latexprep.online_inputs.MAX_REFERENCES", 1):
            _, _, gaps = self.collect()
        self.assertEqual(gaps, ["Online bibliography scan exceeded its 1-entry bound"])

    def test_unreadable_bibliographies_become_gaps_that_name_the_file_and_cause(self):
        self.bib.write_bytes(b"@misc{k, title={\xff}}")
        self.assertEqual(
            self.collect().gaps,
            ["references.bib is not valid UTF-8 (first invalid byte at offset 16)"],
        )
        self.bib.write_text("@misc{k}\n")
        with patch.object(
            BibFileReader, "read", side_effect=PermissionError(13, "Permission denied")
        ):
            self.assertEqual(
                self.collect().gaps, ["references.bib could not be read (Permission denied)"]
            )

    def test_a_syntax_error_makes_the_whole_file_a_gap(self):
        self.bib.write_text("@misc{k, title={open")
        inputs = self.collect()
        self.assertEqual(inputs.references, [])
        self.assertEqual(inputs.gaps, ["references.bib: bibliography syntax is incomplete"])

    def test_a_linked_bibliography_is_not_read(self):
        (self.root / "main.tex").unlink()
        target = self.root / "target.txt"
        target.write_text("@misc{k}\n")
        self.bib.symlink_to(target)
        inputs = self.collect()
        self.assertEqual(inputs.references, [])
        self.assertEqual(
            inputs.gaps,
            ["references.bib is, or lies behind, a symbolic link, which is not supported"],
        )

    def test_a_missing_input_directory_uses_the_shared_wording(self):
        self.bib.write_text("@misc{k}\n")
        with self.assertRaises(PreparationError) as raised:
            collect_inputs(self.bib, "main.tex", options(online_metadata=True))
        self.assertTrue(
            str(raised.exception).startswith("Online reference input must be a directory, but ")
        )

    def test_a_directory_without_a_document_uses_every_bibliography_in_it(self):
        (self.root / "main.tex").unlink()
        (self.root / "old").mkdir()
        (self.root / "old" / "backup.bib").write_text("@misc{spare}\n")
        self.bib.write_text("@misc{key}\n")
        self.assertEqual(
            sorted(reference.path for reference in self.collect().references),
            ["old/backup.bib", "references.bib"],
        )


if __name__ == "__main__":
    unittest.main()
