"""Bounded artwork observations; explicit regions use top-left PDF-point coordinates.

MuPDF trace supplies painted operations, not semantic figure/table labels. qpdf
supplies declared Type 3 glyph programs, not proof that every glyph is used. All
visual candidates remain advisory. Numerical policies are supplied by the user.
"""

from __future__ import annotations

import base64
import binascii
import json
import math
import re
import struct
import xml.etree.ElementTree as ET
import zlib
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from .models import Finding, PreparationError
from .pdf import _font_inventory, _page_count, _read_raster, _require_rendered_pages
from .pdf_checks import (
    PdfRegion,
    _contains,
    _finite,
    _geometry_support,
    _gray_raster,
    _intersection,
    _luminance,
    _number,
    _Objects,
    _properties,
    _result,
    _Session,
    _session,
    _strings,
    _unavailable,
    _xml,
)
from .runtime import CommandResult, ToolRunner
from .scheduler import ResourceBudget, cancellation_point

_MAX_PIXELS = 4_000_000
_MAX_SAMPLES = 100
_MAX_OPERATIONS = 50_000
_MAX_PIXEL_VISITS = 8_000_000
Box = tuple[float, float, float, float]
Point = tuple[float, float]
Matrix = tuple[float, float, float, float, float, float]


@dataclass(frozen=True)
class PdfContrastSample:
    """User-selected foreground/background patches, with no inferred text semantics."""

    name: str
    foreground: PdfRegion
    background: PdfRegion
    minimum_ratio: float
    max_channel_spread: int = 0

    def __post_init__(self) -> None:
        _strings((self.name,), "Contrast sample name")
        if not isinstance(self.foreground, PdfRegion) or not isinstance(self.background, PdfRegion):
            raise PreparationError("Contrast samples require foreground and background PDF regions")
        if self.foreground.page != self.background.page:
            raise PreparationError("Foreground and background samples must be on the same page")
        _number(self.minimum_ratio, "Minimum rendered sample contrast")
        if not 1 <= self.minimum_ratio <= 21:
            raise PreparationError("Minimum rendered sample contrast must be in [1, 21]")
        if type(self.max_channel_spread) is not int or not 0 <= self.max_channel_spread <= 255:
            raise PreparationError("Sample channel spread must be an integer from 0 to 255")


@dataclass(frozen=True)
class PdfArtworkOptions:
    detect_printer_marks: bool = False
    trim_regions: tuple[PdfRegion, ...] = ()
    printer_mark_max_length_pt: float | None = None
    printer_mark_exemptions: tuple[int, ...] = ()
    figure_regions: tuple[PdfRegion, ...] = ()
    max_figure_whitespace_ratio: float | None = None
    figure_edge_band_pt: float | None = None
    drawing_regions: tuple[PdfRegion, ...] = ()
    check_clipping: bool = False
    classify_artwork: bool = False
    artwork_category: str | None = None
    category_min_dpi: tuple[tuple[str, float], ...] = ()
    classify_type3_glyphs: bool = False
    forbid_bitmap_type3_glyphs: bool = False
    min_stroke_width_pt: float | None = None
    raster_regions: tuple[PdfRegion, ...] = ()
    min_raster_region_coverage: float | None = None
    grayscale_preview: bool = False
    contrast_samples: tuple[PdfContrastSample, ...] = ()
    raster_dpi: int = 72
    background_gray: int = 255
    ink_difference: int = 5
    geometry_tolerance_pt: float = 0.5

    def __post_init__(self) -> None:
        for name in (
            "detect_printer_marks",
            "check_clipping",
            "classify_artwork",
            "classify_type3_glyphs",
            "forbid_bitmap_type3_glyphs",
            "grayscale_preview",
        ):
            if type(getattr(self, name)) is not bool:
                raise PreparationError(f"{name} must be a boolean")
        for name in ("trim_regions", "figure_regions", "drawing_regions", "raster_regions"):
            values = getattr(self, name)
            if (
                not isinstance(values, tuple)
                or len(values) > 300
                or any(not isinstance(value, PdfRegion) for value in values)
            ):
                raise PreparationError(f"{name} requires at most 300 immutable PDF regions")
        if len({region.page for region in self.trim_regions}) != len(self.trim_regions):
            raise PreparationError("Only one trim region may be supplied for each page")
        if (
            not isinstance(self.contrast_samples, tuple)
            or len(self.contrast_samples) > 300
            or any(not isinstance(sample, PdfContrastSample) for sample in self.contrast_samples)
        ):
            raise PreparationError("Contrast samples require at most 300 immutable sample pairs")
        for name in ("printer_mark_max_length_pt", "figure_edge_band_pt", "min_stroke_width_pt"):
            if getattr(self, name) is not None:
                _number(getattr(self, name), name)
        for name in ("max_figure_whitespace_ratio", "min_raster_region_coverage"):
            value = getattr(self, name)
            if value is not None:
                _number(value, name, zero=name == "max_figure_whitespace_ratio")
                if value > 1:
                    raise PreparationError(f"{name} cannot exceed one")
        if self.detect_printer_marks and (
            not self.trim_regions or self.printer_mark_max_length_pt is None
        ):
            raise PreparationError("Printer marks need explicit trim regions and maximum length")
        if bool(self.raster_regions) != (self.min_raster_region_coverage is not None):
            raise PreparationError("Raster candidates need explicit regions and minimum coverage")
        if not isinstance(self.printer_mark_exemptions, tuple) or any(
            type(page) is not int or page < 1 for page in self.printer_mark_exemptions
        ):
            raise PreparationError("Printer-mark exemptions require immutable positive pages")
        if self.artwork_category is not None:
            _strings((self.artwork_category,), "Artwork category")
        if not isinstance(self.category_min_dpi, tuple) or any(
            not isinstance(pair, tuple) or len(pair) != 2 for pair in self.category_min_dpi
        ):
            raise PreparationError("Category DPI requires immutable category/minimum pairs")
        _strings(tuple(pair[0] for pair in self.category_min_dpi), "Artwork categories")
        for _, minimum in self.category_min_dpi:
            _number(minimum, "Category minimum DPI")
        if self.category_min_dpi and self.artwork_category not in dict(self.category_min_dpi):
            raise PreparationError("Select an artwork category with an explicit DPI threshold")
        if type(self.raster_dpi) is not int or not 18 <= self.raster_dpi <= 144:
            raise PreparationError("Artwork raster DPI must be an integer from 18 to 144")
        if type(self.background_gray) is not int or not 0 <= self.background_gray <= 255:
            raise PreparationError("Artwork background must be an 8-bit gray value")
        if type(self.ink_difference) is not int or not 1 <= self.ink_difference <= 255:
            raise PreparationError("Artwork ink difference must be an integer from 1 to 255")
        _number(self.geometry_tolerance_pt, "Artwork geometry tolerance", zero=True)


