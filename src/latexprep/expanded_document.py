r"""The selected literal source graph read as one expanded document.

Literal inputs are spliced in source order up to ``\end{document}``, so checks can
scan commands across file boundaries and still report the original file and line
of every offset. Nothing is executed: macros stay unexpanded, and every gap in the
literal graph is recorded as uncertainty instead of being guessed.
"""

from __future__ import annotations

from bisect import bisect_left, bisect_right
from collections import defaultdict, deque
from collections.abc import Container, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
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
from .source_findings import can_hide_manuscript_text
from .source_graph import MAX_INPUT_DEPTH, ArgumentLocation, SourceGraph
from .source_loading import ParsedSource
from .tex_lexer import (
    MAX_COMMANDS,
    MAX_TOTAL_SOURCE_BYTES,
    Command,
    ParseLimitExceeded,
    scan_commands,
    split_argument_items,
)
from .tex_spans import (
    command_end,
    conditional_spans,
    defined_names,
    definition_spans,
    endinput_has_suffix,
    in_conditional,
    is_source_terminator,
    runs_in_place,
    stops_source,
)

# Commands that splice another manuscript source into the document. Class and
# style files are package evidence, not manuscript content.
CONTENT_INPUT_COMMANDS = frozenset({"input", "include", *IMPORT_COMMANDS})
# Commands whose targets hold document text: content inputs and subfiles. A selected source
# reached only through class and package commands is package code.
_DOCUMENT_TEXT_COMMANDS = CONTENT_INPUT_COMMANDS | {"subfile"}
# Tokens that copy a macro under another name, so that the copy may run it later.
_ALIASING_COMMANDS = frozenset({"let", "futurelet"})


class DocumentPart(Enum):
    r"""A part of the document in the order LaTeX reads it, which decides what can still change.

    Attributes:
        PROLOGUE: Up to the end of the ``\documentclass`` declaration. Only what runs here
            can pass options to the class.
        PREAMBLE: From the class declaration to ``\begin{document}``. Packages can be
            loaded only here and in the prologue.
        BODY: After ``\begin{document}``.
    """

    PROLOGUE = "prologue"
    PREAMBLE = "preamble"
    BODY = "body"


# Where a construct the scan cannot place may run.
EVERY_PART = frozenset(DocumentPart)


def finding_reason(finding: Finding) -> str:
    """Quote a source finding as a reason, its file first when it names one."""
    return f"{finding.path}: {finding.message}" if finding.path else finding.message


def commands_before_end(
    source: ParsedSource, *, may_not_run: Sequence[tuple[int, int]] | None = None
) -> list[Command]:
    r"""Collect the commands up to a top-level ``\end{document}`` or ``\endinput``.

    The ``\endinput`` is included in the result.

    Args:
        source: The parsed source.
        may_not_run: Spans of primitive conditionals, computed when omitted. A stop
            inside one may not run, so it remains in the result for callers to judge.

    Returns:
        The commands, in source order.
    """
    conditionals = conditional_spans(source.commands) if may_not_run is None else may_not_run
    result: list[Command] = []
    for command in source.commands:
        if stops_source(command, conditionals):
            # TeX may still consume the current physical line. Keep the stop
            # token for uncertainty reporting, but never treat its suffix as
            # definitive evidence without interpreting TeX's token buffering.
            if command.name == "endinput":
                result.append(command)
            break
        result.append(command)
    return result


def _uses(command: Command, name: str) -> bool:
    r"""Return whether ``command`` may run the project definition ``name``.

    Args:
        command: A command of the document or of package code.
        name: The defined command without its backslash, or ``begin{name}``.

    Returns:
        ``True`` for the command itself, an alias of it (``\let\b\a``), ``\@nameuse{a}``,
        or ``\begin{e}`` and ``\e`` for an environment ``begin{e}``.
    """
    if command.name == "@nameuse":
        return bool(command.arguments) and command.arguments[0].value.strip() == name
    if name.startswith("begin{"):
        if command.name == "begin" and not command.named and command.arguments:
            return f"begin{{{command.arguments[0].value.strip()}}}" == name
        name = name[len("begin{") : -1]
    return command.name == name and (not command.named or command.named_by in _ALIASING_COMMANDS)


def _active_locations(graph: SourceGraph) -> set[ArgumentLocation]:
    r"""Locate the file names that the visited files give before their end.

    A file ends at its top-level ``\end{document}`` or ``\endinput``.
    """
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
    """Find the sources the main file reaches through active file commands.

    Args:
        graph: The literal source graph.
        kinds: The file commands to follow, or ``None`` for every file command.

    Returns:
        The reached sources that the graph visited, the main file included.
    """
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


