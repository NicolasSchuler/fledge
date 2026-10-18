"""Stroke-width, color-space, text-contrast and grayscale-separation measurements.

MuPDF's trace lists the drawing operations of each page with their color spaces,
colors and transforms. These checks read only what the operations declare: opaque
DeviceRGB and DeviceGray colors under an explicit sRGB assumption, and uniformly scaled
strokes. They do not see raster pixels, outlined text, ICC conversion, compositing or the
real page background, and never establish accessibility. This module owns the four paint
measurements; the trace parser is ``pdf_tool_output`` and running mutool is ``pdf_tools``.
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from collections.abc import Collection, Iterator, Sequence
from dataclasses import dataclass, field
from itertools import combinations
from typing import NamedTuple

from .errors import PreparationError
from .findings import Finding
from .measurement_findings import MAX_SAMPLES, heuristic_finding, measurement_finding
from .pdf_check_options import PdfCheckOptions
from .pdf_geometry import (
    MAX_PAIR_CHECKS,
    finite_number,
    relative_luminance,
    wcag_contrast_ratio,
)
from .pdf_rule_names import PdfRule
from .pdf_tool_output import COLOR_OPERATIONS, TracedDocument, parse_mupdf_trace
from .pdf_tools import PdfToolSession, ToolMeasurement, mupdf_trace_argv

Rgb = tuple[float, float, float]

# Distinct colors kept for one page; a page with more is only partly compared.
MAX_PAGE_PALETTE_COLORS = 256
_STROKE_OPERATIONS = frozenset({"stroke_path", "stroke_text"})
_TEXT_OPERATIONS = frozenset({"fill_text", "stroke_text"})
# Operations that blend with what is below them, so a nominal color is not the visible one.
_COMPOSITING_TAGS = frozenset({"group", "mask", "tile"})
# Tolerances for deciding that a stroke transform scales both axes equally and at right angles.
_SCALE_RELATIVE_TOLERANCE = 1e-6
_SCALE_ABSOLUTE_TOLERANCE = 1e-8
_ORTHOGONALITY_TOLERANCE = 1e-6


def _simple_rgb(node: ET.Element) -> Rgb | None:
    """Interpret only opaque DeviceRGB/Gray values under an explicit sRGB assumption."""
    if finite_number(node.get("alpha", "1")) != 1:
        return None
    values = tuple(finite_number(value) for value in node.get("color", "").split())
    if node.get("colorspace") == "DeviceGray" and len(values) == 1:
        values = values * 3
    elif node.get("colorspace") != "DeviceRGB":
        return None
    if len(values) != 3 or any(value < 0 or value > 1 for value in values):
        return None
    return values[0], values[1], values[2]


class _Stroke(NamedTuple):
    """One stroke operation's width as written, and as placed when that is measurable."""

    # None for hairlines (their width depends on the output device) and for transforms
    # that scale the axes unequally (their width depends on the path direction).
    effective_width: float | None
    source_width: float
    transform: list[float]


def _stroke_measurement(node: ET.Element) -> _Stroke:
    width = finite_number(node.get("linewidth"))
    transform = [finite_number(value) for value in node.get("transform", "").split()]
    if len(transform) != 6 or width < 0:
        raise PreparationError("MuPDF stroke measurement lacks a valid transform/width")
    a, b, c, d, _, _ = transform
    scale_x, scale_y = math.hypot(a, b), math.hypot(c, d)
    uniform = math.isclose(
        scale_x, scale_y, rel_tol=_SCALE_RELATIVE_TOLERANCE, abs_tol=_SCALE_ABSOLUTE_TOLERANCE
    ) and math.isclose(
        a * c + b * d, 0, abs_tol=max(1, scale_x * scale_y) * _ORTHOGONALITY_TOLERANCE
    )
    if not uniform or width == 0 or scale_x == 0:
        return _Stroke(None, width, transform)
    return _Stroke(width * scale_x, width, transform)


