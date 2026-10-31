"""Copy a project folder or ZIP archive into a new, bounded, link-free snapshot.

Import never executes project files or follows links: a folder is read through open
directory descriptors without following links, and an archive entry is extracted only
after ``zip_validation`` has admitted it. Excluded metadata directories are not
traversed when importing folders. Unsafe names (traversal, absolute paths, links,
collisions) abort the import; names that merely cannot be used on every operating
system are kept and reported as warnings. A failed import deletes only the new copy.

The same walk serves ``project_archive`` (writing a reproducible submission ZIP) and
``project_cleanup`` (proposing debris to remove); names and collisions are
``project_names``'s work and the limits ``import_budget``'s.
"""

from __future__ import annotations

import contextlib
import errno
import os
import shutil
import stat
import zipfile
import zlib
from collections.abc import Callable, Generator
from contextlib import AbstractContextManager
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import IO, BinaryIO, NamedTuple

from .cancellation import cancellation_point
from .errors import PreparationError
from .findings import Finding
from .import_budget import ImportBudget, ImportLimits
from .limits import IO_CHUNK_BYTES, MAX_WALKED_FOLDER_LEVELS
from .project_names import (
    PathRegistry,
    ProjectEntry,
    metadata_prefix,
    normalize_path,
    portability_findings,
)
from .report import Change, ChangeKind
from .zip_validation import check_zip_directory, zip_entries

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK
_DEFAULT_IMPORT_LIMITS = ImportLimits()


@dataclass
class ImportedProject:
    """A finished import: where the copy is, its files, and what was warned or changed.

    Attributes:
        root: The new copy.
        files: The project-relative paths of the copied files, sorted.
        findings: The warnings of the import, such as nonportable names.
        changes: What the import left out or unwrapped.
    """

    root: Path
    files: list[str]
    findings: list[Finding] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)


class ResolvedSource(NamedTuple):
    """An input path with its links resolved, and whether it is a folder (else a file).

    Attributes:
        path: The resolved path.
        is_dir: Whether the input is a folder.
    """

    path: Path
    is_dir: bool


class EntrySelection(NamedTuple):
    """The entries an import keeps, with unwrapped paths, and the changes it made.

    Attributes:
        entries: The kept entries.
        changes: The excluded metadata and the removed wrapper folder, if any.
    """

    entries: list[ProjectEntry]
    changes: list[Change]


def folder_entries(
    directory_fd: int,
    budget: ImportBudget | None = None,
    *,
    exclude_metadata: bool = False,
) -> list[ProjectEntry]:
    """List every file and folder below an open directory, by normalized path, links refused.

    Args:
        directory_fd: The open directory to walk.
        budget: Limits the entry count, nesting depth and time; without one the nesting is
            capped at ``MAX_WALKED_FOLDER_LEVELS``.
        exclude_metadata: Whether to skip the contents of metadata folders; the folders
            themselves are still listed.

    Returns:
        The entries, sorted by path.

    Raises:
        PreparationError: An entry is a link or a special file, has an unsafe name or
            collides with another name, or a limit is exceeded.
        OSError: An entry cannot be inspected or opened; the error names its project path.
    """
    entries: list[ProjectEntry] = []
    registry = PathRegistry()

    def visit(current_fd: int, prefix: str) -> None:
        """Add the entries below ``current_fd``, whose paths start with ``prefix``."""
        with os.scandir(current_fd) as listing:
            for item in listing:
                cancellation_point()
                path = normalize_path(f"{prefix}{item.name}")
                if budget is not None:
                    budget.add_entry(path)
                elif path.count("/") >= MAX_WALKED_FOLDER_LEVELS:
                    raise PreparationError(
                        f"Folder nesting exceeds {MAX_WALKED_FOLDER_LEVELS} levels at {path!r}. "
                        "Move the files closer to the project root and run again."
                    )
                try:
                    metadata = item.stat(follow_symlinks=False)
                except OSError as error:
                    # An entry listed through a directory descriptor knows only its name.
                    error.filename = path
                    raise
                is_dir = stat.S_ISDIR(metadata.st_mode)
                if not is_dir and not stat.S_ISREG(metadata.st_mode):
                    raise PreparationError(
                        f"Links and special files are forbidden, but {path!r} is one. "
                        "Replace it with a real copy of the file or folder, or remove it, "
                        "and run again."
                    )
                registry.add(path, is_dir=is_dir)
                entries.append(ProjectEntry(path, is_dir, metadata.st_size if not is_dir else 0))
                if is_dir and not (exclude_metadata and metadata_prefix(path, is_dir=True)):
                    child_fd = _open_below(current_fd, item.name, DIRECTORY_FLAGS, path)
                    try:
                        visit(child_fd, path + "/")
                    finally:
                        os.close(child_fd)

    visit(directory_fd, "")
    return sorted(entries, key=lambda entry: entry.path)


