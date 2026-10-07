"""Controlled metadata bytes test conservative proposals and bounded privacy evidence."""

from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from latexprep.metadata_privacy import (
    MetadataEdit,
    MetadataPrivacyOptions,
    check_image_metadata,
    check_sanitized_pdf_metadata,
    plan_metadata_sanitization,
)
from latexprep.models import Finding, PreparationError, Report, has_blockers
from latexprep.option_config import read_options


class MetadataPrivacyTests(unittest.TestCase):
    def test_options_are_explicit_literal_and_immutable(self):
        for parameters in (
            {"path": "../main.tex"},
            {"path": "template.cls"},
            {"field": "copyright"},
            {"replacement": r"\newcommand{bad}{value}"},
            {"expected_before": "Alice%comment"},
        ):
            with self.subTest(parameters=parameters), self.assertRaises(PreparationError):
                MetadataEdit(
                    **{
                        "path": "main.tex",
                        "field": "author",
                        "expected_before": "Alice",
                        "replacement": "Anonymous",
                        **parameters,
                    }
                )
        edit = MetadataEdit("main.tex", "author", "Alice", "Anonymous")
        with self.assertRaises(FrozenInstanceError):
            edit.replacement = "Bob"
        with self.assertRaises(PreparationError):
            MetadataPrivacyOptions(edits=(edit, edit))
        loaded = read_options(
            MetadataPrivacyOptions,
            {
                "edits": [
                    {
                        "path": "main.tex",
                        "field": "author",
                        "expected_before": "Alice",
                        "replacement": "Anonymous",
                    }
                ]
            },
            "metadata_privacy",
        )
        self.assertEqual(loaded.edits, (edit,))

    def test_literal_replacements_preserve_originals_and_other_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = (
                b"\xef\xbb\xbf\\documentclass{article}\r\n"
                b"% Keep license and rights\r\n\\title{A Study}\r\n\\author{Alice}\r\n"
                b"\\email{alice@example.org}\r\n\\affiliation{Example Institute}\r\n"
                b"\\hypersetup{pdfauthor={Alice}, pdftitle=Old Title, "
                b"pdfsubject={Research}, pdfcopyright={Author-owned rights}}\r\n"
                b"\\begin{document}Body\\end{document}\r\n"
            )
            (root / "main.tex").write_bytes(original)
            options = MetadataPrivacyOptions(
                edits=(
                    MetadataEdit("main.tex", "author", "Alice", "Anonymous"),
                    MetadataEdit("main.tex", "hypersetup.pdfauthor", "Alice", ""),
                    MetadataEdit(
                        "main.tex", "hypersetup.pdftitle", "Old Title", "New, Reviewed Title"
                    ),
                )
            )
            plan = plan_metadata_sanitization(root, options)
            self.assertFalse(has_blockers(plan.findings), plan.findings)
            self.assertEqual((root / "main.tex").read_bytes(), original)
            self.assertEqual(plan.originals["main.tex"], original)
            prepared = plan.contents["main.tex"]
            self.assertTrue(prepared.startswith(b"\xef\xbb\xbf"))
            self.assertIn(b"\\author{Anonymous}\r\n", prepared)
            self.assertIn(b"pdfauthor={}", prepared)
            self.assertIn(b"pdftitle={New, Reviewed Title}", prepared)
            for untouched in (
                b"\\title{A Study}",
                b"alice@example.org",
                b"Example Institute",
                b"Author-owned rights",
                b"% Keep license and rights",
            ):
                self.assertIn(untouched, prepared)
            self.assertTrue(plan.findings[0].details["final_pdf_recheck_required"])
            self.assertIn("-\\author{Alice}", plan.changes[0].diff)
            self.assertIn("+\\author{Anonymous}", plan.changes[0].diff)
            self.assertEqual(
                plan_metadata_sanitization(root, MetadataPrivacyOptions()).contents, {}
            )

    def test_uncertain_or_stale_source_blocks_the_whole_plan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "other.tex").write_text(r"\title{Old}")
            options = MetadataPrivacyOptions(
                edits=(
                    MetadataEdit("main.tex", "author", "Alice", "Anonymous"),
                    MetadataEdit("other.tex", "title", "Old", "New"),
                )
            )
            for text in (
                r"\author{Bob}",
                r"\author{Alice}\author{Alice}",
                r"\iftrue\author{Alice}\fi",
                r"{\author{Alice}}",
                r"\newcommand{\who}{\author{Alice}}",
                r"\author[Short]{Alice}",
                r"\author{\Name}",
                "\\author{Alice%comment\n}",
                r"\endinput\author{Alice}",
            ):
                with self.subTest(text=text):
                    (root / "main.tex").write_text(text)
                    plan = plan_metadata_sanitization(root, options)
                    self.assertTrue(has_blockers(plan.findings))
                    self.assertEqual(plan.contents, {})
                    self.assertEqual(plan.originals, {})
                    self.assertEqual(plan.changes, [])

    def test_literal_examples_and_comments_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            text = (
                "% \\author{Alice}\n\\begin{verbatim}\n\\author{Alice}\n"
                "\\end{verbatim}\n\\author{Alice}\n"
            )
            (root / "main.tex").write_text(text)
            options = MetadataPrivacyOptions(
                edits=(MetadataEdit("main.tex", "author", "Alice", ""),)
            )
            result = plan_metadata_sanitization(root, options)
            self.assertFalse(has_blockers(result.findings))
            self.assertEqual(
                result.contents["main.tex"].decode(),
                text.rsplit("\\author{Alice}", 1)[0] + "\\author{}\n",
            )
            with patch("latexprep.metadata_privacy.MAX_COMMANDS", 1):
                self.assertTrue(has_blockers(plan_metadata_sanitization(root, options).findings))

    def test_final_pdf_recheck_requires_evidence_and_selected_values(self):
        options = MetadataPrivacyOptions(
            edits=(
                MetadataEdit("main.tex", "hypersetup.pdfauthor", "Alice", ""),
                MetadataEdit("main.tex", "hypersetup.pdftitle", "Old title", "New title"),
                MetadataEdit("main.tex", "email", "alice@example.org", ""),
            )
        )

        def inventory(properties):
            return [
                Finding(
                    "pdf.metadata",
                    "Properties",
                    "info",
                    "passed",
                    details={"properties": properties},
                )
            ]

        complete = inventory({"Pages": "3", "Author": "", "Title": "New title"})
        self.assertEqual(check_sanitized_pdf_metadata(complete, options)[0].status, "passed")
        lower = inventory({"pages": "3", "author": "", "title": "New title"})
        self.assertEqual(check_sanitized_pdf_metadata(lower, options)[0].status, "passed")
        for findings in (
            [],
            complete + complete,
            inventory({}),
            [Finding("pdf.metadata", "Unavailable", "error", "inconclusive")],
        ):
            self.assertEqual(
                check_sanitized_pdf_metadata(findings, options)[0].status, "inconclusive"
            )
        for properties in (
            {"Pages": "3", "Author": "Alice", "Title": "New title"},
            {"Pages": "3", "Author": "", "Title": "Wrong title"},
            {"Pages": "3", "Author": "", "Title": "New title", "Subject": "alice@example.org"},
        ):
            result = check_sanitized_pdf_metadata(inventory(properties), options)
            self.assertEqual(result[0].status, "failed")
            public = str(Report("check", findings=result).to_dict())
            self.assertNotIn("alice@example.org", public)
            self.assertNotIn("Alice", public)


