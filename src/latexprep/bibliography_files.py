"""Finding and reading the .bib files of a project, within byte and count budgets.

This module owns the one loader that every bibliography feature uses: the integrity
checks, the coverage and field checks, the transformations and the online lookups. A
file that cannot be used becomes a typed ``BibliographyProblem`` instead of an
exception, so each caller decides whether that is a finding, a gap in its evidence or
a refusal. It does not decide what a syntax error means (a returned document may be
invalid) and it never writes.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import NamedTuple

from .bibtex_syntax import (
    MAX_BIB_FILE_BYTES,
    BibDocument,
    BibFileReader,
    describe_read_failure,
    mebibytes,
)
from .cancellation import cancellation_point


class ProblemKind(StrEnum):
    """Why one declared bibliography file, or the set of them, could not be used.

    Attributes:
        LINKED: The file is, or lies behind, a symbolic link.
        NOT_A_FILE: The file is missing or is not a regular file.
        TOO_LARGE: The file exceeds the byte budget.
        UNREADABLE: Reading the file failed.
        NOT_UTF8: The file is not valid UTF-8.
        TOO_MANY_FILES: The set holds more files than the budget allows.
        TOO_MANY_ENTRIES: The documents together hold more records than the budget allows.
    """

    LINKED = "linked"
    NOT_A_FILE = "not_a_file"
    TOO_LARGE = "too_large"
    UNREADABLE = "unreadable"
    NOT_UTF8 = "not_utf8"
    TOO_MANY_FILES = "too_many_files"
    TOO_MANY_ENTRIES = "too_many_entries"


@dataclass(frozen=True, slots=True)
class BibliographyProblem:
    """A file that was not read, or a limit of the whole set that was reached.

    Attributes:
        kind: Why the file or the set could not be used.
        detail: A phrase that follows the file name ("is not valid UTF-8 (…)"), or a
            complete sentence for a problem of the whole set.
        name: The file's project-relative name, or ``None`` for a problem of the whole set.
    """

    kind: ProblemKind
    detail: str
    name: str | None = None

    @property
    def message(self) -> str:
        """The detail, preceded by the file name when the problem has one."""
        return self.detail if self.name is None else f"{self.name} {self.detail}"


@dataclass(frozen=True, slots=True)
class BibliographyBudget:
    """How much one read may take: bytes per file and in total, files and records.

    Attributes:
        max_file_bytes: The most bytes one file may hold.
        max_total_bytes: The most bytes all files may hold together, or ``None`` for no
            cumulative limit.
        max_files: The most files that are read, or ``None`` for no limit.
        max_records: The most entries, ``@string`` and ``@preamble`` declarations all
            documents may hold together, or ``None`` for no limit.
    """

    max_file_bytes: int = MAX_BIB_FILE_BYTES
    max_total_bytes: int | None = None
    max_files: int | None = None
    max_records: int | None = None

    def describe(self) -> str:
        """Describe the byte limits as a phrase for messages, built from the limits themselves.

        Returns:
            The per-file limit and, when set, the total limit, in parentheses.
        """
        text = f"{mebibytes(self.max_file_bytes)} per file"
        if self.max_total_bytes is not None:
            text += f", {mebibytes(self.max_total_bytes)} in total"
        return f"({text})"


class BibliographyLoad(NamedTuple):
    """The documents that were read, valid or not, and what stopped the others.

    Attributes:
        documents: The documents that were read, including those with syntax errors.
        problems: The files that were skipped and the limits of the set that were reached.
    """

    documents: list[BibDocument]
    problems: list[BibliographyProblem]


def discover_bibliographies(root: Path) -> list[str]:
    """List the project-relative names of every .bib file or link below ``root``, sorted."""
    return sorted(
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.suffix.lower() == ".bib" and (path.is_symlink() or path.is_file())
    )


def read_declared_bibliographies(
    root: Path, names: Iterable[str], budget: BibliographyBudget
) -> BibliographyLoad:
    """Read the named files under ``root`` in name order until a budget runs out.

    A file behind a link, a missing or special file, an unreadable or non-UTF-8
    file and a file over its byte allowance are skipped with a problem. Reaching the
    file or record limit adds a problem for the set and stops reading. A document
    with a syntax error is returned, because only the caller knows whether that
    makes it unusable.

    Args:
        root: The project directory the names are relative to.
        names: The project-relative names of the declared files.
        budget: The byte, file and record limits of this read.

    Returns:
        The documents that were read and the problems that stopped the others.
    """
    reader = BibFileReader(
        max_file_bytes=budget.max_file_bytes, max_total_bytes=budget.max_total_bytes
    )
    documents: list[BibDocument] = []
    problems: list[BibliographyProblem] = []
    records = 0
    for index, name in enumerate(sorted(names)):
        cancellation_point()
        if budget.max_files is not None and index >= budget.max_files:
            detail = f"Bibliography resource count exceeds {budget.max_files}"
            problems.append(BibliographyProblem(ProblemKind.TOO_MANY_FILES, detail))
            break
        document = _read_one(root, name, reader, budget, problems)
        if document is None:
            continue
        records += len(document.records)
        if budget.max_records is not None and records > budget.max_records:
            detail = f"Bibliography entry count exceeds {budget.max_records}"
            problems.append(BibliographyProblem(ProblemKind.TOO_MANY_ENTRIES, detail))
            break
        documents.append(document)
    return BibliographyLoad(documents, problems)


def _read_one(
    root: Path,
    name: str,
    reader: BibFileReader,
    budget: BibliographyBudget,
    problems: list[BibliographyProblem],
) -> BibDocument | None:
    """Read one named file, or record why it cannot be used.

    Args:
        root: The project directory the name is relative to.
        name: The project-relative name of the file.
        reader: The reader that keeps the cumulative byte allowance.
        budget: The limits whose description a byte-budget problem names.
        problems: The list that receives the problem when the file is skipped.

    Returns:
        The document, valid or not, or ``None`` when the file was skipped.
    """
    path = root / name
    if _has_link_below(root, name):
        detail = "is, or lies behind, a symbolic link, which is not supported"
        problems.append(BibliographyProblem(ProblemKind.LINKED, detail, name))
        return None
    if not path.is_file():
        detail = "is missing or is not a regular file"
        problems.append(BibliographyProblem(ProblemKind.NOT_A_FILE, detail, name))
        return None
    try:
        document = reader.read(path, name)
    except UnicodeError as error:
        problems.append(
            BibliographyProblem(ProblemKind.NOT_UTF8, describe_read_failure(error), name)
        )
        return None
    except OSError as error:
        problems.append(
            BibliographyProblem(ProblemKind.UNREADABLE, describe_read_failure(error), name)
        )
        return None
    if document is None:
        detail = f"exceeds the bibliography byte budget {budget.describe()}"
        problems.append(BibliographyProblem(ProblemKind.TOO_LARGE, detail, name))
    return document


def _has_link_below(root: Path, name: str) -> bool:
    """Return whether the file or a directory between it and ``root`` is a symbolic link.

    ``root`` itself may be reached through a link: it is the project the user named.

    Args:
        root: The project directory.
        name: The file's path relative to ``root``.

    Returns:
        Whether a symbolic link lies on the path below ``root``.
    """
    parts = Path(name).parts
    return any(root.joinpath(*parts[:count]).is_symlink() for count in range(1, len(parts) + 1))
