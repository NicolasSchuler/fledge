"""Search for entries that probably describe the same work.

This module owns the title-similarity heuristic behind ``bibliography.fuzzy_duplicates``:
entries with a literal title, author and year are grouped by normalized author and year,
and pairs within a group whose titles are similar enough are reported for review. Exact
duplicate keys and DOIs are the integrity checks' job (``bibliography_integrity``).
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Iterator
from difflib import SequenceMatcher
from typing import NamedTuple

from .bibliography_index import LocatedEntry
from .bibliography_options import BibliographyOptions
from .bibliography_results import ResultCollector, SummarySpec, item_finding
from .bibtex_syntax import field_literal, read_doi
from .cancellation import cancellation_point
from .citation_graph import Uncertainty
from .findings import EvidenceKind, Finding

_MAX_FUZZY_ENTRIES = 2_000
_MAX_FUZZY_PAIRS = 20_000
_MAX_FUZZY_TEXT = 2_000


def normalized_text(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    return " ".join(re.findall(r"[^\W_]+", value, re.UNICODE))


class _FuzzyCandidate(NamedTuple):
    reference: LocatedEntry
    title: str
    doi: str | None


def _fuzzy_candidates(
    references: list[LocatedEntry], uncertainties: list[Uncertainty]
) -> dict[tuple[str, str], list[_FuzzyCandidate]]:
    """Entries with literal title, author and year, grouped by normalized author and year."""
    groups: dict[tuple[str, str], list[_FuzzyCandidate]] = defaultdict(list)
    for index, reference in enumerate(references):
        cancellation_point()
        if index >= _MAX_FUZZY_ENTRIES:
            uncertainties.append(Uncertainty(f"Fuzzy entry count exceeds {_MAX_FUZZY_ENTRIES}"))
            break
        values: dict[str, str] = {}
        for name in ("title", "author", "year"):
            bib_fields = reference.entry.fields_named(name)
            if name == "year" and not bib_fields:
                bib_fields = reference.entry.fields_named("date")
            value = field_literal(bib_fields[0]) if len(bib_fields) == 1 else None
            if value is not None and len(value) <= _MAX_FUZZY_TEXT:
                values[name] = normalized_text(value)
        if (
            len(values) != 3
            or not all(values.values())
            or re.fullmatch(r"\d{4}(?: \d{2}){0,2}", values["year"]) is None
        ):
            uncertainties.append(
                Uncertainty(
                    f"{reference.document.path}: {reference.entry.key} "
                    "lacks supported title/author/year evidence"
                )
            )
            continue
        doi_fields = reference.entry.fields_named("doi")
        doi = read_doi(doi_fields[0]).identifier if len(doi_fields) == 1 else None
        groups[(values["author"], values["year"][:4])].append(
            _FuzzyCandidate(reference, values["title"], doi)
        )
    return groups


def _candidate_pairs(
    groups: dict[tuple[str, str], list[_FuzzyCandidate]],
) -> Iterator[tuple[_FuzzyCandidate, _FuzzyCandidate]]:
    """Each earlier candidate with each later one of the same group."""
    for group in groups.values():
        for index, current in enumerate(group):
            for previous in group[:index]:
                yield previous, current


def find_fuzzy_duplicates(
    references: list[LocatedEntry],
    uncertainties: list[Uncertainty],
    options: BibliographyOptions,
    results: ResultCollector,
) -> None:
    """Report entries whose normalized title is close to an earlier entry's with the same
    author and year, and summarize the search."""
    uncertain = list(uncertainties)
    findings: list[Finding] = []
    comparisons = 0
    for previous, current in _candidate_pairs(_fuzzy_candidates(references, uncertain)):
        cancellation_point()
        if comparisons >= _MAX_FUZZY_PAIRS:
            uncertain.append(Uncertainty(f"Fuzzy comparison count exceeds {_MAX_FUZZY_PAIRS}"))
            break
        comparisons += 1
        if current.doi is not None and current.doi == previous.doi:
            # Exact DOI identity is bibliography.duplicate_doi (BIB007), not a heuristic.
            continue
        similarity = SequenceMatcher(None, current.title, previous.title, autojunk=False).ratio()
        if similarity >= options.fuzzy_duplicate_threshold:
            first = previous.reference
            findings.append(
                item_finding(
                    "fuzzy_duplicates",
                    f"Entries {first.entry.key!r} and {current.reference.entry.key!r} "
                    "may describe the same work; identity and version require review.",
                    "failed",
                    reference=current.reference,
                    evidence=EvidenceKind.HEURISTIC,
                    details={
                        "first_entry": first.entry.key,
                        "first_path": first.document.path,
                        "first_line": first.document.line(first.entry.start),
                        "title_similarity": similarity,
                        "threshold": options.fuzzy_duplicate_threshold,
                        "matching": "normalized author and year",
                    },
                )
            )
    spec = SummarySpec("fuzzy_duplicates", "Likely-work duplicate search")
    results.summarize(spec, findings, comparisons, uncertain)
