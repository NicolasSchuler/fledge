"""Literal text of LaTeX arguments: math, citations, references, placeholders and bounds."""

from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from latexprep.literal_text import is_placeholder_text, plain_argument_text, scan_literal_text
from latexprep.tex_lexer import scan_commands

# A backtracking pattern after a stray dollar sign doubles its work for each escape pair, so
# twice the repeats cost a thousand times the work, where a linear scan costs twice. The test
# compares the processor time of two sizes instead of timing one: a loaded machine slows both.
SMALL_REPEATS = 6
LARGE_REPEATS = 12
# Linear growth gives about 2, noise a few times more, and backtracking about 64.
MAX_GROWTH_FACTOR = 20
# Measurements below this are noise of the clock, whatever the machine does.
MIN_MEASURED_SECONDS = 2e-4


def stray_dollar_value(repeats: int) -> str:
    """Return text with a stray dollar sign followed by ``repeats`` commands and plain words."""
    return "The price is 5$ and see " + "text \\cite{a} \\emph{b} " * repeats


def processor_seconds(value: str) -> float:
    """Return the least processor time of three scans of ``value``."""
    best = float("inf")
    for _ in range(3):
        started = time.process_time()
        scan_literal_text(value)
        best = min(best, time.process_time() - started)
    return best


class LiteralTextTests(unittest.TestCase):
    """:func:`scan_literal_text`, :func:`plain_argument_text` and :func:`is_placeholder_text`."""

    def test_a_stray_dollar_before_many_commands_is_scanned_in_linear_time(self) -> None:
        small = processor_seconds(stray_dollar_value(SMALL_REPEATS))
        large = processor_seconds(stray_dollar_value(LARGE_REPEATS))
        self.assertLess(large, MAX_GROWTH_FACTOR * max(small, MIN_MEASURED_SECONDS))
        result = scan_literal_text(stray_dollar_value(LARGE_REPEATS))
        self.assertIn("unbalanced math delimiters", result.unresolved)
        self.assertIn("text", result.text)

    def test_inline_math_is_dropped_including_escaped_dollars_and_backslashes(self) -> None:
        for source, expected in (
            ("a $x^2$ b", "a   b"),
            ("a $5\\$ off$ b", "a   b"),
            ("a $x\\\\$ b", "a   b"),
            ("a $x$ b $y$ c", "a   b   c"),
            ("a $$x$$ b", "a   b"),
        ):
            with self.subTest(source=source):
                result = scan_literal_text(source)
                self.assertEqual(result.text, expected)
                self.assertEqual(result.unresolved, [])

    def test_more_commands_than_the_lexer_bound_is_uncertain_not_partly_interpreted(self) -> None:
        with patch("latexprep.tex_lexer.MAX_COMMANDS", 3):
            result = scan_literal_text("\\emph{a} " * 10)
        self.assertEqual(result.text, "")
        self.assertEqual(result.unresolved_macros, 1)
        self.assertEqual(len(result.unresolved), 1)
        self.assertIn("more commands", result.unresolved[0])

    def test_an_unclosed_argument_is_uncertain(self) -> None:
        result = scan_literal_text("text \\emph{open")
        self.assertEqual(result.unresolved, ["unclosed or oversized argument"])

    def test_every_citation_command_and_its_capitalised_twin_holds_only_keys(self) -> None:
        for command in ("citep", "citealp", "Citet", "Textcite", "footfullcite", "nocite"):
            with self.subTest(command=command):
                result = scan_literal_text(f"before \\{command}{{smith2020}} after")
                self.assertEqual(result.unresolved, [])
                self.assertNotIn("smith2020", result.text)

    def test_references_that_print_the_targets_text_stay_unresolved(self) -> None:
        for command in ("nameref", "vref"):
            with self.subTest(command=command):
                self.assertEqual(
                    scan_literal_text(f"\\{command}{{sec:a}}").unresolved, [f"\\{command}"]
                )

    def test_math_alone_is_reported_as_only_mathematical_content_even_in_an_environment(
        self,
    ) -> None:
        for raw in (r"\(x\)", r"$x$", r"\begin{equation}x\end{equation}"):
            with self.subTest(raw=raw):
                command = scan_commands(r"\author{" + raw + "}")[0]
                self.assertIn(
                    "only mathematical content is observable", plain_argument_text(command)[1]
                )

    def test_placeholder_markers_come_from_the_shared_marker_vocabulary(self) -> None:
        for value in ("TODO", "todo: name", "TBD", "tbd.", "FIXME", "name ??", "<xxx>"):
            with self.subTest(value=value):
                self.assertTrue(is_placeholder_text(value))
        for value in ("Ada Lovelace", "Todos Santos", "private note"):
            with self.subTest(value=value):
                self.assertFalse(is_placeholder_text(value))


if __name__ == "__main__":
    unittest.main()
