"""Render and compare the pages of two PDFs: page ranges per process, remembered digests.

A comparison renders every common page of both PDFs at one resolution (``comparison_dpi``:
``COMPARISON_DPI`` unless a page is too large for it) and asks whether the rasters are
identical, or, with explicit tolerances, how many pixels differ. Two things keep that cheap.
Pages are rendered in ranges, several pages per ``pdftoppm`` process, because a process start
costs about as much as rendering a text page; a range that fails is rendered again page by
page, so a failure is still reported for the page that caused it. And a page whose raster was
seen before is known by a digest (``PageDigests``) that lives as long as the job's PDF session,
so the prepared PDF, which two comparisons of one preparation share, is rendered once, and
equal digests prove equal rasters without reading a second raster.

A page that needs pixel counts (explicit tolerances and differing digests) is rendered once
more when its raster was not kept. Peak memory stays at two rasters of a page pair for every
range in flight; the files of a range wait on disk, bounded by ``CHUNK_MAX_RASTER_BYTES``.

Does not own: the tool session and argument lists (``pdf_tools``), the digests' lifetime
(``pdf_job_sessions``) or the findings a comparison reports (``pdf_comparison``).
"""

from __future__ import annotations

import hashlib
import logging
import math
import shutil
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final

from .cancellation import run_in_thread
from .errors import PreparationError
from .limits import MIB
from .pdf_geometry import COMPARISON_DPI, measurement_raster_size
from .pdf_job_sessions import PageDigests, PageRaster
from .pdf_tools import (
    PdfToolSession,
    gather_measurements,
    raster_filename,
    render_page_argv,
    render_pages_argv,
    rendered_page_files,
)
from .raster_pixels import RasterImage, count_changed_pixels, read_pnm_raster

logger = logging.getLogger(__name__)

# Most pages one ``pdftoppm`` process renders, and the most raster bytes (three per pixel)
# that its pages occupy on disk at once; a page larger than the byte limit is rendered alone.
CHUNK_MAX_PAGES: Final = 8
CHUNK_MAX_RASTER_BYTES: Final = 32 * MIB
# Memory a render needs beyond its two rasters: the renderer process and its buffers.
RENDER_HEADROOM_MB: Final = 128
_RGB_BYTES_PER_PIXEL: Final = 3
# The tool that renders pages, as the session records its version.
_RENDERER: Final = "pdftoppm"
# A range is split into this many parts per concurrent renderer, so that one slow part
# does not leave the others idle.
_PARTS_PER_RENDERER: Final = 2


@dataclass(frozen=True, slots=True)
class PageDifference:
    """How the renders of one page pair differ; absent pairs are identical and not recorded.

    Attributes:
        page: The page number.
        left_pixels: The width and height of the left render.
        right_pixels: The width and height of the right render.
        page_dimensions_changed: Whether the page sizes differ.
        changed_pixels: The number of pixels that differ beyond the channel tolerance.
        changed_pixel_ratio: The share of the page's pixels that differ.
        within_tolerance: Whether that share is within the configured limit.
    """

    page: int
    left_pixels: tuple[int, int]
    right_pixels: tuple[int, int]
    page_dimensions_changed: bool
    # Set only when the pixels were counted: both renders have one size and the page
    # sizes agree, and tolerances were configured.
    changed_pixels: int | None = None
    changed_pixel_ratio: float | None = None
    within_tolerance: bool | None = None

    @property
    def counts_as_changed(self) -> bool:
        """Whether the page differs beyond the configured tolerances (or none were measured)."""
        return not self.within_tolerance

    def to_report(self) -> dict[str, object]:
        """Return the difference as a report row, with the pixel counts when they were counted."""
        row: dict[str, object] = {
            "page": self.page,
            "left_pixels": self.left_pixels,
            "right_pixels": self.right_pixels,
            "page_dimensions_changed": self.page_dimensions_changed,
        }
        if self.changed_pixels is not None:
            row.update(
                changed_pixels=self.changed_pixels,
                changed_pixel_ratio=self.changed_pixel_ratio,
                within_tolerance=self.within_tolerance,
            )
        return row


