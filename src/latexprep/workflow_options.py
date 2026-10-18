"""Options for packaging several documents and for comparing PDFs, independent of any publisher.

Owns the settings tables ``workflow`` (documents, baseline builds, comparison tolerance) and
``package`` (extra files to ship). It does not run workflows or comparisons; the job does.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import ClassVar

from .errors import PreparationError, SettingsError
from .option_config import (
    EIGHT_BIT_VALUE,
    FRACTION,
    Bounds,
    require_records,
    require_text,
    require_unique_strings,
    validate_scalars,
)
from .path_safety import is_relative_posix_path
from .setting_choices import Engine

# Bounds on a workflow: baseline builds that confirm the source builds repeatably, and
# the documents of one job.
MAX_BASELINE_RUNS = 5
MAX_DOCUMENTS = 32
MAX_DOCUMENT_NAME_CHARACTERS = 64
_DOCUMENT_NAME = re.compile(rf"[A-Za-z0-9][A-Za-z0-9_-]{{0,{MAX_DOCUMENT_NAME_CHARACTERS - 1}}}")


@dataclass(frozen=True)
class ComparisonOptions:
    """How much a rendered page may differ from its reference before it counts as changed.

    ``channel_tolerance`` is the per-pixel difference of a color channel (0 to 255) that
    is still treated as equal; ``max_changed_pixel_ratio`` is the share of a page's pixels
    (0 to 1) that may differ. The defaults demand an exact match.
    """

    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        "channel_tolerance": EIGHT_BIT_VALUE,
        "max_changed_pixel_ratio": FRACTION,
    }

    channel_tolerance: int = 0
    max_changed_pixel_ratio: float = 0.0

    def __post_init__(self) -> None:
        validate_scalars(self)


def _relative(value: str, label: str, *, directory: bool = False) -> None:
    """Require a nonempty relative POSIX path without ``..``; a directory may be ``.``."""
    if not isinstance(value, str) or not is_relative_posix_path(
        value, normalized=False, allow_current=directory, characters="no_controls"
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
        require_unique_strings("include", self.include, allow_surrounding_whitespace=True)
        for path in self.include:
            _relative(path, "package.include path")
            if any(character in path for character in "*?["):
                raise PreparationError("package.include lists exact paths, not glob patterns")


@dataclass(frozen=True)
class DocumentOptions:
    """One document of a multi-document job: where it lives, how to build it, what to ship.

    ``name`` labels its stages and files in reports. ``main`` is its root ``.tex`` file
    and ``include`` lists extra files or patterns, both relative to ``source_directory``.
    ``engine`` overrides the job's TeX engine; ``reference_pdf`` is the PDF its build is
    compared with. Every path is a relative POSIX path that stays inside the project.
    """

    name: str
    main: str
    source_directory: str = "."
    include: tuple[str, ...] = ()
    engine: Engine | None = None
    reference_pdf: str | None = None

    def __post_init__(self) -> None:
        validate_scalars(self)
        require_text(
            "name",
            self.name,
            expected=f"1 to {MAX_DOCUMENT_NAME_CHARACTERS} letters, digits, underscores or "
            "hyphens, starting with a letter or digit",
            pattern=_DOCUMENT_NAME,
        )
        _relative(self.main, "Document main")
        if not self.main.endswith(".tex"):
            raise PreparationError("Document main must be a .tex source")
        _relative(self.source_directory, "Document source_directory", directory=True)
        require_unique_strings("include", self.include, allow_surrounding_whitespace=True)
        for pattern in self.include:
            _relative(pattern, "Document include pattern")
        require_text(
            "reference_pdf", self.reference_pdf, expected="the path of a PDF", allow_none=True
        )


@dataclass(frozen=True)
class WorkflowOptions:
    """The ``workflow`` settings table: several documents, repeated baselines, tolerance.

    Without ``documents`` the job prepares the single document selected by ``main`` or
    found in the project. ``baseline_runs`` builds the unmodified source that many times
    to confirm the build is repeatable. Document names must differ, ignoring case.
    """

    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {"baseline_runs": Bounds(1, MAX_BASELINE_RUNS)}

    documents: tuple[DocumentOptions, ...] = ()
    baseline_runs: int = 1
    comparison: ComparisonOptions = field(default_factory=ComparisonOptions)

    def __post_init__(self) -> None:
        validate_scalars(self)
        if not isinstance(self.comparison, ComparisonOptions):
            raise SettingsError("comparison must be a settings table", ("comparison",))
        require_records(
            "documents",
            self.documents,
            DocumentOptions,
            max_items=MAX_DOCUMENTS,
            distinct_by=lambda document: document.name.casefold(),
            distinct_description="document name, ignoring case",
        )
