"""Parsers for the text and XML that Poppler and MuPDF print about a PDF.

Every parser validates what it reads and raises ``PreparationError`` rather than
guessing, so a tool whose output format changes leaves a measurement inconclusive
instead of wrong. This module reads output; it does not run tools (``pdf_tools``) or
interpret qpdf JSON (``pdf_qpdf_graph``).

Page boxes use the bottom-left PDF frame as reported by Poppler; regions and text
boxes elsewhere use the top-left frame of an unrotated, uncropped page.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Sequence
from dataclasses import dataclass
from typing import NamedTuple

from .errors import PreparationError
from .pdf_geometry import finite_number

MAX_INSPECTION_PAGES = 5000
# Rendering costs one process and raster per page, so it keeps a smaller cap.
MAX_RENDERED_PAGES = 300
# Elements of one tool XML document, or objects of one qpdf JSON document, that are parsed.
MAX_PARSED_NODES = 50_000
# PDF 1.7 Annex C: readers need not support pages beyond 14,400 units on a side. Larger
# reported sizes are treated as damaged geometry, which also keeps raster arithmetic finite.
MAX_PAGE_DIMENSION_PT = 14_400
METADATA_FIELDS = frozenset({"Title", "Author", "Subject", "Keywords", "Creator", "Producer"})
# MuPDF trace operations that paint with a color space.
COLOR_OPERATIONS = frozenset(
    {
        "fill_path",
        "stroke_path",
        "fill_text",
        "stroke_text",
        "fill_image",
        "fill_image_mask",
        "fill_shade",
    }
)


def pdfinfo_fields(output: str) -> dict[str, str]:
    """The ``key: value`` lines of pdfinfo output, split at each line's first colon."""
    return {
        key.strip(): value.strip()
        for key, value in (line.split(":", 1) for line in output.splitlines() if ":" in line)
    }


def page_count(fields: dict[str, str], maximum: int = MAX_INSPECTION_PAGES) -> int:
    """The page count of pdfinfo ``fields``, which must lie between 1 and ``maximum``."""
    try:
        pages = int(fields["Pages"])
    except (KeyError, ValueError) as error:
        raise PreparationError("pdfinfo did not return a valid page count") from error
    if pages < 1 or pages > maximum:
        raise PreparationError(f"PDF has {pages} pages; this implementation supports 1–{maximum}")
    return pages


def pdfinfo_properties(output: str) -> dict[str, str]:
    """pdfinfo fields whose compared keys and page count are unambiguous."""
    fields = pdfinfo_fields(output)
    keys = [line.split(":", 1)[0].strip() for line in output.splitlines() if ":" in line]
    if any(keys.count(key) != 1 for key in (*METADATA_FIELDS, "Pages", "Tagged") if key in keys):
        raise PreparationError("Ambiguous duplicate pdfinfo properties prevent reliable comparison")
    page_count(fields)
    return fields


def require_rendered_pages(pages: int, subject: str = "Rendered comparison") -> None:
    """Refuse a render of more pages than ``MAX_RENDERED_PAGES``; ``subject`` names the check."""
    if pages > MAX_RENDERED_PAGES:
        raise PreparationError(
            f"{subject} supports at most {MAX_RENDERED_PAGES} pages; this PDF has {pages}"
        )


def parse_page_sizes(output: str, pages: int) -> list[tuple[float, float]]:
    """Every page size in points from ``pdfinfo -box`` output; geometry only, no render limits."""
    sizes: dict[int, tuple[float, float]] = {}
    for match in re.finditer(
        r"^Page\s+(?:(\d+)\s+)?size:\s*([\d.eE+-]+)\s+x\s+([\d.eE+-]+)\s+pts",
        output,
        re.MULTILINE,
    ):
        index = int(match[1]) if match[1] else 1
        if index in sizes:
            raise PreparationError("pdfinfo returned duplicate page dimension rows")
        try:
            sizes[index] = (float(match[2]), float(match[3]))
        except ValueError as error:
            raise PreparationError("pdfinfo returned invalid page dimensions") from error
    if set(sizes) != set(range(1, pages + 1)):
        raise PreparationError("PDF page dimensions were not available for every page")
    for width, height in sizes.values():
        if not math.isfinite(width) or not math.isfinite(height) or min(width, height) <= 0:
            raise PreparationError("PDF contains invalid page dimensions")
        if max(width, height) > MAX_PAGE_DIMENSION_PT:
            raise PreparationError(
                f"PDF page dimensions exceed {MAX_PAGE_DIMENSION_PT} points on one side; "
                "the file may be damaged"
            )
    return [sizes[page] for page in range(1, pages + 1)]