def _stroke_width_finding(trace: TracedDocument, minimum: float) -> Finding:
    strokes: list[object] = []
    uncertain = False
    count = 0
    for number, page in trace.pages:
        for node in page.iter():
            if node.tag not in _STROKE_OPERATIONS:
                continue
            count += 1
            stroke = _stroke_measurement(node)
            if stroke.effective_width is None:
                uncertain = True
            elif stroke.effective_width < minimum:
                strokes.append(
                    {
                        "page": number,
                        "effective_width_pt": stroke.effective_width,
                        "source_width": stroke.source_width,
                        "transform": stroke.transform,
                    }
                )
    return heuristic_finding(
        PdfRule.STROKE_WIDTH,
        "Compared uniformly transformed stroke widths with the supplied minimum.",
        strokes,
        incomplete=uncertain or count == 0,
        details={
            "stroke_count": count,
            "minimum_pt": minimum,
            "scope": "Stroke operators including placement scale; raster lines "
            "and filled outlines "
            "are excluded. Hairlines and nonuniform transforms are unmeasured.",
        },
    )


def _color_space_finding(trace: TracedDocument, allowed: Sequence[str]) -> Finding:
    colors: list[object] = []
    uncertain = False
    count = 0
    for number, page in trace.pages:
        for node in page.iter():
            if node.tag not in COLOR_OPERATIONS:
                continue
            space = node.get("colorspace")
            if space is None:
                uncertain = True
                continue
            count += 1
            if space not in allowed:
                colors.append({"page": number, "operation": node.tag, "color_space": space})
    return measurement_finding(
        PdfRule.COLOR_SPACE_POLICY,
        "Compared rendered operation color-space names with the supplied list.",
        colors,
        incomplete=uncertain or count == 0,
        details={
            "measured_operations": count,
            "allowed": list(allowed),
            "scope": "MuPDF rendering color-space names; source encoding, contrast, color-only "
            "meaning and perceptual accessibility are not established.",
        },
    )


@dataclass(slots=True)
class _TextContrast:
    """The text operations compared with the assumed background."""

    violations: list[object] = field(default_factory=list)
    measured: int = 0
    uncertain: bool = False


def _measure_text_contrast(
    trace: TracedDocument, minimum: float, background: Rgb, *, uncertain: bool
) -> _TextContrast:
    """Compare each text operation's opaque color with the supplied background."""
    result = _TextContrast(uncertain=uncertain)
    background_luminance = relative_luminance(background)
    for number, page in trace.pages:
        for node in page.iter():
            if node.tag not in _TEXT_OPERATIONS:
                continue
            foreground = _simple_rgb(node)
            if foreground is None or not node.findall(".//g"):
                result.uncertain = True
                continue
            result.measured += 1
            ratio = wcag_contrast_ratio(relative_luminance(foreground), background_luminance)
            if ratio < minimum:
                result.violations.append(
                    {"page": number, "foreground_rgb": foreground, "contrast_ratio": ratio}
                )
    return result


@dataclass(slots=True)
class _PagePalettes:
    """The distinct opaque colors of each page, in page order, each sorted."""

    pages: list[tuple[int, list[Rgb]]] = field(default_factory=list)
    uncertain: bool = False

    @property
    def entries(self) -> int:
        return sum(len(colors) for _, colors in self.pages)


def _page_palettes(trace: TracedDocument, *, uncertain: bool) -> _PagePalettes:
    """Collect each page's painted colors, capped at ``MAX_PAGE_PALETTE_COLORS``."""
    result = _PagePalettes(uncertain=uncertain)
    for number, page in trace.pages:
        palette: set[Rgb] = set()
        for node in page.iter():
            if node.tag not in COLOR_OPERATIONS:
                continue
            rgb = _simple_rgb(node)
            if rgb is not None and (len(palette) < MAX_PAGE_PALETTE_COLORS or rgb in palette):
                palette.add(rgb)
            else:
                result.uncertain = True
        result.pages.append((number, sorted(palette)))
    return result


@dataclass(slots=True)
class _PairScan:
    """How many color pairs were compared, and whether more existed than the cap allows."""

    compared: int = 0
    capped: bool = False