def _configured(options: PdfArtworkOptions) -> list[str]:
    return [
        name
        for name, enabled in (
            ("pdf.printer_marks", options.detect_printer_marks),
            ("pdf.figure_whitespace", options.max_figure_whitespace_ratio is not None),
            ("pdf.figure_edge_ink", options.figure_edge_band_pt is not None),
            ("pdf.drawing_bounds", bool(options.drawing_regions)),
            ("pdf.clipping_candidates", options.check_clipping),
            ("pdf.artwork_composition", options.classify_artwork),
            ("pdf.artwork_category_resolution", bool(options.category_min_dpi)),
            (
                "pdf.type3_glyph_programs",
                options.classify_type3_glyphs or options.forbid_bitmap_type3_glyphs,
            ),
            ("pdf.transformed_stroke_width", options.min_stroke_width_pt is not None),
            ("pdf.raster_region_candidates", bool(options.raster_regions)),
            ("pdf.grayscale_preview", options.grayscale_preview),
            ("pdf.rendered_sample_contrast", bool(options.contrast_samples)),
        )
        if enabled
    ]


def _matrix(node: ET.Element) -> Matrix:
    values = tuple(_finite(value) for value in node.get("transform", "").split())
    if len(values) != 6:
        raise PreparationError("Drawing operation has no complete affine transform")
    return values[0], values[1], values[2], values[3], values[4], values[5]


def _point(point: Point, matrix: Matrix) -> Point:
    x, y = point
    a, b, c, d, e, f = matrix
    result = a * x + c * y + e, b * x + d * y + f
    if not all(math.isfinite(value) for value in result):
        raise PreparationError("Transformed drawing coordinate exceeds numerical limits")
    return result


def artwork_checks_enabled(options: PdfArtworkOptions) -> bool:
    """Tuning values alone do not select a measurement or require a source graph."""
    return bool(_configured(options))


def _box(points: list[Point]) -> Box:
    if not points:
        raise PreparationError("Drawing path has no measured vertices")
    return (
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )


def _segments(node: ET.Element) -> tuple[list[tuple[Point, Point]], bool]:
    result: list[tuple[Point, Point]] = []
    start = previous = None
    for child in node:
        if child.tag in {"moveto", "lineto"}:
            current = _finite(child.get("x")), _finite(child.get("y"))
            if child.tag == "moveto":
                start = current
            elif previous is None:
                return result, False
            else:
                result.append((previous, current))
            previous = current
        elif child.tag == "closepath" and previous is not None and start is not None:
            result.append((previous, start))
            previous = start
        else:
            return result, False
    return result, bool(result)


def _path_box(node: ET.Element) -> Box | None:
    segments, supported = _segments(node)
    if not supported:
        return None
    matrix = _matrix(node)
    return _box([_point(point, matrix) for segment in segments for point in segment])


def _rectangle(node: ET.Element) -> Box | None:
    segments, supported = _segments(node)
    if not supported or len(segments) != 4 or segments[-1][1] != segments[0][0]:
        return None
    matrix = _matrix(node)
    points = [_point(segment[0], matrix) for segment in segments]
    box = _box(points)
    corners = {(box[0], box[1]), (box[2], box[1]), (box[2], box[3]), (box[0], box[3])}
    if set(points) != corners:
        return None
    for first, second in zip(points, points[1:] + points[:1], strict=True):
        if first[0] != second[0] and first[1] != second[1]:
            return None
    return box


@dataclass(frozen=True)
class _Drawing:
    page: int
    index: int
    node: ET.Element
    box: Box | None
    clip: Box | None
    uncertain: bool


def _trace(output: str, pages: int) -> tuple[list[_Drawing], list[Box], set[int]]:
    root = _xml(output, "document")
    traced = root.findall("page")
    try:
        numbers = [int(page.attrib["number"]) for page in traced]
    except (KeyError, ValueError) as error:
        raise PreparationError("Trace page numbers are missing or invalid") from error
    if numbers != list(range(1, pages + 1)):
        raise PreparationError("Trace did not report every page exactly once")
    drawings: list[_Drawing] = []
    page_boxes: list[Box] = []
    uncertain_pages: set[int] = set()
    for number, page in zip(numbers, traced, strict=True):
        values = tuple(_finite(value) for value in page.get("mediabox", "").split())
        if len(values) != 4 or values[0] >= values[2] or values[1] >= values[3]:
            raise PreparationError("Trace page bounds are incomplete")
        page_box: Box = values[0], values[1], values[2], values[3]
        page_boxes.append(page_box)
        clips: list[Box | None] = [page_box]
        index = 0
        # Clip events are siblings in MuPDF XML. Transparency groups are nested.
        for node in page.iter():
            if node is page:
                continue
            if node.tag in {"group", "tile", "clip_mask"}:
                uncertain_pages.add(number)
            elif node.tag.startswith("clip_"):
                clip = _rectangle(node) if node.tag == "clip_path" else None
                parent = clips[-1]
                if clip is not None and parent is not None:
                    clip = (
                        max(clip[0], parent[0]),
                        max(clip[1], parent[1]),
                        min(clip[2], parent[2]),
                        min(clip[3], parent[3]),
                    )
                else:
                    clip = None
                clips.append(clip)
            elif node.tag == "pop_clip":
                if len(clips) == 1:
                    raise PreparationError("Trace has an unmatched clipping restore")
                clips.pop()
            elif node.tag in {
                "fill_image",
                "fill_image_mask",
                "fill_path",
                "stroke_path",
                "fill_text",
                "stroke_text",
                "ignore_text",
                "fill_shade",
            }:
                index += 1
                box = None
                if node.tag in {"fill_image", "fill_image_mask"}:
                    matrix = _matrix(node)
                    box = _box(
                        [_point(point, matrix) for point in ((0, 0), (1, 0), (0, 1), (1, 1))]
                    )
                elif node.tag == "fill_path":
                    box = _path_box(node)
                drawings.append(
                    _Drawing(number, index, node, box, clips[-1], number in uncertain_pages)
                )
            elif node.tag not in {
                "moveto",
                "lineto",
                "curveto",
                "closepath",
                "span",
                "g",
                "layer",
                "structure",
                "metatext",
            }:
                uncertain_pages.add(number)
        if len(clips) != 1:
            uncertain_pages.add(number)
    return drawings, page_boxes, uncertain_pages


def _sample(drawing: _Drawing) -> dict[str, object]:
    return {
        "page": drawing.page,
        "operation": drawing.index,
        "kind": drawing.node.tag,
        "bounds_pt": drawing.box,
    }