@dataclass(frozen=True, slots=True)
class PixelTolerance:
    """The explicit tolerances of a comparison that counts changed pixels.

    Attributes:
        max_changed_pixel_ratio: The largest share of a page's pixels that may differ before
            the page counts as changed.
        channel_tolerance: The largest per-channel difference (0 to 255) that still counts
            as the same color.
    """

    max_changed_pixel_ratio: float
    channel_tolerance: int


@dataclass(frozen=True, slots=True)
class RenderSide:
    """One side of a comparison, as far as rendering is concerned.

    Attributes:
        name: The side, ``left`` or ``right``; it names the side's command directories.
        session: The side's view of its PDF session, which runs the renders.
        dimensions: The size of every page in points.
        digests: The digests of the pages of this PDF rendered so far.
        dpi: The resolution of the renders, the same for both sides (``comparison_dpi``).
    """

    name: str
    session: PdfToolSession
    dimensions: list[tuple[float, float]]
    digests: PageDigests
    dpi: int = COMPARISON_DPI


@dataclass(frozen=True, slots=True)
class RenderRequest:
    """What the page ranges of one comparison share.

    Attributes:
        left: The left side.
        right: The right side.
        tolerance: The explicit tolerances, or ``None`` for an exact comparison, which counts
            no pixels.
    """

    left: RenderSide
    right: RenderSide
    tolerance: PixelTolerance | None

    @property
    def sides(self) -> tuple[RenderSide, RenderSide]:
        """Both sides, left first."""
        return self.left, self.right

    @property
    def renderers(self) -> int:
        """How many ranges may be rendered at once."""
        budget = self.left.session.budget
        return 1 if budget is None else max(1, min(budget.render_jobs, budget.cpu_slots))


@dataclass(slots=True)
class RenderOutcome:
    """Page-pair render results of one comparison, collected in page order.

    Attributes:
        differences: The pairs whose renders differ.
        completed_pages: The pages whose pair was rendered.
        failed_pages: The pages whose pair could not be rendered.
        errors: Why those pages could not be rendered.
    """

    differences: list[PageDifference]
    completed_pages: list[int]
    failed_pages: list[int]
    errors: list[str]

    @property
    def changed_pages(self) -> list[int]:
        """The pages that differ beyond the tolerances."""
        return [item.page for item in self.differences if item.counts_as_changed]


def raster_pixel_count(size: tuple[float, float], dpi: int = COMPARISON_DPI) -> int:
    """Return the pixels of a page of ``size`` points rendered at ``dpi``."""
    return math.prod(measurement_raster_size(size, dpi))


def pair_memory_mb(
    left: tuple[float, float], right: tuple[float, float], dpi: int = COMPARISON_DPI
) -> int:
    """Return the memory to reserve for holding both renders of a page pair at ``dpi``, in MiB."""
    pixels = raster_pixel_count(left, dpi) + raster_pixel_count(right, dpi)
    return math.ceil(_RGB_BYTES_PER_PIXEL * pixels / MIB) + RENDER_HEADROOM_MB


def range_page_limit(pages: int, renderers: int) -> int:
    """Return how many pages one render range may hold for ``pages`` pages and ``renderers``.

    A document that the renderers can take a page each keeps its one page per process, so
    nothing that could overlap is merged; a longer one is cut into about two ranges per
    renderer, and into ranges of at most ``CHUNK_MAX_PAGES``.

    Args:
        pages: The number of page pairs to render.
        renderers: How many ranges may be rendered at once.

    Returns:
        The page limit of a range, at least 1.
    """
    return max(1, min(CHUNK_MAX_PAGES, math.ceil(pages / (_PARTS_PER_RENDERER * renderers))))


