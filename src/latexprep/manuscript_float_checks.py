r"""Captions, labels, references and descriptions of figures and tables (``MAN011`` to ``MAN016``).

Six opt-in policies read each ``figure`` and ``table`` environment of the expanded
document: a nonempty caption, a label, a label that follows the caption, an external
reference, first references in source order, and a usable figure description (a
``Description``, or alt text or an artifact mark on every included graphic). Order
means source order only; rendered float placement is not measured.

Each float is judged on its own for each policy. Evidence that satisfies a policy (a
caption with literal text, a literal label, a reference that was read) stands whatever
else the reader could not interpret. A construct the reader did not interpret matters
only for a float whose evidence is missing, or, for the label placement and reference
order, where it could hide a misplaced label or an earlier reference; such a float is
undecided and named with the evidence it lacks. Text that was not read at all (a file
that could not be included, a parsing limit) may hide whole floats, so it leaves every
policy undecided.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import NamedTuple

from .cancellation import cancellation_point
from .expanded_document import (
    EnvironmentRegion,
    ExpandedDocument,
    Location,
    environment_regions,
)
from .finding_text import plural
from .findings import Finding
from .literal_text import (
    LABEL_COMMANDS,
    first_argument_names,
    is_placeholder_text,
    key_value_items,
    plain_argument_text,
    plain_text,
)
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_float_parts import foreign_spans, inside, part_spans, stored_spans, within
from .manuscript_float_references import (
    ReferenceReader,
    References,
    out_of_order,
    unread_text,
)
from .manuscript_policy_kernel import (
    CheckOutcome,
    Policy,
    Reading,
    construct_reasons,
    policy_finding,
)
from .manuscript_vocabulary import (
    FLOAT_ENVIRONMENTS,
)
from .tex_commands import TYPESET_ARGUMENT_COMMANDS
from .tex_lexer import Command
from .tex_spans import (
    command_end,
    defined_names,
    in_conditional,
    in_half_open_span,
    runs_in_place,
)
from .uncertainty_reasons import CONDITIONAL_FLOW, grouped_reason


class _FloatRule(NamedTuple):
    """One float policy: its rule and title, and the template of its failure summary.

    Attributes:
        policy: The rule name, title and weight of the policy.
        failure: The ``str.format`` template of the failure summary.
    """

    policy: Policy
    # {count} is the number of violations; {floats}, {labels} and {figures} are counts
    # with their nouns: the inspected floats, the violating labels and the violating figures.
    failure: str


_CAPTIONS = _FloatRule(
    Policy("manuscript.float_captions", "Checked literal figure/table captions."),
    "{count} of {floats} without a caption",
)
_LABELS = _FloatRule(
    Policy("manuscript.float_labels", "Checked literal figure/table labels."),
    "{count} of {floats} without a label",
)
_LABEL_ORDER = _FloatRule(
    Policy("manuscript.float_label_order", "Checked float label/caption source order."),
    "{labels} placed before the caption",
)
_REFERENCES = _FloatRule(
    Policy("manuscript.float_references", "Checked external literal float references."),
    "{count} of {floats} without an external reference",
)
_REFERENCE_ORDER = _FloatRule(
    Policy("manuscript.float_reference_order", "Checked first-reference source order."),
    "{count} of {floats} first referenced out of source order",
)
_DESCRIPTIONS = _FloatRule(
    Policy("manuscript.figure_descriptions", "Checked literal figure descriptions."),
    "{figures} without a usable Description",
)
# The values of the \includegraphics key artifact that mark a graphic as decorative: the bare
# key and true. LaTeX's keys are case-sensitive, so no other spelling counts.
_ARTIFACT_VALUES = frozenset({None, "true"})
# The unknown-construct label of an explicit counter change (``interpret``).
_COUNTER_CHANGE = "explicit counter modification"
_DUPLICATE_LABEL = "duplicate float labels have ambiguous reference targets"


@dataclass(slots=True)
class _FloatRecord:
    """What the source shows about one figure or table environment.

    Attributes:
        location: Where the environment begins.
        kind: ``figure`` or ``table``, without a star.
        confirmed: ``False`` when the float's own region holds a construct that was not
            interpreted.
        source_offset: The offset of the environment's opening command.
        end: The offset of the environment's end.
        captions: Each caption's location and plain text.
        labels: The float's literal labels, in source order.
        first_reference_offset: The offset of the first external reference read to the
            float, or ``None``.
        first_text_reference_offset: The offset of the first of those references in
            running text, outside every float, or ``None``.
        misplaced_labels: Labels that precede every caption of a confirmed float, as
            reported violations.
        described: Whether a figure has a nonempty, non-placeholder Description; ``None`` for
            tables.
        names: The labels that refer to the float: its own and those of its subfigures.
        region_gaps: Every construct in the float that was not interpreted.
        label_gaps: The constructs in the float that may hold a label: control flow, the
            project's own macros, a file that was not read, a label that was not literal.
        notes: Further gaps per policy, found while reading the captions and labels.
        has_caption: Whether a literal caption runs in place, outside any conditional.
        has_label: Whether a literal label runs in place, outside any conditional.
        duplicated: Whether one of the float's labels occurs more than once.
    """

    location: Location
    kind: str
    confirmed: bool
    source_offset: int
    end: int
    captions: list[dict[str, object]]
    labels: list[str]
    first_reference_offset: int | None
    misplaced_labels: list[dict[str, object]]
    described: bool | None
    names: list[str] = field(default_factory=list)
    region_gaps: list[str] = field(default_factory=list)
    label_gaps: list[str] = field(default_factory=list)
    notes: dict[_FloatRule, list[str]] = field(default_factory=dict)
    has_caption: bool = False
    has_label: bool = False
    duplicated: bool = False
    first_text_reference_offset: int | None = None

    @property
    def cited_offset(self) -> int | None:
        """Where the float is first cited: in running text, else from inside another float."""
        text = self.first_text_reference_offset
        return self.first_reference_offset if text is None else text

    def summary(self) -> dict[str, object]:
        """Render where the float is and what kind it is, as report details."""
        return {
            **self.location.as_dict(),
            "kind": self.kind,
            "confirmed": self.confirmed,
            "source_offset": self.source_offset,
        }

    def details(self) -> dict[str, object]:
        """Render the summary with the captions, labels and first reference offset."""
        return {
            **self.summary(),
            "captions": self.captions,
            "labels": self.labels,
            "first_reference_offset": self.first_reference_offset,
        }

    def owns(self, offset: int) -> bool:
        """Return whether an offset lies inside the float, where a reference is not external."""
        return self.source_offset <= offset < self.end

    def reason(self, missing: str, gaps: Sequence[str]) -> str:
        """Name the float, the evidence it lacks and the gaps that leave it undecided.

        Args:
            missing: What the float lacks, such as ``without a literal caption``.
            gaps: Why that is not confirmed.

        Returns:
            One uncertainty reason.
        """
        where = f"{self.location.path}:{self.location.line}"
        return f"{where}: {self.kind} {missing} ({'; '.join(dict.fromkeys(gaps))})"


class _Verdict(NamedTuple):
    """One policy's result over every float.

    Attributes:
        violations: The confirmed violations.
        reasons: The gaps that leave floats undecided, and those beside the violations.
        undecided: How many floats could not be judged.
    """

    violations: list[dict[str, object]]
    reasons: list[str]
    undecided: int


@dataclass
class _FloatScan:
    """Reads each float region and the references to it.

    Attributes:
        reading: The expanded document and what was not interpreted in it.
        floats: The float regions to read.
        references: The references of the document.
        label_counts: How often each literal label occurs in the whole document.
        labels_seen: The float labels read so far.
        records: One record per float read, in order.
        definitions: The definition spans of the document, inside which nothing runs.
        defined: The commands and environments the document defines.
        parts: The spans of every subfigure, whose captions and labels are the part's,
            not the float's.
        foreign: The spans of equations and lists, whose labels name an equation or an
            item, never the float around them.
        stored: The spans of environments that may not typeset what they hold.
        unread: The gaps of text that was not read inside each float, by the offset of
            the float's opening command.
        description_gaps: The interpretation gaps of the figure descriptions.
    """

    reading: Reading
    floats: list[EnvironmentRegion]
    references: References = field(default_factory=References)
    label_counts: Counter[str] = field(default_factory=Counter)
    labels_seen: set[str] = field(default_factory=set)
    records: list[_FloatRecord] = field(default_factory=list)
    definitions: list[tuple[int, int]] = field(default_factory=list)
    defined: frozenset[str] = frozenset()
    parts: list[tuple[int, int]] = field(default_factory=list)
    foreign: list[tuple[int, int]] = field(default_factory=list)
    stored: list[tuple[int, int]] = field(default_factory=list)
    unread: dict[int, list[str]] = field(default_factory=dict)
    description_gaps: list[str] = field(default_factory=list)
    _region_parts: list[tuple[int, int]] = field(default_factory=list)
    _region_foreign: list[tuple[int, int]] = field(default_factory=list)
    _conditional: bool = False
    _swallowers: dict[int, str] = field(default_factory=dict)

    @property
    def document(self) -> ExpandedDocument:
        """The expanded document."""
        return self.reading.document

    def run(self) -> None:
        """Count the labels, read the references, then read every float into a record."""
        commands = self.document.commands
        self.defined = frozenset(defined_names(commands))
        self.foreign = foreign_spans(self.document)
        self.parts = part_spans(self.document)
        # An environment of the paper that only changes the layout typesets what it holds.
        unknown = self.reading.constructs(0, len(self.document.text), layout_matters=False)
        self.stored = stored_spans(self.document, unknown, self.defined)
        self.label_counts.update(
            command.arguments[0].value.strip()
            for command in commands
            if command.name in LABEL_COMMANDS and command.arguments
        )
        self.references = ReferenceReader(self.reading, self.definitions, self.defined).read()
        self.references.place_floats(self.floats)
        for region in self.floats:
            cancellation_point()
            self.records.append(self._record(region))
        _apply_ranges(self.records, self.references, self.label_counts)

    def _record(self, region: EnvironmentRegion) -> _FloatRecord:
        """Read one float region into a record.

        Args:
            region: The float region, with its own structural gaps.

        Returns:
            What the source shows about the float.
        """
        kind = region.kind.rstrip("*")
        # A subfigure's caption and label are its own; a reference to it refers to the float.
        # Only equations, lists and subfigures inside the float set their labels apart: a
        # float inside a list is still the float.
        self._region_parts = within(self.parts, region)
        self._region_foreign = within(self.foreign, region)
        own = [
            command
            for command in region.commands
            if not inside(self._region_parts, command.start)
            and not inside(self._region_foreign, command.start)
        ]
        constructs = self.reading.constructs(region.start, region.end, layout_matters=False)
        unread = self.unread.get(region.command.start, [])
        record = _FloatRecord(
            location=self.document.location(region.command.start),
            kind=kind,
            confirmed=False,
            source_offset=region.command.start,
            end=region.end,
            captions=[],
            labels=[],
            first_reference_offset=None,
            misplaced_labels=[],
            described=self._described(region) if kind == "figure" else None,
            # A command that typesets its arguments, such as \fbox, makes no caption or
            # label itself; what its arguments hold is read.
            region_gaps=[
                *region.uncertainty,
                *construct_reasons(
                    [item for item in constructs if item[1][1:] not in TYPESET_ARGUMENT_COMMANDS]
                ),
                *unread,
            ],
            notes={rule: [] for rule in (_CAPTIONS, _LABELS, _LABEL_ORDER, _REFERENCES)},
        )
        record.confirmed = not record.region_gaps
        record.label_gaps = [*region.uncertainty, *self._hiding(constructs), *unread]
        if any(label == _COUNTER_CHANGE for _, label in constructs):
            record.notes[_LABEL_ORDER].append(_COUNTER_CHANGE)
        if any(command.name == "subcaption" for command in own):
            # A \subcaption outside a subfigure or minipage has no part to label.
            record.label_gaps.append("subcaption outside a subfigure")
        self._conditional = any(label == CONDITIONAL_FLOW for _, label in constructs)
        self._swallowers = self._ends(constructs)
        captions = [command for command in own if command.name == "caption"]
        self._read_captions(record, captions)
        self._read_labels(record, own, captions)
        record.names = [*record.labels, *self._part_labels(region)]
        external = [
            (label, offset)
            for label, offset in self.references.readable
            if label in record.names and not record.owns(offset)
        ]
        # A reference to a label that occurs twice may name either target.
        record.duplicated = any(self.label_counts[label] > 1 for label, _ in external)
        unique = [offset for label, offset in external if self.label_counts[label] <= 1]
        record.first_reference_offset = min(unique, default=None)
        record.first_text_reference_offset = self.references.first_in_text(unique)
        return record

    def _hiding(self, constructs: list[tuple[int, str]]) -> list[str]:
        """Describe the constructs of a float that may hold a label, as one reason or none.

        A command that the project does not define comes from the class, a package or the
        kernel. It may typeset a caption with its label, as a class's float macros do, but
        in the order the class gives them; it does not place the author's label. What may
        hold a label in any place is control flow and the project's own macros whose
        definitions may typeset one (``Reading.label_makers``).

        Args:
            constructs: The constructs of the float that were not interpreted.

        Returns:
            One reason naming those that may hold a label, or an empty list.
        """
        names = sorted(
            {
                label
                for _, label in constructs
                if label == CONDITIONAL_FLOW or self.reading.may_hold_label(label)
            }
        )
        return ["constructs that may hold a label: " + ", ".join(names)] if names else []

    def _ends(self, constructs: list[tuple[int, str]]) -> dict[int, str]:
        """Map where each project macro of a float that lacks arguments ends to its label.

        A macro reads its undelimited arguments from the tokens after it, so a caption or
        label written right after a use that supplies fewer braced arguments than the
        definition reads may be its argument instead of running.

        Args:
            constructs: The constructs of the float that were not interpreted.

        Returns:
            The offset just after each such command and its groups, with its label.
        """
        ends = {}
        for offset, label in constructs:
            for command in self.document.between(offset, offset + 1):
                if self.reading.arity.get(command.name, 0) > len(command.arguments):
                    ends[command_end(command)] = label
        return ends

    def _doubt(self, command: Command, noun: str) -> str | None:
        """Return why a caption or label of the float being read may not run, if it may not.

        Args:
            command: The caption or label.
            noun: ``caption`` or ``label``, for the reason.

        Returns:
            The reason, or ``None`` when it runs where it stands.
        """
        if not self._in_place(command):
            return grouped_reason(noun)
        if in_half_open_span(self.stored, command.start):
            return f"{noun} inside an environment that may not typeset it"
        if self._conditional and in_conditional(self.document.conditionals, command.start):
            return f"{CONDITIONAL_FLOW} around the {noun}"
        text, offset = self.document.text, command.start
        while offset > 0 and text[offset - 1].isspace():
            offset -= 1
        swallower = self._swallowers.get(offset)
        return f"{swallower} may take the {noun} as its argument" if swallower else None

    def _read_captions(self, record: _FloatRecord, captions: list[Command]) -> None:
        """Read the captions of one float into its record, noting what is unclear about them.

        Args:
            record: The float's record, whose captions, notes and ``has_caption`` are set.
            captions: The float's caption commands.
        """
        for command in captions:
            value, unknown = plain_argument_text(command)
            doubt = self._doubt(command, "caption")
            # A caption with literal text is not empty, whatever its macros expand to.
            if not value:
                record.notes[_CAPTIONS].extend(unknown)
            if doubt:
                record.notes[_CAPTIONS].append(doubt)
            record.has_caption = record.has_caption or (bool(value) and not doubt)
            # A caption whose text is only macros is unknown, not empty.
            located = self.document.located(command.start, value=value)
            if unknown and not value:
                located["unresolved"] = True
            if doubt:
                located["unconfirmed"] = doubt
            record.captions.append(located)

    def _read_labels(
        self,
        record: _FloatRecord,
        own: list[Command],
        captions: list[Command],
    ) -> None:
        """Read the float's literal labels into its record and judge their placement.

        Args:
            record: The float's record, whose labels, notes and placement are set.
            own: The float's commands outside its subfigures, equations and lists.
            captions: The float's caption commands.
        """
        placed: list[Command] = []
        for command in own:
            if command.name not in LABEL_COMMANDS:
                continue
            names = first_argument_names(command)
            if command.arguments and not command.arguments[0].value.strip():
                continue
            # A paper that defines a class's label command itself may drop the label.
            own_macro = command.name != "label" and command.name in self.defined
            if names is None or len(names) != 1 or own_macro or not self._in_place(command):
                record.label_gaps.append("grouped, empty or nonliteral float label")
                continue
            label = names[0]
            record.labels.append(label)
            placed.append(command)
            if label in self.labels_seen:
                self.label_counts[label] = max(self.label_counts[label], 2)
            self.labels_seen.add(label)
            doubt = self._doubt(command, "label")
            if doubt:
                record.notes[_LABELS].append(doubt)
            record.has_label = record.has_label or not doubt
        record.notes[_LABELS].extend(record.label_gaps)
        for command in placed:
            self._place_label(record, command, placed, captions)

    def _place_label(
        self,
        record: _FloatRecord,
        label: Command,
        labels: list[Command],
        captions: list[Command],
    ) -> None:
        r"""Judge whether one literal label follows a caption of its float.

        A label inside the caption's argument, ``\caption{\label{x}A}`` or
        ``\caption{A\label{x}}``, follows it. With several captions, as in two minipages,
        each label follows the caption before it; a second label between two captions may
        belong to either.

        Args:
            record: The float's record.
            label: The label command.
            labels: The float's literal labels.
            captions: The float's caption commands.
        """
        if label.name != "label":
            return  # The class places this label itself, after the caption.
        before = [caption for caption in captions if caption.start < label.start]
        if not before:
            located = self.document.located(label.start, label=record.labels[labels.index(label)])
            if record.confirmed:
                record.misplaced_labels.append({**located, "kind": record.kind})
            else:
                record.notes[_LABEL_ORDER].append("a label precedes every caption")
            return
        if all(self._doubt(caption, "caption") for caption in before):
            record.notes[_LABEL_ORDER].append("a label follows a caption that may not be typeset")
        nearest = max(caption.start for caption in before)
        if any(caption.start > label.start for caption in captions) and any(
            nearest < other.start < label.start for other in labels
        ):
            record.notes[_LABEL_ORDER].append("several labels between two captions")

    def _in_place(self, command: Command) -> bool:
        """Return whether a caption or label runs where it stands."""
        return runs_in_place(command, self.definitions, self.defined)

    def _in_part(self, command: Command) -> bool:
        """Return whether a command lies inside a subfigure of the float being read."""
        return inside(self._region_parts, command.start)

    def _part_labels(self, region: EnvironmentRegion) -> list[str]:
        """Return the literal labels of the float's subfigures, which refer to the float."""
        labels = []
        for command in region.commands:
            if (
                command.name == "label"
                and self._in_part(command)
                and not inside(self._region_foreign, command.start)
            ):
                names = first_argument_names(command)
                if names is not None and len(names) == 1:
                    labels.append(names[0])
        return labels

    def _described(self, region: EnvironmentRegion) -> bool:
        """Return whether a figure has a usable Description, or alt text for all its graphics."""
        descriptions = [command for command in region.commands if command.name == "Description"]
        if len(descriptions) > 1:
            self.description_gaps.append("multiple descriptions within one figure")
        usable = False
        for command in descriptions:
            value, unknown = plain_argument_text(command)
            self.description_gaps.extend(unknown)
            if command.depth:
                self.description_gaps.append(grouped_reason("description"))
            usable = usable or (bool(value) and not is_placeholder_text(value))
        return usable or self._graphics_described(region)

    def _graphics_described(self, region: EnvironmentRegion) -> bool:
        r"""Return whether every ``\includegraphics`` of a figure has alt text or is decorative.

        LaTeX's tagging support reads ``alt=...`` as the alternative text of a graphic and
        ``artifact`` (or ``artifact=true``) as a mark that the graphic is decorative. Its keys
        are case-sensitive and a later setting of a key replaces an earlier one, so only
        these exact spellings count, the last one deciding; ``artifact=false``, ``ALT=`` and
        anything else do not describe a graphic. A figure without ``\includegraphics`` is not
        described this way.

        Args:
            region: The figure region.

        Returns:
            Whether the figure has graphics and each has nonempty, non-placeholder alt text
            or is marked as an artifact.
        """
        graphics = [command for command in region.commands if command.name == "includegraphics"]
        for command in graphics:
            items = [
                item
                for option in command.options
                for item in key_value_items(option.value, fold_keys=False)
            ]
            artifact = [value for key, value in items if key == "artifact"]
            if artifact and artifact[-1] in _ARTIFACT_VALUES:
                continue
            alt = next((value for key, value in reversed(items) if key == "alt"), None)
            if not alt:
                return False
            text, unknown = plain_text(alt)
            self.description_gaps.extend(unknown)
            if not text.strip() or is_placeholder_text(text):
                return False
        return bool(graphics)


