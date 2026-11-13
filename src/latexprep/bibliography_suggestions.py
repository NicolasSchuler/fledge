"""Optional advice about the entries of the .bib files: preprints, repeated DOIs and venues.

This module owns the bibliography suggestions, which never change the outcome and are
reported only when a selector other than ``ALL`` selects them:

- ``bibliography.preprint``: an entry that cites a preprint, as
  :func:`~latexprep.bibliography_preprints.entry_preprint_evidence` recognises one; the
  online published-version check (``online.published_version``) asks the same detector;
- ``bibliography.url_repeats_doi``: a ``url`` field that only repeats the entry's DOI as a
  doi.org address, so that a style that prints both shows the same link twice;
- ``bibliography.venue_variants``: one venue whose name is spelled in several ways in one
  .bib file (:mod:`~latexprep.bibliography_venues`).

Only complete, parsed files are read, and nothing is changed. Every finding has the
``suggestion`` severity, so a ``% fledge: ignore CODE`` comment on the line before the
entry silences it.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from urllib.parse import unquote, urlsplit

from .bibliography_preprints import entry_preprint_evidence
from .bibliography_venues import venue_name, venue_spelling
from .bibtex_syntax import BibDocument, Entry, read_doi
from .cancellation import cancellation_point
from .finding_text import more_suffix
from .findings import EvidenceKind, Finding

# Fields that hold the name of the venue a work appeared in (biblatex: journaltitle).
_VENUE_FIELDS = ("journal", "journaltitle", "booktitle")
# Longest venue value compared; longer ones are not grouped.
_MAX_VENUE_CHARACTERS = 512
# At most this many venue groups are reported for one run, those of earlier files first.
MAX_VENUE_GROUPS = 10
# Variants quoted in a message, and entry keys listed for each variant in the details.
_MAX_QUOTED_VARIANTS = 3
_MAX_LISTED_KEYS = 20
_MAX_QUOTED_CHARACTERS = 80
_DOI_HOSTS = frozenset({"doi.org", "dx.doi.org", "www.doi.org"})


def _suggestion(
    document: BibDocument,
    entry: Entry,
    rule: str,
    message: str,
    *,
    evidence: EvidenceKind,
    suggestion: str,
    details: dict[str, object],
) -> Finding:
    """Build a suggestion about ``entry``, located at the line where the entry starts.

    Args:
        document: The document that holds the entry.
        entry: The entry the suggestion is about.
        rule: The rule name that follows ``bibliography.``.
        message: The message.
        evidence: The kind of evidence behind the suggestion.
        suggestion: What the author could do.
        details: Further details, to which the entry's key and type are added.

    Returns:
        The finding, with the ``suggestion`` severity and the ``failed`` status.
    """
    return Finding(
        f"bibliography.{rule}",
        message,
        severity="suggestion",
        status="failed",
        path=document.path,
        line=document.line(entry.start),
        evidence_kind=evidence,
        suggestion=suggestion,
        details={"entry": entry.key, "entry_type": entry.entry_type, **details},
    )


def _preprint(document: BibDocument, entry: Entry) -> Finding | None:
    """Suggest checking for a published version when ``entry`` cites a preprint."""
    reason = entry_preprint_evidence(entry)
    if reason is None:
        return None
    return _suggestion(
        document,
        entry,
        "preprint",
        f"Entry {entry.key!r} cites a preprint: {reason}.",
        evidence=EvidenceKind.HEURISTIC,
        suggestion=(
            "Check whether the work has been published since and cite the published version "
            "if so. With --online and online_published_versions, NET004 looks for it."
        ),
        details={"reason": reason},
    )


def _doi_in_url(value: str) -> str | None:
    """Return the DOI that a doi.org address names, or ``None`` for any other address.

    Args:
        value: The literal URL.

    Returns:
        The percent-decoded path of an HTTP or HTTPS address on doi.org (or dx.doi.org)
        without user information, port, query or fragment, or ``None``.
    """
    try:
        parts = urlsplit(value.strip())
        port = parts.port
    except ValueError:
        return None
    if (
        parts.scheme.lower() not in {"http", "https"}
        or (parts.hostname or "") not in _DOI_HOSTS
        or parts.username is not None
        or port is not None
        or parts.query
        or parts.fragment
    ):
        return None
    return unquote(parts.path.removeprefix("/")) or None


def _url_repeats_doi(document: BibDocument, entry: Entry) -> Finding | None:
    """Suggest dropping a ``url`` that names the entry's own DOI on doi.org."""
    doi_fields, url_fields = entry.fields_named("doi"), entry.fields_named("url")
    if len(doi_fields) != 1 or len(url_fields) != 1:
        return None
    doi = read_doi(doi_fields[0]).identifier
    atoms = url_fields[0].atoms
    if doi is None or len(atoms) != 1 or atoms[0].kind != "literal":
        return None
    url = atoms[0].value.strip()
    if (_doi_in_url(url) or "").casefold() != doi.casefold():
        return None
    return _suggestion(
        document,
        entry,
        "url_repeats_doi",
        f"Entry {entry.key!r}: the url field only repeats the DOI {doi} as a doi.org address.",
        evidence=EvidenceKind.DERIVED,
        suggestion=(
            "Remove the url field and keep the doi field, unless your bibliography style "
            "prints URLs but not DOIs; a style that prints both shows the same link twice."
        ),
        details={"doi": doi, "url": url},
    )


