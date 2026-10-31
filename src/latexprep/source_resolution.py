"""Turning the file name in a command into the project file TeX would read.

``resolve_reference`` follows TeX's lookup: the name is joined to each directory the
command may search, each default suffix is tried before the bare name, and the first
existing file wins. A name that is not literal, or that escapes the project, is
reported rather than guessed; an ambiguous lookup, a case-only mismatch and a missing
file are reported with the paths that were tried. It owns that lookup and the wording
of its findings. It does not walk files (``source_graph``) or judge commands
(``source_command_checks``), and it records nothing itself: the caller applies the
returned ``Resolution``.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .findings import EvidenceKind, Finding
from .path_safety import lexically_escapes
from .project_files import path_comparison_key
from .source_file_commands import (
    DEFAULT_SUFFIXES_BY_FILE_COMMAND,
    TOOLCHAIN_FILE_COMMANDS,
    is_literal_text,
)
from .source_findings import NEEDED_FOR_RESTRUCTURING, GraphFindings, listed_names
from .tex_lexer import Command

COPY_INTO_PROJECT = (
    "Copy the file into the project and refer to it by a path relative to the project folder."
)
# Commands that also look for the name from each enclosing directory, as TeX does when a
# file is read from a subdirectory that was entered by an earlier \input or \subfile.
_CONTEXT_SEARCHING_COMMANDS = frozenset(
    {"input", "include", "includegraphics", "subfile", *TOOLCHAIN_FILE_COMMANDS}
)
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


class SystemReference(NamedTuple):
    """A class, package or style reference left for the TeX toolchain to resolve.

    Attributes:
        path: The project file the reference appears in.
        command: The command that makes the reference.
        reference: The name as the command writes it.
        basename: The file name TeX looks for: ``reference`` with the command's default
            suffix when it has none.
    """

    path: str
    command: Command
    reference: str
    basename: str


@dataclass(frozen=True, slots=True)
class Reference:
    """The file name a command writes: its text, the file it appears in, and the command.

    Attributes:
        value: The file name as the command writes it.
        path: The project file the command appears in.
        command: The command that names the file.
    """

    value: str
    path: str
    command: Command


@dataclass(frozen=True, slots=True)
class LookupScope:
    r"""The directories a lookup may start from.

    Attributes:
        search: The graphics search path.
        context: The directories of the files entered so far, innermost first.
        base: The directory that overrides the compilation directory (an ``\import``
            target, a font ``Path``), if any.
        subfiles_loaded: Whether the ``subfiles`` package is in force, which changes where a
            bibliography is read from.
    """

    search: tuple[str, ...] = ()
    context: tuple[str, ...] = ()
    base: str | None = None
    subfiles_loaded: bool = False


@dataclass(frozen=True, slots=True)
class ProjectTree:
    """The project files a lookup may find, and what to do when a candidate is a link.

    Attributes:
        files: The project files by project-relative name.
        cwd: The compilation directory, relative to the project root.
        on_link: Called with the name of every linked file the lookup passes over, so the
            graph can report that it was skipped.
    """

    files: Mapping[str, Path]
    cwd: str
    on_link: Callable[[str], object]


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome of one lookup: the file found, or a toolchain reference, or neither.

    Attributes:
        target: The project file found, if any.
        system: The reference left to the toolchain, if any.
    """

    target: str | None = None
    system: SystemReference | None = None


