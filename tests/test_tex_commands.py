from __future__ import annotations

import unittest

from latexprep.tex_commands import (
    CITATION_COMMANDS,
    COMMAND_DEFINITION_COMMANDS,
    DEFINITION_COMMANDS,
    MACRO_DEFINITION_COMMANDS,
    MANUSCRIPT_SUFFIXES,
    MATH_DELIMITER,
    MATH_MARKER,
    MULTICITE_COMMANDS,
    PRIMITIVE_DEFINITION_COMMANDS,
    PRINTED_CITATION_COMMANDS,
    ROOT_SUFFIXES,
    TEX_SOURCE_SUFFIXES,
    with_capitalized_forms,
)


class SuffixTests(unittest.TestCase):
    def test_each_suffix_set_extends_the_one_before(self) -> None:
        self.assertEqual(ROOT_SUFFIXES, {".tex", ".ltx", ".latex"})
        self.assertLess(ROOT_SUFFIXES, MANUSCRIPT_SUFFIXES)
        self.assertLess(MANUSCRIPT_SUFFIXES, TEX_SOURCE_SUFFIXES)

    def test_picture_sources_are_manuscript_text_but_packages_and_classes_are_not(self) -> None:
        self.assertLessEqual({".tikz", ".pgf"}, MANUSCRIPT_SUFFIXES)
        self.assertTrue(MANUSCRIPT_SUFFIXES.isdisjoint({".sty", ".cls", ".def", ".cfg"}))
        self.assertLessEqual({".sty", ".cls", ".def", ".cfg"}, TEX_SOURCE_SUFFIXES)

    def test_every_suffix_is_lower_case_with_a_dot(self) -> None:
        for suffix in TEX_SOURCE_SUFFIXES:
            self.assertRegex(suffix, r"^\.[a-z]+$")


class MathTests(unittest.TestCase):
    def test_delimiters_are_unescaped_dollars_and_the_four_control_symbols(self) -> None:
        for text in ("a $b", r"\(", r"\)", r"\[", r"\]"):
            with self.subTest(text=text):
                self.assertIsNotNone(MATH_DELIMITER.search(text))
        for text in (r"a \$5", "plain text", r"\begin{equation}"):
            with self.subTest(text=text):
                self.assertIsNone(MATH_DELIMITER.search(text))

    def test_the_marker_also_finds_math_environments_by_name_prefix(self) -> None:
        for text in (
            r"\begin{equation}",
            r"\begin{equation*}",
            r"\begin{align}",
            r"\begin{aligned}",
            r"\begin{math}",
            "$x$",
        ):
            with self.subTest(text=text):
                self.assertIsNotNone(MATH_MARKER.search(text))
        for text in (r"\begin{itemize}", r"\end{equation}", "no mathematics"):
            with self.subTest(text=text):
                self.assertIsNone(MATH_MARKER.search(text))


class DefinitionTests(unittest.TestCase):
    def test_primitive_and_command_definitions_are_disjoint_parts_of_the_macro_definitions(
        self,
    ) -> None:
        self.assertTrue(PRIMITIVE_DEFINITION_COMMANDS.isdisjoint(COMMAND_DEFINITION_COMMANDS))
        self.assertLessEqual(
            PRIMITIVE_DEFINITION_COMMANDS | COMMAND_DEFINITION_COMMANDS,
            MACRO_DEFINITION_COMMANDS,
        )

    def test_the_lexer_names_add_prefixes_and_newif_to_the_primitives(self) -> None:
        self.assertLess(PRIMITIVE_DEFINITION_COMMANDS, DEFINITION_COMMANDS)
        self.assertEqual(
            DEFINITION_COMMANDS - PRIMITIVE_DEFINITION_COMMANDS,
            {"newif", "global", "long", "protected"},
        )
        self.assertLess(DEFINITION_COMMANDS, MACRO_DEFINITION_COMMANDS)

    def test_the_xparse_declarations_count_as_command_definitions(self) -> None:
        self.assertLessEqual(
            {"newcommand", "DeclareRobustCommand", "NewDocumentCommand", "DeclareDocumentCommand"},
            COMMAND_DEFINITION_COMMANDS,
        )
        self.assertIn("newenvironment", MACRO_DEFINITION_COMMANDS)
        self.assertNotIn("newenvironment", COMMAND_DEFINITION_COMMANDS)


class CitationTests(unittest.TestCase):
    def test_nocite_adds_a_key_without_printing_a_citation(self) -> None:
        self.assertNotIn("nocite", PRINTED_CITATION_COMMANDS)
        self.assertEqual(CITATION_COMMANDS, PRINTED_CITATION_COMMANDS | {"nocite"})

    def test_multicite_commands_are_separate_from_single_key_group_citations(self) -> None:
        self.assertTrue(MULTICITE_COMMANDS.isdisjoint(CITATION_COMMANDS))
        self.assertIn("parencites", MULTICITE_COMMANDS)

    def test_canonical_names_are_lower_case(self) -> None:
        for name in CITATION_COMMANDS | MULTICITE_COMMANDS:
            self.assertEqual(name, name.lower())

    def test_capitalised_forms_keep_the_original_spellings(self) -> None:
        forms = with_capitalized_forms(["cite", "textcite"])
        self.assertEqual(forms, {"cite", "Cite", "textcite", "Textcite"})
        self.assertEqual(with_capitalized_forms([]), frozenset())


if __name__ == "__main__":
    unittest.main()
