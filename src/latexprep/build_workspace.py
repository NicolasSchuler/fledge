"""The disposable workspace of one build: a bounded copy of the source and an output folder.

``prepare_layout`` makes the new directory ``work`` hold ``project`` (the copy that
latexmk reads and may write into) and ``output`` (where it writes the log, the
recorder trace and the PDF). The copy is made by ``project.import_project``, which
opens every entry without following links, so a link that appears during the copy is
refused rather than followed. Running latexmk and judging its evidence are the other
build modules' work.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from .errors import PreparationError
from .import_budget import ImportLimits
from .limits import MAX_WALKED_FOLDER_LEVELS, MAX_WORKSPACE_ENTRIES
from .project import import_project


@dataclass(frozen=True, slots=True)
class BuildLayout:
    """Where one build lives: the resolved source, its new workspace and the paths inside."""

    source: Path
    work: Path
    project: Path
    output: Path
    # The directory of the main file inside ``project``, where latexmk runs.
    cwd: Path


def prepare_layout(source: Path, main: str, work: Path, max_total_bytes: int) -> BuildLayout:
    """Create ``work`` as a new workspace holding a bounded copy of ``source``.

    ``max_total_bytes`` caps the combined size of the copied files; see ``copy_source``.
    Raises ``PreparationError`` when ``main`` is not in the source, when ``work`` exists
    or lies inside the source, or when the copy exceeds a limit or meets a link.
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
    return BuildLayout(source, work, project, output, project / PurePosixPath(main).parent)


def copy_source(source: Path, destination: Path, max_total_bytes: int) -> None:
    """Import a bounded project snapshot into a disposable writable build copy.

    ``max_total_bytes`` is the import budget of the copy: it caps the combined size of
    every imported file rather than the size of any single one. Callers pass
    ``RuntimeLimits.max_copy_bytes``, the job's import byte limit, not the per-file limit.
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
