"""Raster reading and pixel analysis against straightforward per-pixel references."""

from __future__ import annotations

import random
import tempfile
import unittest
from pathlib import Path

from latexprep.errors import PreparationError
from latexprep.raster_pixels import (
    InkMask,
    RasterImage,
    channel_extrema,
    count_changed_pixels,
    read_pnm_raster,
)

PixelBox = tuple[int, int, int, int]


def gray_image(width: int, height: int, values: bytes) -> RasterImage:
    return RasterImage(width, height, 1, values)


def reference_ink(image: RasterImage, background: int, difference: int, box: PixelBox):
    """The ink pixels of a box, as (x, y) pairs, by the per-pixel definition."""
    left, top, right, bottom = box
    return [
        (x, y)
        for y in range(top, bottom)
        for x in range(left, right)
        if abs(image.data[y * image.width + x] - background) >= difference
    ]


def random_box(rng: random.Random, width: int, height: int) -> PixelBox:
    left, right = sorted(rng.randint(0, width) for _ in range(2))
    top, bottom = sorted(rng.randint(0, height) for _ in range(2))
    return left, top, right, bottom


def random_gray(rng: random.Random, width: int, height: int, sparse: bool) -> RasterImage:
    if sparse:
        # Mostly background with a few ink pixels, including whole blank rows and columns.
        data = bytearray([255]) * (width * height)
        for _ in range(rng.randint(0, 6)):
            data[rng.randrange(width * height)] = rng.randrange(256)
    else:
        data = bytearray(rng.randrange(256) for _ in range(width * height))
    return gray_image(width, height, bytes(data))


class InkMaskTests(unittest.TestCase):
    def test_counts_and_bounds_match_the_per_pixel_definition(self) -> None:
        rng = random.Random(20240601)
        for case in range(400):
            width, height = rng.randint(1, 23), rng.randint(1, 19)
            image = random_gray(rng, width, height, sparse=case % 2 == 0)
            background, difference = rng.randrange(256), rng.randint(1, 255)
            mask = InkMask.from_gray(image, background, difference)
            everything = (0, 0, width, height)
            self.assertEqual(
                mask.count(), len(reference_ink(image, background, difference, everything))
            )
            for _ in range(6):
                box = random_box(rng, width, height)
                ink = reference_ink(image, background, difference, box)
                with self.subTest(case=case, box=box):
                    self.assertEqual(mask.count(box), len(ink))
                    expected = (
                        None
                        if not ink
                        else (
                            min(x for x, _ in ink),
                            min(y for _, y in ink),
                            max(x for x, _ in ink) + 1,
                            max(y for _, y in ink) + 1,
                        )
                    )
                    self.assertEqual(mask.bounds(box), expected)

    def test_the_threshold_is_inclusive_at_both_sides_of_the_background(self) -> None:
        image = gray_image(5, 1, bytes([95, 96, 100, 104, 105]))
        mask = InkMask.from_gray(image, background=100, difference=5)
        self.assertEqual(mask.mask, bytes([1, 0, 0, 0, 1]))
        self.assertEqual(mask.bounds((0, 0, 5, 1)), (0, 0, 5, 1))

    def test_boxes_are_clamped_to_the_image_and_empty_boxes_hold_no_ink(self) -> None:
        mask = InkMask.from_gray(gray_image(3, 2, bytes([0] * 6)), background=255, difference=1)
        self.assertEqual(mask.count((-4, -4, 9, 9)), 6)
        self.assertEqual(mask.bounds((-4, -4, 9, 9)), (0, 0, 3, 2))
        for empty in ((2, 0, 2, 2), (0, 1, 3, 1), (2, 0, 1, 2), (5, 5, 9, 9)):
            self.assertEqual(mask.count(empty), 0, empty)
            self.assertIsNone(mask.bounds(empty), empty)

    def test_colour_images_are_rejected(self) -> None:
        with self.assertRaises(ValueError):
            InkMask.from_gray(RasterImage(1, 1, 3, bytes(3)), 255, 5)