def package_code_sources(graph: SourceGraph, selected: set[str]) -> set[str]:
    r"""Return the selected class and package files that hold no document text.

    Document text follows ``\input``, ``\include``, ``\import`` and ``\subfile`` from the
    main file. A project ``.cls`` or ``.sty`` reached only through ``\documentclass``,
    ``\usepackage`` and their relatives runs as preamble code: what the scan cannot follow
    inside it leaves the dependency graph incomplete, but not the document text.

    Args:
        graph: The literal source graph.
        selected: The selected sources, by relative path.

    Returns:
        The selected sources that no document-text command reaches, never the main file.
    """
    return selected - reachable_sources(graph, _DOCUMENT_TEXT_COMMANDS) - {graph.main}


class Location(NamedTuple):
    """A source file and 1-based line; the line is unknown when nothing was expanded.

    Attributes:
        path: The source file, or ``None``.
        line: The 1-based line, or ``None``.
    """

    path: str | None
    line: int | None

    def as_dict(self) -> dict[str, object]:
        """Return the location as report details."""
        return {"path": self.path, "line": self.line}


@dataclass(frozen=True, slots=True)
class SourceSegment:
    """Where a span of the expanded text was copied from.

    Attributes:
        start: Where the span starts in the expanded text.
        end: Where the span ends in the expanded text.
        path: The file the span was copied from.
        line: The 1-based line of ``path`` that the span starts on.
        source_start: The span's offset in that file's masked text (which has the same
            offsets as its raw text).
    """

    start: int
    end: int
    path: str
    line: int
    source_start: int


