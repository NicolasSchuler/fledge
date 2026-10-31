"""Bounded PNM raster reading and pixel analysis in C-speed stdlib byte operations (a leaf module).

Pixel loops over ``bytes`` in Python cost 10 to 100 times more than the equivalent
byte operations of the standard library, so every analysis here is built from them:

- ``read_pnm_raster`` reads a renderer's binary PGM (1 channel) or PPM (3 channels)
  page into a ``RasterImage`` after validating the header against caller-supplied
  limits, before it allocates the pixel buffer.
- ``InkMask`` marks the pixels that differ from a background gray level by at least a
  threshold (a 256-entry ``bytes.translate`` table) and answers ink counts and ink
  bounding boxes with ``count``, ``find`` and ``rfind`` over row slices.
- ``channel_extrema`` returns each channel's minimum and maximum in a rectangle
  using strided row slices and the builtin ``min`` and ``max``.
- ``count_changed_pixels`` counts the pixels whose channels differ by more than a
  tolerance. It skips identical chunks and compares the others with big-integer
  arithmetic on 16-bit lanes, so no per-pixel Python code runs.

Rectangles are ``(left, top, right, bottom)`` pixel boxes with exclusive right and
bottom edges. They are clamped to the image, and an empty box yields an empty result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Literal

from .cancellation import cancellation_point
from .errors import PreparationError
from .file_safety import open_regular_file

# Largest page a renderer may produce for a comparison, and the largest side of it.
MAX_RASTER_PIXELS = 16_000_000
MAX_RASTER_DIMENSION = 8192
PixelBox = tuple[int, int, int, int]
# Room for a PNM header and its trailing newline in the size bound of a raster file.
_HEADER_ALLOWANCE = 1024
_PNM_MAGIC = {1: b"P5", 3: b"P6"}
_PNM_KIND = {1: "8-bit grayscale PGM", 3: "8-bit RGB PPM"}
# A whole number of pixels for every channel count; big-integer lanes stay below 1 MiB.
_CHANGED_CHUNK_PIXELS = 1 << 16
_MAX_TOLERANCE = 255


@dataclass(frozen=True, slots=True)
class RasterImage:
    """Interleaved 8-bit channels, row by row from the top-left pixel.

    Construction raises ``ValueError`` when ``data`` does not match the dimensions.

    Attributes:
        width: The width in pixels.
        height: The height in pixels.
        channels: 1 for grayscale, 3 for RGB.
        data: The ``width * height * channels`` samples.
    """

    width: int
    height: int
    channels: Literal[1, 3]
    data: bytes

    def __post_init__(self) -> None:
        if min(self.width, self.height) < 1 or len(self.data) != (
            self.width * self.height * self.channels
        ):
            raise ValueError("Raster data does not match its width, height and channel count")


def read_pnm_raster(
    path: Path,
    *,
    channels: Literal[1, 3],
    max_pixels: int,
    max_dimension: int = MAX_RASTER_DIMENSION,
    max_file_bytes: int | None = None,
) -> RasterImage:
    """Read a binary PGM (one channel) or PPM (three channels) page of 8-bit samples.

    The file size, the header and the dimensions are validated against the limits
    (``max_pixels`` never exceeds ``MAX_RASTER_PIXELS``) before the pixels are read, and
    the pixel data must fill the file exactly.

    Args:
        path: The renderer's output file.
        channels: 1 for a PGM page, 3 for a PPM page.
        max_pixels: The largest number of pixels accepted.
        max_dimension: The largest width or height accepted.
        max_file_bytes: A further limit on the file size, if any.

    Returns:
        The page.

    Raises:
        PreparationError: The file is missing, a link, a special file, too large or
            unreadable, its header is unsupported, its dimensions are invalid or
            oversized, or its pixel data does not fill it exactly.
    """
    pixel_limit = min(max_pixels, MAX_RASTER_PIXELS)
    size_limit = channels * pixel_limit + _HEADER_ALLOWANCE
    if max_file_bytes is not None:
        size_limit = min(size_limit, max_file_bytes)
    try:
        with open_regular_file(path) as (stream, size):
            if size > size_limit:
                raise PreparationError("Renderer did not produce a bounded raster page")
            # Validate the header before allocating pixel buffers; reading the entire
            # file and then slicing it transiently duplicates a full page in memory.
            header = re.match(
                _PNM_MAGIC[channels] + rb"\s+(\d+)\s+(\d+)\s+255(?:\r\n|\s)", stream.read(1024)
            )
            if header is None:
                raise PreparationError(
                    f"Renderer output is not a supported {_PNM_KIND[channels]} page"
                )
            width, height = int(header[1]), int(header[2])
            if (
                min(width, height) < 1
                or max(width, height) > max_dimension
                or width * height > pixel_limit
            ):
                raise PreparationError("Renderer output has invalid or oversized raster dimensions")
            stream.seek(header.end())
            pixels = stream.read(width * height * channels + 1)
    except OSError:
        # A missing file, a link, a special file or a failed read all mean no usable page.
        raise PreparationError("Renderer did not produce a bounded raster page") from None
    if len(pixels) != width * height * channels:
        raise PreparationError("Renderer output is truncated or has unexpected trailing data")
    return RasterImage(width, height, channels, pixels)


def _clamped(box: PixelBox, width: int, height: int) -> PixelBox | None:
    """Clamp ``box`` to an image of ``width`` by ``height`` pixels.

    Args:
        box: The rectangle to clamp.
        width: The image width in pixels.
        height: The image height in pixels.

    Returns:
        The clamped box, or ``None`` when it is empty.
    """
    left, top, right, bottom = (
        max(box[0], 0),
        max(box[1], 0),
        min(box[2], width),
        min(box[3], height),
    )
    return (left, top, right, bottom) if left < right and top < bottom else None


@dataclass(frozen=True, slots=True)
class InkMask:
    """Which pixels of a grayscale image differ from the background by a threshold.

    A pixel is ink when ``abs(value - background) >= difference``.

    Attributes:
        width: The width in pixels.
        height: The height in pixels.
        mask: One byte per pixel, 1 for ink and 0 otherwise.
    """

    width: int
    height: int
    mask: bytes

    @classmethod
    def from_gray(cls, image: RasterImage, background: int, difference: int) -> InkMask:
        """Mark the ink of a grayscale image.

        Args:
            image: The grayscale image.
            background: The background gray level.
            difference: The smallest distance from ``background`` that counts as ink.

        Returns:
            The ink mask of ``image``.

        Raises:
            ValueError: ``image`` is not grayscale.
        """
        if image.channels != 1:
            raise ValueError("Ink is measured on grayscale images")
        table = bytes(1 if abs(value - background) >= difference else 0 for value in range(256))
        return cls(image.width, image.height, image.data.translate(table))

    def count(self, box: PixelBox | None = None) -> int:
        """Count the ink pixels in ``box``, or in the whole image when it is ``None``."""
        if box is None:
            return self.mask.count(1)
        clamped = _clamped(box, self.width, self.height)
        if clamped is None:
            return 0
        left, top, right, bottom = clamped
        total = 0
        for y in range(top, bottom):
            cancellation_point()
            total += self.mask.count(1, y * self.width + left, y * self.width + right)
        return total

    def bounds(self, box: PixelBox) -> PixelBox | None:
        """Find the smallest box holding all ink in ``box``.

        Returns:
            The box, or ``None`` when ``box`` holds no ink.
        """
        clamped = _clamped(box, self.width, self.height)
        if clamped is None:
            return None
        left, top, right, bottom = clamped
        min_x, min_y, max_x, max_y = right, bottom, left - 1, top - 1
        for y in range(top, bottom):
            cancellation_point()
            start = y * self.width
            first = self.mask.find(1, start + left, start + right)
            if first < 0:
                continue
            last = self.mask.rfind(1, start + left, start + right)
            min_x, max_x = min(min_x, first - start), max(max_x, last - start)
            min_y, max_y = min(min_y, y), max(max_y, y)
        return None if max_x < min_x else (min_x, min_y, max_x + 1, max_y + 1)


def channel_extrema(image: RasterImage, box: PixelBox) -> tuple[tuple[int, ...], tuple[int, ...]]:
    """Find the per-channel minimum and maximum values within ``box``.

    An empty box yields the neutral extrema: 255 for every minimum and 0 for every maximum.

    Args:
        image: The image to measure.
        box: The rectangle to measure.

    Returns:
        The minima and the maxima, one value per channel each.
    """
    channels = image.channels
    minima, maxima = [255] * channels, [0] * channels
    if (clamped := _clamped(box, image.width, image.height)) is not None:
        left, top, right, bottom = clamped
        for y in range(top, bottom):
            cancellation_point()
            row = image.data[
                (y * image.width + left) * channels : (y * image.width + right) * channels
            ]
            for channel in range(channels):
                samples = row[channel::channels]
                minima[channel] = min(minima[channel], min(samples))
                maxima[channel] = max(maxima[channel], max(samples))
    return tuple(minima), tuple(maxima)


@lru_cache(maxsize=4)
def _lane_constants(pixels: int) -> tuple[int, int]:
    """Return a 1 in every 16-bit lane, and a 512 (bit 9) in every lane, for ``pixels`` lanes."""
    ones = int.from_bytes(b"\x01\x00" * pixels, "little")
    return ones, ones << 9


def _widen(samples: bytes) -> int:
    """Widen each sample byte into a 16-bit little-endian lane of a single big integer."""
    lanes = bytearray(2 * len(samples))
    lanes[::2] = samples
    return int.from_bytes(lanes, "little")


def _changed_in_chunk(left: bytes, right: bytes, channels: int, tolerance: int) -> int:
    """Count the pixels where any channel differs by more than ``tolerance``, lane-wise.

    With ``x`` and ``y`` the 8-bit samples in 16-bit lanes, ``x + 256 - y + (255 - tolerance)``
    reaches 512 exactly when ``x - y > tolerance``, and never exceeds 766, so bit 9 of each
    lane flags that direction (the swapped subtraction flags the other) without any carry
    into a neighbouring lane. A pixel changed when any channel flags either direction.

    Args:
        left: The interleaved samples of some pixels in one image.
        right: The interleaved samples of the same pixels in the other image.
        channels: The number of channels per pixel.
        tolerance: The largest channel difference that does not count as a change.

    Returns:
        The number of changed pixels.
    """
    ones, mark = _lane_constants(len(left) // channels)
    bias, margin = ones << 8, ones * (_MAX_TOLERANCE - tolerance)
    flags = 0
    for channel in range(channels):
        x, y = _widen(left[channel::channels]), _widen(right[channel::channels])
        flags |= ((x + bias - y + margin) | (y + bias - x + margin)) & mark
    return flags.bit_count()


def count_changed_pixels(left: RasterImage, right: RasterImage, tolerance: int) -> int:
    """Count the pixels where some channel differs by more than ``tolerance`` (0 to 255).

    Args:
        left: One raster.
        right: The other raster, of the same shape.
        tolerance: The largest channel difference that does not count as a change.

    Returns:
        The number of changed pixels; 0 when ``tolerance`` is 255.

    Raises:
        ValueError: The rasters differ in shape, or ``tolerance`` is outside 0 to 255.
    """
    if (left.width, left.height, left.channels) != (right.width, right.height, right.channels):
        raise ValueError("Changed pixels are counted between rasters of the same shape")
    if not 0 <= tolerance <= _MAX_TOLERANCE:
        raise ValueError("The channel tolerance must be from 0 to 255")
    if tolerance == _MAX_TOLERANCE:
        return 0
    chunk = _CHANGED_CHUNK_PIXELS * left.channels
    changed = 0
    for start in range(0, len(left.data), chunk):
        cancellation_point()
        first, second = left.data[start : start + chunk], right.data[start : start + chunk]
        # Identical bytes cannot exceed any tolerance; compare only differing chunks.
        if first != second:
            changed += _changed_in_chunk(first, second, left.channels, tolerance)
    return changed
