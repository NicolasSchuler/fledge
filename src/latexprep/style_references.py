r"""Cross-reference suggestions: unused labels, doubled names, ``\ref`` to equations (``STY2xx``).

A label counts as used when any command other than ``\label`` names it in an argument or
option, so custom reference macros count too. A reference built from a macro parameter,
such as ``\ref{fig:#1}`` in a definition, makes every label with that literal prefix used;
one with no literal prefix makes every label used, and no label is reported. cleveref,
hyperref's ``\autoref`` and zref-clever print the name of what they refer to, so a name
written before them ("Figure~\cref{...}") appears twice. ``\eqref`` prints an equation
number in parentheses, which ``\ref`` leaves out.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections.abc import Iterator

from .cancellation import cancellation_point
from .literal_text import REFERENCE_COMMANDS
from .style_support import Occurrence, StyleContext, math_environment_spans
from .tex_lexer import (
    Command,
    command_end,
    definition_spans,
    in_conditional,
    in_half_open_span,
    merge_spans,
    split_argument_items,
)

# Reference commands whose argument may be built from a macro parameter: the shared table,
# hyperref's \hyperref[label]{text} and the capitalised and varioref forms.
_DYNAMIC_REFERENCE_COMMANDS = REFERENCE_COMMANDS | {
    "hyperref",
    "Autoref",
    "autopageref",
    "Nameref",
    "Vref",
    "vpageref",
    "fref",
    "Fref",
}
# Characters read on each side of a reference for the parentheses an author typed.
_PARENTHESIS_WINDOW = 16
# The literal part of a label before a macro parameter or command.
_DYNAMIC_PART = re.compile(r"[#\\]")
# Names of what a reference points to, in the forms authors type before a reference.
_REFERENCE_NAME = (
    r"figures?|figs?\.|tables?|tabs?\.|sections?|secs?\.|sects?\.|chapters?|chaps?\."
    r"|equations?|eqs?\.|appendix|appendices|algorithms?|algs?\.|theorems?|lemmas?"
    r"|corollary|corollaries|propositions?|definitions?|listings?|lines?"
)
# A name, a tie or white space within one paragraph, then a command that prints the name.
_DOUBLED_NAME = re.compile(
    rf"(?<![\\\w])(?i:{_REFERENCE_NAME})(?:~|[ \t]+(?:\r?\n[ \t]*)?|\r?\n[ \t]*)"
    r"\\(cref|Cref|autoref|Autoref|zcref)(?![A-Za-z@])"
)


def _literal_label(command: Command) -> str | None:
    r"""Return the literal name of a ``\label``, or ``None`` when it is empty or not literal."""
    if not command.arguments:
        return None
    value = command.arguments[0].value.strip()
    return value if value and not _DYNAMIC_PART.search(value) else None


def _mentions(command: Command) -> Iterator[str]:
    """Yield every argument and option of a command, whole and split at commas, stripped."""
    for argument in (*command.arguments, *command.options):
        yield argument.value.strip()
        for item in split_argument_items(argument):
            yield item.value


def _shortest_prefixes(prefixes: set[str]) -> list[str]:
    """Return the prefixes that extend no other prefix, sorted.

    A label that starts with a longer prefix also starts with the shorter one it extends, so
    the shorter one decides alone. In sorted order a prefix comes before its extensions,
    which follow it directly.

    Args:
        prefixes: The literal prefixes of dynamic references, none of them empty.

    Returns:
        The prefixes no other prefix is a prefix of, in sorted order.
    """
    shortest: list[str] = []
    for prefix in sorted(prefixes):
        if not shortest or not prefix.startswith(shortest[-1]):
            shortest.append(prefix)
    return shortest


def _has_prefix(shortest: list[str], name: str) -> bool:
    """Return whether ``name`` starts with one of the sorted prefixes, none extending another.

    A prefix of ``name`` sorts at or before it, and any string between that prefix and
    ``name`` would extend the prefix, so only the last prefix at or before ``name`` can
    match. One binary search replaces a comparison with every prefix.

    Args:
        shortest: Prefixes as :func:`_shortest_prefixes` returns them.
        name: The label.

    Returns:
        Whether a prefix of ``name`` is in ``shortest``.
    """
    index = bisect_right(shortest, name)
    return bool(index) and name.startswith(shortest[index - 1])


def unused_labels(context: StyleContext) -> list[Occurrence]:
    r"""Find literal labels that no command names.

    Labels inside macro definitions or conditionals are left out: they may never be set.

    Args:
        context: The document.

    Returns:
        One occurrence per unused label, at the ``\label``; none when a reference is built
        from a macro parameter with no literal prefix.
    """
    document = context.document
    definitions = definition_spans(document.text, document.commands)
    labels: list[tuple[str, int]] = []
    mentioned: set[str] = set()
    prefixes: set[str] = set()
    for command in document.commands:
        cancellation_point()
        if command.name == "label":
            name = _literal_label(command)
            if name is not None and not (
                in_half_open_span(definitions, command.start)
                or in_conditional(document.conditionals, command.start)
            ):
                labels.append((name, command.start))
            continue
        for value in _mentions(command):
            dynamic = _DYNAMIC_PART.search(value)
            if dynamic is None:
                mentioned.add(value)
            elif command.name in _DYNAMIC_REFERENCE_COMMANDS:
                if not dynamic.start():
                    return []
                prefixes.add(value[: dynamic.start()])
    shortest = _shortest_prefixes(prefixes)
    occurrences = []
    for name, offset in labels:
        cancellation_point()
        if name not in mentioned and not _has_prefix(shortest, name):
            occurrences.append(
                Occurrence(offset, f"Label {name!r} is never referenced.", details={"label": name})
            )
    return occurrences


def doubled_reference_names(context: StyleContext) -> list[Occurrence]:
    r"""Find a name such as "Figure" typed before a command that prints the name itself.

    Args:
        context: The document.

    Returns:
        One occurrence per doubled name, at the name.
    """
    occurrences = []
    for match in _DOUBLED_NAME.finditer(context.prose):
        written = match.group().split("\\", 1)[0].rstrip(" \t\r\n~")
        command = match.group(1)
        occurrences.append(
            Occurrence(
                match.start(),
                f"{written!r} before \\{command} prints the name twice, because \\{command} "
                "writes the name of what it refers to.",
                suggestion=f"Write \\{command}{{...}} without {written!r}, or keep the word "
                "and use \\ref.",
                details={"name": written, "command": command},
            )
        )
    return occurrences


def _equation_labels(context: StyleContext) -> tuple[set[str], list[tuple[int, int]]]:
    """Return the labels set inside math environments, and the spans of those environments."""
    document = context.document
    spans = merge_spans(math_environment_spans(document.commands, len(document.text)))
    labels = {
        name
        for command in document.commands
        if command.name == "label"
        and in_half_open_span(spans, command.start)
        and (name := _literal_label(command)) is not None
    }
    return labels, spans


def _parenthesized(text: str, start: int, end: int) -> bool:
    r"""Return whether the source writes the parentheses itself, as in ``(\ref{eq:x})``.

    Only a few characters on each side are read, so many references stay linear.

    Args:
        text: The expanded text.
        start: The offset of the reference.
        end: The offset just past it.

    Returns:
        Whether an opening parenthesis precedes the reference and a closing one follows it,
        white space aside.
    """
    before = text[max(0, start - _PARENTHESIS_WINDOW) : start].rstrip()
    return before.endswith("(") and text[end : end + _PARENTHESIS_WINDOW].lstrip().startswith(")")


def equation_references(context: StyleContext) -> list[Occurrence]:
    r"""Find ``\ref`` pointing to a label set in a math environment.

    A reference that the source puts in parentheses itself, or that stands in mathematics,
    is left out.

    Args:
        context: The document.

    Returns:
        One occurrence per such reference, at the ``\ref``.
    """
    labels, spans = _equation_labels(context)
    if not labels:
        return []
    text = context.document.text
    occurrences = []
    for command in context.document.commands:
        cancellation_point()
        if command.name != "ref" or not command.arguments:
            continue
        label = command.arguments[0].value.strip()
        end = command_end(command)
        if (
            label not in labels
            or in_half_open_span(spans, command.start)
            or _parenthesized(text, command.start, end)
        ):
            continue
        occurrences.append(
            Occurrence(
                command.start,
                f"\\ref{{{label}}} points to an equation; \\eqref prints its number in "
                "parentheses, as equation numbers are shown.",
                suggestion=f"Write \\eqref{{{label}}} (amsmath).",
                details={"label": label},
            )
        )
    return occurrences
