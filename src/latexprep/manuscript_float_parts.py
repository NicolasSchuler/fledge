r"""The spans inside a float whose captions and labels are not the float's own.

A subfigure (the ``subfigure`` and ``subtable`` environments, ``\subfloat``,
``\subcaptionbox``, a minipage that holds a ``\subcaption``) is captioned and labelled
apart from the float around it, and a label inside an equation or a list names that
equation or item. The float policies (``manuscript_float_checks``) read a float's own
commands outside these spans. An environment that stores its content, or one the paper
defines in a way that was not read, may not typeset a caption or label inside it at all.
"""

from __future__ import annotations

from .expanded_document import EnvironmentRegion, ExpandedDocument, environment_regions
from .manuscript_vocabulary import (
    LIST_ENVIRONMENTS,
    SUBFIGURE_COMMANDS,
    SUBFIGURE_ENVIRONMENTS,
)
from .tex_commands import ALL_MATH_ENVIRONMENTS
from .tex_lexer import Command
from .tex_spans import merge_spans

# Environments that save their content in a box instead of typesetting it where it stands.
_STORING_ENVIRONMENTS = frozenset({"lrbox"})
_UNKNOWN_ENVIRONMENT = "environment "


def part_spans(document: ExpandedDocument) -> list[tuple[int, int]]:
    """Return the span of every subfigure of the document, in no particular order."""
    subfigures, _ = environment_regions(document, SUBFIGURE_ENVIRONMENTS)
    minipages, _ = environment_regions(document, {"minipage"})
    return [
        *((region.command.start, region.end) for region in subfigures),
        *(
            _subfigure_span(command)
            for command in document.commands
            if command.name in SUBFIGURE_COMMANDS
        ),
        *_subcaption_minipages(document.commands, minipages),
    ]


def stored_spans(
    document: ExpandedDocument, unknown: list[tuple[int, str]], defined: frozenset[str]
) -> list[tuple[int, int]]:
    r"""Return the merged spans of environments that may not typeset what they hold.

    Those are ``lrbox``, which saves its content in a box, and each environment of the
    paper that the policies could not interpret, such as
    ``\newenvironment{hidebox}{\setbox0\vbox\bgroup}{\egroup}``.

    Args:
        document: The expanded document.
        unknown: The constructs of the document that were not interpreted, with their
            labels, without those that only change the layout (``Reading.constructs``).
        defined: The commands and environments the document defines.

    Returns:
        The spans, merged and sorted.
    """
    names = {
        label.removeprefix(_UNKNOWN_ENVIRONMENT)
        for _, label in unknown
        if label.startswith(_UNKNOWN_ENVIRONMENT)
    }
    own = {name for name in names if f"begin{{{name}}}" in defined}
    regions, _ = environment_regions(document, own | _STORING_ENVIRONMENTS)
    return merge_spans((region.command.start, region.end) for region in regions)


def foreign_spans(document: ExpandedDocument) -> list[tuple[int, int]]:
    """Return the span of every equation and list, whose labels never name a float."""
    equations, _ = environment_regions(document, ALL_MATH_ENVIRONMENTS | LIST_ENVIRONMENTS)
    return [(region.command.start, region.end) for region in equations]


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


def within(spans: list[tuple[int, int]], region: EnvironmentRegion) -> list[tuple[int, int]]:
    """Return the spans that begin inside the float ``region``."""
    return [span for span in spans if region.command.start < span[0] < region.end]


def inside(spans: list[tuple[int, int]], offset: int) -> bool:
    """Return whether ``offset`` lies in one of a float's few ``spans``."""
    return any(start <= offset < end for start, end in spans)