@dataclass(frozen=True, slots=True)
class ExpandedDocument:
    r"""Masked literal text of the selected graph, its commands, and their source locations.

    Attributes:
        graph: The literal source graph.
        selected: The sources the main file reaches.
        text: The masked literal text.
        commands: The commands of ``text``.
        segments: Where each span of ``text`` was copied from.
        graph_uncertainty: Source-graph gaps: missing inputs, unparsable files,
            bounded-limit stops. Conditional control flow is not one: both branches are in
            ``text``, and each check decides what a command inside ``conditionals`` means
            for it.
        conditionals: The spans of conditional control flow in ``text``.
        body_start: Where the document body starts in ``text``: after the first
            ``\begin{document}`` outside every conditional, or after the first one at all
            when a conditional encloses each.
        parse_limit_exceeded: Whether the spliced text exceeded a bounded parsing limit, so
            that ``commands`` is empty whatever the text holds.
    """

    graph: SourceGraph
    selected: set[str]
    text: str
    commands: list[Command]
    segments: list[SourceSegment]
    graph_uncertainty: list[str]
    conditionals: list[tuple[int, int]]
    # Offset just after \begin{document}; zero when the document has no body opener.
    body_start: int
    parse_limit_exceeded: bool
    _segment_starts: list[int] = field(repr=False)
    _command_starts: list[int] = field(repr=False)
    _newlines: list[int] = field(repr=False)
    # Offset just after the last \documentclass before the body; None without one.
    _class_end: int | None = field(repr=False)
    # Offset just after the first \begin{document}, which a conditional may enclose, and
    # the start of the first one that no conditional encloses; None without such an opener.
    # Between the two, the text may belong to the preamble or to the body.
    _body_may_start: int | None = field(default=None, repr=False)
    _body_sure_from: int | None = field(default=None, repr=False)
    # Where each project definition asked about is used; filled on first use.
    _uses: dict[str, frozenset[DocumentPart]] = field(
        default_factory=dict, repr=False, compare=False
    )

    def parts_between(self, start: int, end: int) -> frozenset[DocumentPart]:
        """Return the parts of the document that ``text[start:end]`` overlaps.

        Without a class declaration, everything before the body may still set the class;
        without a body opener, everything after the class may be preamble or body, and so
        may everything from an opener inside a conditional to the first one outside every
        conditional; after a parsing limit, the parts are unknown.

        Args:
            start: Where the span starts in ``text``.
            end: Where it ends; an empty span stands for the character at ``start``.

        Returns:
            The parts, never empty.
        """
        if self.parse_limit_exceeded:
            return EVERY_PART
        end = max(end, start + 1)
        body = self._body_may_start
        if body is None and self._class_end is None:
            return EVERY_PART
        # The span may lie before the body until an opener that surely runs.
        before_body = self._body_sure_from is None or start < self._body_sure_from
        parts: set[DocumentPart] = set()
        if body is not None and end > body:
            parts.add(DocumentPart.BODY)
        if self._class_end is None:
            # Everything before the body may still set the class or load a package.
            if body is not None and before_body:
                parts |= {DocumentPart.PROLOGUE, DocumentPart.PREAMBLE}
            return frozenset(parts)
        if start < self._class_end:
            parts.add(DocumentPart.PROLOGUE)
        if end > self._class_end and (body is None or before_body):
            parts.add(DocumentPart.PREAMBLE)
        if end > self._class_end and body is None:
            parts.add(DocumentPart.BODY)
        return frozenset(parts)

    def parts_at(self, path: str | None, line: int | None) -> frozenset[DocumentPart]:
        r"""Return the parts of the document where a line of a project file runs.

        A file the expansion did not copy, such as package code or a file that could not
        be read, runs where it is loaded or included. A line of a copied file that no
        segment holds, after its ``\end{document}`` or ``\endinput``, never runs.

        Args:
            path: The project file, or ``None`` when the location is unknown.
            line: The 1-based line, or ``None`` when the whole file is meant.

        Returns:
            The parts, empty for a line that never runs and ``EVERY_PART`` for a location
            that cannot be placed.
        """
        return self._parts_at(path, line, frozenset())

    def _parts_at(
        self, path: str | None, line: int | None, seen: frozenset[str]
    ) -> frozenset[DocumentPart]:
        """Place a line as :meth:`parts_at` does, following the files that load it.

        Args:
            path: The project file, or ``None``.
            line: The 1-based line, or ``None``.
            seen: The files whose loading sites are already being followed.

        Returns:
            The parts, as :meth:`parts_at` returns them.
        """
        if path is None or self.parse_limit_exceeded:
            return EVERY_PART
        segments = [segment for segment in self.segments if segment.path == path]
        if segments and line is not None:
            return self._line_parts(segments, line)
        if path in seen:
            return EVERY_PART
        sites = [
            location
            for location, reference in self.graph.references.items()
            if reference.target == path
        ]
        if not sites:
            return EVERY_PART
        parts: set[DocumentPart] = set()
        for location in sites:
            source = self.graph.sources.get(location.path)
            if source is None:
                return EVERY_PART
            parts |= self._parts_at(location.path, source.line_at(location.start), seen | {path})
        return frozenset(parts)

    def _line_parts(self, segments: list[SourceSegment], line: int) -> frozenset[DocumentPart]:
        """Return the parts that one line overlaps in the segments copied from its file.

        Args:
            segments: The segments copied from the file of the line.
            line: The 1-based line.

        Returns:
            The parts; empty when no segment holds the line.
        """
        parts: set[DocumentPart] = set()
        for segment in segments:
            first = bisect_left(self._newlines, segment.start)
            last = bisect_left(self._newlines, segment.end)
            index = line - segment.line
            if not 0 <= index <= last - first:
                continue
            start = segment.start if index == 0 else self._newlines[first + index - 1] + 1
            end = self._newlines[first + index] + 1 if first + index < last else segment.end
            parts |= self.parts_between(start, end)
        return frozenset(parts)

    def uses_of(self, name: str) -> frozenset[DocumentPart]:
        r"""Return where a command or environment that the project defines may run.

        A use is the command itself, an alias such as ``\let\b\a`` that may run it later,
        ``\@nameuse{a}``, or ``\begin{e}`` for an environment ``begin{e}``. A use inside
        package code runs where that code is loaded.

        Args:
            name: The command without its backslash, or ``begin{name}``.

        Returns:
            The parts of every use, empty when the definition is never used.
        """
        if self.parse_limit_exceeded:
            return EVERY_PART
        if name in self._uses:
            return self._uses[name]
        parts: set[DocumentPart] = set()
        for command in self.commands:
            if _uses(command, name):
                parts |= self.parts_between(command.start, command_end(command))
        copied = {segment.path for segment in self.segments}
        for path in sorted(self.selected - copied):
            if any(_uses(command, name) for command in self.graph.sources[path].commands):
                parts |= self.parts_at(path, None)
        self._uses[name] = frozenset(parts)
        return frozenset(parts)

    def finding_parts(self, finding: Finding) -> frozenset[DocumentPart]:
        """Return where the construct of a source finding may run.

        A finding inside a deferred replacement text (``definition_of``) runs where that
        definition is used; one merged from several occurrences no longer says which
        definition holds each, so it is placed by its file and line.

        Args:
            finding: A finding of the source graph.

        Returns:
            The parts, as :meth:`parts_at` returns them.
        """
        holder = finding.details.get("definition_of")
        if isinstance(holder, str) and "occurrences" not in finding.details:
            return self.uses_of(holder)
        return self.parts_at(finding.path, finding.line)

    def location(self, offset: int) -> Location:
        """Find the original file and line of an offset in the expanded text.

        Returns:
            The location; the main file without a line when nothing was expanded.
        """
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
        """Build report details for an offset: its path and line followed by ``details``."""
        return {**self.location(offset).as_dict(), **details}

    def between(self, start: int, end: int) -> list[Command]:
        """List the commands that start inside ``[start, end)``."""
        return self.commands[
            bisect_left(self._command_starts, start) : bisect_left(self._command_starts, end)
        ]


