"""Safe file reads and writes shared by the build and output paths.

Reads check the opened descriptor and respect the stated byte limit. A reader
can also name a trusted root and refuse links in every component below it.
Writes can stage a complete file before making it visible at its final path.
"""

from __future__ import annotations

import errno
import os
import secrets
import stat
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import BinaryIO, NamedTuple

from .cancellation import cancellation_point
from .errors import PreparationError
from .limits import KIB, MIB

DIRECTORY_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
_FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK


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


def _open_below(root: Path, path: Path) -> int:
    """Open a file descriptor through unlinked directories under the trusted root."""
    if not path.is_relative_to(root):
        raise OSError(errno.EINVAL, "File is outside the read root.")
    parts = path.relative_to(root).parts
    if not parts or ".." in parts:
        raise OSError(errno.EINVAL, "File must be below the read root.")
    directory_fd = os.open(root, DIRECTORY_FLAGS)
    try:
        for part in parts[:-1]:
            child_fd = os.open(part, DIRECTORY_FLAGS, dir_fd=directory_fd)
            os.close(directory_fd)
            directory_fd = child_fd
        return os.open(parts[-1], _FILE_FLAGS, dir_fd=directory_fd)
    finally:
        os.close(directory_fd)


@contextmanager
def open_regular_file(
    path: Path, *, root: Path | None = None
) -> Generator[tuple[BinaryIO, int], None, None]:
    """Open an unlinked regular file without blocking on special files.

    For a reader that must look at part of the file before it reads the rest.

    Args:
        path: The file to open.
        root: A trusted ancestor of the file. When given, the root and every
            subsequent path component must be unlinked.

    Yields:
        The open binary stream and its size. The size is that of the opened descriptor,
        so it cannot disagree with what the stream then reads.

    Raises:
        OSError: The path is a link or a special file, or opening it failed.
    """
    cancellation_point()
    if root is None and path.is_symlink():
        raise OSError(errno.ELOOP, "Linked files are outside the scan.")
    descriptor = os.open(path, _FILE_FLAGS) if root is None else _open_below(root, path)
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


def read_regular_file(path: Path, limit: int, *, root: Path | None = None) -> bytes:
    """Read a whole regular, unlinked file that holds at most ``limit`` bytes.

    The reasons the function itself gives (link, special file, size) are in
    ``strerror`` and name no path.

    Args:
        path: The file to read.
        limit: The largest number of bytes the file may hold.
        root: The trusted ancestor for a descriptor-relative, link-free read, if given.

    Returns:
        The contents of the file.

    Raises:
        OSError: The path is a link or a special file, or reading failed.
        FileTooLargeError: The subclass of ``OSError`` raised when the file holds more
            than ``limit`` bytes, including when it grows while it is read.
    """
    with open_regular_file(path, root=root) as (stream, size):
        if size > limit:
            raise _too_large(limit)
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise _too_large(limit)
    return data


def read_file_prefix(path: Path, size: int, *, root: Path | None = None) -> bytes:
    """Read at most the first ``size`` bytes of a regular, unlinked file of any length.

    Args:
        path: The file to read.
        size: The largest number of bytes to read.
        root: The trusted ancestor for a descriptor-relative, link-free read, if given.

    Returns:
        The bytes read.

    Raises:
        OSError: The path is a link or a special file, or reading failed.
    """
    with open_regular_file(path, root=root) as (stream, _):
        return stream.read(size)


def _write_file_bytes(stream: BinaryIO, data: bytes) -> None:
    """Write all ``data`` or raise ``OSError`` if the write was incomplete."""
    if stream.write(data) != len(data):
        raise OSError(errno.EIO, "Writing the temporary file was incomplete.")


def write_sibling_temporary(destination: Path, data: bytes, *, private: bool = False) -> Path:
    """Stage complete file contents beside ``destination``.

    Args:
        destination: The path beside which to create the temporary file.
        data: The complete contents to write.
        private: Limit access to the owner while staging a replacement.

    Returns:
        The closed temporary file. Its caller owns it and must publish or remove it.

    Raises:
        OSError: The temporary file could not be created or written.
    """
    temporary = destination.with_name(f".fledge-file-{secrets.token_hex(8)}.tmp")
    mode = 0o600 if private else 0o666
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        stream = os.fdopen(descriptor, "wb")
    except BaseException:
        os.close(descriptor)
        temporary.unlink(missing_ok=True)
        raise
    try:
        with stream:
            _write_file_bytes(stream, data)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return temporary


def write_new_file(destination: Path, data: bytes) -> None:
    """Publish ``data`` as a complete new file at ``destination``, without replacing a path.

    Args:
        destination: The new file's path.
        data: The complete contents to publish.

    Raises:
        OSError: Staging or publishing failed, including when the destination already exists.
    """
    temporary = write_sibling_temporary(destination, data)
    try:
        os.link(temporary, destination, follow_symlinks=False)
    finally:
        temporary.unlink(missing_ok=True)