def _apply_ranges(
    records: list[_FloatRecord], references: References, counts: Counter[str]
) -> None:
    r"""Give the floats between the two ends of a range the range as a reference.

    ``\crefrange{a}{c}`` and ``\ref{a}--\ref{c}`` refer to every float of the same kind
    from the one labelled ``a`` to the one labelled ``c``, which LaTeX numbers in source
    order. Only unique labels of two floats of one kind, the first before the second, make
    a range.

    Args:
        records: The records of every float, in source order.
        references: The references of the document, with its range references.
        counts: How often each label occurs in the document.
    """
    by_label = {name: record for record in records for name in record.names if counts[name] <= 1}
    for first, last, offset in references.ranges:
        start, end = by_label.get(first), by_label.get(last)
        if start is None or end is None or start.kind != end.kind:
            continue
        in_text = references.first_in_text((offset,)) is not None
        for record in records:
            if (
                record.kind == start.kind
                and start.source_offset < record.source_offset < end.source_offset
                and not record.owns(offset)
            ):
                record.first_reference_offset = _earlier(record.first_reference_offset, offset)
                if in_text:
                    text = record.first_text_reference_offset
                    record.first_text_reference_offset = _earlier(text, offset)


def _earlier(known: int | None, offset: int) -> int:
    """Return the smaller of a known offset, if there is one, and ``offset``."""
    return offset if known is None else min(known, offset)


