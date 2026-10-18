"""Shared PDF tool sessions, Poppler/qpdf/MuPDF output parsing and finding builders.

Coordinates are PDF points. Page boxes use the bottom-left PDF frame as reported
by Poppler; regions and text boxes use the top-left frame of an unrotated,
uncropped page. Every adapter runs one tool per new directory on frozen inputs.
"""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, NamedTuple, Self, TypeGuard, TypeVar

from .cancellation import bounded_map, run_in_thread
from .errors import PreparationError
from .findings import EvidenceKind, Finding, Severity
from .resource_budget import ResourceBudget
from .runtime import CommandResult, CommandRunner, ToolVersion

_T = TypeVar("_T")

MAX_INSPECTION_PAGES = 5000
# Rendering costs one process and raster per page, so it keeps a smaller cap.
MAX_RENDERED_PAGES = 300
# Comparison and preview renders use this resolution; every page must fit it.
COMPARISON_DPI = 144
MAX_RASTER_PIXELS = 16_000_000
MAX_RASTER_DIMENSION = 8192
# Configured measurement renders use their own, lower resolution and limits.
MAX_MEASUREMENT_PIXELS = 4_000_000
MAX_MEASUREMENT_DIMENSION = 4000
MIN_MEASUREMENT_DPI = 18
MAX_MEASUREMENT_DPI = 144
# PDF 1.7 Annex C: readers need not support pages beyond 14,400 units on a side. Larger
# reported sizes are treated as damaged geometry, which also keeps raster arithmetic finite.
MAX_PAGE_DIMENSION_PT = 14_400
MEASUREMENT_RENDER_MEMORY_MB = 128
MAX_SAMPLES = 100
MAX_PARSED_NODES = 50_000
MAX_PAIR_CHECKS = 250_000
MAX_FIGURE_INPUTS = 300
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
TOOL_FAILURE_SUGGESTION = "Resolve the tool, resource, or isolation failure and repeat this check."
EVIDENCE_LIMITATION_SUGGESTION = (
    "Resolve the stated evidence/tool limitation and repeat the configured check."
)
_OBJECT_REFERENCE = re.compile(r"[0-9]+ [0-9]+ R")
_VERSION_ARGUMENTS = {"qpdf": "--version"}
# pdftoppm option and output file for each supported measurement raster.
_RASTER_FORMATS: dict[str, tuple[tuple[str, ...], str]] = {
    "gray": (("-gray",), "page.pgm"),
    "rgb": ((), "page.ppm"),
}
_DOCUMENT_NAME = "document"


def pdf_input(name: str) -> str:
    """The argument a session replaces with the read-only path of a frozen input."""
    return f"@{name}@"


DOCUMENT = pdf_input(_DOCUMENT_NAME)


def page_boxes_argv(pages: int, document: str = DOCUMENT) -> list[str]:
    return ["pdfinfo", "-box", "-f", "1", "-l", str(pages), document]


def layout_text_argv(document: str = DOCUMENT) -> list[str]:
    return ["pdftotext", "-enc", "UTF-8", "-layout", document, "-"]


def qpdf_objects_argv() -> list[str]:
    """qpdf JSON v2 object and page data without stream contents."""
    return [
        "qpdf",
        "--json=2",
        "--json-key=qpdf",
        "--json-key=pages",
        "--json-stream-data=none",
        DOCUMENT,
    ]


def mupdf_trace_argv(pages: int) -> list[str]:
    return ["mutool", "trace", DOCUMENT, f"1-{pages}"]


async def freeze_pdf_inputs(
    work: Path, documents: Mapping[str, Path], runner: CommandRunner, budget: ResourceBudget | None
) -> tuple[Path, dict[str, Path]]:
    """Copy regular PDF inputs into a new workspace; return it resolved with the copies."""
    async with (
        budget.lease(f"{work.name}: freeze PDF inputs", memory_mb=64)
        if budget is not None
        else nullcontext()
    ):
        if work.exists():
            raise PreparationError("PDF checks require a new, separate workspace")
        for document in documents.values():
            if not document.is_file() or document.is_symlink():
                raise PreparationError("PDF check input must be a regular, non-symlink file")
            if document.stat().st_size > runner.limits.max_file_bytes:
                raise PreparationError("PDF input exceeds the configured file size limit")
        work.mkdir(parents=True)
        work = work.resolve()
        frozen: dict[str, Path] = {}
        for name, document in documents.items():
            frozen[name] = runner.freeze_input(document, work / "inputs")
            with frozen[name].open("rb") as stream:
                if stream.read(5) != b"%PDF-":
                    raise PreparationError("PDF input has no PDF header")
        return work, frozen