def _composition(
    drawings: list[_Drawing],
    options: PdfArtworkOptions,
    unknown: set[int],
    *,
    scalable_text_fonts: bool = False,
) -> list[Finding]:
    raster = [d for d in drawings if d.node.tag in {"fill_image", "fill_image_mask"}]
    text = [d for d in drawings if d.node.tag in {"fill_text", "stroke_text"}]
    vector = [d for d in drawings if d.node.tag in {"fill_path", "stroke_path"}]
    if scalable_text_fonts:
        vector.extend(text)
    unsupported = (
        bool(unknown)
        or any(d.node.tag == "fill_shade" for d in drawings)
        or bool(text and not scalable_text_fonts)
    )
    classification = (
        "mixed" if raster and vector else "raster" if raster else "vector" if vector else "empty"
    )
    result = []
    if options.classify_artwork:
        result.append(
            _result(
                "pdf.artwork_composition",
                "Inventoried painted raster and vector operations.",
                [],
                uncertain=unsupported or classification == "empty",
                advisory=True,
                classification=classification,
                raster_operations=len(raster),
                vector_operations=len(vector),
                unclassified_text_operations=0 if scalable_text_fonts else len(text),
                scope="Whole selected PDF, including text and backgrounds; figure categories, "
                "font text needs a complete scalable-font inventory. Outlined lettering "
                "and source-to-final attribution are not inferred.",
            )
        )
    if options.category_min_dpi:
        minimum = dict(options.category_min_dpi)[options.artwork_category or ""]
        measurements: list[object] = []
        violations: list[object] = []
        incomplete = unsupported
        for drawing in raster:
            node = drawing.node
            a, b, c, d, _, _ = _matrix(node)
            width, height = _finite(node.get("width")), _finite(node.get("height"))
            lengths = math.hypot(a, b), math.hypot(c, d)
            determinant = a * d - b * c
            if (
                min(width, height, *lengths) <= 0
                or not math.isfinite(determinant)
                or abs(determinant) < 1e-12
            ):
                incomplete = True
                continue
            axes = 72 * width / lengths[0], 72 * height / lengths[1]
            # Largest singular value of the points-per-pixel transform bounds
            # the coarsest sampling direction under shear as well as rotation.
            aa, bb, cc, dd = a / width, b / width, c / height, d / height
            norm = aa * aa + bb * bb + cc * cc + dd * dd
            if not math.isfinite(norm * norm) or norm <= 0:
                incomplete = True
                continue
            discriminant = max(0.0, norm * norm - 4 * (aa * dd - bb * cc) ** 2)
            coarse = 72 / math.sqrt((norm + math.sqrt(discriminant)) / 2)
            measurement = {
                **_sample(drawing),
                "x_dpi": axes[0],
                "y_dpi": axes[1],
                "minimum_direction_dpi": coarse,
            }
            measurements.append(measurement)
            if min(*axes, coarse) < minimum:
                violations.append(measurement)
        result.append(
            _result(
                "pdf.artwork_category_resolution",
                "Compared image placement sampling with the selected category.",
                violations,
                uncertain=incomplete or (not raster and not vector),
                category=options.artwork_category,
                minimum_dpi=minimum,
                measurements=measurements[:_MAX_SAMPLES],
                placement_count=len(raster),
                scope="Declared category and traced raster placements; axes and coarsest "
                "sampling includes rotation/shear. Vector operations have no raster DPI.",
            )
        )
    return result


def _stroke_finding(
    drawings: list[_Drawing], options: PdfArtworkOptions, unknown: set[int]
) -> Finding:
    violations: list[object] = []
    measured = 0
    uncertain = bool(unknown)
    for drawing in drawings:
        node = drawing.node
        if node.tag not in {"stroke_path", "stroke_text"}:
            continue
        width = _finite(node.get("linewidth"))
        a, b, c, d, _, _ = _matrix(node)
        determinant = abs(a * d - b * c)
        if width <= 0 or determinant == 0 or not math.isfinite(determinant):
            uncertain = True
            continue
        segments, supported = _segments(node)
        widths = []
        if supported:
            for first, second in segments:
                x, y = second[0] - first[0], second[1] - first[1]
                length = math.hypot(x, y)
                placed = math.hypot(a * x + c * y, b * x + d * y)
                if length and placed:
                    widths.append(width * determinant * length / placed)
        else:
            sx, sy = math.hypot(a, b), math.hypot(c, d)
            if math.isclose(sx, sy, rel_tol=1e-6) and math.isclose(a * c + b * d, 0, abs_tol=1e-8):
                widths.append(width * sx)
            else:
                uncertain = True
        if not widths:
            uncertain = True
            continue
        measured += len(widths)
        if options.min_stroke_width_pt is not None and min(widths) < options.min_stroke_width_pt:
            violations.append({**_sample(drawing), "minimum_effective_width_pt": min(widths)})
    return _result(
        "pdf.transformed_stroke_width",
        "Measured stroke thickness normal to transformed straight segments.",
        violations,
        uncertain=uncertain or measured == 0,
        advisory=True,
        measured_segments=measured,
        minimum_pt=options.min_stroke_width_pt,
        scope="Straight-segment normal thickness supports arbitrary nonsingular affine transforms; "
        "curves/text need uniform transforms. Hairlines, joins, caps and raster lines are omitted.",
    )


def _mark_finding(
    drawings: list[_Drawing], options: PdfArtworkOptions, unsupported: set[int], boxes: list[Box]
) -> Finding:
    violations: list[object] = []
    measured = 0
    pages = len(boxes)
    uncertain = any(
        region.page > pages
        or region.page in unsupported
        or not _contains(boxes[region.page - 1], region.box, 0)
        for region in options.trim_regions
    )
    uncertain |= any(page > pages for page in options.printer_mark_exemptions)
    tolerance = options.geometry_tolerance_pt
    for region in options.trim_regions:
        if (
            region.page in options.printer_mark_exemptions
            or region.page in unsupported
            or region.page > pages
            or not _contains(boxes[region.page - 1], region.box, 0)
        ):
            continue
        measured += 1
        lines: list[tuple[Point, Point, int]] = []
        for drawing in drawings:
            if drawing.page != region.page or drawing.node.tag != "stroke_path":
                continue
            segments, supported = _segments(drawing.node)
            if not supported:
                continue
            matrix = _matrix(drawing.node)
            for first, second in segments:
                first, second = _point(first, matrix), _point(second, matrix)
                length = math.dist(first, second)
                if 0 < length <= (options.printer_mark_max_length_pt or 0):
                    lines.append((first, second, drawing.index))
        left, top, right, bottom = region.box
        for x, y in ((left, top), (right, top), (left, bottom), (right, bottom)):
            horizontal = [
                index
                for first, second, index in lines
                if max(abs(first[1] - y), abs(second[1] - y)) <= tolerance
                and (
                    max(first[0], second[0]) <= left + tolerance
                    if x == left
                    else min(first[0], second[0]) >= right - tolerance
                )
                and min(abs(first[0] - x), abs(second[0] - x))
                <= (options.printer_mark_max_length_pt or 0)
            ]
            vertical = [
                index
                for first, second, index in lines
                if max(abs(first[0] - x), abs(second[0] - x)) <= tolerance
                and (
                    max(first[1], second[1]) <= top + tolerance
                    if y == top
                    else min(first[1], second[1]) >= bottom - tolerance
                )
                and min(abs(first[1] - y), abs(second[1] - y))
                <= (options.printer_mark_max_length_pt or 0)
            ]
            if horizontal and vertical:
                violations.append(
                    {
                        "page": region.page,
                        "region": region.name,
                        "corner_pt": (x, y),
                        "operations": sorted(set(horizontal + vertical)),
                        "candidate": "paired trim-aligned crop strokes",
                    }
                )
        # Crossed short horizontal/vertical strokes outside the trim rectangle
        # are registration-target candidates. Graphic content can look identical.
        horizontal = [
            (first, second, index)
            for first, second, index in lines
            if abs(first[1] - second[1]) <= tolerance
        ]
        vertical = [
            (first, second, index)
            for first, second, index in lines
            if abs(first[0] - second[0]) <= tolerance
        ]
        if len(horizontal) * len(vertical) > 250_000:
            uncertain = True
            continue
        for first, second, horizontal_index in horizontal:
            for other_first, other_second, vertical_index in vertical:
                x = (other_first[0] + other_second[0]) / 2
                y = (first[1] + second[1]) / 2
                if (
                    min(first[0], second[0]) + tolerance < x < max(first[0], second[0]) - tolerance
                    and min(other_first[1], other_second[1]) + tolerance
                    < y
                    < max(other_first[1], other_second[1]) - tolerance
                    and not _contains(region.box, (x, y, x, y), tolerance)
                ):
                    violations.append(
                        {
                            "page": region.page,
                            "region": region.name,
                            "center_pt": (x, y),
                            "operations": [horizontal_index, vertical_index],
                            "candidate": "crossed registration strokes",
                        }
                    )
    return _result(
        "pdf.printer_marks",
        "Searched for short crop/registration strokes outside explicit trim regions.",
        violations,
        uncertain=uncertain or measured == 0,
        advisory=True,
        measured_trim_regions=measured,
        exempt_pages=options.printer_mark_exemptions,
        scope="Geometric paired-corner and crossed-stroke candidates. Outlined, curved and "
        "raster marks are unrecognized; intentional marks and similar artwork require review.",
    )


