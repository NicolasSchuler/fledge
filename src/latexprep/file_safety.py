"""Bounded, link-safe file helpers shared by tool execution, builds and packaging."""

from __future__ import annotations

import os
import stat
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .errors import PreparationError

MIB = 1024 * 1024


def byte_count_text(count: int) -> str:
    """A byte count for messages: ``256 MiB``, ``1.5 MiB``, ``4 KiB`` or ``300 bytes``."""
    if count >= MIB:
        return f"{count // MIB} MiB" if count % MIB == 0 else f"{count / MIB:.1f} MiB"
    if count >= 1024 and count % 1024 == 0:
        return f"{count // 1024} KiB"
    return f"{count} bytes"


class FileIdentity(NamedTuple):
    """Detects that a regular file was replaced or modified between two checks."""

    device: int
    inode: int
    size: int
    modified_ns: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> FileIdentity:
        return cls(info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns)


class DirectoryIdentity(NamedTuple):
    """Detects that a directory was replaced between two checks."""

    device: int
    inode: int

    @classmethod
    def from_stat(cls, info: os.stat_result) -> DirectoryIdentity:
        return cls(info.st_dev, info.st_ino)


def regular_file_identity(path: Path) -> FileIdentity:
    """Identify ``path`` itself; a final symlink or special file is refused."""
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise PreparationError(f"A declared input must be a regular file: {path.name}")
    return FileIdentity.from_stat(info)


def is_plain_relative_name(name: str) -> bool:
    """Whether ``name`` is already a normalized, project-relative POSIX path."""
    relative = PurePosixPath(name)
    return not (
        relative.is_absolute()
        or ".." in relative.parts
        or relative.as_posix() != name
        or "\\" in name
    )


def is_contained_regular_file(root: Path, path: Path) -> bool:
    """Whether ``path`` is a regular file, not a link, that resolves inside ``root``."""
    return (
        not path.is_symlink() and path.is_file() and path.resolve().is_relative_to(root.resolve())
    )
