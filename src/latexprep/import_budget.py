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
    def max_files(self) -> int: ...

    @property
    def import_byte_limit(self) -> int: ...

    @property
    def max_depth(self) -> int: ...


@dataclass(frozen=True)
class ImportLimits:
    """Import bounds; max_files counts directory entries as well as files."""

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
        """The limits that a job's ``max_files``, import byte limit and ``max_depth`` set.

        The byte limit is ``max_import_bytes``, which is ``max_bytes`` unless set.

        The compression ratio keeps its default unless ``max_ratio`` is given.
        """
        bounds = {
            "max_files": settings.max_files,
            "max_bytes": settings.import_byte_limit,
            "max_depth": settings.max_depth,
        }
        return cls(**bounds) if max_ratio is None else cls(**bounds, max_ratio=max_ratio)


def entry_limit_error(max_files: int) -> PreparationError:
    return PreparationError(
        f"Project exceeds the {max_files} entry limit (files and folders together). "
        "Remove files or folders that are not needed and run again."
    )


@dataclass
class ImportBudget:
    """What one import has used so far against its limits; each use may raise."""

    limits: ImportLimits
    entries: int = 0
    total_bytes: int = 0
    started: float = field(default_factory=time.monotonic)

    def check_time(self) -> None:
        cancellation_point()
        if time.monotonic() - self.started > IMPORT_TIMEOUT_SECONDS:
            raise PreparationError(
                f"Project import exceeded the {IMPORT_TIMEOUT_SECONDS:g}-second processing "
                "limit. Remove large or numerous files that are not needed and run again."
            )

    def add_entry(self, path: str) -> None:
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
        self.check_time()
        self.total_bytes += count
        if self.total_bytes > self.limits.max_bytes:
            raise PreparationError(
                f"Project exceeds the {byte_count_text(self.limits.max_bytes)} size limit at "
                f"{path!r}. Remove large files that are not needed, such as build output or "
                "data sets, and run again."
            )
