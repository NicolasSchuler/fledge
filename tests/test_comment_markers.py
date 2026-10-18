from __future__ import annotations

import unittest

from latexprep.comment_markers import MarkerCategory, marker_pattern, matching_category


class MarkerPatternTests(unittest.TestCase):
    def test_edit_markers_are_whole_words_and_uncertainty_marks_are_double_question_marks(
        self,
    ) -> None:
        pattern = marker_pattern(MarkerCategory.EDIT_MARKER, MarkerCategory.UNCERTAINTY_MARK)
        for text in ("TODO: cite", "a FIXME here", "XXX", "really ??", "todo", "Fixme"):
            with self.subTest(text=text):
                self.assertIsNotNone(pattern.search(text))
        for text in ("TODOS", "boxxxed", "a single ? mark", "TBD"):
            with self.subTest(text=text):
                self.assertIsNone(pattern.search(text))

    def test_case_sensitive_matching_leaves_ordinary_lowercase_words_alone(self) -> None:
        pattern = marker_pattern(MarkerCategory.EDIT_MARKER, ignore_case=False)
        self.assertIsNotNone(pattern.search("TODO"))
        self.assertIsNone(pattern.search("Todo lo demas, todo"))

    def test_to_be_determined_is_its_own_category(self) -> None:
        self.assertIsNotNone(marker_pattern(MarkerCategory.TO_BE_DETERMINED).search("tbd"))
        self.assertIsNone(marker_pattern(MarkerCategory.EDIT_MARKER).search("tbd"))

    def test_privacy_keywords_are_single_words_wider_than_the_private_note_phrases(self) -> None:
        keywords = marker_pattern(MarkerCategory.PRIVACY_KEYWORD)
        notes = marker_pattern(MarkerCategory.PRIVATE_NOTE)
        for text in ("private", "an internal detail", "Confidential", "note to self"):
            with self.subTest(text=text):
                self.assertIsNotNone(keywords.search(text))
        self.assertIsNone(notes.search("an internal detail"))
        self.assertIsNotNone(notes.search("a private note"))
        self.assertIsNotNone(notes.search("Note to co-authors: check"))

    def test_review_correspondence_names_a_reviewer_or_the_rebuttal(self) -> None:
        pattern = marker_pattern(MarkerCategory.REVIEW_CORRESPONDENCE)
        for text in ("Reviewer 2 wants this", "reply to the referees", "for the rebuttal"):
            with self.subTest(text=text):
                self.assertIsNotNone(pattern.search(text))
        self.assertIsNone(pattern.search("the reviewer experience of the user"))

    def test_distribution_restrictions_ignore_confidential_computing(self) -> None:
        pattern = marker_pattern(MarkerCategory.DISTRIBUTION_RESTRICTION)
        for text in ("do not submit", "Don't share", "not for distribution", "internal use only"):
            with self.subTest(text=text):
                self.assertIsNotNone(pattern.search(text))
        self.assertIsNotNone(pattern.search("strictly confidential"))
        self.assertIsNone(pattern.search("confidential computing enclave"))

    def test_a_pattern_over_several_categories_matches_any_of_them(self) -> None:
        pattern = marker_pattern(MarkerCategory.EDIT_MARKER, MarkerCategory.PRIVATE_NOTE)
        self.assertIsNotNone(pattern.search("TODO"))
        self.assertIsNotNone(pattern.search("private comment"))
        self.assertIsNone(pattern.search("do not submit"))


class MatchingCategoryTests(unittest.TestCase):
    CATEGORIES = (
        MarkerCategory.REVIEW_CORRESPONDENCE,
        MarkerCategory.DISTRIBUTION_RESTRICTION,
        MarkerCategory.PRIVATE_NOTE,
    )

    def test_the_first_category_in_the_given_order_wins(self) -> None:
        text = "private note: do not submit; reviewer 2 asked"
        self.assertIs(
            matching_category(text, self.CATEGORIES), MarkerCategory.REVIEW_CORRESPONDENCE
        )
        self.assertIs(
            matching_category(text, tuple(reversed(self.CATEGORIES))), MarkerCategory.PRIVATE_NOTE
        )

    def test_no_marker_gives_none(self) -> None:
        self.assertIsNone(matching_category("an ordinary remark", self.CATEGORIES))

    def test_category_values_are_the_reported_labels(self) -> None:
        self.assertEqual(
            [category.value for category in self.CATEGORIES],
            ["review correspondence", "distribution restriction", "private note"],
        )


if __name__ == "__main__":
    unittest.main()
