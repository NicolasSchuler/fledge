"""The expanded document keeps where every span of its text came from."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from latexprep.expanded_document import expand_document
from tests.support import write_project

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


if __name__ == "__main__":
    unittest.main()
