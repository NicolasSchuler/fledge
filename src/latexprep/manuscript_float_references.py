r"""The references to figures and tables, and the text a float check could not read.

The float policies (``manuscript_float_checks``) need to know where each float is first
referenced and what may hide a reference or a float. This module reads every reference
command and every use of a project macro that makes one, sorts them into references
read where they stand and possible ones, finds the ranges among them, finds the text
that was not read, and places floats in reference order.
"""

from __future__ import annotations

import re
from bisect import bisect_left, bisect_right
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field

from .expanded_document import (
    CONTENT_INPUT_COMMANDS,
    EnvironmentRegion,
    ExpandedDocument,
    package_code_sources,
)
from .literal_text import (
    RANGE_REFERENCE_COMMANDS,
    REFERENCE_COMMANDS,
    literal_list,
    reference_labels,
)
from .manuscript_policy_kernel import PARAMETER, Reading, ReferenceMacro
from .source_graph import UNPARSED_SOURCE_RULES
from .tex_commands import TYPESET_ARGUMENT_COMMANDS
from .tex_lexer import Command
from .tex_spans import (
    MAX_PLACED_DEPTH,
    command_end,
    in_conditional,
    in_half_open_span,
    merge_spans,
)
from .uncertainty_reasons import CONDITIONAL_FLOW, grouped_reason

# What stands between two references of a typed range, \ref{a}--\ref{c}, once spaces and
# ties are stripped: a hyphen, an en dash written as two hyphens, or the character itself.
_RANGE_DASHES = frozenset({"-", "--", "\N{EN DASH}"})
# Findings that the traversal stopped or skipped text: what follows is not read at all.
_TRAVERSAL_GAP_RULES = UNPARSED_SOURCE_RULES | {
    "source.input_cycle",
    "source.input_depth",
    "source.expansion_limit",
    "source.literal_unclosed",
}
_ANY_LABEL = re.compile(r".*", re.DOTALL)


@dataclass(frozen=True, slots=True)
class PossibleReference:
    """A reference that may name a label but was not read as a reference to it.

    Attributes:
        offset: Where it stands in the expanded text.
        pattern: The labels it may name.
        reason: Why it was not read.
        labels: The labels it names when each is written out, else ``None``.
    """

    offset: int
    pattern: re.Pattern[str]
    reason: str
    labels: tuple[str, ...] | None = None

    def may_name(self, labels: Iterable[str]) -> bool:
        """Return whether the reference may name one of ``labels``."""
        return any(self.pattern.fullmatch(label) for label in labels)


# Where the possible references with one reason stand, by reason, each list sorted.
_Occurrences = dict[str, list[int]]
# How many distinct label patterns, and how many reasons per pattern, the index keeps
# apart; beyond that, a reference may name any label, for a reason that names no command.
_MAX_INDEXED = 64
_FURTHER_REFERENCES = "further references that were not read"


def _add(occurrences: _Occurrences, reference: PossibleReference) -> None:
    """Note where ``reference`` stands under its reason, or under a shared one beyond the bound."""
    reason = reference.reason
    if reason not in occurrences and len(occurrences) >= _MAX_INDEXED:
        reason = _FURTHER_REFERENCES
    occurrences.setdefault(reason, []).append(reference.offset)


def _first_outside(offsets: list[int], start: int, end: int, before: int | None) -> int | None:
    """Return the first of the sorted ``offsets`` outside ``[start, end)``, below ``before``.

    Args:
        offsets: The sorted offsets.
        start: The first offset of the span to leave out.
        end: The offset just past that span.
        before: The offset the result must lie below, or ``None`` for no such bound.

    Returns:
        The offset, or ``None`` when every offset lies in the span or at or after ``before``.
    """
    limit = len(offsets) if before is None else bisect_left(offsets, before)
    if limit and offsets[0] < start:
        return offsets[0]
    after = bisect_left(offsets, end)
    return offsets[after] if after < limit else None


