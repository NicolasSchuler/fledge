"""Escaping unsafe characters for terminals, HTML, CI annotations and single-line text."""

from __future__ import annotations

import unittest

from latexprep.text_safety import (
    escape_annotation_text,
    escape_html_text,
    escape_single_line_text,
    is_unsafe_character,
    sanitize_terminal_text,
    visible_escape,
)

# One example of each kind of unsafe character: a C0 control, a C1 control, a bidi
# override (format), a lone surrogate (an undecodable file name), a private-use and
# an unassigned code point.
CONTROL = "\x07"
C1_CONTROL = "\u0085"
BIDI_OVERRIDE = "‮"
LONE_SURROGATE = "\udc80"
PRIVATE_USE = ""
UNASSIGNED = "\U000e0080"
UNSAFE_NON_CONTROLS = (BIDI_OVERRIDE, LONE_SURROGATE, PRIVATE_USE, UNASSIGNED)


class SharedDecisionTests(unittest.TestCase):
    """Which characters are unsafe, and the visible escapes that every output shares."""

    def test_every_other_category_is_unsafe_and_ordinary_text_is_not(self) -> None:
        for char in (CONTROL, C1_CONTROL, "\n", "\x7f", *UNSAFE_NON_CONTROLS):
            with self.subTest(char=char):
                self.assertTrue(is_unsafe_character(char))
        for char in ("a", " ", "é", "漢", "😀", " "):
            with self.subTest(char=char):
                self.assertFalse(is_unsafe_character(char))

    def test_escapes_use_four_digits_in_the_basic_plane_and_eight_beyond_it(self) -> None:
        self.assertEqual(visible_escape(BIDI_OVERRIDE), "\\u202e")
        self.assertEqual(visible_escape("\U000e0001"), "\\U000e0001")


class TerminalTests(unittest.TestCase):
    """Sanitising text for the terminal."""

    def test_escape_sequences_and_controls_are_removed_and_newlines_and_tabs_kept(self) -> None:
        text = "a\x1b[31mb\x1b]0;title\x07c\x00d\u0085e\x7ff\r\ng\th"
        self.assertEqual(sanitize_terminal_text(text), "abcdef\ng\th")

    def test_other_unsafe_characters_are_shown_not_dropped(self) -> None:
        self.assertEqual(
            sanitize_terminal_text("a" + "".join(UNSAFE_NON_CONTROLS)),
            "a\\u202e\\udc80\\ue000\\U000e0080",
        )

    def test_the_result_can_always_be_encoded(self) -> None:
        sanitize_terminal_text("figure\udc80.tex").encode("utf-8")


class HtmlTests(unittest.TestCase):
    """Escaping text for HTML."""

    def test_markup_is_escaped_and_newlines_and_tabs_are_kept(self) -> None:
        self.assertEqual(
            escape_html_text('<a href="x">&\'\n\t'), "&lt;a href=&quot;x&quot;&gt;&amp;&#x27;\n\t"
        )

    def test_controls_and_other_unsafe_characters_are_visible(self) -> None:
        self.assertEqual(
            escape_html_text(f"a{CONTROL}\r{C1_CONTROL}{BIDI_OVERRIDE}{LONE_SURROGATE}"),
            "a\\u0007\\u000d\\u0085\\u202e\\udc80",
        )

    def test_characters_beyond_the_basic_plane_are_not_ambiguous(self) -> None:
        self.assertEqual(escape_html_text("\U000e0001"), "\\U000e0001")


class AnnotationTests(unittest.TestCase):
    """Escaping the messages and properties of CI annotations."""

    def test_line_breaks_and_percent_cannot_start_another_command(self) -> None:
        self.assertEqual(
            escape_annotation_text("a%b\r\nc::error::x\x1b\t\x7f"),
            "a%25b%0D%0Ac::error::x%1B%09%7F",
        )

    def test_properties_also_encode_their_separators(self) -> None:
        self.assertEqual(escape_annotation_text("a:b,c", property_value=True), "a%3Ab%2Cc")
        self.assertEqual(escape_annotation_text("a:b,c"), "a:b,c")

    def test_other_unsafe_characters_are_visible(self) -> None:
        self.assertEqual(
            escape_annotation_text(f"{C1_CONTROL}{BIDI_OVERRIDE}{PRIVATE_USE}\U000e0001"),
            "\\u0085\\u202e\\ue000\\U000e0001",
        )


class SingleLineTests(unittest.TestCase):
    """Escaping text that must stay on one line."""

    def test_every_unsafe_character_including_line_breaks_is_visible(self) -> None:
        self.assertEqual(
            escape_single_line_text("a\nb\tc\rd" + CONTROL + BIDI_OVERRIDE + LONE_SURROGATE),
            "a\\u000ab\\u0009c\\u000dd\\u0007\\u202e\\udc80",
        )

    def test_ordinary_text_is_unchanged(self) -> None:
        self.assertEqual(escape_single_line_text("Résumé 漢字 😀"), "Résumé 漢字 😀")


if __name__ == "__main__":
    unittest.main()