def _caption_verdict(records: list[_FloatRecord]) -> _Verdict:
    """Judge the required captions of every float."""
    violations, reasons, undecided = [], [], 0
    for record in records:
        if record.has_caption:
            continue
        gaps = [*record.region_gaps, *record.notes[_CAPTIONS]]
        if record.confirmed and not any(
            caption["value"] or caption.get("unresolved") for caption in record.captions
        ):
            violations.append(record.details())
            if gaps:
                reasons.append(record.reason("without a caption", gaps))
            continue
        undecided += 1
        reasons.append(record.reason("without a literal caption", gaps or ["caption not read"]))
    return _Verdict(violations, reasons, undecided)


def _label_verdict(records: list[_FloatRecord]) -> _Verdict:
    """Judge the required labels of every float."""
    violations, reasons, undecided = [], [], 0
    for record in records:
        if record.has_label:
            continue
        gaps = [*record.region_gaps, *record.notes[_LABELS]]
        # A label that is not literal, stands in a macro's argument or may come from a
        # macro of the paper is a label that was not read, not a missing one.
        if not gaps and not record.labels:
            violations.append(record.details())
            continue
        undecided += 1
        reasons.append(record.reason("without a literal label", gaps or ["label not read"]))
    return _Verdict(violations, reasons, undecided)