class ChannelExtremaTests(unittest.TestCase):
    def test_extrema_match_a_per_pixel_reference(self) -> None:
        rng = random.Random(7)
        for case in range(300):
            width, height = rng.randint(1, 17), rng.randint(1, 13)
            channels = rng.choice((1, 3))
            data = bytes(rng.randrange(256) for _ in range(width * height * channels))
            image = RasterImage(width, height, channels, data)
            box = random_box(rng, width, height)
            left, top, right, bottom = box
            minima, maxima = [255] * channels, [0] * channels
            for y in range(top, bottom):
                for x in range(left, right):
                    for channel in range(channels):
                        value = data[(y * width + x) * channels + channel]
                        minima[channel] = min(minima[channel], value)
                        maxima[channel] = max(maxima[channel], value)
            with self.subTest(case=case, box=box):
                self.assertEqual(channel_extrema(image, box), (tuple(minima), tuple(maxima)))

    def test_an_empty_box_yields_the_neutral_extrema(self) -> None:
        image = RasterImage(2, 2, 3, bytes(range(12)))
        self.assertEqual(channel_extrema(image, (1, 1, 1, 2)), ((255,) * 3, (0,) * 3))


class ChangedPixelTests(unittest.TestCase):
    @staticmethod
    def reference(left: RasterImage, right: RasterImage, tolerance: int) -> int:
        channels = left.channels
        return sum(
            any(
                abs(left.data[offset + channel] - right.data[offset + channel]) > tolerance
                for channel in range(channels)
            )
            for offset in range(0, len(left.data), channels)
        )

    def test_counts_match_the_per_pixel_definition_for_random_pairs(self) -> None:
        rng = random.Random(99)
        for case in range(300):
            width, height = rng.randint(1, 40), rng.randint(1, 9)
            channels = rng.choice((1, 3))
            left_data = bytes(rng.randrange(256) for _ in range(width * height * channels))
            right = bytearray(left_data)
            # Sparse small and large differences, exercising both directions and extremes.
            for _ in range(rng.randint(0, width * height)):
                index = rng.randrange(len(right))
                right[index] = rng.choice(
                    (0, 255, (right[index] + rng.randint(-9, 9)) % 256, rng.randrange(256))
                )
            left_image = RasterImage(width, height, channels, left_data)
            right_image = RasterImage(width, height, channels, bytes(right))
            for tolerance in (0, 1, 2, 5, 17, 128, 254, 255):
                with self.subTest(case=case, tolerance=tolerance):
                    self.assertEqual(
                        count_changed_pixels(left_image, right_image, tolerance),
                        self.reference(left_image, right_image, tolerance),
                    )

    def test_extreme_sample_pairs_agree_with_the_reference_at_every_tolerance(self) -> None:
        # Every pair of 8-bit samples against every tolerance, one pixel per pair.
        pairs = [(a, b) for a in range(0, 256, 5) for b in range(0, 256, 5)]
        pairs += [(0, 255), (255, 0), (0, 0), (255, 255), (1, 0), (0, 1), (254, 255)]
        left = RasterImage(len(pairs), 1, 1, bytes(a for a, _ in pairs))
        right = RasterImage(len(pairs), 1, 1, bytes(b for _, b in pairs))
        for tolerance in range(256):
            self.assertEqual(
                count_changed_pixels(left, right, tolerance),
                self.reference(left, right, tolerance),
                tolerance,
            )

    def test_chunk_boundaries_do_not_change_the_count(self) -> None:
        # More than two chunks, ending inside a partial chunk.
        pixels = 2 * (1 << 16) + 517
        data = bytes((index * 7) % 256 for index in range(3 * pixels))
        changed = bytearray(data)
        for pixel, channel, delta in (
            (0, 0, 9),
            ((1 << 16) - 1, 2, 1),
            (1 << 16, 1, 5),
            ((1 << 16) + 1, 0, 2),
            (2 * (1 << 16) - 1, 1, 200),
            (2 * (1 << 16), 2, 3),
            (pixels - 1, 0, 40),
        ):
            offset = 3 * pixel + channel
            changed[offset] = (changed[offset] + delta) % 256
        left = RasterImage(pixels, 1, 3, data)
        right = RasterImage(pixels, 1, 3, bytes(changed))
        for tolerance in (0, 1, 2, 5, 255):
            with self.subTest(tolerance=tolerance):
                self.assertEqual(
                    count_changed_pixels(left, right, tolerance),
                    self.reference(left, right, tolerance),
                )
        self.assertEqual(count_changed_pixels(left, left, 0), 0)

    def test_mismatched_shapes_and_tolerances_are_rejected(self) -> None:
        small = RasterImage(1, 1, 3, bytes(3))
        with self.assertRaises(ValueError):
            count_changed_pixels(small, RasterImage(3, 1, 1, bytes(3)), 0)
        for tolerance in (-1, 256):
            with self.assertRaises(ValueError):
                count_changed_pixels(small, small, tolerance)


class PnmReaderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def write(self, name: str, data: bytes) -> Path:
        path = self.root / name
        path.write_bytes(data)
        return path

    def test_reads_gray_and_rgb_pages_into_one_image_type(self) -> None:
        gray = read_pnm_raster(
            self.write("a.pgm", b"P5\n2 2\n255\n" + bytes(range(4))), channels=1, max_pixels=4
        )
        self.assertEqual(gray, RasterImage(2, 2, 1, bytes(range(4))))
        rgb = read_pnm_raster(
            self.write("a.ppm", b"P6\n2 1\n255\n" + bytes(range(6))), channels=3, max_pixels=4
        )
        self.assertEqual(rgb, RasterImage(2, 1, 3, bytes(range(6))))

    def test_rejects_unbounded_malformed_truncated_and_oversized_pages(self) -> None:
        cases = {
            "wrong magic": (b"P6\n1 1\n255\n\0", 1),
            "16 bit": (b"P5\n1 1\n65535\n\0\0", 1),
            "truncated": (b"P5\n2 2\n255\n\0\0\0", 1),
            "trailing": (b"P5\n1 1\n255\n\0\0", 1),
            "zero width": (b"P5\n0 1\n255\n", 1),
            "oversized": (b"P5\n3 3\n255\n" + bytes(9), 1),
            "long side": (b"P5\n9 1\n255\n" + bytes(9), 1),
            "no header": (b"\0\0\0\0", 1),
        }
        for label, (data, channels) in cases.items():
            with (
                self.subTest(label),
                self.assertRaises(PreparationError),
            ):
                read_pnm_raster(
                    self.write("bad.pnm", data), channels=channels, max_pixels=8, max_dimension=8
                )

    def test_the_file_size_bound_is_checked_before_reading(self) -> None:
        path = self.write("big.pgm", b"P5\n2 2\n255\n" + bytes(4))
        with self.assertRaises(PreparationError):
            read_pnm_raster(path, channels=1, max_pixels=4, max_file_bytes=10)
        with self.assertRaises(PreparationError):
            read_pnm_raster(self.root / "missing.pgm", channels=1, max_pixels=4)
        link = self.root / "link.pgm"
        link.symlink_to(path)
        with self.assertRaises(PreparationError):
            read_pnm_raster(link, channels=1, max_pixels=4)

    def test_dimension_limit_message_names_the_oversized_dimensions(self) -> None:
        path = self.write("two.ppm", b"P6\n2 2\n255\n" + bytes(12))
        with self.assertRaisesRegex(PreparationError, "oversized raster dimensions"):
            read_pnm_raster(path, channels=3, max_pixels=1)


if __name__ == "__main__":
    unittest.main()
