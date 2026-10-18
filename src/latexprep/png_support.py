"""PNG framing shared by the encoder, the validator and the two readers (a leaf module).

Four variants, from strictest to most lenient:

- ``encode_gray_png`` writes an 8-bit grayscale image from raw pixels.
- ``png_dimensions`` validates bytes produced by a renderer we run ourselves: the
  signature, a first IHDR chunk, the trailing IEND chunk and caller-supplied raster
  limits. It does not check chunk checksums.
- ``iter_png_chunks`` walks a whole, untrusted file chunk by chunk and rejects any
  structural defect: bad checksums, truncation, an unbounded chunk count, a missing
  IEND chunk and bytes after it. Privacy scans use it.
- ``read_png_header`` reads the IHDR fields from a possibly truncated file prefix
  without any checksum, for cheap colour-type observations.

The two JPEG walkers deliberately stay separate (``metadata_privacy`` walks whole files
strictly, ``submission_readiness_checks`` reads a bounded prefix leniently).
"""

from __future__ import annotations

import struct
import zlib
from collections.abc import Iterator
from typing import NamedTuple

from .errors import PreparationError

PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_IHDR_CHUNK_START = b"\x00\x00\x00\rIHDR"
_IEND_CHUNK = b"\x00\x00\x00\x00IEND\xaeB`\x82"
_IHDR_DATA_BYTES = 13
_CHUNK_OVERHEAD = 12
# Signature (8) + IHDR chunk (25) + a minimal empty IDAT chunk (12) + IEND chunk (12): no
# complete PNG image is shorter.
MIN_PNG_BYTES = 57


class PngHeader(NamedTuple):
    """The fields of an IHDR chunk that observations need."""

    width: int
    height: int
    bit_depth: int
    color_type: int


class PngChunk(NamedTuple):
    """One checksum-verified chunk: its four-byte type and its payload."""

    kind: bytes
    data: bytes


def _chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload))
    )


def encode_gray_png(width: int, height: int, pixels: bytes) -> bytes:
    """Encode already validated 8-bit grayscale pixels without another dependency."""
    rows = b"".join(b"\0" + pixels[y * width : (y + 1) * width] for y in range(height))
    return (
        PNG_SIGNATURE
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(rows))
        + _chunk(b"IEND", b"")
    )


def png_dimensions(data: bytes, *, max_dimension: int, max_pixels: int) -> tuple[int, int]:
    """Validate the framing and dimensions of internally rendered PNG bytes."""
    if (
        len(data) < MIN_PNG_BYTES
        or not data.startswith(PNG_SIGNATURE + _IHDR_CHUNK_START)
        or not data.endswith(_IEND_CHUNK)
    ):
        raise PreparationError("The renderer did not produce a complete PNG image")
    width, height = int.from_bytes(data[16:20]), int.from_bytes(data[20:24])
    if min(width, height) < 1 or max(width, height) > max_dimension or width * height > max_pixels:
        raise PreparationError("The renderer produced oversized PNG dimensions")
    return width, height


def iter_png_chunks(data: bytes, *, max_chunks: int) -> Iterator[PngChunk]:
    """Yield every chunk of a complete PNG file, ending with its IEND chunk.

    Each chunk is yielded only after its length and checksum are verified, so a
    consumer sees exactly the chunks before a structural defect. The walk raises
    ``PreparationError`` for a wrong signature, more than ``max_chunks`` chunks, a
    truncated or oversized chunk, a bad checksum, an IEND chunk that is not empty or
    is followed by more bytes, and a file that ends without an IEND chunk.
    """
    if not data.startswith(PNG_SIGNATURE):
        raise PreparationError("PNG signature is invalid")
    cursor = len(PNG_SIGNATURE)
    chunks = 0
    while cursor < len(data):
        chunks += 1
        if chunks > max_chunks or cursor + _CHUNK_OVERHEAD > len(data):
            raise PreparationError("PNG chunk structure is incomplete or oversized")
        length = int.from_bytes(data[cursor : cursor + 4], "big")
        kind = data[cursor + 4 : cursor + 8]
        end = cursor + 8 + length
        if end + 4 > len(data):
            raise PreparationError("PNG chunk exceeds its file")
        payload = data[cursor + 8 : end]
        if zlib.crc32(kind + payload) != int.from_bytes(data[end : end + 4], "big"):
            raise PreparationError("PNG chunk checksum is invalid")
        if kind == b"IEND":
            if length or end + 4 != len(data):
                raise PreparationError("PNG has malformed termination or uninspected trailing data")
            yield PngChunk(kind, payload)
            return
        yield PngChunk(kind, payload)
        cursor = end + 4
    raise PreparationError("PNG termination was not found")


def read_png_header(data: bytes) -> PngHeader | None:
    """The IHDR fields at the start of ``data``, or None if it is not a PNG header.

    Only the signature and the first chunk's type and length are checked, so a file
    prefix long enough to hold the IHDR chunk is enough.
    """
    header_end = len(PNG_SIGNATURE) + 8 + _IHDR_DATA_BYTES
    if len(data) < header_end or not data.startswith(PNG_SIGNATURE + _IHDR_CHUNK_START):
        return None
    width, height, bit_depth, color_type = struct.unpack(">IIBB", data[16:26])
    return PngHeader(width, height, bit_depth, color_type)
