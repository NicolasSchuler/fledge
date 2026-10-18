"""Bounded BibTeX syntax reader shared by bibliography checks, transformations and lookups.

This is a lexical reader, not a bibliography backend. It does not infer required
fields, expand string macros, apply inheritance, or interpret field contents.
Offsets index the decoded text after any UTF-8 byte-order mark, so callers can
splice edits into ``BibDocument.text`` and restore the mark when writing.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .cancellation import cancellation_point
from .findings import Finding, Severity, Status
from .text_edits import with_bom

MAX_BIB_FILE_BYTES = 16 * 1024 * 1024
# Keyed entries, @string and @preamble declarations count alike, per file and per
# declared resource set.
MAX_BIB_ENTRIES = 100_000
# A manuscript's declared resources share these bounds in checks and transformations.
MAX_RESOURCE_FILES = 128
MAX_RESOURCES_TOTAL_BYTES = 32 * 1024 * 1024
# Longest literal field value read as one scalar.
_MAX_FIELD_CHARS = 64 * 1024
_MAX_NESTING = 256
# Field names, string-macro names and bare value tokens.
IDENTIFIER = re.compile(r"[^\s\"#%'(),={}]+")
ENTRY_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
# Lowercase entry-type and field names accepted in option tables.
LOWERCASE_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_DOI_PREFIX = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
_DOI = re.compile(r"10\.\d{4,9}/[!-~]+")

AtomKind = Literal["literal", "number", "macro"]
DeclarationKind = Literal["string", "preamble"]


@dataclass(frozen=True, slots=True)
class Atom:
    """One operand of a field value; a literal's span excludes its braces or quotes."""

    kind: AtomKind
    value: str
    start: int
    end: int

    @property
    def outer_start(self) -> int:
        """Start offset including a literal's opening delimiter."""
        return self.start - (self.kind == "literal")

    @property
    def outer_end(self) -> int:
        """End offset including a literal's closing delimiter."""
        return self.end + (self.kind == "literal")


@dataclass(frozen=True, slots=True)
class Field:
    """A lowercased field name, the offset of the name, and its concatenated value."""

    name: str
    start: int
    atoms: tuple[Atom, ...]


@dataclass(frozen=True, slots=True)
class Entry:
    """A keyed bibliography entry such as ``@article{key, ...}``."""

    entry_type: str
    key: str
    start: int
    fields: tuple[Field, ...]


@dataclass(frozen=True, slots=True)
class Declaration:
    """An unkeyed ``@string`` macro definition or ``@preamble``."""

    kind: DeclarationKind
    start: int
    fields: tuple[Field, ...]


@dataclass(slots=True)
class BibDocument:
    """One parsed .bib file; parsing stops at the first syntax error."""

    path: str
    text: str
    bom: bool = False
    # Entries and declarations in source order; @comment blocks are skipped.
    records: list[Entry | Declaration] = field(default_factory=list)
    syntax_error: Finding | None = None
    _newlines: list[int] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        self._newlines = [index for index, char in enumerate(self.text) if char == "\n"]

    @property
    def valid(self) -> bool:
        return self.syntax_error is None

    @property
    def entries(self) -> list[Entry]:
        """Keyed entries in source order."""
        return [record for record in self.records if isinstance(record, Entry)]

    @property
    def file_text(self) -> str:
        """The decoded file content, including a byte-order mark the reader removed."""
        return with_bom(self.text, self.bom)

    def line(self, offset: int) -> int:
        return bisect_right(self._newlines, offset) + 1

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


class BibtexSyntaxError(Exception):
    def __init__(self, message: str, position: int) -> None:
        super().__init__(message)
        self.position = position


def skip_trivia(text: str, position: int) -> int:
    """The first offset at or after ``position`` that is not whitespace or a % comment."""
    while position < len(text):
        if position % 4096 == 0:
            cancellation_point()
        if text[position].isspace():
            position += 1
        elif text[position] == "%":
            newline = text.find("\n", position)
            position = len(text) if newline < 0 else newline + 1
        else:
            break
    return position