def _geometry_findings(
    drawings: list[_Drawing],
    boxes: list[Box],
    unknown: set[int],
    unsupported: set[int],
    options: PdfArtworkOptions,
) -> list[Finding]:
    result = []
    if options.drawing_regions:
        violations: list[object] = []
        measured = 0
        uncertain = bool(unknown) or any(
            r.page > len(boxes) or r.page in unsupported for r in options.drawing_regions
        )
        for drawing in drawings:
            allowed = [r for r in options.drawing_regions if r.page == drawing.page]
            if not allowed or drawing.node.tag not in {
                "fill_image",
                "fill_image_mask",
                "fill_path",
                "stroke_path",
            }:
                continue
            if (
                drawing.box is None
                or drawing.page in unsupported
                or drawing.uncertain
                or drawing.clip is None
            ):
                uncertain = True
                continue
            measured += 1
            visible = (
                max(drawing.box[0], drawing.clip[0]),
                max(drawing.box[1], drawing.clip[1]),
                min(drawing.box[2], drawing.clip[2]),
                min(drawing.box[3], drawing.clip[3]),
            )
            if visible[0] >= visible[2] or visible[1] >= visible[3]:
                continue
            if not any(_contains(r.box, visible, options.geometry_tolerance_pt) for r in allowed):
                violations.append({**_sample(drawing), "visible_bounds_pt": visible})
        result.append(
            _result(
                "pdf.drawing_bounds",
                "Compared transformed image and straight filled-path bounds with allowed regions.",
                violations,
                uncertain=uncertain or measured == 0,
                advisory=True,
                measured_operations=measured,
                scope="Affine image rectangles and polygon bounds after rectangular clipping. "
                "Text, strokes, curves, masks and unsupported page frames remain unmeasured.",
            )
        )
    if options.check_clipping:
        violations = []
        measured = 0
        uncertain = bool(unknown)
        for drawing in drawings:
            if drawing.node.tag not in {
                "fill_image",
                "fill_image_mask",
                "fill_path",
                "stroke_path",
            }:
                continue
            if drawing.box is None or drawing.clip is None or drawing.uncertain:
                uncertain = True
                continue
            measured += 1
            if not _contains(drawing.clip, drawing.box, options.geometry_tolerance_pt):
                violations.append({**_sample(drawing), "clip_bounds_pt": drawing.clip})
        result.append(
            _result(
                "pdf.clipping_candidates",
                "Compared painted operation extents with active rectangular clips.",
                violations,
                uncertain=uncertain or measured == 0,
                advisory=True,
                measured_operations=measured,
                scope="Extents crossing page or rectangular clip boundaries are candidates; "
                "intentional cropping, curves, strokes and arbitrary masks need review.",
            )
        )
    if options.min_stroke_width_pt is not None:
        result.append(_stroke_finding(drawings, options, unknown))
    if options.detect_printer_marks:
        result.append(_mark_finding(drawings, options, unsupported | unknown, boxes))
    if options.raster_regions:
        for region in options.raster_regions:
            incomplete = (
                region.page > len(boxes)
                or region.page in unsupported | unknown
                or not _contains(boxes[region.page - 1], region.box, 0)
            )
            images = []
            text = False
            for drawing in drawings:
                if drawing.page != region.page:
                    continue
                # Text trace has origins, not reliable glyph bounds. A page with
                # text cannot establish absence within a selected region.
                if drawing.node.tag in {"fill_text", "stroke_text", "ignore_text"}:
                    text = True
                if drawing.node.tag in {"fill_image", "fill_image_mask"}:
                    if drawing.box is None or drawing.clip is None or drawing.uncertain:
                        incomplete = True
                    else:
                        clipped = (
                            max(drawing.box[0], drawing.clip[0]),
                            max(drawing.box[1], drawing.clip[1]),
                            min(drawing.box[2], drawing.clip[2]),
                            min(drawing.box[3], drawing.clip[3]),
                        )
                        a, b, c, d, _, _ = _matrix(drawing.node)
                        if not ((b == c == 0) or (a == d == 0)):
                            incomplete = True
                        else:
                            images.append(clipped)
            area = (region.right_pt - region.left_pt) * (region.bottom_pt - region.top_pt)
            coverage = _union_area(images, region.box) / area
            candidate = coverage >= (options.min_raster_region_coverage or 1) and not text
            result.append(
                _result(
                    "pdf.raster_region_candidates",
                    f"Measured raster coverage in {region.name}.",
                    [{"page": region.page, "region": region.name, "raster_coverage": coverage}]
                    if candidate and not incomplete
                    else [],
                    uncertain=incomplete or text,
                    advisory=True,
                    page=region.page,
                    region=region.name,
                    raster_coverage=coverage,
                    page_has_text_operations=text,
                    scope="Union of axis-aligned images in an explicit region; high coverage "
                    "without page text is a scan/raster-table candidate, not a semantic diagnosis.",
                )
            )
    return result


def _union_area(boxes: list[Box], region: Box) -> float:
    clipped = [
        (max(b[0], region[0]), max(b[1], region[1]), min(b[2], region[2]), min(b[3], region[3]))
        for b in boxes
        if _intersection(b, region) > 0
    ]
    if len(clipped) > 1000:
        raise PreparationError("Raster region exceeds the bounded image-union limit")
    xs = sorted({x for box in clipped for x in (box[0], box[2])})
    area = 0.0
    for left, right in zip(xs, xs[1:], strict=False):
        intervals = sorted((b[1], b[3]) for b in clipped if b[0] < right and b[2] > left)
        length, end = 0.0, -math.inf
        for top, bottom in intervals:
            length += max(0, bottom - max(top, end))
            end = max(end, bottom)
        area += (right - left) * length
    return area


