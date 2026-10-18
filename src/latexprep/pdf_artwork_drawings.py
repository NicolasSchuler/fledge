"""Artwork findings measured from the MuPDF trace of painted drawings.

Covers the composition inventory (raster, vector, text), the DPI of image placements
against an explicit category, stroke thickness after placement, printer-mark
candidates, drawing bounds, clipping candidates, raster-dominated regions and the
colour-space family of painted operations. Every result is an advisory candidate with
a stated scope; evidence that cannot be measured makes a finding inconclusive and
never a pass.

Does not own: parsing the trace (``pdf_artwork_trace``), rendered-pixel measurements
(``pdf_artwork_rendered``), Type 3 glyph programs (``pdf_type3_glyphs``) or deciding
which measurements run (``pdf_artwork``).
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from functools import partial
from typing import Final, NamedTuple

from .cancellation import cancellation_point, run_in_thread
from .errors import PreparationError
from .finding_text import plural
from .findings import Finding
from .measurement_findings import (
    MAX_SAMPLES,
    heuristic_finding,
    measurement_finding,
    unavailable_finding,
)
from .pdf_artwork_options import ArtworkRule, ColorSpaceFamily, PdfArtworkOptions
from .pdf_artwork_trace import (
    EXTENT_OPERATIONS,
    IMAGE_OPERATIONS,
    TEXT_OPERATIONS,
    Box,
    Drawing,
    DrawingTrace,
    Matrix,
    TracedArtwork,
    path_segments,
)
from .pdf_check_options import PdfRegion
from .pdf_geometry import POINTS_PER_INCH, contains, finite_number, intersection_area
from .pdf_printer_marks import printer_mark_finding
from .pdf_tool_output import COLOR_OPERATIONS, TracedDocument, font_inventory
from .pdf_tools import PdfToolSession, font_inventory_argv

# A report row describing one violation or measurement; its keys are part of the report.
Violation = dict[str, object]

SCALABLE_FONT_TYPES: Final = frozenset(
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
# Image boxes that may overlap one region before the exact union area is refused.
_MAX_IMAGE_BOXES_PER_REGION: Final = 1000
# A placement whose transform has a smaller determinant (points squared per pixel squared)
# is singular: it collapses the image to a line and has no sampling resolution.
_MIN_PLACEMENT_DETERMINANT: Final = 1e-12
# A stroke under a non-straight path is measurable only when its transform is a uniform
# scale with perpendicular axes; these are the relative and absolute slack for that test.
_UNIFORM_SCALE_RELATIVE_TOLERANCE: Final = 1e-6
_PERPENDICULAR_ABSOLUTE_TOLERANCE: Final = 1e-8
# Operation names as a researcher would say them.
_KIND_LABELS: Final = {
    "fill_image": "an image",
    "fill_image_mask": "an image mask",
    "fill_path": "a filled shape",
    "stroke_path": "a stroked line",
    "stroke_text": "stroked text",
}
_GRAY_FAMILY: Final = "gray"
# MuPDF colour-space names that carry no device family; they stay unclassified.
_UNCLASSIFIED_SPACE_PREFIXES: Final = ("separation", "devicen", "indexed", "lab")


def _tuple(box: Box | None) -> tuple[float, float, float, float] | None:
    """A plain tuple for a report; the report copier accepts no tuple subclasses."""
    return None if box is None else (box.left, box.top, box.right, box.bottom)


def drawing_report(drawing: Drawing, **extra: object) -> Violation:
    """The report row for a drawing, with any measurement-specific fields."""
    return {
        "page": drawing.page,
        "operation": drawing.index,
        "kind": drawing.kind,
        "bounds_pt": _tuple(drawing.box),
        **extra,
    }


def _kind_label(row: Mapping[str, object]) -> str:
    return _KIND_LABELS.get(str(row.get("kind")), "a drawing")


def _uncertain_suffix(uncertain: bool) -> str:
    return " Some drawings could not be measured." if uncertain else ""


class FontEvidence(NamedTuple):
    """Whether every font is scalable, and why that could not be established if it failed."""

    scalable: bool
    failure: str | None = None


class Composition(NamedTuple):
    """Painted operations by kind for the whole selected PDF."""

    raster: list[Drawing]
    vector: list[Drawing]
    text: list[Drawing]
    unsupported: bool
    fonts: FontEvidence

    @classmethod
    def of(cls, drawings: Sequence[Drawing], uncertain: bool, fonts: FontEvidence) -> Composition:
        raster = [d for d in drawings if d.kind in IMAGE_OPERATIONS]
        text = [d for d in drawings if d.kind in TEXT_OPERATIONS]
        vector = [d for d in drawings if d.kind in {"fill_path", "stroke_path"}]
        if fonts.scalable:
            vector.extend(text)
        unsupported = (
            uncertain
            or any(d.kind == "fill_shade" for d in drawings)
            or bool(text and not fonts.scalable)
        )
        return cls(raster, vector, text, unsupported, fonts)

    @property
    def classification(self) -> str:
        if self.raster and self.vector:
            return "mixed"
        return "raster" if self.raster else "vector" if self.vector else "empty"


def _composition_finding(composition: Composition) -> Finding:
    failure = composition.fonts.failure
    message = "Inventoried painted raster and vector operations."
    if failure is not None:
        message = (
            "Inventoried painted raster and vector operations, but pdffonts could not list "
            f"the fonts ({failure}), so text could not be counted as vector "
            "artwork. Resolve that error and run the check again."
        )
    return heuristic_finding(
        ArtworkRule.ARTWORK_COMPOSITION,
        message,
        [],
        incomplete=composition.unsupported or composition.classification == "empty",
        details={
            "font_inventory_error": failure,
            "classification": composition.classification,
            "raster_operations": len(composition.raster),
            "vector_operations": len(composition.vector),
            "unclassified_text_operations": 0
            if composition.fonts.scalable
            else len(composition.text),
            "scope": "Whole selected PDF, including text and backgrounds; figure categories, "
            "font text needs a complete scalable-font inventory. Outlined lettering "
            "and source-to-final attribution are not inferred.",
        },
    )


class PlacementSampling(NamedTuple):
    """Image pixels per inch along both image axes and in the coarsest direction."""

    x_dpi: float
    y_dpi: float
    minimum_direction_dpi: float


def placement_sampling(drawing: Drawing) -> PlacementSampling | None:
    """Axis and coarsest-direction sampling of one image placement, if measurable."""
    node = drawing.node
    matrix = Matrix.of_node(node)
    a, b, c, d = matrix.a, matrix.b, matrix.c, matrix.d
    width, height = finite_number(node.get("width")), finite_number(node.get("height"))
    lengths = math.hypot(a, b), math.hypot(c, d)
    determinant = matrix.determinant
    if (
        min(width, height, *lengths) <= 0
        or not math.isfinite(determinant)
        or abs(determinant) < _MIN_PLACEMENT_DETERMINANT
    ):
        return None
    axes = POINTS_PER_INCH * width / lengths[0], POINTS_PER_INCH * height / lengths[1]
    # Largest singular value of the points-per-pixel transform bounds
    # the coarsest sampling direction under shear as well as rotation.
    aa, bb, cc, dd = a / width, b / width, c / height, d / height
    norm = aa * aa + bb * bb + cc * cc + dd * dd
    if not math.isfinite(norm * norm) or norm <= 0:
        return None
    discriminant = max(0.0, norm * norm - 4 * (aa * dd - bb * cc) ** 2)
    coarse = POINTS_PER_INCH / math.sqrt((norm + math.sqrt(discriminant)) / 2)
    return PlacementSampling(axes[0], axes[1], coarse)


def _category_resolution_finding(composition: Composition, options: PdfArtworkOptions) -> Finding:
    category = options.artwork_category or ""
    minimum = dict(options.category_min_dpi)[category]
    measurements: list[object] = []
    violations: list[object] = []
    lowest: tuple[float, int] | None = None
    incomplete = composition.unsupported
    for drawing in composition.raster:
        sampling = placement_sampling(drawing)
        if sampling is None:
            incomplete = True
            continue
        effective_dpi = min(sampling)
        measurement = drawing_report(
            drawing,
            x_dpi=sampling.x_dpi,
            y_dpi=sampling.y_dpi,
            minimum_direction_dpi=sampling.minimum_direction_dpi,
            effective_dpi=effective_dpi,
        )
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
        ArtworkRule.CATEGORY_RESOLUTION,
        message,
        violations,
        incomplete=incomplete or (not composition.raster and not composition.vector),
        details={
            "category": options.artwork_category,
            "minimum_dpi": minimum,
            "measurements": measurements[:MAX_SAMPLES],
            "placement_count": len(composition.raster),
            "font_inventory_error": composition.fonts.failure,
            "scope": "Declared category and traced raster placements; axes and coarsest "
            "sampling includes rotation/shear. Vector operations have no raster DPI.",
        },
    )


def effective_stroke_widths(drawing: Drawing) -> list[float]:
    """Stroke thickness normal to each straight segment after placement; empty if unmeasurable.

    A path with curves or text is measured only under a uniform, perpendicular
    transform, where one scale factor applies to every direction.
    """
    node = drawing.node
    width = finite_number(node.get("linewidth"))
    matrix = Matrix.of_node(node)
    determinant = abs(matrix.determinant)
    if width <= 0 or determinant == 0 or not math.isfinite(determinant):
        return []
    path = path_segments(node)
    if not path.straight:
        scale_x, scale_y = math.hypot(matrix.a, matrix.b), math.hypot(matrix.c, matrix.d)
        uniform = math.isclose(scale_x, scale_y, rel_tol=_UNIFORM_SCALE_RELATIVE_TOLERANCE)
        perpendicular = math.isclose(
            matrix.a * matrix.c + matrix.b * matrix.d,
            0,
            abs_tol=_PERPENDICULAR_ABSOLUTE_TOLERANCE,
        )
        return [width * scale_x] if uniform and perpendicular else []
    widths = []
    for first, second in path.segments:
        x, y = second.x - first.x, second.y - first.y
        length = math.hypot(x, y)
        placed = math.hypot(matrix.a * x + matrix.c * y, matrix.b * x + matrix.d * y)
        if length and placed:
            widths.append(width * determinant * length / placed)
    return widths


def _stroke_finding(artwork: TracedArtwork, options: PdfArtworkOptions) -> Finding:
    minimum = options.min_stroke_width_pt
    violations: list[object] = []
    thinnest: tuple[float, int] | None = None
    measured = 0
    uncertain = bool(artwork.trace_uncertain_pages)
    for drawing in artwork.drawings:
        if drawing.kind not in {"stroke_path", "stroke_text"}:
            continue
        cancellation_point()
        widths = effective_stroke_widths(drawing)
        if not widths:
            uncertain = True
            continue
        measured += len(widths)
        if minimum is not None and min(widths) < minimum:
            violations.append(drawing_report(drawing, minimum_effective_width_pt=min(widths)))
            if thinnest is None or min(widths) < thinnest[0]:
                thinnest = (min(widths), drawing.page)
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
        ArtworkRule.TRANSFORMED_STROKE_WIDTH,
        message,
        violations,
        incomplete=uncertain or measured == 0,
        details={
            "measured_segments": measured,
            "minimum_pt": minimum,
            "scope": "Straight-segment normal thickness supports arbitrary nonsingular affine "
            "transforms; curves/text need uniform transforms. Hairlines, joins, caps and "
            "raster lines are omitted.",
        },
    )


def _drawing_bounds_finding(artwork: TracedArtwork, options: PdfArtworkOptions) -> Finding:
    allowed_by_page: dict[int, list[PdfRegion]] = {}
    for region in options.drawing_regions:
        allowed_by_page.setdefault(region.page, []).append(region)
    violations: list[object] = []
    measured = 0
    uncertain = bool(artwork.trace_uncertain_pages) or any(
        region.page > len(artwork.page_boxes) or region.page in artwork.misaligned_frame_pages
        for region in options.drawing_regions
    )
    for drawing in artwork.drawings:
        allowed = allowed_by_page.get(drawing.page)
        if not allowed or drawing.kind not in EXTENT_OPERATIONS:
            continue
        cancellation_point()
        if (
            drawing.box is None
            or drawing.page in artwork.misaligned_frame_pages
            or drawing.uncertain
            or drawing.clip is None
        ):
            uncertain = True
            continue
        measured += 1
        visible = drawing.box.clipped(drawing.clip)
        if visible.is_empty:
            continue
        if not any(contains(r.box, visible, options.geometry_tolerance_pt) for r in allowed):
            violations.append(drawing_report(drawing, visible_bounds_pt=_tuple(visible)))
    return heuristic_finding(
        ArtworkRule.DRAWING_BOUNDS,
        _bounds_message(violations, measured, uncertain),
        violations,
        incomplete=uncertain or measured == 0,
        details={
            "measured_operations": measured,
            "scope": "Affine image rectangles and polygon bounds after rectangular clipping. "
            "Text, strokes, curves, masks and unsupported page frames remain unmeasured.",
        },
    )


def _bounds_message(violations: Sequence[object], measured: int, uncertain: bool) -> str:
    rows = [row for row in violations if isinstance(row, dict)]
    if rows:
        first = rows[0]
        return (
            f"Drawings outside the allowed drawing regions: {len(rows)}; the first is "
            f"{_kind_label(first)} on page {first['page']}."
        )
    if not measured:
        return "No image or filled shape could be compared with the allowed drawing regions."
    return (
        f"All measured images and filled shapes ({measured}) lie inside the allowed drawing "
        f"regions.{_uncertain_suffix(uncertain)}"
    )


def _clipping_finding(artwork: TracedArtwork, options: PdfArtworkOptions) -> Finding:
    tolerance = options.geometry_tolerance_pt
    violations: list[object] = []
    measured = 0
    uncertain = bool(artwork.trace_uncertain_pages)
    for drawing in artwork.drawings:
        if drawing.kind not in EXTENT_OPERATIONS:
            continue
        cancellation_point()
        if drawing.box is None or drawing.clip is None or drawing.uncertain:
            uncertain = True
            continue
        measured += 1
        if not drawing.clip.contains(drawing.box, tolerance):
            violations.append(drawing_report(drawing, clip_bounds_pt=_tuple(drawing.clip)))
    return heuristic_finding(
        ArtworkRule.CLIPPING_CANDIDATES,
        _clipping_message(violations, measured, uncertain),
        violations,
        incomplete=uncertain or measured == 0,
        details={
            "measured_operations": measured,
            "scope": "Extents crossing page or rectangular clip boundaries are candidates; "
            "intentional cropping, curves, strokes and arbitrary masks need review.",
        },
    )


def _clipping_message(violations: Sequence[object], measured: int, uncertain: bool) -> str:
    rows = [row for row in violations if isinstance(row, dict)]
    if rows:
        first = rows[0]
        return (
            f"Images and shapes extending past a clip rectangle or the page: {len(rows)}; the "
            f"first is {_kind_label(first)} on page {first['page']}. Check that the cropping "
            "is intended."
        )
    if not measured:
        return "No image or filled shape could be compared with its clip rectangle."
    return (
        f"None of the {measured} measured images and filled shapes crosses a clip "
        f"boundary.{_uncertain_suffix(uncertain)}"
    )


def union_area(boxes: Sequence[Box], region: Box) -> float:
    """Area of ``region`` covered by the union of ``boxes``, by a sweep over their x edges."""
    clipped = [box.clipped(region) for box in boxes if intersection_area(box, region) > 0]
    if len(clipped) > _MAX_IMAGE_BOXES_PER_REGION:
        raise PreparationError("Raster region exceeds the bounded image-union limit")
    xs = sorted({x for box in clipped for x in (box.left, box.right)})
    area = 0.0
    for left, right in zip(xs, xs[1:], strict=False):
        cancellation_point()
        intervals = sorted((b.top, b.bottom) for b in clipped if b.left < right and b.right > left)
        length, end = 0.0, -math.inf
        for top, bottom in intervals:
            length += max(0, bottom - max(top, end))
            end = max(end, bottom)
        area += (right - left) * length
    return area


def _raster_region_finding(
    artwork: TracedArtwork,
    options: PdfArtworkOptions,
    region: PdfRegion,
    page_drawings: Sequence[Drawing],
) -> Finding:
    incomplete = (
        region.page > len(artwork.page_boxes)
        or region.page in artwork.unusable_pages
        or not artwork.page_boxes[region.page - 1].contains(region.box)
    )
    images: list[Box] = []
    text = False
    for drawing in page_drawings:
        # Text trace has origins, not reliable glyph bounds. A page with
        # text cannot establish absence within a selected region.
        if drawing.kind in {"fill_text", "stroke_text", "ignore_text"}:
            text = True
        if drawing.kind not in IMAGE_OPERATIONS:
            continue
        if drawing.box is None or drawing.clip is None or drawing.uncertain:
            incomplete = True
            continue
        matrix = Matrix.of_node(drawing.node)
        if (matrix.b == matrix.c == 0) or (matrix.a == matrix.d == 0):
            images.append(drawing.box.clipped(drawing.clip))
        else:
            incomplete = True
    area = (region.right_pt - region.left_pt) * (region.bottom_pt - region.top_pt)
    coverage = union_area(images, Box(*region.box)) / area
    candidate = coverage >= (options.min_raster_region_coverage or 1) and not text
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
        ArtworkRule.RASTER_REGION_CANDIDATES,
        message,
        [{"page": region.page, "region": region.name, "raster_coverage": coverage}]
        if candidate and not incomplete
        else [],
        incomplete=incomplete or text,
        details={
            "page": region.page,
            "region": region.name,
            "raster_coverage": coverage,
            "page_has_text_operations": text,
            "scope": "Union of axis-aligned images in an explicit region; high coverage "
            "without page text is a scan/raster-table candidate, not a semantic diagnosis.",
        },
    )


def _geometry_findings(artwork: TracedArtwork, options: PdfArtworkOptions) -> list[Finding]:
    result = []
    if options.drawing_regions:
        result.append(_drawing_bounds_finding(artwork, options))
    if options.check_clipping:
        result.append(_clipping_finding(artwork, options))
    if options.min_stroke_width_pt is not None:
        result.append(_stroke_finding(artwork, options))
    if options.detect_printer_marks:
        result.append(printer_mark_finding(artwork, options))
    by_page = artwork.drawings_by_page()
    for region in options.raster_regions:
        cancellation_point()
        result.append(
            _raster_region_finding(artwork, options, region, by_page.get(region.page, ()))
        )
    return result


def _measure_drawings(
    trace: DrawingTrace,
    misaligned_frame_pages: frozenset[int],
    fonts: FontEvidence,
    options: PdfArtworkOptions,
) -> list[Finding]:
    """Every configured drawing finding, in report order (pure; runs in a worker thread)."""
    composition = Composition.of(trace.drawings, bool(trace.trace_uncertain_pages), fonts)
    result = []
    if options.classify_artwork:
        result.append(_composition_finding(composition))
    if options.category_min_dpi:
        result.append(_category_resolution_finding(composition, options))
    artwork = TracedArtwork(
        trace.drawings, trace.page_boxes, trace.trace_uncertain_pages, misaligned_frame_pages
    )
    result.extend(_geometry_findings(artwork, options))
    return result


async def scalable_text_fonts(
    session: PdfToolSession, drawings: Sequence[Drawing], options: PdfArtworkOptions
) -> FontEvidence:
    """Whether every font is scalable, so painted text can count as vector artwork."""
    if not (options.classify_artwork or options.category_min_dpi) or not any(
        drawing.kind in TEXT_OPERATIONS for drawing in drawings
    ):
        return FontEvidence(False)
    try:
        font_types = await session.measure(
            "composition-fonts",
            font_inventory_argv(),
            lambda execution: [font.font_type for font in font_inventory(execution.stdout)],
        )
    except (PreparationError, OSError) as error:
        # Text-bearing traces need this evidence and retain uncertainty; the cause is
        # reported with the composition so the researcher can repair the tool or the PDF.
        lines = str(error).splitlines()
        return FontEvidence(False, (lines[0] if lines else type(error).__name__)[:200])
    return FontEvidence(
        bool(font_types) and all(kind in SCALABLE_FONT_TYPES for kind in font_types)
    )


def trace_unavailable_findings(rules: Sequence[ArtworkRule], error: Exception) -> list[Finding]:
    """Each rule that needed the MuPDF trace, as evidence that could not be measured."""
    return [
        unavailable_finding(
            rule,
            error,
            severity="error" if rule is ArtworkRule.CATEGORY_RESOLUTION else "warning",
        )
        for rule in rules
    ]


async def inspect_drawings(
    session: PdfToolSession,
    trace: DrawingTrace,
    misaligned_frame_pages: frozenset[int],
    options: PdfArtworkOptions,
    rules: Sequence[ArtworkRule],
) -> list[Finding]:
    """Measure the traced drawings for every configured trace rule in ``rules``.

    Any measurement failure makes all of the rules unavailable evidence, because the
    drawings are measured together.
    """
    try:
        fonts = await scalable_text_fonts(session, trace.drawings, options)
        async with session.lease("artwork geometry"):
            return await run_in_thread(
                partial(_measure_drawings, trace, misaligned_frame_pages, fonts, options)
            )
    except (PreparationError, OSError) as error:
        return trace_unavailable_findings(rules, error)


def color_family(name: str) -> str | None:
    """Map a MuPDF colour-space name to gray/rgb/cmyk; other spaces stay unknown."""
    folded = name.casefold()
    if folded.startswith(_UNCLASSIFIED_SPACE_PREFIXES):
        return None
    if "cmyk" in folded:
        return "cmyk"
    if "rgb" in folded or "bgr" in folded:
        return "rgb"
    if "gray" in folded or "grey" in folded:
        return _GRAY_FAMILY
    return None


def color_space_finding(document: TracedDocument, required: ColorSpaceFamily) -> Finding:
    """Compare every traced paint operation's colour family with the required one."""
    violations: list[object] = []
    spaces: dict[str, int] = {}
    unknown: set[str] = set()
    for number, page in document.pages:
        cancellation_point()
        for node in page.iter():
            if node.tag not in COLOR_OPERATIONS:
                continue
            space = node.get("colorspace")
            if space is None:
                unknown.add(f"{node.tag} without a colour-space name")
                continue
            spaces[space] = spaces.get(space, 0) + 1
            family = color_family(space)
            if family is None:
                unknown.add(space)
            elif family not in {_GRAY_FAMILY, required}:
                violations.append({"page": number, "operation": node.tag, "color_space": space})
    return measurement_finding(
        ArtworkRule.COLOR_SPACE,
        _color_space_message(violations, spaces, unknown, required),
        violations,
        incomplete=bool(unknown) or not spaces,
        details={
            "required": required,
            "color_spaces": dict(sorted(spaces.items())),
            "unclassified": sorted(unknown)[:MAX_SAMPLES],
            "scope": "MuPDF trace colour-space names of painted operations; grayscale satisfies "
            "either family. Separation, DeviceN, Indexed, Lab and unnamed spaces are unclassified; "
            "ICC profile contents are not interpreted.",
        },
    )


def _color_space_message(
    violations: Sequence[object],
    spaces: Mapping[str, int],
    unknown: set[str],
    required: ColorSpaceFamily,
) -> str:
    family = required.upper()
    rows = [row for row in violations if isinstance(row, dict)]
    if rows:
        first = rows[0]
        return (
            f"Painted operations outside the required {family} family: {len(rows)}; for example "
            f"{first['color_space']} on page {first['page']}."
        )
    if not spaces:
        return "No painted operation with a named colour space was found in the traced PDF."
    message = f"All traced colour spaces are {family} or grayscale."
    if unknown:
        message = (
            f"No traced colour space is outside the required {family} family, but "
            f"{plural(len(unknown), 'colour space')} could not be classified."
        )
    return message
