"""What a bibliography proposal is, and the helpers that its operations share.

A proposal is all or nothing: every selected operation succeeds, or one
``ProposalBlocked`` says what stopped it and none of the changes is returned. This module
owns that contract: the result type, the exception, and the steps every operation
repeats (laying out an entry, applying edits to the parsed documents, requiring that
the result still parses). It selects no operation and writes no file; the operations are
in ``bibliography_edits`` and ``bibliography_keys``, and the order in which they run is
``bibliography_transform``'s.

``ProposalBlocked`` never leaves the proposal modules: a per-item failure becomes a
finding at their boundary, and only ``PreparationError`` aborts a job.
"""

from __future__ import annotations

from typing import NamedTuple

from .bibliography_index import LocatedEntry, located_entries
from .bibtex_layout import EntrySpan, UnsupportedLayout, layout_entry
from .bibtex_syntax import BibDocument, Entry, parse_bibtex
from .findings import Finding, Status
from .report import Change
from .text_edits import OverlappingEdits, TextEdit, apply_text_edits

# Reasons quoted in a refusal; the rest are counted so one rerun can fix more than one.
_MAX_LISTED_REASONS = 3


class BibliographyProposal(NamedTuple):
    """The changed files by relative path, a diff for each, and the finding per operation."""

    files: dict[str, bytes]
    changes: list[Change]
    findings: list[Finding]


class ProposalBlocked(Exception):
    """An operation that cannot be carried out safely, and where the cause lies.

    ``status`` is "failed" when the request itself is wrong (a key that does not exist)
    and "inconclusive" when the files or sources cannot be analysed well enough to tell.
    """

    def __init__(
        self,
        message: str,
        *,
        status: Status = "inconclusive",
        path: str | None = None,
        line: int | None = None,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.path = path
        self.line = line


def joined_reasons(reasons: list[str]) -> str:
    """The first few distinct ``reasons`` as one sentence, with a count of those left out."""
    distinct = list(dict.fromkeys(reason.rstrip(".") for reason in reasons))
    text = "; ".join(distinct[:_MAX_LISTED_REASONS])
    if len(distinct) > _MAX_LISTED_REASONS:
        text += f"; and {len(distinct) - _MAX_LISTED_REASONS} more"
    return text + "."


def entry_layout(document: BibDocument, entry: Entry) -> EntrySpan:
    """The layout of ``entry``, or a block that names the document it cannot be found in."""
    try:
        return layout_entry(document, entry)
    except UnsupportedLayout as error:
        raise ProposalBlocked(
            str(error), path=document.path, line=document.line(entry.start)
        ) from error


def require_valid(document: BibDocument) -> BibDocument:
    """``document``, if it parsed completely."""
    if not document.valid:
        raise ProposalBlocked(
            f"{document.path}: repair the incomplete bibliography syntax first.",
            path=document.path,
        )
    return document


def unique_entries(documents: dict[str, BibDocument]) -> dict[str, LocatedEntry]:
    """Every entry by key; a key that occurs twice blocks, because edits could not target it."""
    result: dict[str, LocatedEntry] = {}
    for reference in located_entries(documents.values()):
        key = reference.entry.key
        if key in result:
            raise ProposalBlocked(
                f"Citation key {key!r} is duplicated; identify entries unambiguously.",
                path=reference.document.path,
                line=reference.document.line(reference.entry.start),
            )
        result[key] = reference
    return result


def apply_edits(text: str, edits: list[TextEdit], *, path: str | None = None) -> str:
    """``text`` with ``edits`` applied; edits that overlap block the whole proposal."""
    try:
        return apply_text_edits(text, edits)
    except OverlappingEdits as error:
        raise ProposalBlocked(
            "Requested edits overlap; separate the conflicting transformations.", path=path
        ) from error


def replace_documents(documents: dict[str, BibDocument], edits: dict[str, list[TextEdit]]) -> int:
    """Apply each path's edits, re-parse the result and store it; the number that changed."""
    changed = 0
    for path, changes in edits.items():
        document = documents[path]
        text = apply_edits(document.text, changes, path=path)
        if text != document.text:
            documents[path] = require_valid(parse_bibtex(path, text, bom=document.bom))
            changed += 1
    return changed
