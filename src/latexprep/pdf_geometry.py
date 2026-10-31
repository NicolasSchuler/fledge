"""Page geometry, colour and render-size arithmetic shared by the PDF measurements.

Coordinates are PDF points (1/72 inch). Boxes are ``(left, top, right, bottom)``. The
render limits here bound the rasters of configured measurements and of rendered
comparisons; they say nothing about how a page is laid out.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from pathlib import Path
from typing import Literal

from .errors import PreparationError
from .raster_pixels import (
    MAX_RASTER_DIMENSION,
    MAX_RASTER_PIXELS,
    RasterImage,
    read_pnm_raster,
)

POINTS_PER_INCH = 72
# Comparison and preview renders use this resolution; every page must fit it.
COMPARISON_DPI = 144
# Configured measurement renders use their own, lower resolution and limits.
MAX_MEASUREMENT_PIXELS = 4_000_000
MAX_MEASUREMENT_DIMENSION = 4000
MIN_MEASUREMENT_DPI = 18
MAX_MEASUREMENT_DPI = 144
# Pairs of boxes or colors one measurement compares before it reports itself incomplete.
MAX_PAIR_CHECKS = 250_000
# WCAG adds this flare term to both luminances before taking their ratio: contrast is
# (L1 + 0.05) / (L2 + 0.05), which models a little ambient light.
WCAG_FLARE = 0.05


def finite_number(value: str | None) -> float:
    """Parse a finite float from tool output; anything else is an invalid measurement.

    Args:
        value: The text the tool printed, or ``None`` when it printed nothing.

    Returns:
        The number.

    Raises:
        PreparationError: ``value`` is missing, not a number, or not finite.
    """
    try:
        number = float(value) if value is not None else math.nan
    except ValueError as error:
        raise PreparationError("PDF tool returned an invalid numerical measurement") from error
    if not math.isfinite(number):
        raise PreparationError("PDF tool returned an invalid numerical measurement")
    return number


def contains(outer: tuple[float, ...], inner: tuple[float, ...], tolerance: float) -> bool:
    """Return whether ``inner`` lies within ``outer``, allowing ``tolerance`` points per side."""
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def intersection_area(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    """Return the area of overlap of two boxes, zero when they are apart."""
    return max(0.0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0.0, min(first[3], second[3]) - max(first[1], second[1])
    )


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    """Compute the WCAG relative luminance of sRGB components in [0, 1]."""
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in rgb
    ]
    return sum(
        value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
    )


def wcag_contrast_ratio(first_luminance: float, second_luminance: float) -> float:
    """Return the WCAG contrast ratio, from 1 to 21, of two relative luminances in either order."""
    lighter, darker = max(first_luminance, second_luminance), min(first_luminance, second_luminance)
    return (lighter + WCAG_FLARE) / (darker + WCAG_FLARE)


def measurement_raster_size(size: tuple[float, float], dpi: int) -> tuple[int, int]:
    """Return the pixel width and height of a page of ``size`` points rendered at ``dpi``."""
    return (
        math.ceil(size[0] * dpi / POINTS_PER_INCH),
        math.ceil(size[1] * dpi / POINTS_PER_INCH),
    )


def exceeds_measurement_raster(dimensions: tuple[int, int]) -> bool:
    """Return whether a render of ``dimensions`` pixels is beyond the measurement limits."""
    return math.prod(dimensions) > MAX_MEASUREMENT_PIXELS or max(dimensions) > (
        MAX_MEASUREMENT_DIMENSION
    )


def require_comparison_raster_fit(sizes: Sequence[tuple[float, float]]) -> None:
    """Reject pages that cannot be rendered at the comparison resolution within raster limits.

    Args:
        sizes: The width and height of every page, in points.

    Raises:
        PreparationError: A page is too large; the message names the first one.
    """
    for page, size in enumerate(sizes, 1):
        pixels = measurement_raster_size(size, COMPARISON_DPI)
        if max(pixels) > MAX_RASTER_DIMENSION or math.prod(pixels) > MAX_RASTER_PIXELS:
            raise PreparationError(
                f"Page {page} ({size[0]:g} x {size[1]:g} points) is too large to compare as "
                f"images at {COMPARISON_DPI} DPI (limits: {MAX_RASTER_PIXELS:,} pixels, "
                f"{MAX_RASTER_DIMENSION:,} per side); compare a smaller export of the document"
            )


def read_measurement_raster(
    path: Path, *, channels: Literal[1, 3], max_file_bytes: int
) -> RasterImage:
    """Read a configured measurement render of at most ``MAX_MEASUREMENT_PIXELS`` pixels.

    Args:
        path: The renderer's output file.
        channels: 1 for a gray page, 3 for an RGB page.
        max_file_bytes: The largest file size accepted.

    Returns:
        The page.

    Raises:
        PreparationError: The file or its raster is invalid or beyond the limits.
    """
    return read_pnm_raster(
        path,
        channels=channels,
        max_pixels=MAX_MEASUREMENT_PIXELS,
        max_dimension=MAX_RASTER_DIMENSION,
        max_file_bytes=max_file_bytes,
    )
