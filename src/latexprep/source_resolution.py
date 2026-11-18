r"""Turning the file name in a command into the project file TeX would read.

``resolve_reference`` follows TeX's lookup: the name is joined to each directory the
command may search, and the first file the command would read wins.
A name that is not literal, or that escapes the project, is
reported rather than guessed; an ambiguous lookup, a case-only mismatch and a missing
file are reported with the paths that were tried. It owns that lookup and the wording
of its findings. It does not walk files (``source_graph``) or judge commands
(``source_command_checks``), and it records nothing itself: the caller applies the
returned ``Resolution``.

A bare ``\input{name}``, ``\bibliography{name}`` or ``\includegraphics{name}`` that the
project lacks may name a file of the TeX installation, such as ``glyphtounicode.tex``,
IEEE's ``IEEEabrv.bib`` or the sample figure ``example-image-a.pdf``. The job
asks the installation once about such names (``tex_installation``) and makes the answer
current with :func:`installed_inputs_known`. When the installation could not be asked,
such a name is reported as not checked rather than missing; without a question, as
outside a job, it is reported missing.

A missing file is a confirmed missing dependency only where the build needs it. A
bibliography database is not needed when the ``.bbl`` the build reads is shipped beside
the main file, and a reference in a macro definition or conditional of a class or package
file runs only if that code runs, which the scan cannot decide.
"""

from __future__ import annotations

import posixpath
import re
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .cancellation import cancellation_point
from .file_safety import read_file_prefix
from .findings import EvidenceKind, Finding
from .path_safety import lexically_escapes
from .project_files import is_parsed_source, path_comparison_key, project_files
from .source_file_commands import (
    BIBLIOGRAPHY_RESOURCE_COMMANDS,
    COMMA_LIST_FILE_COMMANDS,
    DEFAULT_SUFFIXES_BY_FILE_COMMAND,
    GRAPHICS_SUFFIXES,
    INSTALLATION_INPUT_COMMANDS,
    TOOLCHAIN_FILE_COMMANDS,
    candidate_paths,
    is_literal_text,
)
from .source_findings import NEEDED_FOR_RESTRUCTURING, GraphFindings, listed_names
from .source_loading import LoadBudget, load_source
from .tex_lexer import Command

COPY_INTO_PROJECT = (
    "Copy the file into the project and refer to it by a path relative to the project folder."
)
# A file name the TeX installation may hold: letters, digits and ``.-_+``, no directory.
_INSTALLATION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,127}")
# The suffixes asked for a figure name without one; graphicx would try more, but the
# installation's figures, such as mwe's example-image-a, come in these.
_INSTALLED_GRAPHICS_SUFFIXES = (".pdf", ".png", ".jpg", ".eps")


@dataclass(frozen=True, slots=True)
class InstalledInputs:
    """What the TeX installation said about bare input names that the project lacks.

    Attributes:
        asked: The file names the installation was asked about, each with its suffix.
        found: The installation path of each asked name that the installation holds.
        identical: The project's class and package files whose bytes equal the
            installation's copy of the same name, by project path. TeX reads the same
            text from either, so they are analysed as the installation's files are: not
            as project source.
        answered: Whether the installation answered. ``False`` when it could not be asked,
            as without ``kpsewhich`` on the search path; ``asked`` and ``found`` are then
            empty, and a bare name the installation may hold is neither found nor missing.
    """

    asked: frozenset[str]
    found: Mapping[str, str]
    identical: frozenset[str] = frozenset()
    answered: bool = True


_installed_inputs: ContextVar[InstalledInputs | None] = ContextVar(
    "latexprep_installed_inputs", default=None
)


@contextmanager
def installed_inputs_known(answer: InstalledInputs | None) -> Generator[None]:
    """Make ``answer`` the installation's answer for the source graphs built inside.

    Args:
        answer: What the installation said, or ``None`` when it was not asked.

    Yields:
        Nothing; the answer applies until the block ends.
    """
    token = _installed_inputs.set(answer)
    try:
        yield
    finally:
        _installed_inputs.reset(token)


