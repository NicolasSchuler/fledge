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
from .rules import BIBLIOGRAPHY_TRANSFORM_RULES, BY_NAME, SOURCE_TRANSFORM_RULES
from .source_graph import INCOMPLETE_GRAPH_RULES
from .text_safety import escape_single_line_text

# The families a command does not run, with exceptions for checks shared across scopes.
_FAMILIES_NOT_RUN: dict[JobCommand, frozenset[str]] = {
    JobCommand.INSPECT: frozenset({"BLD", "CMP", "FMT", "PDF"}),
    JobCommand.CHECK: frozenset({"FMT"}),
    JobCommand.BIBLIOGRAPHY: frozenset(
        {"BLD", "CHK", "CMP", "FMT", "MAN", "PDF", "PKG", "PRV", "STY", "TEX"}
    ),
    JobCommand.PDF: frozenset(
        {"BIB", "BLD", "CHK", "FMT", "MAN", "NET", "PKG", "PRV", "STY", "TEX"}
    ),
}
# Figure color space also has a source check. The bibliography command restates relevant
# source-graph errors and includes import diagnostics. PDF privacy can run without TeX.
_RUN_DESPITE_FAMILY: dict[JobCommand, frozenset[str]] = {
    JobCommand.INSPECT: frozenset({"PDF313"}),
    JobCommand.BIBLIOGRAPHY: frozenset(
        {"PKG005", *(BY_NAME[name].code for name in INCOMPLETE_GRAPH_RULES)}
    ),
    JobCommand.PDF: frozenset({"PRV002", "PRV004", "PRV105"}),
}
# A dry run stops before flattening, checking the prepared bundle and verifying the archive.
_RELEASE_CHECKS = frozenset(
    {
        "PKG001",
        "PKG002",
        "PKG003",
        "PKG004",
        "PKG006",
        "PKG007",
        "PKG008",
        "PKG101",
        "PKG102",
        "PKG103",
        "PKG104",
        "PKG105",
        "PKG201",
        "PKG301",
        "PKG303",
        "PRV102",
    }
)
_PREPARATION_CHECKS = frozenset(
    {
        *(rule.code for rule in SOURCE_TRANSFORM_RULES),
        *(rule.code for rule in BIBLIOGRAPHY_TRANSFORM_RULES),
        *(_RELEASE_CHECKS - {"PKG101", "PKG102", "PKG103", "PKG104"}),
        "PKG302",
        "PRV101",
    }
)
# PRV002 and PRV105 read only the built PDF, which inspect does not build. PRV004 also scans
# the sources, so inspect runs it.
_CODES_NOT_RUN: dict[JobCommand, frozenset[str]] = {
    JobCommand.INSPECT: _PREPARATION_CHECKS | {"PRV002", "PRV105"},
    JobCommand.CHECK: _PREPARATION_CHECKS,
    JobCommand.BIBLIOGRAPHY: frozenset(
        {"BIB109", "BIB110", *(rule.code for rule in BIBLIOGRAPHY_TRANSFORM_RULES)}
    ),
    JobCommand.PDF: frozenset({"CMP101", "PDF212"}),
}
_NOT_RUN_REASONS: dict[JobCommand, tuple[str, str]] = {
    JobCommand.INSPECT: (
        "inspect does not build the PDF or prepare a submission",
        "fledge check builds the PDF and runs them",
    ),
    JobCommand.CHECK: (
        "check does not transform or package the sources",
        "fledge prepare covers preparation and fledge fmt checks formatting",
    ),
    JobCommand.PREPARE: (
        "a dry run stops before final packaging and verification",
        "fledge prepare --output DIR verifies the package",
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


def _runs(command: JobCommand, code: str, *, dry_run: bool) -> bool:
    """Return whether ``command`` can run the check ``code`` at all."""
    family = code.rstrip("0123456789")
    available_family = family not in _FAMILIES_NOT_RUN.get(command, ()) or code in (
        _RUN_DESPITE_FAMILY.get(command, ())
    )
    return (
        available_family
        and code not in _CODES_NOT_RUN.get(command, ())
        and not (dry_run and code in _RELEASE_CHECKS)
    )


def _named(codes: list[str]) -> str:
    """Name up to ``_MAX_NAMED_CODES`` codes and count the rest."""
    shown = ", ".join(codes[:_MAX_NAMED_CODES])
    hidden = len(codes) - _MAX_NAMED_CODES
    return f"{shown} and {hidden} more" if hidden > 0 else shown


def _alternative(command: JobCommand, code: str, default: str) -> str:
    """Point to a command that can run an unavailable check."""
    if command is JobCommand.PREPARE:
        return default
    if code.startswith("FMT"):
        return "fledge fmt checks formatting"
    if code in _PREPARATION_CHECKS:
        return "fledge prepare runs the preparation checks"
    if command is JobCommand.PDF and code in _CODES_NOT_RUN[JobCommand.PDF]:
        return "fledge check on the sources runs them when configured"
    return default


def check_selection_note(
    command: JobCommand, selectors: tuple[str, ...], *, dry_run: bool = False
) -> str | None:
    """Say which checks chosen with ``--select`` the command cannot run.

    Selectors that do not resolve are left to the settings validation, which names them.
    ``ALL`` chooses whatever the command runs; additional explicit choices still draw
    a warning when they cannot run.

    Args:
        command: The job command.
        selectors: The values of ``--select``.
        dry_run: Whether preparation stops after previewing transformations.

    Returns:
        A warning that names the checks this command cannot run, or ``None`` when it can
        run every chosen check or no ``--select`` was given.

    Raises:
        click.UsageError: The command can run none of the chosen checks, so the run would
            pass without checking anything.
    """
    if command not in _NOT_RUN_REASONS or not selectors:
        return None
    try:
        resolved = resolve_selectors(selectors, "--select")
    except PreparationError:
        return None
    chosen = list(
        dict.fromkeys(code for item in resolved if item != "ALL" for code in selected_codes(item))
    )
    not_run = [code for code in chosen if not _runs(command, code, dry_run=dry_run)]
    if not not_run:
        return None
    reason, default = _NOT_RUN_REASONS[command]
    alternative = "; ".join(dict.fromkeys(_alternative(command, code, default) for code in not_run))
    if len(not_run) == len(chosen) and "ALL" not in resolved:
        raise click.UsageError(
            f"--select chose only checks that this command cannot run ({_named(not_run)}): "
            f"{reason}; {alternative}."
        )
    return f"Warning: {_named(not_run)} will not run: {reason}; {alternative}."