# Poppler reports damaged input as "Syntax Error: ..." and, when it knows the byte offset,
# as "Syntax Error (257): ..."; MuPDF reports "error: ...". Warnings describe repairs the
# tool made, so they do not make the evidence incomplete.
_TOOL_ERROR_REPORT = re.compile(
    r"\b(?:Syntax|Command Line|I/O|Permission|Internal|Fontconfig) Error\b(?: \(\d+\))?:|^error:",
    re.IGNORECASE | re.MULTILINE,
)


def _require_complete(result: CommandResult, tool: str) -> None:
    if (reason := result.failure_reason(tool)) is not None:
        raise PreparationError(reason)
    if _TOOL_ERROR_REPORT.search(result.stderr):
        raise PreparationError(
            f"{tool} reported damaged or incomplete input: {result.stderr[:1000]}"
        )


@dataclass(slots=True)
class PdfToolSession:
    """Frozen PDF inputs and one new directory for each tool command.

    An argument equal to ``pdf_input(name)`` is replaced by the frozen copy of that
    input, which is the only read-only file the command may access.
    """

    work: Path
    inputs: dict[str, Path]
    runner: CommandRunner
    budget: ResourceBudget | None
    versions: dict[str, ToolVersion] = field(default_factory=dict)

    @classmethod
    async def open(
        cls,
        work: Path,
        documents: Mapping[str, Path],
        runner: CommandRunner,
        budget: ResourceBudget | None,
    ) -> Self:
        work, inputs = await freeze_pdf_inputs(work, documents, runner, budget)
        return cls(work, inputs, runner, budget)

    def lease(
        self, label: str, *, memory_mb: int = 64, kind: Literal["render"] | None = None
    ) -> AbstractAsyncContextManager[object]:
        if self.budget is None:
            return nullcontext()
        return self.budget.lease(f"{self.work.name}: {label}", memory_mb=memory_mb, kind=kind)

    async def run_without_lease(
        self, name: str, argv: Sequence[str], *, memory_mb: int | None = None
    ) -> CommandResult:
        """Run one tool; the caller already holds a reservation that covers it.

        Each probe and command gets its own writable tool-state directory.
        """
        arguments = {pdf_input(key): path for key, path in self.inputs.items()}
        inputs = tuple(dict.fromkeys(arguments[arg] for arg in argv if arg in arguments))
        actual = [str(arguments[arg]) if arg in arguments else arg for arg in argv]
        tool = argv[0]
        leaf = self.work / name
        version_work, command_work = leaf / "version", leaf / "command"
        version_work.mkdir(parents=True)
        command_work.mkdir()
        self.versions[tool] = await self.runner.tool_version(
            [tool, _VERSION_ARGUMENTS.get(tool, "-v")], version_work
        )
        result = await self.runner.run(
            actual,
            cwd=command_work,
            workspace=command_work,
            readonly_inputs=inputs,
            memory_mb=memory_mb,
        )
        _require_complete(result, tool)
        return result

    async def run(self, name: str, argv: Sequence[str]) -> CommandResult:
        """Run one tool under its own small reservation and return its output."""
        async with self.lease(f"PDF {name}"):
            return await self.run_without_lease(name, argv)

    async def measure(
        self, name: str, argv: Sequence[str], parse: Callable[[CommandResult], _T]
    ) -> _T:
        """Keep command output parsing within the same bounded resource lease."""
        async with self.lease(name):
            result = await self.run_without_lease(name, argv, memory_mb=64)
            self._require_bounded_output(result)
            return parse(result)

    async def render_page(
        self,
        name: str,
        page: int,
        dpi: int,
        parse: Callable[[Path], _T],
        *,
        raster: Literal["gray", "rgb"],
    ) -> _T:
        """Render one page of the document and parse the raster file in a worker thread."""
        options, filename = _RASTER_FORMATS[raster]
        argv = [
            "pdftoppm",
            "-f",
            str(page),
            "-l",
            str(page),
            "-singlefile",
            *options,
            "-r",
            str(dpi),
            DOCUMENT,
            "page",
        ]
        path = self.work / name / "command" / filename
        async with self.lease(name, memory_mb=MEASUREMENT_RENDER_MEMORY_MB, kind="render"):
            try:
                result = await self.run_without_lease(
                    name, argv, memory_mb=MEASUREMENT_RENDER_MEMORY_MB
                )
                self._require_bounded_output(result)
                return await run_in_thread(parse, path)
            finally:
                # Remove only this disposable render, including partial output.
                path.unlink(missing_ok=True)

    def tool_versions(self) -> dict[str, object]:
        return {name: self.versions[name].to_report() for name in sorted(self.versions)}

    def _require_bounded_output(self, result: CommandResult) -> None:
        if len(result.stdout.encode("utf-8")) > self.runner.limits.max_output_bytes:
            raise PreparationError("PDF measurement exceeds the diagnostic output limit")


