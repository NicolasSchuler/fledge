r"""Suggestions silenced in the sources by a ``% fledge: ignore CODE`` comment.

A suggestion is optional advice, so an author may silence it where it occurs, without a
reviewed decision in the settings file: a TeX comment ``% fledge: ignore CODE`` at the end
of the suggestion's line, or alone on the line just before it. Several codes are
separated by commas or spaces, and a ``\%`` is text, not a comment. Only suggestions can
be silenced this way. Warnings and errors decide the outcome, so their exceptions belong
in the settings file, where they carry a reason and are reviewed (``[reporting]``).

A silenced suggestion leaves the findings; :func:`split_silenced_suggestions` returns it
separately so that the report can count it.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from .file_safety import read_regular_file
from .findings import Finding
from .path_safety import is_relative_posix_path
from .tex_lexer import MAX_SOURCE_BYTES

# ``% fledge: ignore CODE[, CODE ...]``; the ``%`` must not be escaped by a backslash.
SUPPRESSION_COMMENT = re.compile(
    r"(?<!\\)%\s*fledge:\s*ignore\s+(?P<codes>[A-Z]+[0-9]+(?:[\s,]+[A-Z]+[0-9]+)*)"
)
_CODE = re.compile(r"[A-Z]+[0-9]+")


@dataclass(frozen=True, slots=True)
class SilencedSuggestion:
    """A suggestion that a comment in the sources silenced.

    Attributes:
        code: The check code of the suggestion.
        path: The project file of the suggestion.
        line: The line of the suggestion.
    """

    code: str
    path: str
    line: int

    def to_report(self) -> dict[str, object]:
        """Return the record as ``Report.execution`` carries it."""
        return {"code": self.code, "path": self.path, "line": self.line}


def _comment_codes(text: str) -> frozenset[str]:
    """Return the codes that the suppression comments in one line of source name."""
    return frozenset(
        code
        for match in SUPPRESSION_COMMENT.finditer(text)
        for code in _CODE.findall(match.group("codes"))
    )


def silenced_codes(lines: Sequence[str], line: int) -> frozenset[str]:
    """Return the codes silenced at ``line``: by its own comment or a comment line before it.

    Args:
        lines: The lines of a source file.
        line: The line of a suggestion, counted from 1.

    Returns:
        The codes named on ``line`` itself, and on the line before when that line holds
        nothing but the comment.
    """
    codes: set[str] = set()
    if 1 <= line <= len(lines):
        codes |= _comment_codes(lines[line - 1])
    if 2 <= line <= len(lines) + 1:
        previous = lines[line - 2]
        if previous.lstrip().startswith("%"):
            codes |= _comment_codes(previous)
    return frozenset(codes)


def _source_lines(root: Path, path: str, cache: dict[str, list[str] | None]) -> list[str] | None:
    """Read the lines of a project file once; ``None`` when it cannot be read.

    Args:
        root: The project root the path is relative to.
        path: The project-relative path.
        cache: The lines read so far, by path.

    Returns:
        The lines, or ``None`` for a file that is unsafe to name, unreadable or too large.
    """
    if path not in cache:
        lines = None
        if is_relative_posix_path(path, characters="literal"):
            try:
                # The source analysis reads no more of a file than this either.
                data = read_regular_file(root / path, MAX_SOURCE_BYTES)
            except OSError:
                data = None
            if data is not None:
                lines = data.decode("utf-8", errors="replace").splitlines()
        cache[path] = lines
    return cache[path]


def split_silenced_suggestions(
    findings: Sequence[Finding], root: Path
) -> tuple[list[Finding], list[SilencedSuggestion]]:
    """Separate the suggestions that a comment in the sources silences from the findings.

    Only findings with the ``suggestion`` severity, a code, a file and a line can be
    silenced; every other finding is kept unchanged.

    Args:
        findings: The findings to examine.
        root: The project root that the findings' paths are relative to.

    Returns:
        The findings that remain, in their order, and the silenced suggestions.
    """
    kept: list[Finding] = []
    silenced: list[SilencedSuggestion] = []
    cache: dict[str, list[str] | None] = {}
    for finding in findings:
        code, path, line = finding.code, finding.path, finding.line
        if finding.is_suggestion and code and path and type(line) is int:
            lines = _source_lines(root, path, cache)
            if lines is not None and code in silenced_codes(lines, line):
                silenced.append(SilencedSuggestion(code, path, line))
                continue
        kept.append(finding)
    return kept, silenced
