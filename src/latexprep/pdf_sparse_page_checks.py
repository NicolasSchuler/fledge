"""Sparse-page measurement: how much of each rendered page carries ink.

Each page is rendered to a small gray raster and compared with the configured paper
tone; pages with too little ink are candidates for spill-over or blank pages. The
measurement speaks only for rendered pixels, so intentional blank or image-only pages
are never inferred. A page that cannot be rendered is reported unavailable, and pages
that fail for the same reason are reported once. The renderer and its limits belong to
``pdf_tools`` and ``pdf_geometry``.
"""

from __future__ import annotations

from collections.abc import Collection
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import NamedTuple

from .errors import PreparationError
from .findings import Finding
from .measurement_findings import heuristic_finding, unavailable_finding
from .pdf_check_options import PdfCheckOptions
from .pdf_geometry import (
    MAX_MEASUREMENT_DIMENSION,
    MAX_MEASUREMENT_PIXELS,
    exceeds_measurement_raster,
    measurement_raster_size,
    read_measurement_raster,
)
from .pdf_rule_names import PdfRule
from .pdf_tool_output import parse_page_sizes, require_rendered_pages
from .pdf_tools import PdfToolSession, page_boxes_argv
from .raster_pixels import InkMask

# A render whose pixel size differs from the page-size computation by more than this is not
# the page that was measured.
_RASTER_SIZE_TOLERANCE_PX = 1


class InkCoverage(NamedTuple):
    """The pixel size of one rendered page and the fraction of its pixels that carry ink.

    Attributes:
        width: The width of the render, in pixels.
        height: The height of the render, in pixels.
        ratio: The fraction of the pixels that carry ink.
    """

    width: int
    height: int
    ratio: float


def _within_tolerance(actual: tuple[int, int], expected: tuple[int, int]) -> bool:
    """Return whether a render's pixel size is the expected one, give or take a pixel."""
    return all(
        abs(measured - target) <= _RASTER_SIZE_TOLERANCE_PX
        for measured, target in zip(actual, expected, strict=True)
    )


def _ink_coverage(
    path: Path, *, expected: tuple[int, int], options: PdfCheckOptions, max_file_bytes: int
) -> InkCoverage:
    """Measure the share of a rendered page's pixels that carry ink.

    Args:
        path: The grayscale render.
        expected: The pixel size the render should have, in either orientation.
        options: The PDF check options, which give the paper gray and the ink difference.
        max_file_bytes: The largest render file accepted.

    Returns:
        The coverage.

    Raises:
        PreparationError: The render is invalid or its size disagrees with the page-size
            preflight.
    """
    image = read_measurement_raster(path, channels=1, max_file_bytes=max_file_bytes)
    size = (image.width, image.height)
    # A render may come out rotated, so the page counts if either orientation matches.
    if not (_within_tolerance(size, expected) or _within_tolerance(size[::-1], expected)):
        raise PreparationError("Rendered dimensions disagree with the page-size preflight")
    ink = InkMask.from_gray(image, options.background_gray, options.ink_difference).count()
    return InkCoverage(image.width, image.height, ink / len(image.data))


def _percent(ratio: float) -> str:
    """Format a ratio as a percentage with three significant digits."""
    return f"{ratio * 100:.3g}%"


def _page_ranges(pages: list[int]) -> str:
    """Format page numbers as ascending ranges, such as ``1-3, 5``."""
    ranges: list[list[int]] = []
    for page in sorted(pages):
        if ranges and page == ranges[-1][1] + 1:
            ranges[-1][1] = page
        else:
            ranges.append([page, page])
    return ", ".join(str(first) if first == last else f"{first}-{last}" for first, last in ranges)


def _merge_page_unavailability(findings: list[Finding]) -> list[Finding]:
    """Keep one finding for each distinct reason pages could not be measured, naming the pages.

    A merged finding takes the place of the first finding with its reason; other findings
    keep their order.
    """

    def unavailable_page(finding: Finding) -> int | None:
        """Return the page of an inconclusive finding about one page, else ``None``."""
        page = finding.details.get("page")
        return page if finding.status == "inconclusive" and type(page) is int else None

    pages_by_message: dict[str, list[int]] = {}
    for finding in findings:
        if (page := unavailable_page(finding)) is not None:
            pages_by_message.setdefault(finding.message, []).append(page)
    result = []
    for finding in findings:
        if unavailable_page(finding) is None:
            result.append(finding)
        elif (pages := pages_by_message.pop(finding.message, None)) is not None:
            if len(pages) == 1:
                label, details = f"page {pages[0]}", finding.details
            else:
                label = f"pages {_page_ranges(pages)}"
                details = {k: v for k, v in finding.details.items() if k != "page"}
                details["pages"] = pages
            result.append(replace(finding, message=f"{finding.message} ({label})", details=details))
    return result


