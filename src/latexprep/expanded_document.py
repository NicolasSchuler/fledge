"""The selected literal source graph read as one expanded document.

Literal inputs are spliced in source order up to ``\\end{document}``, so checks can
scan commands across file boundaries and still report the original file and line
of every offset. Nothing is executed: macros stay unexpanded, and every gap in the
literal graph is recorded as uncertainty instead of being guessed.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections.abc import Container, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .scheduler import cancellation_point
from .source_graph import (
    COMMA_LIST_FILE_COMMANDS,
    FILE_COMMANDS,
    IMPORT_COMMANDS,
    MAX_INPUT_DEPTH,
    SECOND_ARGUMENT_FILE_COMMANDS,
    ArgumentLocation,
    ParsedSource,
    SourceGraph,
)
from .tex_lexer import (
    CONTROL_SEQUENCE,
    DEFINITION_COMMANDS,
    MAX_COMMANDS,
    MAX_TOTAL_SOURCE_BYTES,
    Command,
    ParseLimitExceeded,
    conditional_spans,
    in_conditional,
    merge_spans,
    read_group,
    scan_commands,
    split_argument_items,
)

# Commands that splice another manuscript source into the document. Class and
# style files are package evidence, not manuscript content.
CONTENT_INPUT_COMMANDS = frozenset({"input", "include", *IMPORT_COMMANDS})
# Commands whose arguments declare a macro or environment. Neither the command nor
# its replacement body is executed where it stands, so it is never an unknown
# construct in the surrounding manuscript.
MACRO_DEFINITION_COMMANDS = DEFINITION_COMMANDS | {
    "newcommand",
    "renewcommand",
    "providecommand",
    "DeclareRobustCommand",
    "newcommandx",
    "renewcommandx",
    "newenvironment",
    "renewenvironment",
    "NewDocumentCommand",
    "RenewDocumentCommand",
    "ProvideDocumentCommand",
    "DeclareDocumentCommand",
    "NewDocumentEnvironment",
    "RenewDocumentEnvironment",
    "DeclareMathOperator",
    "DeclarePairedDelimiter",
    "newtheorem",
    "declaretheorem",
    "newcolumntype",
    "newlength",
    "newsavebox",
    "newcounter",
}
# Definitions written with TeX primitives take their name and replacement text
# without braces around the name, so the lexer records no arguments for them.
_PRIMITIVE_DEFINITIONS = DEFINITION_COMMANDS


def commands_before_end(source: ParsedSource) -> list[Command]:
    """Commands up to a top-level ``\\end{document}`` or ``\\endinput``, the latter included."""
    result: list[Command] = []
    for command in source.commands:
        if command.ends("document") and command.depth == 0:
            break
        result.append(command)
        if command.name == "endinput" and command.depth == 0:
            # TeX may still consume the current physical line. Keep the stop
            # token for uncertainty reporting, but never treat its suffix as
            # definitive evidence without interpreting TeX's token buffering.
            break
    return result


def reachable_sources(graph: SourceGraph, kinds: Container[str] | None = None) -> set[str]:
    """Sources the main file reaches through active file commands, optionally of ``kinds``."""
    reached = {graph.main} if graph.main else set()
    active_keys = {
        (name, item.start, item.end)
        for name in graph.visited
        for command in commands_before_end(graph.sources[name])
        if command.name in FILE_COMMANDS and command.arguments
        for argument in command.arguments[
            (1 if command.name in SECOND_ARGUMENT_FILE_COMMANDS else 0) : (
                2 if command.name in SECOND_ARGUMENT_FILE_COMMANDS else 1
            )
        ]
        for item in split_argument_items(
            argument, comma_separated=command.name in COMMA_LIST_FILE_COMMANDS
        )
    }
    active = {
        (name, start, end): target
        for (name, start, end), (target, kind) in graph.references.items()
        if (kinds is None or kind in kinds) and (name, start, end) in active_keys
    }
    while True:
        additions = {target for (name, _, _), target in active.items() if name in reached}
        if additions <= reached:
            return reached & graph.visited
        reached.update(additions)


def command_end(command: Command) -> int:
    """The offset just after a command's last recorded argument or option."""
    return max(
        (argument.end + 1 for argument in (*command.arguments, *command.options)),
        default=command.name_end,
    )


def _definition_end(text: str, command: Command) -> int:
    """Return the offset after a definition's name, parameters and replacement body."""
    stop = command_end(command)
    if command.name not in _PRIMITIVE_DEFINITIONS:
        return stop
    line_end = text.find("\n", stop)
    line_end = len(text) if line_end < 0 else line_end
    cursor = stop
    # \let\a\b and \def\x#1{…} place the defined name outside any group.
    for _ in range(2 if command.name in {"let", "futurelet"} else 1):
        token = CONTROL_SEQUENCE.match(text, cursor)
        if token is None:
            break
        cursor = stop = token.end()
    opening = text.find("{", cursor, line_end)
    if opening >= 0:
        body = read_group(text, opening)
        if body is not None:
            return body.end + 1
    return stop