@dataclass
class _Splice:
    """Accumulates the expanded text while following literal inputs depth first.

    Attributes:
        graph: The literal source graph.
        uncertainty: The gaps noted so far.
        defined: The commands and environments the selected sources define, which may
            drop, repeat or delay an inclusion in their arguments.
        definitions: The merged definition spans of each file read so far that has a
            grouped inclusion.
        chunks: The pieces of the expanded text.
        segments: Where each piece was copied from.
        comment_starts: Offsets in the expanded text of the comments blanked in the copied
            sources.
        size: The length of the expanded text so far.
        events: The commands counted against ``MAX_COMMANDS``.
        stopped: Whether the end of the document was reached.
    """

    graph: SourceGraph
    uncertainty: list[str]
    defined: frozenset[str] = frozenset()
    definitions: dict[str, list[tuple[int, int]]] = field(default_factory=dict)
    chunks: list[str] = field(default_factory=list)
    segments: list[SourceSegment] = field(default_factory=list)
    comment_starts: list[int] = field(default_factory=list)
    size: int = 0
    events: int = 0
    # Set at \end{document}: nothing after it, in any file, is manuscript content.
    stopped: bool = False

    def append(
        self, value: str, path: str, line: int, source_start: int, comments: Sequence[int] = ()
    ) -> None:
        """Add ``value``, copied from ``path`` at ``source_start``; ``comments`` lie within it.

        Args:
            value: The text to add; empty text adds nothing.
            path: The file it was copied from.
            line: The 1-based line of ``path`` that it starts on.
            source_start: Its offset in the masked text of ``path``.
            comments: The offsets of the blanked comments within ``value``.

        Raises:
            ParseLimitExceeded: The expanded text would exceed ``MAX_TOTAL_SOURCE_BYTES``.
        """
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

    def _inclusion_target(self, path: str, command: Command, *, in_place: bool) -> str | None:
        r"""Find the file that an ``\input``-like command splices in.

        An inclusion inside a group is spliced where it stands when it runs there once, as
        in ``\resizebox{…}{!}{\input{x}}``. Groups and brackets after the file argument
        are text that follows the inclusion; an optional argument before it is noted as
        uncertainty.

        Args:
            path: The file that holds the command.
            command: The inclusion command.
            in_place: Whether the command runs where it stands, once
                (:func:`~latexprep.tex_spans.runs_in_place`).

        Returns:
            The file, or ``None`` after noting why there is none.
        """
        index = file_argument_index(command.name)
        if (command.depth and not in_place) or len(command.arguments) <= index:
            self.uncertainty.append(f"{path}: nonliteral or grouped inclusion")
            return None
        argument = command.arguments[index].stripped()
        resolved = self.graph.references.get(ArgumentLocation(path, argument.start, argument.end))
        if resolved is None:
            self.uncertainty.append(f"{path}: unresolved literal inclusion")
            return None
        if any(option.start < argument.start for option in command.options):
            self.uncertainty.append(
                f"{path}: optional syntax precedes the file name of a source inclusion"
            )
        return resolved.target

    def _count_command(self, command: Command) -> None:
        """Count a command against the budget shared by every file being spliced.

        Raises:
            ParseLimitExceeded: More than ``MAX_COMMANDS`` commands were counted.
        """
        self.events += 1
        if self.events > MAX_COMMANDS:
            raise ParseLimitExceeded("command_count", command.start)

    def _runs_in_place(self, path: str, source: ParsedSource, command: Command) -> bool:
        """Return whether an inclusion runs where it stands, once, as ungrouped text does.

        Args:
            path: The file that holds the command.
            source: The parsed source of that file.
            command: The inclusion command.

        Returns:
            ``True`` outside groups, or inside groups that typeset their text in place.
        """
        if not command.depth:
            return True
        if path not in self.definitions:
            self.definitions[path] = definition_spans(source.masked, source.commands)
        return runs_in_place(command, self.definitions[path], self.defined)

    def _terminates_source(
        self,
        path: str,
        source: ParsedSource,
        command: Command,
        conditionals: Sequence[tuple[int, int]],
        *,
        certain: bool,
    ) -> bool:
        r"""Record the scope and certainty of a source or document stop."""
        if not is_source_terminator(command):
            return False
        stops = stops_source(command, conditionals)
        if command.name == "endinput":
            # TeX can keep reading the current physical line from its buffer.
            if not stops or endinput_has_suffix(source.masked, command):
                self.uncertainty.append(f"{path}: unresolved endinput context")
        elif stops and certain:
            self.stopped = True
        else:
            self.uncertainty.append(f"{path}: unresolved end{{document}} context")
        return stops

    def expand(self, path: str, stack: tuple[str, ...], *, certain: bool = True) -> None:
        """Append the masked text of ``path`` up to its end, splicing in each literal input.

        A cycle, excessive depth or a source that was not inspected is noted as
        uncertainty instead.

        Args:
            path: The file to expand.
            stack: The files being expanded around ``path``, outermost first.
            certain: Whether the inclusion runs wherever the current document does.

        Raises:
            ParseLimitExceeded: The expanded text or its commands exceed their bounds.
        """
        cancellation_point()
        if path in stack or len(stack) >= MAX_INPUT_DEPTH:
            self.uncertainty.append("cyclic or excessively deep literal input graph")
            return
        source = self.graph.sources.get(path)
        if source is None:
            self.uncertainty.append(f"{path}: included source was not inspectable")
            return
        cursor = 0
        conditionals = conditional_spans(source.commands)
        newlines = [index for index, char in enumerate(source.masked) if char == "\n"]

        def append_masked(start: int, end: int) -> None:
            """Copy ``source.masked[start:end]``, keeping where its comments were."""
            low = bisect_left(source.comment_starts, start)
            high = bisect_left(source.comment_starts, end)
            comments = [offset - start for offset in source.comment_starts[low:high]]
            line = bisect_right(newlines, start - 1) + 1
            self.append(source.masked[start:end], path, line, start, comments)

        for command in source.commands:
            self._count_command(command)
            if self._terminates_source(path, source, command, conditionals, certain=certain):
                append_masked(cursor, command.start)
                return
            if command.named or command.name not in CONTENT_INPUT_COMMANDS:
                continue
            target = self._inclusion_target(
                path, command, in_place=self._runs_in_place(path, source, command)
            )
            if target is None:
                continue
            append_masked(cursor, command.start)
            self.expand(
                target,
                (*stack, path),
                certain=certain
                and not command.depth
                and not in_conditional(conditionals, command.start),
            )
            cursor = _end_of_inclusion(source, command)
            if self.stopped:
                return
        append_masked(cursor, len(source.masked))


