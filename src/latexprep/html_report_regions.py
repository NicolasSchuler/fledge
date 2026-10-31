"""SVG diagrams of the page regions a PDF finding reports, for the HTML report.

Draws only the rectangles and page size that the finding recorded; it never embeds or
modifies the PDF. The coordinates are PDF points with a top-left origin.
"""

from __future__ import annotations

import math
from typing import TypeGuard
from urllib.parse import quote

from .findings import DetailKey, Finding
from .html_text import escape_html
from .path_safety import is_relative_posix_path
from .report import RedactedReport

# Rectangles and page sizes beyond this many points are not plausible PDF geometry.
MAX_COORDINATE_POINTS = 1_000_000
# Boxes, and violations with boxes, drawn for one finding.
MAX_DRAWN_ITEMS = 100


def _is_coordinate(value: object) -> TypeGuard[int | float]:
    """Return whether ``value`` is a finite integer or float; a boolean is not a coordinate."""
    return isinstance(value, (int, float)) and type(value) in {int, float} and math.isfinite(value)


def _boxes(value: object) -> list[tuple[float, float, float, float]]:
    """Collect every (left, top, right, bottom) box in a nested list of coordinates.

    A box without area or beyond ``MAX_COORDINATE_POINTS`` is left out, and only the
    first ``MAX_DRAWN_ITEMS`` items of each list are searched.
    """
    if not isinstance(value, (tuple, list)):
        return []
    if len(value) == 4 and all(_is_coordinate(v) for v in value):
        left, top, right, bottom = map(float, value)
        if (
            0 <= left < right <= MAX_COORDINATE_POINTS
            and 0 <= top < bottom <= MAX_COORDINATE_POINTS
        ):
            return [(left, top, right, bottom)]
        return []
    return [box for item in value[:MAX_DRAWN_ITEMS] for box in _boxes(item)]


def _pdf_link(path: object, page: int) -> str | None:
    """Link to ``page`` of the PDF at ``path``.

    A path with one leading slash is checked without it but linked as given.

    Args:
        path: The path recorded for the PDF.
        page: The page to open, counted from 1.

    Returns:
        The quoted path with a ``#page=`` fragment, or ``None`` when ``path`` is not text
        ending in ``.pdf`` that forms a relative POSIX path.
    """
    if not isinstance(path, str) or not path.lower().endswith(".pdf"):
        return None
    relative = path[1:] if path.startswith("/") and not path.startswith("//") else path
    if not is_relative_posix_path(relative, characters="literal"):
        return None
    return quote(path, safe="/") + f"#page={page}"


def _plausible_size(size: object) -> tuple[float, float] | None:
    """Read a (width, height) pair of positive, finite, plausible page dimensions, or ``None``."""
    if isinstance(size, (list, tuple)) and len(size) == 2:
        width, height = size
        if (
            _is_coordinate(width)
            and _is_coordinate(height)
            and 0 < width <= MAX_COORDINATE_POINTS
            and 0 < height <= MAX_COORDINATE_POINTS
        ):
            return float(width), float(height)
    return None


def page_size_from_evidence(
    report: RedactedReport, finding: Finding, page: int
) -> tuple[float, float] | None:
    """Find the recorded size of ``page`` from findings of the same document and stage.

    Geometry from another document or stage must not be borrowed for this page.

    Args:
        report: The report whose findings may record page dimensions.
        finding: The finding whose document and stage the evidence must share.
        page: The page number, counted from 1.

    Returns:
        The page's width and height in points, or ``None`` when no finding records them.
    """
    for evidence in report.findings:
        if evidence.stage != finding.stage or evidence.document != finding.document:
            continue
        sizes = evidence.details.get(DetailKey.DIMENSIONS_PT)
        if (
            isinstance(sizes, (list, tuple))
            and len(sizes) >= page
            and (size := _plausible_size(sizes[page - 1])) is not None
        ):
            return size
    return None


def _region_figure(item: dict, finding: Finding, report: RedactedReport) -> str:
    """Draw the figure for one finding or violation record, or an empty string without regions.

    Args:
        item: The details of the finding or one of its violation records.
        finding: The finding, whose document, stage and path locate the page.
        report: The report whose findings record page sizes and whose ``pdf`` artifact is
            the link of last resort.

    Returns:
        The ``<figure>`` HTML, or an empty string without a valid page and box.
    """
    page = item.get(DetailKey.PAGE)
    if type(page) is not int or page < 1:
        return ""
    boxes = _boxes(item.get(DetailKey.BOUNDS_PT, item.get(DetailKey.BBOX)))
    if not boxes:
        return ""
    recorded = page_size_from_evidence(report, finding, page)
    width, height = recorded or (max(box[2] for box in boxes), max(box[3] for box in boxes))
    regions = "".join(
        f'<rect x="{left:g}" y="{top:g}" width="{right - left:g}" '
        f'height="{bottom - top:g}" class="region"/>'
        for left, top, right, bottom in boxes
    )
    link = _pdf_link(finding.path, page) or _pdf_link(report.artifacts.get("pdf"), page)
    size_note = (
        "Page size recorded." if recorded else "Coordinate extent only; page size unavailable."
    )
    link_note = f' <a href="{escape_html(link)}">Open local PDF at page {page}</a>.' if link else ""
    return (
        f'<figure><svg role="img" aria-label="Page {page} reported regions" '
        f'viewBox="0 0 {width:g} {height:g}" xmlns="http://www.w3.org/2000/svg">'
        f'<rect width="{width:g}" height="{height:g}" class="paper"/>{regions}</svg>'
        f"<figcaption>Page {page}: reported regions in PDF points (top-left origin). "
        f"{size_note}{link_note} The diagram does not embed or modify the PDF."
        "</figcaption></figure>"
    )


def regions_html(finding: Finding, report: RedactedReport) -> str:
    """Draw diagrams of the regions recorded in a finding and in its listed violations.

    Args:
        finding: The finding whose details and violations are drawn.
        report: The report the finding belongs to.

    Returns:
        The figures' HTML, or an empty string when nothing records a region.
    """
    candidates = [finding.details]
    violations = finding.details.get(DetailKey.VIOLATIONS)
    if isinstance(violations, (list, tuple)):
        candidates += [item for item in violations[:MAX_DRAWN_ITEMS] if isinstance(item, dict)]
    return "".join(_region_figure(item, finding, report) for item in candidates)
