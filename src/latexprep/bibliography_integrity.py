"""Offline integrity checks of the .bib files themselves.

This module owns the checks that need no manuscript: syntax, duplicate keys and DOIs,
repeated fields, undefined string macros, missing relationship targets and DOI syntax.
It decides which files are read (the selected document's declared resources, or every
.bib file below the root) and reports a file it cannot read as a finding. It does not
relate citations to entries (``bibliography_checks``), contact URLs or propose edits.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

from .analysed_source import AnalysedSource
from .bibliography_files import (
    BibliographyBudget,
    BibliographyProblem,
    ProblemKind,
    discover_bibliographies,
    read_declared_bibliographies,
)
from .bibliography_results import located_finding, syntax_finding
from .bibliography_styles import MONTH_MACROS, StyleMacros, style_macros
from .bibtex_syntax import (
    MAX_BIB_FILE_BYTES,
    BibDocument,
    Declaration,
    Entry,
    Field,
    mebibytes,
    plain_literal,
    read_doi,
)
from .cancellation import cancellation_point
from .citation_graph import RELATIONSHIP_FIELDS, require_directory, selected_bibliography
from .findings import Finding, Severity, Status


def _input_finding(problem: BibliographyProblem) -> Finding:
    """Build the finding for a bibliography file that was not read."""
    if problem.kind is ProblemKind.LINKED:
        return Finding(
            "bibliography.input",
            "Symbolic-link bibliographies are not read or normalized.",
            severity="error",
            status="skipped",
            path=problem.name,
        )
    return Finding(
        "bibliography.input",
        f"Bibliography {problem.message}, so it was not inspected.",
        severity="error",
        status="inconclusive",
        path=problem.name,
        suggestion=(
            "Make the file readable, save it as UTF-8 and keep it within "
            f"{mebibytes(MAX_BIB_FILE_BYTES)}."
        ),
    )


def _load_bibliographies(
    root: Path, only: set[str] | None
) -> tuple[list[BibDocument], list[Finding]]:
    """Parse every .bib file below ``root``, or only the named ones.

    Args:
        root: The project directory.
        only: The project-relative names of the files to read, or ``None`` to read every
            .bib file below ``root``.

    Returns:
        The documents that were read, valid or not, and a finding for each file that
        was not.

    Raises:
        PreparationError: ``root`` is not a directory.
    """
    require_directory(root, "Bibliography input")
    names = discover_bibliographies(root) if only is None else only
    load = read_declared_bibliographies(root, names, BibliographyBudget())
    return load.documents, [_input_finding(problem) for problem in load.problems]


def _defined_keys(documents: list[BibDocument]) -> set[str]:
    """Collect every entry key and literal ``ids`` alias of ``documents``."""
    keys = {entry.key for document in documents for entry in document.entries}
    # biblatex's literal ids field supplies alternate entry keys.
    for document in documents:
        cancellation_point()
        for record in document.records:
            for bib_field in record.fields:
                if bib_field.name == "ids" and (value := plain_literal(bib_field)) is not None:
                    keys.update(key.strip() for key in value.split(",") if key.strip())
    return keys


def _field_values(entry: Entry) -> dict[str, tuple[tuple[str, str], ...]]:
    """Map each field name of ``entry`` to the kind and value of each of its atoms."""
    return {
        item.name: tuple((atom.kind, atom.value) for atom in item.atoms) for item in entry.fields
    }


def _conflicts(first: Entry, second: Entry) -> list[str]:
    """List, sorted, the fields other than ``doi`` that both entries hold with different values."""
    first_values = _field_values(first)
    second_values = _field_values(second)
    return sorted(
        name
        for name in first_values.keys() & second_values.keys()
        if name != "doi" and first_values[name] != second_values[name]
    )


class _IntegrityCheck:
    """Cross-file consistency of keys, string macros, relationships and DOIs.

    A missing macro definition or relationship target is a certain error only when
    ``complete`` is true and the bibliography style is known in full; otherwise it is an
    inconclusive warning. The first entry seen with a key or DOI is the one that later
    duplicates are reported against, so records are checked in document order.

    Args:
        documents: The documents to check, whose keys, ``ids`` aliases and ``@string``
            names count as defined.
        complete: Whether every inspected file parsed.

    Attributes:
        style: The macros the bibliography style is known to define.
        macro_names: The month macros, the style's macros and the names that the
            documents' ``@string`` declarations define.
        keys: Every entry key and literal ``ids`` alias of the documents.
        severity: The severity of a missing macro definition or relationship target.
        status: The status of a missing macro definition or relationship target.
        seen_keys: The first entry seen with each key, and its document.
        seen_dois: The first entry seen with each DOI, and its document.
    """

    def __init__(
        self, documents: list[BibDocument], *, complete: bool, style: StyleMacros | None = None
    ) -> None:
        style = style or StyleMacros()
        self.style = style
        self.macro_names = (
            MONTH_MACROS
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
        """Check one record of ``document``.

        A declaration is checked for undefined macros; an entry for a duplicate key,
        repeated fields, undefined macros, missing relationship targets and its DOI.

        Args:
            document: The document that holds the record.
            record: The entry or declaration to check.

        Yields:
            The record's findings.
        """
        if isinstance(record, Declaration):
            for bib_field in record.fields:
                yield from self._undefined_macros(document, bib_field)
            return
        yield from self._duplicate_key(document, record)
        names: set[str] = set()
        for bib_field in record.fields:
            if bib_field.name in names:
                yield located_finding(
                    document,
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
        """Report ``entry`` when an earlier entry has its key, or remember it as the first.

        Args:
            document: The document that holds the entry.
            entry: The entry to check.

        Yields:
            A duplicate-key finding with the first entry's location and the fields
            whose values differ.
        """
        first = self.seen_keys.get(entry.key)
        if first is None:
            self.seen_keys[entry.key] = (document, entry)
            return
        first_document, first_entry = first
        yield located_finding(
            document,
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
        """Report each string macro in ``bib_field`` that no known definition covers.

        Args:
            document: The document that holds the field.
            bib_field: The field whose macros are checked.

        Yields:
            One finding per undefined macro, naming the style that may define it when
            the style is uncertain.
        """
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
                yield located_finding(
                    document,
                    "bibliography.undefined_macro",
                    message,
                    atom.start,
                    severity=self.severity,
                    status=self.status,
                    suggestion=suggestion,
                )

    def _relationship(self, document: BibDocument, bib_field: Field) -> Iterator[Finding]:
        """Report the targets of a relationship field that no key or alias defines.

        ``crossref`` and ``xref`` name one target; the other relationship fields list
        targets separated by commas.

        Args:
            document: The document that holds the field.
            bib_field: The relationship field.

        Yields:
            An inconclusive finding when the value is not a plain literal, otherwise one
            finding per missing target.
        """
        value = plain_literal(bib_field)
        if value is None:
            yield located_finding(
                document,
                "bibliography.relationship_unresolved",
                f"Field {bib_field.name!r} is not a plain literal; relationships are unverified.",
                bib_field.start,
                status="inconclusive",
            )
            return
        targets = [value] if bib_field.name in {"crossref", "xref"} else value.split(",")
        for target in targets:
            if target.strip() not in self.keys:
                yield located_finding(
                    document,
                    "bibliography.missing_relationship",
                    f"Field {bib_field.name!r} refers to missing key {target.strip()!r}.",
                    bib_field.start,
                    severity=self.severity,
                    status=self.status,
                )

    def _doi(self, document: BibDocument, entry: Entry, bib_field: Field) -> Iterator[Finding]:
        """Check a DOI's syntax and report a DOI that an earlier entry already has.

        Args:
            document: The document that holds the entry.
            entry: The entry that holds the field.
            bib_field: The DOI field.

        Yields:
            An inconclusive finding for markup that cannot be read, an invalid-DOI
            finding, or a duplicate-DOI finding with the first entry's location and the
            fields whose values differ.
        """
        reading = read_doi(bib_field)
        if reading.uncertainty:
            yield located_finding(
                document,
                "bibliography.doi_unresolved",
                reading.uncertainty,
                bib_field.start,
                status="inconclusive",
                suggestion="Inspect this DOI manually; it remains unchanged.",
            )
            return
        if reading.identifier is None:
            yield located_finding(
                document,
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
            yield located_finding(
                document,
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
    documents: list[BibDocument], input_findings: list[Finding], style: StyleMacros
) -> list[Finding]:
    """Run the integrity checks over ``documents``.

    Args:
        documents: The documents that were read.
        input_findings: The findings for the files that were not read; any of them
            makes missing definitions and targets inconclusive.
        style: The macros the bibliography style is known to define.

    Returns:
        The input findings, then each document's syntax finding and record findings in
        document order.
    """
    findings = list(input_findings)
    check = _IntegrityCheck(
        documents,
        complete=all(document.valid for document in documents) and not findings,
        style=style,
    )
    for document in documents:
        cancellation_point()
        if (syntax := syntax_finding(document)) is not None:
            findings.append(syntax)
        for record in document.records:
            findings.extend(check.findings(document, record))
    return findings


def inspect_bibliography_integrity(
    root: Path, main: str | None = None, *, analysed: AnalysedSource | None = None
) -> tuple[list[BibDocument], list[Finding]]:
    """Return the parsed documents of :func:`check_bibliography_integrity` with its findings.

    Args:
        root: The project directory.
        main: The main source whose declared resources select the files, or ``None`` to
            inspect every .bib file below ``root``.
        analysed: A source analysis of the same root and main file shared with other
            checks; without one the checks build their own.

    Returns:
        The parsed documents and the findings.

    Raises:
        PreparationError: ``root`` is not a directory.
    """
    selection = selected_bibliography(root, main, analysed=analysed)
    documents, findings = _load_bibliographies(root, selection.resources)
    style = style_macros(root, selection.styles)
    return documents, _integrity_findings(documents, findings, style)


def check_bibliography_integrity(
    root: Path, main: str | None = None, *, analysed: AnalysedSource | None = None
) -> list[Finding]:
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

    Args:
        root: The project directory.
        main: The main source whose declared resources select the files, or ``None`` to
            inspect every .bib file below ``root``.
        analysed: The source analysis of the same root and main file, shared with other
            checks; without one the checks build their own.

    Returns:
        The findings.

    Raises:
        PreparationError: ``root`` is not a directory.
    """
    return inspect_bibliography_integrity(root, main, analysed=analysed)[1]