def _end_of_inclusion(source: ParsedSource, command: Command) -> int:
    """Find where the masked text resumes after an input command.

    The shared lexer records subsequent groups as possible arguments. Input has only one:
    later groups and bare-input terminators are preserved.
    """
    cursor = command.arguments[file_argument_index(command.name)].end
    if source.masked[cursor : cursor + 1] == "}":
        cursor += 1
    return cursor


# A file that cannot be resolved only leaves the manuscript undecidable when it could
# supply source text. A missing figure or bibliography cannot hold a title, abstract,
# keywords, heading or declaration, so it never hides what a constraint looks for.
def _graph_gaps(document: ExpandedDocument) -> list[str]:
    r"""List the gaps in the spliced text: every unresolved ``source.*`` finding in scope.

    That is a superset of ``INCOMPLETE_GRAPH_RULES``, which names only the findings that make
    the dependency graph incomplete; other unresolved findings, such as an unsupported
    search-path form, are gaps here as well. A main source outside ``selected`` and subfile
    targets are gaps too; what cannot hide manuscript text (``can_hide_manuscript_text``),
    such as a figure or a bibliography file, is not, and neither is a construct that never
    runs (:meth:`ExpandedDocument.finding_parts`): one after ``\end{document}``, or one
    inside a definition that is never used.

    Package code (``package_code_sources``) is left out: its text is never spliced, so
    what the scan cannot follow inside it says nothing about the manuscript text; it still
    makes the dependency graph incomplete for flat layout and source transformations.
    """
    graph, selected = document.graph, document.selected
    gaps = []
    if graph.main not in selected:
        gaps.append("no inspectable selected main source")
    if graph.subfile_targets:
        gaps.append("subfile body/preamble content interpretation is not supported by this check")
    scope = (selected - package_code_sources(graph, selected)) | {graph.main}
    gaps.extend(
        finding_reason(finding)
        for finding in graph.findings
        if finding.rule.startswith("source.")
        and (finding.path in scope or finding.path is None)
        and finding.status in {"failed", "inconclusive"}
        and can_hide_manuscript_text(finding)
        and document.finding_parts(finding)
    )
    return gaps


