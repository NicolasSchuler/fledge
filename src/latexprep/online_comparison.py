"""Comparing a bibliography entry with the Crossref record of its DOI.

This module owns the question "does the entry agree with the record?" for four literal
fields: title, author family names, year and venue. The answer is a typed
``MetadataComparison``; turning it into a finding, and deciding what a difference
means, is ``online_checks``'s job. Text is compared after normalization (case, markup
and punctuation are ignored, but accents count, so ``Müller`` and ``Muller`` differ),
author lists as ordered family names read with BibTeX's name syntax. Agreement is never
proof of identity.
"""

from __future__ import annotations

import html
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Literal

from .bibtex_names import AuthorList, FamilyName, parse_author_list
from .crossref import CrossrefWork
from .online_inputs import Reference

MetadataOutcome = Literal["mismatch", "incomplete", "agreement"]
_LITERAL_DATE = re.compile(r"[0-9]{4}(?:-[0-9]{2}){0,2}")


def normalized_text(value: str) -> str:
    """Reduce ``value`` to casefolded words, with markup and punctuation removed.

    HTML tags are dropped and entities decoded, as Crossref titles carry them. The text is
    NFKC-normalized, so a precomposed and a decomposed accented letter agree, but accents
    are kept: ``Müller`` and ``Muller`` differ.

    Args:
        value: The text to normalize.

    Returns:
        The runs of word characters (letters, digits and underscores) of ``value``,
        casefolded and separated by single spaces.
    """
    return " ".join(
        re.findall(
            r"\w+",
            unicodedata.normalize("NFKC", html.unescape(re.sub(r"<[^>]*>", "", value))).casefold(),
        )
    )


def _family_matches(name: FamilyName, remote: str) -> bool:
    """Return whether the Crossref family name equals the bibliography's, with or without particles.

    Args:
        name: The family name from the bibliography entry.
        remote: The family name from the Crossref record.

    Returns:
        ``True`` when a normalized spelling of one side equals one of the other.
    """
    local_forms = {normalized_text(spelling) for spelling in name.spellings()}
    remote_name = FamilyName(remote, remote)
    remote_forms = {normalized_text(spelling) for spelling in remote_name.spellings()}
    return bool(local_forms & remote_forms)


def _authors_agree(authors: AuthorList, remote: list[str]) -> bool:
    """Return whether both author lists have the same family names in order.

    A list ending in ``and others`` need match only its prefix.

    Args:
        authors: The entry's parsed author list.
        remote: The family names of the Crossref record.

    Returns:
        ``True`` when every family name matches its counterpart.
    """
    names = authors.names
    if authors.truncated:
        if len(names) > len(remote):
            return False
        remote = remote[: len(names)]
    elif len(names) != len(remote):
        return False
    return all(_family_matches(name, family) for name, family in zip(names, remote, strict=True))


@dataclass(frozen=True, slots=True)
class FieldComparison:
    """One compared field: both sides as lists of strings, and whether they agree.

    Attributes:
        local: The values of the bibliography entry.
        remote: The values of the Crossref record.
        matches: Whether the two sides agree.
        local_truncated: Only for authors: the entry's list ends in ``and others``.
    """

    local: list[str]
    remote: list[str]
    matches: bool
    local_truncated: bool = False


@dataclass(slots=True)
class MetadataComparison:
    """The fields that could be compared, and those with no comparable value on a side.

    Attributes:
        fields: The compared fields by name, in comparison order.
        unmeasured: The names of the fields with no comparable value on a side.
    """

    fields: dict[str, FieldComparison] = field(default_factory=dict)
    unmeasured: list[str] = field(default_factory=list)

    @property
    def mismatched(self) -> list[str]:
        """The compared fields that disagree, in comparison order."""
        return [name for name, row in self.fields.items() if not row.matches]

    @property
    def outcome(self) -> MetadataOutcome:
        """The overall outcome: a disagreement outranks missing values, which outrank agreement."""
        if self.mismatched:
            return "mismatch"
        return "incomplete" if self.unmeasured else "agreement"

    def as_details(self) -> dict[str, dict[str, Any]]:
        """Render the comparison as the report records it.

        Returns:
            Both sides and whether they match, per compared field; the authors row also
            records ``local_truncated``.
        """
        details: dict[str, dict[str, Any]] = {}
        for name, row in self.fields.items():
            details[name] = {"local": row.local, "remote": row.remote, "matches": row.matches}
            if name == "authors":
                details[name]["local_truncated"] = row.local_truncated
        return details


def _single(value: str | None) -> list[str]:
    """Wrap a non-empty value in a list; ``None`` or an empty string gives an empty list."""
    return [value] if value else []


def _publication_year(values: Mapping[str, str | None]) -> str | None:
    """Read the year or a literal ISO date, leaving ambiguous pairs unmeasured."""
    year, written = values.get("year", ""), values.get("date", "")
    if year is None or written is None:
        return None
    if not written:
        return year
    if not _LITERAL_DATE.fullmatch(written):
        return None
    try:
        date.fromisoformat(written + "-01" * (2 - written.count("-")))
    except ValueError:
        return None
    date_year = written[:4]
    return date_year if not year or year == date_year else None


def _publication_venue(values: Mapping[str, str | None]) -> str | None:
    """Read the journal aliases, or use the book title when both aliases are absent."""
    journal, journaltitle = values.get("journal", ""), values.get("journaltitle", "")
    if journal is None or journaltitle is None:
        return None
    if journal and journaltitle and normalized_text(journal) != normalized_text(journaltitle):
        return None
    return journal or journaltitle or values.get("booktitle")


def compare_work(reference: Reference, work: CrossrefWork) -> MetadataComparison:
    """Compare ``reference`` with ``work``; a field missing on either side is unmeasured.

    The authors come from the entry's raw author text when it has one, else from its
    ``author`` value. A literal ``date`` can supply the publication year and
    ``journaltitle`` can supply the journal. Conflicting aliases are unmeasured.

    Args:
        reference: The bibliography entry.
        work: The Crossref record of the entry's DOI.

    Returns:
        The compared fields and the unmeasured ones.
    """
    text = reference.author_text
    if text is None:
        text = reference.values.get("author")
    authors = parse_author_list(text) if text else None
    values = reference.values
    pairs: dict[str, tuple[list[str], list[str]]] = {
        "title": (_single(values.get("title")), work.titles),
        "authors": ([name.full for name in authors.names] if authors else [], work.authors),
        "year": (_single(_publication_year(values)), work.years),
        "venue": (_single(_publication_venue(values)), work.venues),
    }
    comparison = MetadataComparison()
    for name, (local, remote) in pairs.items():
        if not local or not remote or any(not value for value in [*local, *remote]):
            comparison.unmeasured.append(name)
        elif name == "authors" and authors is not None:
            matches = _authors_agree(authors, remote)
            comparison.fields[name] = FieldComparison(local, remote, matches, authors.truncated)
        else:
            left = [normalized_text(value) for value in local]
            right = [normalized_text(value) for value in remote]
            comparison.fields[name] = FieldComparison(local, remote, left[0] in right)
    return comparison
