"""Words that mark unfinished work or a private note in manuscript text and comments.

Five readers look for such markers: the active-text lint, the comment policy of the
source transformations, the opt-in private-comment scan, the default private-note
advisory and the placeholder test on declared values. They share one vocabulary,
split into named categories, and each reader selects the categories it needs.

``EDIT_MARKER``, ``TO_BE_DETERMINED`` and ``UNCERTAINTY_MARK``
    Unfinished text: ``TODO``, ``FIXME`` and ``XXX``; ``TBD``; and ``??``.
``REVIEW_CORRESPONDENCE``, ``DISTRIBUTION_RESTRICTION`` and ``PRIVATE_NOTE``
    Narrow phrases that show a comment was written for the authors or reviewers
    rather than for readers: "reviewer 2", "do not submit", "note to self".
``PRIVACY_KEYWORD``
    Single words (``private``, ``internal``, ``confidential``) that make a comment a
    candidate for removal. They are far wider than the phrases above, which is why
    only the comment-removal policy uses them.

Matching is case-insensitive by default, because comments are free-form notes.
Text that is part of the manuscript itself is matched case-sensitively instead, so
that ordinary words such as the Spanish "todo" are not reported as markers; only the
unfinished-text categories are meant for that mode.
"""

from __future__ import annotations

import re
from enum import StrEnum
from functools import cache


class MarkerCategory(StrEnum):
    """One kind of marker; the value is the label reported for it."""

    EDIT_MARKER = "edit marker"
    TO_BE_DETERMINED = "to be determined"
    UNCERTAINTY_MARK = "uncertainty mark"
    REVIEW_CORRESPONDENCE = "review correspondence"
    DISTRIBUTION_RESTRICTION = "distribution restriction"
    PRIVATE_NOTE = "private note"
    PRIVACY_KEYWORD = "privacy keyword"


# Regular-expression source for each category, written for case-insensitive matching
# except where the capital letters are the convention (``TODO``).
_CATEGORY_SOURCES: dict[MarkerCategory, str] = {
    MarkerCategory.EDIT_MARKER: r"\b(?:TODO|FIXME|XXX)\b",
    MarkerCategory.TO_BE_DETERMINED: r"\bTBD\b",
    MarkerCategory.UNCERTAINTY_MARK: r"\?\?",
    MarkerCategory.REVIEW_CORRESPONDENCE: (
        r"\b(?:reviewers?|referees?)\s*#\s*\d+\b|\b(?:reviewer|referee)\s+\d+\b|"
        r"\b(?:responses?|repl(?:y|ies)|answers?)\s+(?:to|for)\s+(?:the\s+)?"
        r"(?:reviewers?|referees?|editors?)\b|"
        r"\b(?:requested|asked|suggested|raised|demanded)\s+by\s+(?:the\s+)?"
        r"(?:reviewers?|referees?|editors?|meta-?reviewers?)\b|"
        r"\b(?:for|in)\s+(?:the|our)\s+rebuttal\b"
    ),
    MarkerCategory.DISTRIBUTION_RESTRICTION: (
        r"\b(?:do\s+not|don['’]?t|never)\s+"
        r"(?:submit|share|distribute|publish|circulate|upload|release)\b|"
        r"\bnot\s+for\s+(?:distribution|publication|submission|circulation)\b|"
        r"\b(?:internal|private)\s+(?:use\s+)?only\b|"
        r"\bconfidential\b(?!\s+(?:comput|transaction|virtual|vms?\b|container|inference|"
        r"data|information|learning|ml\b|ai\b|channel|cloud|enclave))"
    ),
    MarkerCategory.PRIVATE_NOTE: (
        r"\b(?:private|personal)\s+(?:notes?|comments?|remarks?)\b|"
        r"\bnote\s+to\s+(?:self|myself|ourselves|co-?authors?)\b"
    ),
    MarkerCategory.PRIVACY_KEYWORD: r"\b(?:private|internal|confidential|note to self)\b",
}


@cache
def marker_pattern(*categories: MarkerCategory, ignore_case: bool = True) -> re.Pattern[str]:
    """One pattern that matches any marker of the given categories."""
    flags = re.IGNORECASE if ignore_case else 0
    return re.compile("|".join(_CATEGORY_SOURCES[category] for category in categories), flags)


def matching_category(text: str, categories: tuple[MarkerCategory, ...]) -> MarkerCategory | None:
    """The first of ``categories``, in the order given, that has a marker in ``text``."""
    return next(
        (category for category in categories if marker_pattern(category).search(text)), None
    )
