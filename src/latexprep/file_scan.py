"""Bounded scans of supplied project files, and the wording shared by their results.

Inventories never follow links or enter version-control folders, and reads accept
only regular files within explicit byte limits. Comments are found with the shared
lexer's literal regions. Discovered secrets are redacted from every reported value.
"""

from __future__ import annotations

import os
import stat
from collections.abc import Callable, Generator, Iterable, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import BinaryIO, NamedTuple, cast

from .file_safety import byte_count_text
from .models import Finding, Status
from .redaction import SECRET_PATTERNS
from .scheduler import cancellation_point
from .tex_lexer import MAX_SOURCE_BYTES, MAX_TOTAL_SOURCE_BYTES, source_regions

MAX_SCAN_ENTRIES = 10_000
MAX_SCAN_MATCHES = 200
_SKIPPED_DIRECTORIES = frozenset({".git", ".hg", ".svn", "__MACOSX"})


class FileInventory(NamedTuple):
    """Regular files by project-relative POSIX name, and entries that could not be listed."""

    files: dict[str, Path]
    issues: list[str]


def inventory_files(root: Path) -> FileInventory:
    """List regular files without following links or entering version-control folders."""
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
                    if entries > MAX_SCAN_ENTRIES:
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


@contextmanager
def _open_regular_file(path: Path) -> Generator[tuple[BinaryIO, int], None, None]:
    """Open an unlinked regular file without blocking on special files; yield it and its size."""
    cancellation_point()
    if path.is_symlink():
        raise OSError("Linked files are outside the scan.")
    descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, "rb") as stream:
        metadata = os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode):
            raise OSError("File is not a regular file.")
        yield stream, metadata.st_size


def read_regular_file(path: Path, limit: int = MAX_SOURCE_BYTES) -> bytes:
    """Read a whole regular, unlinked file that holds at most ``limit`` bytes."""
    with _open_regular_file(path) as (stream, size):
        if size > limit:
            raise OSError(f"File exceeds the scan limit of {byte_count_text(limit)}.")
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise OSError(f"File exceeds the scan limit of {byte_count_text(limit)}.")
    return data


def read_file_prefix(path: Path, size: int) -> bytes:
    """Read at most the first ``size`` bytes of a regular, unlinked file of any length."""
    with _open_regular_file(path) as (stream, _):
        return stream.read(size)


@dataclass(slots=True)
class ByteBudget:
    """Bytes read by one scan; whole-file reads also respect the remaining total allowance."""

    used: int = 0

    def read(self, path: Path) -> bytes:
        """Read a whole file within the per-file limit and what remains of the total."""
        remaining = min(MAX_SOURCE_BYTES, max(0, MAX_TOTAL_SOURCE_BYTES - self.used))
        data = read_regular_file(path, remaining)
        self.used += len(data)
        return data

    def read_prefix(self, path: Path, size: int) -> bytes:
        """Read a file's first ``size`` bytes and count them against the total."""
        data = read_file_prefix(path, size)
        self.used += len(data)
        return data


class TexComment(NamedTuple):
    """A TeX comment's 1-based line and its text after ``%``, up to the line feed."""

    line: int
    text: str


class CommentMarker(NamedTuple):
    """A comment line and the category of note it carries."""

    line: int
    reason: str


def tex_comments(text: str) -> list[TexComment]:
    """Extract TeX comments with their line numbers, skipping the lexer's literal regions."""
    result = []
    line = 1
    previous = 0
    for start, _, kind in source_regions(text).regions:
        if kind != "comment":
            continue
        line += text.count("\n", previous, start)
        previous = start
        # The comment text runs to the line feed, keeping any carriage return.
        end = text.find("\n", start)
        result.append(TexComment(line, text[start + 1 : len(text) if end < 0 else end]))
    return result


def marked_comments(
    comments: Iterable[TexComment], classify: Callable[[str], str | None], limit: int
) -> list[CommentMarker]:
    """The first ``limit`` comments that ``classify`` labels as private notes.

    Only the line and category are returned; comment text never reaches a report.
    """
    markers: list[CommentMarker] = []
    for comment in comments:
        if len(markers) >= limit:
            break
        reason = classify(comment.text)
        if reason is not None:
            markers.append(CommentMarker(comment.line, reason))
    return markers


def redact_secrets(value: str) -> str:
    """Replace every secret-shaped substring with ``[REDACTED]``."""
    for _, pattern in SECRET_PATTERNS:
        value = pattern.sub("[REDACTED]", value)
    return value


def redact_findings(findings: list[Finding], secrets: set[str]) -> list[Finding]:
    """Remove discovered secret values and secret-shaped text from every reported string."""
    ordered_secrets = sorted(secrets, key=len, reverse=True)

    def clean(value: object) -> object:
        if isinstance(value, str):
            cancellation_point()
            for secret in ordered_secrets:
                value = value.replace(secret, "[REDACTED]")
            return redact_secrets(value)
        if isinstance(value, list):
            return [clean(item) for item in value]
        if isinstance(value, dict):
            return {str(clean(key)): clean(item) for key, item in value.items()}
        return value

    return [
        replace(
            item,
            path=str(clean(item.path)) if item.path else None,
            message=str(clean(item.message)),
            suggestion=str(clean(item.suggestion)) if item.suggestion else None,
            details=cast(dict[str, object], clean(item.details)),
        )
        for item in findings
    ]


def is_relative_project_path(value: str) -> bool:
    """Whether ``value`` is a normalized relative POSIX path that stays inside the project."""
    path = PurePosixPath(value)
    return (
        not path.is_absolute()
        and ".." not in path.parts
        and "\\" not in value
        and path.as_posix() == value
        and value not in {"", "."}
        and "\x00" not in value
    )


def check_status(*, failed: bool, incomplete: bool) -> Status:
    """A confirmed violation fails even when other evidence is incomplete."""
    if failed:
        return "failed"
    return "inconclusive" if incomplete else "passed"


def location_label(item: Mapping[str, object]) -> str:
    """``path:line`` for a reported item, ``path`` without a line, or nothing without a path."""
    path, line = item.get("path"), item.get("line")
    if not isinstance(path, str):
        return ""
    return f"{path}:{line}" if isinstance(line, int) else path


def more_suffix(hidden: int) -> str:
    """`` (+N more)`` for ``hidden`` items left out of a listing, or nothing."""
    return f" (+{hidden} more)" if hidden > 0 else ""


def plural(count: int, noun: str) -> str:
    """``1 float``, ``3 floats``: a count with its regularly pluralized noun."""
    return f"{count} {noun}" if count == 1 else f"{count} {noun}s"


def list_locations(items: Sequence[Mapping[str, object]], limit: int = 3) -> str:
    """Distinct item locations, the first ``limit`` named: ``a.tex:1, b.tex (+2 more)``."""
    places = list(dict.fromkeys(filter(None, map(location_label, items))))
    return ", ".join(places[:limit]) + more_suffix(len(places) - limit)


def format_locations(items: Sequence[Mapping[str, object]], limit: int = 3) -> str:
    """Distinct violation locations, counting the remainder: ``a.tex:1, b.tex:2, +1 more``."""
    places = list(
        dict.fromkeys(location_label(item) for item in items if isinstance(item.get("path"), str))
    )
    extra = len(places) - limit
    return ", ".join(places[:limit]) + (f", +{extra} more" if extra > 0 else "")
