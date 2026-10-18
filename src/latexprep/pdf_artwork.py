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
from collections.abc import Callable
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple

from .models import Finding, PreparationError
from .option_config import require_booleans
from .pdf_previews import PNG_SIGNATURE
from .pdf_support import (
    COLOR_OPERATIONS,
    DOCUMENT,
    MAX_FIGURE_INPUTS,
    MAX_MEASUREMENT_DPI,
    MAX_MEASUREMENT_PIXELS,
    MAX_PAIR_CHECKS,
    MAX_SAMPLES,
    MIN_MEASUREMENT_DPI,
    PdfRegion,
    PdfToolSession,
    QpdfObjectGraph,
    contains,
    exceeds_measurement_raster,
    finite_number,
    font_inventory,
    heuristic_finding,
    intersection_area,
    is_object_reference,
    measurement_finding,
    measurement_raster_size,
    mupdf_trace_argv,
    open_measurement_session,
    page_boxes_argv,
    page_count,
    page_frames,
    parse_mupdf_trace,
    pdfinfo_properties,
    qpdf_objects_argv,
    read_gray_raster,
    read_rgb_raster,
    relative_luminance,
    require_distinct_strings,
    require_number,
    require_rendered_pages,
    unavailable_finding,
)
from .runtime import CommandResult, CommandRunner
from .scheduler import ResourceBudget, cancellation_point