def installation_file_names(reference: str, command: str = "input") -> tuple[str, ...]:
    r"""Return the file names a bare ``\input``, ``\bibliography`` or figure name may have.

    Args:
        reference: The name as the command writes it.
        command: ``input``, ``bibliography`` or ``includegraphics``, without the backslash.

    Returns:
        For ``\input``, the name with ``.tex`` when it has no suffix; for
        ``\bibliography``, the name with ``.bib`` unless it ends with it, as BibTeX reads
        it; for a figure without a graphics suffix, the name with each suffix that TeX
        Live's sample figures such as ``example-image`` come in. Nothing when the
        reference has a directory or characters no installation file name has.
    """
    value = reference.strip()
    if not _INSTALLATION_NAME.fullmatch(value) or value.endswith("."):
        return ()
    if command == "bibliography":
        return tuple(candidate_paths(command, [value], DEFAULT_SUFFIXES_BY_FILE_COMMAND[command]))
    if command == "includegraphics":
        if PurePosixPath(value).suffix.lower() in GRAPHICS_SUFFIXES:
            return (value,)
        return tuple(value + suffix for suffix in _INSTALLED_GRAPHICS_SUFFIXES)
    return (value if PurePosixPath(value).suffix else value + ".tex",)


def installation_candidates(root: Path) -> list[str]:
    r"""Return the bare input, bibliography and figure names that no project file has.

    Only these may be files of the TeX installation. The sources are lexed, not walked:
    the analysis builds the one source graph later, with the installation's answer. A
    name that some project file has is left to that graph's lookup.

    Args:
        root: The project folder.

    Returns:
        The distinct file names, with their suffixes, in project order; figure names,
        each under several suffixes, after the input and bibliography names.
    """
    files = project_files(root)
    project_names = {PurePosixPath(name).name for name in files}
    budget, ignored = LoadBudget(), GraphFindings([], [])
    names: dict[str, None] = {}
    # Figures come last: each is asked under several suffixes, and the lookup is bounded.
    figures: dict[str, None] = {}
    for name, path in files.items():
        cancellation_point()
        source = load_source(name, path, budget, ignored) if is_parsed_source(name, path) else None
        for command in source.commands if source else ():
            if (
                command.name not in INSTALLATION_INPUT_COMMANDS
                or command.named
                or not command.arguments
            ):
                continue
            argument = command.arguments[0].value
            values = argument.split(",") if command.name in COMMA_LIST_FILE_COMMANDS else [argument]
            for value in (item.strip() for item in values):
                file_names = installation_file_names(value, command.name)
                if file_names and not {value, *file_names} & project_names:
                    found = figures if command.name == "includegraphics" else names
                    found.update(dict.fromkeys(file_names))
    return [*names, *(name for name in figures if name not in names)]


def installed_copy(path: str) -> bool:
    """Return whether a project file is a byte-identical copy of the installation's file.

    Args:
        path: The project path of a class or package file.

    Returns:
        ``True`` when the installation was asked and its copy has the same bytes.
    """
    installed = _installed_inputs.get()
    return installed is not None and path in installed.identical


def package_code_candidates(root: Path) -> dict[str, list[str]]:
    """Return the project's class and package files by file name, as the installation names them.

    Args:
        root: The project folder.

    Returns:
        The project paths of each ``.cls`` and ``.sty`` file name, in project order.
    """
    found: dict[str, list[str]] = {}
    for name in project_files(root):
        file_name = PurePosixPath(name).name
        if PurePosixPath(name).suffix in {".cls", ".sty"} and _INSTALLATION_NAME.fullmatch(
            file_name
        ):
            found.setdefault(file_name, []).append(name)
    return found


def installation_query(root: Path) -> tuple[list[str], dict[str, list[str]]]:
    """Return what to ask the TeX installation: missing input names and the project's classes.

    Args:
        root: The project folder.

    Returns:
        The ``installation_candidates`` and the ``package_code_candidates``.
    """
    return installation_candidates(root), package_code_candidates(root)


