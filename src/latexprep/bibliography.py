"""Conservative, offline BibTeX diagnostics and source-preserving DOI edits.

This is a bounded syntax reader, not a bibliography backend. It does not infer
required fields, expand macros, establish citation coverage, resolve identifiers,
or remove or merge entries. Normalization returns proposed bytes only.
"""

from __future__ import annotations

import difflib
import re
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path

from .manuscript import _commands_before_end, _reachable_sources
from .models import Change, Finding, PreparationError, Severity, Status
from .scheduler import cancellation_point
from .source import _Inspection, _items

_MAX_FILE_BYTES = 16 * 1024 * 1024
_MAX_NESTING = 256
_MAX_ENTRIES = 100_000
_IDENTIFIER = re.compile(r"[^\s\"#%'(),={}]+")
_ENTRY_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
_DOI_PREFIX = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_DOI = re.compile(r"10\.\d{4,9}/[!-~]+")
_MONTHS = frozenset("jan feb mar apr may jun jul aug sep oct nov dec".split())
_RELATIONSHIPS = frozenset({"crossref", "xref", "xdata", "related", "entryset"})
_RESOURCE_COMMANDS = frozenset({"bibliography", "addbibresource"})


@dataclass(frozen=True)
class _Atom:
    kind: str
    value: str
    start: int
    end: int


@dataclass(frozen=True)
class _Field:
    name: str
    start: int
    atoms: tuple[_Atom, ...]


@dataclass(frozen=True)
class _Entry:
    kind: str
    key: str | None
    start: int
    fields: tuple[_Field, ...]


@dataclass
class _Document:
    path: str
    text: str
    entries: list[_Entry] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    valid: bool = True
    newlines: list[int] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.newlines = [index for index, char in enumerate(self.text) if char == "\n"]

    def line(self, offset: int) -> int:
        return bisect_right(self.newlines, offset) + 1

    def finding(
        self,
        rule: str,
        message: str,
        offset: int,
        *,
        severity: Severity = "warning",
        status: Status = "failed",
        suggestion: str | None = None,
        details: dict[str, object] | None = None,
    ) -> Finding:
        return Finding(
            rule,
            message,
            severity=severity,
            status=status,
            path=self.path,
            line=self.line(offset),
            suggestion=suggestion,
            details=details or {},
        )


class _SyntaxError(Exception):
    def __init__(self, message: str, position: int) -> None:
        super().__init__(message)
        self.position = position