def plan_render_ranges(
    pages: Iterable[int],
    raster_bytes: Callable[[int], int],
    *,
    max_pages: int,
    max_bytes: int = CHUNK_MAX_RASTER_BYTES,
) -> list[tuple[int, int]]:
    """Group ascending page numbers into runs of consecutive pages that one process renders.

    A run ends at a gap in the numbers, at ``max_pages`` pages, or before the page that would
    take its rasters above ``max_bytes``; a page is never left out.

    Args:
        pages: The pages to render, in ascending order.
        raster_bytes: Returns the size of the raster of a page in bytes.
        max_pages: The most pages of a run.
        max_bytes: The most raster bytes of a run.

    Returns:
        The first and last page of every run, in order.
    """
    ranges: list[tuple[int, int]] = []
    start = previous = 0
    total = 0
    for page in pages:
        size = raster_bytes(page)
        extends = start and page == previous + 1 and page - start < max_pages
        if extends and total + size <= max_bytes:
            previous, total = page, total + size
            continue
        if start:
            ranges.append((start, previous))
        start = previous = page
        total = size
    if start:
        ranges.append((start, previous))
    return ranges


def _directory_name(first: int, last: int) -> str:
    """Name the work directory of a range of pages; a single page keeps its own name."""
    return f"page-{first:04d}" if first == last else f"pages-{first:04d}-{last:04d}"


async def _render_page(side: RenderSide, name: str, page: int) -> Path | PreparationError:
    """Render one page alone and return its raster file.

    Args:
        side: The side to render.
        name: The name of the command's directory below the session's workspace.
        page: The page number.

    Returns:
        The path of the raster file, or the error that stopped the render.
    """
    try:
        await side.session.run_without_lease(
            name,
            render_page_argv(page, side.dpi, "rgb"),
            memory_mb=RENDER_HEADROOM_MB,
        )
    except PreparationError as error:
        return error
    return side.session.work / name / "command" / raster_filename("rgb")


async def _render_run(
    side: RenderSide, name: str, first: int, last: int
) -> dict[int, Path | PreparationError]:
    """Render the pages ``first`` to ``last`` of one side, in one process where that works.

    A range that fails, or that leaves a page without its file, is rendered again page by
    page, so each page reports its own failure.

    Args:
        side: The side to render.
        name: The name of the range's command directory below the session's workspace.
        first: The first page.
        last: The last page.

    Returns:
        The raster file of each page, or the error that stopped its render.
    """
    if first == last:
        return {first: await _render_page(side, f"{name}/{side.name}", first)}
    files: dict[int, Path] = {}
    try:
        await side.session.run_without_lease(
            f"{name}/{side.name}",
            render_pages_argv(first, last, side.dpi, "rgb"),
            memory_mb=RENDER_HEADROOM_MB,
        )
    except PreparationError as error:
        logger.debug("PDF render %s %d-%d: range failed (%s)", side.name, first, last, error)
    else:
        files = rendered_page_files(
            side.session.work / name / side.name / "command", "rgb", first=first, last=last
        )
    results: dict[int, Path | PreparationError] = dict(files)
    for page in range(first, last + 1):
        if page not in results:
            results[page] = await _render_page(side, f"page-{page:04d}/{side.name}", page)
    return results


def _runs(pages: Sequence[int]) -> list[tuple[int, int]]:
    """Return the runs of consecutive numbers of ascending ``pages``."""
    return plan_render_ranges(pages, lambda _page: 0, max_pages=len(pages) + 1)


async def _render_missing(
    side: RenderSide, name: str, first: int, last: int
) -> dict[int, Path | PreparationError]:
    """Render the pages of ``first`` to ``last`` whose digest the side does not know yet.

    Args:
        side: The side to render.
        name: The name of the range's work directory.
        first: The first page of the range.
        last: The last page of the range.

    Returns:
        The raster file of each page that was rendered, or the error that stopped it.
    """
    missing = [page for page in range(first, last + 1) if side.digests.get(page, side.dpi) is None]
    logger.debug(
        "PDF render %s pages %d-%d: %d to render, %d digests reused",
        side.name,
        first,
        last,
        len(missing),
        last - first + 1 - len(missing),
    )
    rendered: dict[int, Path | PreparationError] = {}
    for run_first, run_last in _runs(missing):
        run_name = name if (run_first, run_last) == (first, last) else f"{name}/{run_first:04d}"
        rendered.update(await _render_run(side, run_name, run_first, run_last))
    return rendered


