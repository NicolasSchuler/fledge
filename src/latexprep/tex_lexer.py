"""Bounded lexical subset of LaTeX shared by source inspection, checks and transformations.

This is not a TeX tokenizer. It recognizes control sequences, balanced brace and
bracket groups, comments and common verbatim-like regions without expanding
macros or tracking category codes. Offsets always refer to the scanned text, and
masking keeps every offset and line break in place.
"""

from __future__ import annotations

import bisect
import re
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal, NamedTuple

from .scheduler import cancellation_point

# Bounds shared by every reader of the lexical subset.
MAX_SOURCE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 32 * 1024 * 1024
MAX_COMMANDS = 100_000
MAX_ARGUMENT_CHARS = 64 * 1024

# A control word, with ``@`` as a letter as in package code, or a control symbol
# on the same line.
CONTROL_SEQUENCE = re.compile(r"\\([A-Za-z@]+|[^\r\n])")
# Environments whose body is read verbatim up to the matching \end.
VERBATIM_ENVIRONMENTS = frozenset(
    {
        "verbatim",
        "verbatim*",
        "Verbatim",
        "BVerbatim",
        "LVerbatim",
        "lstlisting",
        "minted",
        "comment",
    }
)
# Commands whose argument is read verbatim, either braced or between a repeated delimiter.
_VERBATIM_COMMANDS = frozenset({"verb", "lstinline", "mintinline", "url", "nolinkurl", "path"})
# Tokens after these commands name a macro being defined, not a conditional being run.
DEFINITION_COMMANDS = frozenset(
    {"newif", "def", "gdef", "edef", "xdef", "let", "futurelet", "global", "long", "protected"}
)
# Package conditionals that take braced branches instead of ending with \fi.
_ARGUMENT_CONDITIONALS = frozenset(
    {
        "ifthenelse",
        "iftoggle",
        "ifbool",
        "ifboolexpr",
        "ifdef",
        "ifundef",
        "ifdefempty",
        "ifdefvoid",
        "ifdefstring",
        "ifcsdef",
        "ifcsundef",
        "ifcsvoid",
        "ifstrequal",
        "ifstrempty",
        "ifblank",
        "ifnumcomp",
        "ifdimcomp",
        "ifpackageloaded",
        "ifclassloaded",
    }
)


class ParseLimitExceeded(Exception):
    """The lexical subset cannot establish bounded, complete argument spans."""


@dataclass(frozen=True, slots=True)
class Argument:
    """A brace or bracket group; ``start`` and ``end`` delimit ``value`` inside the delimiters."""

    start: int
    end: int
    value: str

    def stripped(self) -> Argument:
        """The value without surrounding whitespace, with offsets that still match it."""
        start = self.start + len(self.value) - len(self.value.lstrip())
        value = self.value.strip()
        return Argument(start, start + len(value), value)


@dataclass(frozen=True, slots=True)
class Command:
    """A control sequence with the groups that directly follow it.

    The lexer cannot know a command's signature, so every following brace group
    is recorded as a possible argument and every bracket group as a possible option.
    """

    name: str
    start: int
    line: int
    depth: int
    arguments: tuple[Argument, ...]
    options: tuple[Argument, ...]

    @property
    def name_end(self) -> int:
        """Offset just after the control-sequence name, before any star or group."""
        return self.start + len(self.name) + 1

    def begins(self, environment: str, *, exact: bool = False) -> bool:
        """Whether this is ``\\begin{environment}``.

        Spaces around the name inside the braces are ignored unless ``exact`` is set.
        """
        return self.name == "begin" and self._names_environment(environment, exact)

    def ends(self, environment: str, *, exact: bool = False) -> bool:
        """Whether this is ``\\end{environment}``; ``exact`` as for :meth:`begins`."""
        return self.name == "end" and self._names_environment(environment, exact)

    def _names_environment(self, environment: str, exact: bool) -> bool:
        if not self.arguments:
            return False
        value = self.arguments[0].value
        return (value if exact else value.strip()) == environment


RegionKind = Literal["comment", "literal"]


class Region(NamedTuple):
    """A comment from ``%`` to the end of its line, or a verbatim-like literal span.

    A comment's ``end`` excludes the line terminator, including a carriage return.
    """

    start: int
    end: int
    kind: RegionKind


class RegionScan(NamedTuple):
    regions: list[Region]
    # Offsets of literal regions without a closing delimiter; they extend to the
    # end of the text (or line), so later commands cannot be inspected reliably.
    unclosed: list[int]


def skip_space(text: str, pos: int) -> int:
    while pos < len(text) and text[pos].isspace():
        pos += 1
    return pos


