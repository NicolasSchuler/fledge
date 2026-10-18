"""Controlled metadata bytes test conservative proposals and bounded privacy evidence."""

from __future__ import annotations

import struct
import tempfile
import unittest
import zlib
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from latexprep.finding_view import evidence_excerpt
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
            with patch("latexprep.tex_lexer.MAX_COMMANDS", 0):
                self.assertTrue(has_blockers(plan_metadata_sanitization(root, options).findings))

    def test_conditionals_only_block_the_declarations_they_enclose(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tex").write_text(
                "\\newif\\ifanonymous\\anonymousfalse\n"
                "\\iftrue\\usepackage{graphicx}\\fi\n"
                "\\author{Alice}\n"
                "\\begin{document}Body\\end{document}\n"
            )
            options = MetadataPrivacyOptions(
                edits=(MetadataEdit("main.tex", "author", "Alice", "Anonymous"),)
            )
            plan = plan_metadata_sanitization(root, options)
            self.assertFalse(has_blockers(plan.findings), plan.findings)
            self.assertIn(b"\\author{Anonymous}", plan.contents["main.tex"])

    def test_rejections_name_the_field_that_cannot_be_edited(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = MetadataPrivacyOptions(
                edits=(MetadataEdit("main.tex", "author", "Alice", "Anonymous"),)
            )
            for text, expected in (
                (r"\author{Bob}", "\\author does not hold the expected old value"),
                (r"\author{Alice}\author{Alice}", "\\author was declared 2 times"),
                (r"\title{Study}", "\\author was not found"),
                (r"\iftrue\author{Alice}\fi", "\\author is inside conditional TeX"),
                (r"{\author{Alice}}", "\\author is inside braces or an environment"),
                (r"\author[Short]{Alice}", "\\author has an optional argument"),
                (r"\csname author\endcsname\author{Alice}", "\\csname can change metadata"),
            ):
                with self.subTest(text=text):
                    (root / "main.tex").write_text(text)
                    plan = plan_metadata_sanitization(root, options)
                    (finding,) = plan.findings
                    self.assertEqual(finding.status, "inconclusive")
                    self.assertIn(expected, finding.message)
                    self.assertIn("expected_before", finding.next_step or "")
                    self.assertNotIn("Alice", finding.message)

    def test_hypersetup_keys_without_values_do_not_block_the_plan(self):
        # hyperref defines colorlinks with a default of true and hidelinks as a void option,
        # so \hypersetup{colorlinks, hidelinks} is valid and holds no text to edit.
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "main.tex").write_text(
                "\\hypersetup{colorlinks, pdfauthor={Alice}, hidelinks,\n pdftitle=Old}\n"
            )
            options = MetadataPrivacyOptions(
                edits=(
                    MetadataEdit("main.tex", "hypersetup.pdfauthor", "Alice", "Anonymous"),
                    MetadataEdit("main.tex", "hypersetup.pdftitle", "Old", "New"),
                )
            )
            plan = plan_metadata_sanitization(root, options)
            self.assertFalse(has_blockers(plan.findings), plan.findings)
            self.assertEqual(
                plan.contents["main.tex"].decode(),
                "\\hypersetup{colorlinks, pdfauthor={Anonymous}, hidelinks,\n pdftitle={New}}\n",
            )

    def test_malformed_hypersetup_names_the_key_or_line(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            options = MetadataPrivacyOptions(
                edits=(MetadataEdit("main.tex", "hypersetup.pdfauthor", "Alice", ""),)
            )
            for text, expected in (
                ("\\hypersetup{pdfauthor={Alice} trailing}", "hyperref key pdfauthor"),
                ("\\hypersetup{colorlinks\n 3pt}", "hyperref key colorlinks"),
                ("\\hypersetup{\npdfauthor={Alice},\n =x}", "\\hypersetup on line 3"),
            ):
                with self.subTest(text=text):
                    (root / "main.tex").write_text(text)
                    (finding,) = plan_metadata_sanitization(root, options).findings
                    self.assertEqual(finding.status, "inconclusive")
                    self.assertIn(expected, finding.message)

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
                    severity="info",
                    status="passed",
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
            [Finding("pdf.metadata", "Unavailable", severity="error", status="inconclusive")],
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

    def test_failed_final_recheck_names_each_property_and_edit_without_values(self):
        options = MetadataPrivacyOptions(
            edits=(
                MetadataEdit("main.tex", "hypersetup.pdfauthor", "Alice", ""),
                MetadataEdit("main.tex", "email", "bob@example.org", ""),
            )
        )
        properties = {"Pages": "3", "Author": "Alice", "Subject": "bob@example.org"}
        (result,) = check_sanitized_pdf_metadata(
            [
                Finding(
                    "pdf.metadata",
                    "Properties",
                    severity="info",
                    status="passed",
                    details={"properties": properties},
                )
            ],
            options,
        )
        self.assertEqual(result.status, "failed")
        self.assertNotIn("Checked selected metadata replacements", result.message)
        self.assertIn(
            "Edit 1: PDF property Author does not show the selected replacement", result.message
        )
        self.assertIn("Edit 2: PDF property Subject still contains the value", result.message)
        self.assertIn("rebuild", result.message)
        for secret in ("Alice", "bob@example.org"):
            self.assertNotIn(secret, result.message)
        # The terminal report shows the first few violations from the standard evidence keys.
        self.assertIn("Edit 1: PDF property Author", evidence_excerpt(result))
        clean = {"Pages": "3", "Author": "", "Subject": ""}
        (passed,) = check_sanitized_pdf_metadata(
            [
                Finding(
                    "pdf.metadata",
                    "Properties",
                    severity="info",
                    status="passed",
                    details={"properties": clean},
                )
            ],
            options,
        )
        self.assertEqual(passed.status, "passed")


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
    raw = (value.encode("ascii") if isinstance(value, str) else value) + b"\0"
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


def iptc(dataset, value, record=2):
    return b"\x1c" + bytes([record, dataset]) + struct.pack(">H", len(value)) + value


def photoshop(*datasets):
    payload = b"".join(datasets)
    return (
        b"Photoshop 3.0\0"
        + b"8BIM"
        + struct.pack(">HBB", 0x0404, 0, 0)
        + struct.pack(">I", len(payload))
        + payload
        + b"\0" * (len(payload) % 2)
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
            (jpeg((0xE1, b"http://ns.adobe.com/other/\0data")), ".jpg"),
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

    def test_vendor_blocks_are_limitations_rather_than_an_unusable_scan(self):
        text = b"Alice Researcher"
        camera = jpeg(
            (
                0xE1,
                b"Exif\0\0"
                + b"II\x2a\0"
                + struct.pack("<I", 8)
                + struct.pack("<H", 2)
                + struct.pack("<HHI", 0x013B, 2, len(text) + 1)
                + struct.pack("<I", 38)
                + struct.pack("<HHI", 0x927C, 7, 8)
                + b"\0" * 4
                + struct.pack("<I", 0)
                + text
                + b"\0",
            )
        )
        found = self.scan(camera, ".jpg")
        self.assertEqual(found.status, "failed")
        self.assertFalse(found.details["incomplete"])
        self.assertEqual(
            found.details["limitations"],
            [{"path": "figure.jpg", "skipped": "EXIF MakerNote (vendor binary)"}],
        )
        self.assertNotIn(text.decode(), str(found))
        clean = self.scan(camera, ".jpg", terms=("Bob Author",))
        self.assertEqual(clean.status, "passed")
        self.assertTrue(clean.details["limitations"])
        self.assertFalse(clean.details["incomplete"])
        for data in (
            jpeg((0xED, b"Photoshop unsupported IPTC")),
            jpeg((0xED, photoshop(iptc(0x50, b"\xff\xfe")))),
            jpeg((0xED, photoshop(iptc(0x50, text, record=1)))),
        ):
            with self.subTest(data=data[:30]):
                result = self.scan(data, ".jpg")
                self.assertEqual(result.status, "passed")
                self.assertFalse(result.details["incomplete"])

    def test_iptc_application_records_and_extended_xmp_are_scanned(self):
        text = b"Alice Researcher"
        iim = jpeg((0xED, photoshop(iptc(0x00, b"\0\x04"), iptc(0x50, text))))
        result = self.scan(iim, ".jpg")
        self.assertEqual(result.status, "failed")
        self.assertEqual(
            result.details["matches"],
            [{"path": "figure.jpg", "channel": "JPEG IPTC 2:080", "term_index": 1}],
        )
        self.assertNotIn(text.decode(), str(result))
        extended = jpeg(
            (0xE1, b"http://ns.adobe.com/xmp/extension/\0" + b"a" * 32 + b"<x>" + text + b"</x>")
        )
        self.assertEqual(self.scan(extended, ".jpg").status, "failed")

    def test_non_ascii_names_in_either_encoding_are_scanned_not_rejected(self):
        name = "Müller"
        cases = (
            (".png", png((b"eXIf", exif(name.encode("utf-8"))))),
            (".png", png((b"eXIf", exif(name.encode("latin-1"))))),
            (".jpg", jpeg((0xE1, b"Exif\0\0" + exif(name.encode("utf-8"))))),
            (".jpg", jpeg((0xFE, name.encode("latin-1")))),
            (".jpg", jpeg((0xFE, name.encode("utf-8")))),
            (".jpg", jpeg((0xE1, b"http://ns.adobe.com/xap/1.0/\0<x>" + name.encode("latin-1")))),
            (
                ".jpg",
                jpeg(
                    (
                        0xE1,
                        b"Exif\0\0"
                        + exif(b"ASCII\0\0\0" + name.encode("latin-1"), tag=0x9286, kind=7),
                    )
                ),
            ),
        )
        for suffix, data in cases:
            with self.subTest(suffix=suffix, data=data[-24:]):
                found = self.scan(data, suffix, terms=(name,))
                self.assertEqual(found.status, "failed", found.details["issues"])
                self.assertFalse(found.details["incomplete"])
                self.assertEqual(len(found.details["matches"]), 1)
                clean = self.scan(data, suffix, terms=("Someone Else",))
                self.assertEqual(clean.status, "passed", clean.details["issues"])

    def test_exif_user_comment_character_sets(self):
        def comment(value):
            return jpeg((0xE1, b"Exif\0\0" + exif(value, tag=0x9286, kind=7)))

        text = b"Alice Researcher"
        for charset in (b"\0" * 8, b"ASCII\0\0\0"):
            with self.subTest(charset=charset):
                self.assertEqual(self.scan(comment(charset + text), ".jpg").status, "failed")
                blank = self.scan(comment(charset + b"    "), ".jpg")
                self.assertEqual((blank.status, blank.details["limitations"]), ("passed", []))
        # An empty comment written by a camera has no text and no character set to decode.
        empty = self.scan(comment(b"\0" * 12), ".jpg")
        self.assertEqual((empty.status, empty.details["limitations"]), ("passed", []))
        for charset in (b"JIS\0\0\0\0\0", b"CUSTOM\0\0"):
            with self.subTest(charset=charset):
                result = self.scan(comment(charset + text), ".jpg")
                self.assertEqual(result.status, "passed")
                self.assertFalse(result.details["incomplete"])
                self.assertEqual(
                    result.details["limitations"],
                    [
                        {
                            "path": "figure.jpg",
                            "skipped": "EXIF UserComment in a JIS or unrecognized character set",
                        }
                    ],
                )
        utf16 = comment(b"UNICODE\0" + b"\xff\xfe" + "Alice Researcher".encode("utf-16-le"))
        self.assertEqual(self.scan(utf16, ".jpg").status, "failed")
        broken = comment(b"UNICODE\0" + b"A\0\xff")
        self.assertEqual(self.scan(broken, ".jpg").status, "passed")

    def test_match_output_limit_is_reported_only_when_matches_were_dropped(self):
        def many(count):
            return png(*((b"tEXt", b"Author\0Alice Researcher"),) * count)

        with patch("latexprep.metadata_privacy._MAX_MATCHES", 3):
            for count, limited in ((3, False), (4, True)):
                with self.subTest(count=count):
                    result = self.scan(many(count))
                    self.assertEqual(len(result.details["matches"]), 3)
                    self.assertIs(result.details["match_output_limited"], limited)

    def test_messages_name_the_image_and_cause_of_an_incomplete_scan(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fig").mkdir()
            (root / "fig" / "plot.png").write_bytes(b"not a PNG")
            (root / "photo.jpg").write_bytes(jpeg((0xE1, b"http://ns.adobe.com/other/\0data")))
            (root / "ok.png").write_bytes(png())
            (finding,) = check_image_metadata(
                root, MetadataPrivacyOptions(image_identity_terms=("Alice",))
            )
        self.assertEqual(finding.status, "inconclusive")
        self.assertIn("fig/plot.png: PNG signature is invalid", finding.message)
        self.assertIn("(and 1 more)", finding.message)
        self.assertIn("1 of 3 PNG/JPEG images were scanned", finding.message)
        self.assertNotIn("not a PNG", finding.message)
        self.assertIn("Re-save or re-export", finding.next_step or "")
        # Decoding failures are described by kind, never with file contents.
        text = self.scan(png((b"iTXt", b"Author\0\0\0en\0\0\xff")))
        self.assertIn("unsupported character encoding", text.message)

    def test_found_message_counts_images_and_mentions_unchecked_ones(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.png").write_bytes(png((b"tEXt", b"Author\0Alice")))
            (root / "b.png").write_bytes(
                png((b"tEXt", b"Author\0Alice"), (b"tEXt", b"Title\0Alice"))
            )
            (root / "broken.png").write_bytes(b"x")
            (finding,) = check_image_metadata(
                root, MetadataPrivacyOptions(image_identity_terms=("Alice",))
            )
        self.assertEqual(finding.status, "failed")
        self.assertIn("metadata of 2 images", finding.message)
        self.assertIn("broken.png: PNG signature is invalid", finding.message)

    def test_passed_message_states_what_was_scanned_and_what_was_not(self):
        clean = self.scan(png(), terms=("Bob Author",))
        self.assertEqual(clean.status, "passed")
        self.assertIn("metadata of 1 PNG/JPEG image.", clean.message)
        for unscanned in ("PDF", "EPS", "SVG", "TIFF"):
            self.assertIn(unscanned, clean.message)
        with tempfile.TemporaryDirectory() as directory:
            (finding,) = check_image_metadata(
                Path(directory), MetadataPrivacyOptions(image_identity_terms=("Alice",))
            )
        self.assertEqual(finding.status, "passed")
        self.assertIn("none was found", finding.message)

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