async def open_measurement_session(
    pdf: Path, work: Path, runner: CommandRunner, budget: ResourceBudget | None
) -> PdfToolSession:
    """One frozen document; configured measurements default to the runner's budget."""
    return await PdfToolSession.open(work, {_DOCUMENT_NAME: pdf}, runner, budget or runner.budget)


async def open_inspection_session(
    pdf: Path, work: Path, runner: CommandRunner, budget: ResourceBudget | None
) -> PdfToolSession:
    """One frozen document under exactly the supplied budget."""
    return await PdfToolSession.open(work, {_DOCUMENT_NAME: pdf}, runner, budget)


async def gather_measurements(
    operations: Sequence[Callable[[], Awaitable[_T]]], budget: ResourceBudget | None
) -> list[_T | PreparationError]:
    """Keep completed evidence when a sibling fails, and drain on cancellation."""

    async def measure(operation: Callable[[], Awaitable[_T]]) -> _T | PreparationError:
        try:
            return await operation()
        except (PreparationError, OSError) as error:
            # Exception tracebacks can otherwise retain the failed worker's
            # large raster buffers until the complete comparison is collected.
            return PreparationError(str(error))

    return await bounded_map(
        operations, measure, limit=budget.cpu_slots if budget is not None else 1
    )


def unavailable_finding(
    rule: str,
    error: Exception,
    *,
    severity: Severity = "error",
    suggestion: str = EVIDENCE_LIMITATION_SUGGESTION,
    details: dict[str, object] | None = None,
) -> Finding:
    """Evidence that could not be measured; never a pass."""
    return Finding(
        rule,
        str(error),
        severity=severity,
        status="inconclusive",
        incomplete=True,
        suggestion=suggestion,
        details={} if details is None else details,
    )


def measurement_finding(
    rule: str,
    message: str,
    violations: list[object],
    *,
    incomplete: bool = False,
    severity: Severity | None = None,
    evidence_kind: EvidenceKind = EvidenceKind.DERIVED,
    total_violations: int | None = None,
    **details: object,
) -> Finding:
    """A configured measurement: failed on a violation, inconclusive on incomplete evidence.

    Without an explicit severity a violation or incomplete evidence is an error and a
    clean measurement is informational. Advisory candidates pass one fixed severity.
    """
    status = "failed" if violations else "inconclusive" if incomplete else "passed"
    if severity is None:
        severity = "error" if violations or incomplete else "info"
    return Finding(
        rule,
        message,
        severity=severity,
        status=status,
        evidence_kind=evidence_kind,
        incomplete=incomplete,
        details={
            **details,
            "violations": violations[:MAX_SAMPLES],
            "violation_count": len(violations) if total_violations is None else total_violations,
            "incomplete": incomplete,
        },
    )


