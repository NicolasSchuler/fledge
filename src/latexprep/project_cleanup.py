"""Propose the debris of a staged project that a submission should not contain.

Known debris is operating-system and version-control metadata, files that LaTeX
rebuilds (auxiliary and diagnostic output) and editor backup or swap files. Only
those are proposed; ``.bbl`` is never among them, because a submission usually needs
the generated bibliography, and every input the build observed is protected whatever
its name. Applying the proposal is the caller's work; the walk is ``project``'s.
"""

from __future__ import annotations

import os
from pathlib import Path

from .errors import PreparationError
from .project import DIRECTORY_FLAGS, folder_entries, plain_reason, source_path
from .project_names import collision_key, metadata_prefix
from .report import Change, ChangeKind

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


def cleanup_reason(path: str) -> str | None:
    """Why a project file is known debris, or ``None`` when it is not."""
    *directories, name = path.split("/")
    if metadata_prefix(path, is_dir=False) or name in {"Thumbs.db", "desktop.ini"}:
        return "Operating-system or version-control metadata."
    # Never ``.bbl``: a submission usually needs the generated bibliography.
    if name.endswith(_AUXILIARY_SUFFIXES) or any(
        directory.startswith("_minted") for directory in directories
    ):
        return "Rebuildable LaTeX auxiliary or diagnostic file."
    if (
        name.startswith(".#")
        or (name.startswith("#") and name.endswith("#"))
        or (name.startswith(".") and name.endswith((".swp", ".swo")))
        or name.endswith(("~", ".tex.bak", ".bib.bak", ".sty.bak", ".cls.bak"))
    ):
        return "Editor backup or swap file."
    return None


def plan_cleanup(root: Path, observed_dependencies: set[str]) -> list[Change]:
    """Propose known debris only; retain every observed dependency and unknown file."""
    try:
        source = source_path(root)
        if not source.is_dir:
            raise PreparationError("Cleanup input must be a directory.")
        protected: set[str] = set()
        for dependency in observed_dependencies:
            dependency_path = Path(dependency)
            try:
                dependency_path = (source.path / dependency_path).resolve().relative_to(source.path)
            except ValueError:
                continue
            protected.add(collision_key(dependency_path.as_posix()))
        directory_fd = os.open(source.path, DIRECTORY_FLAGS)
        try:
            entries = folder_entries(directory_fd)
        finally:
            os.close(directory_fd)
        changes = []
        for entry in entries:
            reason = cleanup_reason(entry.path)
            if not entry.is_dir and collision_key(entry.path) not in protected and reason:
                changes.append(Change(path=entry.path, kind=ChangeKind.EXCLUDE, reason=reason))
        return changes
    except OSError as error:
        raise PreparationError(f"Could not plan project cleanup: {plain_reason(error)}") from error
