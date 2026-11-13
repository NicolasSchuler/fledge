"""Generated inputs for the parsers and guards that read untrusted project content.

Each test draws a fixed number of inputs from ``random.Random`` with a fixed seed, so a failure
repeats, and asserts an invariant instead of an expected result: an archive that is damaged is
refused with the documented error and leaves nothing behind, masking keeps the offsets of the
text, an accepted path stays inside the project, and a damaged image never raises an
unhandled error. The counts are small enough for a unit run (each test takes well under two
seconds); raise them locally to hunt for a defect.
"""

from __future__ import annotations

import io
import posixpath
import random
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from latexprep.errors import PreparationError
from latexprep.image_metadata import check_image_metadata
from latexprep.metadata_privacy import MetadataPrivacyOptions
from latexprep.path_safety import relative_path_problem
from latexprep.project import import_project
from latexprep.tex_lexer import ParseLimitExceeded, mask_literals, source_regions
from latexprep.text_edits import OverlappingEdits, TextEdit, apply_text_edits

ZIP_CASES = 2500
LEXER_CASES = 3000
EDIT_CASES = 1500
PATH_CASES = 20000
IMAGE_CASES = 3000


def valid_archive(*, zip64: bool) -> bytes:
    """Return a small ZIP of a paper project, with the ZIP64 extension on its first entry if asked.

    Args:
        zip64: Whether the first entry is written with ZIP64 fields.

    Returns:
        The bytes of the archive.
    """
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_STORED if zip64 else zipfile.ZIP_DEFLATED) as z:
        with z.open("paper/main.tex", "w", force_zip64=zip64) as stream:
            stream.write(b"\\documentclass{article}\\begin{document}x\\end{document}\n" * 5)
        z.writestr("paper/sections/a.tex", "hello " * 100)
        z.writestr(zipfile.ZipInfo("paper/figures/"), b"")
        z.writestr("paper/refs.bib", "@article{a,title={t}}\n")
    return buffer.getvalue()


def mutated(rng: random.Random, original: bytes) -> bytes:
    """Return ``original`` with a few bytes changed, deleted, inserted or cut off.

    Args:
        rng: The source of randomness.
        original: The bytes to damage.

    Returns:
        The damaged copy.
    """
    data = bytearray(original)
    mode = rng.randrange(4)
    if mode == 0:
        for _ in range(rng.randint(1, 4)):
            data[rng.randrange(len(data))] = rng.randrange(256)
    elif mode == 1:
        del data[rng.randrange(len(data)) :]
    elif mode == 2:
        # The end record and the central directory sit at the end of the file.
        for _ in range(rng.randint(1, 3)):
            data[len(data) - 1 - rng.randrange(min(len(data), 200))] = rng.randrange(256)
    else:
        position = rng.randrange(len(data))
        data[position:position] = bytes(rng.randrange(256) for _ in range(rng.randint(1, 8)))
    return bytes(data)


