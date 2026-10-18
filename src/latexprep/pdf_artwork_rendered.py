"""Rendered-pixel artwork measurements: figure whitespace, edge ink, previews and contrast.

Pages are rendered once per measurement by Poppler at the configured resolution and read
back as bounded PNM rasters. Figure regions are measured on a grayscale render (the area
outside the ink bounding box, and ink in a boundary band); the grayscale preview keeps
the validated render as a PNG; contrast samples compare the composited RGB extrema of
explicit foreground and background patches. Every result is advisory and states what it
does not judge.

Does not own: trace-based measurements (``pdf_artwork_drawings``), the options
(``pdf_artwork_options``) or deciding which measurements run (``pdf_artwork``).
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import Final, NamedTuple

from .cancellation import cancellation_point
from .errors import PreparationError
from .findings import Finding
from .measurement_findings import heuristic_finding, measurement_finding, unavailable_finding
from .option_config import EIGHT_BIT_MAXIMUM
from .pdf_artwork_options import (
    REGION_RULES,
    ArtworkLimits,
    ArtworkRule,
    PdfArtworkOptions,
    PdfContrastSample,
    configured_artwork_rules,
)
from .pdf_check_options import PdfRegion
from .pdf_geometry import (
    WCAG_FLARE,
    contains,
    exceeds_measurement_raster,
    measurement_raster_size,
    read_measurement_raster,
    relative_luminance,
)
from .pdf_tool_output import require_rendered_pages
from .pdf_tools import PdfToolSession
from .png_support import encode_gray_png
from .raster_pixels import InkMask, PixelBox, RasterImage, channel_extrema

# The pixel size of a render may differ from the size computed from the page box by this
# many pixels per side (Poppler rounds the page size itself).
_RENDER_SIZE_SLACK_PIXELS: Final = 1


class PageGeometry(NamedTuple):
    """Every page size in points, and the pages whose frame rendered measurements must skip."""

    sizes: Sequence[tuple[float, float]]
    misaligned_frame_pages: frozenset[int]


def figure_regions(
    options: PdfArtworkOptions,
    sizes: Sequence[tuple[float, float]],
    *,
    whole_pages_by_default: bool,
) -> tuple[PdfRegion, ...]:
    """The regions to measure: the configured ones, else every whole page of an input figure."""
    if options.figure_regions or not whole_pages_by_default:
        return options.figure_regions
    return tuple(
        PdfRegion(f"Input page {index}", index, 0, 0, *size) for index, size in enumerate(sizes, 1)
    )


def _render_matches(actual: tuple[int, int], expected: tuple[int, int]) -> bool:
    return all(
        abs(found - wanted) <= _RENDER_SIZE_SLACK_PIXELS
        for found, wanted in zip(actual, expected, strict=True)
    )


@dataclass(frozen=True, slots=True)
class _RenderedFigures:
    """What every rendered artwork page of one inspection shares."""

    regions: tuple[PdfRegion, ...]
    region_rules: tuple[ArtworkRule, ...]
    misaligned_frame_pages: frozenset[int]
    options: PdfArtworkOptions
    limits: ArtworkLimits
    preview_directory: Path
    max_file_bytes: int


class GrayPage(NamedTuple):
    """A rendered grayscale page with its size in points, to map pixels back to the page."""

    page: int
    image: RasterImage
    width_pt: float
    height_pt: float


class RegionInk(NamedTuple):
    """Ink measured within one figure region of a rendered page."""

    ink_pixels: int
    edge_ink_pixels: int
    outside_content_box_ratio: float
    content_bounds_pt: tuple[float, float, float, float] | None


def region_ink(
    raster: GrayPage, ink: InkMask, pixels: PixelBox, options: PdfArtworkOptions
) -> RegionInk:
    """Ink count, boundary ink and the ink bounding box within a pixel rectangle."""
    left, top, right, bottom = pixels
    width, height = ink.width, ink.height
    band = options.figure_edge_band_pt or 0
    band_x = math.ceil(band * width / raster.width_pt)
    band_y = math.ceil(band * height / raster.height_pt)
    count = ink.count(pixels)
    bounds = ink.bounds(pixels) if count else None
    if bounds is None:
        return RegionInk(0, 0, 1, None)
    # Boundary ink lies outside the rectangle shrunk by the band on every side.
    inner = ink.count((left + band_x, top + band_y, right - band_x, bottom - band_y))
    min_x, min_y, max_x, max_y = bounds
    total = (right - left) * (bottom - top)
    return RegionInk(
        count,
        count - inner,
        1 - ((max_x - min_x) * (max_y - min_y) / total),
        (
            min_x * raster.width_pt / width,
            min_y * raster.height_pt / height,
            max_x * raster.width_pt / width,
            max_y * raster.height_pt / height,
        ),
    )


def _whitespace_finding(
    limit: float, where: str, ink: RegionInk, details: dict[str, object]
) -> Finding:
    over = ink.outside_content_box_ratio > limit
    if not ink.ink_pixels:
        outcome = f"{where} has no content against the configured background"
    else:
        outcome = (
            f"{where} leaves {ink.outside_content_box_ratio:.1%} of its area empty around "
            f"the content, {'above' if over else 'within'} the {limit:.1%} limit"
        )
    return heuristic_finding(
        ArtworkRule.FIGURE_WHITESPACE,
        outcome + ".",
        [details] if over else [],
        details={
            **details,
            "maximum_ratio": limit,
            "scope": "Area outside the ink box under the configured background; "
            "internal white areas and intended padding are not judged.",
        },
    )


def _edge_ink_finding(
    band: float, where: str, ink: RegionInk, details: dict[str, object]
) -> Finding:
    if ink.edge_ink_pixels:
        message = (
            f"Ink touches the outer {band:g} pt border of {where} "
            f"({ink.edge_ink_pixels} pixels); the figure may be cropped or framed."
        )
    else:
        message = f"No ink touches the outer {band:g} pt border of {where}."
    return heuristic_finding(
        ArtworkRule.FIGURE_EDGE_INK,
        message,
        [{**details, "edge_ink_pixels": ink.edge_ink_pixels}] if ink.edge_ink_pixels else [],
        details={
            **details,
            "edge_band_pt": band,
            "scope": "Boundary ink is a cropping/border candidate, not proof of "
            "clipping; intended borders and touching content need review.",
        },
    )


def _region_findings(
    figures: _RenderedFigures, raster: GrayPage, region: PdfRegion, ink: RegionInk
) -> list[Finding]:
    options = figures.options
    details: dict[str, object] = {
        "page": raster.page,
        "region": region.name,
        "content_bounds_pt": ink.content_bounds_pt,
        "outside_content_box_ratio": ink.outside_content_box_ratio,
        "ink_pixels": ink.ink_pixels,
        "background_gray": options.background_gray,
        "raster_dpi": options.raster_dpi,
    }
    where = f"{region.name} on page {raster.page}"
    findings = []
    if options.max_figure_whitespace_ratio is not None:
        findings.append(
            _whitespace_finding(options.max_figure_whitespace_ratio, where, ink, details)
        )
    if options.figure_edge_band_pt is not None:
        findings.append(_edge_ink_finding(options.figure_edge_band_pt, where, ink, details))
    return findings


def _grayscale_preview_finding(figures: _RenderedFigures, raster: GrayPage) -> Finding:
    preview = figures.preview_directory / f"page-{raster.page}.png"
    preview.parent.mkdir(exist_ok=True)
    # Copy validated bytes while this operation still owns its render lease.
    encoded = encode_gray_png(raster.image.width, raster.image.height, raster.image.data)
    if len(encoded) > figures.max_file_bytes:
        raise PreparationError("Encoded artwork preview exceeds the file limit")
    preview.write_bytes(encoded)
    return measurement_finding(
        ArtworkRule.GRAYSCALE_PREVIEW,
        f"Created grayscale preview for page {raster.page}.",
        [],
        details={
            "page": raster.page,
            "artifacts": [str(preview)],
            "raster_dpi": figures.options.raster_dpi,
            "scope": "Review preview; semantic color independence is unverified.",
        },
    )


def _pixel_rectangle(region: PdfRegion, raster: GrayPage) -> PixelBox:
    """The pixels that cover a region of the rendered page, rounding outwards."""
    width, height = raster.image.width, raster.image.height
    return (
        math.floor(region.left_pt * width / raster.width_pt),
        math.floor(region.top_pt * height / raster.height_pt),
        math.ceil(region.right_pt * width / raster.width_pt),
        math.ceil(region.bottom_pt * height / raster.height_pt),
    )


def _unavailable_for_rules(
    rules: Sequence[ArtworkRule], error: Exception, **details: object
) -> list[Finding]:
    return [
        unavailable_finding(rule, error, severity="warning", details=details if details else None)
        for rule in rules
    ]


def _rendered_page_findings(
    path: Path,
    *,
    figures: _RenderedFigures,
    page: int,
    size: tuple[float, float],
    expected: tuple[int, int],
) -> list[Finding]:
    image = read_measurement_raster(path, channels=1, max_file_bytes=figures.max_file_bytes)
    if not _render_matches((image.width, image.height), expected):
        raise PreparationError("Artwork render dimensions disagree with page geometry")
    raster = GrayPage(page, image, *size)
    ink_mask: InkMask | None = None
    findings: list[Finding] = []
    if figures.options.grayscale_preview:
        findings.append(_grayscale_preview_finding(figures, raster))
    pixel_visits = 0
    for region in figures.regions:
        if region.page != page:
            continue
        cancellation_point()
        if page in figures.misaligned_frame_pages or not contains((0, 0, *size), region.box, 0):
            findings.extend(
                _unavailable_for_rules(
                    figures.region_rules,
                    PreparationError(f"Figure region {region.name} has unsupported page geometry"),
                )
            )
            continue
        rectangle = _pixel_rectangle(region, raster)
        pixel_visits += (rectangle[2] - rectangle[0]) * (rectangle[3] - rectangle[1])
        if pixel_visits > figures.limits.max_pixel_visits_per_page:
            findings.extend(
                _unavailable_for_rules(
                    figures.region_rules,
                    PreparationError("Figure regions exceed the bounded pixel sampling work"),
                    page=page,
                    region=region.name,
                )
            )
            continue
        if ink_mask is None:
            ink_mask = InkMask.from_gray(
                image, figures.options.background_gray, figures.options.ink_difference
            )
        ink = region_ink(raster, ink_mask, rectangle, figures.options)
        findings.extend(_region_findings(figures, raster, region, ink))
    return findings


async def _rendered_page(
    session: PdfToolSession,
    figures: _RenderedFigures,
    page: int,
    size: tuple[float, float],
    rendered_rules: Sequence[ArtworkRule],
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
        return _unavailable_for_rules(rendered_rules, error, page=page)


def _unmeasurable_region_findings(
    regions: Sequence[PdfRegion], pages: int, region_rules: Sequence[ArtworkRule]
) -> list[Finding]:
    """Unavailable evidence for a policy without regions, and for regions beyond the document."""
    result: list[Finding] = []
    if not regions:
        result.extend(
            _unavailable_for_rules(
                region_rules,
                PreparationError("Figure measurement needs explicit regions on a manuscript PDF"),
            )
        )
    for region in regions:
        if region.page > pages:
            result.extend(
                _unavailable_for_rules(
                    region_rules,
                    PreparationError(f"Figure region {region.name} is outside the document"),
                )
            )
    return result


async def inspect_rendered_figures(
    session: PdfToolSession,
    geometry: PageGeometry,
    options: PdfArtworkOptions,
    regions: tuple[PdfRegion, ...],
    limits: ArtworkLimits,
) -> list[Finding]:
    """Whitespace, edge ink and grayscale previews of the pages that hold figure regions."""
    sizes = geometry.sizes
    configured = configured_artwork_rules(options)
    region_rules = tuple(rule for rule in configured if rule in REGION_RULES)
    rendered_rules = (
        *region_rules,
        *((ArtworkRule.GRAYSCALE_PREVIEW,) if options.grayscale_preview else ()),
    )
    result = _unmeasurable_region_findings(regions, len(sizes), region_rules)
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
            if regions or rule == ArtworkRule.GRAYSCALE_PREVIEW
        )
        return result
    figures = _RenderedFigures(
        regions,
        region_rules,
        geometry.misaligned_frame_pages,
        options,
        limits,
        session.work / "previews",
        session.runner.limits.max_file_bytes,
    )
    for page in page_numbers:
        result.extend(await _rendered_page(session, figures, page, sizes[page - 1], rendered_rules))
    return result


def _pixel_region(
    region: PdfRegion, size: tuple[float, float], raster: tuple[int, int]
) -> PixelBox:
    """The pixels whose full area lies within a contrast patch."""
    if not contains((0, 0, *size), region.box, 0):
        raise PreparationError("Contrast sample region lies outside the rendered page")
    left = math.ceil(region.left_pt * raster[0] / size[0])
    top = math.ceil(region.top_pt * raster[1] / size[1])
    right = math.floor(region.right_pt * raster[0] / size[0])
    bottom = math.floor(region.bottom_pt * raster[1] / size[1])
    if left >= right or top >= bottom:
        raise PreparationError("Contrast sample has no fully contained pixel at the selected DPI")
    return left, top, right, bottom


class ContrastBounds(NamedTuple):
    """The lowest and highest contrast ratio the patch colours allow."""

    minimum: float
    maximum: float


def _contrast_interval(
    foreground: tuple[float, float], background: tuple[float, float]
) -> ContrastBounds:
    """Contrast-ratio bounds from the luminance ranges (low, high) of two patches."""
    low_fg, high_fg = foreground
    low_bg, high_bg = background
    minimum = 1.0
    if high_fg < low_bg:
        minimum = (low_bg + WCAG_FLARE) / (high_fg + WCAG_FLARE)
    elif high_bg < low_fg:
        minimum = (low_fg + WCAG_FLARE) / (high_bg + WCAG_FLARE)
    maximum = max(
        (high_fg + WCAG_FLARE) / (low_bg + WCAG_FLARE),
        (high_bg + WCAG_FLARE) / (low_fg + WCAG_FLARE),
    )
    return ContrastBounds(minimum, maximum)


def _luminance(rgb: tuple[int, ...]) -> float:
    red, green, blue = (value / EIGHT_BIT_MAXIMUM for value in rgb)
    return relative_luminance((red, green, blue))


def _sample_unavailable(sample: PdfContrastSample, page: int, error: Exception) -> Finding:
    return unavailable_finding(
        ArtworkRule.RENDERED_SAMPLE_CONTRAST,
        error,
        severity="warning",
        details={"sample": sample.name, "page": page},
    )


def _contrast_message(
    sample: PdfContrastSample, page: int, bounds: ContrastBounds, spread: int, uniform: bool
) -> str:
    required = f"{sample.minimum_ratio:g}:1"
    where = f"Sample '{sample.name}' on page {page}"
    lowest, highest = bounds
    if not uniform:
        return (
            f"{where} has uneven colours (channel values vary by up to {spread}; the "
            f"allowed spread is {sample.max_channel_spread}), so its contrast is only known to "
            f"lie between {lowest:.2f}:1 and {highest:.2f}:1 against the required {required}. "
            "Choose a uniform patch or allow a larger max_channel_spread."
        )
    if highest < sample.minimum_ratio:
        return (
            f"{where} has a contrast ratio of at most {highest:.2f}:1, below the required "
            f"{required}."
        )
    if lowest < sample.minimum_ratio:
        return (
            f"{where} has a contrast ratio between {lowest:.2f}:1 and {highest:.2f}:1, so the "
            f"required {required} cannot be confirmed or ruled out."
        )
    return f"{where} has a contrast ratio of at least {lowest:.2f}:1 (required {required})."


def _sample_contrast_finding(
    sample: PdfContrastSample,
    page: int,
    image: RasterImage,
    boxes: tuple[PixelBox, PixelBox],
) -> Finding:
    colors = [channel_extrema(image, box) for box in boxes]
    spreads = [
        max(high - low for low, high in zip(minimum, maximum, strict=True))
        for minimum, maximum in colors
    ]
    luminances = [(_luminance(minimum), _luminance(maximum)) for minimum, maximum in colors]
    bounds = _contrast_interval(luminances[0], luminances[1])
    uniform = max(spreads) <= sample.max_channel_spread
    details: dict[str, object] = {
        "page": page,
        "sample": sample.name,
        "foreground_bounds_pt": sample.foreground.box,
        "background_bounds_pt": sample.background.box,
        "foreground_rgb_range": colors[0],
        "background_rgb_range": colors[1],
        "contrast_ratio_min": bounds.minimum,
        "contrast_ratio_max": bounds.maximum,
        "minimum_ratio": sample.minimum_ratio,
        "channel_spreads": spreads,
        "allowed_channel_spread": sample.max_channel_spread,
    }
    return heuristic_finding(
        ArtworkRule.RENDERED_SAMPLE_CONTRAST,
        _contrast_message(sample, page, bounds, max(spreads), uniform),
        [details] if uniform and bounds.maximum < sample.minimum_ratio else [],
        incomplete=not uniform or bounds.minimum < sample.minimum_ratio <= bounds.maximum,
        details={
            **details,
            "scope": "Rendered/composited RGB patches interpreted as sRGB. "
            "Channel extrema give conservative luminance/ratio bounds. "
            "Nonuniform "
            "patches need an explicit spread tolerance; text meaning, all text "
            "contrast, perceptual quality and accessibility are not inferred.",
        },
    )


@dataclass(frozen=True, slots=True)
class _ContrastPage:
    """The contrast samples of one page and the render they are measured on."""

    page: int
    size: tuple[float, float]
    expected: tuple[int, int]
    samples: tuple[PdfContrastSample, ...]
    max_file_bytes: int
    max_pixel_visits: int


def _rendered_contrast_findings(path: Path, job: _ContrastPage) -> list[Finding]:
    image = read_measurement_raster(path, channels=3, max_file_bytes=job.max_file_bytes)
    dimensions = (image.width, image.height)
    if not _render_matches(dimensions, job.expected):
        raise PreparationError("Contrast render dimensions disagree with page geometry")
    findings: list[Finding] = []
    pixel_visits = 0
    for sample in job.samples:
        cancellation_point()
        try:
            boxes = (
                _pixel_region(sample.foreground, job.size, dimensions),
                _pixel_region(sample.background, job.size, dimensions),
            )
            pixel_visits += sum((box[2] - box[0]) * (box[3] - box[1]) for box in boxes)
            if pixel_visits > job.max_pixel_visits:
                raise PreparationError("Contrast patches exceed the bounded pixel sampling work")
            findings.append(_sample_contrast_finding(sample, job.page, image, boxes))
        except PreparationError as error:
            findings.append(_sample_unavailable(sample, job.page, error))
    return findings


async def _contrast_page(
    session: PdfToolSession, job: _ContrastPage, options: PdfArtworkOptions
) -> list[Finding]:
    try:
        if exceeds_measurement_raster(job.expected):
            raise PreparationError("Contrast render exceeds bounded raster dimensions")
        return await session.render_page(
            f"contrast-render-{job.page}",
            job.page,
            options.raster_dpi,
            partial(_rendered_contrast_findings, job=job),
            raster="rgb",
        )
    except (PreparationError, OSError) as error:
        return [_sample_unavailable(sample, job.page, error) for sample in job.samples]


async def inspect_sample_contrast(
    session: PdfToolSession,
    geometry: PageGeometry,
    options: PdfArtworkOptions,
    limits: ArtworkLimits,
) -> list[Finding]:
    """Contrast of each explicit foreground/background patch pair, page by page."""
    sizes = geometry.sizes

    def measurable(page: int) -> bool:
        return page <= len(sizes) and page not in geometry.misaligned_frame_pages

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
        size = sizes[page - 1]
        job = _ContrastPage(
            page,
            size,
            measurement_raster_size(size, options.raster_dpi),
            tuple(s for s in options.contrast_samples if s.foreground.page == page),
            session.runner.limits.max_file_bytes,
            limits.max_pixel_visits_per_page,
        )
        result.extend(await _contrast_page(session, job, options))
    return result
