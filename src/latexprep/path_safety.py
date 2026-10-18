"""Relative-path validation and containment checks for paths from settings, archives and tools.

Two families, kept apart because only the second touches the file system.

Validation looks at text only. ``relative_path_problem`` and ``is_relative_posix_path``
decide whether a string may name a file inside a project, with explicit options for
the places that differ: whether redundant ``.`` and empty segments are tolerated
(``normalized``), whether the project root itself may be named (``allow_current``),
how many unusual characters are refused (``characters``) and an optional whitelist
for each segment (``component_pattern``). Some rules never vary: a path is
non-empty, relative, free of ``..``, backslashes, NUL and a Windows drive prefix.
Project import already refuses such names, so no staged project holds one.

Containment checks the file system. ``is_contained`` resolves links and asks whether
the result stays inside a root, ``is_link_free_regular_file`` is the stricter variant
that refuses a link anywhere between the root and the file, and
``contained_relative_name`` reports where a path lies under a root.
"""

from __future__ import annotations

import re
import stat
from pathlib import Path
from typing import Literal

from .text_safety import is_unsafe_character

# How unusual characters in a path are treated; each level includes the previous one.
# NUL is refused at every level.
#   "any"             nothing further
#   "no_line_breaks"  also refuses carriage return and line feed
#   "no_controls"     also refuses every ASCII control character
#   "literal"         also refuses ":" and any Unicode control, format, surrogate,
#                     private-use or unassigned character, so the text is safe to
#                     show, link or put in a line-oriented report
PathCharacters = Literal["any", "no_line_breaks", "no_controls", "literal"]

# Why a path was refused; ``None`` from ``relative_path_problem`` means it is accepted.
RelativePathProblem = Literal[
    "empty",
    "absolute",
    "backslash",
    "characters",
    "traversal",
    "current",
    "unnormalized",
    "drive",
    "component",
]

_DRIVE_PREFIX = re.compile(r"[A-Za-z]:")


def has_drive_prefix(value: str) -> bool:
    """Whether ``value`` starts with a Windows drive such as ``C:``, which is absolute there."""
    return _DRIVE_PREFIX.match(value) is not None


def lexically_escapes(normalized: str) -> bool:
    """Whether a path already normalized with ``posixpath.normpath`` climbs out of its base."""
    return normalized == ".." or normalized.startswith("../")


def _has_unwanted_characters(value: str, characters: PathCharacters) -> bool:
    if characters == "any":
        return False
    if characters == "no_line_breaks":
        return "\r" in value or "\n" in value
    if characters == "no_controls":
        return any(ord(char) < 32 for char in value)
    return ":" in value or any(map(is_unsafe_character, value))


def relative_path_problem(
    value: str,
    *,
    normalized: bool = True,
    allow_current: bool = False,
    characters: PathCharacters = "any",
    component_pattern: re.Pattern[str] | None = None,
) -> RelativePathProblem | None:
    """Why ``value`` cannot name a file inside a project, or ``None`` when it can.

    ``normalized`` requires the canonical spelling: no empty or ``.`` segment and no
    trailing slash, so ``a//b``, ``./a`` and ``a/`` are refused. Without it those are
    tolerated, as they are in paths a user types or a tool reports.
    ``allow_current`` accepts a path naming the project root itself (``.``).
    ``characters`` sets how unusual characters are treated (see ``PathCharacters``).
    ``component_pattern`` must match every segment completely.
    """
    if not value:
        return "empty"
    if value.startswith("/"):
        return "absolute"
    if "\\" in value:
        return "backslash"
    if "\x00" in value:
        return "characters"
    segments = value.split("/")
    if ".." in segments:
        return "traversal"
    names = [segment for segment in segments if segment not in {"", "."}]
    if not names:
        if normalized and value != ".":
            return "unnormalized"
        return None if allow_current else "current"
    if normalized and len(names) != len(segments):
        return "unnormalized"
    if has_drive_prefix(names[0]):
        return "drive"
    if _has_unwanted_characters(value, characters):
        return "characters"
    if component_pattern is not None and not all(component_pattern.fullmatch(n) for n in names):
        return "component"
    return None


def is_relative_posix_path(
    value: str,
    *,
    normalized: bool = True,
    allow_current: bool = False,
    characters: PathCharacters = "any",
    component_pattern: re.Pattern[str] | None = None,
) -> bool:
    """Whether ``value`` may name a file inside a project; see ``relative_path_problem``."""
    return (
        relative_path_problem(
            value,
            normalized=normalized,
            allow_current=allow_current,
            characters=characters,
            component_pattern=component_pattern,
        )
        is None
    )


def is_contained(root: Path, path: Path, *, regular_file: bool = False) -> bool:
    """Whether ``path`` is not itself a link and resolves to a place inside ``root``.

    Links in the parent directories are followed, so one that leads out of ``root``
    is refused and one that stays inside is accepted. With ``regular_file`` the
    path must also be a regular file. A path that does not exist is contained when
    it would lie inside ``root``, unless ``regular_file`` is set.
    """
    if path.is_symlink():
        return False
    if regular_file and not path.is_file():
        return False
    return path.resolve().is_relative_to(root.resolve())


def is_link_free_regular_file(root: Path, path: Path) -> bool:
    """Whether ``path``, spelled under ``root``, is a regular file reached without any link.

    Stricter than ``is_contained``: nothing is resolved first, so every directory
    from ``root`` down to the file is examined as written, and ``root`` itself and
    every directory between it and the file must not be a link, even one that
    leads somewhere inside ``root``.
    """
    if not path.is_relative_to(root):
        return False
    try:
        return (
            stat.S_ISREG(path.lstat().st_mode)
            and not root.is_symlink()
            and all(not part.is_symlink() for part in path.parents if part.is_relative_to(root))
        )
    except OSError:
        return False


def contained_relative_name(root: Path, path: Path) -> str | None:
    """The POSIX name of ``path`` under ``root`` after resolving links, or ``None`` if outside."""
    resolved, base = path.resolve(), root.resolve()
    return resolved.relative_to(base).as_posix() if resolved.is_relative_to(base) else None