def _program_tokens(data: bytes) -> list[str]:
    """Tokenize a conservative stream subset without treating string payloads as operators."""
    text = data.decode("latin1")
    tokens: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char.isspace() or char == "\0":
            index += 1
        elif char == "%":
            end = re.search(r"[\r\n]", text[index:])
            index = len(text) if end is None else index + end.end()
        elif char in "(<":
            # Text strings, dictionaries and inline image data are deliberately
            # unsupported: do not mistake embedded operator-looking bytes for paint.
            raise PreparationError("Type 3 program uses unsupported string/dictionary content")
        else:
            match = re.match(r"/?[^\s\x00()<>\[\]{}%/]+|[\[\]]", text[index:])
            if match is None:
                raise PreparationError("Type 3 program contains unsupported token syntax")
            token = match.group()
            tokens.append(token)
            index += len(token)
            if len(tokens) > _MAX_OPERATIONS:
                raise PreparationError("Type 3 glyph program exceeds the token limit")
            if token == "BI":
                # BI is an actual inline-image operator, but the binary payload
                # cannot safely be parsed by this restricted content lexer.
                break
    return tokens


def _glyph_program(
    objects: _Objects,
    reference: object,
    resources: object,
    remaining: list[int],
    seen: frozenset[str] = frozenset(),
) -> tuple[bool, bool, bool]:
    if (
        not isinstance(reference, str)
        or not re.fullmatch(r"\d+ \d+ R", reference)
        or reference in seen
        or len(seen) >= 20
        or remaining[0] <= 0
    ):
        return False, False, True
    entry = objects.objects.get("obj:" + reference)
    if not isinstance(entry, dict) or not isinstance(entry.get("stream"), dict):
        return False, False, True
    stream = entry["stream"]
    dictionary = stream.get("dict")
    if (
        not isinstance(dictionary, dict)
        or dictionary.get("/Filter")
        or not isinstance(stream.get("data"), str)
    ):
        return False, False, True
    try:
        data = base64.b64decode(stream["data"], validate=True)
        if len(data) > 1_000_000:
            return False, False, True
        tokens = _program_tokens(data)
        remaining[0] -= len(tokens)
        if remaining[0] < 0:
            return False, False, True
    except (binascii.Error, PreparationError):
        return False, False, True
    raster = vector = uncertain = False
    simple = set(
        "q Q cm w J j M d ri i g G rg RG k K cs CS sc SC m l c v y h re n W W* d0 d1 [ ]".split()
    )
    paint = set("S s f F f* B B* b b*".split())
    for index, token in enumerate(tokens):
        if token in paint:
            vector = True
        elif token == "BI":
            # Positive inline-image evidence survives while later content is unknown.
            raster = uncertain = True
        elif token == "Do":
            try:
                name = tokens[index - 1] if index else ""
                if not name.startswith("/"):
                    raise PreparationError("Missing XObject name")
                xobjects = objects.dictionary(objects.dictionary(resources).get("/XObject"))
                child_reference = xobjects.get(name)
                child = objects.dictionary(child_reference)
                if child.get("/Subtype") == "/Image":
                    raster = True
                elif child.get("/Subtype") == "/Form":
                    child_flags = _glyph_program(
                        objects,
                        child_reference,
                        child.get("/Resources", resources),
                        remaining,
                        seen | {reference},
                    )
                    raster, vector, uncertain = (
                        a or b
                        for a, b in zip((raster, vector, uncertain), child_flags, strict=True)
                    )
                else:
                    uncertain = True
            except PreparationError:
                uncertain = True
        elif (
            token not in simple
            and not token.startswith("/")
            and not re.fullmatch(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)", token)
        ):
            uncertain = True
    return raster, vector, uncertain


def _type3_finding(objects: _Objects, options: PdfArtworkOptions) -> Finding:
    fonts = [item for item in objects.reachable_dictionaries() if item.get("/Subtype") == "/Type3"]
    inventory: list[object] = []
    violations: list[object] = []
    uncertain = False
    count = 0
    remaining = [_MAX_OPERATIONS]
    for index, font in enumerate(fonts, 1):
        programs = objects.dictionary(font.get("/CharProcs"))
        if not programs:
            uncertain = True
        for name, reference in sorted(programs.items()):
            count += 1
            if count > _MAX_OPERATIONS:
                raise PreparationError("Type 3 font inventory exceeds the glyph-program limit")
            raster, vector, incomplete = _glyph_program(
                objects, reference, font.get("/Resources", {}), remaining
            )
            classification = (
                "mixed"
                if raster and vector
                else "bitmap"
                if raster
                else "vector"
                if vector
                else "empty"
            )
            row = {
                "font_index": index,
                "glyph": name,
                "classification": classification,
                "incomplete": incomplete,
            }
            if len(inventory) < _MAX_SAMPLES:
                inventory.append(row)
            uncertain |= incomplete
            if raster and options.forbid_bitmap_type3_glyphs:
                violations.append(row)
    return _result(
        "pdf.type3_glyph_programs",
        "Inspected declared Type 3 glyph programs for image and vector painting.",
        violations,
        uncertain=uncertain,
        font_count=len(fonts),
        glyph_count=count,
        inventory=inventory,
        forbidden_bitmap=options.forbid_bitmap_type3_glyphs,
        scope="Reachable declared glyph programs, including supported nested Form XObjects. "
        "Unused glyphs may be inventoried. Unsupported operators, compressed/opaque streams "
        "and inline-image tails retain uncertainty; Type 3 alone never implies bitmap glyphs.",
    )


async def _inspect_type3(session: _Session, pages: int, options: PdfArtworkOptions) -> Finding:
    objects = await session.measure(
        "glyph-objects",
        [
            "qpdf",
            "--json=2",
            "--json-key=qpdf",
            "--json-key=pages",
            "--json-stream-data=none",
            "@PDF@",
        ],
        lambda execution, _: _Objects(execution.stdout, pages),
    )
    references: set[str] = set()
    for font in objects.reachable_dictionaries():
        if font.get("/Subtype") == "/Type3":
            for value in objects.dictionary(font.get("/CharProcs")).values():
                if isinstance(value, str) and re.fullmatch(r"\d+ \d+ R", value):
                    references.add(value)
    if not references:
        return _type3_finding(objects, options)
    # Include Form programs for supported recursive Do calls, but never request
    # image pixels or font-file streams. Decoding every PDF stream can expand a
    # small image-heavy paper far beyond the diagnostic output budget.
    for key, entry in objects.objects.items():
        if (
            isinstance(entry, dict)
            and isinstance(entry.get("stream"), dict)
            and isinstance(entry["stream"].get("dict"), dict)
            and entry["stream"]["dict"].get("/Subtype") == "/Form"
        ):
            reference = key.removeprefix("obj:")
            if re.fullmatch(r"\d+ \d+ R", reference):
                references.add(reference)
    if len(references) > 1000:
        raise PreparationError("Type 3 program selection exceeds the bounded object limit")

    def decoded(execution: CommandResult, _directory: Path) -> Finding:
        try:
            document = json.loads(execution.stdout)
            entries = document["qpdf"]
            if (
                len(entries) != 2
                or entries[0]["jsonversion"] != 2
                or not isinstance(entries[1], dict)
            ):
                raise ValueError("Unsupported stream JSON")
            for reference in references:
                key = "obj:" + reference
                value = entries[1][key]
                if not isinstance(value, dict):
                    raise ValueError("Invalid stream object")
                objects.objects[key] = value
        except (ValueError, KeyError, TypeError, RecursionError) as error:
            raise PreparationError(
                "qpdf did not return every selected glyph/Form program"
            ) from error
        return _type3_finding(objects, options)

    selectors = [
        "--json-object=" + ",".join(reference.split()[:2]) for reference in sorted(references)
    ]
    return await session.measure(
        "glyph-programs",
        [
            "qpdf",
            "--json=2",
            "--json-key=qpdf",
            "--decode-level=all",
            "--json-stream-data=inline",
            *selectors,
            "@PDF@",
        ],
        decoded,
    )


