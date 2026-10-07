"""Copy projects safely and write reproducible submission archives.

ZIP files use stored or deflated entries only. Packaging uses DEFLATE level 9,
the DOS epoch, and Unix modes 0644/0755, independent of source timestamps/modes.
Import never executes project files or follows links. Excluded metadata
directories are not traversed when importing folders. Unsafe names (traversal,
absolute paths, links, collisions) abort the import; names that merely cannot be
used on every operating system are kept and reported as warnings.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import struct
import time
import unicodedata
import zipfile
import zlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, BinaryIO

from .models import Change, Finding, PreparationError
from .scheduler import cancellation_point

_CHUNK_SIZE = 64 * 1024
_IMPORT_TIMEOUT_SECONDS = 60.0
_METADATA_DIRS = {".git", ".hg", ".svn", "__MACOSX"}
_RESERVED_NAMES = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in "123456789¹²³"
}
_DRIVE_PREFIX = re.compile(r"[A-Za-z]:")
# Finder custom-folder-icon file: the name ends with a carriage return.
_ICON_FILE = "Icon\r"
_AUXILIARY_SUFFIXES = (
    ".aux",
    ".auxlock",
    ".log",
    ".fdb_latexmk",
    ".fls",
    ".synctex",
    ".synctex.gz",
    ".synctex(busy)",
    ".synctex.gz(busy)",
    ".blg",
    ".bcf",
    ".run.xml",
    ".toc",
    ".lof",
    ".lot",
    ".out",
    ".nav",
    ".snm",
    ".vrb",
    ".xdv",
    ".idx",
    ".ilg",
    ".ind",
    ".glo",
    ".gls",
    ".glg",
    ".ist",
    ".brf",
    ".lol",
    ".loa",
    ".thm",
    ".ptc",
    ".bbl-SAVE-ERROR",
    ".pyg",
)
_DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


@dataclass(frozen=True)
class ImportLimits:
    """Import bounds; max_files counts directory entries as well as files."""

    max_files: int = 10_000
    max_bytes: int = 268_435_456
    max_depth: int = 32
    max_ratio: int = 200

    def __post_init__(self) -> None:
        for name in ("max_files", "max_bytes", "max_depth", "max_ratio"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise PreparationError(f"{name} must be a positive integer.")


_DEFAULT_IMPORT_LIMITS = ImportLimits()


@dataclass
class ImportedProject:
    root: Path
    files: list[str]
    findings: list[Finding] = field(default_factory=list)
    changes: list[Change] = field(default_factory=list)


@dataclass(frozen=True)
class _Entry:
    path: str
    is_dir: bool
    size: int = 0
    zip_info: zipfile.ZipInfo | None = None


@dataclass
class _Budget:
    limits: ImportLimits
    entries: int = 0
    total_bytes: int = 0
    started: float = field(default_factory=time.monotonic)

    def check_time(self) -> None:
        cancellation_point()
        if time.monotonic() - self.started > _IMPORT_TIMEOUT_SECONDS:
            raise PreparationError("Project import exceeded the 60-second processing limit.")

    def add_entry(self, path: str) -> None:
        self.check_time()
        self.entries += 1
        if self.entries > self.limits.max_files:
            raise PreparationError(f"Project exceeds the {self.limits.max_files} entry limit.")
        if len(path.split("/")) > self.limits.max_depth:
            raise PreparationError(f"Project path exceeds the nesting limit: {path!r}.")

    def add_bytes(self, count: int, path: str) -> None:
        self.check_time()
        self.total_bytes += count
        if self.total_bytes > self.limits.max_bytes:
            raise PreparationError(
                f"Project exceeds the {self.limits.max_bytes}-byte size limit at {path!r}."
            )


def _key(path: str) -> str:
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", path).casefold())


def _normalise_path(name: str) -> str:
    """Normalise a relative path, rejecting only names that are unsafe to extract.

    Names that are merely nonportable are accepted; see ``_portability_reasons``.
    """
    if not name or name.startswith("/") or "\\" in name or "\x00" in name:
        raise PreparationError(f"Unsafe project path: {name!r}.")
    parts = name.split("/")
    if ".." in parts:
        raise PreparationError(f"Project path traversal is forbidden: {name!r}.")
    parts = [part for part in parts if part not in {"", "."}]
    if not parts:
        raise PreparationError(f"Empty project path: {name!r}.")
    if _DRIVE_PREFIX.match(parts[0]):
        # A Windows drive-letter prefix is an absolute or drive-relative path there.
        raise PreparationError(f"Unsafe project path: {name!r}.")
    return "/".join(parts)


def _portability_reasons(path: str) -> list[str]:
    """Explain why a normalised path cannot be used on every operating system."""
    reasons: list[str] = []
    for part in path.split("/"):
        found = (
            (any(ord(char) < 32 for char in part), "contains a control character"),
            (any(char in '<>:"|?*' for char in part), 'contains a character from <>:"|?*'),
            (part.endswith((" ", ".")), "ends with a space or dot"),
            (_key(part.split(".", 1)[0]) in _RESERVED_NAMES, "uses a reserved Windows device name"),
        )
        for present, reason in found:
            if present and reason not in reasons:
                reasons.append(reason)
    return reasons


def _portability_findings(entries: list[tuple[str, bool]]) -> list[Finding]:
    """Warn once per kept file (or empty directory) whose path is nonportable."""
    occupied: set[str] = set()
    for path, _ in entries:
        parts = path.split("/")
        occupied.update("/".join(parts[:index]) for index in range(1, len(parts)))
    findings = []
    for path, is_dir in entries:
        reasons = _portability_reasons(path)
        if reasons and not (is_dir and path in occupied):
            findings.append(
                Finding(
                    "project.nonportable_filename",
                    f"Project file name is not portable to all operating systems: {path!r} "
                    f"({'; '.join(reasons)}). It was kept unchanged.",
                    "warning",
                    "failed",
                    path=path,
                    details={"reasons": reasons},
                )
            )
    return findings


class _PathRegistry:
    """Account for implicit parents without rejecting a later directory entry."""

    def __init__(self) -> None:
        self.paths: dict[str, tuple[str, bool, bool]] = {}

    def add(self, path: str, is_dir: bool) -> None:
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            candidate = "/".join(parts[:index])
            explicit = index == len(parts)
            directory = not explicit or is_dir
            existing = self.paths.get(_key(candidate))
            if existing is None:
                self.paths[_key(candidate)] = (candidate, directory, explicit)
                continue
            old_path, old_directory, old_explicit = existing
            if old_path != candidate:
                raise PreparationError(
                    f"Case or Unicode filename collision: {old_path!r} and {candidate!r}."
                )
            if not directory or not old_directory:
                raise PreparationError(f"Duplicate path or file/directory conflict: {candidate!r}.")
            if explicit and old_explicit:
                raise PreparationError(f"Duplicate normalized directory path: {candidate!r}.")
            self.paths[_key(candidate)] = (candidate, True, old_explicit or explicit)


def _metadata_prefix(path: str, is_dir: bool) -> str | None:
    parts = path.split("/")
    for index, part in enumerate(parts):
        if part in _METADATA_DIRS and (index < len(parts) - 1 or is_dir):
            return "/".join(parts[: index + 1])
    if not is_dir and (
        parts[-1] in {".DS_Store", _ICON_FILE} or parts[-1].startswith("._")  # AppleDouble
    ):
        return path
    return None


def _folder_entries(
    directory_fd: int,
    budget: _Budget | None = None,
    *,
    exclude_metadata: bool = False,
) -> list[_Entry]:
    entries: list[_Entry] = []
    registry = _PathRegistry()

    def visit(current_fd: int, prefix: str) -> None:
        with os.scandir(current_fd) as listing:
            for item in listing:
                cancellation_point()
                path = _normalise_path(f"{prefix}{item.name}")
                if budget is not None:
                    budget.add_entry(path)
                metadata = item.stat(follow_symlinks=False)
                is_dir = stat.S_ISDIR(metadata.st_mode)
                if not is_dir and not stat.S_ISREG(metadata.st_mode):
                    raise PreparationError(f"Links and special files are forbidden: {path!r}.")
                registry.add(path, is_dir)
                entries.append(_Entry(path, is_dir, metadata.st_size if not is_dir else 0))
                if is_dir and not (exclude_metadata and _metadata_prefix(path, True)):
                    child_fd = os.open(item.name, _DIRECTORY_FLAGS, dir_fd=current_fd)
                    try:
                        visit(child_fd, path + "/")
                    finally:
                        os.close(child_fd)

    visit(directory_fd, "")
    return sorted(entries, key=lambda entry: entry.path)


def _check_zip_directory(stream: BinaryIO, budget: _Budget) -> None:
    """Bound the central directory before ZipFile loads it into memory."""
    stream.seek(0, os.SEEK_END)
    size = stream.tell()
    tail_size = min(size, 65_557)
    stream.seek(size - tail_size)
    tail = stream.read(tail_size)
    end_index = tail.rfind(b"PK\x05\x06")
    if end_index < 0 or len(tail) - end_index < 22:
        raise PreparationError("ZIP end-of-directory record is missing or truncated.")
    _, disk, directory_disk, disk_count, count, directory_size, offset, comment_size = (
        struct.unpack_from("<4s4H2IH", tail, end_index)
    )
    if len(tail) - end_index != 22 + comment_size:
        raise PreparationError("ZIP comment or trailing data is malformed.")
    directory_end = size - tail_size + end_index
    if directory_end >= 20:
        stream.seek(directory_end - 20)
        locator = stream.read(20)
        if locator.startswith(b"PK\x06\x07"):
            _, locator_disk, zip64_offset, total_disks = struct.unpack("<4sIQI", locator)
            if locator_disk or total_disks != 1:
                raise PreparationError("Multi-disk ZIP archives are unsupported.")
            if zip64_offset > directory_end - 76:
                raise PreparationError("ZIP64 directory location is outside the archive.")
            stream.seek(zip64_offset)
            record = stream.read(56)
            if len(record) != 56 or not record.startswith(b"PK\x06\x06"):
                raise PreparationError("ZIP64 directory record is malformed.")
            (
                _,
                record_size,
                _,
                _,
                disk,
                directory_disk,
                disk_count,
                count,
                directory_size,
                offset,
            ) = struct.unpack("<4sQ2H2I4Q", record)
            if record_size < 44 or zip64_offset + record_size + 12 != directory_end - 20:
                raise PreparationError("ZIP64 directory offsets are inconsistent.")
            directory_end = zip64_offset
    if disk or directory_disk or disk_count != count:
        raise PreparationError("Multi-disk ZIP archives are unsupported or inconsistent.")
    if count > budget.limits.max_files:
        raise PreparationError(f"Project exceeds the {budget.limits.max_files} entry limit.")
    if directory_size > 16 * 1024 * 1024:
        raise PreparationError("ZIP central-directory metadata exceeds the 16 MiB limit.")
    if offset + directory_size != directory_end:
        raise PreparationError(
            "ZIP directory offsets are invalid; embedded archives are unsupported."
        )
    stream.seek(offset)
    remaining = directory_size
    actual_count = 0
    while remaining:
        budget.check_time()
        header = stream.read(46)
        if len(header) != 46 or not header.startswith(b"PK\x01\x02"):
            raise PreparationError("ZIP central directory is malformed.")
        entry_size = 46 + sum(struct.unpack_from("<3H", header, 28))
        if entry_size > remaining:
            raise PreparationError("ZIP central directory is truncated.")
        actual_count += 1
        if actual_count > budget.limits.max_files:
            raise PreparationError(f"Project exceeds the {budget.limits.max_files} entry limit.")
        stream.seek(entry_size - 46, os.SEEK_CUR)
        remaining -= entry_size
    if actual_count != count:
        raise PreparationError("ZIP directory entry counts are inconsistent.")
    stream.seek(0)


def _zip_entries(archive: zipfile.ZipFile, budget: _Budget) -> list[_Entry]:
    entries: list[_Entry] = []
    registry = _PathRegistry()
    declared_bytes = 0
    for info in archive.infolist():
        path = _normalise_path(info.orig_filename)
        budget.add_entry(path)
        mode = stat.S_IFMT(info.external_attr >> 16)
        if mode not in {0, stat.S_IFREG, stat.S_IFDIR}:
            raise PreparationError(f"ZIP links and special files are forbidden: {path!r}.")
        is_dir = info.is_dir() or mode == stat.S_IFDIR or bool(info.external_attr & 0x10)
        if is_dir and (mode == stat.S_IFREG or info.file_size):
            raise PreparationError(f"Conflicting ZIP directory metadata: {path!r}.")
        if info.flag_bits & 0x41:
            raise PreparationError(f"Encrypted ZIP entries are unsupported: {path!r}.")
        if info.compress_type not in {zipfile.ZIP_STORED, zipfile.ZIP_DEFLATED}:
            raise PreparationError(f"Unsupported ZIP compression for {path!r}.")
        registry.add(path, is_dir)
        declared_bytes += info.file_size
        if declared_bytes > budget.limits.max_bytes:
            raise PreparationError(f"ZIP exceeds the {budget.limits.max_bytes}-byte size limit.")
        if info.file_size > max(info.compress_size, 1) * budget.limits.max_ratio:
            raise PreparationError(f"ZIP compression ratio exceeds the limit for {path!r}.")
        entries.append(_Entry(path, is_dir, info.file_size, info))
    return sorted(entries, key=lambda entry: entry.path)


def _select_entries(
    entries: list[_Entry], *, unwrap: bool, exclude_metadata: bool
) -> tuple[list[_Entry], str | None, list[Change]]:
    kept: list[_Entry] = []
    excluded: set[str] = set()
    for entry in entries:
        prefix = _metadata_prefix(entry.path, entry.is_dir) if exclude_metadata else None
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
            kind="exclude",
            reason="Version-control or operating-system metadata is excluded from the snapshot.",
        )
        for path in sorted(excluded)
    ]
    if wrapper:
        changes.append(
            Change(
                path=wrapper + "/",
                kind="unwrap",
                reason="Removed the single ZIP wrapper directory from the imported project root.",
                destination=".",
            )
        )
    return kept, wrapper, changes


def _source_path(source: Path) -> tuple[Path, bool]:
    metadata = source.lstat()
    is_dir = stat.S_ISDIR(metadata.st_mode)
    if not is_dir and not stat.S_ISREG(metadata.st_mode):
        raise PreparationError("Project input must be a regular ZIP file or directory, not a link.")
    return source.resolve(strict=True), is_dir


def _new_destination(source: Path, destination: Path, source_is_dir: bool) -> Path:
    if destination.exists() or destination.is_symlink():
        raise PreparationError(f"Output already exists and will not be overwritten: {destination}.")
    target = destination.parent.resolve(strict=True) / destination.name
    if target == source or (source_is_dir and target.is_relative_to(source)):
        raise PreparationError("Output must be outside the input project.")
    return target


def _open_regular(directory_fd: int, path: str) -> BinaryIO:
    parts = path.split("/")
    current_fd = os.dup(directory_fd)
    try:
        for part in parts[:-1]:
            child_fd = os.open(part, _DIRECTORY_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = child_fd
        file_fd = os.open(parts[-1], _FILE_FLAGS, dir_fd=current_fd)
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
    budget: _Budget,
    compressed_size: int | None = None,
) -> None:
    total = 0
    while chunk := source.read(min(_CHUNK_SIZE, budget.limits.max_bytes - budget.total_bytes + 1)):
        budget.add_bytes(len(chunk), path)
        total += len(chunk)
        if (
            compressed_size is not None
            and total > max(compressed_size, 1) * budget.limits.max_ratio
        ):
            raise PreparationError(f"ZIP compression ratio exceeds the limit for {path!r}.")
        target.write(chunk)


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
    already identify their logical root. Use ``unwrap=False`` and
    ``exclude_metadata=False`` when re-extracting an exact final package.
    """
    target: Path | None = None
    created = False
    try:
        source, source_is_dir = _source_path(source)
        target = _new_destination(source, destination, source_is_dir)
        budget = _Budget(limits)
        if source_is_dir:
            directory_fd = os.open(source, _DIRECTORY_FLAGS)
            try:
                entries = _folder_entries(directory_fd, budget, exclude_metadata=exclude_metadata)
                entries, _, changes = _select_entries(
                    entries, unwrap=False, exclude_metadata=exclude_metadata
                )
                target.mkdir(mode=0o700)
                created = True
                for entry in entries:
                    output = target / entry.path
                    if entry.is_dir:
                        output.mkdir(parents=True, exist_ok=True)
                    else:
                        output.parent.mkdir(parents=True, exist_ok=True)
                        with (
                            _open_regular(directory_fd, entry.path) as reader,
                            output.open("xb") as writer,
                        ):
                            _copy_bytes(reader, writer, entry.path, budget)
            finally:
                os.close(directory_fd)
            files = [entry.path for entry in entries if not entry.is_dir]
            findings = _portability_findings([(entry.path, entry.is_dir) for entry in entries])
        else:
            source_fd = os.open(source, _FILE_FLAGS)
            with os.fdopen(source_fd, "rb") as stream:
                if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
                    raise PreparationError("ZIP input became a special file.")
                _check_zip_directory(stream, budget)
                with zipfile.ZipFile(stream) as archive:
                    entries = _zip_entries(archive, budget)
                    entries, wrapper, changes = _select_entries(
                        entries, unwrap=unwrap, exclude_metadata=exclude_metadata
                    )
                    target.mkdir(mode=0o700)
                    created = True
                    files = []
                    kept: list[tuple[str, bool]] = []
                    for entry in entries:
                        path = entry.path.removeprefix(wrapper + "/") if wrapper else entry.path
                        if wrapper and entry.path == wrapper:
                            continue
                        kept.append((path, entry.is_dir))
                        output = target / path
                        if entry.is_dir:
                            output.mkdir(parents=True, exist_ok=True)
                            continue
                        output.parent.mkdir(parents=True, exist_ok=True)
                        assert entry.zip_info is not None
                        with archive.open(entry.zip_info) as reader, output.open("xb") as writer:
                            _copy_bytes(reader, writer, path, budget, entry.zip_info.compress_size)
                        files.append(path)
                    findings = _portability_findings(kept)
        budget.check_time()
        return ImportedProject(target, sorted(files), findings, changes)
    except BaseException as error:
        if created and target is not None:
            shutil.rmtree(target)
        if isinstance(
            error,
            (
                OSError,
                zipfile.BadZipFile,
                zipfile.LargeZipFile,
                RuntimeError,
                UnicodeError,
                zlib.error,
                EOFError,
            ),
        ):
            raise PreparationError(f"Could not import project: {error}") from error
        raise


