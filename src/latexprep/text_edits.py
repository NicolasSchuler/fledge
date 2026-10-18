"""Offset-based text edits, byte-order-mark restoration and unified diffs of proposals."""

from __future__ import annotations

import difflib
from collections.abc import Iterable
from typing import NamedTuple


class TextEdit(NamedTuple):
    """Replace ``text[start:end]`` with ``replacement``; an empty span inserts."""

    start: int
    end: int
    replacement: str


class OverlappingEdits(ValueError):
    """Two edits address overlapping text, or an edit lies outside the text."""


def apply_text_edits(text: str, edits: Iterable[TextEdit]) -> str:
    """Apply edits given in any order; insertions at one offset keep their given order."""
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
    """``text`` as stored in its file, restoring a byte-order mark the reader removed."""
    return "﻿" + text if bom else text


def unified_diff_text(path: str, before: str, after: str, *, destination: str | None = None) -> str:
    """A unified diff of one file's text, labelled with its path (and new path, if moved)."""
    return "".join(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile=path,
            tofile=destination or path,
        )
    )