_MAX_OPERATIONS = 50_000
_MAX_PIXEL_VISITS = 8_000_000
_MAX_GLYPH_STREAM_BYTES = 1_000_000
Box = tuple[float, float, float, float]
Point = tuple[float, float]
Matrix = tuple[float, float, float, float, float, float]
Line = tuple[Point, Point, int]
_IMAGE_OPERATIONS = frozenset({"fill_image", "fill_image_mask"})
_TEXT_OPERATIONS = frozenset({"fill_text", "stroke_text"})
# Operations with a measurable painted extent: affine images and straight paths.
_EXTENT_OPERATIONS = frozenset({"fill_image", "fill_image_mask", "fill_path", "stroke_path"})
_SCALABLE_FONT_TYPES = frozenset(
    {
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
)
_TOKEN = re.compile(r"/?[^\s\x00()<>\[\]{}%/]+|[\[\]]")
_LINE_END = re.compile(r"[\r\n]")
_NUMBER = re.compile(r"[+-]?(?:\d+(?:\.\d*)?|\.\d+)")
_SIMPLE_GLYPH_OPERATORS = frozenset(
    "q Q cm w J j M d ri i g G rg RG k K cs CS sc SC m l c v y h re n W W* d0 d1 [ ]".split()
)
_PAINT_OPERATORS = frozenset("S s f F f* B B* b b*".split())
_REGION_RULES = ("pdf.figure_whitespace", "pdf.figure_edge_ink")
_PREVIEW_RULE = "pdf.grayscale_preview"
_CONTRAST_RULE = "pdf.rendered_sample_contrast"
_CATEGORY_RULE = "pdf.artwork_category_resolution"
# Rules measured from the drawing operations of a MuPDF trace.
DRAWING_TRACE_RULES = frozenset(
    {
        "pdf.printer_marks",
        "pdf.drawing_bounds",
        "pdf.clipping_candidates",
        "pdf.artwork_composition",
        _CATEGORY_RULE,
        "pdf.transformed_stroke_width",
        "pdf.raster_region_candidates",
    }
)


@dataclass(frozen=True)
class PdfContrastSample:
    """User-selected foreground/background patches, with no inferred text semantics."""

    name: str
    foreground: PdfRegion
    background: PdfRegion
    minimum_ratio: float
    max_channel_spread: int = 0

    def __post_init__(self) -> None:
        require_distinct_strings((self.name,), "Contrast sample name")
        if not isinstance(self.foreground, PdfRegion) or not isinstance(self.background, PdfRegion):
            raise PreparationError("Contrast samples require foreground and background PDF regions")
        if self.foreground.page != self.background.page:
            raise PreparationError("Foreground and background samples must be on the same page")
        require_number(self.minimum_ratio, "Minimum rendered sample contrast")
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
    required_color_space: str | None = None
    raster_dpi: int = 72
    background_gray: int = 255
    ink_difference: int = 5
    geometry_tolerance_pt: float = 0.5

    def __post_init__(self) -> None:
        require_booleans(
            self,
            "detect_printer_marks",
            "check_clipping",
            "classify_artwork",
            "classify_type3_glyphs",
            "forbid_bitmap_type3_glyphs",
            "grayscale_preview",
        )
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
                require_number(getattr(self, name), name)
        for name in ("max_figure_whitespace_ratio", "min_raster_region_coverage"):
            value = getattr(self, name)
            if value is not None:
                require_number(value, name, allow_zero=name == "max_figure_whitespace_ratio")
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
            require_distinct_strings((self.artwork_category,), "Artwork category")
        if not isinstance(self.category_min_dpi, tuple) or any(
            not isinstance(pair, tuple) or len(pair) != 2 for pair in self.category_min_dpi
        ):
            raise PreparationError("Category DPI requires immutable category/minimum pairs")
        require_distinct_strings(
            tuple(pair[0] for pair in self.category_min_dpi), "Artwork categories"
        )
        for _, minimum in self.category_min_dpi:
            require_number(minimum, "Category minimum DPI")
        if self.category_min_dpi and self.artwork_category not in dict(self.category_min_dpi):
            raise PreparationError("Select an artwork category with an explicit DPI threshold")
        if self.required_color_space not in {None, "rgb", "cmyk"}:
            raise PreparationError("required_color_space must be 'rgb' or 'cmyk'")
        if (
            type(self.raster_dpi) is not int
            or not MIN_MEASUREMENT_DPI <= self.raster_dpi <= MAX_MEASUREMENT_DPI
        ):
            raise PreparationError(
                "Artwork raster DPI must be an integer from "
                f"{MIN_MEASUREMENT_DPI} to {MAX_MEASUREMENT_DPI}"
            )
        if type(self.background_gray) is not int or not 0 <= self.background_gray <= 255:
            raise PreparationError("Artwork background must be an 8-bit gray value")
        if type(self.ink_difference) is not int or not 1 <= self.ink_difference <= 255:
            raise PreparationError("Artwork ink difference must be an integer from 1 to 255")
        require_number(self.geometry_tolerance_pt, "Artwork geometry tolerance", allow_zero=True)


def configured_artwork_rules(options: PdfArtworkOptions) -> list[str]:
    return [
        name
        for name, enabled in (
            ("pdf.printer_marks", options.detect_printer_marks),
            ("pdf.figure_whitespace", options.max_figure_whitespace_ratio is not None),
            ("pdf.figure_edge_ink", options.figure_edge_band_pt is not None),
            ("pdf.drawing_bounds", bool(options.drawing_regions)),
            ("pdf.clipping_candidates", options.check_clipping),
            ("pdf.artwork_composition", options.classify_artwork),
            (_CATEGORY_RULE, bool(options.category_min_dpi)),
            (
                "pdf.type3_glyph_programs",
                options.classify_type3_glyphs or options.forbid_bitmap_type3_glyphs,
            ),
            ("pdf.transformed_stroke_width", options.min_stroke_width_pt is not None),
            ("pdf.raster_region_candidates", bool(options.raster_regions)),
            (_PREVIEW_RULE, options.grayscale_preview),
            (_CONTRAST_RULE, bool(options.contrast_samples)),
            ("figure.color_space", options.required_color_space is not None),
        )
        if enabled
    ]


def artwork_checks_enabled(options: PdfArtworkOptions) -> bool:
    """Tuning values alone do not select a measurement or require a source graph."""
    return bool(configured_artwork_rules(options))


def _matrix(node: ET.Element) -> Matrix:
    values = tuple(finite_number(value) for value in node.get("transform", "").split())
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


def _box(points: list[Point]) -> Box:
    if not points:
        raise PreparationError("Drawing path has no measured vertices")
    return (
        min(p[0] for p in points),
        min(p[1] for p in points),
        max(p[0] for p in points),
        max(p[1] for p in points),
    )


def _clipped(box: Box, clip: Box) -> Box:
    return max(box[0], clip[0]), max(box[1], clip[1]), min(box[2], clip[2]), min(box[3], clip[3])


def _segments(node: ET.Element) -> tuple[list[tuple[Point, Point]], bool]:
    result: list[tuple[Point, Point]] = []
    start = previous = None
    for child in node:
        if child.tag in {"moveto", "lineto"}:
            current = finite_number(child.get("x")), finite_number(child.get("y"))
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


class _DrawingTrace(NamedTuple):
    drawings: list[_Drawing]
    page_boxes: list[Box]
    uncertain_pages: set[int]


def _drawing_trace(output: str, pages: int) -> _DrawingTrace:
    trace = parse_mupdf_trace(
        output,
        pages,
        invalid_page_message="Trace page numbers are missing or invalid",
        incomplete_message="Trace did not report every page exactly once",
    )
    drawings: list[_Drawing] = []
    page_boxes: list[Box] = []
    uncertain_pages: set[int] = set()
    for number, page in trace.pages:
        values = tuple(finite_number(value) for value in page.get("mediabox", "").split())
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
                clips.append(
                    _clipped(clip, parent) if clip is not None and parent is not None else None
                )
            elif node.tag == "pop_clip":
                if len(clips) == 1:
                    raise PreparationError("Trace has an unmatched clipping restore")
                clips.pop()
            elif node.tag in COLOR_OPERATIONS or node.tag == "ignore_text":
                index += 1
                box = None
                if node.tag in _IMAGE_OPERATIONS:
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
    return _DrawingTrace(drawings, page_boxes, uncertain_pages)


def _drawing_sample(drawing: _Drawing) -> dict[str, object]:
    return {
        "page": drawing.page,
        "operation": drawing.index,
        "kind": drawing.node.tag,
        "bounds_pt": drawing.box,
    }


class _FontEvidence(NamedTuple):
    """Whether every font is scalable, and why that could not be established if it failed."""

    scalable: bool
    failure: str | None = None


class _Composition(NamedTuple):
    """Painted operations by kind for the whole selected PDF."""

    raster: list[_Drawing]
    vector: list[_Drawing]
    text: list[_Drawing]
    unsupported: bool
    scalable_text_fonts: bool
    font_failure: str | None = None

    @property
    def classification(self) -> str:
        if self.raster and self.vector:
            return "mixed"
        return "raster" if self.raster else "vector" if self.vector else "empty"


def _composition(drawings: list[_Drawing], unknown: set[int], fonts: _FontEvidence) -> _Composition:
    raster = [d for d in drawings if d.node.tag in _IMAGE_OPERATIONS]
    text = [d for d in drawings if d.node.tag in _TEXT_OPERATIONS]
    vector = [d for d in drawings if d.node.tag in {"fill_path", "stroke_path"}]
    if fonts.scalable:
        vector.extend(text)
    unsupported = (
        bool(unknown)
        or any(d.node.tag == "fill_shade" for d in drawings)
        or bool(text and not fonts.scalable)
    )
    return _Composition(raster, vector, text, unsupported, fonts.scalable, fonts.failure)


def _composition_finding(composition: _Composition) -> Finding:
    message = "Inventoried painted raster and vector operations."
    if composition.font_failure is not None:
        message = (
            "Inventoried painted raster and vector operations, but pdffonts could not list "
            f"the fonts ({composition.font_failure}), so text could not be counted as vector "
            "artwork. Resolve that error and run the check again."
        )
    return heuristic_finding(
        "pdf.artwork_composition",
        message,
        [],
        incomplete=composition.unsupported or composition.classification == "empty",
        font_inventory_error=composition.font_failure,
        classification=composition.classification,
        raster_operations=len(composition.raster),
        vector_operations=len(composition.vector),
        unclassified_text_operations=0
        if composition.scalable_text_fonts
        else len(composition.text),
        scope="Whole selected PDF, including text and backgrounds; figure categories, "
        "font text needs a complete scalable-font inventory. Outlined lettering "
        "and source-to-final attribution are not inferred.",
    )


class _PlacementSampling(NamedTuple):
    x_dpi: float
    y_dpi: float
    minimum_direction_dpi: float


def _placement_sampling(drawing: _Drawing) -> _PlacementSampling | None:
    """Axis and coarsest-direction sampling of one image placement, if measurable."""
    node = drawing.node
    a, b, c, d, _, _ = _matrix(node)
    width, height = finite_number(node.get("width")), finite_number(node.get("height"))
    lengths = math.hypot(a, b), math.hypot(c, d)
    determinant = a * d - b * c
    if (
        min(width, height, *lengths) <= 0
        or not math.isfinite(determinant)
        or abs(determinant) < 1e-12
    ):
        return None
    axes = 72 * width / lengths[0], 72 * height / lengths[1]
    # Largest singular value of the points-per-pixel transform bounds
    # the coarsest sampling direction under shear as well as rotation.
    aa, bb, cc, dd = a / width, b / width, c / height, d / height
    norm = aa * aa + bb * bb + cc * cc + dd * dd
    if not math.isfinite(norm * norm) or norm <= 0:
        return None
    discriminant = max(0.0, norm * norm - 4 * (aa * dd - bb * cc) ** 2)
    coarse = 72 / math.sqrt((norm + math.sqrt(discriminant)) / 2)
    return _PlacementSampling(axes[0], axes[1], coarse)


def _category_resolution_finding(composition: _Composition, options: PdfArtworkOptions) -> Finding:
    category = options.artwork_category or ""
    minimum = dict(options.category_min_dpi)[category]
    measurements: list[object] = []
    violations: list[object] = []
    lowest: tuple[float, int] | None = None
    incomplete = composition.unsupported
    for drawing in composition.raster:
        sampling = _placement_sampling(drawing)
        if sampling is None:
            incomplete = True
            continue
        effective_dpi = min(sampling)
        measurement = {
            **_drawing_sample(drawing),
            "x_dpi": sampling.x_dpi,
            "y_dpi": sampling.y_dpi,
            "minimum_direction_dpi": sampling.minimum_direction_dpi,
            "effective_dpi": effective_dpi,
        }
        measurements.append(measurement)
        if effective_dpi < minimum:
            violations.append(measurement)
            if lowest is None or effective_dpi < lowest[0]:
                lowest = (effective_dpi, drawing.page)
    placements = len(composition.raster)
    if lowest is not None:
        message = (
            f"Image placements below the {minimum:g} dpi minimum for '{category}': "
            f"{len(violations)} of {placements}; the lowest is {lowest[0]:.0f} dpi "
            f"on page {lowest[1]}."
        )
    elif not placements:
        message = f"No raster image placements were found to compare with {minimum:g} dpi."
    elif len(measurements) < placements:
        message = (
            f"{len(measurements)} of {placements} image placements are at or above the "
            f"{minimum:g} dpi minimum for '{category}'; the others could not be measured."
        )
    else:
        message = (
            f"All {placements} image placements are at or above the {minimum:g} dpi minimum "
            f"for '{category}'."
        )
    return measurement_finding(
        _CATEGORY_RULE,
        message,
        violations,
        incomplete=incomplete or (not composition.raster and not composition.vector),
        category=options.artwork_category,
        minimum_dpi=minimum,
        measurements=measurements[:MAX_SAMPLES],
        placement_count=len(composition.raster),
        font_inventory_error=composition.font_failure,
        scope="Declared category and traced raster placements; axes and coarsest "
        "sampling includes rotation/shear. Vector operations have no raster DPI.",
    )


def _stroke_finding(
    drawings: list[_Drawing], options: PdfArtworkOptions, unknown: set[int]
) -> Finding:
    violations: list[object] = []
    thinnest: tuple[float, int] | None = None
    measured = 0
    uncertain = bool(unknown)
    for drawing in drawings:
        node = drawing.node
        if node.tag not in {"stroke_path", "stroke_text"}:
            continue
        width = finite_number(node.get("linewidth"))
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
            violations.append(
                {**_drawing_sample(drawing), "minimum_effective_width_pt": min(widths)}
            )
            if thinnest is None or min(widths) < thinnest[0]:
                thinnest = (min(widths), drawing.page)
    minimum = options.min_stroke_width_pt
    if thinnest is not None:
        message = (
            f"Strokes thinner than {minimum:g} pt after placement: {len(violations)}; "
            f"the thinnest is {thinnest[0]:.3g} pt on page {thinnest[1]}."
        )
    elif measured == 0:
        message = "No stroke could be measured for its thickness after placement."
    else:
        message = f"No measured stroke is thinner than {minimum:g} pt after placement."
        if uncertain:
            message += " Some strokes could not be measured."
    return heuristic_finding(
        "pdf.transformed_stroke_width",
        message,
        violations,
        incomplete=uncertain or measured == 0,
        measured_segments=measured,
        minimum_pt=options.min_stroke_width_pt,
        scope="Straight-segment normal thickness supports arbitrary nonsingular affine transforms; "
        "curves/text need uniform transforms. Hairlines, joins, caps and raster lines are omitted.",
    )


def _short_lines(drawings: list[_Drawing], page: int, max_length: float) -> list[Line]:
    """Transformed straight stroke segments on a page, with their operation index."""
    lines: list[Line] = []
    for drawing in drawings:
        if drawing.page != page or drawing.node.tag != "stroke_path":
            continue
        segments, supported = _segments(drawing.node)
        if not supported:
            continue
        matrix = _matrix(drawing.node)
        for first, second in segments:
            first, second = _point(first, matrix), _point(second, matrix)
            length = math.dist(first, second)
            if 0 < length <= max_length:
                lines.append((first, second, drawing.index))
    return lines


def _corner_marks(
    region: PdfRegion, lines: list[Line], tolerance: float, max_length: float
) -> list[object]:
    marks: list[object] = []
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
            and min(abs(first[0] - x), abs(second[0] - x)) <= max_length
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
            and min(abs(first[1] - y), abs(second[1] - y)) <= max_length
        ]
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
    region: PdfRegion, lines: list[Line], tolerance: float
) -> list[object] | None:
    """Crossed strokes outside the trim region; None when the pair work exceeds its bound.

    Crossed short horizontal/vertical strokes outside the trim rectangle are
    registration-target candidates. Graphic content can look identical.
    """
    horizontal = [line for line in lines if abs(line[0][1] - line[1][1]) <= tolerance]
    vertical = [line for line in lines if abs(line[0][0] - line[1][0]) <= tolerance]
    if len(horizontal) * len(vertical) > MAX_PAIR_CHECKS:
        return None
    marks: list[object] = []
    for first, second, horizontal_index in horizontal:
        for other_first, other_second, vertical_index in vertical:
            x = (other_first[0] + other_second[0]) / 2
            y = (first[1] + second[1]) / 2
            if (
                min(first[0], second[0]) + tolerance < x < max(first[0], second[0]) - tolerance
                and min(other_first[1], other_second[1]) + tolerance
                < y
                < max(other_first[1], other_second[1]) - tolerance
                and not contains(region.box, (x, y, x, y), tolerance)
            ):
                marks.append(
                    {
                        "page": region.page,
                        "region": region.name,
                        "center_pt": (x, y),
                        "operations": [horizontal_index, vertical_index],
                        "candidate": "crossed registration strokes",
                    }
                )
    return marks


