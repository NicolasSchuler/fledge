from __future__ import annotations

import unittest

from latexprep.tex_lexer import (
    conditional_spans,
    in_closed_span,
    in_conditional,
    in_half_open_span,
    mask_literals,
    merge_spans,
    scan_commands,
    source_regions,
    split_argument_items,
)


class TexLexerTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