def skip_trivia(text: str, pos: int) -> int:
    """Skip whitespace and whole comment lines."""
    while True:
        pos = skip_space(text, pos)
        if pos < len(text) and text[pos] == "%":
            end = text.find("\n", pos)
            pos = len(text) if end < 0 else end + 1
        else:
            return pos


def read_group(text: str, pos: int, opening: str = "{", closing: str = "}") -> Argument | None:
    """Read a balanced group starting at ``pos``; ``None`` if absent, unclosed or oversized.

    A bracket group ignores brackets nested inside braces, as in ``[{a]}]``.
    """
    if pos >= len(text) or text[pos] != opening:
        return None
    depth, cursor = 1, pos + 1
    brace_depth = 0
    while cursor < len(text) and cursor - pos <= MAX_ARGUMENT_CHARS:
        char = text[cursor]
        if char == "\\":
            cursor += 2
            continue
        if opening == "[":
            if char == "{":
                brace_depth += 1
            elif char == "}":
                brace_depth -= 1
            if brace_depth:
                cursor += 1
                continue
        if char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return Argument(pos + 1, cursor, text[pos + 1 : cursor])
        cursor += 1
    return None


def source_regions(text: str) -> RegionScan:
    """Return comment and literal regions and unclosed literal offsets, without TeX expansion."""
    regions: list[Region] = []
    unclosed: list[int] = []
    pos = 0
    while pos < len(text):
        if pos % 4096 == 0:
            cancellation_point()
        if text[pos] == "%":
            end = text.find("\n", pos)
            end = len(text) if end < 0 else end
            regions.append(Region(pos, end - (text[end - 1 : end] == "\r"), "comment"))
            pos = end
            continue
        if text[pos] != "\\":
            pos += 1
            continue
        token = CONTROL_SEQUENCE.match(text, pos)
        if not token:
            pos += 1
            continue
        name, end = token.group(1), token.end()
        if name == "begin":
            environment = read_group(text, skip_trivia(text, end))
            if environment and environment.value.strip() in VERBATIM_ENVIRONMENTS:
                closing = re.search(
                    r"\\end\s*\{\s*" + re.escape(environment.value.strip()) + r"\s*\}",
                    text[environment.end + 1 :],
                )
                if closing:
                    end = environment.end + 1 + closing.end()
                else:
                    end = len(text)
                    unclosed.append(pos)
                regions.append(Region(pos, end, "literal"))
                pos = end
                continue
        if name in _VERBATIM_COMMANDS:
            cursor = end
            if cursor < len(text) and text[cursor] == "*":
                cursor += 1
            if name != "verb":
                cursor = skip_trivia(text, cursor)
                option = read_group(text, cursor, "[", "]")
                if option:
                    cursor = skip_trivia(text, option.end + 1)
                if name == "mintinline":
                    language = read_group(text, cursor)
                    if language:
                        cursor = skip_trivia(text, language.end + 1)
            if cursor < len(text) and not text[cursor].isspace():
                if text[cursor] == "{" and name != "verb":
                    argument = read_group(text, cursor)
                    end = argument.end + 1 if argument else len(text)
                    if not argument:
                        unclosed.append(pos)
                else:
                    delimiter = text[cursor]
                    line_end = text.find("\n", cursor)
                    line_end = len(text) if line_end < 0 else line_end
                    close = text.find(delimiter, cursor + 1, line_end)
                    end = close + 1 if close >= 0 else line_end
                    if close < 0:
                        unclosed.append(pos)
                regions.append(Region(pos, end, "literal"))
                pos = end
                continue
        pos = end
    return RegionScan(regions, unclosed)


def blank_regions(text: str, regions: Iterable[Region]) -> str:
    """Replace every character inside ``regions`` except line breaks with a space."""
    buffer = list(text)
    for start, end, _ in regions:
        buffer[start:end] = [char if char in "\r\n" else " " for char in buffer[start:end]]
    return "".join(buffer)


def mask_literals(text: str) -> str:
    """Blank comments and common literal regions while retaining offsets/newlines."""
    return blank_regions(text, source_regions(text).regions)


