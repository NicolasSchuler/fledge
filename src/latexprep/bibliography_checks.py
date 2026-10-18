"""Offline bibliography checks: file integrity, citation coverage and field details.

``check_bibliography_integrity`` inspects the bibliography files themselves:
syntax, duplicate keys and DOIs, repeated fields, undefined string macros,
missing relationship targets and DOI syntax. ``check_bibliography_coverage_and_fields``
relates a selected manuscript's literal citations to its declared resources and
applies the configured field checks and heuristics; only declared local resources
contribute to a selected manuscript's evidence.

Both are bounded lexical subsets. They do not execute TeX, expand bibliography
string macros, apply a bibliography backend's inheritance rules, contact URLs,
or propose edits.
"""

from __future__ import annotations

import re
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from pathlib import Path
from typing import ClassVar, Literal, NamedTuple
from urllib.parse import urlsplit

from .bibtex_syntax import (
    LOWERCASE_NAME,
    MAX_BIB_ENTRIES,
    MAX_BIB_FILE_BYTES,
    MAX_RESOURCE_FILES,
    MAX_RESOURCES_TOTAL_BYTES,
    BibDocument,
    BibFileReader,
    Declaration,
    Entry,
    Field,
    describe_read_failure,
    field_literal,
    mebibytes,
    read_doi,
)
from .cancellation import cancellation_point
from .citation_graph import (
    RELATIONSHIP_FIELDS,
    CitationGraph,
    EntryIndex,
    LocatedEntry,
    StyleReference,
    Uncertainty,
    bounded_uncertainties,
    build_citation_graph,
    index_entries,
    located_entries,
    require_directory,
    selected_bibliography,
    uncertainty_messages,
)
from .errors import PreparationError, SettingsError
from .file_safety import read_regular_file
from .file_scan import more_suffix
from .findings import EvidenceKind, Finding, Severity, Status
from .limits import CANCELLATION_STRIDE_CHARS, MIB
from .option_config import (
    NONZERO_FRACTION,
    Bounds,
    TextFormat,
    require_pairs,
    require_text,
    require_unique_strings,
    validate_scalars,
)
from .source_graph import UNPARSED_SOURCE_RULES, SourceGraph
from .tex_commands import ROOT_SUFFIXES

_MONTHS = frozenset("jan feb mar apr may jun jul aug sep oct nov dec".split())
# BibTeX styles define their own string macros. The standard styles (and the natbib and
# other styles derived from them) define these journal macros besides the months;
# other styles, such as ACM-Reference-Format, define more or fewer.
_BASE_STYLE_MACROS = frozenset(
    "acmcs acta cacm ibmjrd ibmsj ieeese ieeetc ieeetcad ipl jacm jcss scp sicomp tocs "
    "tods tog toms toois toplas tcs".split()
)
_BASE_STYLES = frozenset(
    "plain unsrt alpha abbrv ieeetr acm siam apalike plainnat unsrtnat abbrvnat".split()
)
_MAX_STYLE_BYTES = 2 * MIB
_STYLE_MACRO = re.compile(r"^[ \t]*MACRO[ \t]*\{[ \t]*([^\s{}]+)[ \t]*\}", re.MULTILINE)
_MAX_ROOTS = 32
_MAX_FUZZY_ENTRIES = 2_000
_MAX_FUZZY_PAIRS = 20_000
_MAX_FUZZY_TEXT = 2_000
_MAX_LISTED_ENTRIES = 3
_INHERITANCE = frozenset({"crossref", "xref", "xdata"})
_SELECTION_UNAVAILABLE = "No complete bibliography resource selection is available"


def _over_budget() -> str:
    return (
        "exceeds the bibliography byte budget "
        f"({mebibytes(MAX_BIB_FILE_BYTES)} per file, "
        f"{mebibytes(MAX_RESOURCES_TOTAL_BYTES)} in total)"
    )


# --- File integrity ---------------------------------------------------------------


