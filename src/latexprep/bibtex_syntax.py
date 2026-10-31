"""Bounded BibTeX syntax reader shared by bibliography checks, transformations and lookups.

This is the one parser of .bib files. It is a lexical reader, not a bibliography
backend: it does not infer required fields, expand string macros, apply inheritance,
or interpret field contents. Offsets index the decoded text after any UTF-8
byte-order mark, so callers can splice edits into ``BibDocument.text`` and restore
the mark when writing.

A syntax error does not escape: it is recorded as ``BibDocument.syntax_error`` with a
message and an offset, and the caller decides how to report it. Choosing rule names,
severities and suggestions is the callers' job; this module builds no findings.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from .cancellation import cancellation_point
from .file_safety import FileTooLargeError, read_regular_file
from .limits import CANCELLATION_STRIDE_CHARS, KIB, MIB
from .text_edits import with_bom

MAX_BIB_FILE_BYTES = 16 * MIB
# Keyed entries, @string and @preamble declarations count alike, per file and per
# declared resource set.
MAX_BIB_ENTRIES = 100_000
# A manuscript's declared resources share these bounds in checks and transformations.
MAX_RESOURCE_FILES = 128
MAX_RESOURCES_TOTAL_BYTES = 32 * MIB
# Longest literal field value read as one scalar.
_MAX_FIELD_CHARS = 64 * KIB
_MAX_NESTING = 256
# Field names, string-macro names and bare value tokens.
IDENTIFIER = re.compile(r"[^\s\"#%'(),={}]+")
ENTRY_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_-]*")
# Lowercase entry-type and field names accepted in option tables.
LOWERCASE_NAME = re.compile(r"[a-z][a-z0-9_-]*")
_DOI_PREFIX = re.compile(r"(?:https?://(?:dx\.)?doi\.org/|doi:\s*)", re.IGNORECASE)
# The one DOI shape every module recognises: a registrant prefix and a suffix of
# printable characters without white space, angle brackets, quotes, braces or backslash.
DOI_PATTERN = re.compile(r"10\.\d{4,9}/[^\s<>\"{}\\]{1,2048}", re.IGNORECASE)

AtomKind = Literal["literal", "number", "macro"]
DeclarationKind = Literal["string", "preamble"]


@dataclass(frozen=True, slots=True)
class Atom:
    """One operand of a field value; a literal's span excludes its braces or quotes.

    Attributes:
        kind: ``literal`` for a braced or quoted value, ``number`` for bare digits and
            ``macro`` for a string-macro name.
        value: The text of the operand, without a literal's delimiters.
        start: The offset where the operand's text starts.
        end: The offset just after the operand's text.
    """

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
    """A lowercased field name, the offset of the name, and its concatenated value.

    Attributes:
        name: The field name, lowercased.
        start: The offset where the name starts.
        atoms: The operands that ``#`` concatenates into the value, in source order.
    """

    name: str
    start: int
    atoms: tuple[Atom, ...]


@dataclass(frozen=True, slots=True)
class Entry:
    """A keyed bibliography entry such as ``@article{key, ...}``.

    Attributes:
        entry_type: The entry type, lowercased.
        key: The citation key.
        start: The offset of the entry's ``@``.
        fields: The fields in source order.
    """

    entry_type: str
    key: str
    start: int
    fields: tuple[Field, ...]

    def fields_named(self, name: str) -> list[Field]:
        """Return the fields called ``name`` (lowercase), in source order."""
        return [item for item in self.fields if item.name == name]


@dataclass(frozen=True, slots=True)
class Declaration:
    """An unkeyed ``@string`` macro definition or ``@preamble``.

    Attributes:
        kind: ``string`` or ``preamble``.
        start: The offset of the declaration's ``@``.
        fields: For ``@string``, the one macro definition; for ``@preamble``, one field
            named ``preamble`` that holds its value.
    """

    kind: DeclarationKind
    start: int
    fields: tuple[Field, ...]


@dataclass(frozen=True, slots=True)
class SyntaxProblem:
    """Where and why parsing stopped.

    Attributes:
        message: Why parsing stopped.
        offset: Where parsing stopped, as an index into ``BibDocument.text``.
    """

    message: str
    offset: int


@dataclass(slots=True)
class BibDocument:
    """One parsed .bib file; parsing stops at the first syntax error.

    Attributes:
        path: The name the file was parsed under.
        text: The decoded text without a byte-order mark; every offset indexes it.
        bom: Whether the reader removed a UTF-8 byte-order mark before ``text``.
        records: The entries and declarations in source order; ``@comment`` blocks are
            skipped.
        syntax_error: The first syntax error, or ``None`` when the whole file parsed.
    """

    path: str
    text: str
    bom: bool = False
    records: list[Entry | Declaration] = field(default_factory=list)
    syntax_error: SyntaxProblem | None = None
    # Offsets of the line breaks, found when the first line number is asked for.
    _newlines: list[int] | None = field(default=None, init=False, repr=False, compare=False)

    @property
    def valid(self) -> bool:
        """Whether the whole file parsed without a syntax error."""
        return self.syntax_error is None

    @property
    def entries(self) -> list[Entry]:
        """Keyed entries in source order."""
        return [record for record in self.records if isinstance(record, Entry)]

    @property
    def file_text(self) -> str:
        """The decoded file content, including a byte-order mark the reader removed."""
        return with_bom(self.text, bom=self.bom)

    def line(self, offset: int) -> int:
        """Return the 1-based line that holds the character at ``offset``."""
        if self._newlines is None:
            self._newlines = _line_break_offsets(self.text)
        return bisect_right(self._newlines, offset) + 1


def _line_break_offsets(text: str) -> list[int]:
    """Return the offset of every line feed in ``text``, in order."""
    offsets: list[int] = []
    position = text.find("\n")
    while position >= 0:
        offsets.append(position)
        position = text.find("\n", position + 1)
    return offsets


class _SyntaxFailure(Exception):
    """Stops the parser at the first error; ``parse_bibtex`` records it, nothing else sees it.

    Args:
        message: What is wrong at ``position``; it becomes the exception's message.

    Attributes:
        position: The offset where parsing stopped.
    """

    def __init__(self, message: str, position: int) -> None:
        super().__init__(message)
        self.position = position


def skip_trivia(text: str, position: int) -> int:
    """Find the first offset at or after ``position`` that is not whitespace or a % comment.

    Args:
        text: The text to scan.
        position: The offset where the scan starts.

    Returns:
        The offset of the next significant character, or ``len(text)`` when none follows.
    """
    while position < len(text):
        if position % CANCELLATION_STRIDE_CHARS == 0:
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
    """A reader that appends the records of one text to a list, moving a cursor through it.

    Attributes:
        text: The text being parsed.
        records: The list that receives each entry and declaration in source order.
        position: The cursor, an offset into ``text``.
    """

    def __init__(self, text: str, records: list[Entry | Declaration]) -> None:
        self.text = text
        self.records = records
        self.position = 0

    def skip_space(self) -> None:
        """Move the cursor past whitespace and % comments."""
        self.position = skip_trivia(self.text, self.position)

    def expect(self, character: str) -> None:
        """Consume ``character`` after any whitespace and comments.

        Args:
            character: The character that must come next.

        Raises:
            _SyntaxFailure: The text ends or another character comes next.
        """
        self.skip_space()
        if self.position >= len(self.text) or self.text[self.position] != character:
            raise _SyntaxFailure(f"Expected {character!r}.", self.position)
        self.position += 1

    def identifier(self) -> tuple[str, int]:
        """Read a field or string-macro name after any whitespace and comments.

        Returns:
            The name as written and the offset where it starts.

        Raises:
            _SyntaxFailure: No name follows, or it starts with a digit.
        """
        self.skip_space()
        start = self.position
        match = IDENTIFIER.match(self.text, start)
        if match is None or match[0][0].isdigit():
            raise _SyntaxFailure("Expected a field or string-macro name.", start)
        self.position = match.end()
        return match[0], start

    def literal(self) -> Atom:
        """Read the braced or quoted literal whose opening delimiter is under the cursor.

        A backslash escapes the character after it.

        Returns:
            The literal, its span excluding the delimiters.

        Raises:
            _SyntaxFailure: Braces nest deeper than ``_MAX_NESTING``, a quoted value holds
                an unmatched closing brace, or the value is unterminated.
        """
        opening = self.text[self.position]
        start = self.position + 1
        self.position = start
        depth = 1 if opening == "{" else 0
        while self.position < len(self.text):
            if self.position % CANCELLATION_STRIDE_CHARS == 0:
                cancellation_point()
            char = self.text[self.position]
            if char == "\\":
                self.position += 2
                continue
            if char == "{":
                depth += 1
                if depth > _MAX_NESTING:
                    raise _SyntaxFailure(
                        "Brace nesting exceeds the supported limit.", self.position
                    )
            elif char == "}":
                if depth == 0:
                    raise _SyntaxFailure("Unmatched brace inside a quoted value.", self.position)
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
        raise _SyntaxFailure("Unterminated braced or quoted value.", start - 1)

    def expression(self) -> tuple[Atom, ...]:
        """Read a field value: literals, numbers and string macros joined by ``#``.

        Returns:
            The operands in source order.

        Raises:
            _SyntaxFailure: The value is missing, an operand is not a literal, number or
                macro name, or a bare operand starts with a digit but is not a number.
        """
        atoms: list[Atom] = []
        while True:
            self.skip_space()
            if self.position >= len(self.text):
                raise _SyntaxFailure("Missing field value.", self.position)
            if self.text[self.position] in '{"':
                atoms.append(self.literal())
            else:
                start = self.position
                match = IDENTIFIER.match(self.text, start)
                if match is None:
                    raise _SyntaxFailure("Expected a literal, number, or string macro.", start)
                self.position = match.end()
                token = match[0]
                if token[0].isdigit() and not token.isdigit():
                    raise _SyntaxFailure(
                        "A value starting with a number must be quoted or braced.", start
                    )
                kind: AtomKind = "number" if token.isdigit() else "macro"
                atoms.append(Atom(kind, token, start, match.end()))
            self.skip_space()
            if self.position >= len(self.text) or self.text[self.position] != "#":
                return tuple(atoms)
            self.position += 1

    def fields(self, closing: str) -> tuple[Field, ...]:
        """Read comma-separated ``name = value`` fields up to and including ``closing``.

        A comma may follow the last field.

        Args:
            closing: The delimiter that closes the record.

        Returns:
            The fields in source order, their names lowercased.

        Raises:
            _SyntaxFailure: A field name, ``=``, value or separating comma is missing or
                malformed.
        """
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
        """Skip the rest of an ``@comment`` block whose opening delimiter was just read.

        A backslash escapes the character after it.

        Args:
            opening: The block's opening delimiter; ``{`` closes with ``}``, anything else
                with ``)``.

        Raises:
            _SyntaxFailure: The delimiters nest deeper than ``_MAX_NESTING``, or the block
                is unterminated.
        """
        closing = "}" if opening == "{" else ")"
        depth = 1
        while self.position < len(self.text):
            if self.position % CANCELLATION_STRIDE_CHARS == 0:
                cancellation_point()
            char = self.text[self.position]
            if char == "\\":
                self.position += 2
                continue
            if char == opening:
                depth += 1
                if depth > _MAX_NESTING:
                    raise _SyntaxFailure(
                        "Comment nesting exceeds the supported limit.", self.position
                    )
            elif char == closing:
                depth -= 1
                if depth == 0:
                    self.position += 1
                    return
            self.position += 1
        raise _SyntaxFailure("Unterminated @comment block.", self.position)

    def entry(self, entry_type: str, closing: str, start: int) -> Entry:
        """Read the key and fields of an entry whose opening delimiter was just read.

        Args:
            entry_type: The entry type, lowercased.
            closing: The delimiter that closes the entry.
            start: The offset of the entry's ``@``.

        Returns:
            The entry; without fields when the key is followed by ``closing``.

        Raises:
            _SyntaxFailure: The key is missing or contains ``{``, ``#``, ``=`` or ``"``, or
                the fields are malformed.
        """
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
            raise _SyntaxFailure("Missing or unsupported citation key.", key_start)
        self.skip_space()
        if self.position < len(self.text) and self.text[self.position] == closing:
            self.position += 1
            return Entry(entry_type, key, start, ())
        self.expect(",")
        return Entry(entry_type, key, start, self.fields(closing))

    def record(self) -> None:
        """Read the record that starts at the ``@`` under the cursor.

        An ``@comment`` block is skipped; an entry or declaration is appended to
        ``records``.

        Raises:
            _SyntaxFailure: The entry type or its opening delimiter is missing, the record
                count has reached ``MAX_BIB_ENTRIES``, an ``@string`` does not define
                exactly one macro, or the record is malformed.
        """
        start = self.position
        self.position += 1
        match = ENTRY_TYPE.match(self.text, self.position)
        if match is None:
            raise _SyntaxFailure("Expected an entry type after '@'.", start)
        kind = match[0].lower()
        self.position = match.end()
        self.skip_space()
        if self.position >= len(self.text) or self.text[self.position] not in "{(":
            raise _SyntaxFailure("Expected '{' or '(' after the entry type.", self.position)
        opening = self.text[self.position]
        closing = "}" if opening == "{" else ")"
        self.position += 1
        if kind == "comment":
            self.comment(opening)
            return
        if len(self.records) >= MAX_BIB_ENTRIES:
            raise _SyntaxFailure("Entry count exceeds the supported limit.", start)
        if kind == "preamble":
            fields = (Field("preamble", self.position, self.expression()),)
            if self.position < len(self.text) and self.text[self.position] == ",":
                self.position += 1
            self.expect(closing)
            self.records.append(Declaration("preamble", start, fields))
        elif kind == "string":
            fields = self.fields(closing)
            if len(fields) != 1:
                raise _SyntaxFailure("An @string declaration must define one macro.", start)
            self.records.append(Declaration("string", start, fields))
        else:
            self.records.append(self.entry(kind, closing, start))

    def parse(self) -> None:
        """Read every record in the text, skipping free text outside them.

        Raises:
            _SyntaxFailure: At the first syntax error.
        """
        while self.position < len(self.text):
            cancellation_point()
            self.skip_space()
            if self.position >= len(self.text):
                break
            if self.text[self.position] == "@":
                self.record()
            else:
                # BibTeX permits free text outside entries.
                self.position += 1


def parse_bibtex(path: str, text: str, *, bom: bool = False) -> BibDocument:
    """Parse ``text`` up to its first syntax error, which is kept as ``syntax_error``.

    Args:
        path: The name to record as the document's ``path``.
        text: The decoded text, without a byte-order mark.
        bom: Records that the reader removed a byte-order mark before ``text``.

    Returns:
        The document with every record read before the first syntax error.
    """
    document = BibDocument(path, text, bom=bom)
    try:
        _Parser(text, document.records).parse()
    except _SyntaxFailure as error:
        document.syntax_error = SyntaxProblem(str(error), min(error.position, len(text)))
    return document


class BibFileReader:
    """Reads .bib files within a per-file and an optional cumulative byte allowance.

    A leading UTF-8 byte-order mark is accepted and removed before parsing; the
    document records it so a rewrite can restore it byte for byte.

    Attributes:
        max_file_bytes: The most bytes one file may hold.
        max_total_bytes: The most bytes all files read may hold together, or ``None`` for no
            cumulative limit.
        total_bytes: The bytes read so far.
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
        """Read and parse ``path`` as ``name``.

        Bytes of a file that fails to decode still count as read.

        Args:
            path: The file to read.
            name: The name to record as the document's ``path``.

        Returns:
            The parsed document, or ``None`` when the file exceeds the allowance.

        Raises:
            OSError: The file is a link or special file or cannot be read.
            UnicodeError: The file is not UTF-8.
        """
        try:
            raw = read_regular_file(path, self.allowance)
        except FileTooLargeError:
            return None
        self.total_bytes += len(raw)
        text = raw.decode("utf-8")
        bom = text.startswith("﻿")
        return parse_bibtex(name, text[1:] if bom else text, bom=bom)


def mebibytes(count: int) -> str:
    """Format a byte limit as whole MiB, for messages built from the limit constants."""
    return f"{count // MIB} MiB"


def describe_read_failure(error: Exception) -> str:
    """Describe why :meth:`BibFileReader.read` failed, as a phrase that follows the file name.

    Codec details and absolute paths stay out: a researcher needs the cause and, for
    an encoding problem, where in the file it starts.

    Args:
        error: The exception the read raised.

    Returns:
        The phrase, such as "is not valid UTF-8"; for any other exception than a decoding
        or OS error, its message.
    """
    if isinstance(error, UnicodeDecodeError):
        return f"is not valid UTF-8 (first invalid byte at offset {error.start})"
    if isinstance(error, UnicodeError):
        return "is not valid UTF-8"
    if isinstance(error, OSError):
        return f"could not be read ({error.strerror or type(error).__name__})"
    return str(error)


def field_literal(bib_field: Field, *, max_chars: int | None = _MAX_FIELD_CHARS) -> str | None:
    """Return the stripped value of a field holding one literal (or number) without escapes.

    Args:
        bib_field: The field to read.
        max_chars: The longest stripped value accepted, or ``None`` for no limit.

    Returns:
        The stripped value, or ``None`` for macros, concatenations, backslashes and values
        over ``max_chars``.
    """
    if len(bib_field.atoms) != 1 or bib_field.atoms[0].kind == "macro":
        return None
    value = bib_field.atoms[0].value.strip()
    if max_chars is not None and len(value) > max_chars:
        return None
    return None if "\\" in value else value


def plain_literal(bib_field: Field) -> str | None:
    """Return the stripped value of a field holding one brace-free literal, whatever its length.

    The shape that relationship and alias fields must have: no number, macro,
    concatenation, backslash or brace.

    Args:
        bib_field: The field to read.

    Returns:
        The stripped value, or ``None`` when the field has another shape.
    """
    if len(bib_field.atoms) != 1 or bib_field.atoms[0].kind != "literal":
        return None
    value = bib_field.atoms[0].value.strip()
    return None if any(char in value for char in "\\{}") else value


@dataclass(frozen=True, slots=True)
class DoiReading:
    """What one DOI field literally states.

    Attributes:
        identifier: The recognized DOI without its supported prefix, or ``None``.
        prefix_span: The start and end offsets of the supported prefix, when one is
            present.
        uncertainty: Why the value needs manual inspection, or ``None``. Neither an
            identifier nor an uncertainty means an invalid DOI.
    """

    identifier: str | None = None
    prefix_span: tuple[int, int] | None = None
    uncertainty: str | None = None


def read_doi(bib_field: Field) -> DoiReading:
    """Read what a DOI field literally states; ambiguous markup is reported, never guessed.

    Complete brace wrappers and white space around the value are ignored.

    Args:
        bib_field: The DOI field.

    Returns:
        The identifier and its prefix offsets for a recognized DOI, an uncertainty for a
        value that needs inspection, or neither for an invalid DOI.
    """
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
    if DOI_PATTERN.fullmatch(identifier) is None:
        return DoiReading()
    span = (atom.start + left, atom.start + left + prefix.end()) if prefix else None
    return DoiReading(identifier, span)