def split_text_pages(text: str, pages: int) -> list[str]:
    """The pages of pdftotext output, split at its form feeds.

    pdftotext ends every page with a form feed, so a complete extraction holds exactly
    ``pages`` of them and nothing but whitespace after the last. Anything else lost or
    gained a page boundary and is refused. The pages keep their line ends as extracted;
    ``normalized_text_pages`` is the variant that also canonicalizes them.
    """
    extracted = text.split("\f")
    if len(extracted) != pages + 1 or extracted[-1].strip():
        raise PreparationError("Text extraction did not report every PDF page boundary")
    return extracted[:-1]


def normalized_text_pages(text: str, pages: int) -> list[str]:
    """``split_text_pages`` with canonical line ends (LF) and no trailing blanks on a line.

    Pages compared between two PDFs, or tested for emptiness, go through this form so the
    producer's line-end convention and trailing spaces never register as differences.
    """

    def canonical(page: str) -> str:
        lines = page.replace("\r\n", "\n").replace("\r", "\n").split("\n")
        return "\n".join(line.rstrip(" \t") for line in lines)

    return [canonical(page) for page in split_text_pages(text, pages)]


class PageFrames(NamedTuple):
    """Pages whose coordinate frame adapters cannot align, and every page size."""

    unsupported_pages: set[int]
    sizes: list[tuple[float, float]]


def page_frames(output: str, pages: int) -> PageFrames:
    """Reject coordinate frames that the Poppler XML adapter cannot align reliably."""
    rotations: dict[int, int] = {}
    boxes: dict[int, dict[str, tuple[float, ...]]] = {}
    for line in output.splitlines():
        rotation = re.match(r"^Page\s+(?:(\d+)\s+)?rot:\s*(-?\d+)\s*$", line)
        if rotation:
            rotations[int(rotation[1] or "1")] = int(rotation[2])
        box = re.match(r"^(?:Page\s+(\d+)\s+)?(MediaBox|CropBox):\s+(.+)$", line)
        if box:
            values = tuple(finite_number(value) for value in box[3].split())
            if len(values) != 4:
                raise PreparationError("Page boxes have an unrecognized coordinate format")
            boxes.setdefault(int(box[1] or "1"), {})[box[2]] = values
    unsupported = set()
    for page in range(1, pages + 1):
        entry = boxes.get(page, {})
        media, crop = entry.get("MediaBox"), entry.get("CropBox")
        if rotations.get(page) != 0 or media is None or crop != media or media[:2] != (0.0, 0.0):
            unsupported.add(page)
    return PageFrames(unsupported, parse_page_sizes(output, pages))


def inventory_rows(output: str, header: Sequence[str], tool: str) -> list[str]:
    """Data rows of a Poppler inventory table after validating its complete header."""
    lines = output.splitlines()
    if len(lines) < 2 or lines[0].split() != header or re.fullmatch(r"[-\s]+", lines[1]) is None:
        raise PreparationError(f"{tool} did not return a recognized complete inventory header")
    return [line for line in lines[2:] if line.strip()]


@dataclass(frozen=True, slots=True)
class PdfFont:
    """One row of the pdffonts inventory."""

    name: str
    font_type: str
    encoding: str
    embedded: bool
    subset: bool
    unicode_map: bool
    object_number: int
    generation: int

    @property
    def is_type3(self) -> bool:
        return is_type3(self.font_type)

    def to_report(self) -> dict[str, object]:
        """The font as a report detail, under the names the report has always used."""
        return {
            "name": self.name,
            "type": self.font_type,
            "encoding": self.encoding,
            "embedded": self.embedded,
            "subset": self.subset,
            "unicode_map": self.unicode_map,
            "object": self.object_number,
            "generation": self.generation,
        }


