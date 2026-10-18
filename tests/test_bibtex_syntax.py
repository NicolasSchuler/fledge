"""The one BibTeX reader: records, offsets, errors, limits and the literal readers."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from latexprep.bibtex_syntax import (
    DOI_PATTERN,
    describe_read_failure,
    field_literal,
    mebibytes,
    parse_bibtex,
    plain_literal,
    read_doi,
)


def parse(text: str):
    return parse_bibtex("refs.bib", text)


def only_field(text: str):
    (entry,) = parse(f"@misc{{k, {text}}}").entries
    (item,) = entry.fields
    return item


class ParseTests(unittest.TestCase):
    def test_entries_declarations_and_comments_are_told_apart(self):
        document = parse(
            'free text\n@string{venue = "Journal"}\n@preamble{"\\\\x"}\n'
            "@comment{ignored {nested}}\n@Article{key, Title = {A} # venue, year = 2024}\n"
        )
        self.assertTrue(document.valid)
        kinds = [type(record).__name__ for record in document.records]
        self.assertEqual(kinds, ["Declaration", "Declaration", "Entry"])
        (entry,) = document.entries
        self.assertEqual((entry.entry_type, entry.key), ("article", "key"))
        self.assertEqual([item.name for item in entry.fields], ["title", "year"])
        self.assertEqual([atom.kind for atom in entry.fields[0].atoms], ["literal", "macro"])
        self.assertEqual(entry.fields[1].atoms[0].kind, "number")
        self.assertEqual(entry.fields_named("year"), [entry.fields[1]])

    def test_parentheses_delimit_entries_too(self):
        (entry,) = parse("@misc(key, title = {T})").entries
        self.assertEqual(entry.key, "key")

    def test_offsets_index_the_text_and_lines_count_from_one(self):
        document = parse("\n\n@misc{a}\n@misc{b}\n")
        first, second = document.entries
        self.assertEqual((document.line(first.start), document.line(second.start)), (3, 4))
        self.assertEqual(document.text[first.start : first.start + 5], "@misc")

    def test_a_syntax_error_stops_parsing_and_is_kept_with_its_offset(self):
        document = parse("@misc{a, title={ok}}\n@misc{b, title={open")
        self.assertFalse(document.valid)
        self.assertEqual([entry.key for entry in document.entries], ["a"])
        problem = document.syntax_error
        assert problem is not None
        self.assertEqual(problem.message, "Unterminated braced or quoted value.")
        self.assertEqual(document.line(problem.offset), 2)

    def test_each_kind_of_malformed_input_names_its_problem(self):
        cases = {
            "@": "Expected an entry type after '@'.",
            "@misc": "Expected '{' or '(' after the entry type.",
            "@misc{}": "Missing or unsupported citation key.",
            "@misc{k, = {x}}": "Expected a field or string-macro name.",
            "@misc{k, a b}": "Expected '='.",
            "@misc{k, a = }": "Expected a literal, number, or string macro.",
            "@misc{k, a = 1x}": "A value starting with a number must be quoted or braced.",
            "@string{a = {1}, b = {2}}": "An @string declaration must define one macro.",
            "@comment{open": "Unterminated @comment block.",
            '@misc{k, a = "x}"}': "Unmatched brace inside a quoted value.",
        }
        for text, message in cases.items():
            with self.subTest(text=text):
                problem = parse(text).syntax_error
                assert problem is not None
                self.assertEqual(problem.message, message)

    def test_nesting_and_entry_count_limits_are_errors_not_crashes(self):
        deep = "@misc{k, t={" + "{" * 300 + "x" + "}" * 301 + "}}"
        problem = parse(deep).syntax_error
        assert problem is not None
        self.assertIn("nesting", problem.message)
        with_limit = "@misc{a}\n" * 3
        with patch("latexprep.bibtex_syntax.MAX_BIB_ENTRIES", 2):
            problem = parse(with_limit).syntax_error
        assert problem is not None
        self.assertEqual(problem.message, "Entry count exceeds the supported limit.")

    def test_a_byte_order_mark_is_restored_in_the_file_text(self):
        document = parse_bibtex("refs.bib", "@misc{k}", bom=True)
        self.assertEqual(document.file_text, "﻿@misc{k}")
        self.assertEqual(document.text, "@misc{k}")


class LiteralTests(unittest.TestCase):
    def test_field_literal_reads_one_literal_or_number(self):
        self.assertEqual(field_literal(only_field("title = { A {B} }")), "A {B}")
        self.assertEqual(field_literal(only_field("year = 2024")), "2024")

    def test_field_literal_refuses_macros_concatenations_escapes_and_long_values(self):
        for text in ("a = macro", 'a = "x" # "y"', "a = {\\\\alpha}"):
            self.assertIsNone(field_literal(only_field(text)))
        self.assertEqual(
            len(field_literal(only_field("a = {" + "x" * 20 + "}"), max_chars=None) or ""), 20
        )

    def test_plain_literal_is_stricter_it_refuses_numbers_and_braces(self):
        self.assertEqual(plain_literal(only_field("crossref = { key }")), "key")
        for text in ("year = 2024", "a = {x {y}}", "a = macro"):
            self.assertIsNone(plain_literal(only_field(text)))


class DoiTests(unittest.TestCase):
    def test_the_shared_pattern_accepts_registrant_and_suffix_only(self):
        for good in ("10.1234/abc", "10.12345678/a.b-c_d", "10.1002/(SICI)1097:x"):
            self.assertIsNotNone(DOI_PATTERN.fullmatch(good), good)
        for bad in (
            "10.12/abc",
            "11.1234/abc",
            "10.1234/",
            "10.1234/a b",
            "10.1234/<x>",
            "10.1234/a{b}",
        ):
            self.assertIsNone(DOI_PATTERN.fullmatch(bad), bad)

    def test_read_doi_strips_a_supported_prefix_and_reports_its_span(self):
        text = "doi = {https://doi.org/10.1234/Abc}"
        reading = read_doi(only_field(text))
        self.assertEqual(reading.identifier, "10.1234/Abc")
        assert reading.prefix_span is not None
        start, end = reading.prefix_span
        self.assertEqual(parse(f"@misc{{k, {text}}}").text[start:end], "https://doi.org/")

    def test_read_doi_separates_an_invalid_doi_from_one_it_cannot_judge(self):
        invalid = read_doi(only_field("doi = {not a doi}"))
        self.assertEqual((invalid.identifier, invalid.uncertainty), (None, None))
        for text in (
            "doi = macro",
            "doi = {10.1234/\\\\x}",
            "doi = {https://doi.org/10.1234/x?y=1}",
        ):
            self.assertIsNotNone(read_doi(only_field(text)).uncertainty, text)


class MessageHelperTests(unittest.TestCase):
    def test_limits_are_quoted_in_whole_mebibytes(self):
        self.assertEqual(mebibytes(16 * 1024 * 1024), "16 MiB")

    def test_read_failures_read_as_a_phrase_without_codec_jargon(self):
        error = UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid start byte")
        self.assertEqual(
            describe_read_failure(error), "is not valid UTF-8 (first invalid byte at offset 0)"
        )
        self.assertEqual(
            describe_read_failure(PermissionError(13, "Permission denied")),
            "could not be read (Permission denied)",
        )
        self.assertEqual(describe_read_failure(OSError()), "could not be read (OSError)")


if __name__ == "__main__":
    unittest.main()
