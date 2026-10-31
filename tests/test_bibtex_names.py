"""Family names of BibTeX author lists, read with BibTeX's own name syntax."""

from __future__ import annotations

import unittest

from latexprep.bibtex_names import FamilyName, parse_author_list, without_particles


def families(text: str) -> list[str] | None:
    """Return the full family names that ``parse_author_list`` reads, or ``None``."""
    parsed = parse_author_list(text)
    return None if parsed is None else [name.full for name in parsed.names]


class ParseAuthorListTests(unittest.TestCase):
    """Tests of the family names, particles and truncation that ``parse_author_list`` reads."""

    def test_both_name_orders_give_the_family_name(self):
        self.assertEqual(families("Ada Smith and Jones, Bo"), ["Smith", "Jones"])
        self.assertEqual(families("Smith, Jr., Ada"), ["Smith"])

    def test_lowercase_particles_belong_to_the_family_name(self):
        self.assertEqual(families("Jan van der Berg"), ["van der Berg"])
        self.assertEqual(families("van der Berg, Jan"), ["van der Berg"])
        self.assertEqual(families("Berg, Jan van der"), ["Berg"])
        self.assertEqual(
            families("Ludwig van Beethoven and Charles de Gaulle"), ["van Beethoven", "de Gaulle"]
        )

    def test_the_last_name_is_kept_without_its_particles(self):
        parsed = parse_author_list("Jan van der Berg")
        assert parsed is not None
        self.assertEqual(parsed.names, [FamilyName("van der Berg", "Berg")])

    def test_a_braced_group_is_one_word(self):
        self.assertEqual(families("{Barnes and Noble}, Ada"), ["Barnes and Noble"])
        self.assertEqual(families("Ada {van der Berg}"), ["van der Berg"])

    def test_and_others_marks_a_truncated_list_and_is_not_a_name(self):
        parsed = parse_author_list("Smith, Ada and Jones, Bo and others")
        assert parsed is not None
        self.assertEqual(
            ([name.full for name in parsed.names], parsed.truncated), (["Smith", "Jones"], True)
        )
        self.assertFalse(parse_author_list("Smith, Ada and Jones, Bo").truncated)  # type: ignore[union-attr]
        # BibTeX reads "AND" like "and".
        self.assertEqual(families("Ada Smith AND Bo Jones"), ["Smith", "Jones"])

    def test_text_without_a_readable_name_gives_none(self):
        for text in ("", "   ", "and", "Smith and and Jones", ", Ada"):
            with self.subTest(text=text):
                self.assertIsNone(families(text))


class SpellingTests(unittest.TestCase):
    """Tests of ``FamilyName.spellings`` and ``without_particles``."""

    def test_a_family_name_offers_every_spelling_another_source_may_use(self):
        name = FamilyName("van der Berg", "Berg")
        self.assertEqual(name.spellings(), ("van der Berg", "Berg", "Berg"))

    def test_only_leading_lowercase_words_are_particles(self):
        self.assertEqual(without_particles("van der Berg"), "Berg")
        self.assertEqual(without_particles("de la Cruz Jr"), "Cruz Jr")
        self.assertEqual(without_particles("van"), "van")
        self.assertEqual(without_particles("Smith"), "Smith")


if __name__ == "__main__":
    unittest.main()