def _load_bibliographies(
    root: Path, only: set[str] | None
) -> tuple[list[BibDocument], list[Finding]]:
    """Parse every .bib file below ``root``, or only the named ones."""
    require_directory(root, "Bibliography input")
    paths = (
        sorted(
            (path for path in root.rglob("*") if path.suffix.lower() == ".bib"),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        if only is None
        else [root / name for name in sorted(only)]
    )
    reader = BibFileReader()
    documents: list[BibDocument] = []
    findings: list[Finding] = []
    for path in paths:
        cancellation_point()
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            findings.append(
                Finding(
                    "bibliography.input",
                    "Symbolic-link bibliographies are not read or normalized.",
                    severity="error",
                    status="skipped",
                    path=relative,
                )
            )
            continue
        if not path.is_file():
            continue
        try:
            document = reader.read(path, relative)
            if document is None:
                raise ValueError(f"exceeds the {mebibytes(MAX_BIB_FILE_BYTES)} parser limit")
        except (OSError, UnicodeError, ValueError) as error:
            findings.append(
                Finding(
                    "bibliography.input",
                    f"Bibliography {relative} {describe_read_failure(error)}, "
                    "so it was not inspected.",
                    severity="error",
                    status="inconclusive",
                    path=relative,
                    suggestion=(
                        "Make the file readable, save it as UTF-8 and keep it within "
                        f"{mebibytes(MAX_BIB_FILE_BYTES)}."
                    ),
                )
            )
            continue
        documents.append(document)
    return documents, findings


def _plain_value(bib_field: Field) -> str | None:
    """A single brace-free literal, as relationship and alias fields must be."""
    return field_literal(bib_field, numbers=False, braces=False, max_chars=None)


def _defined_keys(documents: list[BibDocument]) -> set[str]:
    keys = {entry.key for document in documents for entry in document.entries}
    # biblatex's literal ids field supplies alternate entry keys.
    for document in documents:
        cancellation_point()
        for record in document.records:
            for bib_field in record.fields:
                if bib_field.name == "ids" and (value := _plain_value(bib_field)) is not None:
                    keys.update(key.strip() for key in value.split(",") if key.strip())
    return keys


def _field_values(entry: Entry) -> dict[str, tuple[tuple[str, str], ...]]:
    return {
        item.name: tuple((atom.kind, atom.value) for atom in item.atoms) for item in entry.fields
    }


def _conflicts(first: Entry, second: Entry) -> list[str]:
    first_values = _field_values(first)
    second_values = _field_values(second)
    return sorted(
        name
        for name in first_values.keys() & second_values.keys()
        if name != "doi" and first_values[name] != second_values[name]
    )


@dataclass(frozen=True, slots=True)
class _StyleMacros:
    """The string macros the BibTeX style in use is known to define.

    ``unread`` names the styles whose definitions are not available here, so any
    macro they might define stays uncertain. ``unknown`` means no document names
    a style at all, as in a bibliography-only directory.
    """

    macros: frozenset[str] = frozenset()
    unread: tuple[str, ...] = ()
    unknown: bool = False

    @property
    def uncertain(self) -> bool:
        return self.unknown or bool(self.unread)

    def explanation(self) -> str:
        """Why an undefined macro may still be fine, naming the style when it is known."""
        if self.unread:
            styles = ", ".join(repr(name) for name in self.unread)
            return f"the bibliography style {styles} may define it"
        return "a bibliography style used with this file may define it"


def _style_macros(root: Path, styles: list[StyleReference] | None) -> _StyleMacros:
    """Macros of the styles ``styles`` name: project .bst files are read, standard ones known."""
    if styles is None:
        return _StyleMacros(unknown=True)
    macros: set[str] = set()
    unread: list[str] = []
    for style in styles:
        cancellation_point()
        base = style.name.removesuffix(".bst").casefold()
        if style.local_file is not None:
            try:
                text = read_regular_file(root / style.local_file, _MAX_STYLE_BYTES).decode(
                    "utf-8", errors="replace"
                )
            except OSError:
                unread.append(style.name)
                continue
            # BST comments run from % to the end of the line.
            macros.update(
                name.casefold() for name in _STYLE_MACRO.findall(re.sub(r"%[^\n]*", "", text))
            )
        elif base in _BASE_STYLES:
            macros |= _BASE_STYLE_MACROS
        else:
            unread.append(style.name)
    return _StyleMacros(frozenset(macros), tuple(unread))


class _IntegrityCheck:
    """Cross-file consistency of keys, string macros, relationships and DOIs."""

    def __init__(
        self, documents: list[BibDocument], *, complete: bool, style: _StyleMacros | None = None
    ) -> None:
        style = style or _StyleMacros()
        self.style = style
        self.macro_names = (
            _MONTHS
            | style.macros
            | {
                bib_field.name
                for document in documents
                for record in document.records
                if isinstance(record, Declaration) and record.kind == "string"
                for bib_field in record.fields
            }
        )
        self.keys = _defined_keys(documents)
        # A missing definition is only certain when every inspected file parsed and the
        # bibliography style, which may define macros of its own, is known in full.
        certain = complete and not style.uncertain
        self.severity: Severity = "error" if certain else "warning"
        self.status: Status = "failed" if certain else "inconclusive"
        self.seen_keys: dict[str, tuple[BibDocument, Entry]] = {}
        self.seen_dois: dict[str, tuple[BibDocument, Entry]] = {}

    def findings(self, document: BibDocument, record: Entry | Declaration) -> Iterator[Finding]:
        if isinstance(record, Declaration):
            for bib_field in record.fields:
                yield from self._undefined_macros(document, bib_field)
            return
        yield from self._duplicate_key(document, record)
        names: set[str] = set()
        for bib_field in record.fields:
            if bib_field.name in names:
                yield document.finding(
                    "bibliography.repeated_field",
                    f"Field {bib_field.name!r} is repeated in entry {record.key!r}.",
                    bib_field.start,
                    severity="error",
                    suggestion="Choose the intended value; no field is removed automatically.",
                )
            names.add(bib_field.name)
            yield from self._undefined_macros(document, bib_field)
            if bib_field.name in RELATIONSHIP_FIELDS:
                yield from self._relationship(document, bib_field)
            if bib_field.name == "doi":
                yield from self._doi(document, record, bib_field)

    def _duplicate_key(self, document: BibDocument, entry: Entry) -> Iterator[Finding]:
        first = self.seen_keys.get(entry.key)
        if first is None:
            self.seen_keys[entry.key] = (document, entry)
            return
        first_document, first_entry = first
        yield document.finding(
            "bibliography.duplicate_key",
            f"Citation key {entry.key!r} occurs more than once.",
            entry.start,
            severity="error",
            suggestion="Review the entries and all uses before renaming or merging.",
            details={
                "first_path": first_document.path,
                "first_line": first_document.line(first_entry.start),
                "conflicting_fields": _conflicts(first_entry, entry),
            },
        )

    def _undefined_macros(self, document: BibDocument, bib_field: Field) -> Iterator[Finding]:
        for atom in bib_field.atoms:
            if atom.kind == "macro" and atom.value.lower() not in self.macro_names:
                if self.style.uncertain:
                    message = (
                        f"String macro {atom.value!r} has no definition in the inspected "
                        f"files; {self.style.explanation()}."
                    )
                    suggestion = (
                        "Define it with @string in a .bib file, or replace it with the literal "
                        "text, unless the style defines it."
                    )
                else:
                    message = (
                        f"String macro {atom.value!r} has no definition in the inspected files."
                    )
                    suggestion = (
                        "Define it with @string in a .bib file or replace it with the literal text."
                    )
                yield document.finding(
                    "bibliography.undefined_macro",
                    message,
                    atom.start,
                    severity=self.severity,
                    status=self.status,
                    suggestion=suggestion,
                )

    def _relationship(self, document: BibDocument, bib_field: Field) -> Iterator[Finding]:
        value = _plain_value(bib_field)
        if value is None:
            yield document.finding(
                "bibliography.relationship_unresolved",
                f"Field {bib_field.name!r} is not a plain literal; relationships are unverified.",
                bib_field.start,
                status="inconclusive",
            )
            return
        targets = [value] if bib_field.name in {"crossref", "xref"} else value.split(",")
        for target in targets:
            if target.strip() not in self.keys:
                yield document.finding(
                    "bibliography.missing_relationship",
                    f"Field {bib_field.name!r} refers to missing key {target.strip()!r}.",
                    bib_field.start,
                    severity=self.severity,
                    status=self.status,
                )

    def _doi(self, document: BibDocument, entry: Entry, bib_field: Field) -> Iterator[Finding]:
        reading = read_doi(bib_field)
        if reading.uncertainty:
            yield document.finding(
                "bibliography.doi_unresolved",
                reading.uncertainty,
                bib_field.start,
                status="inconclusive",
                suggestion="Inspect this DOI manually; it remains unchanged.",
            )
            return
        if reading.identifier is None:
            yield document.finding(
                "bibliography.invalid_doi",
                "The DOI field is not a recognized bare DOI or supported DOI URL.",
                bib_field.start,
                suggestion="Verify the identifier; no metadata will be guessed or replaced.",
            )
            return
        first = self.seen_dois.get(reading.identifier)
        if first is None:
            self.seen_dois[reading.identifier] = (document, entry)
            return
        first_document, first_entry = first
        if first_entry is not entry:
            yield document.finding(
                "bibliography.duplicate_doi",
                f"Entries {first_entry.key!r} and {entry.key!r} have the same exact DOI.",
                bib_field.start,
                suggestion="Review the matching works and conflicts before any merge.",
                details={
                    "doi": reading.identifier,
                    "first_path": first_document.path,
                    "first_line": first_document.line(first_entry.start),
                    "conflicting_fields": _conflicts(first_entry, entry),
                },
            )


def _integrity_findings(
    documents: list[BibDocument], input_findings: list[Finding], style: _StyleMacros
) -> list[Finding]:
    findings = list(input_findings)
    check = _IntegrityCheck(
        documents,
        complete=all(document.valid for document in documents) and not findings,
        style=style,
    )
    for document in documents:
        cancellation_point()
        if document.syntax_error is not None:
            findings.append(document.syntax_error)
        for record in document.records:
            findings.extend(check.findings(document, record))
    return findings


def inspect_bibliography_integrity(
    root: Path, main: str | None = None
) -> tuple[list[BibDocument], list[Finding]]:
    """The parsed documents of :func:`check_bibliography_integrity` with its findings."""
    selection = selected_bibliography(root, main)
    documents, findings = _load_bibliographies(root, selection.resources)
    style = _style_macros(root, selection.styles)
    return documents, _integrity_findings(documents, findings, style)


def check_bibliography_integrity(root: Path, main: str | None = None) -> list[Finding]:
    """Inspect UTF-8 .bib files without changing them or accessing the network.

    When ``main`` names a source whose selected literal graph declares resolvable
    bibliography resources, only those resources are inspected, so duplicate keys
    and DOIs are reported within that declared set. Without such a selection every
    .bib file below ``root`` is inspected, including unreferenced spare copies.

    BibTeX styles define string macros of their own. A macro is therefore a certain
    error only when the style is known in full: a standard style, a .bst file in the
    project, or no BibTeX style at all (biblatex). Otherwise it is reported as
    inconclusive and names the style that may define it.

    No findings means no problem was found by these checks, not that metadata,
    macro expansion order, style requirements, or rendered citations are valid.
    """
    return inspect_bibliography_integrity(root, main)[1]


# --- Citation coverage and field details ------------------------------------------


_ENTRY_TYPE_OR_ANY = TextFormat(
    re.compile(rf"\*|{LOWERCASE_NAME.pattern}"), "a lowercase entry type or '*'"
)
# A field name, or alternatives of which one suffices, such as "doi|url".
_FIELD_OR_ALTERNATIVES = TextFormat(
    re.compile(rf"{LOWERCASE_NAME.pattern}(?:\|{LOWERCASE_NAME.pattern})*"),
    "a lowercase field name or alternatives such as 'doi|url'",
)


def _require_entry_type(name: str, value: object) -> None:
    require_text(
        name, value, expected=_ENTRY_TYPE_OR_ANY.description, pattern=_ENTRY_TYPE_OR_ANY.pattern
    )


def _require_field_alternatives(name: str, specs: object) -> None:
    require_unique_strings(name, specs, item_format=_FIELD_OR_ALTERNATIVES)
    if not isinstance(specs, tuple) or not specs:
        raise SettingsError(f"{name} must list at least one field", (name,))
    for spec in specs:
        alternatives = spec.split("|")
        if len(set(alternatives)) != len(alternatives):
            raise SettingsError(f"{name} entry {spec!r} repeats an alternative", (name,))


@dataclass(frozen=True)
class BibliographyOptions:
    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {"fuzzy_duplicate_threshold": NONZERO_FRACTION}

    check_citation_coverage: bool = True
    check_uncited_entries: bool = True
    all_roots: bool = False
    required_fields: tuple[tuple[str, tuple[str, ...]], ...] = ()
    doi_entry_types: tuple[str, ...] = ()
    check_page_ranges: bool = True
    check_urls: bool = True
    check_capitalization: bool = False
    check_fuzzy_duplicates: bool = False
    fuzzy_duplicate_threshold: float = 0.93

    def __post_init__(self) -> None:
        validate_scalars(self)
        require_unique_strings(
            "doi_entry_types", self.doi_entry_types, item_format=_ENTRY_TYPE_OR_ANY
        )
        require_pairs(
            "required_fields",
            self.required_fields,
            first=_require_entry_type,
            second=_require_field_alternatives,
        )

    @property
    def any_enabled(self) -> bool:
        """Whether any check runs; ``all_roots`` and the threshold only refine them."""
        return any(
            (
                self.check_citation_coverage,
                self.check_uncited_entries,
                self.required_fields,
                self.doi_entry_types,
                self.check_page_ranges,
                self.check_urls,
                self.check_capitalization,
                self.check_fuzzy_duplicates,
            )
        )


_DEFAULT_OPTIONS = BibliographyOptions()


@dataclass(frozen=True, slots=True)
class _Scope:
    """What the coverage and field findings describe; repeated in each summary's details."""

    description: str
    documents: tuple[str, ...]
    resources: tuple[str, ...]

    def details(self, **extra: object) -> dict[str, object]:
        """The scope details; ``extra`` adds keys or replaces values in place."""
        return {
            "scope": self.description,
            "documents": list(self.documents),
            "resources": list(self.resources),
            "source_expansion": "not performed",
            "network": "not used",
            **extra,
        }


def _finding(
    rule: str,
    message: str,
    status: Status,
    *,
    reference: LocatedEntry | None = None,
    severity: Severity = "warning",
    evidence: EvidenceKind = EvidenceKind.DERIVED,
    incomplete: bool = False,
    details: dict[str, object] | None = None,
) -> Finding:
    data = dict(details or {})
    if reference:
        data.update({"entry": reference.entry.key, "entry_type": reference.entry.entry_type})
    return Finding(
        f"bibliography.{rule}",
        message,
        severity="info" if status == "passed" else severity,
        status=status,
        path=reference.document.path if reference else None,
        line=reference.document.line(reference.entry.start) if reference else None,
        evidence_kind=evidence,
        incomplete=incomplete,
        details=data,
    )


@dataclass(slots=True)
class _Results:
    """Findings in report order; summaries that only restate reported source errors wait.

    A missing input or an ambiguous main file makes every bibliography summary
    inconclusive with the same reason. The source checks report that error;
    repeating it once per bibliography check buries it, so such summaries become
    one note. Summaries with any other uncertainty, and every per-item finding, stay.
    """

    scope: _Scope
    findings: list[Finding] = field(default_factory=list)
    # Held-back summaries with the uncertainty messages each lists.
    restated: list[tuple[Finding, list[str]]] = field(default_factory=list)

    def summarize(
        self,
        rule: str,
        items: list[Finding],
        checked: int,
        uncertainties: list[Uncertainty],
        *,
        description: str,
        severity: Severity = "warning",
        details: dict[str, object] | None = None,
    ) -> None:
        """Add a check's item findings and its summary for ``checked`` supported items."""
        base = self.scope.details() if details is None else details
        self.findings.extend(items)
        if uncertainties:
            bounded = bounded_uncertainties(uncertainties)
            messages = uncertainty_messages(bounded)
            summary = _finding(
                rule,
                f"{description} is incomplete: {uncertainties[0].message}.",
                "inconclusive",
                severity=severity,
                incomplete=True,
                details={**base, "checked": checked, "uncertainty": messages},
            )
            if all(item.already_reported for item in bounded):
                self.restated.append((summary, messages))
            else:
                self.findings.append(summary)
        elif items:
            return
        elif not checked:
            # Reporting "passed" for an empty inspection would overstate the evidence.
            self.findings.append(
                _finding(
                    rule,
                    "No supported items to check.",
                    "not_applicable",
                    severity="info",
                    details={**base, "checked": 0},
                )
            )
        else:
            self.findings.append(
                _finding(
                    rule,
                    f"{description}: no issue found in {checked} supported item(s).",
                    "passed",
                    details={**base, "checked": checked},
                )
            )

    def report(self) -> list[Finding]:
        if not self.restated:
            return self.findings
        codes = list(dict.fromkeys(summary.code or summary.rule for summary, _ in self.restated))
        reported = sorted({message for _, messages in self.restated for message in messages})
        # An unavailable selection only follows from the causes; name a cause instead.
        reported = [reason for reason in reported if reason != _SELECTION_UNAVAILABLE] or reported
        note = Finding(
            "bibliography.source_error",
            f"Bibliography checks {', '.join(codes)} were inconclusive because of a source "
            f"error reported above: {reported[0].rstrip('.')}.",
            severity="warning",
            status="inconclusive",
            evidence_kind=EvidenceKind.DERIVED,
            suggestion="Fix the reported source error; these bibliography checks then run in full.",
            details=self.scope.details(inconclusive_checks=codes, uncertainty=reported),
        )
        return [*self.findings, note]


def _load_resources(root: Path, names: set[str]) -> tuple[list[BibDocument], list[Uncertainty]]:
    """Parse the declared resources within the shared resource-set budget."""
    documents: list[BibDocument] = []
    uncertainties: list[Uncertainty] = []
    reader = BibFileReader(max_total_bytes=MAX_RESOURCES_TOTAL_BYTES)
    entries = 0
    for index, name in enumerate(sorted(names)):
        cancellation_point()
        if index >= MAX_RESOURCE_FILES:
            uncertainties.append(
                Uncertainty(f"Bibliography resource count exceeds {MAX_RESOURCE_FILES}")
            )
            break
        path = root / name
        try:
            if path.is_symlink() or not path.is_file():
                raise ValueError("is missing or is a symbolic link")
            if path.stat().st_size > reader.allowance:
                raise ValueError(_over_budget())
            document = reader.read(path, name)
            if document is None:
                raise ValueError(_over_budget())
        except (OSError, UnicodeError, ValueError) as error:
            uncertainties.append(Uncertainty(f"{name} {describe_read_failure(error)}"))
            continue
        if not document.valid:
            uncertainties.append(Uncertainty(f"{name}: bibliography syntax is incomplete"))
        entries += len(document.records)
        if entries > MAX_BIB_ENTRIES:
            uncertainties.append(Uncertainty(f"Bibliography entry count exceeds {MAX_BIB_ENTRIES}"))
            break
        documents.append(document)
    return documents, uncertainties


def _fields(entry: Entry, name: str) -> list[Field]:
    return [item for item in entry.fields if item.name == name]


_Presence = Literal["present", "unknown", "missing"]


def _content_state(entry: Entry, names: list[str]) -> _Presence:
    uncertain = False
    for name in names:
        matches = _fields(entry, name)
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


def _missing_citations(
    graph: CitationGraph, index: EntryIndex, uncertain: list[Uncertainty]
) -> list[Finding]:
    details = uncertainty_messages(bounded_uncertainties(uncertain))
    missing: list[Finding] = []
    for key, path, line in graph.citations:
        cancellation_point()
        if key in index.keys or key in graph.inline_keys:
            continue
        missing.append(
            Finding(
                "bibliography.citation_coverage",
                f"Citation key {key!r} has no entry in this root's inspected resources.",
                severity="warning" if uncertain else "error",
                status="inconclusive" if uncertain else "failed",
                path=path,
                line=line,
                evidence_kind=EvidenceKind.DERIVED,
                details={
                    "key": key,
                    "document": graph.main,
                    "resources": sorted(graph.resources),
                    "uncertainty": details,
                },
            )
        )
    return missing


def _uncited_entries(
    references: list[LocatedEntry],
    used: set[tuple[str, int]],
    uncertainties: list[Uncertainty],
    results: _Results,
) -> None:
    candidates = [item for item in references if item.identity not in used]
    unused: list[Finding] = []
    if candidates:
        cancellation_point()
        # One aggregate keeps a shared master bibliography readable; every
        # candidate stays individually addressable through the details.
        rows = [
            {
                "entry": item.entry.key,
                "entry_type": item.entry.entry_type,
                "path": item.document.path,
                "line": item.document.line(item.entry.start),
            }
            for item in candidates
        ]
        listed = ", ".join(item.entry.key for item in candidates[:_MAX_LISTED_ENTRIES])
        unused.append(
            Finding(
                "bibliography.uncited_entries",
                f"{len(candidates)} of {len(references)} entries have no scanned use: "
                + listed
                + more_suffix(len(candidates) - _MAX_LISTED_ENTRIES),
                severity="warning",
                status="inconclusive" if uncertainties else "failed",
                path=candidates[0].document.path,
                line=candidates[0].document.line(candidates[0].entry.start),
                evidence_kind=EvidenceKind.HEURISTIC,
                incomplete=bool(uncertainties),
                details=results.scope.details(
                    entries=rows,
                    uncertainty=uncertainty_messages(bounded_uncertainties(uncertainties)),
                ),
            )
        )
    results.summarize(
        "uncited_entries",
        unused,
        len(references),
        uncertainties,
        description="Citation and relationship reachability",
    )


def _coverage(
    graphs: list[CitationGraph],
    references: list[LocatedEntry],
    uncertainties: list[Uncertainty],
    options: BibliographyOptions,
    results: _Results,
) -> None:
    used: set[tuple[str, int]] = set()
    unused_uncertainties = list(uncertainties)
    for graph in graphs:
        # Keys resolve per root, against only the resources that root declares.
        index = index_entries(
            [item for item in references if item.document.path in graph.resources]
        )
        uncertain = [*uncertainties, *graph.uncertainties, *index.uncertainties]
        unused_uncertainties.extend([*uncertain, *graph.unused_uncertainties])
        if options.check_citation_coverage:
            results.summarize(
                "citation_coverage",
                _missing_citations(graph, index, uncertain),
                len(graph.citations),
                uncertain,
                description="Supported literal citation coverage",
                details=results.scope.details(
                    document=graph.main, resources=sorted(graph.resources)
                ),
            )
        used.update(index.reachable(graph))
    if options.check_uncited_entries:
        _uncited_entries(references, used, unused_uncertainties, results)


class _Verdict(NamedTuple):
    """The outcome of one literal field syntax check."""

    status: Status
    reason: str = ""
    tokens: tuple[str, ...] = ()


def _page_verdict(value: str) -> _Verdict:
    if not value:
        return _Verdict("failed", "empty page field")
    if any(character in value for character in "{}"):
        return _Verdict("inconclusive", "brace markup")
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
                return _Verdict("failed", "descending page endpoints")
        elif not part or re.fullmatch(r"[\d\s–—-]+", part):
            return _Verdict("failed", "malformed numeric page range")
        else:
            unknown = True
    return _Verdict("inconclusive", "unsupported page notation") if unknown else _Verdict("passed")


def _url_verdict(value: str) -> _Verdict:
    if any(character in value for character in "{}"):
        return _Verdict("inconclusive", "brace markup")
    if not value or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127 for character in value
    ):
        return _Verdict("failed", "empty URL, whitespace or control characters")
    if re.search(r"%(?![0-9a-fA-F]{2})", value) or any(char in value for char in '<>"'):
        return _Verdict("failed", "invalid percent escape or reserved delimiter")
    try:
        parts = urlsplit(value)
        if not parts.scheme:
            return _Verdict("failed", "an absolute URL scheme is missing")
        if parts.scheme.lower() not in {"http", "https", "ftp"}:
            return _Verdict(
                "inconclusive", "URL scheme is outside the supported HTTP/HTTPS/FTP subset"
            )
        if not parts.netloc or not parts.hostname:
            return _Verdict("failed", "a network host is missing")
        parts.hostname.encode("idna")
        _ = parts.port
    except (ValueError, UnicodeError):
        return _Verdict("failed", "host or port syntax is malformed")
    return _Verdict("passed")