class _Parser:
    def __init__(self, text: str, records: list[Entry | Declaration]) -> None:
        self.text = text
        self.records = records
        self.position = 0

    def skip_space(self) -> None:
        self.position = skip_trivia(self.text, self.position)

    def expect(self, character: str) -> None:
        self.skip_space()
        if self.position >= len(self.text) or self.text[self.position] != character:
            raise BibtexSyntaxError(f"Expected {character!r}.", self.position)
        self.position += 1

    def identifier(self) -> tuple[str, int]:
        self.skip_space()
        start = self.position
        match = IDENTIFIER.match(self.text, start)
        if match is None or match[0][0].isdigit():
            raise BibtexSyntaxError("Expected a field or string-macro name.", start)
        self.position = match.end()
        return match[0], start

    def literal(self) -> Atom:
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
                    raise BibtexSyntaxError(
                        "Brace nesting exceeds the supported limit.", self.position
                    )
            elif char == "}":
                if depth == 0:
                    raise BibtexSyntaxError("Unmatched brace inside a quoted value.", self.position)
                depth -= 1
                if opening == "{" and depth == 0:
                    end = self.position
                    self.position += 1
                    return Atom("literal", self.text[start:end], start, end)
            elif char == '"' and opening == '"' and depth == 0:
                end = self.position
                self.position += 1
                return Atom("literal", self.text[start:end], start, end)
            self.position += 1
        raise BibtexSyntaxError("Unterminated braced or quoted value.", start - 1)

    def expression(self) -> tuple[Atom, ...]:
        atoms: list[Atom] = []
        while True:
            self.skip_space()
            if self.position >= len(self.text):
                raise BibtexSyntaxError("Missing field value.", self.position)
            if self.text[self.position] in '{"':
                atoms.append(self.literal())
            else:
                start = self.position
                match = IDENTIFIER.match(self.text, start)
                if match is None:
                    raise BibtexSyntaxError("Expected a literal, number, or string macro.", start)
                self.position = match.end()
                token = match[0]
                if token[0].isdigit() and not token.isdigit():
                    raise BibtexSyntaxError(
                        "A value starting with a number must be quoted or braced.", start
                    )
                kind: AtomKind = "number" if token.isdigit() else "macro"
                atoms.append(Atom(kind, token, start, match.end()))
            self.skip_space()
            if self.position >= len(self.text) or self.text[self.position] != "#":
                return tuple(atoms)
            self.position += 1

    def fields(self, closing: str) -> tuple[Field, ...]:
        fields: list[Field] = []
        while True:
            self.skip_space()
            if self.position < len(self.text) and self.text[self.position] == closing:
                self.position += 1
                return tuple(fields)
            name, start = self.identifier()
            self.expect("=")
            fields.append(Field(name.lower(), start, self.expression()))
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
                    raise BibtexSyntaxError(
                        "Comment nesting exceeds the supported limit.", self.position
                    )
            elif char == closing:
                depth -= 1
                if depth == 0:
                    self.position += 1
                    return
            self.position += 1
        raise BibtexSyntaxError("Unterminated @comment block.", self.position)

    def entry(self, entry_type: str, closing: str, start: int) -> Entry:
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
            raise BibtexSyntaxError("Missing or unsupported citation key.", key_start)
        self.skip_space()
        if self.position < len(self.text) and self.text[self.position] == closing:
            self.position += 1
            return Entry(entry_type, key, start, ())
        self.expect(",")
        return Entry(entry_type, key, start, self.fields(closing))

    def parse(self) -> None:
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
            match = ENTRY_TYPE.match(self.text, self.position)
            if match is None:
                raise BibtexSyntaxError("Expected an entry type after '@'.", start)
            kind = match[0].lower()
            self.position = match.end()
            self.skip_space()
            if self.position >= len(self.text) or self.text[self.position] not in "{(":
                raise BibtexSyntaxError("Expected '{' or '(' after the entry type.", self.position)
            opening = self.text[self.position]
            closing = "}" if opening == "{" else ")"
            self.position += 1
            if kind == "comment":
                self.comment(opening)
                continue
            if len(self.records) >= MAX_BIB_ENTRIES:
                raise BibtexSyntaxError("Entry count exceeds the supported limit.", start)
            if kind == "preamble":
                fields = (Field("preamble", self.position, self.expression()),)
                if self.position < len(self.text) and self.text[self.position] == ",":
                    self.position += 1
                self.expect(closing)
                self.records.append(Declaration("preamble", start, fields))
            elif kind == "string":
                fields = self.fields(closing)
                if len(fields) != 1:
                    raise BibtexSyntaxError("An @string declaration must define one macro.", start)
                self.records.append(Declaration("string", start, fields))
            else:
                self.records.append(self.entry(kind, closing, start))


