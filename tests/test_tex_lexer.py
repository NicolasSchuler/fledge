"""The bounded TeX lexer: spans, conditionals, source regions, arguments and parse limits."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from latexprep.tex_lexer import (
    MAX_ARGUMENT_CHARS,
    ParseLimitExceeded,
    conditional_spans,
    in_closed_span,
    in_conditional,
    in_half_open_span,
    mask_literals,
    merge_spans,
    scan_commands,
    scan_source_commands,
    source_regions,
    split_argument_items,
)


class TexLexerTests(unittest.TestCase):
    """Spans, conditionals, source regions, argument scanning and the bounds of the lexer."""

    def test_span_tests_differ_only_at_span_boundaries(self) -> None:
        spans = merge_spans([(10, 20), (20, 25), (30, 40), (32, 35)])
        self.assertEqual(spans, [(10, 25), (30, 40)])
        for offset, strict, half_open, closed in (
            (9, False, False, False),
            (10, False, True, True),
            (15, True, True, True),
            (25, False, False, True),
            (26, False, False, False),
        ):
            with self.subTest(offset=offset):
                self.assertEqual(in_conditional(spans, offset), strict)
                self.assertEqual(in_half_open_span(spans, offset), half_open)
                self.assertEqual(in_closed_span(spans, offset), closed)

    def test_conditional_spans_exclude_their_opening_and_closing_tokens(self) -> None:
        text = "\\newif\\ifdraft\n\\ifdraft\\input{a}\\fi\n\\iftoggle{x}{y}{z}\n"
        commands = scan_commands(mask_literals(text))
        spans = conditional_spans(commands)
        located = {command.name: command.start for command in commands}
        self.assertEqual(spans, [(text.index("\\ifdraft\\input"), located["fi"])])
        self.assertTrue(in_conditional(spans, located["input"]))
        self.assertFalse(in_conditional(spans, located["fi"]))

    def test_environment_tests_distinguish_spaced_names_only_when_exact(self) -> None:
        begin, end = scan_commands("\\begin{ document }\\end{document}")
        self.assertTrue(begin.begins("document"))
        self.assertFalse(begin.begins("document", exact=True))
        self.assertFalse(begin.ends("document"))
        self.assertTrue(end.ends("document", exact=True))
        self.assertEqual(begin.name_end, len("\\begin"))

    def test_regions_keep_carriage_returns_outside_comments(self) -> None:
        text = "a % note\r\n\\verb|x%y| b\n\\verb|open\n"
        regions, unclosed = source_regions(text)
        self.assertEqual(
            [(text[region.start : region.end], region.kind) for region in regions],
            [("% note", "comment"), ("\\verb|x%y|", "literal"), ("\\verb|open", "literal")],
        )
        self.assertEqual(unclosed, [text.rindex("\\verb")])

    def test_argument_items_keep_source_offsets(self) -> None:
        text = "\\cite{ a , bc }"
        (command,) = scan_commands(text)
        items = split_argument_items(command.arguments[0])
        self.assertEqual([item.value for item in items], ["a", "bc"])
        self.assertEqual([text[item.start : item.end] for item in items], ["a", "bc"])
        (whole,) = split_argument_items(command.arguments[0], comma_separated=False)
        self.assertEqual(text[whole.start : whole.end], "a , bc")

    def test_tikz_path_is_markup_and_only_the_url_forms_are_literal(self) -> None:
        for text in (
            "\\path[fill] (0,0) -- (1,1);",
            "\\path (0,0) circle (1);",
            "\\begin{tikzpicture}\n\\path[draw] (0,0)\n  -- (1,1);\n\\end{tikzpicture}",
        ):
            with self.subTest(text=text):
                self.assertEqual(source_regions(text), ([], []))
        text = "a \\path{a%b} b \\path|c d| e"
        regions, unclosed = source_regions(text)
        self.assertEqual(
            [text[region.start : region.end] for region in regions],
            ["\\path{a%b}", "\\path|c d|"],
        )
        self.assertEqual(unclosed, [])
        commands = scan_source_commands("\\path[fill] (0,0) -- (1,1); \\node{x}")
        self.assertEqual([command.name for command in commands], ["path", "node"])

    def test_a_line_holding_only_a_comment_does_not_end_argument_scanning(self) -> None:
        text = (
            "\\input\n% note\n{file}\n\\includegraphics[width=3cm]\n  % one\n  % two\n  {figure}\n"
        )
        input_command, graphics = scan_source_commands(text)
        self.assertEqual([item.value for item in input_command.arguments], ["file"])
        self.assertEqual([item.value for item in graphics.arguments], ["figure"])
        self.assertEqual([item.value for item in graphics.options], ["width=3cm"])

    def test_a_real_blank_line_still_ends_argument_scanning(self) -> None:
        for text in (
            "\\foo\n\n{text}",
            "\\foo\n  \n{text}",
            "\\foo\n\n% note\n{text}",
            "\\foo\n% note\n\n{text}",
        ):
            with self.subTest(text=text):
                (command,) = scan_source_commands(text)
                self.assertEqual(command.arguments, ())
        # Callers without comment offsets keep the earlier reading: any blank-looking line ends it.
        (command,) = scan_commands(mask_literals("\\foo\n% note\n{text}"))
        self.assertEqual(command.arguments, ())

    def test_exceeding_a_bound_reports_the_reason_and_offset(self) -> None:
        with self.assertRaises(ParseLimitExceeded) as unclosed:
            scan_source_commands("Intro.\n\\foo{open")
        self.assertEqual(
            (unclosed.exception.reason, unclosed.exception.offset), ("unclosed_group", 11)
        )
        with self.assertRaises(ParseLimitExceeded) as oversized:
            scan_source_commands("\\foo{" + "x" * (MAX_ARGUMENT_CHARS + 1) + "}")
        self.assertEqual(
            (oversized.exception.reason, oversized.exception.offset), ("oversized_group", 4)
        )
        self.assertIn(f"{MAX_ARGUMENT_CHARS // 1024} KiB", str(oversized.exception))

    def test_more_commands_than_the_bound_raise_instead_of_returning_a_prefix(self) -> None:
        text = "\\a \\b \\c \\d \\e"
        with patch("latexprep.tex_lexer.MAX_COMMANDS", 3):
            self.assertEqual(len(scan_source_commands("\\a \\b \\c")), 3)
            with self.assertRaises(ParseLimitExceeded) as error:
                scan_source_commands(text)
        self.assertEqual((error.exception.reason, error.exception.offset), ("command_count", 9))


if __name__ == "__main__":
    unittest.main()
