"""Text-size, printable-bounds and overlap measurements from Poppler's text boxes.

Coordinates are PDF points from the top-left of an unrotated, uncropped page. Poppler
text boxes do not describe images, outlines, clipping paths or semantics, so these
checks speak only for extractable text. A page whose coordinate frame cannot be aligned
is unmeasured, never passed. This module owns the three text-box measurements; the PDF
tools and their output formats belong to ``pdf_tools`` and ``pdf_tool_output``.
"""

from __future__ import annotations

from collections.abc import Collection, Iterator
from dataclasses import dataclass
from typing import NamedTuple

from .errors import PreparationError
from .findings import Finding
from .measurement_findings import (
    MAX_SAMPLES,
    findings_or_unavailable,
    heuristic_finding,
    measurement_finding,
)
from .pdf_check_options import PdfCheckOptions
from .pdf_geometry import MAX_PAIR_CHECKS, contains, finite_number, intersection_area
from .pdf_rule_names import PdfRule
from .pdf_tool_output import PageFrames, page_frames, parse_tool_xml
from .pdf_tools import PdfToolSession, ToolMeasurement, page_boxes_argv, text_geometry_argv

# Poppler reports a font size as a whole number of points even with -noroundcoord, so a
# size is only known to within half a point.
FONT_SIZE_ROUNDING_PT = 0.5
# A text-box page whose size differs from the page-box size by more than this is a
# different frame, and its boxes are not compared with regions.
_FRAME_SIZE_TOLERANCE_PT = 1.0
_LAYOUT_SCOPE = (
    "Extractable Poppler text boxes only. Outlined/raster text, figures, tables, "
    "equations and clipping paths are not identified semantically."
)


@dataclass(frozen=True)
class _TextBox:
    """One nonempty Poppler text box.

    Attributes:
        page: The page number, from 1.
        index: The box's position among the page's text elements, from 1.
        box: The bounds as ``(left, top, right, bottom)`` in points.
        size: The reported font size in points.
    """

    page: int
    index: int
    box: tuple[float, float, float, float]
    size: float


@dataclass(frozen=True)
class _TextPage:
    """One page of Poppler text boxes and the page size they were measured on.

    Attributes:
        page: The page number, from 1.
        width: The page width in points.
        height: The page height in points.
        boxes: The nonempty text boxes.
    """

    page: int
    width: float
    height: float
    boxes: tuple[_TextBox, ...]


def _text_pages(output: str, pages: int) -> list[_TextPage]:
    """Read the text boxes of every page from Poppler's XML output.

    Args:
        output: The XML that ``pdftohtml`` printed.
        pages: The number of pages the document has.

    Returns:
        One text page per page, in order.

    Raises:
        PreparationError: The XML lacks a valid page number or size, has ambiguous font
            sizes or an unmeasured text box, or does not list every page once in order.
    """
    root = parse_tool_xml(output, "pdf2xml")
    fonts: dict[str, float] = {}
    result = []
    for page in root.findall("page"):
        try:
            number = int(page.attrib["number"])
        except (KeyError, ValueError) as error:
            raise PreparationError("Text geometry lacks a valid page number") from error
        width, height = finite_number(page.get("width")), finite_number(page.get("height"))
        if min(width, height) <= 0:
            raise PreparationError("Text geometry contains an invalid page size")
        for font in page.findall("fontspec"):
            key = font.get("id")
            size = finite_number(font.get("size"))
            if key is None or size <= 0 or (key in fonts and fonts[key] != size):
                raise PreparationError("Text geometry contains ambiguous font-size identifiers")
            fonts[key] = size
        boxes = []
        for index, node in enumerate(page.findall("text"), 1):
            if not "".join(node.itertext()).strip():
                continue
            left, top = finite_number(node.get("left")), finite_number(node.get("top"))
            box_width, box_height = (
                finite_number(node.get("width")),
                finite_number(node.get("height")),
            )
            if min(box_width, box_height) <= 0 or node.get("font") not in fonts:
                raise PreparationError("Text geometry has unmeasured text size or bounds")
            boxes.append(
                _TextBox(
                    number,
                    index,
                    (left, top, left + box_width, top + box_height),
                    fonts[node.attrib["font"]],
                )
            )
        result.append(_TextPage(number, width, height, tuple(boxes)))
    if [page.page for page in result] != list(range(1, pages + 1)):
        raise PreparationError("Text geometry did not report every page exactly once in order")
    return result


def _text_box_sample(box: _TextBox) -> dict[str, object]:
    """Describe a text box for a finding: its page, index, bounds and reported size."""
    return {
        "page": box.page,
        "text_box": box.index,
        "bounds_pt": box.box,
        "reported_size_pt": box.size,
    }


