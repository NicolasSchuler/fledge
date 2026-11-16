"""Captions, labels, references and descriptions of figures and tables (``MAN011`` to ``MAN016``).

Six opt-in policies read each ``figure`` and ``table`` environment of the expanded
document: a nonempty caption, a label, a label that follows the caption, an external
reference, first references in source order, and a usable figure description (a
``Description``, or alt text or an artifact mark on every included graphic). Order
means source order only; rendered float placement is not measured. A float whose own
region cannot be interpreted yields reasons instead of violations, because an unexpanded
construct inside it may be the caption, label or description that appears to be missing.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
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
    REFERENCE_COMMANDS,
    first_argument_names,
    is_placeholder_text,
    key_value_items,
    literal_list,
    plain_argument_text,
    plain_text,
    reference_labels,
)
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_policy_kernel import CheckOutcome, Policy, Reading, policy_finding
from .manuscript_vocabulary import (
    FLOAT_ENVIRONMENTS,
    LIST_ENVIRONMENTS,
    SUBFIGURE_COMMANDS,
    SUBFIGURE_ENVIRONMENTS,
)
from .tex_commands import ALL_MATH_ENVIRONMENTS
from .tex_lexer import Command
from .tex_spans import (
    command_end,
    defined_names,
    in_closed_span,
    in_conditional,
    merge_spans,
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
_FLOAT_RULES = (_CAPTIONS, _LABELS, _LABEL_ORDER, _REFERENCES, _REFERENCE_ORDER, _DESCRIPTIONS)
# The values of the \includegraphics key artifact that mark a graphic as decorative: the bare
# key and true. LaTeX's keys are case-sensitive, so no other spelling counts.
_ARTIFACT_VALUES = frozenset({None, "true"})


@dataclass(slots=True)
class _FloatRecord:
    """What the source shows about one figure or table environment.

    Attributes:
        location: Where the environment begins.
        kind: ``figure`` or ``table``, without a star.
        confirmed: ``False`` when the float's own region could not be interpreted.
        source_offset: The offset of the environment's opening command.
        captions: Each caption's location and plain text.
        labels: The float's literal labels, in source order.
        first_reference_offset: The offset of the first external reference to one of the
            labels, or ``None``.
        misplaced_labels: Labels that precede the float's first caption, as reported
            violations.
        described: Whether a figure has a nonempty, non-placeholder Description; ``None`` for
            tables.
        conditionally_referenced: Whether a reference inside a conditional names one of
            its labels, so that a missing external reference is not confirmed.
    """

    location: Location
    kind: str
    confirmed: bool
    source_offset: int
    captions: list[dict[str, object]]
    labels: list[str]
    first_reference_offset: int | None
    misplaced_labels: list[dict[str, object]]
    described: bool | None
    conditionally_referenced: bool = False

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


@dataclass
class _FloatScan:
    """Reads each float region while collecting per-rule interpretation gaps.

    Attributes:
        document: The expanded document.
        floats: The float regions to read.
        uncertainty: The interpretation gaps collected for each float rule.
        first_references: The offset of the first external reference to each label.
        conditional_references: The labels named by references inside a conditional.
        label_counts: How often each literal label occurs in the whole document.
        labels_seen: The float labels read so far.
        records: One record per float read, in order.
        definitions: The definition spans of the document, inside which nothing runs.
        defined: The commands and environments the document defines.
        parts: The spans of every subfigure, whose captions and labels are the part's,
            not the float's.
        foreign: The spans of equations and lists, whose labels name an equation or an
            item, never the float around them.
    """

    document: ExpandedDocument
    floats: list[EnvironmentRegion]
    uncertainty: dict[_FloatRule, list[str]] = field(
        default_factory=lambda: {rule: [] for rule in _FLOAT_RULES}
    )
    first_references: dict[str, int] = field(default_factory=dict)
    conditional_references: set[str] = field(default_factory=set)
    label_counts: Counter[str] = field(default_factory=Counter)
    labels_seen: set[str] = field(default_factory=set)
    records: list[_FloatRecord] = field(default_factory=list)
    definitions: list[tuple[int, int]] = field(default_factory=list)
    defined: frozenset[str] = frozenset()
    parts: list[tuple[int, int]] = field(default_factory=list)
    foreign: list[tuple[int, int]] = field(default_factory=list)
    _region_parts: list[tuple[int, int]] = field(default_factory=list)
    _region_foreign: list[tuple[int, int]] = field(default_factory=list)

    def note(self, reasons: Iterable[str], *rules: _FloatRule) -> None:
        """Record interpretation gaps against each of the given rules.

        Args:
            reasons: The gaps to record.
            *rules: The rules whose results the gaps affect.
        """
        for rule in rules:
            self.uncertainty[rule].extend(reasons)

    def run(self) -> None:
        """Count the labels and first references, then read every float into a record."""
        commands = self.document.commands
        self.defined = frozenset(defined_names(commands))
        subfigures, _ = environment_regions(self.document, SUBFIGURE_ENVIRONMENTS)
        minipages, _ = environment_regions(self.document, {"minipage"})
        equations, _ = environment_regions(self.document, ALL_MATH_ENVIRONMENTS | LIST_ENVIRONMENTS)
        self.foreign = [(region.command.start, region.end) for region in equations]
        self.parts = [
            *((region.command.start, region.end) for region in subfigures),
            *(
                _subfigure_span(command)
                for command in commands
                if command.name in SUBFIGURE_COMMANDS
            ),
            *_subcaption_minipages(commands, minipages),
        ]
        self.label_counts.update(
            command.arguments[0].value.strip()
            for command in self.document.commands
            if command.name == "label" and command.arguments
        )
        self._collect_first_references()
        for region in self.floats:
            cancellation_point()
            self.records.append(self._record(region))

    def _collect_first_references(self) -> None:
        r"""Record the first offset of each label referenced outside every float.

        A grouped or nonliteral reference is a gap for both reference rules, and so is a
        reference inside a conditional, which may never be typeset (``\iffalse`` is the
        usual way to comment text out).
        """
        float_spans = merge_spans((region.command.start, region.end) for region in self.floats)
        for command in self.document.commands:
            if command.name not in REFERENCE_COMMANDS:
                continue
            if in_closed_span(float_spans, command.start):
                continue
            if in_conditional(self.document.conditionals, command.start):
                self.conditional_references.update(
                    name.strip()
                    for argument in reference_labels(command)
                    for name in argument.value.split(",")
                )
                self.note([CONDITIONAL_FLOW + " around a reference"], _REFERENCES, _REFERENCE_ORDER)
                continue
            labels = reference_labels(command)
            if (
                not labels
                or not self._in_place(command)
                or any(literal_list(item.value) is None for item in labels)
            ):
                self.note(["grouped or nonliteral reference"], _REFERENCES, _REFERENCE_ORDER)
                continue
            for argument in labels:
                self.first_references.setdefault(argument.value, argument.start)

    def _record(self, region: EnvironmentRegion) -> _FloatRecord:
        """Read one float region into a record.

        Args:
            region: The float region, with its own interpretation gaps.

        Returns:
            What the source shows about the float.
        """
        # A float whose own region could not be interpreted contributes reasons,
        # never violations: an unexpanded construct inside it may be the caption,
        # label or description that appears to be missing.
        confirmed = not region.uncertainty
        kind = region.kind.rstrip("*")
        # A subfigure's caption and label are its own; a reference to it refers to the float.
        # Only equations, lists and subfigures inside the float set their labels apart: a
        # float inside a list is still the float.
        self._region_parts = _within(self.parts, region)
        self._region_foreign = _within(self.foreign, region)
        own = [
            command
            for command in region.commands
            if not _inside(self._region_parts, command.start)
            and not _inside(self._region_foreign, command.start)
        ]
        if any(command.name == "subcaption" for command in own):
            # A \subcaption outside a subfigure or minipage has no part to label.
            self.note(["subcaption outside a subfigure"], _LABELS, _LABEL_ORDER, _REFERENCES)
        captions = [command for command in own if command.name == "caption"]
        caption_data = self._read_captions(captions)
        first_caption_end = min((command_end(command) for command in captions), default=None)
        labels, misplaced = self._read_labels(region, own, first_caption_end, confirmed=confirmed)
        referenced = [*labels, *self._part_labels(region)]
        first = min(
            (
                self.first_references[label]
                for label in referenced
                if label in self.first_references
            ),
            default=None,
        )
        if first is None:
            self.note(["a float lacks a label or external first reference"], _REFERENCE_ORDER)
        return _FloatRecord(
            location=self.document.location(region.command.start),
            kind=kind,
            confirmed=confirmed,
            source_offset=region.command.start,
            captions=caption_data,
            labels=labels,
            first_reference_offset=first,
            misplaced_labels=misplaced,
            described=self._described(region) if kind == "figure" else None,
            conditionally_referenced=bool(set(referenced) & self.conditional_references),
        )

    def _read_captions(self, captions: list[Command]) -> list[dict[str, object]]:
        """Read the captions of one float, noting what is unclear about them.

        Args:
            captions: The float's caption commands.

        Returns:
            Each caption's location and plain text.
        """
        data = []
        for command in captions:
            value, unknown = plain_argument_text(command)
            # A caption with literal text is not empty, whatever its macros expand to.
            if not value:
                self.note(unknown, _CAPTIONS)
            if not self._in_place(command):
                self.note([grouped_reason("caption")], _CAPTIONS)
            # A caption whose text is only macros is unknown, not empty.
            located = self.document.located(command.start, value=value)
            data.append({**located, "unresolved": True} if unknown and not value else located)
        if len(captions) > 1:
            self.note(["multiple captions within one float"], _LABEL_ORDER)
        return data

    def _read_labels(
        self,
        region: EnvironmentRegion,
        own: list[Command],
        first_caption_end: int | None,
        *,
        confirmed: bool,
    ) -> tuple[list[str], list[dict[str, object]]]:
        """Read the float's literal labels, and those placed before its first caption.

        Args:
            region: The float region.
            own: The float's commands outside its subfigures.
            first_caption_end: The offset just past the first caption, or ``None`` when
                the float has none.
            confirmed: Whether the region could be interpreted; only then are misplaced
                labels reported.

        Returns:
            The literal labels in source order, and the located labels that precede the
            first caption.
        """
        kind = region.kind.rstrip("*")
        labels: list[str] = []
        misplaced: list[dict[str, object]] = []
        for command in own:
            if command.name != "label":
                continue
            names = first_argument_names(command)
            if command.arguments and not command.arguments[0].value.strip():
                continue
            if names is None or len(names) != 1 or not self._in_place(command):
                self.note(
                    ["grouped, empty or nonliteral float label"],
                    _LABELS,
                    _LABEL_ORDER,
                    _REFERENCES,
                    _REFERENCE_ORDER,
                )
                continue
            label = names[0]
            labels.append(label)
            if label in self.labels_seen or self.label_counts[label] > 1:
                self.note(
                    ["duplicate float labels have ambiguous reference targets"],
                    _REFERENCES,
                    _REFERENCE_ORDER,
                )
            self.labels_seen.add(label)
            if confirmed and (first_caption_end is None or first_caption_end > command.start):
                misplaced.append(self.document.located(command.start, label=label, kind=kind))
        return labels, misplaced

    def _in_place(self, command: Command) -> bool:
        """Return whether a caption, label or reference runs where it stands."""
        return runs_in_place(command, self.definitions, self.defined)

    def _in_part(self, command: Command) -> bool:
        """Return whether a command lies inside a subfigure of the float being read."""
        return _inside(self._region_parts, command.start)

    def _part_labels(self, region: EnvironmentRegion) -> list[str]:
        """Return the literal labels of the float's subfigures, which refer to the float."""
        labels = []
        for command in region.commands:
            if (
                command.name == "label"
                and self._in_part(command)
                and not _inside(self._region_foreign, command.start)
            ):
                names = first_argument_names(command)
                if names is not None and len(names) == 1:
                    labels.append(names[0])
        return labels

    def _described(self, region: EnvironmentRegion) -> bool:
        """Return whether a figure has a usable Description, or alt text for all its graphics."""
        descriptions = [command for command in region.commands if command.name == "Description"]
        if len(descriptions) > 1:
            self.note(["multiple descriptions within one figure"], _DESCRIPTIONS)
        usable = False
        for command in descriptions:
            value, unknown = plain_argument_text(command)
            self.note(unknown, _DESCRIPTIONS)
            if command.depth:
                self.note([grouped_reason("description")], _DESCRIPTIONS)
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
            self.note(unknown, _DESCRIPTIONS)
            if not text.strip() or is_placeholder_text(text):
                return False
        return bool(graphics)


