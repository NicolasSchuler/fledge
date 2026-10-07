"""Explicit document packages and preservation policy, independent of any publisher."""

from __future__ import annotations

import fnmatch
import math
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .models import PreparationError
from .scheduler import cancellation_point


@dataclass(frozen=True)
class ComparisonOptions:
    channel_tolerance: int = 0
    max_changed_pixel_ratio: float = 0.0

    def __post_init__(self) -> None:
        if type(self.channel_tolerance) is not int or not 0 <= self.channel_tolerance <= 255:
            raise PreparationError("channel_tolerance must be an integer from 0 to 255")
        ratio = self.max_changed_pixel_ratio
        if type(ratio) not in {int, float} or not math.isfinite(ratio) or not 0 <= ratio <= 1:
            raise PreparationError("max_changed_pixel_ratio must be a number from 0 to 1")


def _relative(value: str, label: str, *, directory: bool = False) -> None:
    if (
        not isinstance(value, str)
        or not value
        or "\\" in value
        or PurePosixPath(value).is_absolute()
        or ".." in PurePosixPath(value).parts
        or any(ord(char) < 32 for char in value)
        or (not directory and value == ".")
    ):
        raise PreparationError(f"{label} must be a nonempty relative POSIX path without '..'")


@dataclass(frozen=True)
class DocumentOptions:
    name: str
    main: str
    source_directory: str = "."
    include: tuple[str, ...] = ()
    engine: str | None = None
    reference_pdf: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not re.fullmatch(
            r"[A-Za-z0-9][A-Za-z0-9_-]{0,63}", self.name
        ):
            raise PreparationError(
                "Document name must use 1–64 letters, digits, underscores or hyphens"
            )
        _relative(self.main, "Document main")
        if not self.main.endswith(".tex"):
            raise PreparationError("Document main must be a .tex source")
        _relative(self.source_directory, "Document source_directory", directory=True)
        if not isinstance(self.include, tuple) or len(set(self.include)) != len(self.include):
            raise PreparationError("Document include must be a tuple of unique relative patterns")
        for pattern in self.include:
            _relative(pattern, "Document include pattern")
        if self.engine not in {None, "pdflatex", "xelatex", "lualatex"}:
            raise PreparationError("Unsupported document engine")
        if self.reference_pdf is not None and (
            not isinstance(self.reference_pdf, str) or not self.reference_pdf.strip()
        ):
            raise PreparationError("Document reference_pdf must name an explicit PDF")


@dataclass(frozen=True)
class WorkflowOptions:
    documents: tuple[DocumentOptions, ...] = ()
    baseline_runs: int = 1
    comparison: ComparisonOptions = field(default_factory=ComparisonOptions)

    def __post_init__(self) -> None:
        if type(self.baseline_runs) is not int or not 1 <= self.baseline_runs <= 5:
            raise PreparationError("baseline_runs must be an integer from 1 to 5")
        if not isinstance(self.comparison, ComparisonOptions):
            raise PreparationError("comparison must be ComparisonOptions")
        if not isinstance(self.documents, tuple) or len(self.documents) > 32:
            raise PreparationError("documents must be a tuple of at most 32 DocumentOptions")
        if any(not isinstance(document, DocumentOptions) for document in self.documents):
            raise PreparationError("Each document must be DocumentOptions")
        names = [document.name.casefold() for document in self.documents]
        if len(set(names)) != len(names):
            raise PreparationError("Document names must be unique, ignoring case")


def stage_document(snapshot: Path, destination: Path, document: DocumentOptions) -> Path:
    """Materialize one explicit file selection from an already validated import."""
    source = snapshot / document.source_directory
    if (
        not source.is_dir()
        or source.is_symlink()
        or not source.resolve().is_relative_to(snapshot.resolve())
    ):
        raise PreparationError(f"Missing or external source_directory for document {document.name}")
    files = sorted(path for path in source.rglob("*") if path.is_file() or path.is_symlink())
    matched: set[str] = set()
    selected: list[tuple[Path, str]] = []
    for path in files:
        cancellation_point()
        relative = path.relative_to(source).as_posix()
        patterns = {
            pattern for pattern in document.include if fnmatch.fnmatchcase(relative, pattern)
        }
        if document.include and not patterns:
            continue
        if path.is_symlink() or not path.resolve().is_relative_to(source.resolve()):
            raise PreparationError(f"Document {document.name} contains an unsafe input")
        matched.update(patterns)
        selected.append((path, relative))
    missing = set(document.include) - matched
    if missing:
        raise PreparationError(
            f"Document {document.name} include patterns matched no files: "
            f"{', '.join(sorted(missing))}"
        )
    if document.main not in {relative for _, relative in selected}:
        raise PreparationError(
            f"Document {document.name} does not include its main file {document.main}"
        )
    destination.mkdir(parents=True)
    for path, relative in selected:
        cancellation_point()
        target = destination / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
    return destination