def _mark_finding(
    drawings: list[_Drawing], options: PdfArtworkOptions, unsupported: set[int], boxes: list[Box]
) -> Finding:
    violations: list[object] = []
    measured = 0
    pages = len(boxes)
    uncertain = any(
        region.page > pages
        or region.page in unsupported
        or not contains(boxes[region.page - 1], region.box, 0)
        for region in options.trim_regions
    )
    uncertain |= any(page > pages for page in options.printer_mark_exemptions)
    tolerance = options.geometry_tolerance_pt
    max_length = options.printer_mark_max_length_pt or 0
    for region in options.trim_regions:
        if (
            region.page in options.printer_mark_exemptions
            or region.page in unsupported
            or region.page > pages
            or not contains(boxes[region.page - 1], region.box, 0)
        ):
            continue
        measured += 1
        lines = _short_lines(drawings, region.page, max_length)
        violations.extend(_corner_marks(region, lines, tolerance, max_length))
        registration = _registration_marks(region, lines, tolerance)
        if registration is None:
            uncertain = True
        else:
            violations.extend(registration)
    return heuristic_finding(
        "pdf.printer_marks",
        "Searched for short crop/registration strokes outside explicit trim regions.",
        violations,
        incomplete=uncertain or measured == 0,
        measured_trim_regions=measured,
        exempt_pages=options.printer_mark_exemptions,
        scope="Geometric paired-corner and crossed-stroke candidates. Outlined, curved and "
        "raster marks are unrecognized; intentional marks and similar artwork require review.",
    )


@dataclass(frozen=True, slots=True)
class _TracedArtwork:
    """Traced drawings and page frames shared by the drawing-geometry measurements."""

    drawings: list[_Drawing]
    boxes: list[Box]
    unknown: frozenset[int]
    unsupported: frozenset[int]
    options: PdfArtworkOptions


def _drawing_bounds_finding(artwork: _TracedArtwork) -> Finding:
    options = artwork.options
    violations: list[object] = []
    measured = 0
    uncertain = bool(artwork.unknown) or any(
        r.page > len(artwork.boxes) or r.page in artwork.unsupported
        for r in options.drawing_regions
    )
    for drawing in artwork.drawings:
        allowed = [r for r in options.drawing_regions if r.page == drawing.page]
        if not allowed or drawing.node.tag not in _EXTENT_OPERATIONS:
            continue
        if (
            drawing.box is None
            or drawing.page in artwork.unsupported
            or drawing.uncertain
            or drawing.clip is None
        ):
            uncertain = True
            continue
        measured += 1
        visible = _clipped(drawing.box, drawing.clip)
        if visible[0] >= visible[2] or visible[1] >= visible[3]:
            continue
        if not any(contains(r.box, visible, options.geometry_tolerance_pt) for r in allowed):
            violations.append({**_drawing_sample(drawing), "visible_bounds_pt": visible})
    return heuristic_finding(
        "pdf.drawing_bounds",
        "Compared transformed image and straight filled-path bounds with allowed regions.",
        violations,
        incomplete=uncertain or measured == 0,
        measured_operations=measured,
        scope="Affine image rectangles and polygon bounds after rectangular clipping. "
        "Text, strokes, curves, masks and unsupported page frames remain unmeasured.",
    )