def _select_entries(
    entries: list[ProjectEntry], *, unwrap: bool, exclude_metadata: bool
) -> EntrySelection:
    """Drop metadata and, when asked, the single wrapping folder; record each as a change.

    A single top-level folder is a wrapper only when every kept file lies inside it.

    Args:
        entries: The listed entries.
        unwrap: Whether to remove a single wrapping folder.
        exclude_metadata: Whether to drop metadata entries.

    Returns:
        The kept entries, with the wrapper removed from their paths, and the changes.
    """
    kept: list[ProjectEntry] = []
    excluded: set[str] = set()
    for entry in entries:
        prefix = metadata_prefix(entry.path, is_dir=entry.is_dir) if exclude_metadata else None
        if prefix:
            excluded.add(prefix)
        else:
            kept.append(entry)
    top_levels = {entry.path.split("/", 1)[0] for entry in kept}
    wrapper = None
    if unwrap and len(top_levels) == 1:
        candidate = next(iter(top_levels))
        if all(entry.is_dir or "/" in entry.path for entry in kept):
            wrapper = candidate
    changes = [
        Change(
            path=path.removeprefix(wrapper + "/") if wrapper else path,
            kind=ChangeKind.EXCLUDE,
            reason="Version-control or operating-system metadata is excluded from the snapshot.",
        )
        for path in sorted(excluded)
    ]
    if wrapper:
        changes.append(
            Change(
                path=wrapper + "/",
                kind=ChangeKind.UNWRAP,
                reason="Removed the single ZIP wrapper directory from the imported project root.",
                destination=".",
            )
        )
        kept = [
            replace(entry, path=entry.path.removeprefix(wrapper + "/"))
            for entry in kept
            if entry.path != wrapper
        ]
    return EntrySelection(kept, changes)


def plain_reason(error: Exception) -> str:
    """Explain why a file operation or ZIP read failed, in words rather than Python error text."""
    if isinstance(error, FileNotFoundError):
        return (
            f"{error.filename or 'a file or folder'} was not found. Check the path and that "
            "nothing was moved or deleted while Fledge was reading the project."
        )
    if isinstance(error, PermissionError):
        return (
            f"{error.filename or 'a file or folder'} cannot be read or written (permission "
            "denied). Check its permissions and run again."
        )
    if isinstance(error, OSError) and error.errno == errno.ELOOP:
        return (
            f"{error.filename or 'a file'} was replaced by a link while Fledge was reading "
            "the project. Replace the link with a real file and run again."
        )
    if isinstance(error, OSError):
        where = f" ({error.filename})" if error.filename else ""
        return f"the file system reported: {error.strerror or error}{where}."
    if isinstance(error, (zipfile.BadZipFile, zlib.error, EOFError)):
        return "the ZIP file is damaged or incomplete. Export or download it again and retry."
    if isinstance(error, zipfile.LargeZipFile):
        return "the archive is too large for the ZIP format. Remove files that are not needed."
    if isinstance(error, UnicodeError):
        return (
            "a file name in the ZIP file is not valid text. Create the archive again with "
            "UTF-8 file names."
        )
    return (
        "the ZIP file uses a feature Fledge does not support, such as a password or a "
        "multi-part archive. Create it again as a plain ZIP file."
    )


def source_path(source: Path) -> ResolvedSource:
    """Resolve an input that must be a regular file or a folder, never a link.

    Returns:
        The resolved path and whether it is a folder.

    Raises:
        PreparationError: The input is a link or a special file.
        OSError: The input does not exist or cannot be inspected.
    """
    metadata = source.lstat()
    is_dir = stat.S_ISDIR(metadata.st_mode)
    if not is_dir and not stat.S_ISREG(metadata.st_mode):
        raise PreparationError("Project input must be a regular ZIP file or directory, not a link.")
    return ResolvedSource(source.resolve(strict=True), is_dir)