def _unprotected_tokens(value: str) -> list[str]:
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


def _capitalization_verdict(value: str) -> _Verdict:
    tokens = _unprotected_tokens(value)
    if tokens:
        return _Verdict("failed", "case-sensitive tokens may need brace protection", tuple(tokens))
    return _Verdict("passed")


# Literal field syntax checks: the field each reads, its verdict and its evidence kind.
_FIELD_CHECKS: dict[str, tuple[str, Callable[[str], _Verdict], EvidenceKind]] = {
    "page_ranges": ("pages", _page_verdict, EvidenceKind.DERIVED),
    "url_syntax": ("url", _url_verdict, EvidenceKind.DERIVED),
    "capitalization": ("title", _capitalization_verdict, EvidenceKind.HEURISTIC),
}
_METADATA_DESCRIPTIONS = {
    "required_fields": "Configured field presence",
    "missing_doi": "Requested DOI presence",
    "page_ranges": "Literal page-range syntax",
    "url_syntax": "Literal URL syntax (offline)",
    "capitalization": "Style-dependent capitalization advisory",
}


def _required_field(reference: LocatedEntry, spec: str, *, uncertain: bool) -> Finding | None:
    state = _content_state(reference.entry, spec.split("|"))
    if state == "missing" and uncertain:
        state = "unknown"
    if state == "present":
        return None
    return _finding(
        "required_fields",
        f"Entry {reference.entry.key!r}: required field {spec!r} is "
        + ("missing or empty." if state == "missing" else "not statically established."),
        "failed" if state == "missing" else "inconclusive",
        reference=reference,
        severity="error",
        details={"requirement": spec},
    )


