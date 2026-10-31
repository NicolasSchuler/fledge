"""Where the pieces of a parsed .bib entry sit in its text, and edits that cut them out.

``bibtex_syntax`` records where each entry, field and value starts; editing also needs
the delimiters between them: the commas, the closing brace and the end of each value.
This module owns that layout and the edits built from it: removing a field or an
entry while keeping the percent comments inside it, choosing a file's line ending, and
quoting a replacement value so that it parses back to exactly the intended text. It
decides nothing about which fields or entries to edit (see ``bibliography_edits`` and
``bibliography_keys``) and writes no file.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .bibtex_syntax import (
    ENTRY_TYPE,
    IDENTIFIER,
    BibDocument,
    Entry,
    Field,
    parse_bibtex,
    skip_trivia,
)
from .text_edits import TextEdit

_ENTRY_HEADER = re.compile("@" + ENTRY_TYPE.pattern)


class UnsupportedLayout(Exception):
    """An entry whose delimiters cannot be located, so it cannot be edited safely."""


class InvalidValueSyntax(ValueError):
    """A replacement value that would not read back as the single literal intended."""


@dataclass(frozen=True)
class FieldSpan:
    """One field with the offsets of its name end, value and trailing comma.

    Attributes:
        field: The parsed field.
        name_end: The offset just after the field name.
        value_start: The offset where the first value atom starts, delimiters included.
        value_end: The offset just after the last value atom, delimiters included.
        comma: The offset of the comma after the value, or ``None`` when there is none.
    """

    field: Field
    name_end: int
    value_start: int
    value_end: int
    comma: int | None


@dataclass(frozen=True)
class EntrySpan:
    """One entry with the offsets of its key, comma, fields and closing delimiter.

    Attributes:
        entry: The parsed entry.
        key_start: The offset where the citation key starts.
        key_end: The offset just after the citation key.
        comma: The offset of the comma after the key, or ``None`` when there is none.
        fields: The layout of every field, in source order.
        closing: The offset of the entry's closing delimiter.
    """

    entry: Entry
    key_start: int
    key_end: int
    comma: int | None
    fields: tuple[FieldSpan, ...]
    closing: int

    @property
    def end(self) -> int:
        """The offset just after the entry's closing delimiter."""
        return self.closing + 1


def layout_entry(document: BibDocument, entry: Entry) -> EntrySpan:
    """Locate the delimiters of ``entry``.

    Args:
        document: The parsed file that holds the entry.
        entry: The entry to lay out.

    Returns:
        The offsets of the entry's key, comma, fields and closing delimiter.

    Raises:
        UnsupportedLayout: The entry has no supported literal key, or a field name has
            unsupported syntax.
    """
    text = document.text
    match = _ENTRY_HEADER.match(text, entry.start)
    if match is None:
        raise UnsupportedLayout("An entry has no supported literal key.")
    opening = skip_trivia(text, match.end())
    key_start = skip_trivia(text, opening + 1)
    key_end = key_start + len(entry.key)
    after_key = skip_trivia(text, key_end)
    comma = after_key if text[after_key] == "," else None
    spans: list[FieldSpan] = []
    for item in entry.fields:
        start, end = item.atoms[0].outer_start, item.atoms[-1].outer_end
        delimiter = skip_trivia(text, end)
        field_comma = delimiter if text[delimiter] == "," else None
        name = IDENTIFIER.match(text, item.start)
        if name is None:
            raise UnsupportedLayout("A field name has unsupported syntax.")
        spans.append(FieldSpan(item, name.end(), start, end, field_comma))
    if spans:
        final = spans[-1]
        closing = skip_trivia(text, final.comma + 1 if final.comma is not None else final.value_end)
    else:
        closing = skip_trivia(text, comma + 1 if comma is not None else key_end)
    return EntrySpan(entry, key_start, key_end, comma, tuple(spans), closing)


def retained_comments(text: str, start: int, end: int, fields: tuple[Field, ...]) -> str:
    """Collect the percent comments between ``start`` and ``end`` outside the value atoms.

    Args:
        text: The text of the .bib file.
        start: The offset where the search starts.
        end: The offset where the search stops.
        fields: The fields whose value atoms, delimiters included, are skipped.

    Returns:
        The comments in order, each with its line break when it has one inside the range.
    """
    protected = {atom.outer_start: atom.outer_end for item in fields for atom in item.atoms}
    result: list[str] = []
    position = start
    while position < end:
        if position in protected:
            position = protected[position]
        elif text[position] == "%":
            newline = text.find("\n", position, end)
            stop = end if newline < 0 else newline + 1
            result.append(text[position:stop])
            position = stop
        else:
            position += 1
    return "".join(result)


def field_removal(document: BibDocument, span: FieldSpan) -> TextEdit:
    """Remove a field and its comma, keeping the comments inside it.

    Args:
        document: The parsed file that holds the field.
        span: The layout of the field.

    Returns:
        The edit that replaces the field and its comma with the comments inside them.
    """
    start = span.field.start
    end = span.comma + 1 if span.comma is not None else span.value_end
    return TextEdit(start, end, retained_comments(document.text, start, end, (span.field,)))


def entry_removal(document: BibDocument, span: EntrySpan) -> TextEdit:
    """Remove a whole entry, keeping the comments inside it.

    Args:
        document: The parsed file that holds the entry.
        span: The layout of the entry.

    Returns:
        The edit that replaces the entry with the comments inside it.
    """
    start, end = span.entry.start, span.end
    return TextEdit(start, end, retained_comments(document.text, start, end, span.entry.fields))


def newline_style(text: str) -> str:
    """Choose the file's line ending: CRLF when the text has any, else LF."""
    return "\r\n" if "\r\n" in text else "\n"


def value_expression(value: str, wrapper: str = "{") -> str:
    """Wrap ``value`` in braces or quotes, if that parses back as exactly ``value``.

    Args:
        value: The intended field value.
        wrapper: The opening delimiter; ``{`` closes with a brace, anything else with a
            double quote.

    Returns:
        The wrapped value.

    Raises:
        InvalidValueSyntax: The wrapped value does not parse back as a single literal
            equal to ``value``.
    """
    closing = "}" if wrapper == "{" else '"'
    expression = wrapper + value + closing
    test = parse_bibtex("replacement", "@misc{key, value=" + expression + "}")
    if (
        not test.valid
        or len(test.records) != 1
        or len(test.records[0].fields) != 1
        or len(test.records[0].fields[0].atoms) != 1
        or test.records[0].fields[0].atoms[0].value != value
    ):
        raise InvalidValueSyntax(value)
    return expression