class ArchiveImportTests(unittest.TestCase):
    """A damaged ZIP is refused with ``PreparationError`` and leaves no directory behind."""

    def test_damaged_archives_are_refused_cleanly_or_imported_inside_the_destination(self) -> None:
        rng = random.Random(20261113)
        originals = [valid_archive(zip64=False), valid_archive(zip64=True)]
        outcomes = {"imported": 0, "refused": 0}
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            for case in range(ZIP_CASES):
                archive = base / f"input{case}.zip"
                archive.write_bytes(mutated(rng, rng.choice(originals)))
                destination = base / f"import{case}"
                try:
                    import_project(archive, destination)
                except PreparationError:
                    outcomes["refused"] += 1
                    self.assertFalse(destination.exists(), f"case {case} left its destination")
                else:
                    outcomes["imported"] += 1
                    root = destination.resolve()
                    for path in destination.rglob("*"):
                        self.assertTrue(path.resolve().is_relative_to(root), f"case {case}")
        self.assertGreater(outcomes["refused"], ZIP_CASES // 2)
        self.assertGreater(outcomes["imported"], 0)


class LexerTests(unittest.TestCase):
    """Masking keeps the offsets and line breaks of a text, and masking twice changes nothing."""

    PIECES = (
        "a",
        "b ",
        " ",
        "\n",
        "\n\n",
        "\r\n",
        "\r",
        "%",
        "\\\\",
        "\\%",
        "{",
        "}",
        "[",
        "]",
        "$",
        "\\$",
        "\\foo",
        "\\input{x}",
        "\\cite{k1,k2}",
        "\\verb|x%y|",
        "\\verb",
        "|",
        "\\begin{verbatim}",
        "\\end{verbatim}",
        "\\url{a%b}",
        "\\lstinline|z|",
        "\\begin{lstlisting}",
        "\\end{lstlisting}",
        "é",
        "\x00",
    )

    def test_masked_text_keeps_its_length_and_line_breaks_and_masking_is_idempotent(self) -> None:
        rng = random.Random(20261114)
        for case in range(LEXER_CASES):
            text = "".join(rng.choice(self.PIECES) for _ in range(rng.randint(0, 25)))
            try:
                masked = mask_literals(text)
                regions = source_regions(text).regions
            except ParseLimitExceeded:
                continue
            with self.subTest(case=case):
                self.assertEqual(len(masked), len(text), repr(text))
                breaks = [index for index, char in enumerate(text) if char in "\r\n"]
                self.assertEqual(
                    breaks, [index for index, char in enumerate(masked) if char in "\r\n"]
                )
                self.assertEqual(mask_literals(masked), masked, repr(text))
                previous_end = 0
                for region in regions:
                    self.assertGreaterEqual(region.start, previous_end, repr(text))
                    self.assertLessEqual(region.end, len(text))
                    previous_end = max(previous_end, region.end)


class TextEditTests(unittest.TestCase):
    """Edits that do not overlap are applied in any order; overlapping ones are refused."""

    @staticmethod
    def disjoint_edits(rng: random.Random, length: int) -> list[TextEdit]:
        """Draw edits on distinct, non-overlapping spans of a text of ``length`` characters."""
        cuts = sorted(rng.sample(range(length + 1), min(length + 1, rng.randint(0, 8))))
        edits = []
        for start, end in zip(cuts[::2], cuts[1::2], strict=False):
            edits.append(
                TextEdit(start, end, "".join(rng.choice("XYZ") for _ in range(rng.randint(0, 3))))
            )
        return edits

    def test_no_edits_leave_the_text_and_disjoint_edits_do_not_depend_on_their_order(self) -> None:
        rng = random.Random(20261115)
        for _ in range(EDIT_CASES):
            text = "".join(rng.choice("abc \n") for _ in range(rng.randint(0, 30)))
            self.assertEqual(apply_text_edits(text, []), text)
            edits = self.disjoint_edits(rng, len(text))
            expected = text
            for start, end, replacement in sorted(edits, reverse=True):
                expected = expected[:start] + replacement + expected[end:]
            shuffled = list(edits)
            rng.shuffle(shuffled)
            self.assertEqual(apply_text_edits(text, shuffled), expected)

    def test_overlapping_and_out_of_range_edits_are_refused_in_any_order(self) -> None:
        rng = random.Random(20261116)
        for _ in range(EDIT_CASES):
            length = rng.randint(4, 30)
            text = "x" * length
            first_start = rng.randint(0, length - 3)
            first_end = rng.randint(first_start + 2, length)
            overlapping = TextEdit(rng.randint(first_start + 1, first_end - 1), length, "Y")
            edits = [TextEdit(first_start, first_end, "X"), overlapping]
            rng.shuffle(edits)
            with self.assertRaises(OverlappingEdits):
                apply_text_edits(text, edits)
            with self.assertRaises(OverlappingEdits):
                apply_text_edits(text, [TextEdit(0, length + 1, "Y")])


class PathSafetyTests(unittest.TestCase):
    """A path that the check accepts stays inside the project it is joined to."""

    PIECES = (
        "a",
        "b",
        ".",
        "..",
        "/",
        "//",
        "\\",
        ":",
        "\x00",
        " ",
        "~",
        "%2e",
        "．",
        "․",
        "／",
        "C:",
        "CON",
        "x.tex",
    )

    def test_an_accepted_path_never_leaves_the_project_root(self) -> None:
        rng = random.Random(20261117)
        root = "/work/project"
        accepted = 0
        for _ in range(PATH_CASES):
            value = "".join(rng.choice(self.PIECES) for _ in range(rng.randint(1, 8)))
            for normalized in (True, False):
                if relative_path_problem(value, normalized=normalized) is not None:
                    continue
                accepted += 1
                joined = posixpath.normpath(posixpath.join(root, value))
                self.assertTrue(joined == root or joined.startswith(root + "/"), repr(value))
                self.assertNotIn("\x00", value)
        self.assertGreater(accepted, PATH_CASES // 10)


def chunk(kind: bytes, data: bytes) -> bytes:
    """Return a PNG chunk with its length and checksum."""
    payload = kind + data
    return struct.pack(">I", len(data)) + payload + struct.pack(">I", zlib.crc32(payload))


def exif_block(*, little_endian: bool) -> bytes:
    """Return a TIFF block with an image description, an artist and a nested user comment."""
    fmt = "<" if little_endian else ">"
    description, artist = b"A description for Jane Doe\0", b"Jane Doe\0"
    comment = b"ASCII\0\0\0comment by Jane"
    header = (b"II*\0" if little_endian else b"MM\0*") + struct.pack(fmt + "I", 8)
    first_size = 2 + 12 * 3 + 4
    description_offset = 8 + first_size
    artist_offset = description_offset + len(description)
    exif_offset = artist_offset + len(artist)
    entries = struct.pack(fmt + "HHII", 0x010E, 2, len(description), description_offset)
    entries += struct.pack(fmt + "HHII", 0x013B, 2, len(artist), artist_offset)
    entries += struct.pack(fmt + "HHII", 0x8769, 4, 1, exif_offset)
    first = struct.pack(fmt + "H", 3) + entries + struct.pack(fmt + "I", 0)
    comment_offset = exif_offset + 2 + 12 + 4
    nested = struct.pack(fmt + "H", 1)
    nested += struct.pack(fmt + "HHII", 0x9286, 7, len(comment), comment_offset)
    nested += struct.pack(fmt + "I", 0)
    return header + first + description + artist + nested + comment


def jpeg_segment(marker: int, payload: bytes) -> bytes:
    """Return a JPEG segment of ``marker`` with its length."""
    return bytes([0xFF, marker]) + struct.pack(">H", len(payload) + 2) + payload


def sample_png() -> bytes:
    """Return a PNG with text chunks, a compressed text chunk and an EXIF chunk."""
    header = struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"tEXt", b"Author\0Jane Doe")
        + chunk(b"zTXt", b"Comment\0\0" + zlib.compress(b"secret Jane"))
        + chunk(b"iTXt", b"Description\0\0\0en\0Desc\0" + "text für Jane".encode())
        + chunk(b"eXIf", exif_block(little_endian=True))
        + chunk(b"IDAT", zlib.compress(b"\0\0\0\0"))
        + chunk(b"IEND", b"")
    )


