"""The selected literal source graph read as one expanded document.

Literal inputs are spliced in source order up to ``\\end{document}``, so checks can
scan commands across file boundaries and still report the original file and line
of every offset. Nothing is executed: macros stay unexpanded, and every gap in the
literal graph is recorded as uncertainty instead of being guessed.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from collections.abc import Container, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import NamedTuple

from .cancellation import cancellation_point
from .findings import Finding
from .source_file_commands import (
    COMMA_LIST_FILE_COMMANDS,
    FILE_COMMANDS,
    IMPORT_COMMANDS,
    file_argument_index,
)
from .source_graph import MAX_INPUT_DEPTH, ArgumentLocation, SourceGraph
from .source_loading import ParsedSource
from .tex_lexer import (
    MAX_COMMANDS,
    MAX_TOTAL_SOURCE_BYTES,
    Command,
    ParseLimitExceeded,
    command_end,
    conditional_spans,
    in_conditional,
    scan_commands,
    split_argument_items,
)

# Commands that splice another manuscript source into the document. Class and
# style files are package evidence, not manuscript content.
CONTENT_INPUT_COMMANDS = frozenset({"input", "include", *IMPORT_COMMANDS})


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


def _active_locations(graph: SourceGraph) -> set[ArgumentLocation]:
    """Where the visited files name a file before their ``\\end{document}`` or ``\\endinput``."""
    locations: set[ArgumentLocation] = set()
    for name in graph.visited:
        for command in commands_before_end(graph.sources[name]):
            if command.name not in FILE_COMMANDS:
                continue
            for argument in command.arguments[file_argument_index(command.name) :][:1]:
                items = split_argument_items(
                    argument, comma_separated=command.name in COMMA_LIST_FILE_COMMANDS
                )
                locations.update(ArgumentLocation(name, item.start, item.end) for item in items)
    return locations


def reachable_sources(graph: SourceGraph, kinds: Container[str] | None = None) -> set[str]:
    """Sources the main file reaches through active file commands, optionally of ``kinds``."""
    active = _active_locations(graph)
    reads: dict[str, set[str]] = defaultdict(set)
    for location, reference in graph.references.items():
        if location in active and (kinds is None or reference.command in kinds):
            reads[location.path].add(reference.target)
    reached = {graph.main} if graph.main else set()
    pending = deque(reached)
    while pending:
        for target in reads[pending.popleft()] - reached:
            reached.add(target)
            pending.append(target)
    return reached & graph.visited


class Location(NamedTuple):
    """A source file and 1-based line; the line is unknown when nothing was expanded."""

    path: str | None
    line: int | None

    def as_dict(self) -> dict[str, object]:
        """The location as report details."""
        return {"path": self.path, "line": self.line}


@dataclass(frozen=True, slots=True)
class SourceSegment:
    """Where a span of the expanded text was copied from.

    ``start`` and ``end`` bound the span in the expanded text; ``path`` and ``line`` name the
    file and 1-based line it starts on, and ``source_start`` is its offset in that file's
    masked text (which has the same offsets as its raw text).
    """

    start: int
    end: int
    path: str
    line: int
    source_start: int


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
    # Offsets in the expanded text of the comments blanked in the copied sources.
    comment_starts: list[int] = field(default_factory=list)
    size: int = 0
    events: int = 0
    # Set at \end{document}: nothing after it, in any file, is manuscript content.
    stopped: bool = False

    def append(
        self, value: str, path: str, line: int, source_start: int, comments: Sequence[int] = ()
    ) -> None:
        """Add ``value``, copied from ``path`` at ``source_start``; ``comments`` lie within it."""
        if not value:
            return
        if self.size + len(value) > MAX_TOTAL_SOURCE_BYTES:
            raise ParseLimitExceeded("expanded_size", self.size)
        self.segments.append(
            SourceSegment(self.size, self.size + len(value), path, line, source_start)
        )
        self.chunks.append(value)
        self.comment_starts.extend(self.size + offset for offset in comments)
        self.size += len(value)

    def _inclusion_target(self, path: str, command: Command) -> str | None:
        """The file a ``\\input``-like command splices in, or ``None`` after noting why not."""
        index = file_argument_index(command.name)
        if command.depth or len(command.arguments) <= index:
            self.uncertainty.append(f"{path}: nonliteral or grouped inclusion")
            return None
        argument = command.arguments[index].stripped()
        resolved = self.graph.references.get(ArgumentLocation(path, argument.start, argument.end))
        if resolved is None:
            self.uncertainty.append(f"{path}: unresolved literal inclusion")
            return None
        if len(command.arguments) > index + 1 or command.options:
            self.uncertainty.append(
                f"{path}: trailing grouped or optional syntax follows a source inclusion"
            )
        return resolved.target

    def _count_command(self, command: Command) -> None:
        """Count a command against the budget shared by every file being spliced."""
        self.events += 1
        if self.events > MAX_COMMANDS:
            raise ParseLimitExceeded("command_count", command.start)

    def _note_unclear_endinput(self, path: str, source: ParsedSource, command: Command) -> None:
        """Record that ``\\endinput`` stops this file where the text after it is not plain."""
        suffix = source.masked[command_end(command) :].split("\n", 1)[0]
        if command.depth or suffix.strip():
            self.uncertainty.append(f"{path}: unresolved endinput context")

    def expand(self, path: str, stack: tuple[str, ...]) -> None:
        """Append the masked text of ``path`` up to its end, splicing in each literal input."""
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

        def append_masked(start: int, end: int) -> None:
            """Copy ``source.masked[start:end]``, keeping where its comments were."""
            low = bisect_left(source.comment_starts, start)
            high = bisect_left(source.comment_starts, end)
            comments = [offset - start for offset in source.comment_starts[low:high]]
            line = bisect_right(newlines, start - 1) + 1
            self.append(source.masked[start:end], path, line, start, comments)

        for command in commands_before_end(source):
            self._count_command(command)
            if command.name == "endinput":
                self._note_unclear_endinput(path, source, command)
                append_masked(cursor, command.start)
                return
            if command.name not in CONTENT_INPUT_COMMANDS:
                continue
            target = self._inclusion_target(path, command)
            if target is None:
                continue
            append_masked(cursor, command.start)
            self.expand(target, (*stack, path))
            cursor = _end_of_inclusion(source, command)
            if self.stopped:
                return
        closing = _document_end(source)
        append_masked(cursor, closing.start if closing else len(source.masked))
        if closing:
            self.stopped = True


def _end_of_inclusion(source: ParsedSource, command: Command) -> int:
    """Where the masked text resumes after an input command.

    The shared lexer records subsequent groups as possible arguments. Input has only one:
    later groups and bare-input terminators are preserved.
    """
    cursor = command.arguments[file_argument_index(command.name)].end
    if source.masked[cursor : cursor + 1] == "}":
        cursor += 1
    return cursor


def _document_end(source: ParsedSource) -> Command | None:
    """The top-level ``\\end{document}`` of a source, if it has one."""
    return next(
        (command for command in source.commands if command.ends("document") and not command.depth),
        None,
    )


# A file that cannot be resolved only leaves the manuscript undecidable when it could
# supply source text. A missing figure or bibliography cannot hold a title, abstract,
# keywords, heading or declaration, so it never hides what a constraint looks for.
_UNRESOLVED_FILE_RULES = frozenset({"source.missing_dependency", "source.case_mismatch"})
_NON_CONTENT_FILE_COMMANDS = frozenset(
    {"includegraphics", "includepdf", "bibliography", "addbibresource", "bibliographystyle"}
)


def unresolved_non_content_file(finding: Finding) -> bool:
    """Whether ``finding`` reports a figure or bibliography file that cannot be found."""
    return (
        finding.rule in _UNRESOLVED_FILE_RULES
        and finding.details.get("command") in _NON_CONTENT_FILE_COMMANDS
    )


def _graph_gaps(graph: SourceGraph, selected: set[str]) -> list[str]:
    """The gaps in the spliced text: every unresolved ``source.*`` finding in scope.

    That is a superset of ``INCOMPLETE_GRAPH_RULES``, which names only the findings that make
    the dependency graph incomplete; other unresolved findings, such as an unsupported
    search-path form, are gaps here as well.
    """
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
        and not unresolved_non_content_file(finding)
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
        commands = scan_commands(text, splice.comment_starts)
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


def _open_region(
    document: ExpandedDocument,
    command: Command,
    environment: str,
    enclosing: list[EnvironmentRegion],
) -> EnvironmentRegion:
    """Start the region of a ``\\begin`` and mark it and the open region as nested."""
    region = EnvironmentRegion(environment, command, command_end(command), len(document.text))
    if command.depth:
        region.uncertainty.append("environment declared inside a group or macro argument")
    if enclosing:
        region.uncertainty.append("nested manuscript environments")
        enclosing[-1].uncertainty.append("nested manuscript environments")
    return region


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
            stack.append(_open_region(document, command, environment, stack))
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