def project_path(
    reference: Reference, tree: ProjectTree, findings: GraphFindings, *, base: str | None = None
) -> str | None:
    """Return the project-relative path ``reference`` names, under a base directory.

    Records an error and returns ``None`` for an absolute, URL-like or user-specific path
    and for one that climbs out of the project.

    Args:
        reference: The file name and the command that writes it.
        tree: The project files; its ``cwd`` is the default base directory.
        findings: Where an external path is recorded as an error.
        base: The directory the name is joined to; ``None`` uses the compilation directory.

    Returns:
        The normalized project-relative path, or ``None`` after a reported error.
    """
    value = reference.value
    if value.startswith(("/", "\\\\", "~")) or _URL_SCHEME.match(value):
        findings.error(
            "source.external_path",
            f"Dependency {value!r} uses an absolute, external, or user-specific path.",
            reference.path,
            reference.command.line,
            {"reference": value},
            suggestion=COPY_INTO_PROJECT,
        )
        return None
    path = posixpath.normpath(posixpath.join(tree.cwd if base is None else base, value))
    if lexically_escapes(path):
        findings.error(
            "source.external_path",
            f"Dependency {value!r} resolves outside the selected project.",
            reference.path,
            reference.command.line,
            {"reference": value},
            suggestion=COPY_INTO_PROJECT,
        )
        return None
    return path


def _lookup_bases(
    reference: Reference, scope: LookupScope, tree: ProjectTree, findings: GraphFindings
) -> list[str] | None:
    r"""Return every project path the name may mean, in TeX's order.

    A ``\subfile``, and a ``\bibliography`` while ``subfiles`` is loaded, start from the
    innermost entered directory unless the scope sets a base. Commands that search from
    each enclosing directory add those paths, and ``\includegraphics`` adds the graphics
    search path.

    Args:
        reference: The file name and the command that writes it.
        scope: The directories the lookup may start from.
        tree: The project files and the compilation directory.
        findings: Where an external path (an error) or a name that is not literal (an
            uncertainty) is recorded.

    Returns:
        The project paths without suffixes, or ``None`` after a reported error or
        uncertainty.
    """
    name = reference.command.name
    base = scope.base
    if (
        base is None
        and scope.context
        and (name == "subfile" or (name == "bibliography" and scope.subfiles_loaded))
    ):
        base = scope.context[0]
    initial = project_path(reference, tree, findings, base=base)
    if initial is None:
        return None
    if not is_literal_text(reference.value):
        findings.uncertain(
            "source.dynamic_path",
            f"\\{name} has a dynamic or unsupported filename expression.",
            reference.path,
            reference.command.line,
            "Make the filename literal. " + NEEDED_FOR_RESTRUCTURING,
            {"command": name},
        )
        return None
    paths = [initial]
    if base is None and name in _CONTEXT_SEARCHING_COMMANDS:
        for directory in scope.context:
            path = project_path(reference, tree, findings, base=directory)
            if path is not None:
                paths.append(path)
    if name == "includegraphics":
        for directory in scope.search:
            entry = Reference(
                posixpath.join(directory, reference.value), reference.path, reference.command
            )
            path = project_path(entry, tree, findings)
            if path is not None:
                paths.append(path)
    return paths


def candidate_paths(command_name: str, paths: list[str], suffixes: tuple[str, ...]) -> list[str]:
    r"""Return the files TeX tries for ``paths``: per path its default suffixes, then the bare name.

    ``\include`` always appends ``.tex``; a name that already has a suffix is tried as
    written. The order is TeX's lookup order: base paths first, then declared search
    directories, and the extensions a command tries within one directory.

    Args:
        command_name: The name of the command, without the backslash.
        paths: The project paths the name may mean, in TeX's order.
        suffixes: The default suffixes of the command, in the order TeX tries them.

    Returns:
        The candidate files in lookup order; repeats are kept.
    """
    candidates: list[str] = []
    for path in paths:
        if command_name == "include":
            candidates.append(path + ".tex")
        elif PurePosixPath(path).suffix:
            candidates.append(path)
        else:
            candidates.extend(path + suffix for suffix in suffixes)
            candidates.append(path)
    return candidates


def existing_files(candidates: list[str], tree: ProjectTree) -> list[str]:
    """Return the candidates that are project files, without repeats.

    Links are skipped and reported through ``tree.on_link``.

    Args:
        candidates: The candidate files in lookup order.
        tree: The project files and the callback for links.

    Returns:
        The candidates that are regular project files, in lookup order.
    """
    found: list[str] = []
    for candidate in candidates:
        if candidate not in tree.files or candidate in found:
            continue
        if tree.files[candidate].is_symlink():
            tree.on_link(candidate)
            continue
        found.append(candidate)
    return found