def _doi_presence(reference: LocatedEntry) -> Finding | None:
    state = _content_state(reference.entry, ["doi"])
    if state == "present":
        return None
    return _finding(
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
        _Verdict("inconclusive", "macro, concatenation, markup or repeated field")
        if value is None
        else verdict_of(value)
    )
    if verdict.status == "passed":
        return None
    return _finding(
        rule,
        f"Entry {reference.entry.key!r}: {name} check found {verdict.reason}.",
        verdict.status,
        reference=reference,
        evidence=evidence,
        details={"field": name, **({"tokens": list(verdict.tokens)} if verdict.tokens else {})},
    )


def _metadata(
    references: list[LocatedEntry],
    uncertainties: list[Uncertainty],
    options: BibliographyOptions,
    results: _Results,
) -> None:
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
            if enabled[rule] and (bib_fields := _fields(entry, name)):
                add(rule, _field_syntax(reference, rule, bib_fields))
    for rule, row in rows.items():
        results.summarize(
            rule,
            row,
            counts[rule],
            uncertainties,
            description=_METADATA_DESCRIPTIONS[rule],
            severity="error" if rule == "required_fields" else "warning",
        )


def _normalized(value: str) -> str:
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
            bib_fields = _fields(reference.entry, name)
            if name == "year" and not bib_fields:
                bib_fields = _fields(reference.entry, "date")
            value = field_literal(bib_fields[0]) if len(bib_fields) == 1 else None
            if value is not None and len(value) <= _MAX_FUZZY_TEXT:
                values[name] = _normalized(value)
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
        doi_fields = _fields(reference.entry, "doi")
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