class _Parser:
    def __init__(self, document: _Document) -> None:
        self.document = document
        self.text = document.text
        self.position = 0

    def skip_space(self) -> None:
        while self.position < len(self.text):
            if self.position % 4096 == 0:
                cancellation_point()
            if self.text[self.position].isspace():
                self.position += 1
            elif self.text[self.position] == "%":
                newline = self.text.find("\n", self.position)
                self.position = len(self.text) if newline < 0 else newline + 1
            else:
                break

    def expect(self, character: str) -> None:
        self.skip_space()
        if self.position >= len(self.text) or self.text[self.position] != character:
            raise _SyntaxError(f"Expected {character!r}.", self.position)
        self.position += 1

    def identifier(self) -> tuple[str, int]:
        self.skip_space()
        start = self.position
        match = _IDENTIFIER.match(self.text, start)
        if match is None or match[0][0].isdigit():
            raise _SyntaxError("Expected a field or string-macro name.", start)
        self.position = match.end()
        return match[0], start

    def literal(self) -> _Atom:
        opening = self.text[self.position]
        start = self.position + 1
        self.position = start
        depth = 1 if opening == "{" else 0
        while self.position < len(self.text):
            if self.position % 4096 == 0:
                cancellation_point()
            char = self.text[self.position]
            if char == "\\":
                self.position += 2
                continue
            if char == "{":
                depth += 1
                if depth > _MAX_NESTING:
                    raise _SyntaxError("Brace nesting exceeds the supported limit.", self.position)
            elif char == "}":
                if depth == 0:
                    raise _SyntaxError("Unmatched brace inside a quoted value.", self.position)
                depth -= 1
                if opening == "{" and depth == 0:
                    end = self.position
                    self.position += 1
                    return _Atom("literal", self.text[start:end], start, end)
            elif char == '"' and opening == '"' and depth == 0:
                end = self.position
                self.position += 1
                return _Atom("literal", self.text[start:end], start, end)
            self.position += 1
        raise _SyntaxError("Unterminated braced or quoted value.", start - 1)

    def expression(self) -> tuple[_Atom, ...]:
        atoms: list[_Atom] = []
        while True:
            self.skip_space()
            if self.position >= len(self.text):
                raise _SyntaxError("Missing field value.", self.position)
            if self.text[self.position] in '{"':
                atoms.append(self.literal())
            else:
                start = self.position
                match = _IDENTIFIER.match(self.text, start)
                if match is None:
                    raise _SyntaxError("Expected a literal, number, or string macro.", start)
                self.position = match.end()
                token = match[0]
                if token[0].isdigit() and not token.isdigit():
                    raise _SyntaxError(
                        "A value starting with a number must be quoted or braced.", start
                    )
                atoms.append(
                    _Atom("number" if token.isdigit() else "macro", token, start, match.end())
                )
            self.skip_space()
            if self.position >= len(self.text) or self.text[self.position] != "#":
                return tuple(atoms)
            self.position += 1

    def fields(self, closing: str) -> tuple[_Field, ...]:
        fields: list[_Field] = []
        while True:
            self.skip_space()
            if self.position < len(self.text) and self.text[self.position] == closing:
                self.position += 1
                return tuple(fields)
            name, start = self.identifier()
            self.expect("=")
            fields.append(_Field(name.lower(), start, self.expression()))
            if self.position < len(self.text) and self.text[self.position] == closing:
                self.position += 1
                return tuple(fields)
            self.expect(",")

    def comment(self, opening: str) -> None:
        closing = "}" if opening == "{" else ")"
        depth = 1
        while self.position < len(self.text):
            if self.position % 4096 == 0:
                cancellation_point()
            char = self.text[self.position]
            if char == "\\":
                self.position += 2
                continue
            if char == opening:
                depth += 1
                if depth > _MAX_NESTING:
                    raise _SyntaxError(
                        "Comment nesting exceeds the supported limit.", self.position
                    )
            elif char == closing:
                depth -= 1
                if depth == 0:
                    self.position += 1
                    return
            self.position += 1
        raise _SyntaxError("Unterminated @comment block.", self.position)

    def parse(self) -> None:
        try:
            while self.position < len(self.text):
                cancellation_point()
                self.skip_space()
                if self.position >= len(self.text):
                    break
                if self.text[self.position] != "@":
                    # BibTeX permits free text outside entries.
                    self.position += 1
                    continue
                start = self.position
                self.position += 1
                match = _ENTRY_TYPE.match(self.text, self.position)
                if match is None:
                    raise _SyntaxError("Expected an entry type after '@'.", start)
                kind = match[0].lower()
                self.position = match.end()
                self.skip_space()
                if self.position >= len(self.text) or self.text[self.position] not in "{(":
                    raise _SyntaxError("Expected '{' or '(' after the entry type.", self.position)
                opening = self.text[self.position]
                closing = "}" if opening == "{" else ")"
                self.position += 1
                if kind == "comment":
                    self.comment(opening)
                    continue
                if len(self.document.entries) >= _MAX_ENTRIES:
                    raise _SyntaxError("Entry count exceeds the supported limit.", start)
                key = None
                if kind == "preamble":
                    fields = (_Field("preamble", self.position, self.expression()),)
                    if self.position < len(self.text) and self.text[self.position] == ",":
                        self.position += 1
                    self.expect(closing)
                elif kind == "string":
                    fields = self.fields(closing)
                    if len(fields) != 1:
                        raise _SyntaxError("An @string declaration must define one macro.", start)
                else:
                    self.skip_space()
                    key_start = self.position
                    while (
                        self.position < len(self.text)
                        and self.text[self.position] not in ",})%"
                        and not self.text[self.position].isspace()
                    ):
                        self.position += 1
                    key = self.text[key_start : self.position]
                    if not key or any(char in '{#="' for char in key):
                        raise _SyntaxError("Missing or unsupported citation key.", key_start)
                    self.skip_space()
                    if self.position < len(self.text) and self.text[self.position] == closing:
                        self.position += 1
                        fields = ()
                    else:
                        self.expect(",")
                        fields = self.fields(closing)
                self.document.entries.append(_Entry(kind, key, start, fields))
        except _SyntaxError as error:
            self.document.valid = False
            self.document.findings.append(
                self.document.finding(
                    "bibliography.syntax",
                    str(error),
                    min(error.position, len(self.text)),
                    severity="error",
                    suggestion=(
                        "Repair this entry; the remaining file was not parsed or normalized."
                    ),
                )
            )


