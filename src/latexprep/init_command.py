r"""`fledge init`: write a starter or preset fledge.toml without prompting.

The new file names the main file when the project has exactly one file that declares
``\documentclass``, and lists the candidates, commented out, when it has several.
"""

from __future__ import annotations

import json
import os
import shlex
from pathlib import Path
from typing import NamedTuple

import click

from .cli_options import FledgeCommand
from .command_options import debug_option
from .config import Settings, parse_settings_text
from .config_discovery import resolve_config_path
from .errors import PreparationError
from .import_budget import ImportLimits
from .main_selection import candidate_mains
from .presets import PRESETS, preset_names, preset_text
from .report import Outcome

CONFIG_NAME = "fledge.toml"
# How many candidate main files the settings file lists before it counts the rest.
MAX_LISTED_CANDIDATES = 20

_STARTER_HEAD = """\
# Fledge project settings. Every value is a constraint you supply for this
# submission; Fledge maintains no venue rules. Uncomment only what applies.
# `fledge rules` lists check codes; `fledge init --list` shows example presets.

"""
_MAIN_PLACEHOLDER = """\
# Relative path of the document to build when the project has several roots.
# main = "main.tex"
"""
_STARTER_TAIL = """\
# PDF001: total pages, including references and appendices. Replace 0 with your limit.
# max_pages = 0

[checks]
select = ["ALL"] # Codes, families such as "TEX", or prefixes such as "BIB1".
ignore = [] # For example ["TEX001"] to skip unfinished-text markers.
"""
# The starter file of a project in which no main file was found.
STARTER = _STARTER_HEAD + _MAIN_PLACEHOLDER + _STARTER_TAIL

INIT_EXAMPLES = (
    ("fledge init paper", "Write paper/fledge.toml, naming the main file it finds."),
    ("fledge init paper --preset arxiv", "Start from the arxiv preset instead."),
    ("fledge init --list", "Describe the presets."),
)


class MainSetting(NamedTuple):
    """The main-file lines of a new settings file and what to tell the user about them.

    Attributes:
        lines: The TOML lines, ending with a newline: ``main = "..."`` for the only
            candidate, the commented candidates when there are several, or ``None`` when
            no candidate was found.
        note: One line for the user, or ``None``.
    """

    lines: str | None
    note: str | None


def _toml_string(value: str) -> str:
    """Write ``value`` as a TOML basic string; JSON escapes are valid TOML escapes."""
    return json.dumps(value, ensure_ascii=False)


def _main_setting(directory: Path) -> MainSetting:
    """Find the main file of the project in ``directory`` and write its settings lines.

    The project is read as a job reads it, within the default import limits; a project
    that cannot be read that way gets no main file and a note that says why.

    Args:
        directory: The project folder.

    Returns:
        The lines and the note.
    """
    try:
        candidates = candidate_mains(directory, ImportLimits.from_settings(Settings()))
    except (PreparationError, OSError) as error:
        return MainSetting(None, f"Could not look for the main file ({error}); set main by hand.")
    if not candidates:
        return MainSetting(
            None, "No file declares \\documentclass yet; main is left commented out."
        )
    if len(candidates) == 1:
        lines = (
            "# The document to build: the only file that declares \\documentclass.\n"
            f"main = {_toml_string(candidates[0])}\n"
        )
        return MainSetting(lines, f"Main file: {candidates[0]}.")
    listed = candidates[:MAX_LISTED_CANDIDATES]
    lines = "# Several files declare \\documentclass; uncomment the main file to build:\n"
    lines += "".join(f"# main = {_toml_string(name)}\n" for name in listed)
    if len(candidates) > len(listed):
        lines += f"# ... and {len(candidates) - len(listed)} more.\n"
    return MainSetting(
        lines,
        f"Found {len(candidates)} candidate main files ({', '.join(listed[:3])}"
        f"{', ...' if len(candidates) > 3 else ''}); uncomment one main line in {CONFIG_NAME}.",
    )


def settings_text(directory: Path, preset: str | None) -> tuple[str, str | None]:
    """Compose the new settings file for the project in ``directory``.

    Args:
        directory: The project folder.
        preset: The preset to start from, or ``None`` for the minimal starter.

    Returns:
        The settings text and a note about the main file, or ``None``.
    """
    main = _main_setting(directory)
    if preset is None:
        return _STARTER_HEAD + (main.lines or _MAIN_PLACEHOLDER) + _STARTER_TAIL, main.note
    header = f"# Written by `fledge init --preset {preset}`.\n"
    if main.lines is not None:
        header += main.lines + "\n"
    return header + preset_text(preset), main.note


class InitError(click.ClickException):
    """A refusal of ``fledge init`` to write the settings file.

    Attributes:
        exit_code: The process exit code when the error ends the command.
    """

    # The CLI's "error" outcome; usage errors use the CLI's usage exit code.
    exit_code = Outcome.ERROR.exit_code


def _print_presets() -> None:
    """Print the presets with their descriptions, and how to copy or apply one."""
    width = max(len(name) for name in PRESETS)
    click.echo("Presets (starting points, not certifications of venue compliance):")
    for name in preset_names():
        click.echo(f"  {name.ljust(width)}  {PRESETS[name]}")
    click.echo("")
    click.echo("Copy one for editing:     fledge init --preset NAME")
    click.echo("Apply one beneath config: fledge check INPUT --preset NAME")