def _read_page(side: RenderSide, page: int, path: Path) -> RasterImage:
    """Read the raster of a rendered page and remove its file.

    Args:
        side: The side the page belongs to.
        page: The page number.
        path: The raster file.

    Returns:
        The raster.

    Raises:
        PreparationError: The file is missing, unreadable, malformed or larger than the page.
    """
    image = read_pnm_raster(
        path, channels=3, max_pixels=raster_pixel_count(side.dimensions[page - 1], side.dpi)
    )
    path.unlink()
    return image


def _page_raster(
    side: RenderSide,
    page: int,
    rendered: Path | PreparationError | None,
    *,
    keep_image: bool,
) -> tuple[PageRaster, RasterImage | None]:
    """Return what is known of a page's raster, reading its file when it was rendered now.

    Args:
        side: The side the page belongs to.
        page: The page number.
        rendered: The raster file of the page, the error that stopped its render, or ``None``
            when the side's digest of the page was already known.
        keep_image: Whether to return the raster as well as its digest.

    Returns:
        The page's digest and, when asked for and read now, its raster.

    Raises:
        PreparationError: The page could not be rendered or its raster read.
    """
    if rendered is None:
        known = side.digests.get(page, side.dpi)
        if known is None:
            raise PreparationError("The page raster is unknown")
        if side.digests.renderer is not None:
            # The comparison relies on the renderer that produced the remembered digest.
            side.session.versions[_RENDERER] = side.digests.renderer
        return known, None
    if isinstance(rendered, PreparationError):
        raise rendered
    image = _read_page(side, page, rendered)
    raster = PageRaster(image.width, image.height, hashlib.sha256(image.data).digest())
    side.digests.record(page, side.dpi, raster, side.session.versions.get(_RENDERER))
    return raster, image if keep_image else None


async def _image_of(side: RenderSide, page: int, image: RasterImage | None) -> RasterImage:
    """Return the raster of a page, rendering the page alone when it was not kept.

    Args:
        side: The side the page belongs to.
        page: The page number.
        image: The raster read earlier, if any.

    Returns:
        The raster.

    Raises:
        PreparationError: The page could not be rendered or its raster read.
    """
    if image is not None:
        return image
    rendered = await _render_page(side, f"page-{page:04d}/{side.name}-pixels", page)
    if isinstance(rendered, PreparationError):
        raise rendered
    return _read_page(side, page, rendered)


async def _counted_difference(
    request: RenderRequest,
    difference: PageDifference,
    tolerance: PixelTolerance,
    kept: tuple[RasterImage | None, RasterImage | None],
) -> PageDifference:
    """Count the pixels that differ beyond the channel tolerance on a page pair of one size.

    A raster that was not kept is rendered again, alone.

    Args:
        request: What the comparison shares.
        difference: The difference of the pair, whose renders have the same size.
        tolerance: The explicit tolerances.
        kept: The left and right raster, each ``None`` when it was not kept.

    Returns:
        The difference with its pixel counts.

    Raises:
        PreparationError: A raster could not be rendered or read.
    """
    page = difference.page
    left = await _image_of(request.left, page, kept[0])
    right = await _image_of(request.right, page, kept[1])
    changed = await run_in_thread(count_changed_pixels, left, right, tolerance.channel_tolerance)
    ratio = changed / (left.width * left.height)
    return PageDifference(
        page,
        difference.left_pixels,
        difference.right_pixels,
        difference.page_dimensions_changed,
        changed_pixels=changed,
        changed_pixel_ratio=ratio,
        within_tolerance=ratio <= tolerance.max_changed_pixel_ratio,
    )