def _resource_declarations(
    inspection: _Inspection, selected: set[str]
) -> tuple[set[str], list[str]]:
    """Resolve literal bibliography resource declarations in the selected sources."""
    resources: set[str] = set()
    reasons: list[str] = []
    for name in sorted(selected):
        cancellation_point()
        for command in _commands_before_end(inspection.sources[name]):
            if command.name not in _RESOURCE_COMMANDS or not command.arguments:
                continue
            for argument in _items(command.arguments[0], command.name == "bibliography"):
                target = inspection.references.get((name, argument.start, argument.end))
                if target and target[1] in _RESOURCE_COMMANDS:
                    resources.add(target[0])
                else:
                    reasons.append(
                        f"{name}:{command.line}: bibliography resource could not be resolved"
                    )
    return resources, reasons


def _selected_resources(root: Path, main: str | None) -> set[str] | None:
    """Return the resources one selected literal graph declares, or None if unavailable.

    An unselectable main source or a declaration that cannot be resolved keeps the
    documented whole-tree scope instead of silently narrowing the inspected files.
    """
    if main is None:
        return None
    inspection = _Inspection(root, main)
    if inspection.main is None:
        return None
    resources, _ = _resource_declarations(inspection, _reachable_sources(inspection))
    return resources or None


def _load(root: Path, only: set[str] | None = None) -> tuple[list[_Document], list[Finding]]:
    if not root.is_dir():
        raise PreparationError(f"Bibliography input is not a directory: {root}")
    documents: list[_Document] = []
    findings: list[Finding] = []
    paths = (
        sorted(
            (path for path in root.rglob("*") if path.suffix.lower() == ".bib"),
            key=lambda path: path.relative_to(root).as_posix(),
        )
        if only is None
        else [root / name for name in sorted(only)]
    )
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
            with path.open("rb") as stream:
                data = stream.read(_MAX_FILE_BYTES + 1)
            if len(data) > _MAX_FILE_BYTES:
                raise ValueError(f"Bibliography exceeds the {_MAX_FILE_BYTES}-byte parser limit.")
            text = data.decode("utf-8")
        except (OSError, UnicodeError, ValueError) as error:
            findings.append(
                Finding(
                    "bibliography.input",
                    f"Bibliography could not be inspected: {error}",
                    severity="error",
                    status="inconclusive",
                    path=relative,
                    suggestion=(
                        "Use a readable UTF-8 bibliography within the documented parser limit."
                    ),
                )
            )
            continue
        document = _Document(relative, text)
        _Parser(document).parse()
        documents.append(document)
    return documents, findings


def _plain_value(bib_field: _Field) -> str | None:
    if len(bib_field.atoms) != 1 or bib_field.atoms[0].kind != "literal":
        return None
    value = bib_field.atoms[0].value.strip()
    return None if any(char in value for char in "{}\\") else value


def _doi_value(bib_field: _Field) -> tuple[str | None, tuple[int, int] | None, str | None]:
    """Return the identifier, removable prefix span, and any uncertainty reason."""
    if len(bib_field.atoms) != 1 or bib_field.atoms[0].kind != "literal":
        return None, None, "A macro, number, or concatenated value cannot be normalized safely."
    atom = bib_field.atoms[0]
    left = len(atom.value) - len(atom.value.lstrip())
    right = len(atom.value.rstrip())
    # Only complete extra brace wrappers can surround a recognized literal DOI.
    # Any braces remaining inside the candidate make it unresolved below.
    while left < right and atom.value[left] == "{" and atom.value[right - 1] == "}":
        left += 1
        right -= 1
        while left < right and atom.value[left].isspace():
            left += 1
        while right > left and atom.value[right - 1].isspace():
            right -= 1
    value = atom.value[left:right]
    prefix = _DOI_PREFIX.match(value)
    identifier = value[prefix.end() :] if prefix else value
    if any(char in value for char in "{}\\%") or not value.isascii():
        return None, None, "DOI markup, escapes, or non-ASCII text requires manual inspection."
    if prefix and value.lower().startswith("http") and any(char in identifier for char in "?#"):
        return None, None, "A DOI URL with a query or fragment has an ambiguous identifier."
    if _DOI.fullmatch(identifier) is None or any(char in identifier for char in '<>"'):
        return None, None, None
    span = (atom.start + left, atom.start + left + prefix.end()) if prefix else None
    return identifier, span, None


