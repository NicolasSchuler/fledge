r"""What the style suggestions share: the prose view of a document and the finding per occurrence.

The ``STY`` suggestions read the expanded document (comments and verbatim text are
already blanked). Typography and wording checks need the prose alone, so
:class:`StyleContext` also blanks the preamble, mathematics and the arguments of commands
that hold keys, file names or values rather than prose, keeping every offset, so a match
in the prose maps back to its file and line. A suggestion is reported once per
occurrence, so that a ``% fledge: ignore CODE`` comment can silence one; each rule
reports at most ``MAX_FINDINGS_PER_RULE`` and counts the rest in one more finding. A
document whose commands could not be read reports each selected suggestion as not checked.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import NamedTuple

from .cancellation import cancellation_point
from .expanded_document import ExpandedDocument
from .findings import EvidenceKind, Finding
from .literal_text import HEADING_DEPTHS, TEXT_WRAPPERS
from .tex_commands import ALL_MATH_ENVIRONMENTS
from .tex_lexer import Command, Region, blank_regions, command_end, merge_spans

# Suggestions listed per rule; the rest are counted in one further finding.
MAX_FINDINGS_PER_RULE = 50
# Reasons a document was not read that a not-checked suggestion lists in its details.
_MAX_UNREAD_REASONS = 5
# Commands whose arguments are prose, with the position of the first such argument; the
# arguments of every other command hold keys, file names or values and are blanked.
_PROSE_ARGUMENTS = {
    **dict.fromkeys(TEXT_WRAPPERS, 0),
    **dict.fromkeys(HEADING_DEPTHS, 0),
    **dict.fromkeys(("caption", "footnote", "footnotetext", "thanks", "abstract"), 0),
    "href": 1,
}
# An inline or display math delimiter that a backslash does not escape.
_MATH_DELIMITER = re.compile(r"(?<!\\)(?:\$\$?|\\[()[\]])")
_MATH_CLOSER = {"$$": "$$", "$": "$", "\\(": "\\)", "\\[": "\\]"}


class Occurrence(NamedTuple):
    """One place a suggestion applies to.

    Attributes:
        offset: Where it is in the expanded text.
        message: What the suggestion says about this place.
        suggestion: The fix for this place, or ``None`` for the rule's generic fix.
        details: Evidence for the report, or ``None``.
    """

    offset: int
    message: str
    suggestion: str | None = None
    details: dict[str, object] | None = None


def _math_spans(text: str) -> tuple[list[tuple[int, int]], list[int]]:
    r"""Find inline and display mathematics delimited by dollars, ``\(`` or ``\[``.

    One pass over the delimiters: an opener waits for its own closer, and mathematics left
    open runs to the end of the text.

    Args:
        text: The text, with the math environments already blanked.

    Returns:
        The spans of mathematics, and the offsets where ``$$`` opens display mathematics.
    """
    spans: list[tuple[int, int]] = []
    displays: list[int] = []
    opener: tuple[str, int] | None = None
    for match in _MATH_DELIMITER.finditer(text):
        token, start = match.group(), match.start()
        if opener is None:
            if token in _MATH_CLOSER:
                opener = (token, start)
                if token == "$$":
                    displays.append(start)
            continue
        if token == _MATH_CLOSER[opener[0]]:
            spans.append((opener[1], match.end()))
            opener = None
        elif opener[0] == "$" and token == "$$":
            # "$a$$b$": the first dollar closes the inline formula, the second opens one.
            spans.append((opener[1], start + 1))
            opener = ("$", start + 1)
    if opener is not None:
        spans.append((opener[1], len(text)))
    return spans, displays


def math_environment_spans(commands: Iterable[Command], size: int) -> list[tuple[int, int]]:
    r"""Find the math environments, from ``\begin`` to the end of the matching ``\end``.

    Args:
        commands: The commands of the text, in order.
        size: The length of the text, where an environment left open ends.

    Returns:
        The spans; an environment left open runs to the end of the text.
    """
    spans: list[tuple[int, int]] = []
    current: tuple[str, int] | None = None
    for command in commands:
        name = command.arguments[0].value.strip() if command.arguments else ""
        if current is None:
            if command.name == "begin" and name in ALL_MATH_ENVIRONMENTS:
                current = (name, command.start)
        elif command.name == "end" and name == current[0]:
            spans.append((current[1], command_end(command)))
            current = None
    if current is not None:
        spans.append((current[1], size))
    return spans


def _argument_spans(command: Command) -> list[tuple[int, int]]:
    """Return the spans of a command's options and of its arguments that hold no prose."""
    first_prose = _PROSE_ARGUMENTS.get(command.name, len(command.arguments))
    blanked = (*command.options, *command.arguments[:first_prose])
    return [(argument.start, argument.end) for argument in blanked]


@dataclass
class StyleContext:
    """The expanded document of one style check run, with its prose view built on first use.

    Attributes:
        document: The expanded document.
    """

    document: ExpandedDocument
    _prose: str | None = field(default=None, init=False, repr=False)
    _displays: list[int] = field(default_factory=list, init=False, repr=False)
    _segment_starts: list[int] | None = field(default=None, init=False, repr=False)

    @property
    def prose(self) -> str:
        """The body text with mathematics and non-prose arguments blanked, offsets unchanged."""
        if self._prose is None:
            self._build_prose()
        assert self._prose is not None
        return self._prose

    @property
    def display_dollars(self) -> list[int]:
        """The offsets where ``$$`` opens display mathematics in the body."""
        if self._prose is None:
            self._build_prose()
        return self._displays

    def _build_prose(self) -> None:
        """Blank the preamble, mathematics and non-prose arguments of the expanded text."""
        document = self.document
        body = [command for command in document.commands if command.start >= document.body_start]
        size = len(document.text)
        spans = [(0, document.body_start), *math_environment_spans(body, size)]
        for command in body:
            cancellation_point()
            spans.extend(_argument_spans(command))
        # Arguments nest, so the spans are merged before the text is blanked once.
        text = blank_regions(document.text, _regions(merge_spans(spans), size))
        math, displays = _math_spans(text)
        self._displays = displays
        self._prose = blank_regions(text, _regions(math, len(text)))

    def comment_between(self, start: int, end: int) -> bool:
        """Return whether the original source between two offsets holds a comment.

        A comment at the end of a line swallows the line break, so what looks like space in
        the expanded text, where comments are blanked, is none. A range that crosses from
        one file into another counts as holding a comment, so it is never reported.

        Args:
            start: The first offset of the range in the expanded text.
            end: The offset just past the range.

        Returns:
            Whether the range holds a ``%`` in the original source.
        """
        document = self.document
        if not document.segments or end <= start:
            return False
        if self._segment_starts is None:
            self._segment_starts = [segment.start for segment in document.segments]
        index = max(0, bisect_right(self._segment_starts, start) - 1)
        segment = document.segments[index]
        source = document.graph.sources.get(segment.path)
        if source is None or end > segment.end:
            return True
        offset = segment.source_start + start - segment.start
        return "%" in source.text[offset : offset + end - start]


def _regions(spans: Iterable[tuple[int, int]], size: int) -> list[Region]:
    """Turn ``(start, end)`` spans into regions for :func:`blank_regions`, clipped to ``size``."""
    return [Region(start, min(end, size), "literal") for start, end in spans if start < size]


def occurrence_findings(
    document: ExpandedDocument, rule: str, occurrences: Iterable[Occurrence]
) -> list[Finding]:
    """Report each occurrence as one suggestion, at most ``MAX_FINDINGS_PER_RULE`` of them.

    Args:
        document: The expanded document, which locates each occurrence.
        rule: The rule name of the findings.
        occurrences: The occurrences, in the order to report them.

    Returns:
        One failed suggestion per listed occurrence, then one that counts the occurrences
        not listed, if there are any.
    """
    findings: list[Finding] = []
    hidden = 0
    for occurrence in occurrences:
        if len(findings) >= MAX_FINDINGS_PER_RULE:
            hidden += 1
            continue
        location = document.location(occurrence.offset)
        findings.append(
            Finding(
                rule,
                occurrence.message,
                severity="suggestion",
                status="failed",
                path=location.path,
                line=location.line,
                evidence_kind=EvidenceKind.HEURISTIC,
                suggestion=occurrence.suggestion,
                details=occurrence.details or {},
            )
        )
    if hidden:
        findings.append(
            Finding(
                rule,
                f"{hidden} more {'occurrence' if hidden == 1 else 'occurrences'} of this "
                f"suggestion {'is' if hidden == 1 else 'are'} not listed.",
                severity="suggestion",
                status="failed",
                path=document.graph.main,
                evidence_kind=EvidenceKind.HEURISTIC,
                details={"not_listed": hidden},
            )
        )
    return findings


def unread_document_reasons(document: ExpandedDocument) -> list[str]:
    """Say why the commands of a document could not be read, if they could not.

    The style suggestions read the commands of the expanded document. When a parsing limit
    stopped the expansion, or the main file itself could not be read, there are none, and
    a suggestion that found nothing would claim a clean document.

    Args:
        document: The expanded document.

    Returns:
        At most ``_MAX_UNREAD_REASONS`` of the document's source-graph gaps when its
        commands could not be read; an empty list when they could.
    """
    if not document.parse_limit_exceeded and document.graph.main in document.selected:
        return []
    return document.graph_uncertainty[:_MAX_UNREAD_REASONS] or ["the manuscript was not read"]


def not_checked_finding(document: ExpandedDocument, rule: str, reasons: list[str]) -> Finding:
    """Report that a suggestion was not looked for, because the document was not read.

    Args:
        document: The expanded document, whose main file the finding names.
        rule: The rule name of the finding.
        reasons: Why the document was not read, as :func:`unread_document_reasons` says.

    Returns:
        An inconclusive, incomplete suggestion that lists the reasons in its details.
    """
    return Finding(
        rule,
        "Not checked: the manuscript's TeX commands could not be read, so this suggestion "
        "was not looked for.",
        severity="suggestion",
        status="inconclusive",
        path=document.graph.main,
        evidence_kind=EvidenceKind.HEURISTIC,
        incomplete=True,
        details={"not_checked": reasons},
    )
