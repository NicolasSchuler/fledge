"""Comparing a bibliography entry with the Crossref record of its DOI.

This module owns the question "does the entry agree with the record?" for four literal
fields: title, author family names, year and venue. The answer is a typed
``MetadataComparison``; turning it into a finding, and deciding what a difference
means, is ``online_checks``'s job. Text is compared after normalization (case, accents,
markup and punctuation are ignored), author lists as ordered family names read with
BibTeX's name syntax. Agreement is never proof of identity.
"""

from __future__ import annotations

import html
import re
import unicodedata
from dataclasses import dataclass, field
from typing import Any, Literal

from .bibtex_names import AuthorList, FamilyName, parse_author_list
from .crossref import CrossrefWork
from .online_inputs import Reference

MetadataOutcome = Literal["mismatch", "incomplete", "agreement"]


def normalized_text(value: str) -> str:
    """``value`` as lowercase words: markup, entities, accents and punctuation removed."""
    return " ".join(
        re.findall(
            r"\w+",
            unicodedata.normalize("NFKC", html.unescape(re.sub(r"<[^>]*>", "", value))).casefold(),
        )
    )


def _family_matches(name: FamilyName, remote: str) -> bool:
    """Whether the Crossref family name equals the bibliography's, with or without particles."""
    local_forms = {normalized_text(spelling) for spelling in name.spellings()}
    remote_name = FamilyName(remote, remote)
    remote_forms = {normalized_text(spelling) for spelling in remote_name.spellings()}
    return bool(local_forms & remote_forms)


def _authors_agree(authors: AuthorList, remote: list[str]) -> bool:
    """Same family names in order; a list ending in ``and others`` need match only its prefix."""
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
    """One compared field: both sides as lists of strings, and whether they agree."""

    local: list[str]
    remote: list[str]
    matches: bool
    # Only for authors: the entry's list ends in ``and others``.
    local_truncated: bool = False


@dataclass(slots=True)
class MetadataComparison:
    """The fields that could be compared, and those with no comparable value on a side."""

    fields: dict[str, FieldComparison] = field(default_factory=dict)
    unmeasured: list[str] = field(default_factory=list)

    @property
    def mismatched(self) -> list[str]:
        """The compared fields that disagree, in comparison order."""
        return [name for name, row in self.fields.items() if not row.matches]

    @property
    def outcome(self) -> MetadataOutcome:
        """A disagreement outranks missing values, which outrank agreement."""
        if self.mismatched:
            return "mismatch"
        return "incomplete" if self.unmeasured else "agreement"

    def as_details(self) -> dict[str, dict[str, Any]]:
        """The comparison as the report records it."""
        details: dict[str, dict[str, Any]] = {}
        for name, row in self.fields.items():
            details[name] = {"local": row.local, "remote": row.remote, "matches": row.matches}
            if name == "authors":
                details[name]["local_truncated"] = row.local_truncated
        return details


def _single(value: str | None) -> list[str]:
    return [value] if value else []


def compare_work(reference: Reference, work: CrossrefWork) -> MetadataComparison:
    """Compare ``reference`` with ``work``; a field missing on either side is unmeasured."""
    text = reference.author_text
    if text is None:
        text = reference.values.get("author")
    authors = parse_author_list(text) if text else None
    values = reference.values
    pairs: dict[str, tuple[list[str], list[str]]] = {
        "title": (_single(values.get("title")), work.titles),
        "authors": ([name.full for name in authors.names] if authors else [], work.authors),
        "year": (_single(values.get("year")), work.years),
        "venue": (_single(values.get("journal") or values.get("booktitle")), work.venues),
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