def _label_order_verdict(records: list[_FloatRecord]) -> _Verdict:
    """Judge whether every float label follows its caption."""
    violations, reasons, undecided = [], [], 0
    for record in records:
        violations.extend(record.misplaced_labels)
        gaps = [*record.label_gaps, *record.notes[_LABEL_ORDER]]
        if "a label precedes every caption" in gaps:
            gaps.extend(record.region_gaps)
        if not gaps:
            continue
        reasons.append(record.reason("label placement not confirmed", gaps))
        undecided += not record.misplaced_labels
    return _Verdict(violations, reasons, undecided)


def _reference_gaps(
    record: _FloatRecord, references: References, before: int | None = None
) -> list[str]:
    """List what may supply the missing external reference of a float.

    Args:
        record: The float's record.
        references: The references of the document, with those that were not read.
        before: Only references that were not read below this offset count, or ``None``
            for all of them.

    Returns:
        The gaps: constructs in the float that may hold a referenced label, references
        that may name one of its labels, and a duplicate label.
    """
    gaps = [
        # Without a label that was read, a label that was not read may be the one referenced.
        *([] if record.labels else record.region_gaps),
        *record.label_gaps,
        *record.notes[_REFERENCES],
        *references.possible_reasons(record.names, record.source_offset, record.end, before),
    ]
    if record.duplicated:
        gaps.append(_DUPLICATE_LABEL)
    return gaps


