"""Common command-line mistakes, turned into what was meant or into a clear usage error.

Owns the readings of a command line that a person would make: a ``.tex`` file given as
INPUT names its folder and its main file, ``--main`` may be written as a path from the
current folder into INPUT, ``prepare`` needs a destination, and ``--select`` must choose a
check the command can run. Everything here happens before the job starts, so the messages
can name the command-line options.
"""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import click

from .check_selection import resolve_selectors, selected_codes
from .errors import PreparationError
from .job_context import JobCommand
from .text_safety import escape_single_line_text

# The check families each command can never run, by code family. ``inspect`` builds no
# PDF, ``bib check`` runs only the bibliography and online reference checks, and
# ``pdf check`` reads nothing but the PDF.
_FAMILIES_NOT_RUN: dict[JobCommand, frozenset[str]] = {
    JobCommand.INSPECT: frozenset({"BLD", "CMP", "FMT", "PDF"}),
    JobCommand.BIBLIOGRAPHY: frozenset({"BLD", "CMP", "FMT", "MAN", "PDF"}),
    JobCommand.PDF: frozenset({"BIB", "BLD", "FMT", "MAN", "NET", "PKG", "TEX"}),
}
# Checks of such a family that the command runs all the same: inspect checks the colour
# space of the figures in the sources (PDF313) as part of its readiness checks.
_RUN_DESPITE_FAMILY: dict[JobCommand, frozenset[str]] = {
    JobCommand.INSPECT: frozenset({"PDF313"}),
}
# Why a command cannot run the other checks, and which command can.
_NOT_RUN_REASONS: dict[JobCommand, tuple[str, str]] = {
    JobCommand.INSPECT: (
        "inspect does not build the PDF",
        "fledge check builds the PDF and runs them",
    ),
    JobCommand.BIBLIOGRAPHY: (
        "bib check runs only the bibliography and online reference checks",
        "fledge inspect or fledge check runs them",
    ),
    JobCommand.PDF: (
        "pdf check reads only the PDF",
        "fledge inspect or fledge check on the sources runs them",
    ),
}
# How many check codes a message about unrunnable checks names before it counts the rest.
_MAX_NAMED_CODES = 5


class InputChoice(NamedTuple):
    """The input and main file a command works on, and a note when they were reinterpreted.

    Attributes:
        source: The input: a project folder or ZIP, or the PDF of ``pdf check``.
        main: The main file relative to the input, or ``None``.
        note: One line that says how a ``.tex`` file given as INPUT was read, or ``None``.
    """

    source: Path
    main: str | None
    note: str | None = None


def _main_inside_input(source: Path, main: str) -> str:
    """Return ``main`` relative to the folder ``source`` when it names a file inside it.

    ``--main`` is relative to INPUT. When it is not, but the path read from the current
    folder (or an absolute path) names a file inside INPUT, the path relative to INPUT is
    returned instead, so ``fledge check paper --main paper/main.tex`` works. Anything else
    is returned unchanged for the job to judge.

    Args:
        source: The input folder.
        main: The value of ``--main``.

    Returns:
        The main file relative to ``source``, or ``main`` unchanged.
    """
    path = Path(main)
    if not path.is_absolute() and (source / path).is_file():
        return main
    candidate = path if path.is_absolute() else Path.cwd() / path
    try:
        root, resolved = source.resolve(), candidate.resolve()
        if resolved.is_file() and resolved.is_relative_to(root):
            return resolved.relative_to(root).as_posix()
    except OSError:
        pass
    return main


def choose_input(source: Path, main: str | None) -> InputChoice:
    """Read a ``.tex`` file given as INPUT as its folder, and ``--main`` relative to INPUT.

    ``fledge check paper/main.tex`` means ``fledge check paper --main main.tex``. A
    ``--main`` path written from the current folder into INPUT, or as an absolute path
    inside it, becomes relative to INPUT.

    Args:
        source: INPUT as given.
        main: The value of ``--main``, or ``None``.

    Returns:
        The input and main file to use, with a note when a ``.tex`` INPUT was read as its
        folder.

    Raises:
        click.UsageError: INPUT is a ``.tex`` file and ``--main`` was given as well.
    """
    if source.suffix.lower() == ".tex" and source.is_file():
        # The path may come from tab completion of a file name in an untrusted project.
        shown = escape_single_line_text(str(source))
        if main is not None:
            raise click.UsageError(
                f"INPUT {shown} is a .tex file; with --main, give the project folder as INPUT."
            )
        folder = source.parent
        return InputChoice(
            folder,
            source.name,
            f"Note: {shown} is a .tex file, so INPUT is its folder "
            f"{escape_single_line_text(str(folder))} and --main is "
            f"{escape_single_line_text(source.name)}.",
        )
    if main is not None and source.is_dir():
        return InputChoice(source, _main_inside_input(source, main))
    return InputChoice(source, main)


def require_destination(*, output: Path | None, dry_run: bool) -> None:
    """Reject ``prepare`` without ``--output`` and without ``--dry-run``.

    Args:
        output: The value of ``--output``.
        dry_run: Whether ``--dry-run`` was given.

    Raises:
        click.UsageError: Neither option was given.
    """
    if output is None and not dry_run:
        raise click.UsageError(
            "prepare needs --output DIR, a new or empty folder outside INPUT for the verified "
            "copy, or --dry-run to check and plan the edits without writing one."
        )


def _runs(command: JobCommand, code: str) -> bool:
    """Return whether ``command`` can run the check ``code`` at all."""
    family = code.rstrip("0123456789")
    return family not in _FAMILIES_NOT_RUN.get(command, frozenset()) or code in (
        _RUN_DESPITE_FAMILY.get(command, frozenset())
    )


def _named(codes: list[str]) -> str:
    """Name up to ``_MAX_NAMED_CODES`` codes and count the rest."""
    shown = ", ".join(codes[:_MAX_NAMED_CODES])
    hidden = len(codes) - _MAX_NAMED_CODES
    return f"{shown} and {hidden} more" if hidden > 0 else shown


def check_selection_note(command: JobCommand, selectors: tuple[str, ...]) -> str | None:
    """Say which checks chosen with ``--select`` the command cannot run.

    Selectors that do not resolve are left to the settings validation, which names them.
    ``ALL`` chooses whatever the command runs, so it never draws a note.

    Args:
        command: The job command.
        selectors: The values of ``--select``.

    Returns:
        A warning that names the checks this command cannot run, or ``None`` when it can
        run every chosen check or no ``--select`` was given.

    Raises:
        click.UsageError: The command can run none of the chosen checks, so the run would
            pass without checking anything.
    """
    if command not in _FAMILIES_NOT_RUN or not selectors:
        return None
    try:
        resolved = resolve_selectors(selectors, "--select")
    except PreparationError:
        return None
    if "ALL" in resolved:
        return None
    chosen = list(dict.fromkeys(code for item in resolved for code in selected_codes(item)))
    not_run = [code for code in chosen if not _runs(command, code)]
    if not not_run:
        return None
    reason, alternative = _NOT_RUN_REASONS[command]
    if len(not_run) == len(chosen):
        raise click.UsageError(
            f"--select chose only checks that this command cannot run ({_named(not_run)}): "
            f"{reason}; {alternative}."
        )
    return f"Warning: {_named(not_run)} will not run: {reason}; {alternative}."