def new_destination(source: Path, destination: Path, *, source_is_dir: bool) -> Path:
    """Resolve ``destination``, which must not exist and must lie outside ``source``.

    Args:
        source: The resolved input.
        destination: The output path.
        source_is_dir: Whether ``source`` is a folder, inside which no output may lie.

    Returns:
        The destination within its resolved parent folder.

    Raises:
        PreparationError: ``destination`` exists, is ``source`` or lies inside it.
        OSError: The parent folder of ``destination`` does not exist.
    """
    if destination.exists() or destination.is_symlink():
        raise PreparationError(f"Output already exists and will not be overwritten: {destination}.")
    target = destination.parent.resolve(strict=True) / destination.name
    if target == source or (source_is_dir and target.is_relative_to(source)):
        raise PreparationError("Output must be outside the input project.")
    return target


def _open_below(directory_fd: int, name: str, flags: int, path: str) -> int:
    """Open ``name`` below an open directory; a failure names the project path ``path``.

    ``os.open`` with ``dir_fd`` reports only the name it was given, which for a file in a
    folder is the bare file name; whoever fixes its permissions needs the path within
    the project.

    Args:
        directory_fd: The open directory.
        name: The name of the entry in it.
        flags: The flags for ``os.open``.
        path: The project path that a failure names.

    Returns:
        The new file descriptor.

    Raises:
        OSError: Opening failed; its ``filename`` is ``path``.
    """
    try:
        return os.open(name, flags, dir_fd=directory_fd)
    except OSError as error:
        error.filename = path
        raise


def open_regular(directory_fd: int, path: str) -> BinaryIO:
    """Open ``path`` below an open directory without following a link anywhere on the way.

    Args:
        directory_fd: The open directory.
        path: The project path of the file, relative to the directory.

    Returns:
        The file, open for reading bytes.

    Raises:
        PreparationError: The file is not a regular file.
        OSError: A part of the path is a link or cannot be opened; the error names the
            project path up to that part.
    """
    parts = path.split("/")
    current_fd = os.dup(directory_fd)
    try:
        for depth, part in enumerate(parts[:-1], start=1):
            child_fd = _open_below(current_fd, part, DIRECTORY_FLAGS, "/".join(parts[:depth]))
            os.close(current_fd)
            current_fd = child_fd
        file_fd = _open_below(current_fd, parts[-1], _FILE_FLAGS, path)
        if not stat.S_ISREG(os.fstat(file_fd).st_mode):
            os.close(file_fd)
            raise PreparationError(f"Project file became a link or special file: {path!r}.")
        return os.fdopen(file_fd, "rb")
    finally:
        os.close(current_fd)


def _copy_bytes(
    source: IO[bytes],
    target: IO[bytes],
    path: str,
    budget: ImportBudget,
    compressed_size: int | None = None,
) -> None:
    """Copy a stream chunk by chunk, counting the bytes actually read against the budget.

    Args:
        source: The stream to read.
        target: The stream to write.
        path: The project path named in errors.
        budget: The import budget that counts the bytes.
        compressed_size: The compressed size of an archive entry, which bounds its
            expansion ratio; ``None`` for a file from a folder.

    Raises:
        PreparationError: The bytes exceed the size limit or the compression ratio limit.
    """
    total = 0
    while chunk := source.read(
        min(IO_CHUNK_BYTES, budget.limits.max_bytes - budget.total_bytes + 1)
    ):
        budget.add_bytes(len(chunk), path)
        total += len(chunk)
        if (
            compressed_size is not None
            and total > max(compressed_size, 1) * budget.limits.max_ratio
        ):
            raise PreparationError(f"ZIP compression ratio exceeds the limit for {path!r}.")
        target.write(chunk)


@contextlib.contextmanager
def discard_on_failure(target: Path) -> Generator[None]:
    """Remove ``target``, just created by the caller, unless the block completes."""
    try:
        yield
    except BaseException:
        if target.is_dir():
            shutil.rmtree(target)
        else:
            target.unlink()
        raise