def create_archive(root: Path, archive: Path) -> None:
    """Write every staged file and directory without a wrapper or an overwrite."""
    target: Path | None = None
    created = False
    try:
        root, is_dir = _source_path(root)
        if not is_dir:
            raise PreparationError("Archive input must be a directory.")
        target = _new_destination(root, archive, True)
        directory_fd = os.open(root, _DIRECTORY_FLAGS)
        try:
            entries = _folder_entries(directory_fd)
            with target.open("xb") as stream:
                created = True
                with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
                    for entry in sorted(
                        entries, key=lambda item: item.path + ("/" if item.is_dir else "")
                    ):
                        cancellation_point()
                        info = zipfile.ZipInfo(entry.path + ("/" if entry.is_dir else ""))
                        info.create_system = 3
                        info.compress_type = zipfile.ZIP_DEFLATED
                        # Byte-identical archives also require the same zlib version.
                        if hasattr(zipfile.ZipInfo, "compress_level"):  # Python 3.13+
                            info.compress_level = 9
                        else:  # No public per-entry level setter before Python 3.13.
                            info._compresslevel = 9  # type: ignore
                        info.file_size = entry.size
                        mode = stat.S_IFDIR | 0o755 if entry.is_dir else stat.S_IFREG | 0o644
                        info.external_attr = mode << 16 | (0x10 if entry.is_dir else 0)
                        with zipped.open(info, "w") as writer:
                            if not entry.is_dir:
                                with _open_regular(directory_fd, entry.path) as reader:
                                    while chunk := reader.read(_CHUNK_SIZE):
                                        cancellation_point()
                                        writer.write(chunk)
                                    cancellation_point()
        finally:
            os.close(directory_fd)
    except BaseException as error:
        if created and target is not None:
            target.unlink()
        if isinstance(error, (OSError, zipfile.BadZipFile, zipfile.LargeZipFile)):
            raise PreparationError(f"Could not create submission archive: {error}") from error
        raise


