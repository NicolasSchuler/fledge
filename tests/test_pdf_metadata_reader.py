"""Reading a PDF's metadata with bounded effort: information, XMP and included-PDF records."""

from __future__ import annotations

import contextlib
import random
import unittest
import zlib

from latexprep.errors import PreparationError
from latexprep.pdf_metadata_reader import (
    MetadataOrigin,
    NotPdfError,
    PdfMetadataLimits,
    read_pdf_metadata,
)
from tests.support import object_stream, pdf_bytes, pdf_stream

DOCUMENT = MetadataOrigin.DOCUMENT
INCLUDED = MetadataOrigin.INCLUDED
XMP = (
    b'<?xpacket begin="" id="W5M0MpCehiHzreSzNTczkc9d"?>'
    b'<x:xmpmeta xmlns:x="adobe:ns:meta/"><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">'
    b'<rdf:Description rdf:about="" xmlns:dc="http://purl.org/dc/elements/1.1/" '
    b'xmlns:xmp="http://ns.adobe.com/xap/1.0/" xmp:CreatorTool="Exporter 2.0">'
    b"<dc:creator><rdf:Seq><rdf:li>&#74;ane Q. Secret</rdf:li></rdf:Seq></dc:creator>"
    b"<!-- a comment that names nobody -->"
    b'<dc:title><rdf:Alt><rdf:li xml:lang="x-default">/home/jsecret/fig.svg</rdf:li></rdf:Alt>'
    b'</dc:title></rdf:Description></rdf:RDF></x:xmpmeta><?xpacket end="w"?>'
)


def entries(data: bytes, limits: PdfMetadataLimits | None = None) -> list[tuple[str, str, str]]:
    """Read ``data`` and return each entry as ``(origin, channel, text)``."""
    return [
        (entry.origin.value, entry.channel, entry.text)
        for entry in read_pdf_metadata(data, limits).entries
    ]


def catalog(extra: bytes = b"") -> bytes:
    """Return a catalog dictionary with one empty page tree, plus ``extra`` entries."""
    return b"<< /Type /Catalog /Pages 2 0 R " + extra + b">>"


PAGES = b"<< /Type /Pages /Kids [] /Count 0 >>"


class DocumentMetadataTests(unittest.TestCase):
    """The file's own information dictionary and XMP, in every string and stream form."""

    def test_information_strings_are_decoded_in_each_encoding(self) -> None:
        utf16 = "Zoë Example".encode("utf-16-be").hex().encode()
        info = (
            b"<< /Author (Jane \\(Q.\\) Secret) /Title <FEFF" + utf16 + b">"
            b" /Subject (Caf\\351) /Keywords () /Creator (line\\\ncontinued) >>"
        )
        data = pdf_bytes({1: catalog(), 2: PAGES, 3: info}, b"<< /Root 1 0 R /Info 3 0 R >>")
        self.assertEqual(
            entries(data),
            [
                ("document", "Info Author", "Jane (Q.) Secret"),
                ("document", "Info Title", "Zoë Example"),
                ("document", "Info Subject", "Café"),
                ("document", "Info Creator", "linecontinued"),
            ],
        )

    def test_xmp_properties_name_their_field_in_raw_and_compressed_streams(self) -> None:
        for compress in (False, True):
            with self.subTest(compress=compress):
                metadata = pdf_stream(b"/Type /Metadata /Subtype /XML", XMP, compress=compress)
                data = pdf_bytes({1: catalog(b"/Metadata 3 0 R "), 2: PAGES, 3: metadata})
                self.assertEqual(
                    entries(data),
                    [
                        ("document", "XMP xmp:CreatorTool", "Exporter 2.0"),
                        ("document", "XMP dc:creator", "Jane Q. Secret"),
                        ("document", "XMP dc:title", "/home/jsecret/fig.svg"),
                    ],
                )

    def test_object_streams_and_cross_reference_stream_trailers_are_followed(self) -> None:
        packed = object_stream({4: b"<< /Author (Packed Person) /Producer (Writer) >>"})
        # A PDF 1.5 file has no classic trailer: the cross-reference stream's dictionary
        # names the information dictionary, which lives in an object stream.
        xref = pdf_stream(b"/Type /XRef /Size 6 /Root 1 0 R /Info 4 0 R /W [1 2 1]", b"\0" * 24)
        data = bytearray(pdf_bytes({1: catalog(), 2: PAGES, 3: packed, 5: xref}, trailer=b"<< >>"))
        self.assertEqual(
            entries(bytes(data)),
            [("document", "Info Author", "Packed Person"), ("document", "Info Producer", "Writer")],
        )