def definition_spans(text: str, commands: Sequence[Command]) -> list[tuple[int, int]]:
    """Return merged offset ranges covering definition commands and their bodies."""
    return merge_spans(
        (command.start, _definition_end(text, command))
        for command in commands
        if command.name in MACRO_DEFINITION_COMMANDS
    )


class Location(NamedTuple):
    """A source file and 1-based line; the line is unknown when nothing was expanded."""

    path: str | None
    line: int | None

    def as_dict(self) -> dict[str, object]:
        """The location as report details."""
        return {"path": self.path, "line": self.line}


@dataclass(frozen=True, slots=True)
class SourceSegment:
    """Where a span of the expanded text was copied from."""

    start: int
    end: int
    path: str
    line: int


@dataclass(frozen=True, slots=True)
class ExpandedDocument:
    """Masked literal text of the selected graph, its commands, and their source locations."""

    graph: SourceGraph
    selected: set[str]
    text: str
    commands: list[Command]
    segments: list[SourceSegment]
    # Source-graph gaps: missing inputs, unparsable files, bounded-limit stops.
    graph_uncertainty: list[str]
    # Graph gaps plus conditional control flow, which can add or remove any
    # declaration and therefore defeats every claim that something is absent.
    presence_uncertainty: list[str]
    conditionals: list[tuple[int, int]]
    # Offset just after \begin{document}; zero when the document has no body opener.
    body_start: int
    _segment_starts: list[int] = field(repr=False)
    _command_starts: list[int] = field(repr=False)
    _newlines: list[int] = field(repr=False)

    def location(self, offset: int) -> Location:
        """The original file and line of an offset in the expanded text."""
        if not self.segments:
            return Location(self.graph.main, None)
        index = max(0, bisect_right(self._segment_starts, offset) - 1)
        segment = self.segments[index]
        line = (
            segment.line
            + bisect_left(self._newlines, offset)
            - bisect_left(self._newlines, segment.start)
        )
        return Location(segment.path, line)

    def located(self, offset: int, **details: object) -> dict[str, object]:
        """Report details for an offset: its path and line followed by ``details``."""
        return {**self.location(offset).as_dict(), **details}

    def between(self, start: int, end: int) -> list[Command]:
        """Commands that start inside ``[start, end)``."""
        return self.commands[
            bisect_left(self._command_starts, start) : bisect_left(self._command_starts, end)
        ]


@dataclass
class _Splice:
    """Accumulates the expanded text while following literal inputs depth first."""

    graph: SourceGraph
    uncertainty: list[str]
    chunks: list[str] = field(default_factory=list)
    segments: list[SourceSegment] = field(default_factory=list)
    size: int = 0
    events: int = 0
    # Set at \end{document}: nothing after it, in any file, is manuscript content.
    stopped: bool = False

    def append(self, value: str, path: str, line: int) -> None:
        if not value:
            return
        if self.size + len(value) > MAX_TOTAL_SOURCE_BYTES:
            raise ParseLimitExceeded
        self.segments.append(SourceSegment(self.size, self.size + len(value), path, line))
        self.chunks.append(value)
        self.size += len(value)

    def expand(self, path: str, stack: tuple[str, ...]) -> None:
        cancellation_point()
        if path in stack or len(stack) >= MAX_INPUT_DEPTH:
            self.uncertainty.append("cyclic or excessively deep literal input graph")
            return
        source = self.graph.sources.get(path)
        if source is None:
            self.uncertainty.append(f"{path}: included source was not inspectable")
            return
        cursor = 0
        newlines = [index for index, char in enumerate(source.masked) if char == "\n"]

        def line_of(offset: int) -> int:
            return bisect_right(newlines, offset - 1) + 1

        for command in commands_before_end(source):
            self.events += 1
            if self.events > MAX_COMMANDS:
                raise ParseLimitExceeded
            if command.name == "endinput":
                suffix = source.masked[command_end(command) :].split("\n", 1)[0]
                if command.depth or suffix.strip():
                    self.uncertainty.append(f"{path}: unresolved endinput context")
                self.append(source.masked[cursor : command.start], path, line_of(cursor))
                return
            if command.name not in CONTENT_INPUT_COMMANDS:
                continue
            argument_index = 1 if command.name in IMPORT_COMMANDS else 0
            if command.depth or len(command.arguments) <= argument_index:
                self.uncertainty.append(f"{path}: nonliteral or grouped inclusion")
                continue
            argument = command.arguments[argument_index].stripped()
            resolved = self.graph.references.get(
                ArgumentLocation(path, argument.start, argument.end)
            )
            if resolved is None:
                self.uncertainty.append(f"{path}: unresolved literal inclusion")
                continue
            if len(command.arguments) > argument_index + 1 or command.options:
                self.uncertainty.append(
                    f"{path}: trailing grouped or optional syntax follows a source inclusion"
                )
            self.append(source.masked[cursor : command.start], path, line_of(cursor))
            self.expand(resolved[0], (*stack, path))
            # The shared lexer records subsequent groups as possible arguments.
            # Input has only one: preserve later groups and bare-input terminators.
            cursor = command.arguments[argument_index].end
            if source.masked[cursor : cursor + 1] == "}":
                cursor += 1
            if self.stopped:
                return
        closing = next(
            (
                command
                for command in source.commands
                if command.ends("document") and not command.depth
            ),
            None,
        )
        end = closing.start if closing else len(source.masked)
        self.append(source.masked[cursor:end], path, line_of(cursor))
        if closing:
            self.stopped = True