def _reference_verdict(
    records: list[_FloatRecord], references: References, anywhere: list[str]
) -> _Verdict:
    """Judge whether every float has an external reference.

    Args:
        records: The records of every float.
        references: The references of the document.
        anywhere: The gaps of text that was not read, which may refer to any float.

    Returns:
        The verdict.
    """
    violations, reasons, undecided = [], [], 0
    for record in records:
        if record.first_reference_offset is not None:
            continue
        gaps = _reference_gaps(record, references)
        if not gaps and not anywhere:
            violations.append({**record.summary(), "labels": record.labels})
            continue
        undecided += 1
        reasons.append(
            record.reason(
                "without a readable external reference",
                gaps or ["text that was not read may refer to it"],
            )
        )
    return _Verdict(violations, reasons, undecided)


def _order_gaps(record: _FloatRecord, references: References) -> list[str]:
    """List what may move the first reference of a float earlier, or supply a missing one.

    Args:
        record: The float's record.
        references: The references of the document, with those that were not read.

    Returns:
        The gaps.
    """
    position = record.cited_offset
    if position is None:
        return ["no first reference was read", *_reference_gaps(record, references)]
    gaps = [
        *record.label_gaps,
        *references.possible_reasons(record.names, record.source_offset, record.end, position),
    ]
    if record.duplicated:
        gaps.append(_DUPLICATE_LABEL)
    return gaps


