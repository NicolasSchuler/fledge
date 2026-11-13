"""Venue names of bibliography entries: how one is written, and the name it stands for.

The venue suggestion (``bibliography.venue_variants``) groups the journal and book titles
of one .bib file by the venue they name and reports a group whose entries spell that
name in more than one way. This module owns the two readings it compares, which are
pure functions of a literal value:

- the spelling (:func:`venue_spelling`): the words as a reader of the reference list
  sees them, with their case, abbreviation dots and ampersands, but without the years
  and edition numbers that rightly differ between the entries of one venue;
- the name (:func:`venue_name`): the spelling case-folded, with common abbreviations
  such as "Proc." and "Conf." written out, "&" read as "and", and small function words
  such as "of" and "the" left out.

A name made only of words such as "Proceedings" or "Journal" says too little to group
by, so it has none. Abbreviations that stand for several words, such as "Comput."
(computing, computer, computational) or "Phys." (physics, physical), are left as they
are: two spellings group only when their names are equal, never by similarity.
"""

from __future__ import annotations

import re
import unicodedata

# A year written as "'17", a word with the dot that marks an abbreviation, or an ampersand.
_TOKEN = re.compile(r"(?P<year>['’]\d{2}(?!\d))|(?P<word>[^\W_]+\.?)|(?P<ampersand>&)")
_YEAR = re.compile(r"(?:19|20)\d{2}")
_ORDINAL = re.compile(r"\d+(?:st|nd|rd|th)")
# Abbreviations that stand for one word only, after ISO 4 and the IEEE reference guide.
_ABBREVIATIONS = {
    "acad": "academy",
    "annu": "annual",
    "artif": "artificial",
    "assoc": "association",
    "commun": "communications",
    "conf": "conference",
    "eng": "engineering",
    "inf": "information",
    "int": "international",
    "intell": "intelligence",
    "internat": "international",
    "intl": "international",
    "j": "journal",
    "learn": "learning",
    "lett": "letters",
    "linguist": "linguistics",
    "mach": "machine",
    "natl": "national",
    "proc": "proceedings",
    "process": "processing",
    "recognit": "recognition",
    "res": "research",
    "rev": "review",
    "softw": "software",
    "symp": "symposium",
    "syst": "systems",
    "technol": "technology",
    "trans": "transactions",
    "wkshp": "workshop",
}
_FUNCTION_WORDS = frozenset({"a", "an", "and", "at", "for", "in", "of", "on", "the", "to"})
# Words that say what kind of venue it is, but not which one.
_GENERIC_WORDS = frozenset(
    {
        "annual",
        "conference",
        "edition",
        "international",
        "journal",
        "letters",
        "meeting",
        "national",
        "papers",
        "part",
        "proceedings",
        "series",
        "symposium",
        "transactions",
        "volume",
        "workshop",
    }
)


def venue_spelling(value: str) -> tuple[str, ...]:
    """Return the words of a venue as they are written, without years and edition numbers.

    Braces are TeX's case protection and invisible in the output, so they are not part of
    the spelling; neither is punctuation other than an abbreviation's dot and "&".

    Args:
        value: The literal journal or book title, without its outer braces.

    Returns:
        The words, each with its case and any dot that follows it, in order.
    """
    words: list[str] = []
    for match in _TOKEN.finditer(unicodedata.normalize("NFKC", value.replace("\\&", "&"))):
        word = match.group("word")
        if word is not None and not _is_year_or_ordinal(word.rstrip(".")):
            words.append(word)
        elif match.group("ampersand"):
            words.append("&")
    return tuple(words)


def _is_year_or_ordinal(word: str) -> bool:
    """Return whether ``word`` is a year from 1900 to 2099 or an ordinal such as ``34th``."""
    return _YEAR.fullmatch(word) is not None or _ORDINAL.fullmatch(word.casefold()) is not None


def venue_name(spelling: tuple[str, ...]) -> tuple[str, ...] | None:
    """Return the venue name a spelling stands for, or ``None`` when it names no venue alone.

    Args:
        spelling: The words as :func:`venue_spelling` returns them.

    Returns:
        The case-folded words with abbreviations written out and function words left out;
        ``None`` when no word other than a kind of venue (such as "Proceedings") and no
        letter is left.
    """
    name: list[str] = []
    for word in spelling:
        folded = "and" if word == "&" else word.rstrip(".").casefold()
        folded = _ABBREVIATIONS.get(folded, folded)
        if folded not in _FUNCTION_WORDS:
            name.append(folded)
    distinctive = any(
        word not in _GENERIC_WORDS and any(character.isalpha() for character in word)
        for word in name
    )
    return tuple(name) if distinctive else None