@dataclass(frozen=True, slots=True)
class _TextLayout:
    """Extracted text pages and the pages whose coordinate frame cannot be aligned.

    Attributes:
        pages: The text pages.
        unsupported: The numbers of the pages whose frame cannot be aligned.
        options: The PDF check options.
    """

    pages: tuple[_TextPage, ...]
    unsupported: frozenset[int]
    options: PdfCheckOptions

    def measurable_pages(self) -> Iterator[_TextPage]:
        """Yield the text pages whose coordinate frame is aligned."""
        return (page for page in self.pages if page.page not in self.unsupported)


class _BoxMinimum(NamedTuple):
    """The minimum text size that applies to one text box, and what the regions said.

    Attributes:
        minimum: The minimum in points, or ``None`` when none applies.
        regions_touched: The indexes of the configured regions that intersect the box.
        partly_in_region: Whether the box's size class is unknown.
    """

    minimum: float | None
    regions_touched: tuple[int, ...]
    # A region overlaps the box without containing it, so the box's size class is unknown.
    partly_in_region: bool


def _minimum_for_box(box: _TextBox, options: PdfCheckOptions) -> _BoxMinimum:
    """Find the strictest minimum of the regions containing ``box``, else the global one."""
    selected: list[float] = []
    touched: list[int] = []
    partly_in_region = False
    for index, region in enumerate(options.text_size_regions):
        if region.page != box.page or not intersection_area(region.box, box.box):
            continue
        touched.append(index)
        if not contains(region.box, box.box, options.geometry_tolerance_pt):
            partly_in_region = True
        elif region.min_text_size_pt is not None:
            selected.append(region.min_text_size_pt)
    minimum = max(selected) if selected else options.min_text_size_pt
    return _BoxMinimum(minimum, tuple(touched), partly_in_region)


def _text_size_finding(layout: _TextLayout) -> Finding:
    """Compare the reported text sizes with the configured minima.

    A size within the rounding precision of its minimum, a page without text, a box only
    partly in a region, a region that touches no box, or no measured box at all leaves
    the result incomplete.
    """
    options = layout.options
    violations: list[object] = []
    uncertain = bool(layout.unsupported)
    measured = 0
    regions_seen: set[int] = set()
    for page in layout.measurable_pages():
        if not page.boxes and options.min_text_size_pt is not None:
            uncertain = True
        for box in page.boxes:
            applicable = _minimum_for_box(box, options)
            regions_seen.update(applicable.regions_touched)
            uncertain |= applicable.partly_in_region
            minimum = applicable.minimum
            if minimum is None:
                continue
            measured += 1
            if box.size + FONT_SIZE_ROUNDING_PT < minimum:
                violations.append({**_text_box_sample(box), "minimum_pt": minimum})
            elif box.size - FONT_SIZE_ROUNDING_PT < minimum:
                uncertain = True
    uncertain |= len(regions_seen) != len(options.text_size_regions) or measured == 0
    return measurement_finding(
        PdfRule.RENDERED_TEXT_SIZE,
        "Compared reported text sizes with configured minima.",
        violations,
        incomplete=uncertain,
        details={
            "measured_text_boxes": measured,
            "unsupported_pages": sorted(layout.unsupported),
            "precision_pt": FONT_SIZE_ROUNDING_PT,
            "scope": _LAYOUT_SCOPE,
        },
    )


def _printable_regions(page: _TextPage, options: PdfCheckOptions) -> list[tuple[float, ...]]:
    """Return the printable regions configured for ``page``, else those its margins leave."""
    regions: list[tuple[float, ...]] = [
        region.box for region in options.printable_regions if region.page == page.page
    ]
    if options.printable_margins_pt is not None and not regions:
        left, top, right, bottom = options.printable_margins_pt
        regions = [(left, top, page.width - right, page.height - bottom)]
    return regions


def _printable_bounds_finding(layout: _TextLayout) -> Finding:
    """Check that every text box lies within a printable region of its page.

    A region for a missing page, a region that does not fit its page, a page without
    text or no measured box at all leaves the result incomplete.
    """
    options = layout.options
    violations: list[object] = []
    page_numbers = {page.page for page in layout.pages}
    uncertain = bool(layout.unsupported) or any(
        region.page not in page_numbers for region in options.printable_regions
    )
    measured = 0
    for page in layout.measurable_pages():
        regions = _printable_regions(page, options)
        if not regions:
            continue
        if any(
            box[0] >= box[2] or box[1] >= box[3] or box[2] > page.width or box[3] > page.height
            for box in regions
        ):
            uncertain = True
            continue
        if not page.boxes:
            uncertain = True
        for box in page.boxes:
            measured += 1
            if not any(
                contains(region, box.box, options.geometry_tolerance_pt) for region in regions
            ):
                violations.append(_text_box_sample(box))
    return heuristic_finding(
        PdfRule.PRINTABLE_TEXT_BOUNDS,
        "Checked text boxes against supplied printable regions.",
        violations,
        incomplete=uncertain or measured == 0,
        details={
            "measured_text_boxes": measured,
            "unsupported_pages": sorted(layout.unsupported),
            "tolerance_pt": options.geometry_tolerance_pt,
            "scope": _LAYOUT_SCOPE,
        },
    )