def _nearest_config(directory: Path) -> Path | None:
    """Find the settings file that applies in ``directory``, or ``None`` when there is none.

    A search that fails with ``PreparationError`` also gives ``None``.
    """
    try:
        return resolve_config_path(directory)
    except PreparationError:
        return None


def _next_steps(directory: Path) -> list[str]:
    """Suggest the commands to run next, as indented lines.

    Args:
        directory: The directory that received the settings file; it is written as ``.``
            when it is the current directory.

    Returns:
        A ``fledge check`` line and a ``fledge prepare`` line whose output is a sibling
        ``<name>-submission`` folder, with shell-quoted paths.
    """
    resolved = directory.resolve()
    source = "." if resolved == Path.cwd().resolve() else str(directory)
    output = os.path.relpath(resolved.parent / f"{resolved.name}-submission")
    return [
        f"  fledge check {shlex.quote(source)}",
        f"  fledge prepare {shlex.quote(source)} --output {shlex.quote(output)}",
    ]


def _check_target(target: Path, *, force: bool) -> None:
    """Refuse to write through a link or special file, or over a file without ``--force``.

    Args:
        target: The settings file to write.
        force: Whether an existing regular file may be overwritten.

    Raises:
        InitError: The target is a link or not a regular file, or it exists and ``force``
            is false.
    """
    if target.is_symlink() or (target.exists() and not target.is_file()):
        raise InitError(f"{target} is not a regular file; refusing to write through it")
    if target.exists() and not force:
        raise InitError(f"{target} already exists; pass --force to overwrite it")


def _write_settings(target: Path, text: str, *, force: bool) -> None:
    """Validate ``text`` as settings, then write it; an invalid file is never written.

    Args:
        target: The settings file to write.
        text: The settings text.
        force: Whether to overwrite an existing file; otherwise the file must not exist.

    Raises:
        InitError: The text is not valid settings, or writing failed.
    """
    # Validate before writing, so --force never replaces a file with an invalid one.
    try:
        parse_settings_text(text, target)
    except PreparationError as error:
        raise InitError(f"Cannot write a valid {target}: {error}") from error
    try:
        with target.open("w" if force else "x", encoding="utf-8") as stream:
            stream.write(text)
    except OSError as error:
        raise InitError(f"Cannot write a valid {target}: {error}") from error


def _announce_precedence(target: Path, previous: Path | None, current: Path | None) -> None:
    """Warn when another settings file wins here, or note which one this file now outranks.

    Args:
        target: The settings file that was written.
        previous: The settings file that applied before the write, or ``None``.
        current: The settings file that applies after the write, or ``None``.
    """
    if current is not None and current != target.resolve():
        click.echo(
            f"Warning: {current} takes precedence over {CONFIG_NAME} here; "
            f"move or merge it, or pass --config {target}.",
            err=True,
        )
    elif previous is not None and previous != target.resolve():
        click.echo(f"Note: this file now takes precedence over {previous}.")


@click.command(
    "init",
    cls=FledgeCommand,
    short_help="Write a starter fledge.toml settings file.",
    examples=INIT_EXAMPLES,
)
@click.argument(
    "directory",
    required=False,
    default=".",
    metavar="[DIRECTORY]",
    type=click.Path(exists=True, file_okay=False, path_type=Path),
)
@click.option(
    "--preset",
    type=click.Choice(preset_names()),
    help="Start from an example preset instead of the minimal starter file.",
)
@click.option("--force", is_flag=True, help="Overwrite an existing fledge.toml.")
@click.option("--list", "list_presets", is_flag=True, help="List the presets and exit.")
@debug_option
def init_command(directory: Path, preset: str | None, *, force: bool, list_presets: bool) -> int:
    r"""Write fledge.toml into DIRECTORY (default: the current directory).

    The file names the main file when exactly one file declares \documentclass,
    and lists the candidates for you to choose from when several do. Presets
    switch on generic checks and leave venue values, such as page limits and
    identity terms, as commented placeholders for you to fill in. They are
    starting points, not certifications of compliance. An existing fledge.toml
    is kept unless --force is given.
    """
    if list_presets:
        if preset is not None or force:
            raise click.UsageError("--list cannot be combined with --preset or --force")
        _print_presets()
        return 0
    target = directory / CONFIG_NAME
    _check_target(target, force=force)
    text, main_note = settings_text(directory, preset)
    previous = _nearest_config(directory)
    _write_settings(target, text, force=force)
    origin = f" from the {preset} preset" if preset is not None else ""
    click.echo(f"Wrote {target}{origin}.")
    if main_note is not None:
        click.echo(main_note)
    _announce_precedence(target, previous, _nearest_config(directory))
    if preset is not None:
        click.echo(
            "Fill in the commented placeholders from your venue's current instructions; "
            "the preset does not certify compliance."
        )
    else:
        click.echo("Uncomment the settings that apply to your submission.")
    click.echo("Next steps:")
    for line in _next_steps(directory):
        click.echo(line)
    return 0
