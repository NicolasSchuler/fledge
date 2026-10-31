r"""Literal release metadata of the class and package files a build loaded.

A build reads classes and packages from the project and from the TeX distribution. This
module records each one's name, where it lives and the release its own header declares
(``\ProvidesPackage`` and its expl3 forms), by reading the first bytes as text. Nothing
is executed or expanded, and a header that is conditional, repeated or inside a
definition declares nothing. Which files a build read is ``build_dependencies``'s work;
policies over the result are ``build_checks``'s.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from datetime import date
from pathlib import Path
from typing import Literal, NamedTuple

from .limits import KIB
from .tex_lexer import ParseLimitExceeded, mask_literals, scan_commands

_MAX_PACKAGE_HEADER_BYTES = 32 * KIB
# A literal class or package filename, as the recorder and the kernel report it.
PACKAGE_FILENAME = re.compile(r"[A-Za-z0-9_.-]+\.(?:cls|sty)")
# LaTeX release dates are written YYYY/MM/DD or YYYY-MM-DD.
_DATE_TEXT = r"\d{4}[-/]\d{2}[-/]\d{2}"
_DECLARED_DATE = re.compile(_DATE_TEXT)
_HEADER_RELEASE = re.compile(rf"\s*({_DATE_TEXT})(?:\s+(\S+))?")
_ORDINARY_DECLARATION = re.compile(r"\\Provides(?:Package|Class)\s*\{([^{}]+)\}\s*\[([^\[\]]*)\]")
_EXPL3_DECLARATION = re.compile(
    r"\\ProvidesExpl(?:Package|Class)\s*\{([^{}]+)\}\s*\{([^{}]+)\}\s*\{([^{}]+)\}"
)

PackageOrigin = Literal["project", "toolchain"]


def parse_declared_date(value: str) -> date | None:
    """Parse a LaTeX release date such as 2025/01/31; ``None`` when absent or invalid."""
    if not _DECLARED_DATE.fullmatch(value):
        return None
    try:
        return date.fromisoformat(value.replace("/", "-"))
    except ValueError:
        return None


@dataclass(frozen=True, slots=True)
class LoadedPackage:
    """A class or package file the build read, with its literal declared release.

    Attributes:
        name: The file name, such as ``article.cls``.
        path: The path relative to the project for a project file, else the path as given.
        origin: Whether the file belongs to the project or to the TeX distribution.
        declared_date: The release date the header declares, as written, or ``None``.
        declared_version: The version that follows the date in the header, or ``None``.
    """

    name: str
    path: str
    origin: PackageOrigin
    declared_date: str | None = None
    declared_version: str | None = None


class _Release(NamedTuple):
    """The release a header declares.

    Attributes:
        date: The date as written, such as 2025/01/31.
        version: The word that follows the date, or ``None``.
    """

    date: str
    version: str | None


def read_loaded_package(path: Path, project: Path) -> LoadedPackage:
    """Record bounded literal header metadata, never execute package source.

    Args:
        path: The class or package file the build read.
        project: The project folder; a file inside it belongs to the project, any other
            file to the toolchain.

    Returns:
        The package, with the release its header declares if the header establishes one.
    """
    local = path.is_relative_to(project)
    package = LoadedPackage(
        name=path.name,
        path=path.relative_to(project).as_posix() if local else str(path),
        origin="project" if local else "toolchain",
    )
    release = _declared_release(path)
    if release is None:
        return package
    return replace(package, declared_date=release.date, declared_version=release.version)


def _declared_release(path: Path) -> _Release | None:
    """Read the release that the header of ``path`` declares, if it establishes one.

    Only the first ``_MAX_PACKAGE_HEADER_BYTES`` are read, with literals masked. The header
    must hold exactly one declaration, at top level and preceded by nothing but top-level
    ``NeedsTeXFormat`` commands, and it must name the file's own stem.

    Returns:
        The release, or ``None`` when the file cannot be read or its header establishes
        none.
    """
    try:
        with path.open("rb") as stream:
            header = stream.read(_MAX_PACKAGE_HEADER_BYTES).decode("utf-8", errors="replace")
    except OSError:
        return None
    header = mask_literals(header)
    ordinary_declarations = list(_ORDINARY_DECLARATION.finditer(header))
    expl3_declarations = list(_EXPL3_DECLARATION.finditer(header))
    declarations = [*ordinary_declarations, *expl3_declarations]
    if len(declarations) != 1:
        return None
    declaration = declarations[0]
    try:
        # Only an unconditional, top-level literal header can establish the date.
        # In particular, declarations after endinput or inside definitions cannot.
        commands = scan_commands(header[: declaration.end()])
    except ParseLimitExceeded:
        return None
    if any(
        command.depth or command.name != "NeedsTeXFormat"
        for command in commands
        if command.start < declaration.start()
    ) or not any(
        command.start == declaration.start() and command.depth == 0 for command in commands
    ):
        return None
    if declaration[1].strip() != path.stem:
        return None
    # An expl3 declaration gives its date and version as separate arguments.
    release = declaration[2] if ordinary_declarations else f"{declaration[2]} {declaration[3]}"
    found = _HEADER_RELEASE.match(release)
    return _Release(found[1], found[2]) if found else None
