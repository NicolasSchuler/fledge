"""The expanded document keeps where every span of its text came from."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from latexprep.expanded_document import EVERY_PART, DocumentPart, expand_document
from tests.support import ProjectTestCase, write_project

NESTED = {
    "main.tex": (
        "\\documentclass{article}\n"
        "\\begin{document}\n"
        "Intro % first comment\n"
        "\\verb|ab| \\input{part} \\input{other} tail % last comment\n"
        "\\end{document}\n"
    ),
    "part.tex": "Part one % comment\n\\input{inner}\nPart end\n",
    "inner.tex": "Inner \\verb|xy| text\n",
    "other.tex": "Other text",
}


class SourceSegmentTests(unittest.TestCase):
    """The segments of an expanded document and the source offsets they record."""

    def setUp(self) -> None:
        """Write the nested project ``NESTED`` into a temporary directory."""
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = write_project(Path(self.temporary.name), NESTED)

    def test_every_segment_names_its_offset_in_the_masked_source(self) -> None:
        document = expand_document(self.root, "main.tex")
        self.assertEqual({segment.path for segment in document.segments}, set(NESTED))
        for segment in document.segments:
            with self.subTest(path=segment.path, start=segment.start):
                masked = document.graph.sources[segment.path].masked
                length = segment.end - segment.start
                self.assertEqual(
                    masked[segment.source_start : segment.source_start + length],
                    document.text[segment.start : segment.end],
                )

    def test_text_between_inputs_is_copied_from_its_own_place_in_the_file(self) -> None:
        document = expand_document(self.root, "main.tex")
        main = [segment for segment in document.segments if segment.path == "main.tex"]
        raw = document.graph.sources["main.tex"].text
        pieces = [
            raw[item.source_start : item.source_start + item.end - item.start] for item in main
        ]
        self.assertEqual(
            pieces,
            [
                "\\documentclass{article}\n\\begin{document}\nIntro % first comment\n\\verb|ab| ",
                " ",
                " tail % last comment\n",
            ],
        )
        self.assertEqual([item.line for item in main], [1, 4, 4])
        # The same text is masked in the expanded document, so the comments are blanked there.
        self.assertNotIn("comment", document.text)

    def test_raw_and_masked_text_share_their_offsets(self) -> None:
        document = expand_document(self.root, "main.tex")
        for name, source in document.graph.sources.items():
            with self.subTest(path=name):
                self.assertEqual(len(source.text), len(source.masked))


class InclusionTests(ProjectTestCase):
    r"""Which ``\input`` forms are spliced, and which stay gaps of the document."""

    def test_a_group_after_an_input_is_text_that_follows_it(self) -> None:
        self.write("sec.tex", "\\section{Sec}\n")
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\begin{document}\n\\input{sec}\n"
            "{\n  \\small\n  Closing text.\n}\n\\input{sec}[htb]\n\\end{document}\n",
        )
        document = expand_document(self.root, "main.tex")
        self.assertEqual(document.graph_uncertainty, [])
        self.assertIn("\\section{Sec}\n\n{\n  \\small\n  Closing text.\n}", document.text)

    def test_an_input_in_a_typeset_argument_is_spliced_where_it_stands(self) -> None:
        self.write("pic.tex", "\\rule{1cm}{1cm}")
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\begin{document}\n"
            "\\resizebox{\\linewidth}{!}{\\input{pic}}{\\input{pic}}\n\\end{document}\n",
        )
        document = expand_document(self.root, "main.tex")
        self.assertEqual(document.graph_uncertainty, [])
        self.assertIn("{!}{\\rule{1cm}{1cm}}{\\rule{1cm}{1cm}}", document.text)

    def test_an_input_that_may_not_run_in_place_or_be_read_stays_a_gap(self) -> None:
        self.write("pic.tex", "\\rule{1cm}{1cm}")
        for body, reason in (
            ("\\newcommand{\\wrap}[1]{}\\wrap{\\input{pic}}", "nonliteral or grouped inclusion"),
            ("\\unknownmacro{\\input{pic}}", "nonliteral or grouped inclusion"),
            ("\\input{missing}", "missing from the project"),
            ("\\input[x]{pic}", "optional syntax precedes the file name"),
        ):
            with self.subTest(body=body):
                self.write(
                    "main.tex",
                    "\\documentclass{article}\n\\begin{document}\n" + body + "\n\\end{document}\n",
                )
                gaps = expand_document(self.root, "main.tex").graph_uncertainty
                self.assertTrue(any(reason in gap for gap in gaps), gaps)

    def test_no_reason_starts_with_a_missing_file_name(self) -> None:
        self.write("notes.tex", "Plain notes without a class.\n")
        gaps = expand_document(self.root, None).graph_uncertainty
        self.assertIn("No literal document root could be selected.", gaps)
        self.assertFalse([gap for gap in gaps if gap.startswith("None")])


class DocumentPartTests(ProjectTestCase):
    """Where a line of a project file runs: before the class, in the preamble or the body."""

    def setUp(self) -> None:
        """Write a main file with every part, an input in the body and a local package."""
        super().setUp()
        self.write("chapter.tex", "Chapter text.\n")
        self.write("local.sty", "\\ProvidesPackage{local}\n")
        self.write(
            "main.tex",
            "\\PassOptionsToClass{a4paper}{article}\n"  # 1
            "\\documentclass{article}\n"  # 2
            "\\usepackage{local}\n"  # 3
            "\\begin{document}\n"  # 4
            "\\input{chapter}\n"  # 5
            "\\end{document}\n"  # 6
            "After the end.\n",  # 7
        )
        self.document = expand_document(self.root, "main.tex")

    def test_lines_of_the_main_file_run_in_their_part(self) -> None:
        parts = {line: self.document.parts_at("main.tex", line) for line in (1, 3, 5, 7)}
        self.assertEqual(parts[1], {DocumentPart.PROLOGUE})
        self.assertEqual(parts[3], {DocumentPart.PREAMBLE})
        self.assertEqual(parts[5], {DocumentPart.BODY})
        self.assertEqual(parts[7], set())

    def test_files_run_where_they_are_loaded_or_included(self) -> None:
        self.assertEqual(self.document.parts_at("chapter.tex", 1), {DocumentPart.BODY})
        self.assertEqual(self.document.parts_at("local.sty", None), {DocumentPart.PREAMBLE})
        self.assertEqual(self.document.parts_at(None, None), EVERY_PART)

    def test_a_definition_runs_where_it_is_used(self) -> None:
        self.write(
            "main.tex",
            "\\documentclass{article}\n\\newcommand{\\used}{}\\newcommand{\\unused}{}\n"
            "\\newenvironment{box}{}{}\\let\\copy\\used\n"
            "\\begin{document}\n\\begin{box}\\end{box}\n\\end{document}\n",
        )
        document = expand_document(self.root, "main.tex")
        self.assertEqual(document.uses_of("unused"), set())
        self.assertEqual(document.uses_of("used"), {DocumentPart.PREAMBLE})
        self.assertEqual(document.uses_of("begin{box}"), {DocumentPart.BODY})


if __name__ == "__main__":
    unittest.main()
