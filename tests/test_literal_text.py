from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from latexprep.literal_text import is_placeholder_text, plain_argument_text, scan_literal_text
from latexprep.tex_lexer import scan_commands

# Enough escapes after a stray dollar sign to make a backtracking pattern take seconds
# (it doubles per escape pair), while a linear scan needs well under a millisecond.
STRAY_DOLLAR_REPEATS = 12
LINEAR_TIME_LIMIT_SECONDS = 1.0


class LiteralTextTests(unittest.TestCase):
    def test_a_stray_dollar_before_many_commands_is_scanned_in_linear_time(self) -> None:
        value = "The price is 5$ and see " + "text \\cite{a} \\emph{b} " * STRAY_DOLLAR_REPEATS
        started = time.perf_counter()
        result = scan_literal_text(value)
        self.assertLess(time.perf_counter() - started, LINEAR_TIME_LIMIT_SECONDS)
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