def _report_ambiguity(reference: Reference, found: list[str], findings: GraphFindings) -> None:
    """Note that several files match, and which one TeX's lookup order selects.

    The note is a passed heuristic warning, recorded with ``findings.add``.

    Args:
        reference: The file name and the command that writes it.
        found: The matching project files in lookup order; the first is selected.
        findings: Where the note is recorded.
    """
    findings.add(
        Finding(
            "source.ambiguous_dependency",
            f"Reference {reference.value!r} also matches {', '.join(found[1:])}; "
            f"TeX's lookup order selects {found[0]}.",
            severity="warning",
            status="passed",
            path=reference.path,
            line=reference.command.line,
            evidence_kind=EvidenceKind.HEURISTIC,
            suggestion="Name the intended file with an explicit extension "
            "to remove the dependence on lookup order.",
            details={
                "reference": reference.value,
                "selected": found[0],
                "alternatives": found[1:],
                "resolved_by": "TeX lookup order",
            },
        )
    )


def _report_missing(
    reference: Reference, candidates: list[str], tree: ProjectTree, findings: GraphFindings
) -> None:
    """Report a file that does not exist: a case-only mismatch, or one absent altogether.

    Args:
        reference: The file name and the command that writes it.
        candidates: The files the lookup tried, in order.
        tree: The project files, searched for names that differ only in case or Unicode
            normalization.
        findings: Where the error is recorded.
    """
    command = reference.command
    keys = {path_comparison_key(candidate) for candidate in candidates}
    mismatches = sorted(path for path in tree.files if path_comparison_key(path) in keys)
    if mismatches:
        findings.error(
            "source.case_mismatch",
            f"Dependency {reference.value!r} differs in case or Unicode normalization "
            f"from: {', '.join(mismatches)}.",
            reference.path,
            command.line,
            {"reference": reference.value, "candidates": mismatches, "command": command.name},
            suggestion=f"Change the reference to match {mismatches[0]!r} exactly, "
            "or rename the file.",
        )
        return
    tried = list(dict.fromkeys(candidates))
    findings.error(
        "source.missing_dependency",
        f"Literal dependency {reference.value!r} is missing from the project; "
        f"looked for {listed_names(tried)}.",
        reference.path,
        command.line,
        {"reference": reference.value, "candidates": candidates, "command": command.name},
        suggestion=f"Add the file to the project or correct the name in \\{command.name}.",
    )


def resolve_reference(
    reference: Reference, scope: LookupScope, tree: ProjectTree, findings: GraphFindings
) -> Resolution:
    """Find the project file ``reference`` names, or report why none can be chosen.

    A found file is returned as ``target``. A class, package or style that is not in the
    project and has no directory is left to the toolchain and returned as ``system``.
    Anything else is recorded in ``findings`` and returns an empty ``Resolution``.
    Several matching files are noted as an ambiguity and the first in lookup order wins.

    Args:
        reference: The file name and the command that writes it.
        scope: The directories the lookup may start from.
        tree: The project files, the compilation directory and the callback for links.
        findings: Where problems with the reference are recorded.

    Returns:
        The file found, the toolchain reference, or an empty ``Resolution``.
    """
    paths = _lookup_bases(reference, scope, tree, findings)
    if paths is None:
        return Resolution()
    command = reference.command
    suffixes = DEFAULT_SUFFIXES_BY_FILE_COMMAND[command.name]
    candidates = candidate_paths(command.name, paths, suffixes)
    found = existing_files(candidates, tree)
    if len(found) > 1:
        _report_ambiguity(reference, found, findings)
    if found:
        return Resolution(target=found[0])
    if command.name in TOOLCHAIN_FILE_COMMANDS and "/" not in reference.value:
        (suffix,) = suffixes
        value = reference.value
        basename = value if PurePosixPath(value).suffix else value + suffix
        return Resolution(system=SystemReference(reference.path, command, value, basename))
    _report_missing(reference, candidates, tree, findings)
    return Resolution()