def scan_commands(masked: str) -> list[Command]:
    """Return the commands of literal-masked text with their following groups.

    Raises ``ParseLimitExceeded`` when an unbalanced brace leaves later argument
    spans unreadable or the arguments exceed their bounded total size.
    """
    result: list[Command] = []
    depth = pos = 0
    line = 1
    argument_chars = 0
    while pos < len(masked):
        if pos % 4096 == 0:
            cancellation_point()
        char = masked[pos]
        if char == "\n":
            line += 1
        elif char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        elif char == "\\":
            token = CONTROL_SEQUENCE.match(masked, pos)
            if token:
                cursor = token.end()
                if cursor < len(masked) and masked[cursor] == "*":
                    cursor += 1
                arguments: list[Argument] = []
                options: list[Argument] = []
                name = token.group(1)
                for _ in range(6):
                    skipped = skip_space(masked, cursor)
                    # A paragraph break ends argument scanning: the next group starts
                    # new text instead of completing this command.
                    if masked.count("\n", cursor, skipped) > 1:
                        break
                    cursor = skipped
                    option = read_group(masked, cursor, "[", "]")
                    argument = read_group(masked, cursor)
                    if option:
                        options.append(option)
                        cursor = option.end + 1
                    elif argument:
                        arguments.append(argument)
                        cursor = argument.end + 1
                    else:
                        # An unbalanced brace leaves every later argument span
                        # unreadable, but an unmatched bracket is ordinary text such
                        # as "[0, 1)" and only ends this command's arguments.
                        if cursor < len(masked) and masked[cursor] == "{":
                            raise ParseLimitExceeded
                        break
                argument_chars += sum(len(group.value) for group in (*arguments, *options))
                if argument_chars > min(MAX_SOURCE_BYTES, len(masked) * 4):
                    raise ParseLimitExceeded
                if not arguments and name == "input":
                    start = skip_space(masked, token.end())
                    match = re.match(r"[^\s\\{}\[\]]+", masked[start:])
                    if match:
                        arguments.append(Argument(start, start + match.end(), match.group()))
                result.append(Command(name, pos, line, depth, tuple(arguments), tuple(options)))
                if len(result) > MAX_COMMANDS:
                    return result
                pos = token.end()
                continue
        pos += 1
    return result


def split_argument_items(argument: Argument, *, comma_separated: bool = True) -> list[Argument]:
    """Return the stripped items of a comma-separated argument, or the whole stripped argument."""
    if not comma_separated:
        return [argument.stripped()]
    items: list[Argument] = []
    cursor = argument.start
    for value in argument.value.split(","):
        items.append(Argument(cursor, cursor + len(value), value).stripped())
        cursor += len(value) + 1
    return items


def merge_spans(spans: Iterable[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sort ``(start, end)`` spans and join those that overlap or touch."""
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def conditional_spans(commands: list[Command]) -> list[tuple[int, int]]:
    """Return offset ranges enclosed by primitive-style ``\\if…`` … ``\\fi`` conditionals.

    A conditional name defined by ``\\newif`` or ``\\def`` is not itself executed,
    and an opener without a matching ``\\fi`` (such as a branch-taking package
    conditional) is ignored rather than extended to the end of the file.
    """
    spans: list[tuple[int, int]] = []
    stack: list[int] = []
    previous: Command | None = None
    for command in commands:
        defined = (
            previous is not None
            and previous.name in DEFINITION_COMMANDS
            and not previous.arguments
            and command.start == previous.name_end
        )
        if command.name == "fi":
            if stack:
                start = stack.pop()
                spans.append((start, command.start))
        elif (
            command.name.startswith("if")
            and command.name not in _ARGUMENT_CONDITIONALS
            and not defined
        ):
            stack.append(command.start)
        previous = command
    return merge_spans(spans)


# The point-in-span tests below share one lookup over merged, sorted spans but
# differ in whether each boundary belongs to the span; callers depend on that.


def _span_starting_at_or_before(
    spans: Sequence[tuple[int, int]], offset: int
) -> tuple[int, int] | None:
    index = bisect.bisect_right(spans, (offset, float("inf"))) - 1
    return spans[index] if index >= 0 else None


def in_conditional(spans: Sequence[tuple[int, int]], offset: int) -> bool:
    """Whether ``offset`` lies strictly inside one of the merged ``conditional_spans``.

    The opening ``\\if…`` (at a span's start) and the closing ``\\fi`` (at its end)
    are outside, so only enclosed commands count as conditional.
    """
    span = _span_starting_at_or_before(spans, offset)
    return span is not None and span[0] < offset < span[1]


def in_half_open_span(spans: Sequence[tuple[int, int]], offset: int) -> bool:
    """Whether ``start <= offset < end`` for one of the merged ``spans``."""
    span = _span_starting_at_or_before(spans, offset)
    return span is not None and span[0] <= offset < span[1]


def in_closed_span(spans: Sequence[tuple[int, int]], offset: int) -> bool:
    """Whether ``start <= offset <= end`` for one of the merged ``spans``."""
    span = _span_starting_at_or_before(spans, offset)
    return span is not None and span[0] <= offset <= span[1]
