"""What every opt-in manuscript policy shares: the reading, the outcome and the finding.

A policy reads the expanded document, finds the violations it can confirm, and
collects the constructs it could not interpret. ``policy_finding`` turns those into one
finding with a rule that is deliberately asymmetric: a confirmed violation fails the
policy even when other constructs are unclear, and an interpretation gap decides the
result only when nothing was confirmed. The ``Reading`` supplies the gaps that apply
to the whole document. This module owns no policy itself.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from .cancellation import cancellation_point
from .expanded_document import (
    EnvironmentRegion,
    ExpandedDocument,
    environment_regions,
)
from .finding_text import check_status, list_locations, more_suffix
from .finding_weight import POLICY, Weight
from .findings import Finding
from .manuscript_vocabulary import COUNTER_CHANGES, INTERPRETED_COMMANDS, INTERPRETED_ENVIRONMENTS
from .tex_lexer import definition_spans, in_conditional, in_half_open_span
from .uncertainty_reasons import CONDITIONAL_FLOW

# Offending names spelled out in a message; the details list every one.
MAX_NAMED = 3
# Unexpanded constructs named in one reason; the rest are counted by the details.
_MAX_REPORTED_CONSTRUCTS = 20
SCOPE = (
    "Selected literal source graph; macros are not expanded and rendered output is not inferred."
)


class Policy(NamedTuple):
    """One policy's rule name, the title it reports, and how hard a violation hits.

    Attributes:
        rule: The rule name of the finding.
        title: The title the finding reports, also its message when the policy passes.
        weight: How hard a violation hits: the severity per status and the evidence kind.
    """

    rule: str
    title: str
    weight: Weight = POLICY


class CheckOutcome(NamedTuple):
    """What one check established: confirmed violations and the gaps that remain.

    Attributes:
        violations: The confirmed violations, each with its location and details.
        uncertainty: The interpretation gaps that remain.
        other_gaps: Whether the details report gaps beyond ``uncertainty``.
    """

    violations: Sequence[Mapping[str, object]]
    uncertainty: Iterable[str]
    # The details report further gaps beyond ``uncertainty``, which also mark the evidence
    # incomplete: the result cannot be waived while any reported evidence is unclear.
    other_gaps: bool = False


def listing(label: str, names: Iterable[str]) -> str:
    """Format the first few distinct names under a label: ``Missing: a, b, c (+2 more)``.

    Args:
        label: The text before the colon.
        names: The names to list; repeats are dropped and at most ``MAX_NAMED`` are
            spelled out.

    Returns:
        The label followed by the names and a count of the rest.
    """
    unique = list(dict.fromkeys(names))
    return f"{label}: {', '.join(unique[:MAX_NAMED])}{more_suffix(len(unique) - MAX_NAMED)}"


def _unknown_reasons(unknown: Sequence[tuple[int, str]], start: int, end: int) -> list[str]:
    """Describe unexpanded body constructs inside one checked region only.

    Args:
        unknown: Offset and label of each unexpanded body construct.
        start: The first offset of the region.
        end: The offset just past the region.

    Returns:
        One reason naming the distinct labels in the region, or an empty list.
    """
    labels = sorted({label for offset, label in unknown if start <= offset < end})
    if not labels:
        return []
    return [
        "unexpanded or context-dependent constructs: "
        + ", ".join(labels[:_MAX_REPORTED_CONSTRUCTS])
    ]


@dataclass(frozen=True, slots=True)
class Reading:
    """The expanded document and the body constructs these checks cannot interpret.

    Attributes:
        document: The expanded document that was read.
        unknown: Offset and label of each unexpanded body construct, in source order.
        uncertainty: Everything a document-wide scan cannot interpret.
    """

    document: ExpandedDocument
    unknown: list[tuple[int, str]]
    uncertainty: list[str]

    def unknown_reasons(self, start: int, end: int) -> list[str]:
        """Describe the unexpanded constructs between two offsets as one reason or none.

        Args:
            start: The first offset of the range.
            end: The offset just past the range.

        Returns:
            One reason naming the constructs in the range, or an empty list.
        """
        return _unknown_reasons(self.unknown, start, end)

    def scope(self, region: EnvironmentRegion) -> None:
        """Attach only the interpretation gaps that fall inside this region.

        A region whose opening command lies inside a conditional also gets
        ``CONDITIONAL_FLOW``.

        Args:
            region: The region whose ``uncertainty`` is extended in place.
        """
        if in_conditional(self.document.conditionals, region.command.start):
            region.uncertainty.append(CONDITIONAL_FLOW)
        region.uncertainty.extend(self.unknown_reasons(region.start, region.end))

    def scoped_regions(self, kinds: Iterable[str]) -> tuple[list[EnvironmentRegion], list[str]]:
        """Find environment regions with the unexpanded constructs inside each one.

        Args:
            kinds: The environment names to collect.

        Returns:
            The regions in source order, each with its unexpanded constructs added to its
            ``uncertainty``, and the delimiter problems found while collecting them.
        """
        regions, reasons = environment_regions(self.document, frozenset(kinds))
        for region in regions:
            region.uncertainty.extend(self.unknown_reasons(region.start, region.end))
        return regions, reasons


def interpret(document: ExpandedDocument) -> Reading:
    """Find body constructs outside the interpreted inventory.

    Preamble declarations configure the toolchain; they are not manuscript content,
    and a macro definition only names a replacement body.

    Args:
        document: The expanded document to read.

    Returns:
        The reading, whose document-wide uncertainty adds the document's presence
        uncertainty to the unexpanded constructs.
    """
    definitions = definition_spans(document.text, document.commands)
    unknown: list[tuple[int, str]] = []
    for command in document.commands:
        cancellation_point()
        if command.start < document.body_start or in_half_open_span(definitions, command.start):
            continue
        if command.name not in INTERPRETED_COMMANDS:
            unknown.append((command.start, "\\" + command.name))
        if command.name in {"begin", "end"} and command.arguments:
            environment = command.arguments[0].value.strip()
            if environment not in INTERPRETED_ENVIRONMENTS:
                unknown.append((command.start, "environment " + environment))
        if command.name in COUNTER_CHANGES:
            unknown.append((command.start, "explicit counter modification"))
    uncertainty = [
        *document.presence_uncertainty,
        *_unknown_reasons(unknown, 0, len(document.text)),
    ]
    return Reading(document, unknown, uncertainty)


def policy_finding(
    policy: Policy,
    outcome: CheckOutcome,
    *,
    failure: str | None = None,
    details: Mapping[str, object] | None = None,
    named: str = "",
) -> Finding:
    """Report confirmed violations as failures even when other constructs are unclear.

    Callers only record a violation for a construct they could interpret, so a
    violation is never in doubt. Remaining interpretation gaps stay in the details
    and decide the outcome only when nothing was confirmed. Any remaining gap, or
    ``outcome.other_gaps``, marks the evidence incomplete.

    Args:
        policy: The policy whose rule, title and weight the finding carries.
        outcome: The confirmed violations and remaining gaps of the check.
        failure: A summary of the violations; it defaults to the policy title.
        details: Extra details merged after the matches, uncertainty and scope.
        named: The offending items, listed after the locations.

    Returns:
        One finding that failed, passed or is inconclusive.
    """
    violations = outcome.violations
    reasons = sorted(set(outcome.uncertainty))
    status = check_status(failed=bool(violations), incomplete=bool(reasons))
    location = violations[0] if len(violations) == 1 else {}
    path, line = location.get("path"), location.get("line")
    title = policy.title
    match status:
        case "failed":
            where = list_locations(violations)
            message = (failure or title.rstrip(".")) + (": " + where if where else "") + "."
            if named:
                message += " " + named + "."
            if reasons:
                message += " Other constructs remain inconclusive: " + reasons[0].rstrip(".") + "."
        case "inconclusive":
            message = title + " Inconclusive: " + reasons[0].rstrip(".") + "."
        case _:
            message = title
    return Finding(
        policy.rule,
        message,
        severity=policy.weight.severity(status),
        status=status,
        path=path if isinstance(path, str) else None,
        line=line if isinstance(line, int) else None,
        evidence_kind=policy.weight.evidence_kind,
        incomplete=bool(reasons) or outcome.other_gaps,
        details={
            "matches": [dict(item) for item in violations],
            "uncertainty": reasons,
            "scope": SCOPE,
            **(details or {}),
        },
    )