def _fuzzy(
    references: list[LocatedEntry],
    uncertainties: list[Uncertainty],
    options: BibliographyOptions,
    results: _Results,
) -> None:
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
            continue  # Exact DOI identity belongs to BIB007, not a title heuristic.
        similarity = SequenceMatcher(None, current.title, previous.title, autojunk=False).ratio()
        if similarity >= options.fuzzy_duplicate_threshold:
            first = previous.reference
            findings.append(
                _finding(
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
    results.summarize(
        "fuzzy_duplicates",
        findings,
        comparisons,
        uncertain,
        description="Likely-work duplicate search",
    )


def _all_root_graphs(
    root: Path, inspection: SourceGraph, selected: CitationGraph, uncertainties: list[Uncertainty]
) -> list[CitationGraph]:
    """One graph per discovered root; ``selected`` is the graph of ``inspection``."""
    for item in inspection.findings:
        if (
            item.path
            and Path(item.path).suffix.lower() in ROOT_SUFFIXES
            and item.rule in UNPARSED_SOURCE_RULES
        ):
            uncertainties.append(
                Uncertainty(f"All-root discovery is incomplete: {item.path}: {item.message}")
            )
    if len(inspection.roots) > _MAX_ROOTS:
        uncertainties.append(Uncertainty(f"Document root count exceeds {_MAX_ROOTS}"))
    graphs: list[CitationGraph] = []
    for main in inspection.roots[:_MAX_ROOTS]:
        cancellation_point()
        graphs.append(
            selected
            if inspection.main == main
            else build_citation_graph(SourceGraph.build(root, main))
        )
    return graphs or [selected]


def check_bibliography_coverage_and_fields(
    root: Path,
    main: str | None = None,
    options: BibliographyOptions = _DEFAULT_OPTIONS,
) -> list[Finding]:
    """Inspect offline evidence without writing files or resolving remote metadata.

    Metadata in a bibliography-only directory can be inspected without a TeX root;
    citation coverage then remains inconclusive. With TeX roots, only selected
    resources are checked. ``all_roots`` unions usage but resolves keys separately
    for each root. Passed summaries describe only the supported literal subset.
    """
    if not isinstance(options, BibliographyOptions):
        raise PreparationError("Bibliography checks require BibliographyOptions")
    if not options.any_enabled:
        return []
    require_directory(root, "Bibliography input")
    inspection = SourceGraph.build(root, main)
    graphs = [build_citation_graph(inspection)]
    uncertainties: list[Uncertainty] = []
    if options.all_roots:
        graphs = _all_root_graphs(root, inspection, graphs[0], uncertainties)
    names = set().union(*(graph.resources for graph in graphs))
    metadata_uncertainties = list(uncertainties)
    standalone = not inspection.roots and main is None
    if not names and standalone:
        # A standalone .bib command has no manuscript resource selection. Its
        # explicitly stated metadata scope is every local bibliography file.
        names = {name for name in inspection.files if Path(name).suffix.lower() == ".bib"}
    elif not names and any(graph.uncertainties for graph in graphs):
        graph_uncertainties = [item for graph in graphs for item in graph.uncertainties]
        # Derived only from the graph uncertainties, so it restates nothing new.
        metadata_uncertainties.append(
            Uncertainty(
                _SELECTION_UNAVAILABLE,
                already_reported=all(item.already_reported for item in graph_uncertainties),
            )
        )
    if not standalone:
        metadata_uncertainties.extend(item for graph in graphs for item in graph.uncertainties)
    documents, input_uncertainties = _load_resources(root, names)
    uncertainties.extend(input_uncertainties)
    metadata_uncertainties.extend(input_uncertainties)
    references = located_entries(document for document in documents if document.valid)
    if standalone:
        description = "bibliography-only directory; citation scope unavailable"
    elif options.all_roots:
        description = "all literal document roots"
    else:
        description = "selected literal document graph"
    results = _Results(
        _Scope(
            description,
            tuple(graph.main for graph in graphs if graph.main),
            tuple(sorted(names)),
        )
    )
    _coverage(graphs, references, uncertainties, options, results)
    _metadata(references, metadata_uncertainties, options, results)
    if options.check_fuzzy_duplicates:
        _fuzzy(references, metadata_uncertainties, options, results)
    return results.report()