class IncludedPdfTests(unittest.TestCase):
    """What pdfTeX and LuaTeX copy from an included PDF into the form XObject that draws it."""

    def test_included_information_is_attributed_to_its_form_xobject(self) -> None:
        # As pdfTeX writes it: the copied information dictionary (7) is packed in an object
        # stream (5), and a name may spell a character as #xx.
        form = pdf_stream(
            b"/Type /XObject /Subtype /Form /PTEX#2EFileName (./figures/plot.pdf) "
            b"/PTEX.PageNumber 1 /PTEX.InfoDict 7 0 R /Metadata 6 0 R "
            b"/PieceInfo << /Illustrator << /Private << "
            b"/Path (C:\\\\Users\\\\ann\\\\a.ai) >> >> >> /BBox [0 0 10 10]",
            b"0 0 10 10 re f",
        )
        data = pdf_bytes(
            {
                1: catalog(),
                2: PAGES,
                3: b"<< /Producer (pdfTeX-1.40.29) /Author () >>",
                4: form,
                5: object_stream({7: b"<< /Author (Figure Author) >>"}),
                6: pdf_stream(b"/Type /Metadata /Subtype /XML", XMP, compress=True),
            },
            b"<< /Root 1 0 R /Info 3 0 R >>",
        )
        result = read_pdf_metadata(data)
        self.assertEqual(result.included_objects, 1)
        self.assertEqual(
            [(entry.origin.value, entry.channel, entry.text) for entry in result.entries],
            [
                ("included", "PTEX.FileName", "./figures/plot.pdf"),
                ("included", "PTEX.InfoDict Author", "Figure Author"),
                ("included", "included XMP xmp:CreatorTool", "Exporter 2.0"),
                ("included", "included XMP dc:creator", "Jane Q. Secret"),
                ("included", "included XMP dc:title", "/home/jsecret/fig.svg"),
                ("included", "included PieceInfo", "C:\\Users\\ann\\a.ai"),
                ("document", "Info Producer", "pdfTeX-1.40.29"),
            ],
        )

    def test_page_application_data_and_unsupported_filters_are_limitations(self) -> None:
        page = (
            b"<< /Type /Page /Parent 2 0 R /PieceInfo << /App << /Name (Ann) /Data 5 0 R >> >> >>"
        )
        data = pdf_bytes(
            {
                1: catalog(b"/Metadata 4 0 R "),
                2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                3: page,
                4: pdf_stream(b"/Type /Metadata /Subtype /XML /Filter /ASCIIHexDecode", b"3C3E>"),
                5: pdf_stream(b"", b"private application bytes"),
            }
        )
        result = read_pdf_metadata(data)
        self.assertEqual(
            [(entry.channel, entry.text) for entry in result.entries], [("PieceInfo", "Ann")]
        )
        self.assertEqual(
            result.limitations,
            ["streams with filters other than FlateDecode", "application data streams (PieceInfo)"],
        )