def _venue_value(entry: Entry, name: str) -> str | None:
    r"""Return the literal value of the venue field ``name``, or ``None`` when it cannot be read.

    A value that is repeated, a macro, a concatenation, longer than
    ``_MAX_VENUE_CHARACTERS`` or holds a backslash other than in ``\&`` is not read.

    Args:
        entry: The entry to read.
        name: The venue field.

    Returns:
        The value as a reader sees it: without braces, with ``\&`` written as "&" and its
        white space collapsed; or ``None``.
    """
    matches = entry.fields_named(name)
    if len(matches) != 1 or len(matches[0].atoms) != 1 or matches[0].atoms[0].kind != "literal":
        return None
    value = matches[0].atoms[0].value.replace("\\&", "&")
    if len(value) > _MAX_VENUE_CHARACTERS or "\\" in value:
        return None
    return " ".join(value.replace("{", "").replace("}", "").split())


@dataclass(slots=True)
class _Variant:
    """One spelling of a venue in one file, and the entries that use it.

    Attributes:
        written: The value of its first entry, as the message quotes it.
        entries: The entries that use the spelling, with the venue field each uses.
    """

    written: str
    entries: list[tuple[Entry, str]] = field(default_factory=list)


def _venue_groups(document: BibDocument) -> Iterator[list[_Variant]]:
    """Group the venue values of one file by venue name; yield the groups spelled several ways.

    Args:
        document: The parsed file.

    Yields:
        The spellings of each venue that has more than one, most entries first and, among
        equally many, the first written first; venues in the order they first appear.
    """
    venues: dict[tuple[str, ...], dict[tuple[str, ...], _Variant]] = {}
    for entry in document.entries:
        cancellation_point()
        for name in _VENUE_FIELDS:
            value = _venue_value(entry, name)
            spelling = venue_spelling(value) if value else ()
            venue = venue_name(spelling)
            if value is None or venue is None:
                continue
            variants = venues.setdefault(venue, {})
            variants.setdefault(spelling, _Variant(value)).entries.append((entry, name))
    for variants in venues.values():
        if len(variants) > 1:
            yield sorted(variants.values(), key=lambda variant: -len(variant.entries))


def _quoted(text: str) -> str:
    """Quote ``text`` for a message, shortened to ``_MAX_QUOTED_CHARACTERS`` characters."""
    if len(text) > _MAX_QUOTED_CHARACTERS:
        text = text[: _MAX_QUOTED_CHARACTERS - 1] + "…"
    return repr(text)


def _variant_row(variant: _Variant) -> dict[str, object]:
    """Describe one spelling as a row of finding details, with its first entry keys."""
    return {
        "spelling": variant.written,
        "count": len(variant.entries),
        "fields": sorted({name for _, name in variant.entries}),
        "entries": [entry.key for entry, _ in variant.entries[:_MAX_LISTED_KEYS]],
    }


def _venue_finding(document: BibDocument, variants: list[_Variant], omitted: int) -> Finding:
    """Report one venue spelled several ways in ``document``.

    The finding is located at the first entry, in file order, that uses another spelling
    than the most common one.

    Args:
        document: The file whose entries spell the venue.
        variants: The spellings, most entries first.
        omitted: How many further venue groups of the run are not reported.

    Returns:
        The suggestion.
    """
    listed = "; ".join(
        f"{_quoted(variant.written)} ({len(variant.entries)} "
        f"{'entry' if len(variant.entries) == 1 else 'entries'})"
        for variant in variants[:_MAX_QUOTED_VARIANTS]
    )
    message = (
        f"One venue is spelled {len(variants)} ways in {document.path}: {listed}"
        + more_suffix(len(variants) - _MAX_QUOTED_VARIANTS)
        + "."
    )
    if omitted:
        message += f" {omitted} more such venue(s) are not listed."
    others = (entry for variant in variants[1:] for entry, _ in variant.entries)
    first = min(others, key=lambda entry: entry.start)
    return _suggestion(
        document,
        first,
        "venue_variants",
        message,
        evidence=EvidenceKind.HEURISTIC,
        suggestion=(
            "Choose one spelling and use it in every entry, or define it once with @string "
            "and use the macro."
        ),
        details={
            "variants": [_variant_row(variant) for variant in variants],
            "omitted_venues": omitted,
        },
    )


def bibliography_suggestions(documents: list[BibDocument]) -> list[Finding]:
    """Offer the bibliography suggestions for the entries of the complete documents.

    A document with a syntax error is skipped. At most ``MAX_VENUE_GROUPS`` venue groups
    are reported; the last one says how many more there are.

    Args:
        documents: The parsed .bib files, in the order they were read.

    Returns:
        The preprint and repeated-DOI suggestions of each entry, then the venue groups of
        each file, in file order.
    """
    findings: list[Finding] = []
    groups: list[tuple[BibDocument, list[_Variant]]] = []
    for document in documents:
        if not document.valid:
            continue
        for entry in document.entries:
            cancellation_point()
            findings.extend(
                item
                for item in (_preprint(document, entry), _url_repeats_doi(document, entry))
                if item is not None
            )
        groups.extend((document, variants) for variants in _venue_groups(document))
    reported = groups[:MAX_VENUE_GROUPS]
    for index, (document, variants) in enumerate(reported):
        omitted = len(groups) - len(reported) if index == len(reported) - 1 else 0
        findings.append(_venue_finding(document, variants, omitted))
    return findings
