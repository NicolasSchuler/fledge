"""Operations of a bibliography proposal that need only the .bib files.

This module owns the four operations that read no TeX source: laying out each entry's
fields, removing named fields, applying guarded field edits, and ordering entries by
key. Each takes the parsed documents of a proposal, replaces them with their edited and
re-parsed versions, and returns how many were affected; each refuses by raising
``ProposalBlocked``. Operations that rename keys or prune entries depend on the
manuscript's citations and are in ``bibliography_keys``.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from typing import NamedTuple

from .bibliography_index import LocatedEntry
from .bibliography_proposal import (
    ProposalBlocked,
    entry_layout,
    replace_documents,
    unique_entries,
)
from .bibtex_layout import (
    EntrySpan,
    FieldSpan,
    InvalidValueSyntax,
    field_removal,
    newline_style,
    value_expression,
)
from .bibtex_syntax import BibDocument
from .cancellation import cancellation_point
from .findings import Status
from .text_edits import TextEdit

_INDENT = "  "
# A guarded field edit as written in the settings: key, field, expected and replacement.
FieldEditRow = tuple[str, str, str | None, str | None]


def _is_blank(gap: str) -> bool:
    """Return whether ``gap`` is empty or only whitespace."""
    return not gap or gap.isspace()


def _field_layout_edits(
    text: str, item: FieldSpan, previous: int, newline: str, edits: list[TextEdit]
) -> int:
    """Add the layout edits of one field.

    A field that follows only whitespace starts on its own indented line, a plain
    ``=`` gets one space on each side, a missing comma is added and whitespace before
    the comma is removed.

    Args:
        text: The document text.
        item: The layout of the field.
        previous: The offset where the gap before the field starts.
        newline: The document's line ending.
        edits: Extended in place with the field's edits.

    Returns:
        The offset just after the field's comma, or after its value when the comma is
        added.
    """
    if _is_blank(text[previous : item.field.start]):
        edits.append(TextEdit(previous, item.field.start, newline + _INDENT))
    if re.fullmatch(r"\s*=\s*", text[item.name_end : item.value_start]):
        edits.append(TextEdit(item.name_end, item.value_start, " = "))
    if item.comma is None:
        edits.append(TextEdit(item.value_end, item.value_end, ","))
        return item.value_end
    if _is_blank(text[item.value_end : item.comma]):
        edits.append(TextEdit(item.value_end, item.comma, ""))
    return item.comma + 1


def _entry_layout_edits(document: BibDocument, span: EntrySpan, newline: str) -> list[TextEdit]:
    """Plan the layout edits of one entry's fields and of the gap before its closing brace.

    Args:
        document: The document that holds the entry.
        span: The layout of the entry.
        newline: The document's line ending.

    Returns:
        The edits, or none for an entry without fields or without a comma after its key.
    """
    if not span.fields or span.comma is None:
        return []
    edits: list[TextEdit] = []
    previous = span.comma + 1
    for item in span.fields:
        previous = _field_layout_edits(document.text, item, previous, newline, edits)
    if _is_blank(document.text[previous : span.closing]):
        edits.append(TextEdit(previous, span.closing, newline))
    return edits


def format_entries(documents: dict[str, BibDocument]) -> int:
    """Give each entry's fields a uniform layout without touching any value or comment.

    Args:
        documents: The parsed documents by relative path; a changed one is replaced.

    Returns:
        The number of documents that changed.

    Raises:
        ProposalBlocked: An entry cannot be laid out, or an edited document no longer
            parses completely.
    """
    edits: dict[str, list[TextEdit]] = defaultdict(list)
    for document in documents.values():
        cancellation_point()
        newline = newline_style(document.text)
        for entry in document.entries:
            span = entry_layout(document, entry)
            edits[document.path].extend(_entry_layout_edits(document, span, newline))
    return replace_documents(documents, edits)


def remove_selected_fields(documents: dict[str, BibDocument], names: tuple[str, ...]) -> int:
    """Remove every field called one of ``names``.

    Args:
        documents: The parsed documents by relative path; a changed one is replaced.
        names: The names of the fields to remove.

    Returns:
        The number of fields removed.

    Raises:
        ProposalBlocked: An entry cannot be laid out, or an edited document no longer
            parses completely.
    """
    edits: dict[str, list[TextEdit]] = defaultdict(list)
    count = 0
    for document in documents.values():
        for entry in document.entries:
            for item in entry_layout(document, entry).fields:
                if item.field.name in names:
                    edits[document.path].append(field_removal(document, item))
                    count += 1
    replace_documents(documents, edits)
    return count


class _FieldEdit(NamedTuple):
    """One guarded edit of a field.

    Attributes:
        key: The key of the entry to edit.
        name: The name of the field to edit.
        expected: The field's current value as the edit expects it, or ``None`` if the
            field must be absent.
        replacement: The new value, or ``None`` to remove the field.
    """

    key: str
    name: str
    expected: str | None
    replacement: str | None


@dataclass(slots=True)
class _FieldEditPlan:
    """The text edits and appended fields that the guarded field edits amount to.

    Attributes:
        edits: The edits of existing fields, by document path.
        additions: The fields to append to an entry, by document path and entry key, as
            (name, expression) pairs.
    """

    edits: dict[str, list[TextEdit]] = field(default_factory=lambda: defaultdict(list))
    additions: dict[str, dict[str, list[tuple[str, str]]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(list))
    )


def _entry_blocked(
    reference: LocatedEntry, message: str, status: Status = "inconclusive"
) -> ProposalBlocked:
    """Build the refusal for an edit of ``reference``, located at the entry's start.

    Args:
        reference: The entry the refused edit targets.
        message: Why the edit is refused.
        status: The status the refusal reports.

    Returns:
        The exception to raise.
    """
    document = reference.document
    return ProposalBlocked(
        message, status=status, path=document.path, line=document.line(reference.entry.start)
    )


def _replacement_expression(
    reference: LocatedEntry, edit: _FieldEdit, value: str, wrapper: str = "{"
) -> str:
    """Write ``value`` as a field value expression in ``wrapper``.

    Args:
        reference: The entry the edit targets, which locates a refusal.
        edit: The edit, which names the entry and field in a refusal.
        value: The replacement value.
        wrapper: The opening delimiter of the expression.

    Returns:
        The value expression.

    Raises:
        ProposalBlocked: The value has unbalanced braces or unsupported syntax.
    """
    try:
        return value_expression(value, wrapper)
    except InvalidValueSyntax as error:
        raise _entry_blocked(
            reference,
            f"The replacement for {edit.key!r}/{edit.name} has unbalanced braces or "
            "unsupported value syntax.",
            "failed",
        ) from error


def _plan_new_field(plan: _FieldEditPlan, reference: LocatedEntry, edit: _FieldEdit) -> None:
    """Plan an edit of a field the entry does not have; a ``None`` replacement plans nothing.

    Args:
        plan: The plan that receives the field to append.
        reference: The entry the edit targets.
        edit: The edit.

    Raises:
        ProposalBlocked: The edit expects a value, or the replacement has unsupported
            syntax.
    """
    if edit.expected is not None:
        raise _entry_blocked(
            reference, f"The expected {edit.name!r} field in {edit.key!r} is absent.", "failed"
        )
    if edit.replacement is not None:
        expression = _replacement_expression(reference, edit, edit.replacement)
        plan.additions[reference.document.path][edit.key].append((edit.name, expression))


def _plan_existing_field(
    plan: _FieldEditPlan, reference: LocatedEntry, item: FieldSpan, edit: _FieldEdit
) -> None:
    """Plan an edit of a field the entry has: remove it or replace its value.

    A number stays a bare number when the replacement is ASCII digits; a literal keeps
    its delimiter.

    Args:
        plan: The plan that receives the text edit.
        reference: The entry the edit targets.
        item: The layout of the existing field.
        edit: The edit.

    Raises:
        ProposalBlocked: The value is a macro or concatenated, it does not match the
            expected value, or the replacement has unsupported syntax.
    """
    document = reference.document
    atoms = item.field.atoms
    if len(atoms) != 1 or atoms[0].kind not in {"literal", "number"}:
        raise _entry_blocked(
            reference, f"{edit.key!r}/{edit.name}: macro or concatenated values cannot be edited."
        )
    atom = atoms[0]
    if atom.value != edit.expected:
        raise _entry_blocked(
            reference,
            f"The expected value for {edit.key!r}/{edit.name} does not match; review the edit.",
            "failed",
        )
    if edit.replacement is None:
        plan.edits[document.path].append(field_removal(document, item))
    elif edit.replacement != edit.expected:
        if atom.kind == "number" and edit.replacement.isascii() and edit.replacement.isdigit():
            expression = edit.replacement
        else:
            wrapper = document.text[item.value_start] if atom.kind == "literal" else "{"
            expression = _replacement_expression(reference, edit, edit.replacement, wrapper)
        plan.edits[document.path].append(TextEdit(item.value_start, item.value_end, expression))


def _plan_field_edit(
    plan: _FieldEditPlan,
    entries: dict[str, LocatedEntry],
    donors: dict[str, str],
    edit: _FieldEdit,
) -> None:
    """Plan one guarded field edit.

    Args:
        plan: The plan that receives the edit.
        entries: Every entry by key.
        donors: The donor keys selected for merge removal, mapped to the keys they merge
            into.
        edit: The edit.

    Raises:
        ProposalBlocked: The key does not exist or is selected for merge removal, the
            entry repeats the field, or the edit itself is refused.
    """
    if edit.key not in entries:
        raise ProposalBlocked(
            f"Field edit key {edit.key!r} does not exist in the selected resources.",
            status="failed",
        )
    reference = entries[edit.key]
    if edit.key in donors:
        raise _entry_blocked(
            reference, f"Field edit key {edit.key!r} is also selected for merge removal.", "failed"
        )
    span = entry_layout(reference.document, reference.entry)
    matches = [item for item in span.fields if item.field.name == edit.name]
    if len(matches) > 1:
        raise _entry_blocked(
            reference, f"{edit.key!r} has repeated {edit.name!r} fields; choose one value first."
        )
    if matches:
        _plan_existing_field(plan, reference, matches[0], edit)
    else:
        _plan_new_field(plan, reference, edit)


def _append_new_fields(
    documents: dict[str, BibDocument], additions: dict[str, dict[str, list[tuple[str, str]]]]
) -> None:
    """Append the planned fields at the end of their entries, after the other edits applied.

    Args:
        documents: The parsed documents by relative path; a changed one is replaced.
        additions: The fields to append, by document path and entry key, as name and
            value expression.

    Raises:
        ProposalBlocked: An entry cannot be laid out, or an edited document no longer
            parses completely.
    """
    entries = unique_entries(documents)
    edits: dict[str, list[TextEdit]] = defaultdict(list)
    for path, groups in additions.items():
        document = documents[path]
        newline = newline_style(document.text)
        for key, rows in groups.items():
            span = entry_layout(document, entries[key].entry)
            if span.fields and span.fields[-1].comma is None:
                end = span.fields[-1].value_end
                edits[path].append(TextEdit(end, end, ","))
            elif not span.fields and span.comma is None:
                edits[path].append(TextEdit(span.key_end, span.key_end, ","))
            lines = newline.join(f"{_INDENT}{name} = {expression}," for name, expression in rows)
            edits[path].append(TextEdit(span.closing, span.closing, newline + lines + newline))
    replace_documents(documents, edits)


def edit_fields(
    documents: dict[str, BibDocument],
    field_edits: Sequence[FieldEditRow],
    merge_keys: Sequence[tuple[str, str]],
) -> int:
    """Apply the guarded field edits.

    A mismatch blocks the whole proposal rather than overwriting a value that has
    changed.

    Args:
        documents: The parsed documents by relative path; a changed one is replaced.
        field_edits: The edits, each naming an entry key, a field, the value it expects
            to find (``None`` for an absent field) and the replacement (``None`` removes the
            field).
        merge_keys: The merges as pairs of a donor key and the key it merges into; a
            donor's fields cannot be edited.

    Returns:
        The number of documents that changed.

    Raises:
        ProposalBlocked: An edit is refused, a key is duplicated, or an edited document
            no longer parses completely.
    """
    before = {path: document.text for path, document in documents.items()}
    entries = unique_entries(documents)
    plan = _FieldEditPlan()
    donors = dict(merge_keys)
    for row in field_edits:
        _plan_field_edit(plan, entries, donors, _FieldEdit(*row))
    replace_documents(documents, plan.edits)
    _append_new_fields(documents, plan.additions)
    return sum(document.text != before[path] for path, document in documents.items())


def _require_plain_boundaries(document: BibDocument, spans: list[EntrySpan]) -> None:
    """Refuse to reorder entries that anything but whitespace separates.

    Args:
        document: The document that holds the entries.
        spans: The layout of each entry, in document order.

    Raises:
        ProposalBlocked: A comment, free text or macro declaration lies between two
            entries.
    """
    for first, second in pairwise(spans):
        if document.text[first.end : second.entry.start].strip():
            raise ProposalBlocked(
                "Ordering would move entries across comments, free text or macro declarations; "
                "separate those boundaries first.",
                path=document.path,
                line=document.line(first.end),
            )


def _require_parents_after_children(
    document: BibDocument, spans: list[EntrySpan], ordered: list[EntrySpan]
) -> None:
    """Refuse an order that places a cross-reference parent before its child.

    Args:
        document: The document that holds the entries.
        spans: The layout of each entry, in document order.
        ordered: The same layouts in the proposed order.

    Raises:
        ProposalBlocked: A ``crossref`` or ``xref`` value is a macro, or its target is
            outside this document or would come before the child.
    """
    positions = {span.entry.key: index for index, span in enumerate(ordered)}
    for span in spans:
        for item in span.entry.fields:
            if item.name not in {"crossref", "xref"}:
                continue
            line = document.line(item.start)
            if len(item.atoms) != 1 or item.atoms[0].kind != "literal":
                raise ProposalBlocked(
                    "Ordering cannot resolve a macro-valued cross-reference.",
                    path=document.path,
                    line=line,
                )
            target = item.atoms[0].value.strip()
            if target not in positions or positions[span.entry.key] >= positions[target]:
                raise ProposalBlocked(
                    "Key ordering would place a cross-reference parent before its child, "
                    "or its ordering is outside this resource.",
                    path=document.path,
                    line=line,
                )


def order_entries(documents: dict[str, BibDocument]) -> int:
    """Sort each document's entries by key.

    Args:
        documents: The parsed documents by relative path; a changed one is replaced.

    Returns:
        The number of documents that changed.

    Raises:
        ProposalBlocked: A key is duplicated, an entry boundary is not plain, a
            cross-reference parent would precede its child, or an edited document no
            longer parses completely.
    """
    edits: dict[str, list[TextEdit]] = defaultdict(list)
    unique_entries(documents)
    for document in documents.values():
        spans = [entry_layout(document, entry) for entry in document.entries]
        ordered = sorted(spans, key=lambda span: span.entry.key)
        if spans == ordered:
            continue
        _require_plain_boundaries(document, spans)
        _require_parents_after_children(document, spans, ordered)
        for current, replacement in zip(spans, ordered, strict=True):
            edits[document.path].append(
                TextEdit(
                    current.entry.start,
                    current.end,
                    document.text[replacement.entry.start : replacement.end],
                )
            )
    return replace_documents(documents, edits)
