r"""Obsolete packages, package options and commands (``STY101``, ``STY102``).

The lists and their deprecation sources are in ``tex_commands``: obsolete packages such
as times and subfigure, output driver options on packages that detect their driver
themselves, the LaTeX 2.09 font switches, ``$$`` display math, ``eqnarray``, ``\over`` and
``\centerline``. Only the manuscript files are read, not local classes and styles, whose
choices the author may not control. A construct inside a macro definition is reported
where the definition is written.
"""

from __future__ import annotations

from collections.abc import Iterator

from .cancellation import cancellation_point
from .literal_text import PACKAGE_COMMANDS, first_argument_names, key_value_items
from .style_support import Occurrence, StyleContext
from .tex_commands import (
    DRIVER_BOUND_PACKAGES,
    DRIVER_OPTION_PACKAGES,
    OBSOLETE_COMMANDS,
    OBSOLETE_ENVIRONMENTS,
    OBSOLETE_FONT_SWITCHES,
    OBSOLETE_PACKAGES,
    OUTPUT_DRIVER_OPTIONS,
)
from .tex_lexer import Command


def _option_keys(command: Command) -> set[str]:
    """Return the keys of every option group of a command."""
    return {key for option in command.options for key, _ in key_value_items(option.value)}


def _package_occurrences(command: Command, *, drivers: bool) -> Iterator[Occurrence]:
    """Describe the obsolete packages and driver options of one package declaration.

    Args:
        command: A package declaration.
        drivers: Whether driver options and driver-bound packages are reported here.

    Yields:
        One occurrence per obsolete package and per driver option.
    """
    names = first_argument_names(command) or []
    for name in names:
        if name in OBSOLETE_PACKAGES and (drivers or name not in DRIVER_BOUND_PACKAGES):
            yield Occurrence(
                command.start,
                f"The {name} package is obsolete; {OBSOLETE_PACKAGES[name]}.",
                details={"package": name, "advice": OBSOLETE_PACKAGES[name]},
            )
    if not drivers or not set(names) & DRIVER_OPTION_PACKAGES:
        return
    for option in sorted(_option_keys(command) & OUTPUT_DRIVER_OPTIONS):
        yield Occurrence(
            command.start,
            f"The driver option {option} ties {', '.join(names)} to one engine; these "
            "packages detect the driver themselves.",
            suggestion=f"Remove the {option} option.",
            details={"option": option, "packages": names},
        )


def obsolete_packages(context: StyleContext, *, drivers: bool = True) -> list[Occurrence]:
    """Find obsolete packages and output driver options in the manuscript files.

    Args:
        context: The document.
        drivers: Whether driver options, including class options, and driver-bound packages
            such as psfig are reported; the output driver policy reports them when it runs.

    Returns:
        The occurrences in source order.
    """
    occurrences: list[Occurrence] = []
    for command in context.document.commands:
        cancellation_point()
        if command.name in PACKAGE_COMMANDS:
            occurrences.extend(_package_occurrences(command, drivers=drivers))
        elif command.name == "documentclass" and drivers:
            occurrences.extend(
                Occurrence(
                    command.start,
                    f"The class option {option} is passed to graphicx and hyperref and ties "
                    "them to one engine; they detect the driver themselves.",
                    suggestion=f"Remove the {option} class option.",
                    details={"option": option},
                )
                for option in sorted(_option_keys(command) & OUTPUT_DRIVER_OPTIONS)
            )
    return occurrences


def _command_occurrence(command: Command) -> Occurrence | None:
    """Describe an obsolete command or environment, or return ``None`` for any other command."""
    name = command.name
    if name in OBSOLETE_FONT_SWITCHES:
        replacement = OBSOLETE_FONT_SWITCHES[name]
        message = f"\\{name} is an obsolete LaTeX 2.09 font switch; use {replacement}."
    elif name in OBSOLETE_COMMANDS:
        replacement = OBSOLETE_COMMANDS[name]
        message = f"\\{name} is a plain TeX command; use {replacement}."
    elif name == "begin" and command.arguments:
        environment = command.arguments[0].value.strip()
        if environment not in OBSOLETE_ENVIRONMENTS:
            return None
        replacement = OBSOLETE_ENVIRONMENTS[environment]
        message = f"The {environment} environment is obsolete; use {replacement}."
        name = environment
    else:
        return None
    return Occurrence(
        command.start, message, details={"construct": name, "replacement": replacement}
    )


def obsolete_commands(context: StyleContext) -> list[Occurrence]:
    r"""Find obsolete commands and environments, and ``$$`` display math, in the manuscript files.

    Args:
        context: The document.

    Returns:
        The occurrences in source order.
    """
    occurrences = [
        Occurrence(
            offset,
            r"$$ ... $$ is plain TeX display math; use \[ ... \] or an equation* environment.",
            details={"construct": "$$", "replacement": r"\[ ... \]"},
        )
        for offset in context.display_dollars
    ]
    for command in context.document.commands:
        cancellation_point()
        if (occurrence := _command_occurrence(command)) is not None:
            occurrences.append(occurrence)
    return sorted(occurrences, key=lambda occurrence: occurrence.offset)