def heuristic_finding(
    rule: str,
    message: str,
    violations: list[object],
    *,
    incomplete: bool = False,
    total_violations: int | None = None,
    **details: object,
) -> Finding:
    """A heuristic candidate: a warning that informs review whatever the outcome."""
    return measurement_finding(
        rule,
        message,
        violations,
        incomplete=incomplete,
        severity="warning",
        evidence_kind=EvidenceKind.HEURISTIC,
        total_violations=total_violations,
        **details,
    )


def require_number(value: object, name: str, *, allow_zero: bool = False) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
        or (value < 0 if allow_zero else value <= 0)
    ):
        raise PreparationError(
            f"{name} must be a {'nonnegative' if allow_zero else 'positive'} number"
        )


def require_distinct_strings(values: tuple[str, ...], name: str) -> None:
    if not isinstance(values, tuple) or any(
        not isinstance(value, str) or not value.strip() or "\0" in value for value in values
    ):
        raise PreparationError(f"{name} must be an immutable tuple of nonempty strings")
    if len(values) != len(set(values)):
        raise PreparationError(f"{name} cannot contain duplicates")


@dataclass(frozen=True)
class PdfRegion:
    """A named rectangle in top-left PDF points on one page."""

    name: str
    page: int
    left_pt: float
    top_pt: float
    right_pt: float
    bottom_pt: float
    min_text_size_pt: float | None = None

    def __post_init__(self) -> None:
        require_distinct_strings((self.name,), "Region name")
        if type(self.page) is not int or self.page < 1:
            raise PreparationError("Region pages must be positive integers")
        for value in self.box:
            require_number(value, "Region coordinates", allow_zero=True)
        if self.left_pt >= self.right_pt or self.top_pt >= self.bottom_pt:
            raise PreparationError("Regions must have positive width and height")
        if self.min_text_size_pt is not None:
            require_number(self.min_text_size_pt, "Region minimum text size")

    @property
    def box(self) -> tuple[float, float, float, float]:
        return self.left_pt, self.top_pt, self.right_pt, self.bottom_pt


def pdfinfo_fields(output: str) -> dict[str, str]:
    return {
        key.strip(): value.strip()
        for key, value in (line.split(":", 1) for line in output.splitlines() if ":" in line)
    }


def page_count(fields: dict[str, str], maximum: int = MAX_INSPECTION_PAGES) -> int:
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


def require_comparison_raster_fit(sizes: Sequence[tuple[float, float]]) -> None:
    """Reject pages that cannot be rendered at the comparison resolution within raster limits."""
    for page, (width, height) in enumerate(sizes, 1):
        pixels = (math.ceil(width * COMPARISON_DPI / 72), math.ceil(height * COMPARISON_DPI / 72))
        if max(pixels) > MAX_RASTER_DIMENSION or math.prod(pixels) > MAX_RASTER_PIXELS:
            raise PreparationError(
                f"Page {page} ({width:g} x {height:g} points) is too large to compare as images "
                f"at {COMPARISON_DPI} DPI (limits: {MAX_RASTER_PIXELS:,} pixels, "
                f"{MAX_RASTER_DIMENSION:,} per side); compare a smaller export of the document"
            )