def _clipping_finding(artwork: _TracedArtwork) -> Finding:
    violations: list[object] = []
    measured = 0
    uncertain = bool(artwork.unknown)
    for drawing in artwork.drawings:
        if drawing.node.tag not in _EXTENT_OPERATIONS:
            continue
        if drawing.box is None or drawing.clip is None or drawing.uncertain:
            uncertain = True
            continue
        measured += 1
        if not contains(drawing.clip, drawing.box, artwork.options.geometry_tolerance_pt):
            violations.append({**_drawing_sample(drawing), "clip_bounds_pt": drawing.clip})
    return heuristic_finding(
        "pdf.clipping_candidates",
        "Compared painted operation extents with active rectangular clips.",
        violations,
        incomplete=uncertain or measured == 0,
        measured_operations=measured,
        scope="Extents crossing page or rectangular clip boundaries are candidates; "
        "intentional cropping, curves, strokes and arbitrary masks need review.",
    )


def _union_area(boxes: list[Box], region: Box) -> float:
    clipped = [_clipped(b, region) for b in boxes if intersection_area(b, region) > 0]
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


def _raster_region_finding(artwork: _TracedArtwork, region: PdfRegion) -> Finding:
    boxes = artwork.boxes
    incomplete = (
        region.page > len(boxes)
        or region.page in artwork.unsupported | artwork.unknown
        or not contains(boxes[region.page - 1], region.box, 0)
    )
    images = []
    text = False
    for drawing in artwork.drawings:
        if drawing.page != region.page:
            continue
        # Text trace has origins, not reliable glyph bounds. A page with
        # text cannot establish absence within a selected region.
        if drawing.node.tag in {"fill_text", "stroke_text", "ignore_text"}:
            text = True
        if drawing.node.tag in _IMAGE_OPERATIONS:
            if drawing.box is None or drawing.clip is None or drawing.uncertain:
                incomplete = True
                continue
            clipped = _clipped(drawing.box, drawing.clip)
            a, b, c, d, _, _ = _matrix(drawing.node)
            if not ((b == c == 0) or (a == d == 0)):
                incomplete = True
            else:
                images.append(clipped)
    area = (region.right_pt - region.left_pt) * (region.bottom_pt - region.top_pt)
    coverage = _union_area(images, region.box) / area
    candidate = coverage >= (artwork.options.min_raster_region_coverage or 1) and not text
    if candidate and not incomplete:
        message = (
            f"{region.name} on page {region.page} is {coverage:.1%} covered by raster images and "
            "the page has no text operations; it may be a scan or a raster table."
        )
    else:
        message = f"Raster images cover {coverage:.1%} of {region.name} on page {region.page}."
        if text:
            message += " The page has text operations, so the region cannot be called text-free."
        elif incomplete:
            message += " Some drawing operations could not be measured."
    return heuristic_finding(
        "pdf.raster_region_candidates",
        message,
        [{"page": region.page, "region": region.name, "raster_coverage": coverage}]
        if candidate and not incomplete
        else [],
        incomplete=incomplete or text,
        page=region.page,
        region=region.name,
        raster_coverage=coverage,
        page_has_text_operations=text,
        scope="Union of axis-aligned images in an explicit region; high coverage "
        "without page text is a scan/raster-table candidate, not a semantic diagnosis.",
    )


def _geometry_findings(artwork: _TracedArtwork) -> list[Finding]:
    options = artwork.options
    result = []
    if options.drawing_regions:
        result.append(_drawing_bounds_finding(artwork))
    if options.check_clipping:
        result.append(_clipping_finding(artwork))
    if options.min_stroke_width_pt is not None:
        result.append(_stroke_finding(artwork.drawings, options, set(artwork.unknown)))
    if options.detect_printer_marks:
        result.append(
            _mark_finding(
                artwork.drawings, options, set(artwork.unsupported | artwork.unknown), artwork.boxes
            )
        )
    result.extend(_raster_region_finding(artwork, region) for region in options.raster_regions)
    return result


async def _trace_output(session: PdfToolSession, pages: int) -> str | PreparationError:
    """One MuPDF trace shared by the drawing and colour-space measurements."""
    try:
        return await session.measure(
            "trace", mupdf_trace_argv(pages), lambda execution: execution.stdout
        )
    except (PreparationError, OSError) as error:
        return PreparationError(str(error))


async def _scalable_text_fonts(
    session: PdfToolSession, drawings: list[_Drawing], options: PdfArtworkOptions
) -> _FontEvidence:
    """Whether every font is scalable, so painted text can count as vector artwork."""
    if not (options.classify_artwork or options.category_min_dpi) or not any(
        drawing.node.tag in _TEXT_OPERATIONS for drawing in drawings
    ):
        return _FontEvidence(False)
    try:
        font_types = await session.measure(
            "composition-fonts",
            ["pdffonts", DOCUMENT],
            lambda execution: [font["type"] for font in font_inventory(execution.stdout)],
        )
    except (PreparationError, OSError) as error:
        # Text-bearing traces need this evidence and retain uncertainty; the cause is
        # reported with the composition so the researcher can repair the tool or the PDF.
        lines = str(error).splitlines()
        return _FontEvidence(False, (lines[0] if lines else type(error).__name__)[:200])
    return _FontEvidence(
        bool(font_types) and all(kind in _SCALABLE_FONT_TYPES for kind in font_types)
    )


def _trace_unavailable(rules: list[str], error: Exception) -> list[Finding]:
    return [
        unavailable_finding(rule, error, severity="error" if rule == _CATEGORY_RULE else "warning")
        for rule in rules
    ]


async def _drawing_findings(
    session: PdfToolSession,
    trace: str | PreparationError,
    pages: int,
    sizes: list[tuple[float, float]],
    unsupported: set[int],
    options: PdfArtworkOptions,
    rules: list[str],
) -> list[Finding]:
    """Measure traced drawings; extends ``unsupported`` with misaligned trace pages.

    Rendered region and contrast measurements later skip those pages as well.
    """
    if isinstance(trace, PreparationError):
        return _trace_unavailable(rules, trace)
    try:
        async with session.lease("drawing trace"):
            drawings, boxes, unknown = _drawing_trace(trace, pages)
        fonts = await _scalable_text_fonts(session, drawings, options)
        async with session.lease("artwork geometry"):
            for index, (box, size) in enumerate(zip(boxes, sizes, strict=True), 1):
                if box[:2] != (0, 0) or any(
                    abs(actual - expected) > options.geometry_tolerance_pt
                    for actual, expected in zip(box[2:], size, strict=True)
                ):
                    unsupported.add(index)
            composition = _composition(drawings, unknown, fonts)
            result = []
            if options.classify_artwork:
                result.append(_composition_finding(composition))
            if options.category_min_dpi:
                result.append(_category_resolution_finding(composition, options))
            result.extend(
                _geometry_findings(
                    _TracedArtwork(
                        drawings, boxes, frozenset(unknown), frozenset(unsupported), options
                    )
                )
            )
            return result
    except (PreparationError, OSError) as error:
        return _trace_unavailable(rules, error)


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
            end = _LINE_END.search(text, index)
            index = len(text) if end is None else end.end()
        elif char in "(<":
            # Text strings, dictionaries and inline image data are deliberately
            # unsupported: do not mistake embedded operator-looking bytes for paint.
            raise PreparationError("Type 3 program uses unsupported string/dictionary content")
        else:
            match = _TOKEN.match(text, index)
            if match is None:
                raise PreparationError("Type 3 program contains unsupported token syntax")
            token = match.group()
            tokens.append(token)
            index = match.end()
            if len(tokens) > _MAX_OPERATIONS:
                raise PreparationError("Type 3 glyph program exceeds the token limit")
            if token == "BI":
                # BI is an actual inline-image operator, but the binary payload
                # cannot safely be parsed by this restricted content lexer.
                break
    return tokens