def _reference_order_verdict(
    records: list[_FloatRecord],
    references: References,
    anywhere: list[str],
    unreferenced: list[dict[str, object]],
) -> _Verdict:
    """Judge the order of first references, separately for figures and tables.

    A float that the reference policy confirms as unreferenced has no first reference to
    place; that policy reports it, and this one compares the others. A float whose first
    reference may lie elsewhere is undecided when another float of its kind is compared.

    Args:
        records: The records of every float, in source order.
        references: The references of the document.
        anywhere: The gaps of text that was not read.
        unreferenced: The violations of the reference policy.

    Returns:
        The verdict.
    """
    missing = {item["source_offset"] for item in unreferenced}
    violations, reasons, undecided = [], [], 0
    for kind in ("figure", "table"):
        compared = [
            record
            for record in records
            if record.kind == kind and record.source_offset not in missing
        ]
        if len(compared) < 2:
            continue
        known = [record for record in compared if record.cited_offset is not None]
        late = [known[index] for index in out_of_order([r.cited_offset or 0 for r in known])]
        for record in compared:
            gaps = _order_gaps(record, references)
            if record in late and not gaps and not anywhere:
                violations.append(record.details())
            elif gaps or (record in late and anywhere):
                undecided += 1
                reasons.append(
                    record.reason(
                        "first reference order not confirmed",
                        gaps or ["text that was not read may refer to it earlier"],
                    )
                )
    return _Verdict(violations, reasons, undecided)