class HostilePdfTests(unittest.TestCase):
    """Malformed and hostile files end in a bounded refusal or a bounded partial read."""

    def test_files_that_are_not_pdfs_are_refused_as_such(self) -> None:
        for data in (
            b"",
            b"GIF89a",
            b"%PDF-1.7\ncontrolled build result\n",
            b"%PDF-1.4 1 0 obj (x",
        ):
            with self.subTest(data=data[:20]), self.assertRaises(NotPdfError):
                read_pdf_metadata(data)

    def test_encrypted_pdfs_are_refused(self) -> None:
        data = pdf_bytes(
            {1: catalog(), 2: PAGES, 3: b"<< /Filter /Standard >>"},
            b"<< /Root 1 0 R /Encrypt 3 0 R >>",
        )
        with self.assertRaisesRegex(PreparationError, "encrypted"):
            read_pdf_metadata(data)

    def test_decompression_and_extraction_limits_end_the_read(self) -> None:
        bomb = pdf_stream(b"/Type /Metadata", b"<x:a>" + b" " * 200_000 + b"</x:a>", compress=True)
        data = pdf_bytes({1: catalog(), 2: PAGES, 3: bomb})
        with self.assertRaisesRegex(PreparationError, "decompression limit"):
            read_pdf_metadata(data, PdfMetadataLimits(max_stream_bytes=1024))
        with self.assertRaisesRegex(PreparationError, "decompression limit"):
            read_pdf_metadata(data, PdfMetadataLimits(max_decoded_bytes=1024))
        raw = pdf_bytes({1: catalog(), 2: PAGES, 3: pdf_stream(b"/Type /Metadata", b"x" * 5000)})
        with self.assertRaisesRegex(PreparationError, "decompression limit"):
            read_pdf_metadata(raw, PdfMetadataLimits(max_stream_bytes=1024))
        long_title = pdf_bytes(
            {1: catalog(), 2: PAGES, 3: b"<< /Title (" + b"x" * 300 + b") >>"},
            b"<< /Root 1 0 R /Info 3 0 R >>",
        )
        with self.assertRaisesRegex(PreparationError, "extraction limit"):
            read_pdf_metadata(long_title, PdfMetadataLimits(max_text_bytes=100))
        with self.assertRaisesRegex(PreparationError, "more objects"):
            read_pdf_metadata(long_title, PdfMetadataLimits(max_objects=2))
        damaged = pdf_stream(b"/Type /Metadata /Filter /FlateDecode", b"not zlib data")
        cut = pdf_stream(b"/Type /Metadata /Filter /FlateDecode", zlib.compress(XMP)[:40])
        for stream, reason in ((damaged, "damaged"), (cut, "incomplete")):
            with self.subTest(reason=reason), self.assertRaisesRegex(PreparationError, reason):
                read_pdf_metadata(pdf_bytes({1: catalog(), 2: PAGES, 3: stream}))

    def test_nesting_lengths_cycles_and_unterminated_objects_stay_bounded(self) -> None:
        deep = b"[" * 5000 + b"]" * 5000
        wrong_length = (
            b"<< /Type /Metadata /Length 999999 >>\nstream\n<x:a>Kept Value</x:a>\nendstream"
        )
        data = pdf_bytes(
            {
                1: catalog(),
                2: PAGES,
                3: deep,
                4: b"5 0 R",
                5: b"4 0 R",
                6: wrong_length,
                7: b"<< /Author (unterminated",
            },
            b"<< /Root 1 0 R /Info 4 0 R >>",
        )
        result = read_pdf_metadata(data)
        self.assertEqual(
            [(entry.channel, entry.text) for entry in result.entries], [("XMP x:a", "Kept Value")]
        )
        self.assertIn("objects that could not be parsed", result.limitations)

    def test_fake_object_headers_inside_an_unclosed_string_are_read_once(self) -> None:
        # Every failed attempt would otherwise re-read the rest of the file from the next
        # header, which is quadratic in its size.
        noise = b"%PDF-1.7\n1 0 obj (" + b"9 0 obj " * 150_000
        result = read_pdf_metadata(noise)
        self.assertEqual(result.entries, [])
        self.assertIn("objects that could not be parsed", result.limitations)

    def test_mutated_files_only_ever_raise_the_documented_refusal(self) -> None:
        # Random damage to a valid file must end in a result or PreparationError, never in
        # an IndexError, a RecursionError or a hang; the seed keeps the cases repeatable.
        form = pdf_stream(
            b"/Subtype /Form /PTEX.FileName (./a.pdf) /PTEX.InfoDict 6 0 R /Metadata 5 0 R",
            b"0 0 1 1 re f",
        )
        valid = pdf_bytes(
            {
                1: catalog(b"/Metadata 5 0 R "),
                2: PAGES,
                3: form,
                4: object_stream({6: b"<< /Author (Ann) /Title <FEFF0041> >>"}),
                5: pdf_stream(b"/Type /Metadata /Subtype /XML", XMP, compress=True),
            },
            b"<< /Root 1 0 R /Info 6 0 R >>",
        )
        tokens = (b"<<", b">>", b"(", b")", b"[", b"\\", b" 0 R", b"1 0 obj", b"stream\n", b"%")
        generator = random.Random(20261112)
        for case in range(300):
            data = bytearray(valid)
            for _ in range(generator.randint(1, 8)):
                where = generator.randrange(len(data))
                if generator.random() < 0.5:
                    data[where] = generator.randrange(256)
                else:
                    data[where:where] = generator.choice(tokens) * generator.randint(1, 40)
            with self.subTest(case=case), contextlib.suppress(PreparationError):
                read_pdf_metadata(bytes(data))

    def test_object_streams_with_inconsistent_headers_are_skipped(self) -> None:
        bogus = pdf_stream(b"/Type /ObjStm /N 3 /First 999", b"1 0 2 5", compress=True)
        negative = pdf_stream(b"/Type /ObjStm /N -1 /First 0", b"", compress=True)
        data = pdf_bytes(
            {1: catalog(), 2: PAGES, 3: bogus, 4: negative, 5: b"<< /Author (Real Name) >>"},
            b"<< /Root 1 0 R /Info 5 0 R >>",
        )
        self.assertEqual(entries(data), [("document", "Info Author", "Real Name")])


if __name__ == "__main__":
    unittest.main()