def font_inventory(output: str) -> list[PdfFont]:
    """The fonts of pdffonts output; an unrecognized header or row refuses the whole table."""
    inventory: list[PdfFont] = []
    for line in inventory_rows(
        output, "name type encoding emb sub uni object ID".split(), "pdffonts"
    ):
        match = re.fullmatch(
            r"(\S+)\s+(.+?)\s+(\S+)\s+(yes|no)\s+(yes|no)\s+(yes|no)\s+(\d+)\s+(\d+)\s*", line
        )
        if not match:
            raise PreparationError("pdffonts returned an unrecognized inventory row")
        inventory.append(
            PdfFont(
                name=match[1],
                font_type=match[2],
                encoding=match[3],
                embedded=match[4] == "yes",
                subset=match[5] == "yes",
                unicode_map=match[6] == "yes",
                object_number=int(match[7]),
                generation=int(match[8]),
            )
        )
    return inventory


def is_type3(font_type: str) -> bool:
    """Shared Type 3 test for pdffonts type cells; spacing and case are not significant."""
    return re.fullmatch(r"Type\s*3", font_type, re.IGNORECASE) is not None


class FontPolicyOutcome(NamedTuple):
    """The fonts of one inventory that break each font policy, in inventory order."""

    nonembedded: list[PdfFont]
    type3: list[PdfFont]
    # Fonts that break a policy that is switched on; the observations above ignore the switches.
    violations: list[PdfFont]


def evaluate_font_policy(
    inventory: Sequence[PdfFont], *, require_embedded: bool, forbid_type3: bool
) -> FontPolicyOutcome:
    """Apply the embedding and Type 3 font policies to a ``font_inventory``.

    The single definition of a violating font for the manuscript PDF, where the policies
    are separate findings that each read their own list, and for input figures, which
    report the fonts breaking any enabled policy.
    """
    nonembedded = [font for font in inventory if not font.embedded]
    type3 = [font for font in inventory if font.is_type3]
    violations = [
        font
        for font in inventory
        if (require_embedded and not font.embedded) or (forbid_type3 and font.is_type3)
    ]
    return FontPolicyOutcome(nonembedded, type3, violations)


def parse_tool_xml(output: str, root_name: str) -> ET.Element:
    """The root of tool XML that declares no entities and holds at most ``MAX_PARSED_NODES``."""
    # xml.etree is safe here without defusedxml (the project depends on nothing but click and
    # rich). Entity expansion ("billion laughs") and external entities both need an <!ENTITY>
    # declaration, and a declaration can only sit in a DOCTYPE internal subset or in an
    # external DTD. Output with either an ENTITY declaration or an internal subset is rejected
    # before the parser sees it. Expat never fetches an external DTD or entity (ElementTree
    # installs no external-entity handler), and a reference to an undeclared entity is a parse
    # error, so nothing can expand. The size left to bound is the tool output limit, the node
    # count below and the RecursionError catch for very deep nesting.
    if "<!ENTITY" in output.upper() or re.search(r"<!DOCTYPE[^>]*\[", output, re.I):
        raise PreparationError("PDF tool XML contains unsupported entity declarations")
    try:
        root = ET.fromstring(output)
    except (ET.ParseError, RecursionError) as error:
        raise PreparationError("PDF tool returned malformed XML") from error
    if root.tag != root_name:
        raise PreparationError(f"PDF tool did not return a {root_name} XML document")
    if sum(1 for _ in root.iter()) > MAX_PARSED_NODES:
        raise PreparationError("PDF XML exceeds the bounded element limit")
    return root


class TracedDocument(NamedTuple):
    """A MuPDF trace and its page elements with validated page numbers."""

    root: ET.Element
    pages: list[tuple[int, ET.Element]]


def parse_mupdf_trace(output: str, pages: int) -> TracedDocument:
    """Require every page exactly once and in order."""
    root = parse_tool_xml(output, "document")
    traced = root.findall("page")
    try:
        numbers = [int(page.attrib["number"]) for page in traced]
    except (KeyError, ValueError) as error:
        raise PreparationError("MuPDF trace contains an invalid page number") from error
    if numbers != list(range(1, pages + 1)):
        raise PreparationError("MuPDF trace did not report every page exactly once")
    return TracedDocument(root, list(zip(numbers, traced, strict=True)))