def _description_verdict(scan: _FloatScan, reasons: list[str]) -> _Verdict:
    """Judge the figure descriptions; every gap of the document counts, as it always has.

    Args:
        scan: The finished scan.
        reasons: The document-wide gaps of the source graph and the float structure.

    Returns:
        The verdict.
    """
    gaps = [
        *reasons,
        *(gap for record in scan.records for gap in record.region_gaps),
        *scan.description_gaps,
    ]
    violations: list[dict[str, object]] = [
        {**record.summary(), "reason": "missing, empty or placeholder Description"}
        for record in scan.records
        if record.confirmed and record.described is False
    ]
    return _Verdict(violations, gaps, 0)


def _verdicts(
    scan: _FloatScan, anywhere: list[str], structure: list[str]
) -> dict[_FloatRule, _Verdict]:
    """Judge every float policy.

    Args:
        scan: The finished scan.
        anywhere: The gaps of text that was not read.
        structure: The delimiter problems of the float environments.

    Returns:
        The verdict of each policy, its document-wide gaps included.
    """
    records, references = scan.records, scan.references
    shared = [*anywhere, *structure]
    unreferenced = _reference_verdict(records, references, shared)
    verdicts = {
        _CAPTIONS: _caption_verdict(records),
        _LABELS: _label_verdict(records),
        _LABEL_ORDER: _label_order_verdict(records),
        _REFERENCES: unreferenced,
        _REFERENCE_ORDER: _reference_order_verdict(
            records, references, shared, unreferenced.violations
        ),
    }
    result = {
        rule: verdict._replace(reasons=[*shared, *verdict.reasons])
        for rule, verdict in verdicts.items()
    }
    result[_DESCRIPTIONS] = _description_verdict(
        scan, [*scan.document.graph_uncertainty, *structure]
    )
    return result