def parse_bibtex(path: str, text: str, *, bom: bool = False) -> BibDocument:
    """Parse ``text`` up to its first syntax error, which becomes ``syntax_error``.

    ``bom`` records that the reader removed a byte-order mark before ``text``.
    """
    document = BibDocument(path, text, bom=bom)
    try:
        _Parser(text, document.records).parse()
    except BibtexSyntaxError as error:
        document.syntax_error = document.finding(
            "bibliography.syntax",
            str(error),
            min(error.position, len(text)),
            severity="error",
            suggestion="Repair this entry; the remaining file was not parsed or normalized.",
        )
    return document


class BibFileReader:
    """Reads .bib files within a per-file and an optional cumulative byte allowance.

    A leading UTF-8 byte-order mark is accepted and removed before parsing; the
    document records it so a rewrite can restore it byte for byte.
    """

    def __init__(
        self, *, max_file_bytes: int = MAX_BIB_FILE_BYTES, max_total_bytes: int | None = None
    ) -> None:
        self.max_file_bytes = max_file_bytes
        self.max_total_bytes = max_total_bytes
        self.total_bytes = 0

    @property
    def allowance(self) -> int:
        """The most bytes the next file may hold."""
        if self.max_total_bytes is None:
            return self.max_file_bytes
        return min(self.max_file_bytes, self.max_total_bytes - self.total_bytes)

    def read(self, path: Path, name: str) -> BibDocument | None:
        """Parse ``path`` as ``name``; None when it exceeds the allowance.

        Raises OSError when the file cannot be read and UnicodeError when it is
        not UTF-8. Bytes of a file that fails to decode still count as read.
        """
        allowance = self.allowance
        with path.open("rb") as stream:
            raw = stream.read(allowance + 1)
        if len(raw) > allowance:
            return None
        self.total_bytes += len(raw)
        text = raw.decode("utf-8")
        bom = text.startswith("﻿")
        return parse_bibtex(name, text[1:] if bom else text, bom=bom)


def mebibytes(count: int) -> str:
    """A byte limit as whole MiB, for messages built from the limit constants."""
    return f"{count // (1024 * 1024)} MiB"


def describe_read_failure(error: Exception) -> str:
    """Why :meth:`BibFileReader.read` failed, as a phrase that follows the file name.

    Codec details and absolute paths stay out: a researcher needs the cause and, for
    an encoding problem, where in the file it starts.
    """
    if isinstance(error, UnicodeDecodeError):
        return f"is not valid UTF-8 (first invalid byte at offset {error.start})"
    if isinstance(error, UnicodeError):
        return "is not valid UTF-8"
    if isinstance(error, OSError):
        return f"could not be read ({error.strerror or type(error).__name__})"
    return str(error)


def field_literal(
    bib_field: Field,
    *,
    numbers: bool = True,
    braces: bool = True,
    max_chars: int | None = _MAX_FIELD_CHARS,
) -> str | None:
    """The stripped value of a field holding one literal (or number) without escapes.

    None for macros, concatenations, backslashes, values over ``max_chars``, numbers
    unless ``numbers`` is set, and any brace unless ``braces`` is set.
    """
    accepted = ("literal", "number") if numbers else ("literal",)
    if len(bib_field.atoms) != 1 or bib_field.atoms[0].kind not in accepted:
        return None
    value = bib_field.atoms[0].value.strip()
    if max_chars is not None and len(value) > max_chars:
        return None
    if "\\" in value or (not braces and any(char in value for char in "{}")):
        return None
    return value


@dataclass(frozen=True, slots=True)
class DoiReading:
    """What one DOI field literally states.

    ``identifier`` is a recognized DOI without its supported prefix, whose offsets
    ``prefix_span`` gives when one is present. ``uncertainty`` explains a value that
    needs manual inspection. Neither identifier nor uncertainty means an invalid DOI.
    """

    identifier: str | None = None
    prefix_span: tuple[int, int] | None = None
    uncertainty: str | None = None


def read_doi(bib_field: Field) -> DoiReading:
    if len(bib_field.atoms) != 1 or bib_field.atoms[0].kind != "literal":
        return DoiReading(
            uncertainty="A macro, number, or concatenated value cannot be normalized safely."
        )
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
        return DoiReading(
            uncertainty="DOI markup, escapes, or non-ASCII text requires manual inspection."
        )
    if prefix and value.lower().startswith("http") and any(char in identifier for char in "?#"):
        return DoiReading(
            uncertainty="A DOI URL with a query or fragment has an ambiguous identifier."
        )
    if _DOI.fullmatch(identifier) is None or any(char in identifier for char in '<>"'):
        return DoiReading()
    span = (atom.start + left, atom.start + left + prefix.end()) if prefix else None
    return DoiReading(identifier, span)
