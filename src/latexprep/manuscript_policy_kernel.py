"""What every opt-in manuscript policy shares: the reading, the outcome and the finding.

A policy reads the expanded document, finds the violations it can confirm, and
collects the constructs it could not interpret. ``policy_finding`` turns those into one
finding with a rule that is deliberately asymmetric: a confirmed violation fails the
policy even when other constructs are unclear, and an interpretation gap decides the
result only when nothing was confirmed. The ``Reading`` supplies the gaps that apply
to the whole document. This module owns no policy itself.
"""

from __future__ import annotations

from bisect import bisect_left
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import NamedTuple

from .cancellation import cancellation_point
from .expanded_document import (
    UNCLOSED_ENVIRONMENT,
    EnvironmentRegion,
    ExpandedDocument,
    environment_regions,
)
from .finding_text import check_status, list_locations, more_suffix
from .finding_weight import POLICY, Weight
from .findings import Finding
from .manuscript_vocabulary import (
    COUNTER_CHANGES,
    DRAWING_ENVIRONMENTS,
    INTERPRETED_COMMANDS,
    INTERPRETED_ENVIRONMENTS,
    LAYOUT_ONLY_COMMANDS,
    NEUTRAL_COMMANDS,
    NEUTRAL_ENVIRONMENTS,
)
from .tex_lexer import Command
from .tex_spans import (
    defined_names,
    definition_spans,
    in_conditional,
    in_half_open_span,
    macro_definitions,
    merge_spans,
    opens_conditional,
)
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
        layout_only: The offsets in ``unknown`` of project macros and environments built
            only from neutral and layout commands: they may change the layout, but print
            no caption, label, reference or heading.
        definitions: The merged definition spans of the document, inside which nothing
            runs where it stands.
    """

    document: ExpandedDocument
    unknown: list[tuple[int, str]]
    uncertainty: list[str]
    layout_only: frozenset[int] = frozenset()
    definitions: tuple[tuple[int, int], ...] = ()

    def unknown_reasons(self, start: int, end: int, *, layout_matters: bool = True) -> list[str]:
        """Describe the unexpanded constructs between two offsets as one reason or none.

        Args:
            start: The first offset of the range.
            end: The offset just past the range.
            layout_matters: Whether a policy reads layout, so that layout-only macros are
                gaps for it; the float policies do not.

        Returns:
            One reason naming the constructs in the range, or an empty list.
        """
        unknown = (
            self.unknown
            if layout_matters
            else [item for item in self.unknown if item[0] not in self.layout_only]
        )
        return _unknown_reasons(unknown, start, end)

    def project_macro_reasons(self) -> list[str]:
        """Describe the uses of the project's own macros that the reading could not interpret.

        A class or package command does not type the author's prose; a macro the project
        defines may, so a scan of typed text keeps these gaps only.

        Returns:
            One reason naming those macros, or an empty list.
        """
        project = defined_names(self.document.commands)
        own = [item for item in self.unknown if _defined_label(item[1]) in project]
        return _unknown_reasons(own, 0, len(self.document.text))

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

    def scoped_regions(
        self, kinds: Iterable[str], *, layout_matters: bool = True
    ) -> tuple[list[EnvironmentRegion], list[str]]:
        """Find environment regions with the unexpanded constructs inside each one.

        Args:
            kinds: The environment names to collect.
            layout_matters: As for :meth:`unknown_reasons`.

        Returns:
            The regions in source order, each with its unexpanded constructs added to its
            ``uncertainty``, and the delimiter problems found while collecting them.
        """
        regions, reasons = environment_regions(self.document, frozenset(kinds))
        for region in regions:
            region.uncertainty.extend(
                self.unknown_reasons(region.start, region.end, layout_matters=layout_matters)
            )
        return regions, reasons


def interpret(document: ExpandedDocument) -> Reading:
    """Find body constructs outside the interpreted inventory.

    Preamble declarations configure the toolchain; they are not manuscript content,
    and a macro definition only names a replacement body. A macro or environment the
    project defines from neutral commands alone (:class:`_NeutralDefinitions`) is
    interpreted where its arguments follow it, and so is a command that is not the
    project's inside a closed drawing or algorithm environment, which draws or typesets
    a step.

    Args:
        document: The expanded document to read.

    Returns:
        The reading, whose document-wide uncertainty adds the source-graph gaps and
        unclosed drawings to the unexpanded constructs.
    """
    definitions = definition_spans(document.text, document.commands)
    neutral = _NeutralDefinitions.of(document)
    project = defined_names(document.commands)
    drawings, drawing_reasons = environment_regions(document, DRAWING_ENVIRONMENTS)
    drawing_spans = merge_spans(
        (region.command.start, region.end)
        for region in drawings
        if UNCLOSED_ENVIRONMENT not in region.uncertainty
    )
    unknown: list[tuple[int, str]] = []
    layout_only: set[int] = set()
    for command in document.commands:
        cancellation_point()
        if (
            command.start < document.body_start
            or in_half_open_span(definitions, command.start)
            or command.named
        ):
            continue
        if opens_conditional(command) or command.name in _CONDITIONAL_WORDS:
            # Both branches are read; a check of a region learns that the region holds
            # control flow, a document-wide claim does not depend on which branch runs.
            unknown.append((command.start, CONDITIONAL_FLOW))
            continue
        if command.name in COUNTER_CHANGES:
            unknown.append((command.start, "explicit counter modification"))
        if neutral.covers(command) or (
            command.name not in project and in_half_open_span(drawing_spans, command.start)
        ):
            continue
        label = _unknown_label(command)
        if label is not None:
            unknown.append((command.start, label))
            if neutral.covers(command, layout=True):
                layout_only.add(command.start)
    uncertainty = [
        *document.graph_uncertainty,
        *drawing_reasons,
        *_unknown_reasons(
            [item for item in unknown if item[1] != CONDITIONAL_FLOW], 0, len(document.text)
        ),
    ]
    return Reading(document, unknown, uncertainty, frozenset(layout_only), tuple(definitions))


def _defined_label(label: str) -> str:
    r"""Return the name ``defined_names`` gives the construct of an unknown label.

    ``\name`` becomes ``name``, and ``environment name`` becomes ``begin{name}``.
    """
    if label.startswith("environment "):
        return f"begin{{{label.removeprefix('environment ')}}}"
    return label.removeprefix("\\")


# The words of a primitive conditional after its opening \if…: its branches and its end.
_CONDITIONAL_WORDS = frozenset({"else", "or", "fi"})


def _unknown_label(command: Command) -> str | None:
    """Return how an uninterpreted command or environment is named in reasons, if it is one."""
    if command.name in {"begin", "end"} and command.arguments:
        environment = command.arguments[0].value.strip()
        return None if environment in INTERPRETED_ENVIRONMENTS else "environment " + environment
    return None if command.name in INTERPRETED_COMMANDS else "\\" + command.name


@dataclass(frozen=True, slots=True)
class _NeutralDefinitions:
    r"""The project's macros and environments whose every definition is neutral.

    A definition is neutral when its replacement text uses only ``NEUTRAL_COMMANDS``,
    ``NEUTRAL_ENVIRONMENTS`` and other neutral definitions, as
    ``\newcommand{\capslot}{$\langle$capability$\rangle$}`` does; it prints text and
    nothing a policy reads. One that also uses ``LAYOUT_ONLY_COMMANDS`` may change the
    layout but hides no caption, label or reference. A definition that uses an unknown
    command, a caption, label, reference, heading or TeX machinery is neither; nor is an
    environment whose options mention a label or caption, nor anything when the project
    builds definition names with ``\csname``. Definitions that use each other are
    resolved in dependency order; a cycle stays unexpanded.

    Attributes:
        neutral: The braced arguments a use of each neutral name reads, by name; an
            environment is named ``begin{name}``.
        layout: The same for names that are neutral only with layout commands.
    """

    neutral: dict[str, int]
    layout: dict[str, int]

    @classmethod
    def of(cls, document: ExpandedDocument) -> _NeutralDefinitions:
        """Read the neutral definitions of a document's selected sources."""
        commands = document.commands
        if any(
            (command.name == "csname" and command.named_by) or command.name == "@namedef"
            for command in commands
        ):
            return cls({}, {})
        starts = [command.start for command in commands]
        strict: dict[str, set[str]] = {}
        loose: dict[str, set[str]] = {}
        parameters: dict[str, int] = {}
        refused: set[str] = set()
        for definition in macro_definitions(document.text, commands):
            cancellation_point()
            body = commands[
                bisect_left(starts, definition.start) : bisect_left(starts, definition.end)
            ]
            uses = _uses(body)
            strict.setdefault(definition.name, set()).update(uses)
            loose.setdefault(definition.name, set()).update(uses - LAYOUT_ONLY_COMMANDS)
            parameters[definition.name] = max(
                parameters.get(definition.name, 0), definition.parameters
            )
            if definition.name.startswith("begin{"):
                text = document.text[definition.start : definition.end]
                if "label" in text or "caption" in text:
                    refused.add(definition.name)
        neutral = _resolved(
            strict, refused | {name for name, uses in strict.items() if uses - strict.keys()}
        )
        layout = _resolved(
            loose, refused | {name for name, uses in loose.items() if uses - loose.keys()}
        )
        return cls(
            {name: parameters[name] for name in neutral},
            {name: parameters[name] for name in layout - neutral},
        )

    def covers(self, command: Command, *, layout: bool = False) -> bool:
        r"""Return whether a use of a command or environment is neutral, its arguments present.

        Args:
            command: The use: a command, or the ``\begin`` of an environment.
            layout: Whether to ask about definitions that are neutral only with layout
                commands instead.

        Returns:
            Whether the definition is neutral and the use has the braced arguments it reads,
            so that it swallows no following token.
        """
        names = self.layout if layout else self.neutral
        if command.name in {"begin", "end"} and command.arguments:
            needed = names.get(f"begin{{{command.arguments[0].value.strip()}}}")
            # \end reads no argument; \begin reads the environment's after its name.
            arguments = len(command.arguments) - 1 if command.name == "begin" else needed
            return needed is not None and arguments is not None and arguments >= needed
        needed = names.get(command.name)
        return needed is not None and len(command.arguments) >= needed


