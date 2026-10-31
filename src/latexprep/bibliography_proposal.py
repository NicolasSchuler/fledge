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
    """The changed files by relative path, a diff for each, and the finding per operation.

    Attributes:
        files: The new contents of each changed file, by relative path.
        changes: A diff for each changed file.
        findings: The finding of each operation.
    """

    files: dict[str, bytes]
    changes: list[Change]
    findings: list[Finding]


class ProposalBlocked(Exception):
    """An operation that cannot be carried out safely, and where the cause lies.

    Args:
        message: What stopped the operation; it becomes the exception's message.

    Attributes:
        status: "failed" when the request itself is wrong (a key that does not exist)
            and "inconclusive" when the files or sources cannot be analysed well enough
            to tell.
        path: The file where the cause lies, if known.
        line: The line in that file where the cause lies, if known.
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
    """Join the first few distinct ``reasons`` into one sentence, with a count of those left out."""
    distinct = list(dict.fromkeys(reason.rstrip(".") for reason in reasons))
    text = "; ".join(distinct[:_MAX_LISTED_REASONS])
    if len(distinct) > _MAX_LISTED_REASONS:
        text += f"; and {len(distinct) - _MAX_LISTED_REASONS} more"
    return text + "."


def entry_layout(document: BibDocument, entry: Entry) -> EntrySpan:
    """Lay out ``entry``, or block with the document it cannot be laid out in.

    Args:
        document: The parsed file that holds the entry.
        entry: The entry to lay out.

    Returns:
        The offsets of the entry's key, comma, fields and closing delimiter.

    Raises:
        ProposalBlocked: The entry's delimiters cannot be located; the block names the
            document and the line where the entry starts.
    """
    try:
        return layout_entry(document, entry)
    except UnsupportedLayout as error:
        raise ProposalBlocked(
            str(error), path=document.path, line=document.line(entry.start)
        ) from error


def require_valid(document: BibDocument) -> BibDocument:
    """Return ``document`` if it parsed completely.

    Returns:
        ``document`` unchanged.

    Raises:
        ProposalBlocked: The document's bibliography syntax is incomplete.
    """
    if not document.valid:
        raise ProposalBlocked(
            f"{document.path}: repair the incomplete bibliography syntax first.",
            path=document.path,
        )
    return document


def unique_entries(documents: dict[str, BibDocument]) -> dict[str, LocatedEntry]:
    """Map every entry by key; a key that occurs twice blocks, because edits could not target it.

    Args:
        documents: The parsed documents by relative path.

    Returns:
        Every entry with the document that holds it, by key.

    Raises:
        ProposalBlocked: A key occurs more than once.
    """
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
    """Apply ``edits`` to ``text``; edits that overlap block the whole proposal.

    Args:
        text: The text to edit.
        edits: The edits, in any order.
        path: The file the text belongs to, which the block names.

    Returns:
        The edited text.

    Raises:
        ProposalBlocked: Edits overlap or lie outside the text.
    """
    try:
        return apply_text_edits(text, edits)
    except OverlappingEdits as error:
        raise ProposalBlocked(
            "Requested edits overlap; separate the conflicting transformations.", path=path
        ) from error


def replace_documents(documents: dict[str, BibDocument], edits: dict[str, list[TextEdit]]) -> int:
    """Apply each path's edits, re-parse the result and store it in ``documents``.

    Args:
        documents: The parsed documents by relative path; a changed one is replaced.
        edits: The edits of each document, by relative path.

    Returns:
        The number of documents that changed.

    Raises:
        ProposalBlocked: Edits overlap, or an edited document no longer parses
            completely.
    """
    changed = 0
    for path, changes in edits.items():
        document = documents[path]
        text = apply_edits(document.text, changes, path=path)
        if text != document.text:
            documents[path] = require_valid(parse_bibtex(path, text, bom=document.bom))
            changed += 1
    return changed