def png_chunk(kind, value):
    return (
        struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    )


def png(*chunks):
    return (
        b"\x89PNG\r\n\x1a\n"
        + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
        + b"".join(png_chunk(kind, value) for kind, value in chunks)
        + png_chunk(b"IDAT", zlib.compress(b"\0\0\0\0"))
        + png_chunk(b"IEND", b"")
    )


def exif(value, *, tag=0x013B, kind=2):
    raw = value.encode("ascii") + b"\0"
    stored = raw.ljust(4, b"\0") if len(raw) <= 4 else struct.pack("<I", 26)
    return (
        b"II\x2a\0"
        + struct.pack("<I", 8)
        + struct.pack("<H", 1)
        + struct.pack("<HHI", tag, kind, len(raw))
        + stored
        + struct.pack("<I", 0)
        + (raw if len(raw) > 4 else b"")
    )


def jpeg(*segments):
    return (
        b"\xff\xd8"
        + b"".join(
            b"\xff" + bytes([kind]) + struct.pack(">H", len(value) + 2) + value
            for kind, value in segments
        )
        + b"\xff\xd9"
    )


class ImageMetadataTests(unittest.TestCase):
    def scan(self, data, suffix=".png", terms=("Alice Researcher",)):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ("figure" + suffix)).write_bytes(data)
            return check_image_metadata(root, MetadataPrivacyOptions(image_identity_terms=terms))[0]

    def test_png_text_compressed_text_and_exif_match_without_echoing_values(self):
        value = b"Alice Researcher"
        data = png(
            (b"tEXt", b"Author\0" + value),
            (b"zTXt", b"Comment\0\0" + zlib.compress(value)),
            (b"iTXt", b"Description\0\0\0en\0\0" + value),
            (b"eXIf", exif(value.decode())),
        )
        result = self.scan(data)
        self.assertEqual(result.status, "failed")
        self.assertFalse(result.details["incomplete"])
        self.assertEqual(len(result.details["matches"]), 4)
        self.assertNotIn("Alice Researcher", str(result))
        self.assertTrue(all(item["path"] == "figure.png" for item in result.details["matches"]))
        compressed_unicode = png((b"iTXt", b"Name\0\1\0fr\0\0" + zlib.compress("École".encode())))
        self.assertEqual(self.scan(compressed_unicode, terms=("ÉCOLE",)).status, "failed")

    def test_jpeg_exif_xmp_and_comments_are_scanned(self):
        text = b"Alice Researcher"
        data = jpeg(
            (0xE1, b"Exif\0\0" + exif(text.decode())),
            (0xE1, b"http://ns.adobe.com/xap/1.0/\0<x:xmp>" + text + b"</x:xmp>"),
            (0xFE, text),
        )
        result = self.scan(data, ".jpg")
        self.assertEqual(result.status, "failed")
        self.assertEqual(len(result.details["matches"]), 3)
        self.assertNotIn(text.decode(), str(result))
        self.assertEqual(self.scan(jpeg((0xFE, b"Unrelated credit")), ".jpeg").status, "passed")
        encoded = jpeg((0xE1, b"http://ns.adobe.com/xap/1.0/\0<xmp>Al&#105;ce Researcher</xmp>"))
        self.assertEqual(self.scan(encoded, ".jpg").status, "failed")

    def test_malformed_compressed_and_unsupported_metadata_are_inconclusive(self):
        cyclic_exif = b"II\x2a\0" + struct.pack("<IHI", 8, 0, 8)
        cases = (
            (b"not a PNG", ".png"),
            (png()[:-4], ".png"),
            (png((b"zTXt", b"Author\0\0invalid deflate")), ".png"),
            (png((b"iTXt", b"Author\0\0\0en\0\0\xff")), ".png"),
            (png((b"eXIf", cyclic_exif)), ".png"),
            (jpeg((0xE1, b"Exif\0\0" + exif("Vendor note", tag=0x927C, kind=7))), ".jpg"),
            (jpeg((0xE1, b"http://ns.adobe.com/xmp/extension/\0data")), ".jpg"),
            (jpeg((0xED, b"Photoshop unsupported IPTC")), ".jpg"),
            (b"\xff\xd8\xff\xda", ".jpg"),
            (jpeg() + b"trailer", ".jpg"),
        )
        for data, suffix in cases:
            with self.subTest(data=data[:20], suffix=suffix):
                result = self.scan(data, suffix)
                self.assertEqual(result.status, "inconclusive")
                self.assertTrue(result.details["incomplete"])
        with patch("latexprep.metadata_privacy._MAX_METADATA_BYTES", 64):
            bomb = png((b"zTXt", b"Author\0\0" + zlib.compress(b"A" * 10000)))
            self.assertEqual(self.scan(bomb).status, "inconclusive")
        with patch("latexprep.metadata_privacy._MAX_METADATA_TOTAL", 60):
            repeated = png(
                (b"zTXt", b"a\0\0" + zlib.compress(b"A" * 40)),
                (b"zTXt", b"b\0\0" + zlib.compress(b"B" * 40)),
            )
            self.assertEqual(self.scan(repeated).status, "inconclusive")

    def test_default_no_scan_and_symlinks_never_prove_absence(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            outside = root / "outside"
            outside.mkdir()
            (outside / "figure.png").write_bytes(png())
            project = root / "project"
            project.mkdir()
            (project / "linked.png").symlink_to(outside / "figure.png")
            self.assertEqual(check_image_metadata(project, MetadataPrivacyOptions()), [])
            result = check_image_metadata(
                project, MetadataPrivacyOptions(image_identity_terms=("Alice",))
            )
            self.assertEqual(result[0].status, "inconclusive")
            self.assertIn("no OCR", result[0].details["scope"])


if __name__ == "__main__":
    unittest.main()
