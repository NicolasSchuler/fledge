"""Regions of TeX source that a tool must leave exactly as written.

A source transformation that removes comments and a formatter that rewrites layout
both have to keep the same places intact: verbatim-like literals, ranges the author
fenced off with a formatter marker, comments that tools read (``%!TEX``, ``%<guard>``,
``% tex-fmt: off``), and comment blocks that carry a license. ``protected_spans``
finds them. It owns that definition; deciding what to do with the rest of the text
belongs to the callers (``source_transform``, ``formatting``).
"""

from __future__ import annotations

import re
from typing import NamedTuple

from .tex_lexer import RegionScan, merge_spans, source_regions

# Comments read by tools rather than people, which comment removal must keep.
_DIRECTIVE = re.compile(
    r"""
    ^ % \s*
    (?:
        ! | & | \*          # %! (as in %!TEX), %& format and %* lines
      | </?                 # %<guard> and %</guard> docstrip guards
      | (?: tex | latex | bib | biber | arara | encoding | coding | spell
          | fmt | tex-fmt | fledge | latex-prep | region | endregion ) \b
    )
    """,
    re.IGNORECASE | re.VERBOSE,
)
_LICENSE = re.compile(
    r"\b(?:copyright|license|licence|SPDX|LPPL|public domain|all rights reserved)\b", re.I
)
_FORMAT_MARKER = re.compile(r"^%\s*(?:tex-fmt|fmt|fledge|latex-prep)\s*:\s*(off|on)\b", re.I)


class ProtectedSpans(NamedTuple):
    """The text a tool must keep, and whether finding it was uncertain.

    Attributes:
        spans: The merged ``(start, end)`` offsets to keep.
        uncertain: Whether finding the spans was uncertain: a literal region is never
            closed or the formatter markers do not pair up (an ``on`` without an ``off``, a
            second ``off``, or an ``off`` never turned on), because then the extent of the
            protected text is a guess.
    """

    spans: list[tuple[int, int]]
    uncertain: bool


def _directive_and_fenced_spans(
    text: str, comments: list[tuple[int, int]]
) -> tuple[list[tuple[int, int]], bool]:
    """Find directive comments, and ranges between ``tex-fmt: off`` and ``on`` markers.

    A range left open runs to the end of the text.

    Args:
        text: The TeX source.
        comments: The ``(start, end)`` offsets of the comments in ``text``, in order.

    Returns:
        The ``(start, end)`` spans of the directive comments and fenced ranges, unmerged,
        and a flag that is true when the markers are unbalanced.
    """
    spans: list[tuple[int, int]] = []
    off: int | None = None
    unbalanced = False
    for start, end in comments:
        marker = _FORMAT_MARKER.match(text[start:end])
        if marker:
            if marker[1].lower() == "off":
                unbalanced = unbalanced or off is not None
                off = start
            elif off is None:
                unbalanced = True
            else:
                spans.append((off, end))
                off = None
        if _DIRECTIVE.match(text[start:end]):
            spans.append((start, end))
    if off is not None:
        spans.append((off, len(text)))
        unbalanced = True
    return spans, unbalanced


def _license_spans(text: str, comments: list[tuple[int, int]]) -> list[tuple[int, int]]:
    """Return whole contiguous comment blocks in which any line carries licensing text.

    Comments separated only by whitespace belong to one block.

    Args:
        text: The TeX source.
        comments: The ``(start, end)`` offsets of the comments in ``text``, in order.

    Returns:
        The ``(start, end)`` span of each block, from its first comment to its last.
    """
    blocks: list[list[tuple[int, int]]] = []
    for comment in comments:
        if blocks and text[blocks[-1][-1][1] : comment[0]].strip() == "":
            blocks[-1].append(comment)
        else:
            blocks.append([comment])
    return [
        (block[0][0], block[-1][1])
        for block in blocks
        if any(_LICENSE.search(text[start:end]) for start, end in block)
    ]


def protected_spans(text: str, scan: RegionScan | None = None) -> ProtectedSpans:
    """Find literal regions, formatter-off ranges and directive/license comment blocks.

    Args:
        text: The TeX source.
        scan: ``source_regions(text)`` when the caller already has it; ``None`` scans
            ``text`` here.

    Returns:
        The merged spans to keep; they are uncertain when a literal region is unclosed or
        the formatter markers are unbalanced.
    """
    regions, unclosed = scan if scan is not None else source_regions(text)
    literals = [(region.start, region.end) for region in regions if region.kind == "literal"]
    comments = [(region.start, region.end) for region in regions if region.kind == "comment"]
    fenced, unbalanced = _directive_and_fenced_spans(text, comments)
    spans = [*literals, *fenced, *_license_spans(text, comments)]
    return ProtectedSpans(merge_spans(spans), bool(unclosed) or unbalanced)