def _graph_gaps(graph: SourceGraph, selected: set[str]) -> list[str]:
    gaps = []
    if graph.main not in selected:
        gaps.append("no inspectable selected main source")
    if graph.subfile_targets:
        gaps.append("subfile body/preamble content interpretation is not supported by this check")
    scope = selected | {graph.main}
    gaps.extend(
        f"{finding.path}: {finding.message}"
        for finding in graph.findings
        if finding.rule.startswith("source.")
        and (finding.path in scope or finding.path is None)
        and finding.status in {"failed", "inconclusive"}
    )
    return gaps


def expand_document(
    root: Path, main: str | None, graph: SourceGraph | None = None
) -> ExpandedDocument:
    """Expand the selected literal graph; reuse ``graph`` when the caller already built it."""
    graph = graph or SourceGraph.build(root, main)
    selected = reachable_sources(graph)
    splice = _Splice(graph, _graph_gaps(graph, selected))
    try:
        if graph.main in selected:
            assert graph.main is not None
            splice.expand(graph.main, ())
        text = "".join(splice.chunks)
        commands = scan_commands(text)
        if len(commands) > MAX_COMMANDS:
            raise ParseLimitExceeded
    except ParseLimitExceeded:
        splice.uncertainty.append("expanded manuscript exceeded a bounded parsing limit")
        text = "".join(splice.chunks)
        commands = []
    graph_uncertainty = sorted(set(splice.uncertainty))
    conditionals = conditional_spans(commands)
    presence_uncertainty = list(graph_uncertainty)
    if conditionals:
        presence_uncertainty.append(
            "conditional source control flow can add or remove declarations"
        )
    # Preamble declarations configure the toolchain; they are not manuscript content.
    opening = next(
        (command for command in commands if command.begins("document") and not command.depth),
        None,
    )
    return ExpandedDocument(
        graph=graph,
        selected=selected,
        text=text,
        commands=commands,
        segments=splice.segments,
        graph_uncertainty=graph_uncertainty,
        presence_uncertainty=presence_uncertainty,
        conditionals=conditionals,
        body_start=command_end(opening) if opening is not None else 0,
        _segment_starts=[segment.start for segment in splice.segments],
        _command_starts=[command.start for command in commands],
        _newlines=[index for index, char in enumerate(text) if char == "\n"],
    )


@dataclass
class EnvironmentRegion:
    """The body of one literal environment, the commands in it, and why it may be unclear."""

    kind: str
    command: Command
    start: int
    end: int
    commands: list[Command] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)


def environment_regions(
    document: ExpandedDocument, kinds: Container[str]
) -> tuple[list[EnvironmentRegion], list[str]]:
    """Environments named in ``kinds``, in source order, and delimiter problems.

    A region's uncertainty covers its own structure (grouping, nesting, a missing
    end) and enclosing conditional control flow, not unknown constructs inside it.
    """
    result: list[EnvironmentRegion] = []
    stack: list[EnvironmentRegion] = []
    reasons: list[str] = []
    for command in document.commands:
        cancellation_point()
        environment = command.arguments[0].value.strip() if command.arguments else ""
        if command.name == "begin" and environment in kinds:
            region = EnvironmentRegion(
                environment, command, command_end(command), len(document.text)
            )
            if command.depth:
                region.uncertainty.append("environment declared inside a group or macro argument")
            if stack:
                region.uncertainty.append("nested manuscript environments")
                stack[-1].uncertainty.append("nested manuscript environments")
            stack.append(region)
        elif command.name == "end" and environment in kinds:
            if not stack or stack[-1].kind != environment:
                reasons.append("unmatched or crossed environment delimiters")
                continue
            region = stack.pop()
            region.end = command.start
            result.append(region)
        elif stack:
            stack[-1].commands.append(command)
    for region in stack:
        region.uncertainty.append("environment has no closing delimiter")
        result.append(region)
    for region in result:
        if in_conditional(document.conditionals, region.command.start):
            region.uncertainty.append("conditional source control flow")
    return sorted(result, key=lambda region: region.command.start), reasons
