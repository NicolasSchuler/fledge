"""Captions, labels, references and descriptions of figures and tables (``MAN011`` to ``MAN016``).

Six opt-in policies read each ``figure`` and ``table`` environment of the expanded
document: a nonempty caption, a label, a label that follows the caption, an external
reference, first references in source order, and a usable figure ``Description``. Order
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
from .expanded_document import EnvironmentRegion, ExpandedDocument, Location
from .finding_text import plural
from .findings import Finding
from .literal_text import (
    REFERENCE_COMMANDS,
    first_argument_names,
    is_placeholder_text,
    plain_argument_text,
)
from .manuscript_check_options import ManuscriptCheckOptions
from .manuscript_policy_kernel import CheckOutcome, Policy, Reading, policy_finding
from .manuscript_vocabulary import FLOAT_ENVIRONMENTS
from .tex_lexer import Command, command_end, in_closed_span, merge_spans, split_argument_items
from .uncertainty_reasons import grouped_reason


class _FloatRule(NamedTuple):
    """One float policy: its rule and title, and the template of its failure summary."""

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


@dataclass(slots=True)
class _FloatRecord:
    """What the source shows about one figure or table environment."""

    location: Location
    kind: str
    # False when the float's own region could not be interpreted.
    confirmed: bool
    source_offset: int
    captions: list[dict[str, object]]
    labels: list[str]
    first_reference_offset: int | None
    # Labels that precede the float's first caption, as reported violations.
    misplaced_labels: list[dict[str, object]]
    # Whether a figure has a nonempty, non-placeholder Description; None for tables.
    described: bool | None

    def summary(self) -> dict[str, object]:
        return {
            **self.location.as_dict(),
            "kind": self.kind,
            "confirmed": self.confirmed,
            "source_offset": self.source_offset,
        }

    def details(self) -> dict[str, object]:
        return {
            **self.summary(),
            "captions": self.captions,
            "labels": self.labels,
            "first_reference_offset": self.first_reference_offset,
        }


@dataclass
class _FloatScan:
    """Reads each float region while collecting per-rule interpretation gaps."""

    document: ExpandedDocument
    floats: list[EnvironmentRegion]
    uncertainty: dict[_FloatRule, list[str]] = field(
        default_factory=lambda: {rule: [] for rule in _FLOAT_RULES}
    )
    first_references: dict[str, int] = field(default_factory=dict)
    label_counts: Counter[str] = field(default_factory=Counter)
    labels_seen: set[str] = field(default_factory=set)
    records: list[_FloatRecord] = field(default_factory=list)

    def note(self, reasons: Iterable[str], *rules: _FloatRule) -> None:
        for rule in rules:
            self.uncertainty[rule].extend(reasons)

    def run(self) -> None:
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
        float_spans = merge_spans((region.command.start, region.end) for region in self.floats)
        for command in self.document.commands:
            if command.name not in REFERENCE_COMMANDS:
                continue
            if in_closed_span(float_spans, command.start):
                continue
            names = first_argument_names(command)
            if names is None or command.depth:
                self.note(["grouped or nonliteral reference"], _REFERENCES, _REFERENCE_ORDER)
                continue
            for argument in split_argument_items(command.arguments[0]):
                self.first_references.setdefault(argument.value, argument.start)

    def _record(self, region: EnvironmentRegion) -> _FloatRecord:
        # A float whose own region could not be interpreted contributes reasons,
        # never violations: an unexpanded construct inside it may be the caption,
        # label or description that appears to be missing.
        confirmed = not region.uncertainty
        kind = region.kind.rstrip("*")
        captions = [command for command in region.commands if command.name == "caption"]
        caption_data = self._read_captions(captions)
        first_caption_end = min((command_end(command) for command in captions), default=None)
        labels, misplaced = self._read_labels(region, first_caption_end, confirmed=confirmed)
        first = min(
            (self.first_references[label] for label in labels if label in self.first_references),
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
        )

    def _read_captions(self, captions: list[Command]) -> list[dict[str, object]]:
        data = []
        for command in captions:
            value, unknown = plain_argument_text(command)
            self.note(unknown, _CAPTIONS)
            if command.depth:
                self.note([grouped_reason("caption")], _CAPTIONS)
            data.append(self.document.located(command.start, value=value))
        if len(captions) > 1:
            self.note(["multiple captions within one float"], _LABEL_ORDER)
        return data

    def _read_labels(
        self, region: EnvironmentRegion, first_caption_end: int | None, *, confirmed: bool
    ) -> tuple[list[str], list[dict[str, object]]]:
        """The float's literal labels, and those placed before its first caption."""
        kind = region.kind.rstrip("*")
        labels: list[str] = []
        misplaced: list[dict[str, object]] = []
        for command in region.commands:
            if command.name != "label":
                continue
            names = first_argument_names(command)
            if command.arguments and not command.arguments[0].value.strip():
                continue
            if names is None or len(names) != 1 or command.depth:
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

    def _described(self, region: EnvironmentRegion) -> bool:
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
        return usable


def _float_violations(records: list[_FloatRecord]) -> dict[_FloatRule, list[dict[str, object]]]:
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
            if not any(caption["value"] for caption in record.captions)
        ],
        _LABELS: [record.details() for record in confirmed if not record.labels],
        _LABEL_ORDER: [label for record in records for label in record.misplaced_labels],
        _REFERENCES: [
            {**record.summary(), "labels": record.labels}
            for record in confirmed
            if record.first_reference_offset is None
        ],
        _REFERENCE_ORDER: out_of_order,
        _DESCRIPTIONS: [
            {**record.summary(), "reason": "missing, empty or placeholder Description"}
            for record in confirmed
            if record.described is False
        ],
    }


def _enabled_rules(options: ManuscriptCheckOptions) -> list[_FloatRule]:
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
    """One finding per float policy that is switched on."""
    enabled = _enabled_rules(options)
    if not enabled:
        return []
    floats, structure_reasons = reading.scoped_regions(FLOAT_ENVIRONMENTS)
    # Unexpanded constructs outside every float cannot caption, label or hide one.
    reasons = [*reading.document.graph_uncertainty, *structure_reasons]
    for region in floats:
        reasons.extend(region.uncertainty)
    scan = _FloatScan(reading.document, floats)
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
