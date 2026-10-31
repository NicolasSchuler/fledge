"""Names inside a project: normalization, collisions, portability and excluded metadata.

Everything here looks at path text only. ``normalize_path`` refuses names that are
unsafe to extract, ``PathRegistry`` finds names that collide on case-insensitive or
Unicode-normalizing file systems, ``portability_findings`` warns about names that are
merely awkward on other operating systems, and ``metadata_prefix`` finds version-control
and operating-system metadata. Reading or writing the files is ``project``'s work.
"""

from __future__ import annotations

import unicodedata
import zipfile
from dataclasses import dataclass
from typing import NamedTuple

from .errors import PreparationError
from .findings import Finding
from .path_safety import relative_path_problem

METADATA_DIRS = {".git", ".hg", ".svn", "__MACOSX"}
_RESERVED_NAMES = {"con", "prn", "aux", "nul", "conin$", "conout$"} | {
    f"{prefix}{number}" for prefix in ("com", "lpt") for number in "123456789¹²³"
}
# Finder custom-folder-icon file: the name ends with a carriage return.
_ICON_FILE = "Icon\r"
_NONPORTABLE_CHARACTERS = '<>:"|?*'


@dataclass(frozen=True)
class ProjectEntry:
    """A file or folder of a project by normalized path; archive entries carry ``zip_info``.

    Attributes:
        path: The normalized project-relative path.
        is_dir: Whether the entry is a folder.
        size: The size in bytes.
        zip_info: The archive record of an entry read from an archive, otherwise ``None``.
    """

    path: str
    is_dir: bool
    size: int = 0
    zip_info: zipfile.ZipInfo | None = None


def collision_key(path: str) -> str:
    """Return the key on which colliding names agree.

    Names that collide on case-insensitive or Unicode-normalizing file systems get the
    same key.
    """
    return unicodedata.normalize("NFC", unicodedata.normalize("NFC", path).casefold())


def normalize_path(name: str) -> str:
    """Normalize a relative path, rejecting only names that are unsafe to extract.

    Names that are merely nonportable are accepted; see ``portability_findings``.

    Returns:
        The path without empty and ``.`` parts.

    Raises:
        PreparationError: The name traverses upwards, is empty or is otherwise unsafe,
            such as an absolute path or one with a Windows drive prefix.
    """
    problem = relative_path_problem(name, normalized=False)
    if problem == "traversal":
        raise PreparationError(f"Project path traversal is forbidden: {name!r}.")
    if problem == "current":
        raise PreparationError(f"Empty project path: {name!r}.")
    if problem is not None:
        # Includes a Windows drive prefix, an absolute or drive-relative path there.
        raise PreparationError(f"Unsafe project path: {name!r}.")
    return "/".join(part for part in name.split("/") if part not in {"", "."})


def _portability_reasons(path: str) -> list[str]:
    """Explain why a normalized path cannot be used on every operating system.

    Returns:
        Each distinct reason once; empty when the path is portable.
    """
    reasons: list[str] = []
    for part in path.split("/"):
        found = (
            (any(ord(char) < 32 for char in part), "contains a control character"),
            (
                any(char in _NONPORTABLE_CHARACTERS for char in part),
                f"contains a character from {_NONPORTABLE_CHARACTERS}",
            ),
            (part.endswith((" ", ".")), "ends with a space or dot"),
            (
                collision_key(part.split(".", 1)[0]) in _RESERVED_NAMES,
                "uses a reserved Windows device name",
            ),
        )
        for present, reason in found:
            if present and reason not in reasons:
                reasons.append(reason)
    return reasons


def portability_findings(entries: list[tuple[str, bool]]) -> list[Finding]:
    """Warn once per kept file (or empty directory) whose path is nonportable.

    Args:
        entries: The kept paths, each with whether it is a directory. A directory that
            is the parent of another entry is not reported itself.

    Returns:
        One warning finding per nonportable path.
    """
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
                    severity="warning",
                    status="failed",
                    path=path,
                    details={"reasons": reasons},
                )
            )
    return findings


class _RegisteredPath(NamedTuple):
    """A path that a :class:`PathRegistry` holds.

    Attributes:
        path: The path as first registered.
        is_dir: Whether the path is a directory.
        explicit: Whether the path was registered explicitly.
    """

    path: str
    is_dir: bool
    # Named by an entry itself rather than only implied as a parent directory.
    explicit: bool


class PathRegistry:
    """Account for implicit parents without rejecting a later directory entry.

    Attributes:
        paths: The registered paths by their :func:`collision_key`.
    """

    def __init__(self) -> None:
        self.paths: dict[str, _RegisteredPath] = {}

    def add(self, path: str, *, is_dir: bool) -> None:
        """Register ``path`` together with its implicit parent directories.

        A parent registered only implicitly may later be named by a directory entry.

        Args:
            path: The normalized project path.
            is_dir: Whether the entry itself is a directory.

        Raises:
            PreparationError: The path collides with another by case or Unicode
                normalization, repeats a file, conflicts between file and directory, or
                names a directory a second time.
        """
        parts = path.split("/")
        for index in range(1, len(parts) + 1):
            candidate = "/".join(parts[:index])
            explicit = index == len(parts)
            directory = not explicit or is_dir
            key = collision_key(candidate)
            existing = self.paths.get(key)
            if existing is None:
                self.paths[key] = _RegisteredPath(candidate, directory, explicit)
                continue
            if existing.path != candidate:
                raise PreparationError(
                    f"Case or Unicode filename collision: {existing.path!r} and {candidate!r}."
                )
            if not directory or not existing.is_dir:
                raise PreparationError(f"Duplicate path or file/directory conflict: {candidate!r}.")
            if explicit and existing.explicit:
                raise PreparationError(f"Duplicate normalized directory path: {candidate!r}.")
            self.paths[key] = _RegisteredPath(candidate, True, existing.explicit or explicit)


def metadata_prefix(path: str, *, is_dir: bool) -> str | None:
    """Find the version-control or operating-system metadata that ``path`` belongs to.

    Args:
        path: The normalized project path.
        is_dir: Whether ``path`` is a directory.

    Returns:
        The metadata directory that contains ``path`` (or is ``path``), ``path`` itself
        for a metadata file, or ``None`` when ``path`` is not metadata.
    """
    parts = path.split("/")
    for index, part in enumerate(parts):
        if part in METADATA_DIRS and (index < len(parts) - 1 or is_dir):
            return "/".join(parts[: index + 1])
    if not is_dir and (
        parts[-1] in {".DS_Store", _ICON_FILE} or parts[-1].startswith("._")  # AppleDouble
    ):
        return path
    return None