def _uses(body: Sequence[Command]) -> set[str]:
    """Return what a replacement text uses beyond ``NEUTRAL_COMMANDS`` and its environments."""
    uses = set()
    for command in body:
        if command.name in {"begin", "end"} and command.arguments:
            environment = command.arguments[0].value.strip()
            if environment not in NEUTRAL_ENVIRONMENTS:
                uses.add(f"begin{{{environment}}}")
        elif command.name not in NEUTRAL_COMMANDS:
            uses.add(command.name)
    return uses


def _resolved(needs: dict[str, set[str]], refused: set[str]) -> set[str]:
    """Return the names whose uses all resolve to other resolved names, in dependency order.

    Args:
        needs: The defined names each definition uses, by defined name.
        refused: The names that can never be resolved.

    Returns:
        The resolved names; a name in a cycle or depending on a refused one is not.
    """
    waiting = {name: set(uses) for name, uses in needs.items() if name not in refused}
    users: dict[str, list[str]] = {}
    for name, uses in waiting.items():
        for used in uses:
            users.setdefault(used, []).append(name)
    ready = [name for name, uses in waiting.items() if not uses]
    resolved: set[str] = set()
    while ready:
        name = ready.pop()
        if name in resolved:
            continue
        resolved.add(name)
        for user in users.get(name, ()):
            remaining = waiting.get(user)
            if remaining is not None:
                remaining.discard(name)
                if not remaining:
                    ready.append(user)
    return resolved


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