def _gray_png(width: int, height: int, pixels: bytes) -> bytes:
    """Encode already validated 8-bit grayscale pixels without another dependency."""

    def chunk(kind: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload))
            + kind
            + payload
            + struct.pack(">I", zlib.crc32(kind + payload))
        )

    rows = b"".join(b"\0" + pixels[y * width : (y + 1) * width] for y in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


async def _render_findings(
    session: _Session,
    sizes: list[tuple[float, float]],
    unsupported: set[int],
    options: PdfArtworkOptions,
    *,
    whole_pages: bool,
) -> list[Finding]:
    result: list[Finding] = []
    regions = options.figure_regions
    if whole_pages and not regions:
        regions = tuple(
            PdfRegion(f"Input page {index}", index, 0, 0, *size)
            for index, size in enumerate(sizes, 1)
        )
    selected_rules = [
        name
        for name in _configured(options)
        if name
        in {
            "pdf.figure_whitespace",
            "pdf.figure_edge_ink",
            "pdf.grayscale_preview",
        }
    ]
    if not regions and any(rule != "pdf.grayscale_preview" for rule in selected_rules):
        result.extend(
            _unavailable(
                rule,
                PreparationError("Figure measurement needs explicit regions on a manuscript PDF"),
                advisory=True,
            )
            for rule in selected_rules
            if rule != "pdf.grayscale_preview"
        )
    for region in regions:
        if region.page > len(sizes):
            result.extend(
                _unavailable(
                    rule,
                    PreparationError(f"Figure region {region.name} is outside the document"),
                    advisory=True,
                )
                for rule in selected_rules
                if rule != "pdf.grayscale_preview"
            )
    page_numbers = (
        range(1, len(sizes) + 1)
        if options.grayscale_preview
        else sorted({r.page for r in regions if r.page <= len(sizes)})
    )
    try:
        _require_rendered_pages(len(page_numbers), "Rendered artwork measurement")
    except PreparationError as error:
        # Inspection reads long documents; a render per page keeps the smaller cap.
        result.extend(
            _unavailable(rule, error, advisory=True)
            for rule in selected_rules
            if regions or rule == "pdf.grayscale_preview"
        )
        return result
    for page in page_numbers:
        width_pt, height_pt = sizes[page - 1]
        expected = (
            math.ceil(width_pt * options.raster_dpi / 72),
            math.ceil(height_pt * options.raster_dpi / 72),
        )
        try:
            if math.prod(expected) > _MAX_PIXELS or max(expected) > 4000:
                raise PreparationError("Artwork preview exceeds bounded raster dimensions")

            def measure(
                _execution: CommandResult,
                directory: Path,
                *,
                expected: tuple[int, int] = expected,
                page: int = page,
                width_pt: float = width_pt,
                height_pt: float = height_pt,
            ) -> list[Finding]:
                width, height, pixels = _gray_raster(
                    directory / "page.pgm",
                    min(session.runner.limits.max_file_bytes, _MAX_PIXELS + 100),
                )
                if any(
                    abs(actual - target) > 1
                    for actual, target in zip((width, height), expected, strict=True)
                ):
                    raise PreparationError("Artwork render dimensions disagree with page geometry")
                findings: list[Finding] = []
                if options.grayscale_preview:
                    preview = session.work / "previews" / f"page-{page}.png"
                    preview.parent.mkdir(exist_ok=True)
                    # Copy validated bytes while this operation still owns its render lease.
                    encoded = _gray_png(width, height, pixels)
                    if len(encoded) > session.runner.limits.max_file_bytes:
                        raise PreparationError("Encoded artwork preview exceeds the file limit")
                    preview.write_bytes(encoded)
                    findings.append(
                        _result(
                            "pdf.grayscale_preview",
                            f"Created grayscale preview for page {page}.",
                            [],
                            page=page,
                            artifacts=[str(preview)],
                            raster_dpi=options.raster_dpi,
                            scope="Review preview; semantic color independence is unverified.",
                        )
                    )
                pixel_visits = 0
                for region in (item for item in regions if item.page == page):
                    cancellation_point()
                    active = [rule for rule in selected_rules if rule != "pdf.grayscale_preview"]
                    if page in unsupported or not _contains(
                        (0, 0, width_pt, height_pt), region.box, 0
                    ):
                        findings.extend(
                            _unavailable(
                                rule,
                                PreparationError(
                                    f"Figure region {region.name} has unsupported page geometry"
                                ),
                                advisory=True,
                            )
                            for rule in active
                        )
                        continue
                    left, top = (
                        math.floor(region.left_pt * width / width_pt),
                        math.floor(region.top_pt * height / height_pt),
                    )
                    right, bottom = (
                        math.ceil(region.right_pt * width / width_pt),
                        math.ceil(region.bottom_pt * height / height_pt),
                    )
                    pixel_visits += (right - left) * (bottom - top)
                    if pixel_visits > _MAX_PIXEL_VISITS:
                        findings.extend(
                            replace(
                                _unavailable(
                                    rule,
                                    PreparationError(
                                        "Figure regions exceed the bounded pixel sampling work"
                                    ),
                                    advisory=True,
                                ),
                                details={"page": page, "region": region.name},
                            )
                            for rule in active
                        )
                        continue
                    ink_x: list[int] = []
                    min_x, min_y, max_x, max_y = right, bottom, left - 1, top - 1
                    band = options.figure_edge_band_pt or 0
                    band_x, band_y = (
                        math.ceil(band * width / width_pt),
                        math.ceil(band * height / height_pt),
                    )
                    edge_ink = 0
                    ink = 0
                    for y in range(top, bottom):
                        cancellation_point()
                        ink_x = [
                            x
                            for x in range(left, right)
                            if abs(pixels[y * width + x] - options.background_gray)
                            >= options.ink_difference
                        ]
                        if not ink_x:
                            continue
                        ink += len(ink_x)
                        min_x, max_x, min_y, max_y = (
                            min(min_x, ink_x[0]),
                            max(max_x, ink_x[-1]),
                            min(min_y, y),
                            max(max_y, y),
                        )
                        edge_ink += sum(
                            x < left + band_x
                            or x >= right - band_x
                            or y < top + band_y
                            or y >= bottom - band_y
                            for x in ink_x
                        )
                    total = (right - left) * (bottom - top)
                    whitespace = (
                        1 if not ink else 1 - ((max_x - min_x + 1) * (max_y - min_y + 1) / total)
                    )
                    bounds = (
                        None
                        if not ink
                        else (
                            min_x * width_pt / width,
                            min_y * height_pt / height,
                            (max_x + 1) * width_pt / width,
                            (max_y + 1) * height_pt / height,
                        )
                    )
                    details: dict[str, Any] = {
                        "page": page,
                        "region": region.name,
                        "content_bounds_pt": bounds,
                        "outside_content_box_ratio": whitespace,
                        "ink_pixels": ink,
                        "background_gray": options.background_gray,
                        "raster_dpi": options.raster_dpi,
                    }
                    if options.max_figure_whitespace_ratio is not None:
                        findings.append(
                            _result(
                                "pdf.figure_whitespace",
                                f"Measured the rendered content box in {region.name}.",
                                [details]
                                if whitespace > options.max_figure_whitespace_ratio
                                else [],
                                advisory=True,
                                **details,
                                maximum_ratio=options.max_figure_whitespace_ratio,
                                scope="Area outside the ink box under the configured background; "
                                "internal white areas and intended padding are not judged.",
                            )
                        )
                    if options.figure_edge_band_pt is not None:
                        findings.append(
                            _result(
                                "pdf.figure_edge_ink",
                                f"Measured ink near the boundary of {region.name}.",
                                [{**details, "edge_ink_pixels": edge_ink}] if edge_ink else [],
                                advisory=True,
                                **details,
                                edge_band_pt=options.figure_edge_band_pt,
                                scope="Boundary ink is a cropping/border candidate, not proof of "
                                "clipping; intended borders and touching content need review.",
                            )
                        )
                return findings

            result.extend(
                await session.measure(
                    f"artwork-render-{page}",
                    [
                        "pdftoppm",
                        "-f",
                        str(page),
                        "-l",
                        str(page),
                        "-singlefile",
                        "-gray",
                        "-r",
                        str(options.raster_dpi),
                        "@PDF@",
                        "page",
                    ],
                    measure,
                    render=True,
                    memory_mb=128,
                    threaded_parser=True,
                )
            )
        except (PreparationError, OSError) as error:
            result.extend(
                replace(_unavailable(rule, error, advisory=True), details={"page": page})
                for rule in selected_rules
            )
    return result


def _pixel_region(
    region: PdfRegion, size: tuple[float, float], raster: tuple[int, int]
) -> tuple[int, int, int, int]:
    if not _contains((0, 0, *size), region.box, 0):
        raise PreparationError("Contrast sample region lies outside the rendered page")
    # Include only pixels whose full area is contained within the supplied patch.
    left = math.ceil(region.left_pt * raster[0] / size[0])
    top = math.ceil(region.top_pt * raster[1] / size[1])
    right = math.floor(region.right_pt * raster[0] / size[0])
    bottom = math.floor(region.bottom_pt * raster[1] / size[1])
    if left >= right or top >= bottom:
        raise PreparationError("Contrast sample has no fully contained pixel at the selected DPI")
    return left, top, right, bottom


def _pixel_colors(
    pixels: bytes, width: int, box: tuple[int, int, int, int]
) -> tuple[tuple[int, int, int], tuple[int, int, int]]:
    minima = [255, 255, 255]
    maxima = [0, 0, 0]
    left, top, right, bottom = box
    for y in range(top, bottom):
        cancellation_point()
        for x in range(left, right):
            offset = (y * width + x) * 3
            for channel in range(3):
                value = pixels[offset + channel]
                minima[channel] = min(minima[channel], value)
                maxima[channel] = max(maxima[channel], value)
    return (minima[0], minima[1], minima[2]), (maxima[0], maxima[1], maxima[2])


def _contrast_interval(
    foreground: tuple[float, float], background: tuple[float, float]
) -> tuple[float, float]:
    low_fg, high_fg = foreground
    low_bg, high_bg = background
    minimum = 1.0
    if high_fg < low_bg:
        minimum = (low_bg + 0.05) / (high_fg + 0.05)
    elif high_bg < low_fg:
        minimum = (low_fg + 0.05) / (high_bg + 0.05)
    maximum = max((high_fg + 0.05) / (low_bg + 0.05), (high_bg + 0.05) / (low_fg + 0.05))
    return minimum, maximum


async def _sample_contrast_findings(
    session: _Session,
    sizes: list[tuple[float, float]],
    unsupported: set[int],
    options: PdfArtworkOptions,
) -> list[Finding]:
    rule = "pdf.rendered_sample_contrast"
    result: list[Finding] = []
    for sample in options.contrast_samples:
        if sample.foreground.page > len(sizes) or sample.foreground.page in unsupported:
            result.append(
                replace(
                    _unavailable(
                        rule,
                        PreparationError(
                            "Contrast sample page is absent or has unsupported geometry"
                        ),
                        advisory=True,
                    ),
                    details={"sample": sample.name, "page": sample.foreground.page},
                )
            )
    for page in sorted(
        {
            sample.foreground.page
            for sample in options.contrast_samples
            if sample.foreground.page <= len(sizes) and sample.foreground.page not in unsupported
        }
    ):
        selected = tuple(
            sample for sample in options.contrast_samples if sample.foreground.page == page
        )
        size = sizes[page - 1]
        expected = tuple(math.ceil(value * options.raster_dpi / 72) for value in size)
        name = f"contrast-render-{page}"
        try:
            if math.prod(expected) > _MAX_PIXELS or max(expected) > 4000:
                raise PreparationError("Contrast render exceeds bounded raster dimensions")

            def measure(
                _execution: CommandResult,
                directory: Path,
                *,
                size: tuple[float, float] = size,
                expected: tuple[int, ...] = expected,
                selected: tuple[PdfContrastSample, ...] = selected,
                page: int = page,
            ) -> list[Finding]:
                path = directory / "page.ppm"
                if not path.is_file() or path.stat().st_size > session.runner.limits.max_file_bytes:
                    raise PreparationError(
                        "Contrast render exceeds the configured file limit or is missing"
                    )
                dimensions, pixels = _read_raster(path, max_pixels=_MAX_PIXELS)
                if any(
                    abs(actual - target) > 1
                    for actual, target in zip(dimensions, expected, strict=True)
                ):
                    raise PreparationError("Contrast render dimensions disagree with page geometry")
                findings: list[Finding] = []
                pixel_visits = 0
                for sample in selected:
                    cancellation_point()
                    try:
                        regions = (
                            _pixel_region(sample.foreground, size, dimensions),
                            _pixel_region(sample.background, size, dimensions),
                        )
                        pixel_visits += sum(
                            (box[2] - box[0]) * (box[3] - box[1]) for box in regions
                        )
                        if pixel_visits > _MAX_PIXEL_VISITS:
                            raise PreparationError(
                                "Contrast patches exceed the bounded pixel sampling work"
                            )
                        colors = [_pixel_colors(pixels, dimensions[0], box) for box in regions]
                        spreads = [
                            max(high - low for low, high in zip(minimum, maximum, strict=True))
                            for minimum, maximum in colors
                        ]
                        luminances = [
                            (
                                _luminance((minimum[0] / 255, minimum[1] / 255, minimum[2] / 255)),
                                _luminance((maximum[0] / 255, maximum[1] / 255, maximum[2] / 255)),
                            )
                            for minimum, maximum in colors
                        ]
                        minimum, maximum = _contrast_interval(luminances[0], luminances[1])
                        uniform = max(spreads) <= sample.max_channel_spread
                        incomplete = not uniform or minimum < sample.minimum_ratio <= maximum
                        details: dict[str, Any] = {
                            "page": page,
                            "sample": sample.name,
                            "foreground_bounds_pt": sample.foreground.box,
                            "background_bounds_pt": sample.background.box,
                            "foreground_rgb_range": colors[0],
                            "background_rgb_range": colors[1],
                            "contrast_ratio_min": minimum,
                            "contrast_ratio_max": maximum,
                            "minimum_ratio": sample.minimum_ratio,
                            "channel_spreads": spreads,
                            "allowed_channel_spread": sample.max_channel_spread,
                        }
                        findings.append(
                            _result(
                                rule,
                                f"Compared rendered color patches for {sample.name}.",
                                [details] if uniform and maximum < sample.minimum_ratio else [],
                                uncertain=incomplete,
                                advisory=True,
                                **details,
                                scope="Rendered/composited RGB patches interpreted as sRGB. "
                                "Channel extrema give conservative luminance/ratio bounds. "
                                "Nonuniform "
                                "patches need an explicit spread tolerance; text meaning, all text "
                                "contrast, perceptual quality and accessibility are not inferred.",
                            )
                        )
                    except PreparationError as error:
                        findings.append(
                            replace(
                                _unavailable(rule, error, advisory=True),
                                details={"sample": sample.name, "page": page},
                            )
                        )
                return findings

            result.extend(
                await session.measure(
                    name,
                    [
                        "pdftoppm",
                        "-f",
                        str(page),
                        "-l",
                        str(page),
                        "-singlefile",
                        "-r",
                        str(options.raster_dpi),
                        "@PDF@",
                        "page",
                    ],
                    measure,
                    render=True,
                    memory_mb=128,
                    threaded_parser=True,
                )
            )
        except (PreparationError, OSError) as error:
            result.extend(
                replace(
                    _unavailable(rule, error, advisory=True),
                    details={"sample": sample.name, "page": page},
                )
                for sample in selected
            )
        finally:
            # _Session removes grayscale intermediates; this adapter owns its RGB render.
            (session.work / name / "command" / "page.ppm").unlink(missing_ok=True)
    return result


async def inspect_pdf_artwork(
    pdf: Path,
    work: Path,
    runner: ToolRunner,
    *,
    options: PdfArtworkOptions | None = None,
    budget: ResourceBudget | None = None,
    whole_pages: bool = False,
) -> list[Finding]:
    """Inspect one PDF; whole_pages is reserved for explicitly selected input figures."""
    options = options or PdfArtworkOptions()
    configured = _configured(options)
    if not configured:
        return []
    try:
        session = await _session(pdf, work, runner, budget)
        fields = await session.measure(
            "properties",
            ["pdfinfo", "-enc", "UTF-8", "@PDF@"],
            lambda result, _: _properties(result.stdout),
        )
        pages = _page_count(fields)
        unsupported, sizes = await session.measure(
            "geometry",
            ["pdfinfo", "-box", "-f", "1", "-l", str(pages), "@PDF@"],
            lambda result, _: _geometry_support(result.stdout, pages),
        )
    except (PreparationError, OSError) as error:
        return [_unavailable(rule, error) for rule in configured]
    result: list[Finding] = []
    trace_rules = [
        rule
        for rule in configured
        if rule
        not in {
            "pdf.figure_whitespace",
            "pdf.figure_edge_ink",
            "pdf.grayscale_preview",
            "pdf.type3_glyph_programs",
            "pdf.rendered_sample_contrast",
        }
    ]
    if trace_rules:
        try:
            drawings, boxes, unknown = await session.measure(
                "trace",
                ["mutool", "trace", "@PDF@", f"1-{pages}"],
                lambda execution, _: _trace(execution.stdout, pages),
            )
            scalable_text_fonts = False
            if (options.classify_artwork or options.category_min_dpi) and any(
                drawing.node.tag in {"fill_text", "stroke_text"} for drawing in drawings
            ):
                try:
                    font_types = await session.measure(
                        "composition-fonts",
                        ["pdffonts", "@PDF@"],
                        lambda execution, _: [
                            font["type"] for font in _font_inventory(execution.stdout)
                        ],
                    )
                    scalable = {
                        "Type 1",
                        "Type 1C",
                        "Type 1C (OT)",
                        "TrueType",
                        "TrueType (OT)",
                        "CID Type 0",
                        "CID Type 0C",
                        "CID Type 0C (OT)",
                        "CID TrueType",
                        "CID TrueType (OT)",
                    }
                    scalable_text_fonts = bool(font_types) and all(
                        kind in scalable for kind in font_types
                    )
                except (PreparationError, OSError):
                    # Text-bearing traces need this evidence and retain uncertainty.
                    pass
            async with (
                session.budget.lease(f"{session.work.name}: artwork geometry", memory_mb=64)
                if session.budget is not None
                else nullcontext()
            ):
                for index, (box, size) in enumerate(zip(boxes, sizes, strict=True), 1):
                    if box[:2] != (0, 0) or any(
                        abs(actual - expected) > options.geometry_tolerance_pt
                        for actual, expected in zip(box[2:], size, strict=True)
                    ):
                        unsupported.add(index)
                result.extend(
                    [
                        *_composition(
                            drawings, options, unknown, scalable_text_fonts=scalable_text_fonts
                        ),
                        *_geometry_findings(drawings, boxes, unknown, unsupported, options),
                    ]
                )
        except (PreparationError, OSError) as error:
            result.extend(
                _unavailable(rule, error, advisory=rule != "pdf.artwork_category_resolution")
                for rule in trace_rules
            )
    if options.classify_type3_glyphs or options.forbid_bitmap_type3_glyphs:
        try:
            result.append(await _inspect_type3(session, pages, options))
        except (PreparationError, OSError) as error:
            result.append(_unavailable("pdf.type3_glyph_programs", error))
    if (
        options.max_figure_whitespace_ratio is not None
        or options.figure_edge_band_pt is not None
        or options.grayscale_preview
    ):
        result.extend(
            await _render_findings(session, sizes, unsupported, options, whole_pages=whole_pages)
        )
    if options.contrast_samples:
        result.extend(await _sample_contrast_findings(session, sizes, unsupported, options))
    return result


async def inspect_artwork_inputs(
    figures: tuple[Path, ...],
    work: Path,
    runner: ToolRunner,
    *,
    options: PdfArtworkOptions | None = None,
    budget: ResourceBudget | None = None,
) -> list[Finding]:
    """Apply the selected artwork policy separately to explicit input PDF figures."""
    if (
        not isinstance(figures, tuple)
        or len(figures) > 300
        or any(not isinstance(figure, Path) for figure in figures)
    ):
        raise PreparationError("Artwork input inspection requires at most 300 immutable PDF paths")
    if work.exists():
        raise PreparationError("Artwork inputs require a new inspection workspace")
    result = []
    for index, figure in enumerate(dict.fromkeys(figures), 1):
        findings = await inspect_pdf_artwork(
            figure, work / str(index), runner, options=options, budget=budget, whole_pages=True
        )
        result.extend(replace(finding, path=str(figure)) for finding in findings)
    return result