def expand_document(
    root: Path, main: str | None, graph: SourceGraph | None = None
) -> ExpandedDocument:
    """Expand the selected literal graph; reuse ``graph`` when the caller already built it.

    Args:
        root: The project directory.
        main: The main file, or ``None``.
        graph: The literal source graph of ``root`` and ``main``, if already built.

    Returns:
        The expanded document. When a parsing limit is exceeded, it has no commands and
        its uncertainty says why.
    """
    graph = graph or SourceGraph.build(root, main)
    selected = reachable_sources(graph)
    defined = defined_names(
        command for name in sorted(selected) for command in graph.sources[name].commands
    )
    splice = _Splice(graph, [], frozenset(defined))
    parse_limit_exceeded = False
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
        parse_limit_exceeded = True
    # Preamble declarations configure the toolchain; they are not manuscript content.
    # An opener inside a conditional, as in \iffalse\begin{document}\fi, may not run: the
    # body starts for sure only at the first opener outside every conditional.
    conditionals = conditional_spans(commands)
    openers = [command for command in commands if command.begins("document") and not command.depth]
    sure = next(
        (command for command in openers if not in_conditional(conditionals, command.start)), None
    )
    opening = sure or (openers[0] if openers else None)
    body_start = command_end(opening) if opening is not None else 0
    # The class is fixed by the last declaration before the body that may run.
    declarations = [
        command
        for command in commands
        if command.name == "documentclass"
        and not command.named
        and (opening is None or command.start < opening.start)
    ]
    document = ExpandedDocument(
        graph=graph,
        selected=selected,
        text=text,
        commands=commands,
        segments=splice.segments,
        graph_uncertainty=[],
        conditionals=conditionals,
        body_start=body_start,
        parse_limit_exceeded=parse_limit_exceeded,
        _segment_starts=[segment.start for segment in splice.segments],
        _command_starts=[command.start for command in commands],
        _newlines=[index for index, char in enumerate(text) if char == "\n"],
        _class_end=command_end(declarations[-1]) if declarations else None,
        _body_may_start=command_end(openers[0]) if openers else None,
        _body_sure_from=sure.start if sure is not None else None,
    )
    return replace(
        document, graph_uncertainty=sorted({*splice.uncertainty, *_graph_gaps(document)})
    )


# The uncertainty of an environment region that runs to the end of the document.
UNCLOSED_ENVIRONMENT = "environment has no closing delimiter"


@dataclass
class EnvironmentRegion:
    """The body of one literal environment, the commands in it, and why it may be unclear.

    Attributes:
        kind: The environment's name.
        command: The command that opens the environment.
        start: Where the body starts in the expanded text.
        end: Where the body ends: at the closing command, or at the end of the text when
            there is none.
        commands: The commands in the body.
        uncertainty: Why the region may be unclear.
    """

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
    r"""Start the region of a ``\begin`` and mark it and the open region as nested.

    Args:
        document: The expanded document.
        command: The ``\begin`` command.
        environment: The environment's name.
        enclosing: The regions still open, innermost last.

    Returns:
        The region, which reaches the end of the text until its end is found.
    """
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
    """Find the environments named in ``kinds``, in source order, and delimiter problems.

    A region's uncertainty covers its own structure (grouping, nesting, a missing
    end) and enclosing conditional control flow, not unknown constructs inside it.

    Args:
        document: The expanded document.
        kinds: The environment names to find.

    Returns:
        The regions in source order, and the delimiter problems.
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
        region.uncertainty.append(UNCLOSED_ENVIRONMENT)
        result.append(region)
    for region in result:
        if in_conditional(document.conditionals, region.command.start):
            region.uncertainty.append("conditional source control flow")
    return sorted(result, key=lambda region: region.command.start), reasons
