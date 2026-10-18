"""Printer-mark candidates: short crop and registration strokes outside a trim region.

A candidate is a pair of short horizontal and vertical strokes meeting at a trim corner,
or two short strokes crossing outside the trim rectangle. Graphic content can look
identical, so every result is advisory; outlined, curved and raster marks are not
recognised.

Does not own: parsing the trace (``pdf_artwork_trace``) or the other drawing measurements
(``pdf_artwork_drawings``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from typing import NamedTuple

from .cancellation import cancellation_point
from .finding_text import plural
from .findings import Finding
from .measurement_findings import heuristic_finding
from .pdf_artwork_options import ArtworkRule, PdfArtworkOptions
from .pdf_artwork_trace import Drawing, Matrix, Point, TracedArtwork, path_segments
from .pdf_check_options import PdfRegion
from .pdf_geometry import MAX_PAIR_CHECKS, contains

# A report row describing one candidate; its keys are part of the report.
Candidate = dict[str, object]


class Line(NamedTuple):
    """A short straight stroke segment after placement, with its operation number."""

    start: Point
    end: Point
    operation: int


def _short_lines(drawings: Sequence[Drawing], max_length: float) -> list[Line]:
    """Transformed straight stroke segments no longer than ``max_length``."""
    lines: list[Line] = []
    for drawing in drawings:
        if drawing.kind != "stroke_path":
            continue
        path = path_segments(drawing.node)
        if not path.straight:
            continue
        matrix = Matrix.of_node(drawing.node)
        for segment in path.segments:
            start, end = matrix.apply(segment.start), matrix.apply(segment.end)
            if 0 < math.dist(start, end) <= max_length:
                lines.append(Line(start, end, drawing.index))
    return lines


class _Search(NamedTuple):
    """How closely strokes must line up with a trim corner to count as a crop mark."""

    tolerance: float  # points; how far an end may stray from the corner's line or the trim edge
    max_length: float  # points; how far from the corner a crop stroke may begin


def _crop_stroke_operations(
    lines: Sequence[Line],
    search: _Search,
    *,
    axis: int,
    corner: Point,
    trim: tuple[float, float],
) -> list[int]:
    """Operations of strokes along ``axis`` that start at a trim corner and lie outside the trim.

    ``axis`` 0 selects horizontal strokes, 1 vertical ones. A crop stroke has both ends on
    the corner's line, extends away from the trim region and begins within ``max_length``
    of the corner. ``trim`` is the trim region's low and high edge along the axis.
    """
    tolerance, max_length = search
    cross = 1 - axis
    low, high = trim
    at_low_edge = corner[axis] == low
    operations = []
    for line in lines:
        ends = (line.start, line.end)
        on_corner_line = max(abs(end[cross] - corner[cross]) for end in ends) <= tolerance
        if at_low_edge:
            outside = max(end[axis] for end in ends) <= low + tolerance
        else:
            outside = min(end[axis] for end in ends) >= high - tolerance
        near = min(abs(end[axis] - corner[axis]) for end in ends) <= max_length
        if on_corner_line and outside and near:
            operations.append(line.operation)
    return operations


def _corner_marks(region: PdfRegion, lines: Sequence[Line], search: _Search) -> list[Candidate]:
    """Corners of the trim region where a horizontal and a vertical crop stroke meet."""
    marks: list[Candidate] = []
    left, top, right, bottom = region.box
    for x, y in ((left, top), (right, top), (left, bottom), (right, bottom)):
        corner = Point(x, y)
        horizontal = _crop_stroke_operations(
            lines, search, axis=0, corner=corner, trim=(left, right)
        )
        vertical = _crop_stroke_operations(lines, search, axis=1, corner=corner, trim=(top, bottom))
        if horizontal and vertical:
            marks.append(
                {
                    "page": region.page,
                    "region": region.name,
                    "corner_pt": (x, y),
                    "operations": sorted(set(horizontal + vertical)),
                    "candidate": "paired trim-aligned crop strokes",
                }
            )
    return marks


def _registration_marks(
    region: PdfRegion, lines: Sequence[Line], tolerance: float
) -> list[Candidate] | None:
    """Crossed strokes outside the trim region; None when the pair work exceeds its bound.

    Crossed short horizontal/vertical strokes outside the trim rectangle are
    registration-target candidates. Graphic content can look identical.
    """
    horizontal = [line for line in lines if abs(line.start.y - line.end.y) <= tolerance]
    vertical = [line for line in lines if abs(line.start.x - line.end.x) <= tolerance]
    if len(horizontal) * len(vertical) > MAX_PAIR_CHECKS:
        return None
    marks: list[Candidate] = []
    for across in horizontal:
        cancellation_point()
        y = (across.start.y + across.end.y) / 2
        for upright in vertical:
            x = (upright.start.x + upright.end.x) / 2
            if (
                min(across.start.x, across.end.x) + tolerance
                < x
                < max(across.start.x, across.end.x) - tolerance
                and min(upright.start.y, upright.end.y) + tolerance
                < y
                < max(upright.start.y, upright.end.y) - tolerance
                and not contains(region.box, (x, y, x, y), tolerance)
            ):
                marks.append(
                    {
                        "page": region.page,
                        "region": region.name,
                        "center_pt": (x, y),
                        "operations": [across.operation, upright.operation],
                        "candidate": "crossed registration strokes",
                    }
                )
    return marks


def printer_mark_finding(artwork: TracedArtwork, options: PdfArtworkOptions) -> Finding:
    exemptions = options.printer_mark_exemptions
    search = _Search(options.geometry_tolerance_pt, options.printer_mark_max_length_pt or 0)
    by_page = artwork.drawings_by_page()
    violations: list[object] = []
    measured = 0
    uncertain = any(
        not artwork.region_is_measurable(region) for region in options.trim_regions
    ) or any(page > len(artwork.page_boxes) for page in exemptions)
    for region in options.trim_regions:
        if region.page in exemptions or not artwork.region_is_measurable(region):
            continue
        cancellation_point()
        measured += 1
        lines = _short_lines(by_page.get(region.page, ()), search.max_length)
        violations.extend(_corner_marks(region, lines, search))
        registration = _registration_marks(region, lines, search.tolerance)
        if registration is None:
            uncertain = True
        else:
            violations.extend(registration)
    return heuristic_finding(
        ArtworkRule.PRINTER_MARKS,
        _mark_message(violations, measured, uncertain),
        violations,
        incomplete=uncertain or measured == 0,
        measured_trim_regions=measured,
        exempt_pages=exemptions,
        scope="Geometric paired-corner and crossed-stroke candidates. Outlined, curved and "
        "raster marks are unrecognized; intentional marks and similar artwork require review.",
    )


def _mark_message(violations: Sequence[object], measured: int, uncertain: bool) -> str:
    rows = [row for row in violations if isinstance(row, dict)]
    if rows:
        corners = sum("corner_pt" in row for row in rows)
        crossings = len(rows) - corners
        pages = sorted({row["page"] for row in rows if isinstance(row["page"], int)})
        return (
            f"Possible printer marks outside the trim region on page {', '.join(map(str, pages))}: "
            f"{plural(corners, 'crop-mark corner')} and "
            f"{plural(crossings, 'crossed registration stroke pair')}."
        )
    if not measured:
        return "No trim region could be measured, so printer marks were not searched for."
    message = (
        f"No short crop or registration strokes were found outside the trim region of "
        f"{plural(measured, 'measured page')}."
    )
    return message + (" Some trim regions could not be measured." if uncertain else "")
