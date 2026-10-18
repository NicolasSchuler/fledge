from __future__ import annotations

import time
import unittest
from unittest.mock import patch

from latexprep.literal_text import scan_literal_text

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


if __name__ == "__main__":
    unittest.main()
