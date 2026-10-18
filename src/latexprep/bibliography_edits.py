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
    return not gap or gap.isspace()


def _field_layout_edits(
    text: str, item: FieldSpan, previous: int, newline: str, edits: list[TextEdit]
) -> int:
    """Add the layout edits of one field; the offset just after its comma is returned."""
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
    """Give each entry's fields a uniform layout without touching any value or comment."""
    edits: dict[str, list[TextEdit]] = defaultdict(list)
    for document in documents.values():
        cancellation_point()
        newline = newline_style(document.text)
        for entry in document.entries:
            span = entry_layout(document, entry)
            edits[document.path].extend(_entry_layout_edits(document, span, newline))
    return replace_documents(documents, edits)


def remove_selected_fields(documents: dict[str, BibDocument], names: tuple[str, ...]) -> int:
    """Remove every field called one of ``names``; the number of fields removed."""
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
    """One guarded edit: ``expected`` must be the field's current value, None if absent."""

    key: str
    name: str
    expected: str | None
    replacement: str | None


@dataclass(slots=True)
class _FieldEditPlan:
    edits: dict[str, list[TextEdit]] = field(default_factory=lambda: defaultdict(list))
    # Fields to append to an entry, by document path and entry key, as (name, expression).
    additions: dict[str, dict[str, list[tuple[str, str]]]] = field(
        default_factory=lambda: defaultdict(lambda: defaultdict(list))
    )


def _entry_blocked(
    reference: LocatedEntry, message: str, status: Status = "inconclusive"
) -> ProposalBlocked:
    document = reference.document
    return ProposalBlocked(
        message, status=status, path=document.path, line=document.line(reference.entry.start)
    )


def _replacement_expression(
    reference: LocatedEntry, edit: _FieldEdit, value: str, wrapper: str = "{"
) -> str:
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
    """Append the planned fields at the end of their entries, after the other edits applied."""
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
    """Apply the guarded field edits; the number of documents that changed.

    An edit names an entry key, a field, the value it expects to find (None for an
    absent field) and the replacement (None removes the field). A mismatch blocks the
    whole proposal rather than overwriting a value that has changed.
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
    for first, second in zip(spans, spans[1:], strict=False):
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
    """Sort each document's entries by key; the number of documents that changed."""
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