class _GlyphPaint(NamedTuple):
    """What a glyph program paints, and whether part of it could not be classified."""

    raster: bool = False
    vector: bool = False
    incomplete: bool = False

    def merged(self, other: _GlyphPaint) -> _GlyphPaint:
        return _GlyphPaint(
            self.raster or other.raster,
            self.vector or other.vector,
            self.incomplete or other.incomplete,
        )

    @property
    def classification(self) -> str:
        if self.raster and self.vector:
            return "mixed"
        return "bitmap" if self.raster else "vector" if self.vector else "empty"


_UNCLASSIFIED = _GlyphPaint(incomplete=True)


@dataclass(slots=True)
class _TokenAllowance:
    """Glyph-program tokens still permitted across one Type 3 inventory."""

    remaining: int = _MAX_OPERATIONS


def _program_tokens_within(
    objects: QpdfObjectGraph, reference: str, allowance: _TokenAllowance
) -> list[str] | None:
    """Tokens of an unfiltered inline program stream, or None when it is unreadable."""
    entry = objects.objects.get("obj:" + reference)
    if not isinstance(entry, dict) or not isinstance(entry.get("stream"), dict):
        return None
    stream = entry["stream"]
    dictionary = stream.get("dict")
    if (
        not isinstance(dictionary, dict)
        or dictionary.get("/Filter")
        or not isinstance(stream.get("data"), str)
    ):
        return None
    try:
        data = base64.b64decode(stream["data"], validate=True)
        if len(data) > _MAX_GLYPH_STREAM_BYTES:
            return None
        tokens = _program_tokens(data)
    except (binascii.Error, PreparationError):
        return None
    allowance.remaining -= len(tokens)
    return tokens if allowance.remaining >= 0 else None


def _glyph_program(
    objects: QpdfObjectGraph,
    reference: object,
    resources: object,
    allowance: _TokenAllowance,
    seen: frozenset[str] = frozenset(),
) -> _GlyphPaint:
    if (
        not is_object_reference(reference)
        or reference in seen
        or len(seen) >= 20
        or allowance.remaining <= 0
    ):
        return _UNCLASSIFIED
    tokens = _program_tokens_within(objects, reference, allowance)
    if tokens is None:
        return _UNCLASSIFIED
    paint = _GlyphPaint()
    for index, token in enumerate(tokens):
        if token in _PAINT_OPERATORS:
            paint = paint._replace(vector=True)
        elif token == "BI":
            # Positive inline-image evidence survives while later content is unknown.
            paint = paint._replace(raster=True, incomplete=True)
        elif token == "Do":
            paint = paint.merged(
                _xobject_paint(objects, tokens, index, resources, allowance, seen | {reference})
            )
        elif (
            token not in _SIMPLE_GLYPH_OPERATORS
            and not token.startswith("/")
            and not _NUMBER.fullmatch(token)
        ):
            paint = paint._replace(incomplete=True)
    return paint


def _xobject_paint(
    objects: QpdfObjectGraph,
    tokens: list[str],
    index: int,
    resources: object,
    allowance: _TokenAllowance,
    seen: frozenset[str],
) -> _GlyphPaint:
    """What the XObject named before a ``Do`` operator paints."""
    try:
        name = tokens[index - 1] if index else ""
        if not name.startswith("/"):
            raise PreparationError("Missing XObject name")
        xobjects = objects.dictionary(objects.dictionary(resources).get("/XObject"))
        child_reference = xobjects.get(name)
        child = objects.dictionary(child_reference)
        if child.get("/Subtype") == "/Image":
            return _GlyphPaint(raster=True)
        if child.get("/Subtype") == "/Form":
            return _glyph_program(
                objects, child_reference, child.get("/Resources", resources), allowance, seen
            )
        return _UNCLASSIFIED
    except PreparationError:
        return _UNCLASSIFIED


def _type3_finding(objects: QpdfObjectGraph, options: PdfArtworkOptions) -> Finding:
    fonts = [item for item in objects.reachable_dictionaries() if item.get("/Subtype") == "/Type3"]
    inventory: list[object] = []
    violations: list[object] = []
    uncertain = False
    count = 0
    allowance = _TokenAllowance()
    for index, font in enumerate(fonts, 1):
        programs = objects.dictionary(font.get("/CharProcs"))
        if not programs:
            uncertain = True
        for name, reference in sorted(programs.items()):
            count += 1
            if count > _MAX_OPERATIONS:
                raise PreparationError("Type 3 font inventory exceeds the glyph-program limit")
            paint = _glyph_program(objects, reference, font.get("/Resources", {}), allowance)
            row = {
                "font_index": index,
                "glyph": name,
                "classification": paint.classification,
                "incomplete": paint.incomplete,
            }
            if len(inventory) < MAX_SAMPLES:
                inventory.append(row)
            uncertain |= paint.incomplete
            if paint.raster and options.forbid_bitmap_type3_glyphs:
                violations.append(row)
    return measurement_finding(
        "pdf.type3_glyph_programs",
        "Inspected declared Type 3 glyph programs for image and vector painting.",
        violations,
        incomplete=uncertain,
        font_count=len(fonts),
        glyph_count=count,
        inventory=inventory,
        forbidden_bitmap=options.forbid_bitmap_type3_glyphs,
        scope="Reachable declared glyph programs, including supported nested Form XObjects. "
        "Unused glyphs may be inventoried. Unsupported operators, compressed/opaque streams "
        "and inline-image tails retain uncertainty; Type 3 alone never implies bitmap glyphs.",
    )


def _decoded_type3_finding(
    execution: CommandResult,
    *,
    objects: QpdfObjectGraph,
    references: set[str],
    options: PdfArtworkOptions,
) -> Finding:
    try:
        document = json.loads(execution.stdout)
        entries = document["qpdf"]
        if len(entries) != 2 or entries[0]["jsonversion"] != 2 or not isinstance(entries[1], dict):
            raise ValueError("Unsupported stream JSON")
        for reference in references:
            key = "obj:" + reference
            value = entries[1][key]
            if not isinstance(value, dict):
                raise ValueError("Invalid stream object")
            objects.objects[key] = value
    except (ValueError, KeyError, TypeError, RecursionError) as error:
        raise PreparationError("qpdf did not return every selected glyph/Form program") from error
    return _type3_finding(objects, options)