# Commands that also look for the name from each enclosing directory, as TeX does when a
# file is read from a subdirectory that was entered by an earlier \input or \subfile.
_CONTEXT_SEARCHING_COMMANDS = frozenset(
    {"input", "include", "includegraphics", "subfile", *TOOLCHAIN_FILE_COMMANDS}
)
_URL_SCHEME = re.compile(r"[A-Za-z][A-Za-z0-9+.-]*:")


class SystemReference(NamedTuple):
    r"""A reference left for the TeX toolchain to resolve from its installation.

    Classes, packages and styles are left to the toolchain unasked; an ``\input`` or
    ``\bibliography`` name only once the installation said that it holds the file.

    Attributes:
        path: The project file the reference appears in.
        command: The command that makes the reference.
        reference: The name as the command writes it.
        basename: The file name TeX looks for: ``reference`` with the command's default
            suffix when it has none.
        installed: The installation path of the file, when the installation was asked.
    """

    path: str
    command: Command
    reference: str
    basename: str
    installed: str | None = None


@dataclass(frozen=True, slots=True)
class Reference:
    """The file name a command writes: its text, the file it appears in, and the command.

    Attributes:
        value: The file name as the command writes it.
        path: The project file the command appears in.
        command: The command that names the file.
        deferred: Whether the command sits in a class or package file inside a macro
            definition or a conditional, where it runs only if the macro is used or the
            condition holds.
    """

    value: str
    path: str
    command: Command
    deferred: bool = False


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
        bibliography_output: The ``.bbl`` the build reads for a bibliography: the main
            file's name with ``.bbl``, beside it. ``None`` when no main file is selected.
    """

    files: Mapping[str, Path]
    cwd: str
    on_link: Callable[[str], object]
    bibliography_output: str | None = None


@dataclass(frozen=True, slots=True)
class Resolution:
    """The outcome of one lookup: the file found, or a toolchain reference, or neither.

    Attributes:
        target: The project file found, if any.
        system: The reference left to the toolchain, if any.
        shipped_bbl: The shipped ``.bbl`` the build reads in place of a bibliography
            database the project lacks, if any.
    """

    target: str | None = None
    system: SystemReference | None = None
    shipped_bbl: str | None = None


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


def _report_case_mismatch(
    reference: Reference, candidates: list[str], tree: ProjectTree, findings: GraphFindings
) -> bool:
    """Report project files whose names differ from a candidate only in case or normalization.

    Args:
        reference: The file name and the command that writes it.
        candidates: The files the lookup tried, in order.
        tree: The project files, searched for names that differ only in case or Unicode
            normalization.
        findings: Where the error is recorded.

    Returns:
        Whether such a file exists and was reported.
    """
    command = reference.command
    keys = {path_comparison_key(candidate) for candidate in candidates}
    mismatches = sorted(path for path in tree.files if path_comparison_key(path) in keys)
    if not mismatches:
        return False
    findings.error(
        "source.case_mismatch",
        f"Dependency {reference.value!r} differs in case or Unicode normalization "
        f"from: {', '.join(mismatches)}.",
        reference.path,
        command.line,
        {"reference": reference.value, "candidates": mismatches, "command": command.name},
        suggestion=f"Change the reference to match {mismatches[0]!r} exactly, or rename the file.",
    )
    return True


def _report_missing(
    reference: Reference,
    candidates: list[str],
    findings: GraphFindings,
    *,
    installation_asked: bool = False,
) -> None:
    """Report a file that exists neither in the project nor, if asked, in the installation.

    Args:
        reference: The file name and the command that writes it.
        candidates: The files the lookup tried, in order.
        findings: Where the error is recorded.
        installation_asked: Whether the TeX installation was asked for the name and lacks
            it too.
    """
    command = reference.command
    tried = list(dict.fromkeys(candidates))
    findings.error(
        "source.missing_dependency",
        f"Literal dependency {reference.value!r} is missing from the project"
        f"{' and from the TeX installation' if installation_asked else ''}; "
        f"looked for {listed_names(tried)}.",
        reference.path,
        command.line,
        {"reference": reference.value, "candidates": candidates, "command": command.name},
        suggestion=f"Add the file to the project or correct the name in \\{command.name}.",
    )


# How much of a shipped .bbl is read to see that it holds entries, and what an entry
# looks like: BibTeX styles write \bibitem (harvard \harvarditem, amsrefs \bib), and
# biblatex writes \entry inside a \datalist.
_BBL_PREFIX_BYTES = 256 * 1024
_BIBLATEX_ENTRY = re.compile(rb"\\entry\s*\{")
_BBL_ENTRY = re.compile(rb"\\(?:bibitem|harvarditem|bib)(?![A-Za-z@])|" + _BIBLATEX_ENTRY.pattern)


def _shipped_bibliography(reference: Reference, tree: ProjectTree) -> str | None:
    r"""Return the shipped ``.bbl`` that the build reads in place of a missing database.

    LaTeX reads the bibliography of ``\bibliography`` and biblatex's resources from the
    ``.bbl`` named after the main file, in its directory. Without the database the build
    runs no BibTeX or Biber and reads that file as it is, and arXiv gives an uploaded one
    precedence over its own run. A ``.bbl`` of another name or folder belongs to another
    document, and one without entries, or one in BibTeX's format beside a biblatex
    resource, is not a bibliography the build can use.

    Args:
        reference: The file name and the command that writes it.
        tree: The project files and the ``.bbl`` the build reads.

    Returns:
        The project path of the ``.bbl``, or ``None`` for another command or when it is
        not shipped as a regular file that holds entries.
    """
    bbl = tree.bibliography_output
    if (
        bbl is None
        or reference.command.name not in BIBLIOGRAPHY_RESOURCE_COMMANDS
        or bbl not in tree.files
    ):
        return None
    try:
        head = read_file_prefix(tree.files[bbl], _BBL_PREFIX_BYTES)
    except OSError:
        return None  # a link, a special file or one that cannot be read
    entries = _BIBLATEX_ENTRY if reference.command.name == "addbibresource" else _BBL_ENTRY
    return bbl if entries.search(head) else None


def _note_shipped_bibliography(
    reference: Reference, candidates: list[str], bbl: str, findings: GraphFindings
) -> None:
    """Note that a bibliography database is not needed because its ``.bbl`` is shipped.

    The note is a passed result of the missing-dependency check, recorded with
    ``findings.add``.

    Args:
        reference: The file name and the command that writes it.
        candidates: The files the lookup tried, in order; the first names the database.
        bbl: The shipped ``.bbl``.
        findings: Where the note is recorded.
    """
    database = PurePosixPath(candidates[0]).name if candidates else reference.value
    findings.add(
        Finding(
            "source.missing_dependency",
            f"{database} is not in the project; the build reads the shipped {bbl} in its "
            "place, so the bibliography database is not needed.",
            severity="info",
            status="passed",
            path=reference.path,
            line=reference.command.line,
            details={
                "reference": reference.value,
                "command": reference.command.name,
                "shipped_bbl": bbl,
            },
        )
    )


def _report_unconfirmed(
    reference: Reference, candidates: list[str], findings: GraphFindings, *, cause: str
) -> None:
    """Record a file the project lacks whose absence the scan cannot confirm.

    The finding is an inconclusive warning, so it does not block.

    Args:
        reference: The file name and the command that writes it.
        candidates: The files the lookup tried, in order.
        findings: Where the uncertainty is recorded.
        cause: ``installation`` when the TeX installation could not be asked about a bare
            name, ``deferred`` for a reference in package code that may never run.
    """
    command = reference.command
    tried = listed_names(list(dict.fromkeys(candidates)))
    if cause == "installation":
        message = (
            f"Literal dependency {reference.value!r} is not in the project, and the TeX "
            f"installation could not be asked whether it holds it; looked for {tried}."
        )
        suggestion = (
            "Run Fledge where the TeX installation's kpsewhich is on the search path, or "
            f"add the file to the project or correct the name in \\{command.name}."
        )
    else:
        message = (
            f"Literal dependency {reference.value!r} is missing from the project; it is "
            "named in a macro definition or conditional of package code, which runs only "
            f"if the macro is used or the condition holds; looked for {tried}."
        )
        suggestion = "If the document runs this code, add the file to the project."
    findings.uncertain(
        "source.missing_dependency",
        message,
        reference.path,
        command.line,
        suggestion,
        {"reference": reference.value, "candidates": candidates, "command": command.name},
    )


def _resolve_outside_project(
    reference: Reference, candidates: list[str], tree: ProjectTree, findings: GraphFindings
) -> Resolution:
    """Resolve a reference that no project file satisfies, or report why it is missing.

    A bare name the TeX installation holds is a toolchain reference. A case-only mismatch
    is an error. A bibliography database whose ``.bbl`` is shipped is not needed. A bare
    name the installation could not be asked about, and a reference in package code that
    may never run, are not confirmed missing. Anything else is a missing dependency.

    Args:
        reference: The file name and the command that writes it.
        candidates: The files the lookup tried, in order.
        tree: The project files and the ``.bbl`` the build reads.
        findings: Where problems with the reference are recorded.

    Returns:
        The toolchain reference, the shipped ``.bbl``, or an empty ``Resolution``.
    """
    command = reference.command
    installed = _installed_inputs.get()
    names = (
        installation_file_names(reference.value, command.name)
        if installed is not None and command.name in INSTALLATION_INPUT_COMMANDS
        else ()
    )
    held = [name for name in names if installed is not None and name in installed.found]
    if installed is not None and held:
        system = SystemReference(
            reference.path, command, reference.value, held[0], installed.found[held[0]]
        )
        return Resolution(system=system)
    if _report_case_mismatch(reference, candidates, tree, findings):
        return Resolution()
    if (bbl := _shipped_bibliography(reference, tree)) is not None:
        _note_shipped_bibliography(reference, candidates, bbl, findings)
        return Resolution(shipped_bbl=bbl)
    if installed is not None and names and not installed.answered:
        _report_unconfirmed(reference, candidates, findings, cause="installation")
    elif reference.deferred:
        _report_unconfirmed(reference, candidates, findings, cause="deferred")
    else:
        asked = installed is not None and bool(names) and installed.asked.issuperset(names)
        _report_missing(reference, candidates, findings, installation_asked=asked)
    return Resolution()


def resolve_reference(
    reference: Reference, scope: LookupScope, tree: ProjectTree, findings: GraphFindings
) -> Resolution:
    """Find the project file ``reference`` names, or report why none can be chosen.

    A found file is returned as ``target``. A class, package or style that is not in the
    project and has no directory is left to the toolchain and returned as ``system``, as
    is a bare name the TeX installation holds. A bibliography database the project lacks
    whose ``.bbl`` is shipped returns that file as ``shipped_bbl``. Anything else is
    recorded in ``findings`` and returns an empty ``Resolution``. Several matching files
    are noted as an ambiguity and the first in lookup order wins.

    Args:
        reference: The file name and the command that writes it.
        scope: The directories the lookup may start from.
        tree: The project files, the compilation directory, the callback for links and the
            ``.bbl`` the build reads.
        findings: Where problems with the reference are recorded.

    Returns:
        The file found, the toolchain reference, the shipped ``.bbl``, or an empty
        ``Resolution``.
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
        value = reference.value
        basename = PurePosixPath(candidates[0]).name
        return Resolution(system=SystemReference(reference.path, command, value, basename))
    return _resolve_outside_project(reference, candidates, tree, findings)