def normalized_text_pages(text: str, pages: int) -> list[str]:
    """Split pdftotext output at form feeds, normalizing line ends and trailing blanks."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    extracted = text.split("\f")
    if extracted and not extracted[-1].strip():
        extracted.pop()
    if len(extracted) != pages:
        raise PreparationError("Extracted text did not preserve the expected page boundaries")
    return ["\n".join(line.rstrip(" \t") for line in page.split("\n")) for page in extracted]


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


def measurement_raster_size(size: tuple[float, float], dpi: int) -> tuple[int, int]:
    return math.ceil(size[0] * dpi / 72), math.ceil(size[1] * dpi / 72)


def exceeds_measurement_raster(dimensions: tuple[int, int]) -> bool:
    return math.prod(dimensions) > MAX_MEASUREMENT_PIXELS or max(dimensions) > (
        MAX_MEASUREMENT_DIMENSION
    )


def inventory_rows(output: str, header: Sequence[str], tool: str) -> list[str]:
    """Data rows of a Poppler inventory table after validating its complete header."""
    lines = output.splitlines()
    if len(lines) < 2 or lines[0].split() != header or re.fullmatch(r"[-\s]+", lines[1]) is None:
        raise PreparationError(f"{tool} did not return a recognized complete inventory header")
    return [line for line in lines[2:] if line.strip()]


def font_inventory(output: str) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    for line in inventory_rows(
        output, "name type encoding emb sub uni object ID".split(), "pdffonts"
    ):
        match = re.fullmatch(
            r"(\S+)\s+(.+?)\s+(\S+)\s+(yes|no)\s+(yes|no)\s+(yes|no)\s+(\d+)\s+(\d+)\s*", line
        )
        if not match:
            raise PreparationError("pdffonts returned an unrecognized inventory row")
        inventory.append(
            {
                "name": match[1],
                "type": match[2],
                "encoding": match[3],
                "embedded": match[4] == "yes",
                "subset": match[5] == "yes",
                "unicode_map": match[6] == "yes",
                "object": int(match[7]),
                "generation": int(match[8]),
            }
        )
    return inventory


def is_type3(font_type: object) -> bool:
    """Shared Type 3 test for pdffonts type cells; spacing and case are not significant."""
    return re.fullmatch(r"Type\s*3", str(font_type), re.IGNORECASE) is not None


def read_rgb_raster(
    path: Path, *, max_pixels: int = MAX_RASTER_PIXELS
) -> tuple[tuple[int, int], bytes]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > 3 * max_pixels + 1024:
        raise PreparationError("Renderer did not produce a bounded raster page")
    with path.open("rb") as stream:
        # Validate the header before allocating pixel buffers; reading the entire
        # file and then slicing it transiently duplicates a full page in memory.
        header = re.match(rb"P6\s+(\d+)\s+(\d+)\s+255(?:\r\n|\s)", stream.read(1024))
        if header is None:
            raise PreparationError("Renderer output is not a supported RGB PPM page")
        width, height = int(header[1]), int(header[2])
        if (
            min(width, height) < 1
            or max(width, height) > MAX_RASTER_DIMENSION
            or width * height > min(max_pixels, MAX_RASTER_PIXELS)
        ):
            raise PreparationError("Renderer output has invalid or oversized raster dimensions")
        stream.seek(header.end())
        pixels = stream.read(width * height * 3 + 1)
    if len(pixels) != width * height * 3:
        raise PreparationError("Renderer output has invalid or oversized raster dimensions")
    return (width, height), pixels


def read_gray_raster(path: Path, max_file_bytes: int) -> tuple[int, int, bytes]:
    if (
        not path.is_file()
        or path.is_symlink()
        or path.stat().st_size > min(max_file_bytes, MAX_MEASUREMENT_PIXELS + 100)
    ):
        raise PreparationError("A complete bounded grayscale render was not produced")
    data = path.read_bytes()
    header = re.match(rb"P5\s+(\d+)\s+(\d+)\s+255(?:\r\n|\s)", data)
    if header is None:
        raise PreparationError("Renderer did not produce supported 8-bit grayscale PGM data")
    width, height = int(header[1]), int(header[2])
    if min(width, height) < 1 or width * height > MAX_MEASUREMENT_PIXELS:
        raise PreparationError("Grayscale render exceeds the safe pixel limit")
    pixels = data[header.end() :]
    if len(pixels) != width * height:
        raise PreparationError("Grayscale render is truncated or contains unexpected trailing data")
    return width, height, pixels


def finite_number(value: str | None) -> float:
    try:
        number = float(value) if value is not None else math.nan
    except ValueError as error:
        raise PreparationError("PDF tool returned an invalid numerical measurement") from error
    if not math.isfinite(number):
        raise PreparationError("PDF tool returned an invalid numerical measurement")
    return number


def contains(outer: tuple[float, ...], inner: tuple[float, ...], tolerance: float) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def intersection_area(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    return max(0.0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0.0, min(first[3], second[3]) - max(first[1], second[1])
    )


def relative_luminance(rgb: tuple[float, float, float]) -> float:
    """WCAG relative luminance of sRGB components in [0, 1]."""
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in rgb
    ]
    return sum(
        value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
    )


def parse_tool_xml(output: str, root_name: str) -> ET.Element:
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


def parse_mupdf_trace(
    output: str,
    pages: int,
    *,
    invalid_page_message: str = "MuPDF trace contains an invalid page number",
    incomplete_message: str = "MuPDF trace did not report every page exactly once",
) -> TracedDocument:
    """Require every page exactly once and in order; callers keep their own messages."""
    root = parse_tool_xml(output, "document")
    traced = root.findall("page")
    try:
        numbers = [int(page.attrib["number"]) for page in traced]
    except (KeyError, ValueError) as error:
        raise PreparationError(invalid_page_message) from error
    if numbers != list(range(1, pages + 1)):
        raise PreparationError(incomplete_message)
    return TracedDocument(root, list(zip(numbers, traced, strict=True)))


def is_object_reference(value: object) -> TypeGuard[str]:
    """An indirect qpdf object reference such as ``12 0 R``."""
    return isinstance(value, str) and _OBJECT_REFERENCE.fullmatch(value) is not None


class QpdfObjectGraph:
    """qpdf JSON v2 object access with bounded, cycle-aware reference traversal."""

    def __init__(self, output: str, pages: int):
        try:
            document = json.loads(output)
            entries = document["qpdf"]
            if (
                document["version"] != 2
                or len(entries) != 2
                or entries[0]["jsonversion"] != 2
                or not isinstance(entries[1], dict)
            ):
                raise ValueError("unsupported JSON shape")
            self.objects = entries[1]
            self.pages = document["pages"]
            if not isinstance(self.pages, list) or len(self.pages) != pages:
                raise ValueError("incomplete page data")
        except (ValueError, KeyError, TypeError, RecursionError) as error:
            raise PreparationError(
                "qpdf did not return complete supported JSON v2 object data"
            ) from error
        if len(self.objects) > MAX_PARSED_NODES:
            raise PreparationError(
                f"The PDF has {len(self.objects):,} objects; object-level checks support at most "
                f"{MAX_PARSED_NODES:,}. Check a smaller PDF or turn these checks off for it."
            )
        self.root = self.dictionary(self.dictionary(self.lookup("trailer")).get("/Root"))
        if self.root.get("/Type") != "/Catalog":
            raise PreparationError("qpdf object data lacks a valid catalog")

    def lookup(self, key: str) -> object:
        try:
            entry = self.objects[key]
            value = entry["value"] if "value" in entry else entry["stream"]["dict"]
        except (KeyError, TypeError) as error:
            raise PreparationError("qpdf contains an unresolved or malformed object") from error
        return value

    def resolve(self, value: object) -> object:
        seen: set[str] = set()
        while is_object_reference(value):
            if value in seen or len(seen) > 100:
                raise PreparationError("qpdf contains a cyclic direct-reference chain")
            seen.add(value)
            value = self.lookup("obj:" + value)
        return value

    def dictionary(self, value: object) -> dict[str, object]:
        value = self.resolve(value)
        if not isinstance(value, dict):
            raise PreparationError("qpdf contains an unexpected non-dictionary object")
        return value

    def reachable_dictionaries(self) -> list[dict[str, object]]:
        pending: list[tuple[object, int]] = [(self.root, 0)]
        references: set[str] = set()
        result = []
        visited = 0
        while pending:
            value, depth = pending.pop()
            visited += 1
            if visited > MAX_PARSED_NODES or depth > 100:
                raise PreparationError("qpdf object graph exceeds the bounded traversal limit")
            if is_object_reference(value):
                if value in references:
                    continue
                references.add(value)
                value = self.resolve(value)
            if isinstance(value, dict):
                result.append(value)
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
            if len(pending) > MAX_PARSED_NODES:
                raise PreparationError("qpdf object graph exceeds the bounded traversal limit")
        return result
