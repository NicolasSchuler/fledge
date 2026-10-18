"""The files of a project as the source graph inventories them, and how their names compare.

Owns the listing of a project folder (files and links, in a stable order), the
case- and normalization-insensitive key under which two names collide, the index of
every name suffix and directory prefix, and the rule for which files the graph
parses. It does not read a file's contents; ``source_loading`` does.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from pathlib import Path

from .errors import PreparationError
from .tex_commands import TEX_SOURCE_SUFFIXES

# Every TeX-like input is parsed; the generated bibliography is read as TeX as well.
PARSED_SUFFIXES = TEX_SOURCE_SUFFIXES | {".bbl"}
# Files that graphics tools generate and TeX then reads as source.
GENERATED_SOURCE_SUFFIXES = (".pdf_tex", ".eps_tex", ".pstex_t")


def path_comparison_key(value: str) -> str:
    """Key under which paths collide on case-insensitive or normalizing file systems."""
    return unicodedata.normalize("NFC", value).casefold()


def project_files(root: Path) -> dict[str, Path]:
    """Map every project-relative POSIX path (files and links) to its path, in stable order."""
    if not root.is_dir():
        raise PreparationError(f"Source project is not a directory: {root}")
    return {
        path.relative_to(root).as_posix(): path
        for path in sorted(
            root.rglob("*"),
            key=lambda path: (path_comparison_key(path.as_posix()), path.as_posix()),
        )
        if path.is_file() or path.is_symlink()
    }


def known_path_index(names: Iterable[str]) -> set[str]:
    """Every path suffix and directory prefix of ``names``.

    For cheap "could this name a project file anywhere" tests before a lookup
    context is established: ``a/b/c.tex`` contributes ``a/b/c.tex``, ``b/c.tex``,
    ``c.tex``, ``a/`` and ``a/b/``.
    """
    index: set[str] = set()
    for name in names:
        parts = name.split("/")
        index.update("/".join(parts[start:]) for start in range(len(parts)))
        index.update("/".join(parts[:end]) + "/" for end in range(1, len(parts)))
    return index


def is_parsed_source(name: str, path: Path) -> bool:
    """Whether the graph reads this file: TeX source, generated TeX, or a link to learn of."""
    return (
        path.is_symlink()
        or path.suffix.lower() in PARSED_SUFFIXES
        or name.endswith(GENERATED_SOURCE_SUFFIXES)
    )