def _subfigure_span(command: Command) -> tuple[int, int]:
    r"""Return the span of ``\subfloat[caption]{body}`` or ``\subcaptionbox{caption}{body}``.

    It ends after the command's own arguments: a later brace group is text of the float.
    """
    count = 2 if command.name == "subcaptionbox" else 1
    groups = [*command.options, *command.arguments[:count]]
    return command.start, max((group.end + 1 for group in groups), default=command.name_end)


def _subcaption_minipages(
    commands: list[Command], minipages: list[EnvironmentRegion]
) -> list[tuple[int, int]]:
    r"""Return the minipages that hold a ``\subcaption``, the subfigures subcaption documents.

    Args:
        commands: The commands of the document.
        minipages: The minipage regions of the document.

    Returns:
        The span of the innermost minipage around each ``\subcaption``.
    """
    spans = []
    for command in commands:
        if command.name != "subcaption":
            continue
        around = [
            (region.command.start, region.end)
            for region in minipages
            if region.command.start < command.start < region.end
        ]
        if around:
            spans.append(max(around))
    return spans


def _within(spans: list[tuple[int, int]], region: EnvironmentRegion) -> list[tuple[int, int]]:
    """Return the spans that begin inside the float ``region``."""
    return [span for span in spans if region.command.start < span[0] < region.end]


