"""Shared PDF tool sessions, argument builders, Poppler/qpdf/MuPDF output parsing and findings.

Coordinates are PDF points. Page boxes use the bottom-left PDF frame as reported
by Poppler; regions and text boxes use the top-left frame of an unrotated,
uncropped page. Every adapter runs one tool per new directory on frozen inputs.
"""

from __future__ import annotations

import asyncio
import json
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, nullcontext
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, NamedTuple, Self, TypeGuard, TypeVar

from .cancellation import bounded_map, run_in_thread
from .errors import PreparationError, SettingsError
from .findings import EvidenceKind, Finding, Severity
from .limits import DEFAULT_LEASE_MEMORY_MB, MAX_PDF_NESTING_LEVELS
from .option_config import (
    EIGHT_BIT_MAXIMUM,
    EIGHT_BIT_VALUE,
    NONNEGATIVE_NUMBER,
    POSITIVE_INTEGER,
    POSITIVE_NUMBER,
    Bounds,
    require_text,
    validate_scalars,
)
from .raster_pixels import (
    MAX_RASTER_DIMENSION,
    MAX_RASTER_PIXELS,
    RasterImage,
    read_pnm_raster,
)
from .resource_budget import ResourceBudget
from .runtime import CommandResult, CommandRunner, ToolVersion

_T = TypeVar("_T")

MAX_INSPECTION_PAGES = 5000
# Rendering costs one process and raster per page, so it keeps a smaller cap.
MAX_RENDERED_PAGES = 300
# Comparison and preview renders use this resolution; every page must fit it.
COMPARISON_DPI = 144
# Configured measurement renders use their own, lower resolution and limits.
MAX_MEASUREMENT_PIXELS = 4_000_000
MAX_MEASUREMENT_DIMENSION = 4000
MIN_MEASUREMENT_DPI = 18
MAX_MEASUREMENT_DPI = 144
# How a rendered page is sampled for ink, as the pdf_checks and rendered/included artwork
# tables spell it: the resolution, the paper gray value and the gray difference that counts
# as ink. The defaults differ between those tables; the limits do not.
RASTER_SAMPLING_BOUNDS = {
    "raster_dpi": Bounds(MIN_MEASUREMENT_DPI, MAX_MEASUREMENT_DPI),
    "background_gray": EIGHT_BIT_VALUE,
    "ink_difference": Bounds(1, EIGHT_BIT_MAXIMUM),
}
# WCAG contrast ratios run from 1:1 (identical colors) to 21:1 (black on white).
CONTRAST_RATIO_BOUNDS = Bounds(1, 21)
# Entries in one configured list of regions, section budgets or contrast samples.
MAX_REGIONS_PER_POLICY = 300
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
# Every external tool the PDF adapters can run; qpdf alone reports its version with --version.
PDF_TOOLS = frozenset(
    {
        "pdfinfo",
        "pdftotext",
        "pdftoppm",
        "pdffonts",
        "pdfdetach",
        "pdfimages",
        "pdftohtml",
        "qpdf",
        "mutool",
    }
)
_VERSION_ARGUMENTS = {"qpdf": "--version"}
# pdftoppm option and output file for each supported page raster.
_RASTER_FORMATS: dict[str, tuple[tuple[str, ...], str]] = {
    "gray": (("-gray",), "page.pgm"),
    "rgb": ((), "page.ppm"),
    "png": (("-png",), "page.png"),
}
_DOCUMENT_NAME = "document"


def pdf_tool_version_argv(tool: str) -> list[str]:
    """The command that prints the version of one of the ``PDF_TOOLS``."""
    return [tool, _VERSION_ARGUMENTS.get(tool, "-v")]


def pdf_input(name: str) -> str:
    """The argument a session replaces with the read-only path of a frozen input."""
    return f"@{name}@"


DOCUMENT = pdf_input(_DOCUMENT_NAME)


# Every external command the PDF checks run is built here, so equal measurements share
# one argument list (and one memoized result within a session).


def pdfinfo_argv(document: str = DOCUMENT) -> list[str]:
    """Document properties, with ISO 8601 dates and UTF-8 text.

    Every PDF check shares this form. The parsers split each line at its first colon,
    which an ISO date does not move, and Poppler writes UTF-8 whatever the locale.
    """
    return ["pdfinfo", "-enc", "UTF-8", "-isodates", document]


def page_boxes_argv(pages: int, document: str = DOCUMENT) -> list[str]:
    return ["pdfinfo", "-box", "-f", "1", "-l", str(pages), document]


def layout_text_argv(document: str = DOCUMENT) -> list[str]:
    return ["pdftotext", "-enc", "UTF-8", "-layout", document, "-"]


def font_inventory_argv(document: str = DOCUMENT) -> list[str]:
    return ["pdffonts", document]


def text_geometry_argv(document: str = DOCUMENT) -> list[str]:
    """Poppler XML text boxes: ``-i`` excludes image extraction, ``-nomerge`` keeps
    smaller text boxes apart."""
    return [
        "pdftohtml",
        "-xml",
        "-stdout",
        "-i",
        "-zoom",
        "1",
        "-noroundcoord",
        "-nomerge",
        "-enc",
        "UTF-8",
        document,
    ]


