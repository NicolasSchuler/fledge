"""Whether a bibliography entry is a preprint, in the forms that common tools export.

This module owns the one preprint detector: the offline preprint suggestion and the
online published-version check (``online.published_version``) both ask it, so they
agree on which entries are preprints. It reads literal field values and decides nothing
else; it builds no findings and makes no request.

An entry is a preprint when its venue is a preprint server, in any of these forms:

- ``journal = {arXiv preprint arXiv:…}`` (Google Scholar) or ``journal = {ArXiv}``
  (Semantic Scholar), and the same in biblatex's ``journaltitle`` or in ``howpublished``;
- ``journal = {CoRR}`` with ``volume = {abs/…}`` (DBLP);
- a DOI under arXiv's prefix, ``10.48550/arXiv.…``;
- the entry type ``@unpublished`` or biblatex's ``@preprint``.

Fields that only say where a copy is kept, ``archivePrefix = {arXiv}`` (arXiv's own
export), biblatex's ``eprinttype = {arxiv}``, an arxiv.org URL or a note that says
"preprint", count only when no journal or book title names a published venue: a published
article that records its arXiv copy is not a preprint.
"""

from __future__ import annotations

from collections.abc import Mapping

from .bibtex_syntax import Entry, field_literal, read_doi

# The DOI prefix under which arXiv registers its articles with DataCite, case-folded.
_ARXIV_DOI_PREFIX = "10.48550/arxiv."
_PREPRINT_ENTRY_TYPES = frozenset({"unpublished", "preprint"})
# Fields that name where a work appeared, and could name a preprint server instead.
_SERVER_FIELDS = ("journal", "journaltitle", "howpublished")
# Fields that name the published venue of a work.
_PUBLISHED_VENUE_FIELDS = ("journal", "journaltitle", "booktitle")
# Fields that say which archive keeps a copy (arXiv's export and biblatex).
_ARCHIVE_FIELDS = ("archiveprefix", "eprinttype")
# Every field the detector reads; entry readers need no other.
_PREPRINT_FIELDS = frozenset(
    {*_SERVER_FIELDS, *_PUBLISHED_VENUE_FIELDS, *_ARCHIVE_FIELDS, "volume", "url", "note"}
)
# Longest field value the detector reads; longer values do not count as evidence.
_MAX_VALUE_CHARACTERS = 2048


def is_arxiv_doi(doi: str | None) -> bool:
    """Return whether ``doi`` lies under arXiv's DOI prefix, in any letter case."""
    return doi is not None and doi.casefold().startswith(_ARXIV_DOI_PREFIX)


def _folded(values: Mapping[str, str | None], name: str) -> str:
    """Return the field ``name`` case-folded with its white space collapsed; empty when unknown."""
    return " ".join((values.get(name) or "").casefold().split())


def _server_evidence(entry_type: str, values: Mapping[str, str | None], doi: str | None) -> str:
    """Say how the entry names a preprint server as its venue, or return an empty string.

    Args:
        entry_type: The entry type, lowercased.
        values: The literal field values, as :func:`preprint_evidence` takes them.
        doi: The entry's DOI without a prefix, or ``None``.

    Returns:
        The reason in words, or an empty string.
    """
    for name in _SERVER_FIELDS:
        venue = _folded(values, name)
        if venue.startswith("arxiv"):
            return f"its {name} field names arXiv"
        if venue == "corr" and _folded(values, "volume").startswith("abs/"):
            return f"its {name} field is CoRR with an abs/ volume, as DBLP exports arXiv papers"
    if is_arxiv_doi(doi):
        return "its DOI is an arXiv DOI"
    if entry_type in _PREPRINT_ENTRY_TYPES:
        return f"it is an @{entry_type} entry"
    return ""


def _archive_evidence(values: Mapping[str, str | None]) -> str:
    """Say how the entry records an archived copy, or return an empty string.

    Args:
        values: The literal field values, as :func:`preprint_evidence` takes them.

    Returns:
        The reason in words, or an empty string.
    """
    for name in _ARCHIVE_FIELDS:
        if _folded(values, name) == "arxiv":
            return f"its {name} field names arXiv"
    if "arxiv.org/" in _folded(values, "url"):
        return "its URL points to arxiv.org"
    if "preprint" in _folded(values, "note"):
        return "its note calls it a preprint"
    return ""


def preprint_evidence(
    entry_type: str, values: Mapping[str, str | None], doi: str | None
) -> str | None:
    """Say why an entry is a preprint, or return ``None`` when it is not one.

    A field whose value is unknown (``None``: repeated, a macro or markup) is no evidence
    of a preprint, but a journal or book title of unknown value still names a published
    venue.

    Args:
        entry_type: The entry type, lowercased.
        values: The literal field values by lowercase field name, without braces; ``None``
            for a field whose value is unknown.
        doi: The entry's DOI without a prefix, or ``None``.

    Returns:
        The reason in words, such as "its journal field names arXiv", or ``None``.
    """
    if reason := _server_evidence(entry_type, values, doi):
        return reason
    if any(_names_published_venue(values, name) for name in _PUBLISHED_VENUE_FIELDS):
        return None
    return _archive_evidence(values) or None


def _names_published_venue(values: Mapping[str, str | None], name: str) -> bool:
    """Return whether the field ``name`` is present and names a venue other than arXiv or CoRR.

    A field of unknown value counts, because it may name one; an empty field does not.

    Args:
        values: The literal field values, as :func:`preprint_evidence` takes them.
        name: The field to inspect.

    Returns:
        Whether the field names a published venue.
    """
    if name not in values or values[name] == "":
        return False
    venue = _folded(values, name)
    return not venue.startswith("arxiv") and venue != "corr"


def entry_preprint_evidence(entry: Entry) -> str | None:
    """Say why a parsed entry is a preprint, or return ``None``; see :func:`preprint_evidence`.

    A repeated field, a macro, a value with a backslash and one longer than
    ``_MAX_VALUE_CHARACTERS`` are unknown values.

    Args:
        entry: The entry to read.

    Returns:
        The reason in words, or ``None``.
    """
    values: dict[str, str | None] = {}
    for item in entry.fields:
        if item.name not in _PREPRINT_FIELDS:
            continue
        literal = (
            None if item.name in values else field_literal(item, max_chars=_MAX_VALUE_CHARACTERS)
        )
        values[item.name] = None if literal is None else literal.replace("{", "").replace("}", "")
    doi_fields = entry.fields_named("doi")
    doi = read_doi(doi_fields[0]).identifier if len(doi_fields) == 1 else None
    return preprint_evidence(entry.entry_type, values, doi)