async def _inspect_type3(
    session: PdfToolSession, pages: int, options: PdfArtworkOptions
) -> Finding:
    objects = await session.measure(
        "glyph-objects",
        qpdf_objects_argv(),
        lambda execution: QpdfObjectGraph(execution.stdout, pages),
    )
    references: set[str] = set()
    for font in objects.reachable_dictionaries():
        if font.get("/Subtype") == "/Type3":
            for value in objects.dictionary(font.get("/CharProcs")).values():
                if is_object_reference(value):
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
            if is_object_reference(reference):
                references.add(reference)
    if len(references) > 1000:
        raise PreparationError("Type 3 program selection exceeds the bounded object limit")
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
            DOCUMENT,
        ],
        partial(_decoded_type3_finding, objects=objects, references=references, options=options),
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
        PNG_SIGNATURE
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _configured_figure_regions(
    options: PdfArtworkOptions, _sizes: list[tuple[float, float]]
) -> tuple[PdfRegion, ...]:
    return options.figure_regions


def _figure_or_whole_page_regions(
    options: PdfArtworkOptions, sizes: list[tuple[float, float]]
) -> tuple[PdfRegion, ...]:
    """An explicit input figure is measured page by page unless regions are configured."""
    return options.figure_regions or tuple(
        PdfRegion(f"Input page {index}", index, 0, 0, *size) for index, size in enumerate(sizes, 1)
    )


@dataclass(frozen=True, slots=True)
class _RenderedFigures:
    """What every rendered artwork page of one inspection shares."""

    regions: tuple[PdfRegion, ...]
    region_rules: tuple[str, ...]
    unsupported: frozenset[int]
    options: PdfArtworkOptions
    preview_directory: Path
    max_file_bytes: int


class _GrayPage(NamedTuple):
    page: int
    width: int
    height: int
    pixels: bytes
    width_pt: float
    height_pt: float


class _RegionInk(NamedTuple):
    ink_pixels: int
    edge_ink_pixels: int
    outside_content_box_ratio: float
    content_bounds_pt: Box | None


def _region_ink(
    raster: _GrayPage, pixels: tuple[int, int, int, int], options: PdfArtworkOptions
) -> _RegionInk:
    """Ink count, boundary ink and the ink bounding box within a pixel rectangle."""
    left, top, right, bottom = pixels
    width, height = raster.width, raster.height
    min_x, min_y, max_x, max_y = right, bottom, left - 1, top - 1
    band = options.figure_edge_band_pt or 0
    band_x = math.ceil(band * width / raster.width_pt)
    band_y = math.ceil(band * height / raster.height_pt)
    edge_ink = 0
    ink = 0
    for y in range(top, bottom):
        cancellation_point()
        ink_x = [
            x
            for x in range(left, right)
            if abs(raster.pixels[y * width + x] - options.background_gray) >= options.ink_difference
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
            x < left + band_x or x >= right - band_x or y < top + band_y or y >= bottom - band_y
            for x in ink_x
        )
    if not ink:
        return _RegionInk(0, edge_ink, 1, None)
    total = (right - left) * (bottom - top)
    return _RegionInk(
        ink,
        edge_ink,
        1 - ((max_x - min_x + 1) * (max_y - min_y + 1) / total),
        (
            min_x * raster.width_pt / width,
            min_y * raster.height_pt / height,
            (max_x + 1) * raster.width_pt / width,
            (max_y + 1) * raster.height_pt / height,
        ),
    )


def _region_findings(
    figures: _RenderedFigures, raster: _GrayPage, region: PdfRegion, ink: _RegionInk
) -> list[Finding]:
    options = figures.options
    details: dict[str, Any] = {
        "page": raster.page,
        "region": region.name,
        "content_bounds_pt": ink.content_bounds_pt,
        "outside_content_box_ratio": ink.outside_content_box_ratio,
        "ink_pixels": ink.ink_pixels,
        "background_gray": options.background_gray,
        "raster_dpi": options.raster_dpi,
    }
    findings = []
    where = f"{region.name} on page {raster.page}"
    if options.max_figure_whitespace_ratio is not None:
        limit = options.max_figure_whitespace_ratio
        over = ink.outside_content_box_ratio > limit
        if not ink.ink_pixels:
            outcome = f"{where} has no content against the configured background"
        else:
            outcome = (
                f"{where} leaves {ink.outside_content_box_ratio:.1%} of its area empty around "
                f"the content, {'above' if over else 'within'} the {limit:.1%} limit"
            )
        findings.append(
            heuristic_finding(
                "pdf.figure_whitespace",
                outcome + ".",
                [details] if over else [],
                **details,
                maximum_ratio=options.max_figure_whitespace_ratio,
                scope="Area outside the ink box under the configured background; "
                "internal white areas and intended padding are not judged.",
            )
        )
    if options.figure_edge_band_pt is not None:
        findings.append(
            heuristic_finding(
                "pdf.figure_edge_ink",
                (
                    f"Ink touches the outer {options.figure_edge_band_pt:g} pt border of {where} "
                    f"({ink.edge_ink_pixels} pixels); the figure may be cropped or framed."
                    if ink.edge_ink_pixels
                    else f"No ink touches the outer {options.figure_edge_band_pt:g} pt border "
                    f"of {where}."
                ),
                [{**details, "edge_ink_pixels": ink.edge_ink_pixels}]
                if ink.edge_ink_pixels
                else [],
                **details,
                edge_band_pt=options.figure_edge_band_pt,
                scope="Boundary ink is a cropping/border candidate, not proof of "
                "clipping; intended borders and touching content need review.",
            )
        )
    return findings


def _grayscale_preview_finding(figures: _RenderedFigures, raster: _GrayPage) -> Finding:
    preview = figures.preview_directory / f"page-{raster.page}.png"
    preview.parent.mkdir(exist_ok=True)
    # Copy validated bytes while this operation still owns its render lease.
    encoded = _gray_png(raster.width, raster.height, raster.pixels)
    if len(encoded) > figures.max_file_bytes:
        raise PreparationError("Encoded artwork preview exceeds the file limit")
    preview.write_bytes(encoded)
    return measurement_finding(
        _PREVIEW_RULE,
        f"Created grayscale preview for page {raster.page}.",
        [],
        page=raster.page,
        artifacts=[str(preview)],
        raster_dpi=figures.options.raster_dpi,
        scope="Review preview; semantic color independence is unverified.",
    )


def _rendered_page_findings(
    path: Path,
    *,
    figures: _RenderedFigures,
    page: int,
    size: tuple[float, float],
    expected: tuple[int, int],
) -> list[Finding]:
    width, height, pixels = read_gray_raster(path, figures.max_file_bytes)
    if any(
        abs(actual - target) > 1 for actual, target in zip((width, height), expected, strict=True)
    ):
        raise PreparationError("Artwork render dimensions disagree with page geometry")
    raster = _GrayPage(page, width, height, pixels, *size)
    findings: list[Finding] = []
    if figures.options.grayscale_preview:
        findings.append(_grayscale_preview_finding(figures, raster))
    pixel_visits = 0
    for region in figures.regions:
        if region.page != page:
            continue
        cancellation_point()
        if page in figures.unsupported or not contains((0, 0, *size), region.box, 0):
            findings.extend(
                unavailable_finding(
                    rule,
                    PreparationError(f"Figure region {region.name} has unsupported page geometry"),
                    severity="warning",
                )
                for rule in figures.region_rules
            )
            continue
        left = math.floor(region.left_pt * width / raster.width_pt)
        top = math.floor(region.top_pt * height / raster.height_pt)
        right = math.ceil(region.right_pt * width / raster.width_pt)
        bottom = math.ceil(region.bottom_pt * height / raster.height_pt)
        pixel_visits += (right - left) * (bottom - top)
        if pixel_visits > _MAX_PIXEL_VISITS:
            findings.extend(
                unavailable_finding(
                    rule,
                    PreparationError("Figure regions exceed the bounded pixel sampling work"),
                    severity="warning",
                    details={"page": page, "region": region.name},
                )
                for rule in figures.region_rules
            )
            continue
        ink = _region_ink(raster, (left, top, right, bottom), figures.options)
        findings.extend(_region_findings(figures, raster, region, ink))
    return findings


