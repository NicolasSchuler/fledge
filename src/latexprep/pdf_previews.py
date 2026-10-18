"""Optional, bounded PNG previews kept outside serializable job reports.

The coordinator runs preview-enabled comparisons in stable stage/document order.
The first affected pages in that order share the page and byte limits. Ordinary
comparison measurements still cover every page, independently of these limits.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .errors import PreparationError
from .limits import MIB
from .png_support import MIN_PNG_BYTES, png_dimensions
from .raster_pixels import MAX_RASTER_DIMENSION, MAX_RASTER_PIXELS

MAX_PREVIEW_BYTES = 12 * MIB


class PreviewLimitError(PreparationError):
    """An optional preview exceeded its independent storage limit."""


def preview_dimensions(data: bytes) -> tuple[int, int]:
    """Validate the framing and dimensions of internally rendered preview PNG bytes."""
    return png_dimensions(data, max_dimension=MAX_RASTER_DIMENSION, max_pixels=MAX_RASTER_PIXELS)


@dataclass
class PdfPreviewStore:
    """Job-local preview assets; never place ``images`` in a generic Report.

    Limits apply across all comparisons, including failed capture attempts.
    ``summary()`` contains descriptors only and remains safe after scratch cleanup.
    """

    max_page_pairs: int = 8
    max_total_bytes: int = MAX_PREVIEW_BYTES
    images: dict[str, bytes] = field(default_factory=dict, init=False)
    _comparisons: list[PdfPreviewCollector] = field(default_factory=list, init=False)
    _selected: int = field(default=0, init=False)
    _image_bytes: int = field(default=0, init=False)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        if any(
            type(value) is not int or value < 0
            for value in (self.max_page_pairs, self.max_total_bytes)
        ):
            raise PreparationError("PDF preview limits must be non-negative integers")

    def for_comparison(self, stage: str, document: str | None = None) -> PdfPreviewCollector:
        collector = PdfPreviewCollector(self, stage, document)
        self._comparisons.append(collector)
        return collector

    @property
    def remaining_bytes(self) -> int:
        return self.max_total_bytes - self._image_bytes

    @asynccontextmanager
    async def exclusive(self) -> AsyncGenerator[None, None]:
        """Select and capture pages for one comparison at a time, in coordinator order."""
        async with self._lock:
            yield

    def reserve_page(self) -> int | None:
        """The next preview slot, or None once the page or byte limit is reached."""
        if self._selected >= self.max_page_pairs or self.remaining_bytes < MIN_PNG_BYTES:
            return None
        self._selected += 1
        return self._selected

    def add_image(self, slot: int, side: str, data: bytes) -> str:
        preview_dimensions(data)
        if len(data) > self.remaining_bytes:
            raise PreviewLimitError("The shared PDF preview byte limit was reached")
        image_id = f"pdf-preview-{slot:04d}-{side}"
        self.images[image_id] = data
        self._image_bytes += len(data)
        return image_id

    def summary(self) -> dict[str, Any]:
        comparisons = [collector.summary() for collector in self._comparisons]
        pages = [page for comparison in comparisons for page in comparison["pages"]]
        return {
            "max_page_pairs": self.max_page_pairs,
            "max_total_bytes": self.max_total_bytes,
            "selected_page_pairs": self._selected,
            "captured_page_pairs": sum(page["status"] == "captured" for page in pages),
            "omitted_page_pairs": sum(page["status"] == "omitted" for page in pages),
            "unavailable_page_pairs": sum(page["status"] == "unavailable" for page in pages),
            "partial_page_pairs": sum(page["status"] == "partial" for page in pages),
            "image_bytes": self._image_bytes,
            "allocation": "First affected pages in coordinator stage/document order.",
            "comparisons": comparisons,
        }


@dataclass
class PdfPreviewCollector:
    """A comparison's descriptors and access to the shared asset budget."""

    store: PdfPreviewStore
    stage: str
    document: str | None
    pages: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    finished: bool = False

    def unavailable(self, message: str) -> None:
        self.error = message
        self.finished = True

    def summary(self) -> dict[str, Any]:
        statuses = {page["status"] for page in self.pages}
        if not self.finished:
            status = "incomplete"
        elif self.error:
            status = "unavailable"
        elif not self.pages:
            status = "unchanged"
        elif statuses == {"captured"}:
            status = "captured"
        elif len(statuses) == 1:
            status = next(iter(statuses))
        else:
            status = "partial"
        return {
            "stage": self.stage,
            "document": self.document,
            "status": status,
            "affected_pages": len(self.pages),
            "omitted_pages": sum(page["status"] == "omitted" for page in self.pages),
            "unavailable_pages": sum(page["status"] == "unavailable" for page in self.pages),
            "pages": deepcopy(self.pages),
            "error": self.error,
        }