async def _compare_page(
    request: RenderRequest,
    renders: dict[str, dict[int, Path | PreparationError]],
    page: int,
) -> PageDifference | PreparationError | None:
    """Compare the renders of one page pair.

    Args:
        request: What the comparison shares.
        renders: The raster files rendered for this range, by side name and page.
        page: The page number.

    Returns:
        ``None`` when the renders and page sizes are identical, otherwise the difference;
        the error when a render or its raster could not be obtained.
    """
    left_side, right_side = request.sides
    tolerance = request.tolerance
    try:
        left, left_image = _page_raster(
            left_side, page, renders[left_side.name].get(page), keep_image=tolerance is not None
        )
        right, right_image = _page_raster(
            right_side, page, renders[right_side.name].get(page), keep_image=tolerance is not None
        )
        sizes_changed = left_side.dimensions[page - 1] != right_side.dimensions[page - 1]
        if left == right and not sizes_changed:
            return None
        difference = PageDifference(
            page, (left.width, left.height), (right.width, right.height), sizes_changed
        )
        if tolerance is None or sizes_changed or difference.left_pixels != difference.right_pixels:
            return difference
        return await _counted_difference(request, difference, tolerance, (left_image, right_image))
    except PreparationError as error:
        return error


async def _compare_range(
    request: RenderRequest, first: int, last: int
) -> list[PageDifference | PreparationError | None]:
    """Render and compare the page pairs ``first`` to ``last`` under one lease.

    The range's work directories are removed afterwards, even after a failure.

    Args:
        request: What the comparison shares.
        first: The first page.
        last: The last page.

    Returns:
        The result of every page, in page order.
    """
    name = _directory_name(first, last)
    work = request.left.session.work
    memory_mb = max(
        pair_memory_mb(
            request.left.dimensions[page - 1], request.right.dimensions[page - 1], request.left.dpi
        )
        for page in range(first, last + 1)
    )
    label = f"compare PDF page {first}" if first == last else f"compare PDF pages {first}-{last}"
    async with request.left.session.lease(label, memory_mb=memory_mb, kind="render"):
        try:
            renders = {
                side.name: await _render_missing(side, name, first, last) for side in request.sides
            }
            return [await _compare_page(request, renders, page) for page in range(first, last + 1)]
        finally:
            # Failed and cancelled renderers can leave partial rasters. The runner has
            # reaped the process before returning control to this cleanup.
            for leftover in (
                work / name,
                *(work / f"page-{page:04d}" for page in range(first, last + 1)),
            ):
                if leftover.exists():
                    shutil.rmtree(leftover)


async def render_page_pairs(request: RenderRequest, pages: int) -> RenderOutcome:
    """Render and compare the first ``pages`` page pairs, ranges concurrently, in page order.

    Args:
        request: What the comparison shares.
        pages: How many pages both sides have in common.

    Returns:
        The differences, and which pages were compared or could not be.
    """
    limit = range_page_limit(pages, request.renderers)
    ranges = plan_render_ranges(
        range(1, pages + 1),
        lambda page: (
            _RGB_BYTES_PER_PIXEL
            * max(
                raster_pixel_count(request.left.dimensions[page - 1], request.left.dpi),
                raster_pixel_count(request.right.dimensions[page - 1], request.right.dpi),
            )
        ),
        max_pages=limit,
    )
    results = await gather_measurements(
        [partial(_compare_range, request, first, last) for first, last in ranges],
        request.left.session.budget,
    )
    outcome = RenderOutcome([], [], [], [])
    for (first, last), result in zip(ranges, results, strict=True):
        pages_results = (
            [result] * (last - first + 1) if isinstance(result, PreparationError) else result
        )
        for page, page_result in zip(range(first, last + 1), pages_results, strict=True):
            if isinstance(page_result, PreparationError):
                outcome.errors.append(f"Page {page} rendering: {page_result}")
                outcome.failed_pages.append(page)
                continue
            outcome.completed_pages.append(page)
            if page_result is not None:
                outcome.differences.append(page_result)
    return outcome