@dataclass
class _PossibleIndex:
    """The possible references by the labels they may name.

    A float asks which references outside it may name one of its labels. Matching every
    float against every reference would take quadratic time in a document with many of
    both, so the references are grouped once: those that name written-out labels by
    label, the others by pattern.

    Attributes:
        named: The occurrences of the references that name each written-out label.
        patterns: The other references, as each distinct pattern with its occurrences.
    """

    named: dict[str, _Occurrences] = field(default_factory=dict)
    patterns: dict[str, tuple[re.Pattern[str], _Occurrences]] = field(default_factory=dict)

    @classmethod
    def of(cls, possible: Iterable[PossibleReference]) -> _PossibleIndex:
        """Group ``possible``, which is in the order of the text."""
        index = cls()
        for reference in possible:
            if reference.labels is not None:
                for label in reference.labels:
                    _add(index.named.setdefault(label, {}), reference)
                continue
            pattern = reference.pattern
            if pattern.pattern not in index.patterns and len(index.patterns) >= _MAX_INDEXED:
                pattern = _ANY_LABEL
            _add(index.patterns.setdefault(pattern.pattern, (pattern, {}))[1], reference)
        return index

    def reasons(self, names: Sequence[str], start: int, end: int, before: int | None) -> list[str]:
        """Return why references outside ``[start, end)`` may name one of ``names``.

        Args:
            names: The labels of one float.
            start: Where the float starts; a reference inside it is not external.
            end: The offset just past the float.
            before: Only references below this offset count, or ``None`` for all.

        Returns:
            The reasons, in the order of the first reference that has each.
        """
        groups = [self.named[name] for name in dict.fromkeys(names) if name in self.named]
        groups.extend(
            occurrences
            for pattern, occurrences in self.patterns.values()
            if any(pattern.fullmatch(name) for name in names)
        )
        first: dict[str, int] = {}
        for occurrences in groups:
            for reason, offsets in occurrences.items():
                offset = _first_outside(offsets, start, end, before)
                if offset is not None:
                    first[reason] = min(offset, first.get(reason, offset))
        return sorted(first, key=first.__getitem__)


@dataclass
class References:
    r"""The references of the document, read or possible.

    Attributes:
        readable: Each label that a reference read where it stands names, with its offset.
        possible: The references that may name labels but were not read.
        ranges: The two end labels of each range reference and the offset of its second
            label, which the floats between the ends share; typed as
            ``\ref{a}--\ref{c}`` or written as ``\crefrange{a}{c}``.
        float_spans: The merged spans of every float, set by :meth:`place_floats`.
    """

    readable: list[tuple[str, int]] = field(default_factory=list)
    possible: list[PossibleReference] = field(default_factory=list)
    ranges: list[tuple[str, str, int]] = field(default_factory=list)
    float_spans: list[tuple[int, int]] = field(default_factory=list)
    _index: _PossibleIndex | None = field(default=None, repr=False)

    def possible_reasons(
        self, names: Sequence[str], start: int, end: int, before: int | None = None
    ) -> list[str]:
        """Return why references that were not read may name a float's labels from outside it.

        Args:
            names: The labels of the float.
            start: Where the float starts.
            end: The offset just past the float.
            before: Only references below this offset count, or ``None`` for all.

        Returns:
            The reasons of those references, each once.
        """
        if self._index is None:
            self._index = _PossibleIndex.of(self.possible)
        return self._index.reasons(names, start, end, before)

    def place_floats(self, floats: Iterable[EnvironmentRegion]) -> None:
        """Record where the floats are, so that references in running text can be told."""
        self.float_spans = merge_spans([(region.command.start, region.end) for region in floats])

    def first_in_text(self, offsets: Iterable[int]) -> int | None:
        """Return the first of ``offsets`` that lies in running text, outside every float.

        A reference in a float's caption or body is printed where LaTeX places that float,
        so it says nothing about the order in which the text cites the floats.
        """
        return min(
            (offset for offset in offsets if not in_half_open_span(self.float_spans, offset)),
            default=None,
        )


