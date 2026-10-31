"""Controlled PDF tool output for tests: fixtures of what the tools print, and runners.

Each runner is a ``FakeCommandRunner`` that answers the Poppler, qpdf and mutool commands of
the PDF checks with fixed text, so that no PDF tool has to be installed. The fixtures are
plain builders; the runners combine them.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from typing import ClassVar

from latexprep.command_runner import CommandResult
from latexprep.pdf_artwork_options import PdfContrastSample
from latexprep.pdf_check_options import PdfRegion
from tests.fakes import FakeCommandRunner, Reply, ToolCall


def geometry(*, pages: int = 1, rotation: int = 0, crop: str = "0 0 612 792") -> str:
    """Return the page geometry that ``pdfinfo -box`` prints for letter pages.

    Args:
        pages: The number of pages.
        rotation: The rotation of every page.
        crop: The crop box of every page.

    Returns:
        The text, four lines per page.
    """
    return "".join(
        f"Page {page} size: 612 x 792 pts\nPage {page} rot: {rotation}\n"
        f"Page {page} MediaBox: 0 0 612 792\nPage {page} CropBox: {crop}\n"
        for page in range(1, pages + 1)
    )


def text_xml(*, size: float = 12, left: float = 72, extra: str = "", text: str = "Body") -> str:
    """Return the XML that ``pdftohtml`` prints for one letter page with one line of text.

    Args:
        size: The font size of the line.
        left: The left position of the line.
        extra: Markup appended to the page after the line.
        text: The text of the line.

    Returns:
        The XML text.
    """
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n<!DOCTYPE pdf2xml SYSTEM "pdf2xml.dtd">'
        '<pdf2xml producer="poppler" version="26.10.0">'
        '<page number="1" position="absolute" top="0" left="0" height="792" width="612">'
        f'<fontspec id="0" size="{size}" family="Times" color="#000000"/>'
        f'<text top="72" left="{left}" width="100" height="12" font="0">{text}</text>'
        f"{extra}</page></pdf2xml>"
    )


def object_json(
    *,
    language: object = "u:en-GB",
    annotations: object = None,
    action: object = None,
    orphan: object = None,
) -> str:
    """Return the qpdf JSON of a one-page PDF.

    Args:
        language: The catalog's ``/Lang`` value, or ``None`` for none.
        annotations: The value of the page's one annotation object, or ``None`` for none.
        action: The value of the catalog's ``/OpenAction`` object, or ``None`` for none.
        orphan: The value of an object that nothing refers to, or ``None`` for none.

    Returns:
        The JSON text.
    """
    catalog: dict[str, object] = {"/Type": "/Catalog", "/Pages": "2 0 R"}
    if language is not None:
        catalog["/Lang"] = language
    if action is not None:
        catalog["/OpenAction"] = "5 0 R"
    page: dict[str, object] = {"/Type": "/Page", "/Parent": "2 0 R"}
    objects: dict[str, object] = {
        "trailer": {"value": {"/Root": "1 0 R"}},
        "obj:1 0 R": {"value": catalog},
        "obj:2 0 R": {"value": {"/Type": "/Pages", "/Kids": ["3 0 R"], "/Count": 1}},
        "obj:3 0 R": {"value": page},
    }
    if annotations is not None:
        page["/Annots"] = ["4 0 R"]
        objects["obj:4 0 R"] = {"value": annotations}
    if action is not None:
        objects["obj:5 0 R"] = {"value": action}
    if orphan is not None:
        objects["obj:6 0 R"] = {"value": orphan}
    return json.dumps(
        {
            "version": 2,
            "pages": [{"object": "3 0 R", "pageposfrom1": 1}],
            "qpdf": [{"jsonversion": 2}, objects],
        }
    )


def structured_pdf_objects() -> dict[str, object]:
    """Return the qpdf JSON of a one-page tagged PDF.

    The structure tree holds a document element with a table (one row with a header cell), a
    figure with alternative text and a link element tied to the page's URI annotation; the parent
    tree maps the page's marked content and the annotation back to their elements.

    Returns:
        The document in the layout of qpdf's JSON version 2.
    """

    def entry(value: dict[str, object]) -> dict[str, object]:
        return {"value": value}

    objects = {
        "trailer": entry({"/Root": "1 0 R"}),
        "obj:1 0 R": entry({"/Type": "/Catalog", "/Pages": "2 0 R", "/StructTreeRoot": "4 0 R"}),
        "obj:2 0 R": entry({"/Type": "/Pages", "/Kids": ["3 0 R"], "/Count": 1}),
        "obj:3 0 R": entry(
            {"/Type": "/Page", "/Parent": "2 0 R", "/StructParents": 0, "/Annots": ["10 0 R"]}
        ),
        "obj:4 0 R": entry({"/Type": "/StructTreeRoot", "/K": ["5 0 R"], "/ParentTree": "11 0 R"}),
        "obj:5 0 R": entry(
            {
                "/Type": "/StructElem",
                "/S": "/Document",
                "/P": "4 0 R",
                "/Pg": "3 0 R",
                "/K": ["6 0 R", "9 0 R", "12 0 R"],
            }
        ),
        "obj:6 0 R": entry(
            {"/Type": "/StructElem", "/S": "/Table", "/P": "5 0 R", "/K": ["7 0 R"]}
        ),
        "obj:7 0 R": entry({"/Type": "/StructElem", "/S": "/TR", "/P": "6 0 R", "/K": ["8 0 R"]}),
        "obj:8 0 R": entry({"/Type": "/StructElem", "/S": "/TH", "/P": "7 0 R", "/K": 0}),
        "obj:9 0 R": entry(
            {
                "/Type": "/StructElem",
                "/S": "/Figure",
                "/P": "5 0 R",
                "/K": 1,
                "/Alt": "u:Measured result chart",
            }
        ),
        "obj:10 0 R": entry(
            {
                "/Type": "/Annot",
                "/Subtype": "/Link",
                "/StructParent": 1,
                "/A": {"/S": "/URI", "/URI": "u:https://example.org"},
            }
        ),
        "obj:11 0 R": entry({"/Nums": [0, ["8 0 R", "9 0 R", "12 0 R"], 1, "12 0 R"]}),
        "obj:12 0 R": entry(
            {
                "/Type": "/StructElem",
                "/S": "/Link",
                "/P": "5 0 R",
                "/K": [2, {"/Type": "/OBJR", "/Pg": "3 0 R", "/Obj": "10 0 R"}],
            }
        ),
    }
    return {
        "version": 2,
        "pages": [{"object": "3 0 R", "pageposfrom1": 1}],
        "qpdf": [{"jsonversion": 2}, objects],
    }


def trace_xml(
    *,
    width: str = "0.7",
    transform: str = "2 0 0 2 0 0",
    color: str = 'colorspace="DeviceGray" color="0"',
) -> str:
    """Return a mutool trace of one letter page with one stroked line.

    Args:
        width: The line width of the stroke.
        transform: The transformation matrix of the stroke.
        color: The colour attributes of the stroke.

    Returns:
        The trace XML.
    """
    return (
        '<document filename="document.pdf"><page number="1" mediabox="0 0 612 792">'
        f'<stroke_path linewidth="{width}" miterlimit="10" linecap="0,0,0" '
        f'linejoin="0" {color} transform="{transform}">'
        '<moveto x="30" y="30"/><lineto x="100" y="30"/></stroke_path>'
        "</page></document>"
    )


def color_trace(
    *,
    text_color: str = "0 0 0",
    other_color: str = "1 1 1",
    space: str = "DeviceRGB",
    alpha: str = "1",
) -> str:
    """Return a mutool trace of one letter page with one filled glyph and one filled path.

    Args:
        text_color: The colour components of the glyph ``A``.
        other_color: The ``DeviceRGB`` colour components of the filled path.
        space: The colour space of the glyph.
        alpha: The alpha of the glyph.

    Returns:
        The trace XML of ``document.pdf``.
    """
    return (
        '<document filename="document.pdf"><page number="1" mediabox="0 0 612 792">'
        f'<fill_text colorspace="{space}" color="{text_color}" alpha="{alpha}" '
        'transform="1 0 0 1 0 0"><span font="Times" wmode="0" trm="12 0 0 12">'
        '<g unicode="A" glyph="A" x="20" y="30"/></span></fill_text>'
        f'<fill_path colorspace="DeviceRGB" color="{other_color}" transform="1 0 0 1 0 0">'
        '<moveto x="20" y="40"/><lineto x="50" y="40"/></fill_path></page></document>'
    )


def gray_raster(*, blank: bool = False, width: int = 306, height: int = 396) -> bytes:
    """Return a greyscale PGM page whose first quarter of pixels is black and the rest white.

    Args:
        blank: Whether the whole page is white instead.
        width: The width of the page in pixels.
        height: The height of the page in pixels.

    Returns:
        The PGM file contents.
    """
    pixels = width * height
    ink = 0 if blank else pixels // 4
    return f"P5\n{width} {height}\n255\n".encode() + b"\0" * ink + b"\xff" * (pixels - ink)


def trace(content: str = "", *, box: str = "0 0 100 100") -> str:
    """Return a mutool trace of one page.

    Args:
        content: The trace markup of the page.
        box: The media box of the page.

    Returns:
        The trace XML.
    """
    return f'<document><page number="1" mediabox="{box}">{content}</page></document>'


def rectangle(
    *, tag: str = "fill_path", left: int = 10, top: int = 10, right: int = 90, bottom: int = 90
) -> str:
    """Return the trace markup of one closed rectangular path.

    Args:
        tag: The element of the path, such as ``fill_path``.
        left: The left edge of the rectangle.
        top: The top edge of the rectangle.
        right: The right edge of the rectangle.
        bottom: The bottom edge of the rectangle.

    Returns:
        The path element.
    """
    return (
        f'<{tag} transform="1 0 0 1 0 0"><moveto x="{left}" y="{top}"/>'
        f'<lineto x="{right}" y="{top}"/><lineto x="{right}" y="{bottom}"/>'
        f'<lineto x="{left}" y="{bottom}"/><closepath/></{tag}>'
    )


def raster(*, box: tuple[int, int, int, int] = (10, 10, 90, 90), background: int = 255) -> bytes:
    """Return a 100 by 100 greyscale PGM page with one rectangle in the inverse grey level.

    Args:
        box: The left, top, right and bottom pixel bounds of the rectangle; the right and
            bottom bounds are excluded.
        background: The grey level of the background; the rectangle has ``255 - background``.

    Returns:
        The PGM file contents.
    """
    pixels = bytearray([background] * 10_000)
    for y in range(box[1], box[3]):
        for x in range(box[0], box[2]):
            pixels[y * 100 + x] = 255 - background
    return b"P5\n100 100\n255\n" + bytes(pixels)


def type3_objects(
    program: bytes = b"500 0 d0 0 0 20 20 re f", *, filtered: bool = False, nested: bool = False
) -> str:
    """Return the qpdf JSON of a page whose Type 3 font has one glyph and an image resource.

    Args:
        program: The content stream of the glyph.
        filtered: Whether the glyph stream declares ``/FlateDecode``; its data stays
            uncompressed either way.
        nested: Whether the font's image resource is a form XObject that draws the image,
            instead of the image itself.

    Returns:
        The JSON text.
    """
    objects: dict[str, object] = {
        "trailer": {"value": {"/Root": "1 0 R"}},
        "obj:1 0 R": {"value": {"/Type": "/Catalog", "/Pages": "2 0 R"}},
        "obj:2 0 R": {"value": {"/Kids": ["3 0 R"]}},
        "obj:3 0 R": {"value": {"/Resources": {"/Font": {"/F1": "4 0 R"}}}},
        "obj:4 0 R": {
            "value": {
                "/Subtype": "/Type3",
                "/CharProcs": {"/A": "5 0 R"},
                "/Resources": {"/XObject": {"/Picture": "6 0 R"}},
            }
        },
        "obj:5 0 R": {
            "stream": {
                "dict": {"/Filter": "/FlateDecode"} if filtered else {},
                "data": base64.b64encode(program).decode(),
            }
        },
        "obj:6 0 R": {"value": {"/Subtype": "/Image"}},
    }
    if nested:
        objects["obj:6 0 R"] = {
            "stream": {
                "dict": {"/Subtype": "/Form", "/Resources": {"/XObject": {"/Picture": "7 0 R"}}},
                "data": base64.b64encode(b"/Picture Do").decode(),
            }
        }
        objects["obj:7 0 R"] = {"value": {"/Subtype": "/Image"}}
    return json.dumps(
        {"version": 2, "pages": [{"object": "3 0 R"}], "qpdf": [{"jsonversion": 2}, objects]}
    )


def rgb_raster(
    *,
    foreground: tuple[int, int, int] = (0, 0, 0),
    background: tuple[int, int, int] = (255, 255, 255),
    alternate: tuple[int, int, int] | None = None,
) -> bytes:
    """Return a 100 by 100 PPM page with a 10 by 10 pixel square at (10, 10).

    Args:
        foreground: The colour of the square.
        background: The colour of the rest of the page.
        alternate: The colour of the right half of the square, or ``None`` for one colour.

    Returns:
        The PPM file contents.
    """
    pixels = bytearray(bytes(background) * 10_000)
    for y in range(10, 20):
        for x in range(10, 20):
            value = alternate if alternate is not None and x >= 15 else foreground
            offset = (y * 100 + x) * 3
            pixels[offset : offset + 3] = bytes(value)
    return b"P6\n100 100\n255\n" + pixels


def contrast_sample(*, minimum: float = 4.5, tolerance: int = 0) -> PdfContrastSample:
    """Return a contrast sample of two regions on page 1, around the square of ``rgb_raster``.

    Args:
        minimum: The ``minimum_ratio`` of the sample.
        tolerance: The ``max_channel_spread`` of the sample.

    Returns:
        The sample ``Label/background``, which compares the region from (10, 10) to
        (20, 20) with the region from (80, 80) to (90, 90).
    """
    return PdfContrastSample(
        "Label/background",
        PdfRegion("Foreground", 1, 10, 10, 20, 20),
        PdfRegion("Background", 1, 80, 80, 90, 90),
        minimum,
        tolerance,
    )


class PdfToolRunner(FakeCommandRunner):
    """A runner that answers the commands of the PDF checks from one attribute per output.

    An attribute can be changed after construction.

    Args:
        **values: Attribute values that replace the defaults of the subclass.

    Attributes:
        defaults: The initial value of each attribute, set by the subclass.
        info: What ``pdfinfo`` prints.
        geometry: What ``pdfinfo`` prints with ``-box``.
        xml: What ``pdftohtml`` prints.
        objects: The qpdf JSON.
        trace: The mutool trace.
        text: The extracted text.
        fonts: The font table.
        raster: The page rendered by ``pdftoppm`` with ``-gray``.
        rgb: The page rendered by ``pdftoppm`` without ``-gray``.
        stderr: What every tool prints on standard error.
    """

    defaults: ClassVar[dict[str, object]] = {}

    def __init__(self, **values: object) -> None:
        super().__init__()
        for name, value in {**self.defaults, **values}.items():
            setattr(self, name, value)

    def version_output(self, call: ToolCall) -> str:
        """Return the version output, insisting that qpdf is probed with ``--version``.

        Returns:
            What a version probe of the call's tool prints.

        Raises:
            AssertionError: A qpdf probe ends in a flag other than ``--version``.
        """
        if call.tool == "qpdf" and call.argv[-1] != "--version":
            raise AssertionError("qpdf requires --version")
        return super().version_output(call)

    def respond(self, call: ToolCall) -> Reply:
        """Return the output attribute of the call's tool, with ``stderr``.

        ``pdfinfo`` prints ``geometry`` with ``-box`` and ``info`` otherwise, and
        ``pdftoppm`` writes ``raster`` to ``page.pgm`` with ``-gray`` and ``rgb`` to
        ``page.ppm`` otherwise. Any other tool without an attribute prints only ``stderr``.
        """
        argv = call.argv
        if call.tool == "pdfinfo":
            return Reply(self.geometry if "-box" in argv else self.info, self.stderr)
        if call.tool == "pdftoppm":
            file = ("page.pgm", self.raster) if "-gray" in argv else ("page.ppm", self.rgb)
            return Reply(stderr=self.stderr, files=dict([file]))
        outputs = {
            "pdftohtml": self.xml,
            "qpdf": self.objects,
            "mutool": self.trace,
            "pdftotext": self.text,
            "pdffonts": self.fonts,
        }
        return Reply(outputs.get(call.tool, ""), self.stderr)


_FONT_TABLE = "name type encoding emb sub uni object ID\n---\nFont Type 1 Builtin yes yes no 3 0\n"


class DetailRunner(PdfToolRunner):
    """A paper-like PDF: letter pages with text, fonts and metadata, for the detail checks."""

    defaults: ClassVar[dict[str, object]] = {
        "info": "Pages: 1\nTitle: Paper\nAuthor: Ada Lovelace\nTagged: yes\n",
        "geometry": geometry(),
        "xml": text_xml(),
        "objects": object_json(),
        "trace": trace_xml(),
        "text": "Body text\n\f",
        "raster": gray_raster(),
        "rgb": b"",
        "fonts": _FONT_TABLE,
        "stderr": "",
    }


class ArtworkRunner(PdfToolRunner):
    """A square page with one filled rectangle, for the artwork and contrast checks."""

    defaults: ClassVar[dict[str, object]] = {
        "info": "Pages: 1\n",
        "geometry": (
            "Page 1 size: 100 x 100 pts\nPage 1 rot: 0\n"
            "Page 1 MediaBox: 0 0 100 100\nPage 1 CropBox: 0 0 100 100\n"
        ),
        "xml": "",
        "objects": type3_objects(),
        "trace": trace(rectangle()),
        "text": "Pages: 1\n",
        "raster": raster(),
        "rgb": rgb_raster(),
        "fonts": _FONT_TABLE,
        "stderr": "",
    }


class FakePdfRunner(FakeCommandRunner):
    """Poppler output for two PDFs, the second of which (containing "right") may differ.

    A PDF is the second one when a read-only input of the command contains ``right``.

    Attributes:
        text_changed: Whether the text of the second PDF differs from the first.
        raster_changed: Whether the rendering of the second PDF differs from the first.
        pages_right: The page count of the second PDF.
        missing: A tool that raises ``PreparationError``, as in :class:`FakeCommandRunner`.
    """

    def __init__(
        self,
        *,
        text_changed: bool = False,
        raster_changed: bool = False,
        pages_right: int = 1,
        missing: str | None = None,
    ) -> None:
        super().__init__()
        self.text_changed = text_changed
        self.raster_changed = raster_changed
        self.pages_right = pages_right
        self.missing = missing

    def respond(self, call: ToolCall) -> Reply:
        """Return what a Poppler tool prints for the first or the second PDF.

        ``pdfinfo``, ``pdftotext``, ``pdftoppm``, ``pdffonts``, ``pdfdetach`` and
        ``pdfimages`` answer; any other tool prints nothing.
        """
        tool = call.tool
        right = any(b"right" in path.read_bytes() for path in call.readonly_inputs)
        pages = self.pages_right if right else 1
        if tool == "pdfinfo":
            output = f"Pages: {pages}\nEncrypted: no\nForm: none\nJavaScript: no\n"
            if "-box" in call.argv:
                output += "".join(
                    f"Page {page} size: 612 x 792 pts\n" for page in range(1, pages + 1)
                )
            return Reply(output)
        if tool == "pdftotext":
            return Reply(
                (("Changed\n" if right and self.text_changed else "Text\n") + "\f") * pages
            )
        if tool == "pdftoppm":
            pixel = b"\0\0\0" if right and self.raster_changed else b"\xff\xff\xff"
            return Reply(files={call.argv[-1] + ".ppm": b"P6\n1 1\n255\n" + pixel})
        if tool == "pdffonts":
            return Reply(
                "name type encoding emb sub uni object ID\n---\n"
                "Test Type 1 Builtin yes yes no 3 0\n"
            )
        if tool == "pdfdetach":
            return Reply("0 embedded files\n")
        if tool == "pdfimages":
            return Reply(
                "page num type width height color comp bpc enc interp object ID "
                "x-ppi y-ppi size ratio\n---\n"
            )
        return Reply()


class ConfigurablePdfRunner(FakePdfRunner):
    """Poppler output for one PDF whose page count and tool outputs a test chooses.

    Every command other than a ``-v`` probe succeeds with the output of ``outputs``, else a
    default for ``pages`` letter pages (``pdfinfo``, ``pdfinfo -box`` and ``pdftotext``),
    else the output of :class:`FakePdfRunner`.

    Args:
        missing: A missing tool, passed to :class:`FakePdfRunner`.

    Attributes:
        pages: The page count of the default outputs.
        outputs: Outputs by tool name; the key ``geometry`` stands for ``pdfinfo -box``.
    """

    def __init__(
        self, *, pages: int = 1, outputs: dict[str, str] | None = None, missing: str | None = None
    ) -> None:
        super().__init__(missing=missing)
        self.pages = pages
        self.outputs = outputs or {}

    async def run(
        self,
        argv: list[str],
        cwd: Path,
        workspace: Path,
        *,
        readonly_inputs: tuple[Path, ...] = (),
        memory_mb: int | None = None,
    ) -> CommandResult:
        """Answer the command with the chosen output, the default or the base output.

        The arguments are those of :meth:`CommandRunner.run`. A ``-v`` probe gets the base
        result; any other command succeeds with the output described in the class docstring.
        """
        result = await super().run(
            argv, cwd, workspace, readonly_inputs=readonly_inputs, memory_mb=memory_mb
        )
        if argv[-1] == "-v":
            return result
        key = "geometry" if argv[0] == "pdfinfo" and "-box" in argv else argv[0]
        defaults = {
            "pdfinfo": f"Pages: {self.pages}\nEncrypted: no\nForm: none\nJavaScript: no\n",
            "geometry": "".join(
                f"Page {page} size: 612 x 792 pts\n" for page in range(1, self.pages + 1)
            ),
            "pdftotext": "Text\n\f" * self.pages,
        }
        output = self.outputs.get(key, defaults.get(key, result.stdout))
        return CommandResult(0, output, "", resolved_command=argv)
