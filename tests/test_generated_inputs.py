"""Generated inputs for the parsers and guards that read untrusted project content.

Each test draws a fixed number of inputs from ``random.Random`` with a fixed seed, so a failure
repeats, and asserts an invariant instead of an expected result: an archive that is damaged is
refused with the documented error and leaves nothing behind, masking keeps the offsets of the
text, an accepted path stays inside the project, a damaged image never raises an
unhandled error, a varied pdfimages inventory gives valid placements or the documented
error, and a report keeps every ordinary word while hiding every secret. The counts are
small enough for a unit run (each test takes well under two seconds); raise them locally
to hunt for a defect.
"""

from __future__ import annotations

import io
import json
import math
import posixpath
import random
import struct
import tempfile
import unittest
import zipfile
import zlib
from pathlib import Path

from latexprep.errors import PreparationError
from latexprep.finding_view import RenderOptions
from latexprep.findings import Finding
from latexprep.html_report import render_redacted_html
from latexprep.image_metadata import check_image_metadata
from latexprep.metadata_privacy import MetadataPrivacyOptions
from latexprep.path_safety import relative_path_problem
from latexprep.pdf_inspection import parse_image_inventory
from latexprep.project import import_project
from latexprep.report import Outcome, Report, redact_report, redacted_json
from latexprep.terminal_report import render_redacted_terminal
from latexprep.tex_lexer import ParseLimitExceeded, mask_literals, source_regions
from latexprep.text_edits import OverlappingEdits, TextEdit, apply_text_edits
from tests.pdf_fakes import CAS_DC_IMAGE_INVENTORY, CAS_DC_PAGES

ZIP_CASES = 2500
LEXER_CASES = 3000
EDIT_CASES = 1500
PATH_CASES = 20000
IMAGE_CASES = 3000
INVENTORY_CASES = 3000
REPORT_CASES = 400


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


# Values that pdfimages prints in some column, and values it should never print there.
_INVENTORY_VALUES = (
    "-",
    "[inline]",
    "0",
    "-1",
    "1e309",
    "nan",
    "inf",
    "-0",
    "9" * 40,
    "1_000",
    "١٢",
    "2033",
    "stencil",
    "smask",
    "jpeg",
    "100%",
    "x" * 200,
)


def varied_inventory(rng: random.Random, original: str) -> str:
    """Return the captured ``pdfimages -list`` output with its rows or columns varied.

    A variation drops, repeats or replaces values of a row, swaps or repeats rows, joins
    two rows, or cuts the text off anywhere, as a damaged or unexpected tool output would.

    Args:
        rng: The source of randomness.
        original: The captured output: a header, its rule and the rows.

    Returns:
        The varied output.
    """
    lines = original.splitlines()
    for _ in range(rng.randint(1, 3)):
        mode = rng.randrange(6)
        index = rng.randrange(2, len(lines))
        values = lines[index].split()
        position = rng.randrange(len(values))
        if mode == 0:
            del values[position]
        elif mode == 1:
            values.insert(position, rng.choice(values))
        elif mode == 2:
            values[position] = rng.choice(_INVENTORY_VALUES)
        elif mode == 3:
            lines.insert(rng.randrange(2, len(lines) + 1), rng.choice(lines[2:]))
            continue
        elif mode == 4:
            if index + 1 < len(lines):
                values.extend(lines.pop(index + 1).split())
        else:
            text = "\n".join(lines)
            return text[: rng.randrange(len(text) + 1)]
        lines[index] = rng.choice((" ", "  ", "\t")).join(values)
    return "\n".join(lines) + "\n"


class ImageInventoryTests(unittest.TestCase):
    """A varied pdfimages inventory gives valid placements or the documented error."""

    def test_varied_inventories_parse_to_valid_placements_or_are_refused(self) -> None:
        rng = random.Random(20261119)
        self.assertEqual(len(parse_image_inventory(CAS_DC_IMAGE_INVENTORY, CAS_DC_PAGES)), 6)
        outcomes = {"parsed": 0, "refused": 0}
        for case in range(INVENTORY_CASES):
            output = varied_inventory(rng, CAS_DC_IMAGE_INVENTORY)
            try:
                placements = parse_image_inventory(output, CAS_DC_PAGES)
            except PreparationError:
                outcomes["refused"] += 1
                continue
            outcomes["parsed"] += 1
            for placement in placements:
                self.assertIn(placement.page, range(1, CAS_DC_PAGES + 1), (case, output))
                self.assertGreaterEqual(min(placement.width_px, placement.height_px), 1)
                self.assertGreaterEqual(placement.number, 0)
                for dpi in (placement.x_dpi, placement.y_dpi):
                    self.assertTrue(math.isfinite(dpi) and dpi >= 0, (case, output))
        self.assertTrue(all(outcomes.values()), outcomes)


