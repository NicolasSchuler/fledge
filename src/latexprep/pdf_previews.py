"""Optional, bounded PNG previews kept outside serializable job reports.

The coordinator runs preview-enabled comparisons in stable stage/document order.
The first affected pages in that order share the page and byte limits. Ordinary
comparison measurements still cover every page, independently of these limits.
"""

from __future__ import annotations

import asyncio
from copy import deepcopy
from dataclasses import dataclass, field
from typing import Any

from .models import PreparationError

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"


class PreviewLimitError(PreparationError):
    """An optional preview exceeded its independent storage limit."""


def png_dimensions(data: bytes) -> tuple[int, int]:
    """Validate the framing and dimensions of internally rendered PNG bytes."""
    if (
        len(data) < 57
        or not data.startswith(PNG_SIGNATURE + b"\x00\x00\x00\rIHDR")
        or not data.endswith(_PNG_END)
    ):
        raise PreparationError("Preview renderer did not produce a complete PNG image")
    width, height = int.from_bytes(data[16:20]), int.from_bytes(data[20:24])
    if min(width, height) < 1 or max(width, height) > 8192 or width * height > 16_000_000:
        raise PreparationError("Preview renderer produced oversized PNG dimensions")
    return width, height


@dataclass
class PdfPreviewStore:
    """Job-local preview assets; never place ``images`` in a generic Report.

    Limits apply across all comparisons, including failed capture attempts.
    ``summary()`` contains descriptors only and remains safe after scratch cleanup.
    """

    max_page_pairs: int = 8
    max_total_bytes: int = 12 * 1024 * 1024
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

    def reserve_page(self) -> int | None:
        if self.store._selected >= self.store.max_page_pairs or self.store.remaining_bytes < 57:
            return None
        self.store._selected += 1
        return self.store._selected

    def add_image(self, slot: int, side: str, data: bytes) -> str:
        png_dimensions(data)
        if len(data) > self.store.remaining_bytes:
            raise PreviewLimitError("The shared PDF preview byte limit was reached")
        image_id = f"pdf-preview-{slot:04d}-{side}"
        self.store.images[image_id] = data
        self.store._image_bytes += len(data)
        return image_id

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