def attachment_list_argv(document: str = DOCUMENT) -> list[str]:
    return ["pdfdetach", "-list", document]


def image_list_argv(document: str = DOCUMENT) -> list[str]:
    return ["pdfimages", "-list", document]


def render_page_argv(
    page: int, dpi: int, raster: Literal["gray", "rgb", "png"], document: str = DOCUMENT
) -> list[str]:
    """Render one page as a single file named ``raster_filename(raster)`` in the working
    directory: 8-bit PGM (gray), PPM (rgb) or PNG."""
    options, _ = _RASTER_FORMATS[raster]
    return [
        "pdftoppm",
        "-f",
        str(page),
        "-l",
        str(page),
        "-singlefile",
        *options,
        "-r",
        str(dpi),
        document,
        "page",
    ]


def raster_filename(raster: Literal["gray", "rgb", "png"]) -> str:
    """The file ``render_page_argv`` writes in a command's working directory."""
    return _RASTER_FORMATS[raster][1]


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


def qpdf_decoded_objects_argv(references: Iterable[str]) -> list[str]:
    """qpdf JSON v2 data for the selected indirect objects, with decoded inline streams.

    ``references`` are indirect references such as ``12 0 R``; only they are decoded, so
    image pixels and font files are never expanded.
    """
    selectors = [
        "--json-object=" + ",".join(reference.split()[:2]) for reference in sorted(references)
    ]
    return [
        "qpdf",
        "--json=2",
        "--json-key=qpdf",
        "--decode-level=all",
        "--json-stream-data=inline",
        *selectors,
        DOCUMENT,
    ]


def mupdf_trace_argv(pages: int) -> list[str]:
    return ["mutool", "trace", DOCUMENT, f"1-{pages}"]


async def freeze_pdf_inputs(
    work: Path, documents: Mapping[str, Path], runner: CommandRunner, budget: ResourceBudget | None
) -> tuple[Path, dict[str, Path]]:
    """Copy regular PDF inputs into a new workspace; return it resolved with the copies."""
    async with (
        budget.lease(f"{work.name}: freeze PDF inputs", memory_mb=DEFAULT_LEASE_MEMORY_MB)
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
    # Completed read-only measurements, and a lock for each argument list; see ``measure``.
    _measured: dict[tuple[str, ...], CommandResult] = field(
        default_factory=dict, init=False, repr=False
    )
    _measure_locks: dict[tuple[str, ...], asyncio.Lock] = field(
        default_factory=dict, init=False, repr=False
    )

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
        self,
        label: str,
        *,
        memory_mb: int = DEFAULT_LEASE_MEMORY_MB,
        kind: Literal["render"] | None = None,
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
            pdf_tool_version_argv(tool), version_work
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
        """Run a read-only measurement and parse its output within one bounded lease.

        The session remembers each completed result by argument list, so asking for the
        same measurement again (the page boxes for two checks, say) parses the earlier
        output instead of running the tool again. Concurrent requests for one argument
        list take turns outside any lease, so the tool still runs once; if the first is
        cancelled or fails, the next runs it. Only deterministic, read-only commands
        belong here; renders and anything that writes use ``run`` or ``render_page``.
        """
        key = tuple(argv)
        async with self._measure_locks.setdefault(key, asyncio.Lock()), self.lease(name):
            result = self._measured.get(key)
            if result is None:
                result = await self.run_without_lease(name, argv, memory_mb=DEFAULT_LEASE_MEMORY_MB)
                self._require_bounded_output(result)
                self._measured[key] = result
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
        argv = render_page_argv(page, dpi, raster)
        path = self.work / name / "command" / raster_filename(raster)
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


async def open_pdf_session(
    pdf: Path,
    work: Path,
    runner: CommandRunner,
    budget: ResourceBudget | None,
    *,
    default_to_runner_budget: bool = True,
) -> PdfToolSession:
    """One frozen document.

    Configured measurements default to the runner's budget when none is supplied, so
    their commands stay admitted and bounded. The basic inspection passes
    ``default_to_runner_budget=False`` and runs under exactly the supplied budget.
    """
    if default_to_runner_budget:
        budget = budget or runner.budget
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


@dataclass(frozen=True)
class PdfRegion:
    """A named rectangle in top-left PDF points on one page."""

    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        "page": POSITIVE_INTEGER,
        "left_pt": NONNEGATIVE_NUMBER,
        "top_pt": NONNEGATIVE_NUMBER,
        "right_pt": NONNEGATIVE_NUMBER,
        "bottom_pt": NONNEGATIVE_NUMBER,
        "min_text_size_pt": POSITIVE_NUMBER,
    }

    name: str
    page: int
    left_pt: float
    top_pt: float
    right_pt: float
    bottom_pt: float
    min_text_size_pt: float | None = None

    def __post_init__(self) -> None:
        require_text("name", self.name)
        validate_scalars(self)
        if self.left_pt >= self.right_pt or self.top_pt >= self.bottom_pt:
            raise SettingsError(
                "A region needs right_pt greater than left_pt and bottom_pt greater than top_pt"
            )

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


