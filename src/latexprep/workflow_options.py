"""Explicit document packages and preservation policy, independent of any publisher."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath

from .errors import PreparationError
from .setting_choices import ENGINES, Engine


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
class PackageOptions:
    """Extra project files to ship although the build does not read them.

    Paths are relative to the input project (or a document's source directory)
    and follow the flat filename map. They are packaging choices, not checks.
    """

    include: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not isinstance(self.include, tuple) or len(set(self.include)) != len(self.include):
            raise PreparationError("package.include must be an array of unique relative paths")
        for path in self.include:
            _relative(path, "package.include path")
            if any(character in path for character in "*?["):
                raise PreparationError("package.include lists exact paths, not glob patterns")


@dataclass(frozen=True)
class DocumentOptions:
    name: str
    main: str
    source_directory: str = "."
    include: tuple[str, ...] = ()
    engine: Engine | None = None
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
        if self.engine is not None and self.engine not in ENGINES:
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
