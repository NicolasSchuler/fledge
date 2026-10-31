"""Comments of a TeX source with their line numbers, and the private notes among them.

Comments are found with the shared lexer's literal regions, so a ``%`` inside verbatim
text or an escaped percent sign is not one. Only the line and the category of a private note
are ever returned from ``marked_comments``; the comment text itself never reaches a
report. The vocabulary of markers is ``comment_markers``'s.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import NamedTuple

from .tex_lexer import source_regions


class TexComment(NamedTuple):
    """A TeX comment and the line it is on.

    Attributes:
        line: The 1-based line on which the comment starts.
        text: The comment text after ``%``, up to the line feed; a carriage return before
            the line feed is kept.
    """

    line: int
    text: str


class CommentMarker(NamedTuple):
    """A comment line and the category of note it carries.

    Attributes:
        line: The 1-based line of the comment.
        reason: The category of private note the comment carries.
    """

    line: int
    reason: str


def tex_comments(text: str) -> list[TexComment]:
    """Extract TeX comments with their line numbers, skipping the lexer's literal regions.

    Args:
        text: The TeX source to scan.

    Returns:
        The comments in the order the lexer reports them.
    """
    result = []
    line = 1
    previous = 0
    for start, _, kind in source_regions(text).regions:
        if kind != "comment":
            continue
        line += text.count("\n", previous, start)
        previous = start
        # The comment text runs to the line feed, keeping any carriage return.
        end = text.find("\n", start)
        result.append(TexComment(line, text[start + 1 : len(text) if end < 0 else end]))
    return result


def marked_comments(
    comments: Iterable[TexComment], classify: Callable[[str], str | None], limit: int
) -> list[CommentMarker]:
    """Return the first ``limit`` comments that ``classify`` labels as private notes.

    Only the line and category are returned; comment text never reaches a report.

    Args:
        comments: The comments to classify, in order.
        classify: Returns the category of private note a comment text carries, or ``None``
            for an ordinary comment.
        limit: The largest number of markers to return.

    Returns:
        The line and category of each private note found, in the order of ``comments``.
    """
    markers: list[CommentMarker] = []
    for comment in comments:
        if len(markers) >= limit:
            break
        reason = classify(comment.text)
        if reason is not None:
            markers.append(CommentMarker(comment.line, reason))
    return markers