def _close_pairs(
    palettes: _PagePalettes, minimum_difference: float, scan: _PairScan
) -> Iterator[tuple[int, Rgb, Rgb, float]]:
    """Yield ``(page, first, second, luminance difference)`` for each pair closer than
    ``minimum_difference``, comparing at most ``MAX_PAIR_CHECKS`` pairs overall."""
    for number, colors in palettes.pages:
        luminances = [(color, relative_luminance(color)) for color in colors]
        for (first, first_luminance), (second, second_luminance) in combinations(luminances, 2):
            if scan.compared >= MAX_PAIR_CHECKS:
                scan.capped = True
                return
            scan.compared += 1
            difference = abs(first_luminance - second_luminance)
            if difference < minimum_difference:
                yield number, first, second, difference


def _has_compositing(trace: TracedDocument) -> bool:
    """Whether any operation blends: nominal colors then say little about visible ones."""
    return any(node.tag in _COMPOSITING_TAGS for node in trace.root.iter())


def _text_contrast_finding(options: PdfCheckOptions, contrast: _TextContrast) -> Finding:
    return heuristic_finding(
        PdfRule.ASSUMED_TEXT_CONTRAST,
        "Compared opaque text colors with the supplied background.",
        contrast.violations,
        incomplete=contrast.uncertain or contrast.measured == 0,
        details={
            "measured_text_operations": contrast.measured,
            "minimum_ratio": options.min_text_contrast,
            "assumed_background_rgb": options.contrast_background_rgb,
            "scope": "DeviceRGB/Gray treated as sRGB on a user-asserted uniform background. "
            "Actual backgrounds, raster/outlined text, alpha blending, soft masks, "
            "ICC conversion and semantic accessibility are not established.",
        },
    )


def _separation_finding(palettes: _PagePalettes, minimum_difference: float) -> Finding:
    scan = _PairScan()
    samples: list[object] = []
    total = 0
    for page, first, second, difference in _close_pairs(palettes, minimum_difference, scan):
        total += 1
        if len(samples) < MAX_SAMPLES:
            samples.append(
                {"page": page, "colors_rgb": [first, second], "luminance_difference": difference}
            )
    return heuristic_finding(
        PdfRule.GRAYSCALE_SEPARATION,
        "Compared distinct observed page colors by nominal luminance.",
        samples,
        incomplete=palettes.uncertain or scan.capped or palettes.entries == 0,
        total_violations=total,
        details={
            "page_palette_entries": palettes.entries,
            "compared_pairs": scan.compared,
            "minimum_difference": minimum_difference,
            "scope": "Opaque DeviceRGB/Gray colors treated as sRGB. A close pair is advisory; "
            "semantic color-only encoding, visual proximity and perceptual discrimination "
            "are not inferred. Raster colors, ICC conversion and compositing are unmeasured.",
        },
    )


def _contrast_findings(trace: TracedDocument, options: PdfCheckOptions) -> list[Finding]:
    result: list[Finding] = []
    compositing = _has_compositing(trace)
    if options.min_text_contrast is not None:
        background = options.contrast_background_rgb
        assert background is not None, "min_text_contrast is only valid with a background"
        contrast = _measure_text_contrast(
            trace, options.min_text_contrast, background, uncertain=compositing
        )
        result.append(_text_contrast_finding(options, contrast))
    if options.min_grayscale_luminance_difference is not None:
        palettes = _page_palettes(trace, uncertain=compositing)
        result.append(_separation_finding(palettes, options.min_grayscale_luminance_difference))
    return result


def _trace_findings(output: str, pages: int, options: PdfCheckOptions) -> list[Finding]:
    trace = parse_mupdf_trace(output, pages)
    result: list[Finding] = []
    if options.min_stroke_width_pt is not None:
        result.append(_stroke_width_finding(trace, options.min_stroke_width_pt))
    if options.allowed_color_spaces:
        result.append(_color_space_finding(trace, options.allowed_color_spaces))
    result.extend(_contrast_findings(trace, options))
    return result


async def measure_paint_trace(
    session: PdfToolSession, pages: int, options: PdfCheckOptions, rules: Collection[str]
) -> list[Finding]:
    """The configured paint measurements; each of ``rules`` is inconclusive if mutool fails."""
    return await ToolMeasurement(
        "trace",
        mupdf_trace_argv(pages),
        tuple(rules),
        lambda result: _trace_findings(result.stdout, pages, options),
    ).run(session)