@dataclass
class ReferenceReader:
    r"""Reads every reference of the document, and the references the project's macros make.

    A reference counts where it runs in place: outside definitions and conditionals, in
    a bare group or an argument that is typeset, including the argument of a project macro
    that typesets it (``\rev{see \ref{x}}``). A project macro whose replacement holds a
    reference (``\newcommand\Fig[1]{Figure~\ref{fig:#1}}``) is read at each use with its
    arguments in place of the parameters. Any other reference, and any use of such a macro
    that cannot be read, is possible: it may name the labels its text allows. So is a use
    of a reference command that the document defines anew without a reference in its
    replacement text, as ``\renewcommand\ref[1]{}`` does: it may print nothing.

    Attributes:
        reading: The expanded document and the project's macros.
        definitions: The definition spans of the document.
        defined: The commands and environments the document defines.
        found: The references read so far.
    """

    reading: Reading
    definitions: list[tuple[int, int]]
    defined: frozenset[str]
    found: References = field(default_factory=References)
    _last: tuple[Command, str] | None = None

    def read(self) -> References:
        """Read every reference command and every use of a reference macro."""
        macros = self.reading.reference_macros
        replacements = merge_spans(span for macro in macros.values() for span in macro.spans)
        for command in self.reading.document.commands:
            if command.named:
                continue  # named by a definition or compared by a test, not used
            if command.name in macros:
                self._macro_use(command, macros[command.name])
            elif command.name in REFERENCE_COMMANDS:
                if in_half_open_span(replacements, command.start):
                    continue  # read at each use of its macro
                if command.name in self.defined:
                    values = [item.value for item in reference_labels(command)]
                    self._possible(command.start, values, f"the document defines \\{command.name}")
                else:
                    self._reference(command)
        return self.found

    def _possible(self, offset: int, values: Sequence[str], reason: str) -> None:
        """Record a reference that may name the labels its written ``values`` allow."""
        self.found.possible.append(
            PossibleReference(offset, _label_pattern(values), reason, _written_labels(values))
        )

    def _reference(self, command: Command) -> None:
        """Read one reference command, or record it as possible."""
        labels = reference_labels(command)
        values = [item.value for item in labels]
        if in_half_open_span(self.definitions, command.start):
            self._possible(command.start, values, "reference inside a macro definition")
        elif in_conditional(self.reading.document.conditionals, command.start):
            self._possible(command.start, values, CONDITIONAL_FLOW + " around a reference")
        elif not labels or not self._in_place(command):
            self._possible(command.start, values, grouped_reason("reference"))
        elif any(literal_list(value) is None for value in values):
            self._possible(command.start, values, "nonliteral reference")
        else:
            self.found.readable.extend((item.value, item.start) for item in labels)
            if command.name in RANGE_REFERENCE_COMMANDS and len(labels) == 2:
                self.found.ranges.append((labels[0].value, labels[1].value, labels[1].start))
            self._typed_range(command, values, labels[0].start)

    def _typed_range(self, command: Command, values: list[str], offset: int) -> None:
        r"""Record ``\ref{a}--\ref{c}``, two single references joined by a dash, as a range.

        Args:
            command: The reference just read.
            values: Its labels.
            offset: Where its first label stands, the range's offset when it ends one.
        """
        if len(values) != 1 or command.name in RANGE_REFERENCE_COMMANDS:
            self._last = None
            return
        if self._last is not None:
            previous, label = self._last
            between = self.reading.document.text[command_end(previous) : command.start]
            if between.strip(" ~\t\n") in _RANGE_DASHES:
                self.found.ranges.append((label, values[0], offset))
        self._last = (command, values[0])

    def _macro_use(self, command: Command, macro: ReferenceMacro) -> None:
        """Read the references of one use of a reference macro, or record them as possible."""
        arguments = [argument.value.strip() for argument in command.arguments[: macro.parameters]]
        labels = [_substitute(template, arguments) for template in macro.templates]
        if (
            macro.readable
            and len(arguments) == macro.parameters
            and not in_half_open_span(self.definitions, command.start)
            and not in_conditional(self.reading.document.conditionals, command.start)
            and self._in_place(command)
            and all(literal_list(label) == [label] for label in labels)
        ):
            self.found.readable.extend((label, command.start) for label in labels)
            return
        self._possible(command.start, labels, f"reference macro \\{command.name} was not read")

    def _in_place(self, command: Command) -> bool:
        """Return whether a reference, or a use of a reference macro, runs where it stands.

        As :func:`runs_in_place`, and an argument of a macro that typesets its arguments
        counts as typeset.
        """
        if command.depth > MAX_PLACED_DEPTH or in_half_open_span(self.definitions, command.start):
            return False
        group = command.group
        while group is not None:
            owner = group.owner
            if (
                owner is not None
                and owner not in self.reading.transparent
                and (
                    owner in self.defined
                    or not (owner in TYPESET_ARGUMENT_COMMANDS or owner.startswith("begin{"))
                )
            ):
                return False
            group = group.parent
        return True


def _substitute(template: str, arguments: Sequence[str]) -> str:
    """Put the arguments of a macro use in place of the parameters a template names."""

    def argument(match: re.Match[str]) -> str:
        index = int(match.group(1)) - 1
        return arguments[index] if index < len(arguments) else match.group(0)

    return PARAMETER.sub(argument, template)


