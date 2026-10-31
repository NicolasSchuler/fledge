"""Bounded scans of supplied project files: an inventory and a byte budget for reading them.

Inventories never follow links or enter version-control folders, and reads accept
only regular files within explicit byte limits. Names reported in issues are redacted
of secrets. The comments of a source are ``tex_comments``'s, and the wording of results
is ``finding_text``'s.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .cancellation import cancellation_point
from .file_safety import read_file_prefix, read_regular_file
from .limits import MAX_PROJECT_ENTRIES
from .redaction import redact_secrets
from .tex_lexer import MAX_SOURCE_BYTES, MAX_TOTAL_SOURCE_BYTES

MAX_SCAN_MATCHES = 200
_SKIPPED_DIRECTORIES = frozenset({".git", ".hg", ".svn", "__MACOSX"})


class FileInventory(NamedTuple):
    """Regular files by project-relative POSIX name, and entries that could not be listed.

    Attributes:
        files: The regular files, keyed by project-relative POSIX name.
        issues: One message for each entry or directory that could not be listed.
    """

    files: dict[str, Path]
    issues: list[str]


def inventory_files(root: Path) -> FileInventory:
    """List regular files without following links or entering version-control folders.

    Linked and special entries and unreadable directories are reported as issues with
    their names redacted of secrets. The listing stops at ``MAX_PROJECT_ENTRIES``
    entries.

    Args:
        root: The project directory to list.

    Returns:
        The files sorted by name and the issues. A root that is not an accessible
        regular directory yields no files and one issue.
    """
    files: dict[str, Path] = {}
    issues: list[str] = []
    if not root.is_dir() or root.is_symlink():
        return FileInventory(
            files, ["The supplied project is not an accessible regular directory."]
        )
    pending = [root]
    entries = 0
    while pending:
        cancellation_point()
        directory = pending.pop()
        try:
            with os.scandir(directory) as listing:
                for entry in listing:
                    entries += 1
                    if entries > MAX_PROJECT_ENTRIES:
                        return FileInventory(
                            files, [*issues, "Project inventory exceeded the entry limit."]
                        )
                    cancellation_point()
                    path = Path(entry.path)
                    name = path.relative_to(root).as_posix()
                    if entry.is_symlink():
                        issues.append(f"Linked entry was not read: {redact_secrets(name)}")
                    elif entry.is_dir(follow_symlinks=False):
                        if entry.name not in _SKIPPED_DIRECTORIES:
                            pending.append(path)
                    elif entry.is_file(follow_symlinks=False):
                        files[name] = path
                    else:
                        issues.append(f"Special entry was not read: {redact_secrets(name)}")
        except OSError:
            name = directory.relative_to(root).as_posix()
            issues.append(
                f"Directory could not be read (check its permissions): {redact_secrets(name)}"
            )
    return FileInventory(dict(sorted(files.items())), issues)


@dataclass(slots=True)
class ByteBudget:
    """Bytes read by one scan; whole-file reads also respect the remaining total allowance.

    Attributes:
        used: The number of bytes read so far.
    """

    used: int = 0

    def read(self, path: Path) -> bytes:
        """Read a whole file within the per-file limit and what remains of the total.

        Args:
            path: The file to read.

        Returns:
            The contents of the file.

        Raises:
            OSError: The path is a link or a special file, or reading failed.
            FileTooLargeError: The file holds more than the per-file limit or what remains
                of the total.
        """
        remaining = min(MAX_SOURCE_BYTES, max(0, MAX_TOTAL_SOURCE_BYTES - self.used))
        data = read_regular_file(path, remaining)
        self.used += len(data)
        return data

    def read_prefix(self, path: Path, size: int) -> bytes:
        """Read a file's first ``size`` bytes and count them against the total.

        Args:
            path: The file to read.
            size: The largest number of bytes to read.

        Returns:
            At most the first ``size`` bytes of the file.
        """
        data = read_file_prefix(path, size)
        self.used += len(data)
        return data