def _enabled_rules(options: ManuscriptCheckOptions) -> list[_FloatRule]:
    """List the float rules that the options switch on, in reporting order."""
    return [
        rule
        for rule, selected in (
            (_CAPTIONS, options.require_float_captions),
            (_LABELS, options.require_float_labels),
            (_LABEL_ORDER, options.check_float_label_order),
            (_REFERENCES, options.require_float_references),
            (_REFERENCE_ORDER, options.check_float_reference_order),
            (_DESCRIPTIONS, options.require_figure_descriptions),
        )
        if selected
    ]


def _failure(rule: _FloatRule, verdict: _Verdict, floats: int) -> str:
    """Summarize a policy's violations, with the floats that could not be judged.

    Args:
        rule: The policy.
        verdict: Its verdict.
        floats: How many floats were inspected.

    Returns:
        The failure summary.
    """
    count = len(verdict.violations)
    summary = rule.failure.format(
        count=count,
        floats=plural(floats, "float"),
        labels=plural(count, "float label"),
        figures=plural(count, "figure"),
    )
    if verdict.undecided:
        summary += f" ({plural(verdict.undecided, 'float')} could not be judged)"
    return summary


def float_checks(reading: Reading, options: ManuscriptCheckOptions) -> list[Finding]:
    """Run the float policies that are switched on.

    Args:
        reading: The expanded document.
        options: The ``require_float_*``, ``check_float_*`` and
            ``require_figure_descriptions`` switches.

    Returns:
        One finding per float policy that is switched on.
    """
    enabled = _enabled_rules(options)
    if not enabled:
        return []
    floats, structure = environment_regions(reading.document, FLOAT_ENVIRONMENTS)
    anywhere, inside = unread_text(reading.document, floats)
    scan = _FloatScan(reading, floats, definitions=list(reading.definitions), unread=inside)
    scan.run()
    verdicts = _verdicts(scan, anywhere, structure)
    details = {
        "floats": [record.details() for record in scan.records],
        "order": "Source appearance and source first reference only; "
        "rendered float order is not measured.",
        "description_quality": "Presence and obvious placeholders only; "
        "human review is needed for adequacy.",
    }
    return [
        policy_finding(
            rule.policy,
            CheckOutcome(verdicts[rule].violations, verdicts[rule].reasons),
            failure=_failure(rule, verdicts[rule], len(floats)),
            details=details,
        )
        for rule in enabled
    ]
