"""Checks of the fields inside each bibliography entry.

This module owns the per-entry checks that the settings configure: required fields, DOI
presence, page-range syntax, URL syntax and the capitalization advisory. The verdict
functions judge one literal value and build no findings; ``check_entry_fields`` runs
them over entries and reports through a ``ResultCollector``. Whether a citation resolves
(``bibliography_checks``) and whether two entries duplicate each other
(``bibliography_duplicates``) are other modules' concern.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Callable
from typing import Literal, NamedTuple
from urllib.parse import urlsplit

from .bibliography_index import LocatedEntry
from .bibliography_options import BibliographyOptions
from .bibliography_results import ResultCollector, SummarySpec, item_finding
from .bibtex_syntax import Entry, Field, field_literal
from .cancellation import cancellation_point
from .citation_graph import Uncertainty
from .findings import EvidenceKind, Finding, Status
from .limits import CANCELLATION_STRIDE_CHARS

_INHERITANCE = frozenset({"crossref", "xref", "xdata"})
_URL_SCHEMES = frozenset({"http", "https", "ftp"})


Presence = Literal["present", "unknown", "missing"]


def content_state(entry: Entry, names: list[str]) -> Presence:
    """Whether one of the fields ``names`` holds text: present, unknown (a macro, repeated or
    inherited field) or missing."""
    uncertain = False
    for name in names:
        matches = entry.fields_named(name)
        if len(matches) > 1:
            uncertain = True
        elif matches:
            value = field_literal(matches[0])
            if value is None:
                uncertain = True
            elif value.translate(str.maketrans("", "", "{}")).strip():
                return "present"
    if uncertain or any(item.name in _INHERITANCE for item in entry.fields):
        return "unknown"
    return "missing"


class Verdict(NamedTuple):
    """The outcome of one literal field syntax check."""

    status: Status
    reason: str = ""
    tokens: tuple[str, ...] = ()


def page_verdict(value: str) -> Verdict:
    """Judge a literal ``pages`` value: ranges must ascend and use one notation."""
    if not value:
        return Verdict("failed", "empty page field")
    if any(character in value for character in "{}"):
        return Verdict("inconclusive", "brace markup")
    unknown = False
    for part in value.split(","):
        part = part.strip()
        if re.fullmatch(r"[A-Za-z]*\d+", part):
            continue
        match = re.fullmatch(r"([A-Za-z]*)(\d+)\s*(?:--?|[–—])\s*([A-Za-z]*)(\d+)", part)
        if match:
            first_prefix, first, last_prefix, last = match.groups()
            if first_prefix != last_prefix:
                unknown = True
            elif (len(first.lstrip("0")), first.lstrip("0")) > (
                len(last.lstrip("0")),
                last.lstrip("0"),
            ):
                return Verdict("failed", "descending page endpoints")
        elif not part or re.fullmatch(r"[\d\s–—-]+", part):
            return Verdict("failed", "malformed numeric page range")
        else:
            unknown = True
    return Verdict("inconclusive", "unsupported page notation") if unknown else Verdict("passed")


def _url_text_verdict(value: str) -> Verdict | None:
    """The verdict on the characters of a URL, or None when they are acceptable."""
    if any(character in value for character in "{}"):
        return Verdict("inconclusive", "brace markup")
    if not value or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        return Verdict("failed", "empty URL, whitespace or control characters")
    if re.search(r"%(?![0-9a-fA-F]{2})", value) or any(char in value for char in '<>"'):
        return Verdict("failed", "invalid percent escape or reserved delimiter")
    return None


def url_verdict(value: str) -> Verdict:
    """Judge a literal ``url`` value: an absolute URL with a valid host and port."""
    text = _url_text_verdict(value)
    if text is not None:
        return text
    try:
        parts = urlsplit(value)
        if not parts.scheme:
            return Verdict("failed", "an absolute URL scheme is missing")
        if parts.scheme.lower() not in _URL_SCHEMES:
            return Verdict(
                "inconclusive", "URL scheme is outside the supported HTTP/HTTPS/FTP subset"
            )
        if not parts.netloc or not parts.hostname:
            return Verdict("failed", "a network host is missing")
        parts.hostname.encode("idna")
        _ = parts.port
    except (ValueError, UnicodeError):
        return Verdict("failed", "host or port syntax is malformed")
    return Verdict("passed")


def unprotected_tokens(value: str) -> list[str]:
    """The case-sensitive words of ``value`` that no brace group protects."""
    depth = 0
    plain: list[str] = []
    for index, char in enumerate(value):
        if index % CANCELLATION_STRIDE_CHARS == 0:
            cancellation_point()
        if char == "{":
            depth += 1
            plain.append(" ")
        elif char == "}":
            depth = max(0, depth - 1)
            plain.append(" ")
        else:
            plain.append(char if depth == 0 else " ")
    return sorted(
        {
            token
            for token in re.findall(r"\b[A-Za-z][A-Za-z0-9]*\b", "".join(plain))
            if sum(char.isupper() for char in token) >= 2
            or any(char.isupper() for char in token[1:])
        }
    )


def capitalization_verdict(value: str) -> Verdict:
    """Judge a literal ``title``: words with capitals beyond the first letter need braces."""
    tokens = unprotected_tokens(value)
    if tokens:
        return Verdict("failed", "case-sensitive tokens may need brace protection", tuple(tokens))
    return Verdict("passed")


# Literal field syntax checks: the field each reads, its verdict and its evidence kind.
_FIELD_CHECKS: dict[str, tuple[str, Callable[[str], Verdict], EvidenceKind]] = {
    "page_ranges": ("pages", page_verdict, EvidenceKind.DERIVED),
    "url_syntax": ("url", url_verdict, EvidenceKind.DERIVED),
    "capitalization": ("title", capitalization_verdict, EvidenceKind.HEURISTIC),
}
_METADATA_DESCRIPTIONS = {
    "required_fields": "Configured field presence",
    "missing_doi": "Requested DOI presence",
    "page_ranges": "Literal page-range syntax",
    "url_syntax": "Literal URL syntax (offline)",
    "capitalization": "Style-dependent capitalization advisory",
}


def _required_field(reference: LocatedEntry, spec: str, *, uncertain: bool) -> Finding | None:
    state = content_state(reference.entry, spec.split("|"))
    if state == "missing" and uncertain:
        state = "unknown"
    if state == "present":
        return None
    return item_finding(
        "required_fields",
        f"Entry {reference.entry.key!r}: required field {spec!r} is "
        + ("missing or empty." if state == "missing" else "not statically established."),
        "failed" if state == "missing" else "inconclusive",
        reference=reference,
        severity="error",
        details={"requirement": spec},
    )


def _doi_presence(reference: LocatedEntry) -> Finding | None:
    state = content_state(reference.entry, ["doi"])
    if state == "present":
        return None
    return item_finding(
        "missing_doi",
        f"Entry {reference.entry.key!r}: DOI presence is "
        + ("missing or empty; a DOI may not exist." if state == "missing" else "unverified."),
        "failed" if state == "missing" else "inconclusive",
        reference=reference,
    )


def _field_syntax(reference: LocatedEntry, rule: str, bib_fields: list[Field]) -> Finding | None:
    name, verdict_of, evidence = _FIELD_CHECKS[rule]
    value = field_literal(bib_fields[0]) if len(bib_fields) == 1 else None
    verdict = (
        Verdict("inconclusive", "macro, concatenation, markup or repeated field")
        if value is None
        else verdict_of(value)
    )
    if verdict.status == "passed":
        return None
    return item_finding(
        rule,
        f"Entry {reference.entry.key!r}: {name} check found {verdict.reason}.",
        verdict.status,
        reference=reference,
        evidence=evidence,
        details={"field": name, **({"tokens": list(verdict.tokens)} if verdict.tokens else {})},
    )


def check_entry_fields(
    references: list[LocatedEntry],
    uncertainties: list[Uncertainty],
    options: BibliographyOptions,
    results: ResultCollector,
) -> None:
    """Run the configured field checks over ``references`` and summarize each of them."""
    requirements = dict(options.required_fields)
    enabled = {
        "required_fields": bool(requirements),
        "missing_doi": bool(options.doi_entry_types),
        "page_ranges": options.check_page_ranges,
        "url_syntax": options.check_urls,
        "capitalization": options.check_capitalization,
    }
    rows: dict[str, list[Finding]] = {name: [] for name, active in enabled.items() if active}
    counts: dict[str, int] = defaultdict(int)

    def add(rule: str, finding: Finding | None) -> None:
        counts[rule] += 1
        if finding is not None:
            rows[rule].append(finding)

    for reference in references:
        cancellation_point()
        entry = reference.entry
        for spec in (*requirements.get("*", ()), *requirements.get(entry.entry_type, ())):
            add("required_fields", _required_field(reference, spec, uncertain=bool(uncertainties)))
        if "*" in options.doi_entry_types or entry.entry_type in options.doi_entry_types:
            add("missing_doi", _doi_presence(reference))
        for rule, (name, _, _) in _FIELD_CHECKS.items():
            if enabled[rule] and (bib_fields := entry.fields_named(name)):
                add(rule, _field_syntax(reference, rule, bib_fields))
    for rule, row in rows.items():
        spec = SummarySpec(
            rule,
            _METADATA_DESCRIPTIONS[rule],
            severity="error" if rule == "required_fields" else "warning",
        )
        results.summarize(spec, row, counts[rule], uncertainties)
