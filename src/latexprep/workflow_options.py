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

    The defaults demand an exact match. Construction raises ``SettingsError`` when a field
    does not match its annotation or its bounds.

    Attributes:
        FIELD_BOUNDS: The bounds :func:`validate_scalars` applies to ``channel_tolerance``
            and ``max_changed_pixel_ratio``.
        channel_tolerance: The per-pixel difference of a color channel (0 to 255) that is
            still treated as equal.
        max_changed_pixel_ratio: The share of a page's pixels (0 to 1) that may differ.
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
    """Require a nonempty relative POSIX path without ``..``; a directory may be ``.``.

    ASCII control characters are refused as well.

    Args:
        value: The path to check.
        label: The name of the setting, used in the error message.
        directory: Whether ``value`` names a directory, which allows ``.``.

    Raises:
        PreparationError: ``value`` is not a string or not such a path.
    """
    if not isinstance(value, str) or not is_relative_posix_path(
        value, normalized=False, allow_current=directory, characters="no_controls"
    ):
        raise PreparationError(f"{label} must be a nonempty relative POSIX path without '..'")


@dataclass(frozen=True)
class PackageOptions:
    """Extra project files to ship although the build does not read them.

    They are packaging choices, not checks. Construction raises ``SettingsError`` when
    ``include`` is not a tuple of distinct strings, and ``PreparationError`` when a path is
    not a nonempty relative POSIX path without ``..`` or is a glob pattern.

    Attributes:
        include: The exact paths of the files, without repeats and without glob patterns.
            Paths are relative to the input project (or a document's source directory) and
            follow the flat filename map.
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

    Every path is a relative POSIX path that stays inside the project. Construction raises
    ``SettingsError`` when a field does not match its annotation, ``name`` or
    ``reference_pdf`` is not acceptable text, or ``include`` is not a tuple of distinct
    strings, and ``PreparationError`` when a path is not a nonempty relative POSIX path
    without ``..`` or ``main`` is not a ``.tex`` file.

    Attributes:
        name: The label of the document's stages and files in reports: 1 to
            ``MAX_DOCUMENT_NAME_CHARACTERS`` letters, digits, underscores or hyphens,
            starting with a letter or digit.
        main: The document's root ``.tex`` file, relative to ``source_directory``.
        source_directory: The directory that holds the document's sources; it may be
            ``.``.
        include: Extra files or patterns to ship, without repeats, relative to
            ``source_directory``.
        engine: The TeX engine that overrides the job's engine for this document.
        reference_pdf: The PDF the document's build is compared with.
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

    Construction raises ``SettingsError`` when ``baseline_runs`` is outside its bounds,
    ``comparison`` is not a settings table, or ``documents`` is not a tuple of documents
    with distinct names and at most ``MAX_DOCUMENTS`` entries.

    Attributes:
        FIELD_BOUNDS: The bounds :func:`validate_scalars` applies to ``baseline_runs``.
        documents: The documents of the job, at most ``MAX_DOCUMENTS``; their names must
            differ, ignoring case. Without documents the job prepares the single document
            selected by ``main`` or found in the project.
        baseline_runs: How many times to build the unmodified source to confirm the build
            is repeatable, from 1 to ``MAX_BASELINE_RUNS``.
        comparison: How much a rendered page may differ from its reference.
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
