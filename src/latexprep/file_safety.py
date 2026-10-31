"""Bounded, link-safe file helpers shared by tool execution, builds and packaging.

Reads open the file itself without following a final link, check the opened
descriptor rather than the path, and never read more than the stated limit, so a
file that is swapped, replaced by a link or grown between a check and the read
cannot slip past. Path validation and containment checks live in ``path_safety``.
"""

from __future__ import annotations

import errno
import os
import stat
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, NamedTuple

from .cancellation import cancellation_point
from .errors import PreparationError
from .limits import KIB, MIB


def byte_count_text(count: int) -> str:
    """Format a byte count for messages: ``256 MiB``, ``1.5 MiB``, ``4 KiB`` or ``300 bytes``."""
    if count >= MIB:
        return f"{count // MIB} MiB" if count % MIB == 0 else f"{count / MIB:.1f} MiB"
    if count >= KIB and count % KIB == 0:
        return f"{count // KIB} KiB"
    return f"{count} bytes"


class FileIdentity(NamedTuple):
    """Detects that a regular file was replaced or modified between two checks.

    Attributes:
        device: The device that holds the file (``st_dev``).
        inode: The file's inode number (``st_ino``).
        size: The file's size in bytes (``st_size``).
        modified_ns: The file's modification time in nanoseconds (``st_mtime_ns``).
    """

    device: int
    inode: int
    size: int
    modified_ns: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> FileIdentity:
        """Take the identity from the result of a ``stat`` call."""
        return cls(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


class DirectoryIdentity(NamedTuple):
    """Detects that a directory was replaced between two checks.

    Attributes:
        device: The device that holds the directory (``st_dev``).
        inode: The directory's inode number (``st_ino``).
    """

    device: int
    inode: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> DirectoryIdentity:
        """Take the identity from the result of a ``stat`` call."""
        return cls(info.st_dev, info.st_ino)


def regular_file_identity(path: Path) -> FileIdentity:
    """Identify ``path`` itself; a final symlink or special file is refused.

    Returns:
        The identity of the regular file.

    Raises:
        PreparationError: ``path`` is a symlink or a special file.
    """
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise PreparationError(f"A declared input must be a regular file: {path.name}")
    return FileIdentity.from_stat(info)


class FileTooLargeError(OSError):
    """A file is larger than the limit its reader allows."""


def _too_large(limit: int) -> FileTooLargeError:
    """Build the error for a file larger than ``limit`` bytes."""
    return FileTooLargeError(
        errno.EFBIG, f"File exceeds the scan limit of {byte_count_text(limit)}."
    )


@contextmanager
def open_regular_file(path: Path) -> Generator[tuple[BinaryIO, int], None, None]:
    """Open an unlinked regular file without blocking on special files.

    For a reader that must look at part of the file before it reads the rest.

    Yields:
        The open binary stream and its size. The size is that of the opened descriptor,
        so it cannot disagree with what the stream then reads.

    Raises:
        OSError: The path is a link or a special file, or opening it failed.
    """
    cancellation_point()
    if path.is_symlink():
        raise OSError(errno.ELOOP, "Linked files are outside the scan.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError(errno.EINVAL, "File is not a regular file.")
        stream = os.fdopen(descriptor, "rb")
    except BaseException:
        os.close(descriptor)
        raise
    with stream:
        yield stream, metadata.st_size


def read_regular_file(path: Path, limit: int) -> bytes:
    """Read a whole regular, unlinked file that holds at most ``limit`` bytes.

    The reasons the function itself gives (link, special file, size) are in
    ``strerror`` and name no path.

    Args:
        path: The file to read.
        limit: The largest number of bytes the file may hold.

    Returns:
        The contents of the file.

    Raises:
        OSError: The path is a link or a special file, or reading failed.
        FileTooLargeError: The subclass of ``OSError`` raised when the file holds more
            than ``limit`` bytes, including when it grows while it is read.
    """
    with open_regular_file(path) as (stream, size):
        if size > limit:
            raise _too_large(limit)
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise _too_large(limit)
    return data


def read_file_prefix(path: Path, size: int) -> bytes:
    """Read at most the first ``size`` bytes of a regular, unlinked file of any length.

    Args:
        path: The file to read.
        size: The largest number of bytes to read.

    Returns:
        The bytes read.

    Raises:
        OSError: The path is a link or a special file, or reading failed.
    """
    with open_regular_file(path) as (stream, _):
        return stream.read(size)
