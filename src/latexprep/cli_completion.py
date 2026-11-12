"""Shell completion of check codes and main files for the ``fledge`` command line.

Click completes commands, options, choices and paths by itself once a shell loads its
completion script (``_FLEDGE_COMPLETE=zsh_source fledge``, see the workflow page). This
module adds what Click cannot know: the check codes and code prefixes that ``--select``,
``--ignore``, ``fledge rule`` and ``fledge rules`` accept, and the ``.tex`` files inside
INPUT that ``--main`` may name. Completion must answer quickly and never fail, so the
search for ``.tex`` files is bounded and skips hidden folders, links and unreadable paths.
"""

from __future__ import annotations

import os
from pathlib import Path

import click
from click.shell_completion import CompletionItem

from .check_selection import SELECTORS
from .rules import BY_CODE
from .text_safety import is_unsafe_character

# The most entries the search for .tex files looks at, and how deep it goes below INPUT.
_MAX_SCANNED_ENTRIES = 2000
_MAX_FOLDER_LEVELS = 4


def _selector_items(incomplete: str, *, codes_only: bool) -> list[CompletionItem]:
    """Return the selectors that start with ``incomplete``, compared in upper case.

    Args:
        incomplete: What the user typed so far.
        codes_only: Whether to offer only complete check codes, not prefixes or ``ALL``.

    Returns:
        The matching selectors in sorted order; a check code carries its title as help.
    """
    typed = incomplete.upper()
    candidates = BY_CODE if codes_only else SELECTORS
    return [
        CompletionItem(item, help=BY_CODE[item].title if item in BY_CODE else None)
        for item in sorted(candidates)
        if item.startswith(typed)
    ]


def complete_check_selectors(
    _context: click.Context, _parameter: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Complete a check selector: ``ALL``, a check code or a code prefix.

    Args:
        _context: The context of the command being completed, unused.
        _parameter: The option or argument being completed, unused.
        incomplete: What the user typed so far.

    Returns:
        The matching selectors.
    """
    return _selector_items(incomplete, codes_only=False)


def complete_check_codes(
    _context: click.Context, _parameter: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Complete a check code, as ``fledge rule CODE`` takes it.

    Args:
        _context: The context of the command being completed, unused.
        _parameter: The argument being completed, unused.
        incomplete: What the user typed so far.

    Returns:
        The matching check codes.
    """
    return _selector_items(incomplete, codes_only=True)


def _tex_files(root: Path) -> list[str]:
    """List the ``.tex`` files below the folder ``root`` as relative POSIX paths.

    The walk visits at most ``_MAX_SCANNED_ENTRIES`` entries and ``_MAX_FOLDER_LEVELS``
    folder levels, and skips hidden entries, links and unreadable folders. A name with a
    control, format or other unsafe character is left out: project names are untrusted,
    and a line break would split one completion into two.
    """
    found: list[str] = []
    scanned = 0
    for folder, folders, files in os.walk(root, onerror=lambda _error: None):
        relative = Path(folder).relative_to(root)
        if len(relative.parts) >= _MAX_FOLDER_LEVELS:
            folders.clear()
        folders[:] = sorted(
            name
            for name in folders
            if not name.startswith(".") and not (Path(folder) / name).is_symlink()
        )
        for name in sorted(files):
            scanned += 1
            relative_name = (relative / name).as_posix()
            if (
                name.lower().endswith(".tex")
                and not name.startswith(".")
                and not any(map(is_unsafe_character, relative_name))
                and not (Path(folder) / name).is_symlink()
            ):
                found.append(relative_name)
        scanned += len(folders)
        if scanned >= _MAX_SCANNED_ENTRIES:
            break
    return found


def complete_main_files(
    context: click.Context, _parameter: click.Parameter, incomplete: str
) -> list[CompletionItem]:
    """Complete ``--main`` with the ``.tex`` files inside INPUT, relative to it.

    INPUT is the input already typed on the command line, or the current folder when none
    was typed yet. A ZIP input offers nothing, since its files are not on disk.

    Args:
        context: The context of the command being completed. INPUT is its ``source``
            parameter, or its first unparsed argument: while ``--main`` still lacks its
            value, Click leaves the arguments unparsed.
        _parameter: The option being completed, unused.
        incomplete: What the user typed so far.

    Returns:
        The matching files.
    """
    source = context.params.get("source") or next(iter(context.args), None)
    root = Path(source) if source is not None else Path.cwd()
    try:
        if not root.is_dir():
            return []
        return [CompletionItem(name) for name in _tex_files(root) if name.startswith(incomplete)]
    except OSError:
        return []