def sample_jpeg(*, little_endian: bool) -> bytes:
    """Return a JPEG with EXIF, IPTC inside a Photoshop block, XMP and a comment."""
    iptc = b"".join(
        b"\x1c\x02" + bytes([dataset]) + struct.pack(">H", len(value)) + value
        for dataset, value in ((5, b"Title"), (80, b"Jane Doe"), (120, b"Caption text"))
    )
    photoshop = b"Photoshop 3.0\0" + b"8BIM" + struct.pack(">H", 0x0404) + b"\0\0"
    photoshop += struct.pack(">I", len(iptc)) + iptc + (b"\0" if len(iptc) % 2 else b"")
    xmp = b"http://ns.adobe.com/xap/1.0/\0<x:xmpmeta><rdf:Description dc:creator='Jane Doe'/>"
    return (
        b"\xff\xd8"
        + jpeg_segment(0xE1, b"Exif\0\0" + exif_block(little_endian=little_endian))
        + jpeg_segment(0xED, photoshop)
        + jpeg_segment(0xE1, xmp)
        + jpeg_segment(0xFE, b"a comment by Jane")
        + jpeg_segment(0xC0, b"\x08\0\x01\0\x01\x01\x01\x11\0")
        + jpeg_segment(0xDA, b"\x01\x01\0\0?\0")
        + b"\0\0\xff\xd9"
    )


class ImageMetadataTests(unittest.TestCase):
    """A damaged image gives a finding, never an unhandled error."""

    def test_the_scan_reports_damaged_images_instead_of_failing(self) -> None:
        rng = random.Random(20261118)
        options = MetadataPrivacyOptions(image_identity_terms=("Jane Doe",))
        originals = [
            ("png", sample_png()),
            ("jpg", sample_jpeg(little_endian=True)),
            ("jpg", sample_jpeg(little_endian=False)),
        ]
        statuses: dict[str, int] = {}
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for suffix, original in originals:
                (root / f"intact.{suffix}").write_bytes(original)
                (finding,) = check_image_metadata(root, options)
                self.assertEqual(finding.status, "failed", "the intact sample names its term")
                (root / f"intact.{suffix}").unlink()
            for case in range(IMAGE_CASES):
                suffix, original = rng.choice(originals)
                image = root / f"image.{suffix}"
                image.write_bytes(mutated(rng, original))
                (finding,) = check_image_metadata(root, options)
                self.assertIn(finding.status, {"passed", "failed", "inconclusive"}, case)
                statuses[finding.status] = statuses.get(finding.status, 0) + 1
        self.assertGreater(len(statuses), 1, statuses)


if __name__ == "__main__":
    unittest.main()
