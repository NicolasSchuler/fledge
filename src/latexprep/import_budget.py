"""The limits that bound a project import, and the running count of what an import has used.

``ImportLimits`` states how many entries, bytes, folder levels and what compression ratio
an import accepts; ``ImportBudget`` counts one import against them and also enforces its
time limit, and every refusal says which limit was hit and what to remove. Reading a
folder or archive is ``project``'s work and ``zip_validation``'s.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import ClassVar, Protocol, Self

from .cancellation import cancellation_point
from .errors import PreparationError
from .file_safety import byte_count_text
from .limits import DEFAULT_MAX_FOLDER_LEVELS, DEFAULT_MAX_PROJECT_BYTES, MAX_PROJECT_ENTRIES
from .option_config import POSITIVE_INTEGER, Bounds, validate_scalars

IMPORT_TIMEOUT_SECONDS = 60.0
# Largest ratio of an archive entry's size to its compressed size; real text, source
# and figure files stay far below it, while a decompression bomb exceeds it by orders.
DEFAULT_MAX_COMPRESSION_RATIO = 200


class ImportLimitSettings(Protocol):
    """The settings that bound a project import; ``config.Settings`` provides them."""

    @property
    def max_files(self) -> int:
        """The most files and folders together that an import accepts."""
        ...

    @property
    def import_byte_limit(self) -> int:
        """The combined size of the imported files: ``max_import_bytes``, else ``max_bytes``."""
        ...

    @property
    def max_depth(self) -> int:
        """The deepest folder nesting that an import accepts."""
        ...


@dataclass(frozen=True)
class ImportLimits:
    """Import bounds; max_files counts directory entries as well as files.

    Construction raises ``SettingsError`` when a limit is not a positive integer.

    Attributes:
        FIELD_BOUNDS: The bounds :func:`validate_scalars` applies to the four limits; each
            must be a positive integer.
        max_files: The most entries, files and folders together, that an import accepts.
        max_bytes: The combined size in bytes of the imported files.
        max_depth: The deepest folder nesting that an import accepts.
        max_ratio: The largest ratio of an archive entry's size to its compressed size.
    """

    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = dict.fromkeys(
        ("max_files", "max_bytes", "max_depth", "max_ratio"), POSITIVE_INTEGER
    )

    max_files: int = MAX_PROJECT_ENTRIES
    max_bytes: int = DEFAULT_MAX_PROJECT_BYTES
    max_depth: int = DEFAULT_MAX_FOLDER_LEVELS
    max_ratio: int = DEFAULT_MAX_COMPRESSION_RATIO

    def __post_init__(self) -> None:
        validate_scalars(self)

    @classmethod
    def from_settings(cls, settings: ImportLimitSettings, *, max_ratio: int | None = None) -> Self:
        """Build the limits that a job's ``max_files``, import byte limit and ``max_depth`` set.

        The byte limit is ``max_import_bytes``, which is ``max_bytes`` unless set.

        Args:
            settings: The settings that bound the import.
            max_ratio: The largest accepted compression ratio; the default is kept unless
                it is given.

        Returns:
            The validated limits.
        """
        bounds = {
            "max_files": settings.max_files,
            "max_bytes": settings.import_byte_limit,
            "max_depth": settings.max_depth,
        }
        return cls(**bounds) if max_ratio is None else cls(**bounds, max_ratio=max_ratio)


def entry_limit_error(max_files: int) -> PreparationError:
    """Build the error for a project with more than ``max_files`` files and folders."""
    return PreparationError(
        f"Project exceeds the {max_files} entry limit (files and folders together). "
        "Remove files or folders that are not needed and run again."
    )


@dataclass
class ImportBudget:
    """What one import has used so far against its limits; each use may raise.

    Attributes:
        limits: The limits the import is counted against.
        entries: The files and folders counted so far.
        total_bytes: The bytes counted so far.
        started: The monotonic clock reading when the import started.
    """

    limits: ImportLimits
    entries: int = 0
    total_bytes: int = 0
    started: float = field(default_factory=time.monotonic)

    def check_time(self) -> None:
        """Stop the import once it is cancelled or its time limit has passed.

        Raises:
            AnalysisCancelled: The worker running the import was cancelled.
            PreparationError: The import has run longer than ``IMPORT_TIMEOUT_SECONDS``.
        """
        cancellation_point()
        if time.monotonic() - self.started > IMPORT_TIMEOUT_SECONDS:
            raise PreparationError(
                f"Project import exceeded the {IMPORT_TIMEOUT_SECONDS:g}-second processing "
                "limit. Remove large or numerous files that are not needed and run again."
            )

    def add_entry(self, path: str) -> None:
        """Count one file or folder of the import.

        Args:
            path: The entry's project path; each ``/``-separated part counts as a level.

        Raises:
            AnalysisCancelled: The worker running the import was cancelled.
            PreparationError: The time, entry or nesting limit is exceeded.
        """
        self.check_time()
        self.entries += 1
        if self.entries > self.limits.max_files:
            raise entry_limit_error(self.limits.max_files)
        if len(path.split("/")) > self.limits.max_depth:
            raise PreparationError(
                f"Project path exceeds the nesting limit of {self.limits.max_depth} folder "
                f"levels: {path!r}. Move the files closer to the project root."
            )

    def add_bytes(self, count: int, path: str) -> None:
        """Count bytes of one imported file against the size limit.

        Args:
            count: The number of bytes to add.
            path: The file the bytes belong to, named in the error.

        Raises:
            AnalysisCancelled: The worker running the import was cancelled.
            PreparationError: The time or size limit is exceeded.
        """
        self.check_time()
        self.total_bytes += count
        if self.total_bytes > self.limits.max_bytes:
            raise PreparationError(
                f"Project exceeds the {byte_count_text(self.limits.max_bytes)} size limit at "
                f"{path!r}. Remove large files that are not needed, such as build output or "
                "data sets, and run again."
            )