@dataclass(frozen=True, slots=True)
class PdfPreflight:
    """A frozen document whose pdfinfo properties and page count are established."""

    session: PdfToolSession
    fields: dict[str, str]
    pages: int

    @classmethod
    async def open(
        cls,
        pdf: Path,
        work: Path,
        runner: CommandRunner,
        budget: ResourceBudget | None,
        *,
        default_to_runner_budget: bool = True,
    ) -> Self:
        """Freeze ``pdf`` and read its properties; raises when either cannot be established."""
        session = await open_pdf_session(
            pdf, work, runner, budget, default_to_runner_budget=default_to_runner_budget
        )
        fields = await session.measure(
            "properties", pdfinfo_argv(), lambda result: pdfinfo_properties(result.stdout)
        )
        return cls(session, fields, page_count(fields))


async def preflight_pdf(
    pdf: Path,
    work: Path,
    runner: CommandRunner,
    budget: ResourceBudget | None,
    rules: Iterable[str],
    *,
    default_to_runner_budget: bool = True,
    suggestion: str = EVIDENCE_LIMITATION_SUGGESTION,
) -> PdfPreflight | list[Finding]:
    """The preflighted document, or each rule of ``rules`` as unavailable evidence.

    Without a readable document, page count and unambiguous properties no configured
    measurement has anything to measure, so none of them may pass. A failure arrives as
    the list of unavailable findings the caller returns as its result.
    """
    try:
        return await PdfPreflight.open(
            pdf, work, runner, budget, default_to_runner_budget=default_to_runner_budget
        )
    except (PreparationError, OSError) as error:
        return [unavailable_finding(rule, error, suggestion=suggestion) for rule in rules]


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


class FontPolicyOutcome(NamedTuple):
    """The fonts of one inventory that break each font policy, in inventory order."""

    nonembedded: list[dict[str, object]]
    type3: list[dict[str, object]]
    # Fonts that break a policy that is switched on; the observations above ignore the switches.
    violations: list[dict[str, object]]


def evaluate_font_policy(
    inventory: Sequence[dict[str, object]], *, require_embedded: bool, forbid_type3: bool
) -> FontPolicyOutcome:
    """Apply the embedding and Type 3 font policies to a ``font_inventory``.

    The single definition of a violating font for the manuscript PDF, where the policies
    are separate findings that each read their own list, and for input figures, which
    report the fonts breaking any enabled policy.
    """
    nonembedded = [font for font in inventory if not font["embedded"]]
    type3 = [font for font in inventory if is_type3(font["type"])]
    violations = [
        font
        for font in inventory
        if (require_embedded and not font["embedded"]) or (forbid_type3 and is_type3(font["type"]))
    ]
    return FontPolicyOutcome(nonembedded, type3, violations)


def read_measurement_raster(
    path: Path, *, channels: Literal[1, 3], max_file_bytes: int
) -> RasterImage:
    """A configured measurement render: at most ``MAX_MEASUREMENT_PIXELS`` pixels."""
    return read_pnm_raster(
        path,
        channels=channels,
        max_pixels=MAX_MEASUREMENT_PIXELS,
        max_dimension=MAX_RASTER_DIMENSION,
        max_file_bytes=max_file_bytes,
    )


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


class QpdfJson(NamedTuple):
    """The object table of a qpdf JSON v2 document, and its page list (empty unless requested)."""

    objects: dict[str, Any]
    pages: list[Any]


def parse_qpdf_json_v2(output: str, failure: str, *, pages: int | None = None) -> QpdfJson:
    """Validate the envelope of qpdf ``--json=2`` output and return its object table.

    The envelope is the format version, the ``qpdf`` entry holding the header and the
    objects, and, when ``pages`` is given, a page list of exactly that length. Any
    deviation raises ``PreparationError(failure)``: the caller names what it needed.
    """
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
        page_list: list[Any] = []
        if pages is not None:
            page_list = document["pages"]
            if not isinstance(page_list, list) or len(page_list) != pages:
                raise ValueError("incomplete page data")
    except (ValueError, KeyError, TypeError, RecursionError) as error:
        raise PreparationError(failure) from error
    return QpdfJson(entries[1], page_list)


class QpdfObjectGraph:
    """qpdf JSON v2 object access with bounded, cycle-aware reference traversal."""

    def __init__(self, output: str, pages: int):
        document = parse_qpdf_json_v2(
            output, "qpdf did not return complete supported JSON v2 object data", pages=pages
        )
        self.objects = document.objects
        self.pages = document.pages
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
            if value in seen or len(seen) > MAX_PDF_NESTING_LEVELS:
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
            if visited > MAX_PARSED_NODES or depth > MAX_PDF_NESTING_LEVELS:
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