async def _rendered_page(
    session: PdfToolSession,
    figures: _RenderedFigures,
    page: int,
    size: tuple[float, float],
    rendered_rules: tuple[str, ...],
) -> list[Finding]:
    expected = measurement_raster_size(size, figures.options.raster_dpi)
    try:
        if exceeds_measurement_raster(expected):
            raise PreparationError("Artwork preview exceeds bounded raster dimensions")
        return await session.render_page(
            f"artwork-render-{page}",
            page,
            figures.options.raster_dpi,
            partial(
                _rendered_page_findings, figures=figures, page=page, size=size, expected=expected
            ),
            raster="gray",
        )
    except (PreparationError, OSError) as error:
        return [
            unavailable_finding(rule, error, severity="warning", details={"page": page})
            for rule in rendered_rules
        ]


async def _render_findings(
    session: PdfToolSession,
    sizes: list[tuple[float, float]],
    unsupported: set[int],
    options: PdfArtworkOptions,
    regions: tuple[PdfRegion, ...],
) -> list[Finding]:
    configured = configured_artwork_rules(options)
    region_rules = tuple(rule for rule in configured if rule in _REGION_RULES)
    rendered_rules = (*region_rules, *((_PREVIEW_RULE,) if options.grayscale_preview else ()))
    result: list[Finding] = []
    if not regions:
        result.extend(
            unavailable_finding(
                rule,
                PreparationError("Figure measurement needs explicit regions on a manuscript PDF"),
                severity="warning",
            )
            for rule in region_rules
        )
    for region in regions:
        if region.page > len(sizes):
            result.extend(
                unavailable_finding(
                    rule,
                    PreparationError(f"Figure region {region.name} is outside the document"),
                    severity="warning",
                )
                for rule in region_rules
            )
    page_numbers = (
        range(1, len(sizes) + 1)
        if options.grayscale_preview
        else sorted({r.page for r in regions if r.page <= len(sizes)})
    )
    try:
        require_rendered_pages(len(page_numbers), "Rendered artwork measurement")
    except PreparationError as error:
        # Inspection reads long documents; a render per page keeps the smaller cap.
        result.extend(
            unavailable_finding(rule, error, severity="warning")
            for rule in rendered_rules
            if regions or rule == _PREVIEW_RULE
        )
        return result
    figures = _RenderedFigures(
        regions,
        region_rules,
        frozenset(unsupported),
        options,
        session.work / "previews",
        session.runner.limits.max_file_bytes,
    )
    for page in page_numbers:
        result.extend(await _rendered_page(session, figures, page, sizes[page - 1], rendered_rules))
    return result


def _pixel_region(
    region: PdfRegion, size: tuple[float, float], raster: tuple[int, int]
) -> tuple[int, int, int, int]:
    if not contains((0, 0, *size), region.box, 0):
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


def _sample_unavailable(sample: PdfContrastSample, page: int, error: Exception) -> Finding:
    return unavailable_finding(
        _CONTRAST_RULE,
        error,
        severity="warning",
        details={"sample": sample.name, "page": page},
    )


def _sample_contrast_finding(
    sample: PdfContrastSample,
    page: int,
    pixels: bytes,
    width: int,
    boxes: tuple[tuple[int, int, int, int], tuple[int, int, int, int]],
) -> Finding:
    colors = [_pixel_colors(pixels, width, box) for box in boxes]
    spreads = [
        max(high - low for low, high in zip(minimum, maximum, strict=True))
        for minimum, maximum in colors
    ]
    luminances = [
        (
            relative_luminance((minimum[0] / 255, minimum[1] / 255, minimum[2] / 255)),
            relative_luminance((maximum[0] / 255, maximum[1] / 255, maximum[2] / 255)),
        )
        for minimum, maximum in colors
    ]
    minimum, maximum = _contrast_interval(luminances[0], luminances[1])
    uniform = max(spreads) <= sample.max_channel_spread
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
    required = f"{sample.minimum_ratio:g}:1"
    where = f"Sample '{sample.name}' on page {page}"
    if not uniform:
        message = (
            f"{where} has uneven colours (channel values vary by up to {max(spreads)}; the "
            f"allowed spread is {sample.max_channel_spread}), so its contrast is only known to "
            f"lie between {minimum:.2f}:1 and {maximum:.2f}:1 against the required {required}. "
            "Choose a uniform patch or allow a larger max_channel_spread."
        )
    elif maximum < sample.minimum_ratio:
        message = (
            f"{where} has a contrast ratio of at most {maximum:.2f}:1, below the required "
            f"{required}."
        )
    elif minimum < sample.minimum_ratio:
        message = (
            f"{where} has a contrast ratio between {minimum:.2f}:1 and {maximum:.2f}:1, so the "
            f"required {required} cannot be confirmed or ruled out."
        )
    else:
        message = f"{where} has a contrast ratio of at least {minimum:.2f}:1 (required {required})."
    return heuristic_finding(
        _CONTRAST_RULE,
        message,
        [details] if uniform and maximum < sample.minimum_ratio else [],
        incomplete=not uniform or minimum < sample.minimum_ratio <= maximum,
        **details,
        scope="Rendered/composited RGB patches interpreted as sRGB. "
        "Channel extrema give conservative luminance/ratio bounds. "
        "Nonuniform "
        "patches need an explicit spread tolerance; text meaning, all text "
        "contrast, perceptual quality and accessibility are not inferred.",
    )


def _rendered_contrast_findings(
    path: Path,
    *,
    page: int,
    size: tuple[float, float],
    expected: tuple[int, int],
    samples: tuple[PdfContrastSample, ...],
    max_file_bytes: int,
) -> list[Finding]:
    if not path.is_file() or path.stat().st_size > max_file_bytes:
        raise PreparationError("Contrast render exceeds the configured file limit or is missing")
    dimensions, pixels = read_rgb_raster(path, max_pixels=MAX_MEASUREMENT_PIXELS)
    if any(abs(actual - target) > 1 for actual, target in zip(dimensions, expected, strict=True)):
        raise PreparationError("Contrast render dimensions disagree with page geometry")
    findings: list[Finding] = []
    pixel_visits = 0
    for sample in samples:
        cancellation_point()
        try:
            boxes = (
                _pixel_region(sample.foreground, size, dimensions),
                _pixel_region(sample.background, size, dimensions),
            )
            pixel_visits += sum((box[2] - box[0]) * (box[3] - box[1]) for box in boxes)
            if pixel_visits > _MAX_PIXEL_VISITS:
                raise PreparationError("Contrast patches exceed the bounded pixel sampling work")
            findings.append(_sample_contrast_finding(sample, page, pixels, dimensions[0], boxes))
        except PreparationError as error:
            findings.append(_sample_unavailable(sample, page, error))
    return findings


