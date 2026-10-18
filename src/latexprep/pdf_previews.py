"""Optional, bounded PNG previews kept outside serializable job reports.

The coordinator runs preview-enabled comparisons in stable stage/document order.
The first affected pages in that order share the page and byte limits. Ordinary
comparison measurements still cover every page, independently of these limits.

Owns the preview budget (``PdfPreviewStore``), the typed per-page descriptors
(``PreviewPage``, ``PreviewSide``) and their report serialization. Does not own how a
comparison finds the affected pages or renders them (``pdf_comparison``).
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Final, Literal

from .errors import PreparationError
from .limits import MIB
from .png_support import MIN_PNG_BYTES, png_dimensions
from .raster_pixels import MAX_RASTER_DIMENSION, MAX_RASTER_PIXELS

MAX_PREVIEW_BYTES: Final = 12 * MIB
DEFAULT_MAX_PAGE_PAIRS: Final = 8

# How far a page pair's preview got: waiting to be captured, the page is absent on this
# side, captured, left out because a shared limit was reached, or its renderer failed.
# A pair whose sides ended differently is partial.
PreviewStatus = Literal["pending", "missing", "captured", "omitted", "unavailable", "partial"]


class PreviewLimitError(PreparationError):
    """An optional preview exceeded its independent storage limit."""


def preview_dimensions(data: bytes) -> tuple[int, int]:
    """Validate the framing and dimensions of internally rendered preview PNG bytes."""
    return png_dimensions(data, max_dimension=MAX_RASTER_DIMENSION, max_pixels=MAX_RASTER_PIXELS)


@dataclass(slots=True)
class PreviewSide:
    """The preview of one side of a page pair: whether the page exists and its image."""

    present: bool
    dimensions_pt: tuple[float, float] | None
    status: PreviewStatus
    image_id: str | None = None
    error: str | None = None
    pixels: tuple[int, int] | None = None

    def to_report(self) -> dict[str, object]:
        row: dict[str, object] = {
            "present": self.present,
            "dimensions_pt": None if self.dimensions_pt is None else list(self.dimensions_pt),
            "image_id": self.image_id,
            "status": self.status,
        }
        if self.error is not None:
            row["error"] = self.error
        if self.pixels is not None:
            row["pixels"] = list(self.pixels)
        return row


@dataclass(slots=True)
class PreviewPage:
    """What changed on one affected page, and the preview of each side."""

    page: int
    text_changed: bool | None
    rendering_changed: bool | None
    text_only: bool
    page_dimensions_changed: bool | None
    page_count_changed: bool
    within_render_tolerance: bool | None
    left: PreviewSide
    right: PreviewSide
    status: PreviewStatus = "pending"
    omission_reason: str | None = None

    def sides(self) -> Iterator[tuple[str, PreviewSide]]:
        yield "left", self.left
        yield "right", self.right

    def to_report(self) -> dict[str, object]:
        row: dict[str, object] = {
            "page": self.page,
            "text_changed": self.text_changed,
            "rendering_changed": self.rendering_changed,
            "text_only": self.text_only,
            "page_dimensions_changed": self.page_dimensions_changed,
            "page_count_changed": self.page_count_changed,
            "within_render_tolerance": self.within_render_tolerance,
            "status": self.status,
            "left": self.left.to_report(),
            "right": self.right.to_report(),
        }
        if self.omission_reason is not None:
            row["omission_reason"] = self.omission_reason
        return row


@dataclass
class PdfPreviewStore:
    """Job-local preview assets; never place ``images`` in a generic Report.

    Limits apply across all comparisons, including failed capture attempts.
    ``summary()`` contains descriptors only and remains safe after scratch cleanup.
    """

    max_page_pairs: int = DEFAULT_MAX_PAGE_PAIRS
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

    def summary(self) -> dict[str, object]:
        """The descriptors of every comparison, with counts of page pairs by outcome."""
        pages = [page for comparison in self._comparisons for page in comparison.pages]

        def count(status: PreviewStatus) -> int:
            return sum(page.status == status for page in pages)

        return {
            "max_page_pairs": self.max_page_pairs,
            "max_total_bytes": self.max_total_bytes,
            "selected_page_pairs": self._selected,
            "captured_page_pairs": count("captured"),
            "omitted_page_pairs": count("omitted"),
            "unavailable_page_pairs": count("unavailable"),
            "partial_page_pairs": count("partial"),
            "image_bytes": self._image_bytes,
            "allocation": "First affected pages in coordinator stage/document order.",
            "comparisons": [collector.summary() for collector in self._comparisons],
        }


@dataclass
class PdfPreviewCollector:
    """A comparison's descriptors and access to the shared asset budget."""

    store: PdfPreviewStore
    stage: str
    document: str | None
    pages: list[PreviewPage] = field(default_factory=list)
    error: str | None = None
    finished: bool = False

    def unavailable(self, message: str) -> None:
        self.error = message
        self.finished = True

    def _status(self) -> str:
        statuses = {page.status for page in self.pages}
        if not self.finished:
            return "incomplete"
        if self.error:
            return "unavailable"
        if not self.pages:
            return "unchanged"
        return next(iter(statuses)) if len(statuses) == 1 else "partial"

    def summary(self) -> dict[str, object]:
        return {
            "stage": self.stage,
            "document": self.document,
            "status": self._status(),
            "affected_pages": len(self.pages),
            "omitted_pages": sum(page.status == "omitted" for page in self.pages),
            "unavailable_pages": sum(page.status == "unavailable" for page in self.pages),
            "pages": [page.to_report() for page in self.pages],
            "error": self.error,
        }