def _field_values(entry: _Entry) -> dict[str, tuple[tuple[str, str], ...]]:
    return {
        item.name: tuple((atom.kind, atom.value) for atom in item.atoms) for item in entry.fields
    }


def _conflicts(first: _Entry, second: _Entry) -> list[str]:
    first_values = _field_values(first)
    second_values = _field_values(second)
    return sorted(
        name
        for name in first_values.keys() & second_values.keys()
        if name != "doi" and first_values[name] != second_values[name]
    )


def _check(documents: list[_Document], findings: list[Finding]) -> list[Finding]:
    findings = list(findings)
    macro_names = _MONTHS | {
        bib_field.name
        for document in documents
        for entry in document.entries
        if entry.kind == "string"
        for bib_field in entry.fields
    }
    keys = {
        entry.key for document in documents for entry in document.entries if entry.key is not None
    }
    # biblatex's literal ids field supplies alternate entry keys.
    for document in documents:
        cancellation_point()
        for entry in document.entries:
            for bib_field in entry.fields:
                if bib_field.name == "ids" and (value := _plain_value(bib_field)) is not None:
                    keys.update(key.strip() for key in value.split(",") if key.strip())
    seen_keys: dict[str, tuple[_Document, _Entry]] = {}
    seen_dois: dict[str, tuple[_Document, _Entry]] = {}
    complete = all(document.valid for document in documents) and not findings
    for document in documents:
        cancellation_point()
        findings.extend(document.findings)
        for entry in document.entries:
            if entry.key is not None:
                if entry.key in seen_keys:
                    first_document, first_entry = seen_keys[entry.key]
                    findings.append(
                        document.finding(
                            "bibliography.duplicate_key",
                            f"Citation key {entry.key!r} occurs more than once.",
                            entry.start,
                            severity="error",
                            suggestion=(
                                "Review the entries and all uses before renaming or merging."
                            ),
                            details={
                                "first_path": first_document.path,
                                "first_line": first_document.line(first_entry.start),
                                "conflicting_fields": _conflicts(first_entry, entry),
                            },
                        )
                    )
                else:
                    seen_keys[entry.key] = (document, entry)
            field_names: set[str] = set()
            for bib_field in entry.fields:
                if bib_field.name in field_names:
                    findings.append(
                        document.finding(
                            "bibliography.repeated_field",
                            f"Field {bib_field.name!r} is repeated in entry {entry.key!r}.",
                            bib_field.start,
                            severity="error",
                            suggestion=(
                                "Choose the intended value; no field is removed automatically."
                            ),
                        )
                    )
                field_names.add(bib_field.name)
                for atom in bib_field.atoms:
                    if atom.kind == "macro" and atom.value.lower() not in macro_names:
                        findings.append(
                            document.finding(
                                "bibliography.undefined_macro",
                                f"String macro {atom.value!r} has no definition "
                                "in the inspected files.",
                                atom.start,
                                severity="error" if complete else "warning",
                                status="failed" if complete else "inconclusive",
                            )
                        )
                if entry.key is None:
                    continue
                if bib_field.name in _RELATIONSHIPS:
                    value = _plain_value(bib_field)
                    if value is None:
                        findings.append(
                            document.finding(
                                "bibliography.relationship_unresolved",
                                f"Field {bib_field.name!r} is not a plain literal; "
                                "relationships are unverified.",
                                bib_field.start,
                                status="inconclusive",
                            )
                        )
                    else:
                        targets = (
                            [value] if bib_field.name in {"crossref", "xref"} else value.split(",")
                        )
                        for target in targets:
                            if target.strip() not in keys:
                                findings.append(
                                    document.finding(
                                        "bibliography.missing_relationship",
                                        f"Field {bib_field.name!r} refers to missing "
                                        f"key {target.strip()!r}.",
                                        bib_field.start,
                                        severity="error" if complete else "warning",
                                        status="failed" if complete else "inconclusive",
                                    )
                                )
                if bib_field.name != "doi":
                    continue
                identifier, _, uncertainty = _doi_value(bib_field)
                if uncertainty:
                    findings.append(
                        document.finding(
                            "bibliography.doi_unresolved",
                            uncertainty,
                            bib_field.start,
                            status="inconclusive",
                            suggestion="Inspect this DOI manually; it remains unchanged.",
                        )
                    )
                elif identifier is None:
                    findings.append(
                        document.finding(
                            "bibliography.invalid_doi",
                            "The DOI field is not a recognized bare DOI or supported DOI URL.",
                            bib_field.start,
                            suggestion=(
                                "Verify the identifier; no metadata will be guessed or replaced."
                            ),
                        )
                    )
                elif identifier in seen_dois:
                    first_document, first_entry = seen_dois[identifier]
                    if first_entry is not entry:
                        findings.append(
                            document.finding(
                                "bibliography.duplicate_doi",
                                f"Entries {first_entry.key!r} and {entry.key!r} "
                                "have the same exact DOI.",
                                bib_field.start,
                                suggestion=(
                                    "Review the matching works and conflicts before any merge."
                                ),
                                details={
                                    "doi": identifier,
                                    "first_path": first_document.path,
                                    "first_line": first_document.line(first_entry.start),
                                    "conflicting_fields": _conflicts(first_entry, entry),
                                },
                            )
                        )
                else:
                    seen_dois[identifier] = (document, entry)
    return findings


