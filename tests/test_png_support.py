"""PNG framing: the encoder, the strict validator, the chunk walker and the header reader."""

from __future__ import annotations

import struct
import unittest
import zlib

from latexprep.errors import PreparationError
from latexprep.png_support import (
    MIN_PNG_BYTES,
    PNG_SIGNATURE,
    PngHeader,
    encode_gray_png,
    iter_png_chunks,
    png_dimensions,
    read_png_header,
)

LIMITS = {"max_dimension": 64, "max_pixels": 1024}


def chunk(kind: bytes, payload: bytes) -> bytes:
    return (
        struct.pack(">I", len(payload))
        + kind
        + payload
        + struct.pack(">I", zlib.crc32(kind + payload))
    )


def png(
    *chunks: tuple[bytes, bytes], width: int = 1, height: int = 1, color_type: int = 2
) -> bytes:
    header = struct.pack(">IIBBBBB", width, height, 8, color_type, 0, 0, 0)
    return (
        PNG_SIGNATURE
        + chunk(b"IHDR", header)
        + b"".join(chunk(kind, payload) for kind, payload in chunks)
        + chunk(b"IDAT", zlib.compress(b"\0\0\0\0"))
        + chunk(b"IEND", b"")
    )


class EncoderTests(unittest.TestCase):
    def test_encoded_gray_pixels_decode_back_exactly(self) -> None:
        pixels = bytes((x * 31 + y * 7) % 256 for y in range(5) for x in range(7))
        encoded = encode_gray_png(7, 5, pixels)
        self.assertEqual(png_dimensions(encoded, **LIMITS), (7, 5))
        self.assertEqual(read_png_header(encoded), PngHeader(7, 5, 8, 0))
        chunks = list(iter_png_chunks(encoded, max_chunks=8))
        self.assertEqual([item.kind for item in chunks], [b"IHDR", b"IDAT", b"IEND"])
        rows = zlib.decompress(chunks[1].data)
        self.assertEqual(rows, b"".join(b"\0" + pixels[y * 7 : (y + 1) * 7] for y in range(5)))

    def test_the_smallest_encoding_is_the_minimum_png_size(self) -> None:
        self.assertGreaterEqual(len(encode_gray_png(1, 1, b"\0")), MIN_PNG_BYTES)
        self.assertEqual(MIN_PNG_BYTES, len(PNG_SIGNATURE) + 25 + 12 + 12)


class DimensionTests(unittest.TestCase):
    def test_complete_images_report_their_dimensions(self) -> None:
        self.assertEqual(png_dimensions(png(width=3, height=4), **LIMITS), (3, 4))

    def test_incomplete_or_foreign_bytes_are_rejected(self) -> None:
        valid = png()
        for label, data in {
            "empty": b"",
            "short": valid[: MIN_PNG_BYTES - 1],
            "truncated": valid[:-1],
            "signature": b"\x89PNX" + valid[4:],
            "no header chunk": PNG_SIGNATURE + chunk(b"IDAT", b"") * 3,
            "trailing bytes": valid + b"x",
        }.items():
            with self.subTest(label), self.assertRaisesRegex(PreparationError, "complete PNG"):
                png_dimensions(data, **LIMITS)

    def test_the_raster_limits_are_the_callers(self) -> None:
        for width, height in ((0, 1), (1, 0), (65, 1), (1, 65), (33, 32)):
            with (
                self.subTest(width=width, height=height),
                self.assertRaisesRegex(PreparationError, "oversized PNG dimensions"),
            ):
                png_dimensions(png(width=width, height=height), **LIMITS)
        self.assertEqual(png_dimensions(png(width=32, height=32), **LIMITS), (32, 32))
        self.assertEqual(
            png_dimensions(png(width=200, height=1), max_dimension=200, max_pixels=200), (200, 1)
        )


class ChunkWalkTests(unittest.TestCase):
    def test_every_chunk_is_yielded_in_order_with_its_payload(self) -> None:
        data = png((b"tEXt", b"Author\0Alice"), (b"eXIf", b"II*\0"))
        self.assertEqual(
            [(item.kind, len(item.data)) for item in iter_png_chunks(data, max_chunks=16)],
            [(b"IHDR", 13), (b"tEXt", 12), (b"eXIf", 4), (b"IDAT", 12), (b"IEND", 0)],
        )

    def assert_rejected(self, data: bytes, message: str, max_chunks: int = 16) -> None:
        with self.assertRaisesRegex(PreparationError, message):
            list(iter_png_chunks(data, max_chunks=max_chunks))

    def test_structural_defects_are_named(self) -> None:
        valid = png()
        end = chunk(b"IEND", b"")
        self.assert_rejected(b"\x89PNX\r\n\x1a\n", "signature is invalid")
        self.assert_rejected(valid[:-4], "chunk exceeds its file|incomplete or oversized")
        self.assert_rejected(valid[: -len(end)], "termination was not found")
        self.assert_rejected(valid + b"x", "malformed termination")
        self.assert_rejected(
            valid[: -len(end)] + chunk(b"IEND", b"x"), "malformed termination or uninspected"
        )
        corrupted = bytearray(valid)
        corrupted[20] ^= 1
        self.assert_rejected(bytes(corrupted), "checksum is invalid")
        oversized = PNG_SIGNATURE + struct.pack(">I", 1 << 30) + b"tEXt" + bytes(8)
        self.assert_rejected(oversized, "chunk exceeds its file")

    def test_the_chunk_count_is_bounded(self) -> None:
        data = png(*[(b"tEXt", b"a\0b")] * 5)
        self.assert_rejected(data, "incomplete or oversized", max_chunks=7)
        self.assertEqual(len(list(iter_png_chunks(data, max_chunks=8))), 8)

    def test_chunks_before_a_defect_are_still_yielded(self) -> None:
        data = png((b"tEXt", b"Author\0Alice"))
        seen = []
        with self.assertRaises(PreparationError):
            seen.extend(item.kind for item in iter_png_chunks(data + b"x", max_chunks=16))
        self.assertEqual(seen, [b"IHDR", b"tEXt", b"IDAT"])


class HeaderTests(unittest.TestCase):
    def test_the_header_is_read_without_checksums_from_a_prefix(self) -> None:
        data = png(width=10, height=20, color_type=6)
        for length in (29, 33, 60, len(data)):
            self.assertEqual(read_png_header(data[:length]), PngHeader(10, 20, 8, 6), length)
        corrupted = data[:29] + b"\xff\xff\xff\xff"
        self.assertEqual(read_png_header(corrupted).color_type, 6)

    def test_non_png_and_truncated_headers_have_no_fields(self) -> None:
        data = png()
        self.assertIsNone(read_png_header(b""))
        self.assertIsNone(read_png_header(data[:28]))
        self.assertIsNone(read_png_header(b"\x89PNX" + data[4:]))
        self.assertIsNone(read_png_header(PNG_SIGNATURE + chunk(b"IDAT", bytes(13))))
        self.assertIsNone(
            read_png_header(PNG_SIGNATURE + struct.pack(">I", 12) + b"IHDR" + bytes(20))
        )


if __name__ == "__main__":
    unittest.main()