def _label_pattern(values: Iterable[str]) -> re.Pattern[str]:
    """Return a pattern of the labels a reference written as ``values`` may name.

    A literal label names itself, and a parameter of a macro (``fig:#1``) stands for any
    text; anything else may name any label.

    Args:
        values: The label arguments as written.

    Returns:
        The pattern, matched against whole labels.
    """
    alternatives = []
    for value in values:
        names = literal_list(PARAMETER.sub("x", value))
        if names is None:
            return _ANY_LABEL
        parts = [re.escape(part) for part in PARAMETER.split(value)[::2]]
        alternatives.extend(
            ".+".join(parts).split(re.escape(",")) if "," in value else [".+".join(parts)]
        )
    if not alternatives:
        return _ANY_LABEL
    return re.compile("|".join(f"(?:{alternative.strip()})" for alternative in alternatives))


def _written_labels(values: Sequence[str]) -> tuple[str, ...] | None:
    """Return the labels that ``values`` name when each is written out, as the pattern reads them.

    Args:
        values: The label arguments as written.

    Returns:
        The labels, or ``None`` when a value holds a parameter or is not literal, or when
        there is none.
    """
    if not values or any(
        PARAMETER.search(value) or literal_list(value) is None for value in values
    ):
        return None
    return tuple(part.strip() for value in values for part in value.split(","))


def unread_text(
    document: ExpandedDocument, floats: list[EnvironmentRegion]
) -> tuple[list[str], dict[int, list[str]]]:
    r"""Find the text that was not read: what it may hide decides every float policy.

    An inclusion that could not be followed stays in the expanded text where it stands;
    one of a file that the TeX installation supplies is no manuscript text and is skipped.
    Inside a float it may hold that float's caption, label or content, never another
    float; anywhere else it may hold whole floats. A parsing limit, a missing main file,
    subfiles and a source that could not be read or whose reading stopped may hide floats
    anywhere. A construct that the source checks only suspect of reading a file, such as
    ``\csname`` in a definition, is reported by those checks and is not repeated here.

    Args:
        document: The expanded document.
        floats: The float regions.

    Returns:
        The gaps that may hide whole floats, and the gaps inside each float by the offset
        of its opening command.
    """
    graph = document.graph
    anywhere: list[str] = []
    if document.parse_limit_exceeded:
        anywhere.append("expanded manuscript exceeded a bounded parsing limit")
    if graph.main not in document.selected:
        anywhere.append("no inspectable selected main source")
    if graph.subfile_targets:
        anywhere.append("subfile content is not read by this check")
    scope = (document.selected - package_code_sources(graph, document.selected)) | {graph.main}
    anywhere.extend(
        f"{finding.path}: {finding.message}"
        for finding in graph.findings
        if finding.rule in _TRAVERSAL_GAP_RULES
        and finding.path in scope
        and finding.status in {"failed", "inconclusive"}
    )
    # A file the TeX installation supplies, such as glyphtounicode.tex, is no manuscript text.
    installed = {
        (reference.path, reference.command.line, reference.command.name)
        for reference in graph.system_references
        if reference.installed
    }
    inside: dict[int, list[str]] = {}
    for command in document.commands:
        if command.name not in CONTENT_INPUT_COMMANDS or command.named:
            continue
        place = document.location(command.start)
        if (place.path, place.line, command.name) in installed:
            continue
        region = next((item for item in floats if item.start <= command.start < item.end), None)
        if region is not None:
            inside.setdefault(region.command.start, []).append(
                f"\\{command.name} of a file that was not read"
            )
        else:
            anywhere.append(
                f"{place.path}:{place.line}: \\{command.name} of a file that was not read"
            )
    return anywhere, inside


def out_of_order(positions: Sequence[int]) -> set[int]:
    """Return which floats are first referenced out of order: those outside the longest ordered run.

    The floats are in source order. The longest run whose first references do not
    decrease is in order; among runs of that length the one with the latest floats is
    kept, so that a float cited only after the floats that follow it is the one named,
    not every float that follows it.

    Args:
        positions: The offset of each float's first reference, in source order.

    Returns:
        The indices of the floats outside the run.
    """
    tails: list[int] = []
    lengths = []
    for position in positions:
        length = bisect_right(tails, position)
        if length == len(tails):
            tails.append(position)
        else:
            tails[length] = position
        lengths.append(length + 1)
    need, bound, kept = len(tails), float("inf"), set()
    for index in reversed(range(len(positions))):
        if need and lengths[index] == need and positions[index] <= bound:
            kept.add(index)
            bound = positions[index]
            need -= 1
    return set(range(len(positions))) - kept
