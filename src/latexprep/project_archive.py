"""Write a staged project folder as a reproducible submission ZIP archive.

Packaging uses DEFLATE level 9, the DOS epoch and Unix modes 0644 for files and 0755
for folders, independent of source timestamps and modes, and sorts entries by name, so
the same files always give the same bytes (with the same zlib version). Every file is
read through the link-refusing walk of ``project``, and an archive is never written
over an existing file. Reading an archive back is ``project``'s work.
"""

from __future__ import annotations

import os
import stat
import zipfile
from pathlib import Path
from typing import BinaryIO

from .cancellation import cancellation_point
from .errors import PreparationError
from .limits import IO_CHUNK_BYTES
from .project import (
    DIRECTORY_FLAGS,
    discard_on_failure,
    folder_entries,
    new_destination,
    open_regular,
    plain_reason,
    source_path,
)
from .project_names import ProjectEntry
from .zip_validation import DOS_DIRECTORY_ATTRIBUTE

_COMPRESSION_LEVEL = 9
_FILE_MODE = 0o644
_DIRECTORY_MODE = 0o755
# The "created by" system code of a ZIP entry that carries Unix modes.
_UNIX_SYSTEM = 3


def create_archive(root: Path, archive: Path) -> None:
    """Write every staged file and directory without a wrapper or an overwrite."""
    try:
        source = source_path(root)
        if not source.is_dir:
            raise PreparationError("Archive input must be a directory.")
        target = new_destination(source.path, archive, True)
        directory_fd = os.open(source.path, DIRECTORY_FLAGS)
        try:
            entries = folder_entries(directory_fd)
            stream = target.open("xb")
            with discard_on_failure(target), stream:
                _write_archive(stream, directory_fd, entries)
        finally:
            os.close(directory_fd)
    except (OSError, zipfile.BadZipFile, zipfile.LargeZipFile) as error:
        raise PreparationError(
            f"Could not create submission archive: {plain_reason(error)}"
        ) from error


def _entry_info(entry: ProjectEntry) -> zipfile.ZipInfo:
    """The archive record of one entry: fixed date, modes and compression level."""
    info = zipfile.ZipInfo(entry.path + ("/" if entry.is_dir else ""))
    info.create_system = _UNIX_SYSTEM
    info.compress_type = zipfile.ZIP_DEFLATED
    # Byte-identical archives also require the same zlib version.
    if hasattr(zipfile.ZipInfo, "compress_level"):  # Python 3.13+
        info.compress_level = _COMPRESSION_LEVEL
    else:  # No public per-entry level setter before Python 3.13.
        info._compresslevel = _COMPRESSION_LEVEL  # type: ignore
    info.file_size = entry.size
    mode = stat.S_IFDIR | _DIRECTORY_MODE if entry.is_dir else stat.S_IFREG | _FILE_MODE
    info.external_attr = mode << 16 | (DOS_DIRECTORY_ATTRIBUTE if entry.is_dir else 0)
    return info


def _write_archive(stream: BinaryIO, directory_fd: int, entries: list[ProjectEntry]) -> None:
    with zipfile.ZipFile(stream, "w", compression=zipfile.ZIP_DEFLATED) as zipped:
        for entry in sorted(entries, key=lambda item: item.path + ("/" if item.is_dir else "")):
            cancellation_point()
            with zipped.open(_entry_info(entry), "w") as writer:
                if not entry.is_dir:
                    with open_regular(directory_fd, entry.path) as reader:
                        while chunk := reader.read(IO_CHUNK_BYTES):
                            cancellation_point()
                            writer.write(chunk)
                        cancellation_point()