def _inside(spans: list[tuple[int, int]], offset: int) -> bool:
    """Return whether ``offset`` lies in one of a float's few ``spans``."""
    return any(start <= offset < end for start, end in spans)


def _float_violations(records: list[_FloatRecord]) -> dict[_FloatRule, list[dict[str, object]]]:
    """Collect the violations of every float rule from the records.

    Only confirmed floats are violations. Figures and tables are each checked for first
    references out of source order.

    Args:
        records: The records of every float, in source order.

    Returns:
        The violations of each float rule.
    """
    confirmed = [record for record in records if record.confirmed]
    out_of_order: list[dict[str, object]] = []
    for kind in ("figure", "table"):
        previous = -1
        for record in records:
            position = record.first_reference_offset
            if record.kind != kind or position is None:
                continue
            if position < previous and record.confirmed:
                out_of_order.append(record.details())
            previous = max(previous, position)
    return {
        _CAPTIONS: [
            record.details()
            for record in confirmed
            if not any(caption["value"] or caption.get("unresolved") for caption in record.captions)
        ],
        _LABELS: [record.details() for record in confirmed if not record.labels],
        _LABEL_ORDER: [label for record in records for label in record.misplaced_labels],
        _REFERENCES: [
            {**record.summary(), "labels": record.labels}
            for record in confirmed
            if record.first_reference_offset is None and not record.conditionally_referenced
        ],
        _REFERENCE_ORDER: out_of_order,
        _DESCRIPTIONS: [
            {**record.summary(), "reason": "missing, empty or placeholder Description"}
            for record in confirmed
            if record.described is False
        ],
    }


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
    # A layout-only macro cannot hide a caption, label or reference.
    floats, structure_reasons = reading.scoped_regions(FLOAT_ENVIRONMENTS, layout_matters=False)
    # Unexpanded constructs outside every float cannot caption, label or hide one.
    reasons = [*reading.document.graph_uncertainty, *structure_reasons]
    for region in floats:
        reasons.extend(region.uncertainty)
    scan = _FloatScan(reading.document, floats, definitions=list(reading.definitions))
    scan.run()
    violations = _float_violations(scan.records)
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
            CheckOutcome(violations[rule], [*reasons, *scan.uncertainty[rule]]),
            failure=rule.failure.format(
                count=len(violations[rule]),
                floats=plural(len(floats), "float"),
                labels=plural(len(violations[rule]), "float label"),
                figures=plural(len(violations[rule]), "figure"),
            ),
            details=details,
        )
        for rule in enabled
    ]