def _unavailable_page(page: int, error: Exception) -> Finding:
    """Report that ``page`` could not be measured, because of ``error``."""
    return unavailable_finding(
        PdfRule.SPARSE_PAGES, error, severity="warning", details={"page": page}
    )


def _coverage_outcome(page: int, ratio: float, minimum: float | None) -> tuple[str, list[object]]:
    """Word the message about one page's ink coverage and, below the minimum, its violation.

    Args:
        page: The page number.
        ratio: The fraction of the page's pixels that carry ink.
        minimum: The smallest fraction allowed, or ``None``.

    Returns:
        The message, and the violation as the only item of a list when there is one.
    """
    ink = f"Page {page} has {_percent(ratio)} inked pixels"
    if minimum is None:
        return f"{ink}.", []
    if ratio >= minimum:
        return f"{ink}; the minimum is {_percent(minimum)}.", []
    violation = {
        "page": page,
        "ink_ratio": ratio,
        "minimum": minimum,
        "reason": f"{_percent(ratio)} inked pixels against a minimum of {_percent(minimum)}",
    }
    return f"{ink}, below the configured minimum of {_percent(minimum)}.", [violation]


async def _sparse_page_finding(
    session: PdfToolSession,
    page: int,
    pages: int,
    size: tuple[float, float],
    options: PdfCheckOptions,
) -> Finding:
    """Render one page and measure its ink coverage.

    Args:
        session: The PDF tool session that renders the page.
        page: The page number.
        pages: The number of pages of the PDF.
        size: The width and height of the page, in points.
        options: The PDF check options.

    Returns:
        The finding; unavailable when the render would exceed the limits or fails.
    """
    expected = measurement_raster_size(size, options.raster_dpi)
    if exceeds_measurement_raster(expected):
        return _unavailable_page(
            page,
            PreparationError(
                f"A {size[0]:g} x {size[1]:g} point page would need a "
                f"{expected[0]:,} x {expected[1]:,} pixel render at {options.raster_dpi} DPI, "
                f"above the limit of {MAX_MEASUREMENT_PIXELS:,} pixels "
                f"({MAX_MEASUREMENT_DIMENSION:,} per side); lower raster_dpi to measure it"
            ),
        )
    try:
        coverage = await session.render_page(
            f"raster-{page}",
            page,
            options.raster_dpi,
            partial(
                _ink_coverage,
                expected=expected,
                options=options,
                max_file_bytes=session.runner.limits.max_file_bytes,
            ),
            raster="gray",
        )
    except (PreparationError, OSError) as error:
        return _unavailable_page(page, error)
    minimum = options.min_page_ink_ratio
    message, violations = _coverage_outcome(page, coverage.ratio, minimum)
    return heuristic_finding(
        PdfRule.SPARSE_PAGES,
        message,
        violations,
        details={
            "page": page,
            "last_page": page == pages,
            "ink_ratio": coverage.ratio,
            "minimum": minimum,
            "raster_pixels": [coverage.width, coverage.height],
            "background_gray": options.background_gray,
            "ink_difference": options.ink_difference,
            "raster_dpi": options.raster_dpi,
            "scope": "Rendered pixel coverage; intentional blank/image-only pages "
            "are not inferred. "
            f"Pages above {MAX_MEASUREMENT_PIXELS:,} pixels "
            f"({MAX_MEASUREMENT_DIMENSION:,} per side) are inconclusive before rendering.",
        },
    )


async def measure_sparse_pages(
    session: PdfToolSession,
    pages: int,
    options: PdfCheckOptions,
    rules: Collection[str] = (),  # noqa: ARG001 - the signature every measurement family shares
) -> list[Finding]:
    """Measure the ink coverage of every page that is not exempt.

    Args:
        session: The PDF tool session that renders the pages.
        pages: The number of pages of the PDF.
        options: The PDF check options.
        rules: Completes the signature every measurement family shares; this one always
            reports under its single rule.

    Returns:
        One finding per measured page, plus unavailable findings for pages that failed.
    """
    try:
        require_rendered_pages(pages, "Rendered sparse-page measurement")
        sizes = await session.measure(
            "raster-sizes",
            page_boxes_argv(pages),
            lambda result: parse_page_sizes(result.stdout, pages),
        )
    except (PreparationError, OSError) as error:
        return [unavailable_finding(PdfRule.SPARSE_PAGES, error, severity="warning")]
    result = []
    if any(page > pages for page in options.sparse_page_exemptions):
        result.append(
            unavailable_finding(
                PdfRule.SPARSE_PAGES,
                PreparationError("A configured sparse-page exemption is outside the document"),
                severity="warning",
            )
        )
    result.extend(
        [
            await _sparse_page_finding(session, page, pages, sizes[page - 1], options)
            for page in range(1, pages + 1)
            if page not in options.sparse_page_exemptions
        ]
    )
    return _merge_page_unavailability(result)