def check_bibliography(root: Path, main: str | None = None) -> list[Finding]:
    """Inspect UTF-8 .bib files without changing them or accessing the network.

    When ``main`` names a source whose selected literal graph declares resolvable
    bibliography resources, only those resources are inspected, so duplicate keys
    and DOIs are reported within that declared set. Without such a selection every
    .bib file below ``root`` is inspected, including unreferenced spare copies.

    No findings means no problem was found by these checks, not that metadata,
    macro expansion order, style requirements, or rendered citations are valid.
    """
    return _check(*_load(root, _selected_resources(root, main)))


def normalize_dois(
    root: Path, main: str | None = None
) -> tuple[dict[str, bytes], list[Change], list[Finding]]:
    """Propose literal DOI prefix removal; callers explicitly select and apply it.

    ``main`` narrows the inspected and proposed files exactly as it does for
    :func:`check_bibliography`. Invalid files and repeated DOI fields are left
    unchanged. Only changed files are returned, keyed by their relative POSIX
    path, with visible unified diffs.
    """
    documents, input_findings = _load(root, _selected_resources(root, main))
    findings = _check(documents, input_findings)
    proposed: dict[str, bytes] = {}
    changes: list[Change] = []
    for document in documents:
        cancellation_point()
        if not document.valid:
            continue
        replacements: list[tuple[int, int]] = []
        for entry in document.entries:
            doi_fields = [bib_field for bib_field in entry.fields if bib_field.name == "doi"]
            if entry.key is None or len(doi_fields) != 1:
                continue
            _, span, _ = _doi_value(doi_fields[0])
            if span is not None:
                replacements.append(span)
        if not replacements:
            continue
        pieces: list[str] = []
        cursor = 0
        for start, end in replacements:
            pieces.append(document.text[cursor:start])
            cursor = end
        pieces.append(document.text[cursor:])
        updated = "".join(pieces)
        proposed[document.path] = updated.encode("utf-8")
        diff = "".join(
            difflib.unified_diff(
                document.text.splitlines(keepends=True),
                updated.splitlines(keepends=True),
                fromfile=document.path,
                tofile=document.path,
            )
        )
        changes.append(
            Change(
                document.path,
                "normalize-doi",
                f"Remove supported prefixes from {len(replacements)} literal DOI field(s).",
                diff=diff,
            )
        )
    return proposed, changes, findings