def import_project(
    source: Path,
    destination: Path,
    limits: ImportLimits = _DEFAULT_IMPORT_LIMITS,
    *,
    unwrap: bool = True,
    exclude_metadata: bool = True,
) -> ImportedProject:
    """Import into a new directory, deleting only this new copy on failure.

    A ZIP's single wrapping directory is removed by default; folder inputs
    already identify their logical root.

    Args:
        source: The project folder or ZIP file.
        destination: The new directory; it must not exist and must lie outside ``source``.
        limits: The bounds on the entries, bytes, nesting and compression ratio.
        unwrap: Whether to remove a ZIP's single wrapping directory. Use ``unwrap=False``
            and ``exclude_metadata=False`` when re-extracting an exact final package.
        exclude_metadata: Whether to leave out version-control and operating-system
            metadata.

    Returns:
        The finished import.

    Raises:
        PreparationError: The input or the destination is unsuitable, an entry is unsafe,
            a limit is exceeded, or reading or writing failed.
    """
    try:
        resolved = source_path(source)
        target = new_destination(resolved.path, destination, source_is_dir=resolved.is_dir)
        budget = ImportBudget(limits)
        if resolved.is_dir:
            return _import_folder(resolved.path, target, budget, exclude_metadata=exclude_metadata)
        return _import_zip(
            resolved.path, target, budget, unwrap=unwrap, exclude_metadata=exclude_metadata
        )
    except (
        OSError,
        zipfile.BadZipFile,
        zipfile.LargeZipFile,
        RuntimeError,
        UnicodeError,
        zlib.error,
        EOFError,
    ) as error:
        raise PreparationError(f"Could not import project: {plain_reason(error)}") from error


def _write_tree(
    target: Path,
    entries: list[ProjectEntry],
    budget: ImportBudget,
    open_reader: Callable[[ProjectEntry], AbstractContextManager[IO[bytes]]],
) -> ImportedProject:
    """Create the folders and files of ``entries`` under the new ``target``.

    The new ``target`` is removed again if anything fails.

    Args:
        target: The directory to create.
        entries: The entries to write.
        budget: The import budget.
        open_reader: Opens the contents of a file entry.

    Returns:
        The imported project with its portability warnings and no changes yet.
    """
    target.mkdir(mode=0o700)
    with discard_on_failure(target):
        for entry in entries:
            output = target / entry.path
            if entry.is_dir:
                output.mkdir(parents=True, exist_ok=True)
                continue
            output.parent.mkdir(parents=True, exist_ok=True)
            compressed = entry.zip_info.compress_size if entry.zip_info else None
            with open_reader(entry) as reader, output.open("xb") as writer:
                _copy_bytes(reader, writer, entry.path, budget, compressed)
        findings = portability_findings([(entry.path, entry.is_dir) for entry in entries])
        budget.check_time()
    files = sorted(entry.path for entry in entries if not entry.is_dir)
    return ImportedProject(target, files, findings)


def _import_folder(
    source: Path, target: Path, budget: ImportBudget, *, exclude_metadata: bool
) -> ImportedProject:
    """Copy a project folder into the new ``target``, reading it without following links.

    Args:
        source: The resolved folder.
        target: The new directory.
        budget: The import budget.
        exclude_metadata: Whether to leave out metadata.

    Returns:
        The imported project with its changes.
    """
    directory_fd = os.open(source, DIRECTORY_FLAGS)
    try:
        entries = folder_entries(directory_fd, budget, exclude_metadata=exclude_metadata)
        selection = _select_entries(entries, unwrap=False, exclude_metadata=exclude_metadata)
        project = _write_tree(
            target,
            selection.entries,
            budget,
            lambda entry: open_regular(directory_fd, entry.path),
        )
    finally:
        os.close(directory_fd)
    project.changes = selection.changes
    return project


def _import_zip(
    source: Path, target: Path, budget: ImportBudget, *, unwrap: bool, exclude_metadata: bool
) -> ImportedProject:
    """Extract a ZIP file into the new ``target`` once its central directory is checked.

    Args:
        source: The resolved ZIP file.
        target: The new directory.
        budget: The import budget.
        unwrap: Whether to remove a single wrapping directory.
        exclude_metadata: Whether to leave out metadata.

    Returns:
        The imported project with its changes.

    Raises:
        PreparationError: The input became a special file, or an entry is refused.
    """
    source_fd = os.open(source, _FILE_FLAGS)
    with os.fdopen(source_fd, "rb") as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise PreparationError("ZIP input became a special file.")
        check_zip_directory(stream, budget)
        with zipfile.ZipFile(stream) as archive:
            selection = _select_entries(
                zip_entries(archive, budget), unwrap=unwrap, exclude_metadata=exclude_metadata
            )
            project = _write_tree(
                target,
                selection.entries,
                budget,
                lambda entry: archive.open(_member(entry)),
            )
    project.changes = selection.changes
    return project


def _member(entry: ProjectEntry) -> zipfile.ZipInfo:
    """Return the archive record of an entry read from an archive.

    Returns:
        The entry's ``zip_info``.

    Raises:
        ValueError: The entry has no archive record.
    """
    if entry.zip_info is None:
        raise ValueError(f"Archive entry {entry.path!r} has no archive record")
    return entry.zip_info