async def _contrast_page(
    session: PdfToolSession,
    page: int,
    size: tuple[float, float],
    samples: tuple[PdfContrastSample, ...],
    options: PdfArtworkOptions,
) -> list[Finding]:
    expected = measurement_raster_size(size, options.raster_dpi)
    try:
        if exceeds_measurement_raster(expected):
            raise PreparationError("Contrast render exceeds bounded raster dimensions")
        return await session.render_page(
            f"contrast-render-{page}",
            page,
            options.raster_dpi,
            partial(
                _rendered_contrast_findings,
                page=page,
                size=size,
                expected=expected,
                samples=samples,
                max_file_bytes=session.runner.limits.max_file_bytes,
            ),
            raster="rgb",
        )
    except (PreparationError, OSError) as error:
        return [_sample_unavailable(sample, page, error) for sample in samples]


async def _sample_contrast_findings(
    session: PdfToolSession,
    sizes: list[tuple[float, float]],
    unsupported: set[int],
    options: PdfArtworkOptions,
) -> list[Finding]:
    def measurable(page: int) -> bool:
        return page <= len(sizes) and page not in unsupported

    result = [
        _sample_unavailable(
            sample,
            sample.foreground.page,
            PreparationError("Contrast sample page is absent or has unsupported geometry"),
        )
        for sample in options.contrast_samples
        if not measurable(sample.foreground.page)
    ]
    pages = sorted(
        {
            sample.foreground.page
            for sample in options.contrast_samples
            if measurable(sample.foreground.page)
        }
    )
    for page in pages:
        samples = tuple(
            sample for sample in options.contrast_samples if sample.foreground.page == page
        )
        result.extend(await _contrast_page(session, page, sizes[page - 1], samples, options))
    return result


def _color_family(name: str) -> str | None:
    """Map a MuPDF colour-space name to gray/rgb/cmyk; other spaces stay unknown."""
    folded = name.casefold()
    if folded.startswith(("separation", "devicen", "indexed", "lab")):
        return None
    if "cmyk" in folded:
        return "cmyk"
    if "rgb" in folded or "bgr" in folded:
        return "rgb"
    if "gray" in folded or "grey" in folded:
        return "gray"
    return None


def _color_space_finding(output: str, pages: int, required: str) -> Finding:
    """Compare every traced paint operation's colour family with the required one."""
    trace = parse_mupdf_trace(output, pages)
    violations: list[object] = []
    spaces: dict[str, int] = {}
    unknown: set[str] = set()
    for number, page in trace.pages:
        cancellation_point()
        for node in page.iter():
            if node.tag not in COLOR_OPERATIONS:
                continue
            space = node.get("colorspace")
            if space is None:
                unknown.add(f"{node.tag} without a colour-space name")
                continue
            spaces[space] = spaces.get(space, 0) + 1
            family = _color_family(space)
            if family is None:
                unknown.add(space)
            elif family not in {"gray", required}:
                violations.append({"page": number, "operation": node.tag, "color_space": space})
    return measurement_finding(
        "figure.color_space",
        f"Compared traced colour spaces with the required {required.upper()} family.",
        violations,
        incomplete=bool(unknown) or not spaces,
        required=required,
        color_spaces=dict(sorted(spaces.items())),
        unclassified=sorted(unknown)[:MAX_SAMPLES],
        scope="MuPDF trace colour-space names of painted operations; grayscale satisfies "
        "either family. Separation, DeviceN, Indexed, Lab and unnamed spaces are unclassified; "
        "ICC profile contents are not interpreted.",
    )


async def _color_space(
    session: PdfToolSession, trace: str | PreparationError, pages: int, required: str
) -> Finding:
    if isinstance(trace, PreparationError):
        return unavailable_finding("figure.color_space", trace)
    try:
        async with session.lease("color trace"):
            return _color_space_finding(trace, pages, required)
    except (PreparationError, OSError) as error:
        return unavailable_finding("figure.color_space", error)


async def _inspect_artwork(
    pdf: Path,
    work: Path,
    runner: CommandRunner,
    options: PdfArtworkOptions,
    budget: ResourceBudget | None,
    figure_regions: Callable[[PdfArtworkOptions, list[tuple[float, float]]], tuple[PdfRegion, ...]],
) -> list[Finding]:
    configured = configured_artwork_rules(options)
    if not configured:
        return []
    try:
        session = await open_measurement_session(pdf, work, runner, budget)
        fields = await session.measure(
            "properties",
            ["pdfinfo", "-enc", "UTF-8", DOCUMENT],
            lambda result: pdfinfo_properties(result.stdout),
        )
        pages = page_count(fields)
        unsupported, sizes = await session.measure(
            "geometry", page_boxes_argv(pages), lambda result: page_frames(result.stdout, pages)
        )
    except (PreparationError, OSError) as error:
        return [unavailable_finding(rule, error) for rule in configured]
    result: list[Finding] = []
    trace_rules = [rule for rule in configured if rule in DRAWING_TRACE_RULES]
    trace: str | PreparationError = ""  # read only when a trace measurement is configured
    if trace_rules or options.required_color_space is not None:
        trace = await _trace_output(session, pages)
    if trace_rules:
        result.extend(
            await _drawing_findings(session, trace, pages, sizes, unsupported, options, trace_rules)
        )
    if options.classify_type3_glyphs or options.forbid_bitmap_type3_glyphs:
        try:
            result.append(await _inspect_type3(session, pages, options))
        except (PreparationError, OSError) as error:
            result.append(unavailable_finding("pdf.type3_glyph_programs", error))
    if (
        options.max_figure_whitespace_ratio is not None
        or options.figure_edge_band_pt is not None
        or options.grayscale_preview
    ):
        result.extend(
            await _render_findings(
                session, sizes, unsupported, options, figure_regions(options, sizes)
            )
        )
    if options.contrast_samples:
        result.extend(await _sample_contrast_findings(session, sizes, unsupported, options))
    if options.required_color_space is not None:
        result.append(await _color_space(session, trace, pages, options.required_color_space))
    return result


async def inspect_pdf_artwork(
    pdf: Path,
    work: Path,
    runner: CommandRunner,
    *,
    options: PdfArtworkOptions | None = None,
    budget: ResourceBudget | None = None,
) -> list[Finding]:
    """Inspect one manuscript PDF; figure measurements need explicit regions."""
    return await _inspect_artwork(
        pdf, work, runner, options or PdfArtworkOptions(), budget, _configured_figure_regions
    )


async def inspect_artwork_inputs(
    figures: tuple[Path, ...],
    work: Path,
    runner: CommandRunner,
    *,
    options: PdfArtworkOptions | None = None,
    budget: ResourceBudget | None = None,
) -> list[Finding]:
    """Apply the selected artwork policy separately to explicit input PDF figures."""
    if (
        not isinstance(figures, tuple)
        or len(figures) > MAX_FIGURE_INPUTS
        or any(not isinstance(figure, Path) for figure in figures)
    ):
        raise PreparationError(
            f"Artwork input inspection requires at most {MAX_FIGURE_INPUTS} immutable PDF paths"
        )
    if work.exists():
        raise PreparationError("Artwork inputs require a new inspection workspace")
    result = []
    for index, figure in enumerate(dict.fromkeys(figures), 1):
        findings = await _inspect_artwork(
            figure,
            work / str(index),
            runner,
            options or PdfArtworkOptions(),
            budget,
            _figure_or_whole_page_regions,
        )
        result.extend(replace(finding, path=str(figure)) for finding in findings)
    return result
