r"""The disposable workspace of one build: a bounded copy of the source and an output folder.

``prepare_layout`` makes the new directory ``work`` hold ``project`` (the copy that
latexmk reads and may write into) and ``output`` (where it writes the log, the
recorder trace and the PDF). The copy is made by ``project.import_project``, which
opens every entry without following links, so a link that appears during the copy is
refused rather than followed. Running latexmk and judging its evidence are the other
build modules' work.

Two preparations of the output folder keep every latexmk release on the path it handles
best. TeX writes the ``.aux`` file of ``\include{chapters/one}`` to ``chapters/one.aux``
inside the output folder and stops when that folder does not exist; latexmk 4.83, the
release of Ubuntu 24.04, does not recognise the ``file:line:`` form of that error, which
``-file-line-error`` produces, so the folders of literal ``\include`` targets are created
before the build. And a generated file supplied beside the main file, such as a
``main.bbl``, is copied into the output folder, where TeX looks first: latexmk then
attributes it to the aux folder, as it does for a file the build generated, instead of
creating a rule relative to the working directory whose source does not exist there.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .errors import PreparationError
from .file_safety import read_regular_file
from .generated_files import GENERATED_SUFFIXES
from .import_budget import ImportLimits
from .limits import MAX_WALKED_FOLDER_LEVELS, MAX_WORKSPACE_ENTRIES
from .path_safety import is_link_free_regular_file, is_relative_posix_path
from .project import import_project
from .tex_commands import ROOT_SUFFIXES
from .tex_lexer import MAX_SOURCE_BYTES, ParseLimitExceeded, scan_source_commands

# Distinct folders of \include targets created in one output folder; a paper has a few.
MAX_INCLUDE_FOLDERS = 256


@dataclass(frozen=True, slots=True)
class BuildLayout:
    """Where one build lives: the resolved source, its new workspace and the paths inside.

    Attributes:
        source: The resolved source directory.
        work: The new workspace directory.
        project: The copy of the source inside ``work`` that latexmk reads and may write
            into.
        output: The folder inside ``work`` where latexmk writes the log, the recorder
            trace and the PDF.
        cwd: The directory of the main file inside ``project``, where latexmk runs.
        seeded: The copies of supplied generated files placed in ``output`` before the
            build, each mapped to the project-relative name of the file it copies. A
            build that reads such a copy without regenerating it read the supplied file.
    """

    source: Path
    work: Path
    project: Path
    output: Path
    cwd: Path
    seeded: Mapping[Path, str] = field(default_factory=dict)


def prepare_layout(source: Path, main: str, work: Path, max_total_bytes: int) -> BuildLayout:
    r"""Create ``work`` as a new workspace holding a bounded copy of ``source``.

    The output folder receives the folders of the literal ``\include`` targets and a
    copy of every supplied generated file beside the main file (see the module
    docstring).

    Args:
        source: The project directory to copy; it is resolved first.
        main: The main file, relative to ``source``.
        work: The workspace directory to create.
        max_total_bytes: The cap on the combined size of the copied files; see
            ``copy_source``.

    Returns:
        The layout of the new workspace.

    Raises:
        PreparationError: ``main`` is not in the source, ``work`` exists or lies inside
            the source, or the copy exceeds a limit or meets a link.
    """
    source = source.resolve(strict=True)
    if not (source / main).is_file():
        raise PreparationError(f"Build main file does not exist: {main}")
    work = work.absolute()
    if work.exists() or work.resolve().is_relative_to(source):
        raise PreparationError("Each build requires a new workspace outside its source tree")
    work.mkdir(parents=True)
    work = work.resolve()
    project, output = work / "project", work / "output"
    copy_source(source, project, max_total_bytes=max_total_bytes)
    output.mkdir()
    cwd = project / PurePosixPath(main).parent
    for folder in include_folders(project):
        (output / folder).mkdir(parents=True, exist_ok=True)
    seeded = seed_generated_files(project, cwd, output)
    return BuildLayout(source, work, project, output, cwd, seeded)


def copy_source(source: Path, destination: Path, max_total_bytes: int) -> None:
    """Import a bounded project snapshot into a disposable writable build copy.

    Args:
        source: The project directory to import.
        destination: The directory that receives the copy.
        max_total_bytes: The import budget of the copy: it caps the combined size of
            every imported file rather than the size of any single one. Callers pass
            ``RuntimeLimits.max_copy_bytes``, the job's import byte limit, not the
            per-file limit.
    """
    limits = ImportLimits(
        max_files=MAX_WORKSPACE_ENTRIES,
        max_bytes=max_total_bytes,
        max_depth=MAX_WALKED_FOLDER_LEVELS,
    )
    import_project(source, destination, limits, unwrap=False, exclude_metadata=False)
    # Imported snapshots may be read-only; writable disposable build copies are distinct.
    destination.chmod(0o700)
    for item in destination.rglob("*"):
        item.chmod(0o700 if item.is_dir() else 0o600)


def _tex_sources(project: Path) -> Iterator[Path]:
    """Yield the regular TeX source files of the build copy, in name order."""
    for path in sorted(project.rglob("*")):
        if path.suffix.lower() in ROOT_SUFFIXES and is_link_free_regular_file(project, path):
            yield path


def include_folder(target: str) -> str | None:
    r"""Return the folder that TeX writes the ``.aux`` file of ``\include{target}`` into.

    Args:
        target: The literal argument of ``\include``, relative to the main file's folder.

    Returns:
        The folder relative to the output folder, or ``None`` when the target names no
        folder or is not a plain relative path; a macro, a ``..`` segment or an absolute
        path is never followed.
    """
    target = target.strip()
    if not is_relative_posix_path(target, normalized=False, characters="literal") or any(
        char in target for char in "#$%&{}~^"
    ):
        return None
    folder = PurePosixPath(target).parent.as_posix()
    return None if folder == "." else folder


def include_folders(project: Path) -> list[str]:
    r"""Return the folders of the literal ``\include`` targets in the build copy's TeX sources.

    Every ``.tex``, ``.ltx`` and ``.latex`` file is scanned, whichever file includes it,
    because ``\include`` names its target relative to the main file's folder. An
    unreadable or unparsable file contributes nothing.

    Args:
        project: The build copy.

    Returns:
        At most ``MAX_INCLUDE_FOLDERS`` folders, relative to the output folder, sorted.
    """
    folders: set[str] = set()
    for path in _tex_sources(project):
        try:
            text = read_regular_file(path, MAX_SOURCE_BYTES).decode("utf-8", errors="replace")
            commands = scan_source_commands(text)
        except (OSError, ParseLimitExceeded):
            continue
        for command in commands:
            if command.name != "include" or not command.arguments:
                continue
            folder = include_folder(command.arguments[0].value)
            if folder is not None:
                folders.add(folder)
            if len(folders) >= MAX_INCLUDE_FOLDERS:
                return sorted(folders)
    return sorted(folders)


def seed_generated_files(project: Path, cwd: Path, output: Path) -> dict[Path, str]:
    """Copy the supplied generated files beside the main file into the output folder.

    Only regular, link-free files directly in the main file's folder with a suffix of
    ``GENERATED_SUFFIXES`` are copied; TeX looks for them in the output folder first.

    Args:
        project: The build copy.
        cwd: The main file's folder inside ``project``.
        output: The output folder, which holds no file of the same name yet.

    Returns:
        Each copy in ``output``, mapped to the project-relative name of the file it copies.
    """
    seeded: dict[Path, str] = {}
    for path in sorted(cwd.iterdir()):
        if path.suffix not in GENERATED_SUFFIXES or not is_link_free_regular_file(project, path):
            continue
        copy = output / path.name
        if copy.exists():
            continue
        shutil.copyfile(path, copy)
        seeded[copy] = path.relative_to(project).as_posix()
    return seeded