def _vertically_overlapping_pairs(
    layout: _TextLayout,
) -> Iterator[tuple[int, _TextBox, _TextBox]]:
    """Sweep each page top to bottom, pairing boxes whose vertical extents overlap.

    Yields:
        The page number and the two boxes of each pair, the upper one first.
    """
    for page in layout.measurable_pages():
        active: list[_TextBox] = []
        for box in sorted(page.boxes, key=lambda item: item.box[1]):
            active = [other for other in active if other.box[3] > box.box[1]]
            for other in active:
                yield page.page, other, box
            active.append(box)


def _overlap_finding(layout: _TextLayout, minimum_ratio: float) -> Finding:
    """Report pairs of text boxes that overlap by at least ``minimum_ratio``.

    The ratio is the intersection area over the area of the smaller box. More than
    ``MAX_PAIR_CHECKS`` comparisons leave the result incomplete.

    Args:
        layout: The text pages.
        minimum_ratio: The smallest area ratio that counts as an overlap.

    Returns:
        The overlap finding, with at most ``MAX_SAMPLES`` overlaps listed and all counted.
    """
    overlaps: list[object] = []
    comparisons = overlap_count = 0
    uncertain = bool(layout.unsupported)
    measured = sum(len(page.boxes) for page in layout.measurable_pages())
    for comparisons, (page, other, box) in enumerate(_vertically_overlapping_pairs(layout), 1):
        if comparisons > MAX_PAIR_CHECKS:
            uncertain = True
            break
        area = intersection_area(box.box, other.box)
        smaller = min(
            (item.box[2] - item.box[0]) * (item.box[3] - item.box[1]) for item in (box, other)
        )
        if area / smaller >= minimum_ratio:
            overlap_count += 1
            if len(overlaps) < MAX_SAMPLES:
                overlaps.append(
                    {
                        "page": page,
                        "text_boxes": [other.index, box.index],
                        "bounds_pt": [other.box, box.box],
                        "area_ratio": area / smaller,
                    }
                )
    return heuristic_finding(
        PdfRule.TEXT_OVERLAP,
        "Measured intersections between extractable text boxes.",
        overlaps,
        incomplete=uncertain or measured == 0,
        total_violations=overlap_count,
        details={
            "minimum_area_ratio": minimum_ratio,
            "comparisons": min(comparisons, MAX_PAIR_CHECKS),
            "scope": _LAYOUT_SCOPE
            + " Overlaps are candidates, including legitimate mathematical overlays.",
        },
    )


def _layout_findings(
    pages: list[_TextPage], frames: PageFrames, options: PdfCheckOptions
) -> list[Finding]:
    """Run the configured text-box measurements on the pages whose frame can be aligned.

    A page whose text-box size differs from its page-box size by more than
    ``_FRAME_SIZE_TOLERANCE_PT`` is unsupported, like the pages ``frames`` cannot align.

    Args:
        pages: The text pages.
        frames: The page boxes of every page.
        options: The PDF check options.

    Returns:
        One finding per configured measurement.
    """
    unsupported = set(frames.unsupported_pages)
    for page, size in zip(pages, frames.sizes, strict=True):
        if (
            abs(page.width - size[0]) > _FRAME_SIZE_TOLERANCE_PT
            or abs(page.height - size[1]) > _FRAME_SIZE_TOLERANCE_PT
        ):
            unsupported.add(page.page)
    layout = _TextLayout(tuple(pages), frozenset(unsupported), options)
    result: list[Finding] = []
    if options.min_text_size_pt is not None or options.text_size_regions:
        result.append(_text_size_finding(layout))
    if options.printable_margins_pt is not None or options.printable_regions:
        result.append(_printable_bounds_finding(layout))
    if options.overlap_min_area_ratio is not None:
        result.append(_overlap_finding(layout, options.overlap_min_area_ratio))
    return result


async def measure_text_geometry(
    session: PdfToolSession, pages: int, options: PdfCheckOptions, rules: Collection[str]
) -> list[Finding]:
    """Measure the configured text-box checks; each of ``rules`` is inconclusive on failure.

    Args:
        session: The session over the frozen document.
        pages: The number of pages.
        options: The PDF check options.
        rules: The configured text-box rules.

    Returns:
        The findings of the configured measurements.
    """

    async def measure() -> list[Finding]:
        """Read the page boxes, then measure the text boxes against them."""
        frames = await session.measure(
            "page-boxes", page_boxes_argv(pages), lambda result: page_frames(result.stdout, pages)
        )
        return await ToolMeasurement(
            "text-geometry",
            text_geometry_argv(),
            tuple(rules),
            lambda result: _layout_findings(_text_pages(result.stdout, pages), frames, options),
        ).run(session)

    return await findings_or_unavailable(rules, measure)