# Detail keys whose names end like a credential's; the words below them are ordinary.
SECRET_LIKE_KEYS = (
    "tokens",
    "token",
    "password",
    "passwords",
    "secret",
    "secrets",
    "api_key",
    "apiKey",
    "access-token",
    "client_secret",
    "credentials",
    "signature",
    "Authorization",
    "PrivateKey",
    "passphrase",
)
# Words of manuscripts and of Fledge's own report text, short ones included.
ORDINARY_WORDS = (
    "PDF",
    "GPU",
    "iPhone",
    "LaTeX",
    "Bayesian",
    "compilation",
    "checks",
    "source",
    "bibliography",
    "passed",
    "error",
    "main",
    "title",
    "author",
    "Blocked",
    "the",
    "of",
    "a",
    "x",
    "2020",
)
# Credentials as they are written in project sources; ``{}`` stands for the secret.
SECRET_TEMPLATES = (
    "token={}",
    "password: {}",
    "see https://reader:{}@example.org/data",
    "-----BEGIN PRIVATE KEY-----\n{}\n-----END PRIVATE KEY-----",
)


def detail_value(rng: random.Random, words: list[str], depth: int = 0) -> object:
    """Return an ordinary detail value: a word, a list of words or a record with secret-like keys.

    Every word used is appended to ``words``.

    Args:
        rng: The source of randomness.
        words: The words used so far.
        depth: The nesting depth of the value.

    Returns:
        The value.
    """
    shape = rng.randrange(3 if depth < 2 else 2)
    if shape == 0:
        word = rng.choice(ORDINARY_WORDS)
        words.append(word)
        return word
    if shape == 1:
        chosen = rng.sample(ORDINARY_WORDS, rng.randint(1, 4))
        words.extend(chosen)
        return chosen
    return {
        key: detail_value(rng, words, depth + 1)
        for key in rng.sample(SECRET_LIKE_KEYS, rng.randint(1, 3))
    }


def generated_secret(rng: random.Random) -> str:
    """Return a credential of 12 to 20 letters and digits that is no ordinary word."""
    alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
    body = "".join(rng.choice(alphabet) for _ in range(rng.randint(10, 18)))
    return body + str(rng.randint(10, 99))


class ReportRedactionTests(unittest.TestCase):
    """Redaction keeps ordinary words under secret-like detail keys and hides real secrets."""

    def generated_report(
        self, rng: random.Random, *, with_secrets: bool
    ) -> tuple[Report, list[str], list[str], list[str]]:
        """Return a report of generated findings and what its renderings must show or hide.

        Args:
            rng: The source of randomness.
            with_secrets: Whether some findings quote a credential from a project source.

        Returns:
            The report, the words of its details, the words of its messages and the
            secrets it holds.
        """
        detail_words: list[str] = []
        message_words: list[str] = []
        secrets: list[str] = []
        findings = []
        for _ in range(rng.randint(1, 4)):
            words = rng.sample(ORDINARY_WORDS, rng.randint(1, 5))
            message_words.extend(words)
            details = {
                key: detail_value(rng, detail_words)
                for key in rng.sample(SECRET_LIKE_KEYS, rng.randint(1, 4))
            }
            if with_secrets and rng.random() < 0.7:
                secret = generated_secret(rng)
                secrets.append(secret)
                # Sources reach a report as a quoted excerpt or a log tail.
                details[rng.choice(("excerpt", "log_tail"))] = rng.choice(SECRET_TEMPLATES).format(
                    secret
                )
            findings.append(
                Finding(
                    "source.edit_marker",
                    "Observed " + " ".join(words) + ".",
                    path="main.tex",
                    line=rng.randint(1, 50),
                    details=details,
                )
            )
        report = Report(
            "inspect", outcome=Outcome.PASSED_WITH_ADVISORIES, scope="fixture", findings=findings
        )
        return report, detail_words, message_words, secrets

    def renderings(self, report: Report) -> dict[str, str]:
        """Return the JSON, terminal and HTML renderings of ``report``, redacted once."""
        public = redact_report(report)
        return {
            "json": redacted_json(public),
            "terminal": render_redacted_terminal(public, options=RenderOptions(show_passed=True)),
            "html": render_redacted_html(public),
        }

    def test_ordinary_words_under_secret_like_detail_keys_stay_in_every_format(self) -> None:
        rng = random.Random(20261116)
        for case in range(REPORT_CASES):
            report, detail_words, message_words, _ = self.generated_report(rng, with_secrets=False)
            outputs = self.renderings(report)
            for name, text in outputs.items():
                self.assertNotIn("REDACTED", text, (case, name))
                flowing = " ".join(text.split())
                for word in message_words:
                    self.assertIn(word, flowing, (case, name))
            serialized = json.loads(outputs["json"])
            details = json.dumps([item["details"] for item in serialized["findings"]])
            for word in detail_words:
                self.assertIn(word, details, case)

    def test_secrets_from_sources_are_hidden_and_ordinary_words_kept(self) -> None:
        rng = random.Random(20261117)
        hidden = 0
        for case in range(REPORT_CASES):
            report, detail_words, message_words, secrets = self.generated_report(
                rng, with_secrets=True
            )
            outputs = self.renderings(report)
            for name, text in outputs.items():
                for secret in secrets:
                    self.assertNotIn(secret, text, (case, name))
                flowing = " ".join(text.split())
                for word in message_words:
                    self.assertIn(word, flowing, (case, name))
            details = json.dumps(
                [item["details"] for item in json.loads(outputs["json"])["findings"]]
            )
            for word in detail_words:
                self.assertIn(word, details, case)
            hidden += len(secrets)
        self.assertGreater(hidden, REPORT_CASES // 2)


if __name__ == "__main__":
    unittest.main()
