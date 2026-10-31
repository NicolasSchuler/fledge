"""Offset-based text edits, byte-order-mark restoration and unified diffs of proposals."""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from typing import NamedTuple


class TextEdit(NamedTuple):
    """Replace ``text[start:end]`` with ``replacement``; an empty span inserts.

    Attributes:
        start: The offset of the first character to replace.
        end: The offset after the last character to replace; equal to ``start`` for an
            insertion.
        replacement: The text that takes the place of ``text[start:end]``.
    """

    start: int
    end: int
    replacement: str


class OverlappingEdits(ValueError):
    """Two edits address overlapping text, or an edit lies outside the text."""


def apply_text_edits(text: str, edits: Iterable[TextEdit]) -> str:
    """Apply edits given in any order; insertions at one offset keep their given order.

    Args:
        text: The text to edit.
        edits: The edits, with offsets into ``text``.

    Returns:
        ``text`` with every edit applied.

    Raises:
        OverlappingEdits: Two edits address overlapping text, or an edit lies outside the
            text.
    """
    pieces: list[str] = []
    cursor = 0
    for start, end, replacement in sorted(edits, key=lambda edit: (edit.start, edit.end)):
        if start < cursor or not 0 <= start <= end <= len(text):
            raise OverlappingEdits("Text edits overlap or lie outside the text.")
        pieces.extend((text[cursor:start], replacement))
        cursor = end
    pieces.append(text[cursor:])
    return "".join(pieces)


def with_bom(text: str, *, bom: bool) -> str:
    """Return ``text`` as stored in its file, restoring a byte-order mark the reader removed.

    Args:
        text: The text as read, without a byte-order mark.
        bom: Whether the file began with a byte-order mark.

    Returns:
        ``text``, preceded by U+FEFF when ``bom`` is set.
    """
    return "﻿" + text if bom else text


def unified_diff_text(path: str, before: str, after: str, *, destination: str | None = None) -> str:
    """Return a unified diff of one file's text, labelled with its path (and new path, if moved).

    Args:
        path: The file's path, used as the label of the old text.
        before: The text before the change.
        after: The text after the change.
        destination: The new path when the file is moved; ``None`` labels the new text with
            ``path``.

    Returns:
        The diff as one string; it is empty when the texts are equal.
    """
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=path,
            tofile=destination or path,
        )
    )