def _cleanup_reason(path: str) -> str | None:
    *directories, name = path.split("/")
    if _metadata_prefix(path, False) or name in {"Thumbs.db", "desktop.ini"}:
        return "Operating-system or version-control metadata."
    # Never ``.bbl``: a submission usually needs the generated bibliography.
    if name.endswith(_AUXILIARY_SUFFIXES) or any(
        directory.startswith("_minted") for directory in directories
    ):
        return "Rebuildable LaTeX auxiliary or diagnostic file."
    if (
        name.endswith("~")
        or name.startswith(".#")
        or (name.startswith("#") and name.endswith("#"))
        or (name.startswith(".") and name.endswith((".swp", ".swo")))
        or name.endswith((".tex.bak", ".bib.bak", ".sty.bak", ".cls.bak"))
    ):
        return "Editor backup or swap file."
    return None


def plan_cleanup(root: Path, observed_dependencies: set[str]) -> list[Change]:
    """Propose known debris only; retain every observed dependency and unknown file."""
    try:
        root, is_dir = _source_path(root)
        if not is_dir:
            raise PreparationError("Cleanup input must be a directory.")
        protected: set[str] = set()
        for dependency in observed_dependencies:
            dependency_path = Path(dependency)
            try:
                dependency_path = (root / dependency_path).resolve().relative_to(root)
            except ValueError:
                continue
            protected.add(_key(dependency_path.as_posix()))
        directory_fd = os.open(root, _DIRECTORY_FLAGS)
        try:
            entries = _folder_entries(directory_fd)
        finally:
            os.close(directory_fd)
        changes = []
        for entry in entries:
            reason = _cleanup_reason(entry.path)
            if not entry.is_dir and _key(entry.path) not in protected and reason:
                changes.append(Change(path=entry.path, kind="exclude", reason=reason))
        return changes
    except OSError as error:
        raise PreparationError(f"Could not plan project cleanup: {error}") from error
