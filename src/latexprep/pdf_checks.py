"""Optional, bounded PDF measurements with explicit evidence boundaries.

Coordinates are PDF points from the top-left of an unrotated, uncropped page.
Poppler text boxes do not describe images, outlines, clipping paths or semantics.
qpdf JSON supplies object structure; MuPDF trace supplies drawing operations.
Neither adapter asserts reading order, description quality or full accessibility.
"""

from __future__ import annotations

import json
import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Callable
from contextlib import nullcontext
from dataclasses import dataclass, replace
from pathlib import Path
from typing import TypeVar

from .models import Finding, PreparationError
from .pdf import (
    _fields,
    _font_inventory,
    _is_type3,
    _new_workspace,
    _page_count,
    _page_sizes,
    _require_complete,
    _require_rendered_pages,
)
from .runtime import CommandResult, ToolRunner
from .scheduler import ResourceBudget, run_in_thread

_T = TypeVar("_T")
_MAX_NODES = 50_000
_MAX_SAMPLES = 100
_MAX_PIXELS = 4_000_000
_MAX_PAIR_CHECKS = 250_000
_METADATA_FIELDS = frozenset({"Title", "Author", "Subject", "Keywords", "Creator", "Producer"})


def _number(value: object, name: str, *, zero: bool = False) -> None:
    if (
        isinstance(value, bool)
        or not isinstance(value, (float, int))
        or not math.isfinite(value)
        or (value < 0 if zero else value <= 0)
    ):
        raise PreparationError(f"{name} must be a {'nonnegative' if zero else 'positive'} number")


def _strings(values: tuple[str, ...], name: str) -> None:
    if not isinstance(values, tuple) or any(
        not isinstance(value, str) or not value.strip() or "\0" in value for value in values
    ):
        raise PreparationError(f"{name} must be an immutable tuple of nonempty strings")
    if len(values) != len(set(values)):
        raise PreparationError(f"{name} cannot contain duplicates")


@dataclass(frozen=True)
class PdfSectionBudget:
    name: str
    start_page: int
    end_page: int
    max_pages: int

    def __post_init__(self) -> None:
        _strings((self.name,), "Section name")
        if (
            any(
                type(n) is not int or n < 1
                for n in (self.start_page, self.end_page, self.max_pages)
            )
            or self.start_page > self.end_page
        ):
            raise PreparationError("Section budgets need positive, ordered inclusive page ranges")


@dataclass(frozen=True)
class PdfRegion:
    name: str
    page: int
    left_pt: float
    top_pt: float
    right_pt: float
    bottom_pt: float
    min_text_size_pt: float | None = None

    def __post_init__(self) -> None:
        _strings((self.name,), "Region name")
        if type(self.page) is not int or self.page < 1:
            raise PreparationError("Region pages must be positive integers")
        for value in self.box:
            _number(value, "Region coordinates", zero=True)
        if self.left_pt >= self.right_pt or self.top_pt >= self.bottom_pt:
            raise PreparationError("Regions must have positive width and height")
        if self.min_text_size_pt is not None:
            _number(self.min_text_size_pt, "Region minimum text size")

    @property
    def box(self) -> tuple[float, float, float, float]:
        return self.left_pt, self.top_pt, self.right_pt, self.bottom_pt


@dataclass(frozen=True)
class PdfCheckOptions:
    section_budgets: tuple[PdfSectionBudget, ...] = ()
    min_text_size_pt: float | None = None
    text_size_regions: tuple[PdfRegion, ...] = ()
    printable_margins_pt: tuple[float, float, float, float] | None = None
    printable_regions: tuple[PdfRegion, ...] = ()
    overlap_min_area_ratio: float | None = None
    geometry_tolerance_pt: float = 0.5
    min_page_ink_ratio: float | None = None
    sparse_page_exemptions: tuple[int, ...] = ()
    raster_dpi: int = 36
    background_gray: int = 255
    ink_difference: int = 5
    required_metadata: tuple[str, ...] = ()
    expected_metadata: tuple[tuple[str, str], ...] = ()
    require_tagged: bool = False
    require_language: bool = False
    expected_language: str | None = None
    forbidden_annotation_types: tuple[str, ...] = ()
    forbidden_action_types: tuple[str, ...] = ()
    min_stroke_width_pt: float | None = None
    allowed_color_spaces: tuple[str, ...] = ()
    min_text_contrast: float | None = None
    contrast_background_rgb: tuple[float, float, float] | None = None
    min_grayscale_luminance_difference: float | None = None
    require_embedded_figure_fonts: bool = False
    forbid_type3_figure_fonts: bool = False

    def __post_init__(self) -> None:
        for name in (
            "require_tagged",
            "require_language",
            "require_embedded_figure_fonts",
            "forbid_type3_figure_fonts",
        ):
            if type(getattr(self, name)) is not bool:
                raise PreparationError(f"{name} must be a boolean")
        for name, kind in (
            ("section_budgets", PdfSectionBudget),
            ("text_size_regions", PdfRegion),
            ("printable_regions", PdfRegion),
        ):
            values = getattr(self, name)
            if not isinstance(values, tuple) or any(not isinstance(v, kind) for v in values):
                raise PreparationError(f"{name} must be an immutable tuple of {kind.__name__}")
            if len(values) > 300:
                raise PreparationError(f"{name} exceeds the 300-entry configuration limit")
        if any(region.min_text_size_pt is None for region in self.text_size_regions):
            raise PreparationError("Every text-size region needs a minimum text size")
        for name in ("min_text_size_pt", "min_stroke_width_pt"):
            if getattr(self, name) is not None:
                _number(getattr(self, name), name)
        _number(self.geometry_tolerance_pt, "Geometry tolerance", zero=True)
        for name in (
            "overlap_min_area_ratio",
            "min_page_ink_ratio",
            "min_grayscale_luminance_difference",
        ):
            value = getattr(self, name)
            if value is not None:
                _number(value, name)
                if value > 1:
                    raise PreparationError(f"{name} cannot exceed one")
        if self.min_text_contrast is not None:
            _number(self.min_text_contrast, "Minimum text contrast")
            if not 1 <= self.min_text_contrast <= 21 or self.contrast_background_rgb is None:
                raise PreparationError(
                    "Text contrast requires a ratio from 1 to 21 and background RGB"
                )
        if self.contrast_background_rgb is not None:
            if (
                not isinstance(self.contrast_background_rgb, tuple)
                or len(self.contrast_background_rgb) != 3
            ):
                raise PreparationError(
                    "Contrast background RGB must have three immutable components"
                )
            for value in self.contrast_background_rgb:
                _number(value, "Contrast background component", zero=True)
                if value > 1:
                    raise PreparationError("Contrast background components must be in [0, 1]")
        if self.printable_margins_pt is not None:
            if (
                not isinstance(self.printable_margins_pt, tuple)
                or len(self.printable_margins_pt) != 4
            ):
                raise PreparationError("Printable margins must be (left, top, right, bottom)")
            for value in self.printable_margins_pt:
                _number(value, "Printable margins", zero=True)
        if not isinstance(self.sparse_page_exemptions, tuple) or any(
            type(page) is not int or page < 1 for page in self.sparse_page_exemptions
        ):
            raise PreparationError("Sparse-page exemptions must be a tuple of positive pages")
        if type(self.raster_dpi) is not int or not 18 <= self.raster_dpi <= 144:
            raise PreparationError("Raster DPI must be an integer from 18 to 144")
        if type(self.background_gray) is not int or not 0 <= self.background_gray <= 255:
            raise PreparationError("Background gray must be an integer from 0 to 255")
        if type(self.ink_difference) is not int or not 1 <= self.ink_difference <= 255:
            raise PreparationError("Ink difference must be an integer from 1 to 255")
        for name in (
            "required_metadata",
            "forbidden_annotation_types",
            "forbidden_action_types",
            "allowed_color_spaces",
        ):
            _strings(getattr(self, name), name)
        if not set(self.required_metadata) <= _METADATA_FIELDS:
            raise PreparationError("Required metadata must name supported pdfinfo text fields")
        if not isinstance(self.expected_metadata, tuple) or any(
            not isinstance(pair, tuple)
            or len(pair) != 2
            or not isinstance(pair[0], str)
            or pair[0] not in _METADATA_FIELDS
            or not isinstance(pair[1], str)
            or not pair[1].strip()
            or "\0" in pair[1]
            for pair in self.expected_metadata
        ):
            raise PreparationError(
                "Expected metadata needs immutable (supported field, value) pairs"
            )
        if len({key for key, _ in self.expected_metadata}) != len(self.expected_metadata):
            raise PreparationError("Expected metadata fields cannot repeat")
        if self.expected_language is not None:
            _strings((self.expected_language,), "Expected language")
        for name in ("forbidden_annotation_types", "forbidden_action_types"):
            if any(not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", item) for item in getattr(self, name)):
                raise PreparationError(f"{name} uses PDF names without a leading slash")


def _configured(options: PdfCheckOptions) -> list[str]:
    settings = {
        "pdf.section_page_budget": bool(options.section_budgets),
        "pdf.rendered_text_size": options.min_text_size_pt is not None
        or bool(options.text_size_regions),
        "pdf.printable_text_bounds": options.printable_margins_pt is not None
        or bool(options.printable_regions),
        "pdf.text_overlap": options.overlap_min_area_ratio is not None,
        "pdf.sparse_pages": options.min_page_ink_ratio is not None,
        "pdf.required_metadata": bool(options.required_metadata),
        "pdf.metadata_agreement": bool(options.expected_metadata),
        "pdf.tagging_presence": options.require_tagged,
        "pdf.document_language": options.require_language or options.expected_language is not None,
        "pdf.annotation_policy": bool(options.forbidden_annotation_types),
        "pdf.action_policy": bool(options.forbidden_action_types),
        "pdf.stroke_width": options.min_stroke_width_pt is not None,
        "pdf.color_space_policy": bool(options.allowed_color_spaces),
        "pdf.assumed_text_contrast": options.min_text_contrast is not None,
        "pdf.grayscale_separation": options.min_grayscale_luminance_difference is not None,
    }
    return [name for name, enabled in settings.items() if enabled]


def _result(
    rule: str,
    message: str,
    violations: list[object],
    *,
    uncertain: bool = False,
    advisory: bool = False,
    total_violations: int | None = None,
    **details: object,
) -> Finding:
    status = "failed" if violations else "inconclusive" if uncertain else "passed"
    return Finding(
        rule,
        message,
        "warning" if advisory else "error" if violations or uncertain else "info",
        status,
        evidence="heuristic" if advisory else "derived",
        details={
            **details,
            "violations": violations[:_MAX_SAMPLES],
            "violation_count": len(violations) if total_violations is None else total_violations,
            "incomplete": uncertain,
        },
    )


def _unavailable(rule: str, error: Exception, *, advisory: bool = False) -> Finding:
    return Finding(
        rule,
        str(error),
        "warning" if advisory else "error",
        "inconclusive",
        suggestion="Resolve the stated evidence/tool limitation and repeat the configured check.",
    )


@dataclass
class _Session:
    work: Path
    pdf: Path
    runner: ToolRunner
    budget: ResourceBudget | None

    async def measure(
        self,
        name: str,
        argv: list[str],
        parser: Callable[[CommandResult, Path], _T],
        *,
        render: bool = False,
        memory_mb: int = 64,
        threaded_parser: bool = False,
    ) -> _T:
        """Keep command output parsing within the same bounded resource lease."""
        async with (
            self.budget.lease(
                f"{self.work.name}: {name}",
                memory_mb=memory_mb,
                kind="render" if render else None,
            )
            if self.budget is not None
            else nullcontext()
        ):
            version_work = self.work / name / "version"
            command_work = self.work / name / "command"
            version_work.mkdir(parents=True)
            command_work.mkdir()
            version_args = ["--version"] if argv[0] == "qpdf" else ["-v"]
            try:
                await self.runner.tool_version([argv[0], *version_args], version_work)
                actual = [str(self.pdf) if part == "@PDF@" else part for part in argv]
                result = await self.runner.run(
                    actual,
                    cwd=command_work,
                    workspace=command_work,
                    readonly_inputs=(self.pdf,),
                    memory_mb=memory_mb,
                )
                _require_complete(result, argv[0])
                if len(result.stdout.encode("utf-8")) > self.runner.limits.max_output_bytes:
                    raise PreparationError("PDF measurement exceeds the diagnostic output limit")
                if threaded_parser:
                    return await run_in_thread(parser, result, command_work)
                return parser(result, command_work)
            finally:
                if render:
                    # Remove only this disposable render, including partial output.
                    (command_work / "page.pgm").unlink(missing_ok=True)


async def _session(
    pdf: Path,
    work: Path,
    runner: ToolRunner,
    budget: ResourceBudget | None,
) -> _Session:
    budget = budget or runner.budget
    work, documents = await _new_workspace(work, {"document.pdf": pdf}, runner, budget)
    return _Session(work, documents["document.pdf"], runner, budget)


def _properties(output: str) -> dict[str, str]:
    fields = _fields(output)
    keys = [line.split(":", 1)[0].strip() for line in output.splitlines() if ":" in line]
    if any(keys.count(key) != 1 for key in (*_METADATA_FIELDS, "Pages", "Tagged") if key in keys):
        raise PreparationError("Ambiguous duplicate pdfinfo properties prevent reliable comparison")
    _page_count(fields)
    return fields


async def extract_pdf_evidence(
    pdf: Path,
    work: Path,
    runner: ToolRunner,
    *,
    budget: ResourceBudget | None = None,
) -> tuple[str, dict[str, str]]:
    """Return bounded text/properties for caller-owned privacy or agreement checks.

    Empty pages remain empty evidence, not proof of anonymity or readability.
    No PDF text or metadata is copied into diagnostic messages by this helper.
    """
    session = await _session(pdf, work, runner, budget)
    fields = await session.measure(
        "properties",
        ["pdfinfo", "-enc", "UTF-8", "-isodates", "@PDF@"],
        lambda result, _: _properties(result.stdout),
    )
    pages = _page_count(fields)

    def extract(result: CommandResult, _: Path) -> str:
        if result.stdout.count("\f") != pages:
            raise PreparationError("Text extraction did not report every PDF page boundary")
        return result.stdout

    text = await session.measure(
        "text",
        ["pdftotext", "-enc", "UTF-8", "-layout", "@PDF@", "-"],
        extract,
    )
    return text, fields


def _metadata_findings(fields: dict[str, str], options: PdfCheckOptions) -> list[Finding]:
    result: list[Finding] = []
    if options.required_metadata:
        missing: list[object] = [
            key for key in options.required_metadata if not fields.get(key, "").strip()
        ]
        result.append(
            _result(
                "pdf.required_metadata",
                "Compared nonempty PDF properties with configured required fields.",
                missing,
                required=list(options.required_metadata),
                scope="pdfinfo document properties",
            )
        )
    if options.expected_metadata:

        def normalize(value: str) -> str:
            return " ".join(value.split())

        different: list[object] = [
            key
            for key, expected in options.expected_metadata
            if normalize(fields.get(key, "")) != normalize(expected)
        ]
        result.append(
            _result(
                "pdf.metadata_agreement",
                "Compared PDF properties with supplied expected text.",
                different,
                compared_fields=[key for key, _ in options.expected_metadata],
                normalization="Whitespace only; case, punctuation and author order are retained.",
                scope="Explicit expected text; source macro expansion and identity "
                "are not established.",
            )
        )
    if options.require_tagged:
        value = fields.get("Tagged", "").lower()
        result.append(
            _result(
                "pdf.tagging_presence",
                "Checked the reported presence of PDF tagging.",
                ["Tagged: no"] if value == "no" else [],
                uncertain=value not in {"yes", "no"},
                reported=value or None,
                scope="Tagging presence only; reading order, links, tables and PDF/UA "
                "are not validated.",
            )
        )
    return result


def _section_findings(pages: int, options: PdfCheckOptions) -> list[Finding]:
    result = []
    for section in options.section_budgets:
        actual = section.end_page - section.start_page + 1
        result.append(
            _result(
                "pdf.section_page_budget",
                f"Checked the supplied page range for {section.name}.",
                [actual] if actual > section.max_pages and section.end_page <= pages else [],
                uncertain=section.end_page > pages,
                section=section.name,
                start_page=section.start_page,
                end_page=section.end_page,
                observed_total_pages=pages,
                section_pages=actual if section.end_page <= pages else None,
                maximum=section.max_pages,
                scope="Inclusive user-supplied boundaries; semantic section locations "
                "are not inferred.",
            )
        )
    return result


def _xml(output: str, root_name: str) -> ET.Element:
    if "<!ENTITY" in output.upper() or re.search(r"<!DOCTYPE[^>]*\[", output, re.I):
        raise PreparationError("PDF tool XML contains unsupported entity declarations")
    try:
        root = ET.fromstring(output)
    except (ET.ParseError, RecursionError) as error:
        raise PreparationError("PDF tool returned malformed XML") from error
    if root.tag != root_name:
        raise PreparationError(f"PDF tool did not return a {root_name} XML document")
    if sum(1 for _ in root.iter()) > _MAX_NODES:
        raise PreparationError("PDF XML exceeds the bounded element limit")
    return root


def _finite(value: str | None) -> float:
    try:
        number = float(value) if value is not None else math.nan
    except ValueError as error:
        raise PreparationError("PDF tool returned an invalid numerical measurement") from error
    if not math.isfinite(number):
        raise PreparationError("PDF tool returned an invalid numerical measurement")
    return number


@dataclass(frozen=True)
class _TextBox:
    page: int
    index: int
    box: tuple[float, float, float, float]
    size: float


@dataclass(frozen=True)
class _TextPage:
    page: int
    width: float
    height: float
    boxes: tuple[_TextBox, ...]


def _text_pages(output: str, pages: int) -> list[_TextPage]:
    root = _xml(output, "pdf2xml")
    fonts: dict[str, float] = {}
    result = []
    for page in root.findall("page"):
        try:
            number = int(page.attrib["number"])
        except (KeyError, ValueError) as error:
            raise PreparationError("Text geometry lacks a valid page number") from error
        width, height = _finite(page.get("width")), _finite(page.get("height"))
        if min(width, height) <= 0:
            raise PreparationError("Text geometry contains an invalid page size")
        for font in page.findall("fontspec"):
            key = font.get("id")
            size = _finite(font.get("size"))
            if key is None or size <= 0 or (key in fonts and fonts[key] != size):
                raise PreparationError("Text geometry contains ambiguous font-size identifiers")
            fonts[key] = size
        boxes = []
        for index, node in enumerate(page.findall("text"), 1):
            if not "".join(node.itertext()).strip():
                continue
            left, top = _finite(node.get("left")), _finite(node.get("top"))
            box_width, box_height = _finite(node.get("width")), _finite(node.get("height"))
            if min(box_width, box_height) <= 0 or node.get("font") not in fonts:
                raise PreparationError("Text geometry has unmeasured text size or bounds")
            boxes.append(
                _TextBox(
                    number,
                    index,
                    (left, top, left + box_width, top + box_height),
                    fonts[node.attrib["font"]],
                )
            )
        result.append(_TextPage(number, width, height, tuple(boxes)))
    if [page.page for page in result] != list(range(1, pages + 1)):
        raise PreparationError("Text geometry did not report every page exactly once in order")
    return result


def _geometry_support(output: str, pages: int) -> tuple[set[int], list[tuple[float, float]]]:
    """Reject coordinate frames that the Poppler XML adapter cannot align reliably."""
    rotations: dict[int, int] = {}
    boxes: dict[int, dict[str, tuple[float, ...]]] = {}
    for line in output.splitlines():
        rotation = re.match(r"^Page\s+(?:(\d+)\s+)?rot:\s*(-?\d+)\s*$", line)
        if rotation:
            rotations[int(rotation[1] or "1")] = int(rotation[2])
        box = re.match(r"^(?:Page\s+(\d+)\s+)?(MediaBox|CropBox):\s+(.+)$", line)
        if box:
            values = tuple(_finite(value) for value in box[3].split())
            if len(values) != 4:
                raise PreparationError("Page boxes have an unrecognized coordinate format")
            boxes.setdefault(int(box[1] or "1"), {})[box[2]] = values
    unsupported = set()
    for page in range(1, pages + 1):
        entry = boxes.get(page, {})
        media, crop = entry.get("MediaBox"), entry.get("CropBox")
        if rotations.get(page) != 0 or media is None or crop != media or media[:2] != (0.0, 0.0):
            unsupported.add(page)
    return unsupported, _page_sizes(output, pages)


def _contains(outer: tuple[float, ...], inner: tuple[float, ...], tolerance: float) -> bool:
    return (
        inner[0] >= outer[0] - tolerance
        and inner[1] >= outer[1] - tolerance
        and inner[2] <= outer[2] + tolerance
        and inner[3] <= outer[3] + tolerance
    )


def _intersection(first: tuple[float, ...], second: tuple[float, ...]) -> float:
    return max(0.0, min(first[2], second[2]) - max(first[0], second[0])) * max(
        0.0, min(first[3], second[3]) - max(first[1], second[1])
    )


def _sample(box: _TextBox) -> dict[str, object]:
    return {
        "page": box.page,
        "text_box": box.index,
        "bounds_pt": box.box,
        "reported_size_pt": box.size,
    }


def _layout_findings(
    pages: list[_TextPage],
    geometry: tuple[set[int], list[tuple[float, float]]],
    options: PdfCheckOptions,
) -> list[Finding]:
    unsupported, sizes = geometry
    unsupported = unsupported.copy()
    for page, size in zip(pages, sizes, strict=True):
        if abs(page.width - size[0]) > 1 or abs(page.height - size[1]) > 1:
            unsupported.add(page.page)
    result: list[Finding] = []
    page_numbers = {page.page for page in pages}
    scope = (
        "Extractable Poppler text boxes only. Outlined/raster text, figures, tables, "
        "equations and clipping paths are not identified semantically."
    )
    if options.min_text_size_pt is not None or options.text_size_regions:
        violations: list[object] = []
        uncertain = bool(unsupported)
        measured = 0
        regions_seen: set[int] = set()
        for page in pages:
            if page.page in unsupported:
                continue
            if not page.boxes and options.min_text_size_pt is not None:
                uncertain = True
            for box in page.boxes:
                minimum = options.min_text_size_pt
                selected = []
                for index, region in enumerate(options.text_size_regions):
                    if region.page == page.page and _intersection(region.box, box.box):
                        regions_seen.add(index)
                        if _contains(region.box, box.box, options.geometry_tolerance_pt):
                            selected.append(region.min_text_size_pt)
                        else:
                            uncertain = True
                if selected:
                    minimum = max(value for value in selected if value is not None)
                if minimum is None:
                    continue
                measured += 1
                # Poppler fontspec sizes are integer-rounded even with -noroundcoord.
                if box.size + 0.5 < minimum:
                    violations.append({**_sample(box), "minimum_pt": minimum})
                elif box.size - 0.5 < minimum:
                    uncertain = True
        uncertain |= len(regions_seen) != len(options.text_size_regions) or measured == 0
        result.append(
            _result(
                "pdf.rendered_text_size",
                "Compared reported text sizes with configured minima.",
                violations,
                uncertain=uncertain,
                measured_text_boxes=measured,
                unsupported_pages=sorted(unsupported),
                precision_pt=0.5,
                scope=scope,
            )
        )
    if options.printable_margins_pt is not None or options.printable_regions:
        violations = []
        uncertain = bool(unsupported) or any(
            region.page not in page_numbers for region in options.printable_regions
        )
        measured = 0
        for page in pages:
            if page.page in unsupported:
                continue
            regions = [
                region.box for region in options.printable_regions if region.page == page.page
            ]
            if options.printable_margins_pt is not None and not regions:
                left, top, right, bottom = options.printable_margins_pt
                regions = [(left, top, page.width - right, page.height - bottom)]
            if not regions:
                continue
            if any(
                box[0] >= box[2] or box[1] >= box[3] or box[2] > page.width or box[3] > page.height
                for box in regions
            ):
                uncertain = True
                continue
            if not page.boxes:
                uncertain = True
            for box in page.boxes:
                measured += 1
                if not any(
                    _contains(region, box.box, options.geometry_tolerance_pt) for region in regions
                ):
                    violations.append(_sample(box))
        result.append(
            _result(
                "pdf.printable_text_bounds",
                "Checked text boxes against supplied printable regions.",
                violations,
                uncertain=uncertain or measured == 0,
                advisory=True,
                measured_text_boxes=measured,
                unsupported_pages=sorted(unsupported),
                tolerance_pt=options.geometry_tolerance_pt,
                scope=scope,
            )
        )
    if options.overlap_min_area_ratio is not None:
        overlaps: list[object] = []
        comparisons = measured = overlap_count = 0
        uncertain = bool(unsupported)
        for page in pages:
            if page.page in unsupported:
                continue
            measured += len(page.boxes)
            active: list[_TextBox] = []
            for box in sorted(page.boxes, key=lambda item: item.box[1]):
                active = [other for other in active if other.box[3] > box.box[1]]
                for other in active:
                    comparisons += 1
                    if comparisons > _MAX_PAIR_CHECKS:
                        uncertain = True
                        break
                    area = _intersection(box.box, other.box)
                    smaller = min(
                        (item.box[2] - item.box[0]) * (item.box[3] - item.box[1])
                        for item in (box, other)
                    )
                    if area / smaller >= options.overlap_min_area_ratio:
                        overlap_count += 1
                        if len(overlaps) < _MAX_SAMPLES:
                            overlaps.append(
                                {
                                    "page": page.page,
                                    "text_boxes": [other.index, box.index],
                                    "bounds_pt": [other.box, box.box],
                                    "area_ratio": area / smaller,
                                }
                            )
                if comparisons > _MAX_PAIR_CHECKS:
                    break
                active.append(box)
            if comparisons > _MAX_PAIR_CHECKS:
                break
        result.append(
            _result(
                "pdf.text_overlap",
                "Measured intersections between extractable text boxes.",
                overlaps,
                uncertain=uncertain or measured == 0,
                advisory=True,
                total_violations=overlap_count,
                minimum_area_ratio=options.overlap_min_area_ratio,
                comparisons=comparisons,
                scope=scope
                + " Overlaps are candidates, including legitimate mathematical overlays.",
            )
        )
    return result


def _gray_raster(path: Path, limit: int) -> tuple[int, int, bytes]:
    if not path.is_file() or path.is_symlink() or path.stat().st_size > limit:
        raise PreparationError("A complete bounded grayscale render was not produced")
    data = path.read_bytes()
    header = re.match(rb"P5\s+(\d+)\s+(\d+)\s+255(?:\r\n|\s)", data)
    if header is None:
        raise PreparationError("Renderer did not produce supported 8-bit grayscale PGM data")
    width, height = int(header[1]), int(header[2])
    if min(width, height) < 1 or width * height > _MAX_PIXELS:
        raise PreparationError("Grayscale render exceeds the safe pixel limit")
    pixels = data[header.end() :]
    if len(pixels) != width * height:
        raise PreparationError("Grayscale render is truncated or contains unexpected trailing data")
    return width, height, pixels


async def _sparse_findings(
    session: _Session, pages: int, options: PdfCheckOptions
) -> list[Finding]:
    result = []
    try:
        _require_rendered_pages(pages, "Rendered sparse-page measurement")
        sizes = await session.measure(
            "raster-sizes",
            ["pdfinfo", "-box", "-f", "1", "-l", str(pages), "@PDF@"],
            lambda execution, _: _page_sizes(execution.stdout, pages),
        )
    except (PreparationError, OSError) as error:
        return [_unavailable("pdf.sparse_pages", error, advisory=True)]
    if any(page > pages for page in options.sparse_page_exemptions):
        result.append(
            _unavailable(
                "pdf.sparse_pages",
                PreparationError("A configured sparse-page exemption is outside the document"),
                advisory=True,
            )
        )
    for page in range(1, pages + 1):
        if page in options.sparse_page_exemptions:
            continue
        expected = tuple(math.ceil(size * options.raster_dpi / 72) for size in sizes[page - 1])
        if math.prod(expected) > _MAX_PIXELS or max(expected) > 4000:
            result.append(
                replace(
                    _unavailable(
                        "pdf.sparse_pages",
                        PreparationError(
                            "Page exceeds the bounded raster dimensions at configured DPI"
                        ),
                        advisory=True,
                    ),
                    details={"page": page},
                )
            )
            continue

        def measure(
            _execution: CommandResult,
            directory: Path,
            expected: tuple[int, ...] = expected,
        ) -> tuple[int, int, float]:
            width, height, pixels = _gray_raster(
                directory / "page.pgm", min(session.runner.limits.max_file_bytes, _MAX_PIXELS + 100)
            )
            if not (
                all(
                    abs(actual - target) <= 1
                    for actual, target in zip((width, height), expected, strict=True)
                )
                or all(
                    abs(actual - target) <= 1
                    for actual, target in zip((height, width), expected, strict=True)
                )
            ):
                raise PreparationError("Rendered dimensions disagree with the page-size preflight")
            ink = sum(
                abs(value - options.background_gray) >= options.ink_difference for value in pixels
            )
            return width, height, ink / len(pixels)

        try:
            width, height, ratio = await session.measure(
                f"raster-{page}",
                [
                    "pdftoppm",
                    "-f",
                    str(page),
                    "-l",
                    str(page),
                    "-singlefile",
                    "-gray",
                    "-r",
                    str(options.raster_dpi),
                    "@PDF@",
                    "page",
                ],
                measure,
                render=True,
                memory_mb=128,
            )
            result.append(
                _result(
                    "pdf.sparse_pages",
                    f"Measured rendered ink coverage on page {page}.",
                    [ratio]
                    if options.min_page_ink_ratio is not None and ratio < options.min_page_ink_ratio
                    else [],
                    advisory=True,
                    page=page,
                    last_page=page == pages,
                    ink_ratio=ratio,
                    minimum=options.min_page_ink_ratio,
                    raster_pixels=[width, height],
                    background_gray=options.background_gray,
                    ink_difference=options.ink_difference,
                    raster_dpi=options.raster_dpi,
                    scope="Rendered pixel coverage; intentional blank/image-only pages "
                    "are not inferred. "
                    "Pages above four million pixels are inconclusive before rendering.",
                )
            )
        except (PreparationError, OSError) as error:
            result.append(
                replace(
                    _unavailable("pdf.sparse_pages", error, advisory=True), details={"page": page}
                )
            )
    return result


class _Objects:
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
            if (
                not isinstance(self.pages, list)
                or len(self.pages) != pages
                or len(self.objects) > _MAX_NODES
            ):
                raise ValueError("incomplete page/object data")
        except (ValueError, KeyError, TypeError, RecursionError) as error:
            raise PreparationError(
                "qpdf did not return complete supported JSON v2 object data"
            ) from error
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
        while isinstance(value, str) and re.fullmatch(r"\d+ \d+ R", value):
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
            if visited > _MAX_NODES or depth > 100:
                raise PreparationError("qpdf object graph exceeds the bounded traversal limit")
            if isinstance(value, str) and re.fullmatch(r"\d+ \d+ R", value):
                if value in references:
                    continue
                references.add(value)
                value = self.resolve(value)
            if isinstance(value, dict):
                result.append(value)
                pending.extend((child, depth + 1) for child in value.values())
            elif isinstance(value, list):
                pending.extend((child, depth + 1) for child in value)
            if len(pending) > _MAX_NODES:
                raise PreparationError("qpdf object graph exceeds the bounded traversal limit")
        return result


def _object_measurements(objects: _Objects, options: PdfCheckOptions) -> list[Finding]:
    result = []
    if options.require_language or options.expected_language is not None:
        raw = objects.resolve(objects.root.get("/Lang"))
        language = raw[2:].strip() if isinstance(raw, str) and raw.startswith("u:") else None
        uncertain = raw is not None and language is None
        violations: list[object] = []
        if raw is None or language == "":
            violations.append("Missing nonempty catalog language")
        elif (
            language is not None
            and options.expected_language is not None
            and language.casefold() != options.expected_language.casefold()
        ):
            violations.append("Catalog language differs from configured language")
        result.append(
            _result(
                "pdf.document_language",
                "Checked the catalog language declaration.",
                violations,
                uncertain=uncertain,
                declared_language=language,
                expected=options.expected_language,
                scope="Declaration presence/exact language-tag agreement, "
                "not the language of the prose.",
            )
        )
    if options.forbidden_annotation_types:
        annotations: list[object] = []
        count = 0
        for page_number, page in enumerate(objects.pages, 1):
            if not isinstance(page, dict) or "object" not in page:
                raise PreparationError("qpdf page inventory lacks a page object reference")
            page_object = objects.dictionary(page["object"])
            values = objects.resolve(page_object.get("/Annots", []))
            if not isinstance(values, list):
                raise PreparationError("qpdf page annotation inventory is malformed")
            for value in values:
                annotation = objects.dictionary(value)
                subtype = objects.resolve(annotation.get("/Subtype"))
                if not isinstance(subtype, str) or not subtype.startswith("/"):
                    raise PreparationError("An annotation has no measurable subtype")
                count += 1
                if count > _MAX_NODES:
                    raise PreparationError("Page annotations exceed the bounded traversal limit")
                if subtype[1:] in options.forbidden_annotation_types:
                    annotations.append({"page": page_number, "type": subtype[1:]})
        result.append(
            _result(
                "pdf.annotation_policy",
                "Inspected page annotation object subtypes.",
                annotations,
                annotation_count=count,
                forbidden_types=list(options.forbidden_annotation_types),
                scope="Page Annots arrays; content-stream drawings and inaccessible "
                "orphan objects excluded.",
            )
        )
    if options.forbidden_action_types:
        actions: list[object] = []
        # A matching /S in reachable objects is intentionally conservative: qpdf
        # resolves compressed dictionaries but does not classify executable semantics.
        for node in objects.reachable_dictionaries():
            subtype = objects.resolve(node.get("/S"))
            if (
                isinstance(subtype, str)
                and subtype.startswith("/")
                and subtype[1:] in options.forbidden_action_types
            ):
                actions.append(subtype[1:])
        result.append(
            _result(
                "pdf.action_policy",
                "Scanned reachable object dictionaries for configured action names.",
                actions,
                advisory=True,
                forbidden_types=list(options.forbidden_action_types),
                scope="Reachable /S names; a match is an action candidate requiring "
                "context review. "
                "Opaque streams and unreferenced objects are excluded; no malware-free claim.",
            )
        )
    return result


def _object_findings(output: str, pages: int, options: PdfCheckOptions) -> list[Finding]:
    objects = _Objects(output, pages)
    # Preserve completed observations if another part of the object graph is incomplete.
    selections = (
        (
            "pdf.document_language",
            replace(options, forbidden_annotation_types=(), forbidden_action_types=()),
        ),
        (
            "pdf.annotation_policy",
            replace(
                options, require_language=False, expected_language=None, forbidden_action_types=()
            ),
        ),
        (
            "pdf.action_policy",
            replace(
                options,
                require_language=False,
                expected_language=None,
                forbidden_annotation_types=(),
            ),
        ),
    )
    result = []
    for rule, selected in selections:
        if rule not in _configured(selected):
            continue
        try:
            result.extend(_object_measurements(objects, selected))
        except PreparationError as error:
            result.append(_unavailable(rule, error, advisory=rule == "pdf.action_policy"))
    return result


def _simple_rgb(node: ET.Element) -> tuple[float, float, float] | None:
    """Interpret only opaque DeviceRGB/Gray values under an explicit sRGB assumption."""
    if _finite(node.get("alpha", "1")) != 1:
        return None
    values = tuple(_finite(value) for value in node.get("color", "").split())
    if node.get("colorspace") == "DeviceGray" and len(values) == 1:
        values = values * 3
    elif node.get("colorspace") != "DeviceRGB":
        return None
    if len(values) != 3 or any(value < 0 or value > 1 for value in values):
        return None
    return values[0], values[1], values[2]


def _luminance(rgb: tuple[float, float, float]) -> float:
    linear = [
        value / 12.92 if value <= 0.04045 else ((value + 0.055) / 1.055) ** 2.4 for value in rgb
    ]
    return sum(
        value * weight for value, weight in zip(linear, (0.2126, 0.7152, 0.0722), strict=True)
    )


def _contrast_findings(root: ET.Element, options: PdfCheckOptions) -> list[Finding]:
    contrast: list[object] = []
    separation: list[object] = []
    contrast_count = palette_count = pairs = separation_count = 0
    contrast_uncertain = separation_uncertain = any(
        node.tag in {"group", "mask", "tile"} for node in root.iter()
    )
    for page in root.findall("page"):
        page_number = int(page.attrib["number"])
        palette: set[tuple[float, float, float]] = set()
        for node in page.iter():
            if options.min_text_contrast is not None and node.tag in {"fill_text", "stroke_text"}:
                foreground = _simple_rgb(node)
                if foreground is None or not node.findall(".//g"):
                    contrast_uncertain = True
                elif options.contrast_background_rgb is not None:
                    contrast_count += 1
                    fg, bg = _luminance(foreground), _luminance(options.contrast_background_rgb)
                    ratio = (max(fg, bg) + 0.05) / (min(fg, bg) + 0.05)
                    if ratio < options.min_text_contrast:
                        contrast.append(
                            {
                                "page": page_number,
                                "foreground_rgb": foreground,
                                "contrast_ratio": ratio,
                            }
                        )
            if options.min_grayscale_luminance_difference is not None and node.tag in {
                "fill_text",
                "stroke_text",
                "fill_path",
                "stroke_path",
                "fill_image",
                "fill_image_mask",
                "fill_shade",
            }:
                rgb = _simple_rgb(node)
                if rgb is None:
                    separation_uncertain = True
                elif len(palette) < 256 or rgb in palette:
                    palette.add(rgb)
                else:
                    separation_uncertain = True
        colors = sorted(palette)
        palette_count += len(colors)
        for index, first in enumerate(colors):
            for second in colors[index + 1 :]:
                pairs += 1
                if pairs > _MAX_PAIR_CHECKS:
                    separation_uncertain = True
                    break
                difference = abs(_luminance(first) - _luminance(second))
                if (
                    options.min_grayscale_luminance_difference is not None
                    and difference < options.min_grayscale_luminance_difference
                ):
                    separation_count += 1
                    if len(separation) < _MAX_SAMPLES:
                        separation.append(
                            {
                                "page": page_number,
                                "colors_rgb": [first, second],
                                "luminance_difference": difference,
                            }
                        )
            if pairs > _MAX_PAIR_CHECKS:
                break
    result = []
    if options.min_text_contrast is not None:
        result.append(
            _result(
                "pdf.assumed_text_contrast",
                "Compared opaque text colors with the supplied background.",
                contrast,
                uncertain=contrast_uncertain or contrast_count == 0,
                advisory=True,
                measured_text_operations=contrast_count,
                minimum_ratio=options.min_text_contrast,
                assumed_background_rgb=options.contrast_background_rgb,
                scope="DeviceRGB/Gray treated as sRGB on a user-asserted uniform background. "
                "Actual backgrounds, raster/outlined text, alpha blending, soft masks, "
                "ICC conversion and semantic accessibility are not established.",
            )
        )
    if options.min_grayscale_luminance_difference is not None:
        result.append(
            _result(
                "pdf.grayscale_separation",
                "Compared distinct observed page colors by nominal luminance.",
                separation,
                uncertain=separation_uncertain or palette_count == 0,
                advisory=True,
                total_violations=separation_count,
                page_palette_entries=palette_count,
                compared_pairs=min(pairs, _MAX_PAIR_CHECKS),
                minimum_difference=options.min_grayscale_luminance_difference,
                scope="Opaque DeviceRGB/Gray colors treated as sRGB. A close pair is advisory; "
                "semantic color-only encoding, visual proximity and perceptual discrimination "
                "are not inferred. Raster colors, ICC conversion and compositing are unmeasured.",
            )
        )
    return result


def _trace_findings(output: str, pages: int, options: PdfCheckOptions) -> list[Finding]:
    root = _xml(output, "document")
    traced_pages = root.findall("page")
    try:
        numbers = [int(page.attrib["number"]) for page in traced_pages]
    except (ValueError, KeyError) as error:
        raise PreparationError("MuPDF trace contains an invalid page number") from error
    if numbers != list(range(1, pages + 1)):
        raise PreparationError("MuPDF trace did not report every page exactly once")
    strokes: list[object] = []
    colors: list[object] = []
    stroke_uncertain = color_uncertain = False
    stroke_count = color_count = 0
    color_ops = {
        "fill_path",
        "stroke_path",
        "fill_text",
        "stroke_text",
        "fill_image",
        "fill_image_mask",
        "fill_shade",
    }
    for page in traced_pages:
        number = int(page.attrib["number"])
        for node in page.iter():
            if options.min_stroke_width_pt is not None and node.tag in {
                "stroke_path",
                "stroke_text",
            }:
                stroke_count += 1
                width = _finite(node.get("linewidth"))
                transform = [_finite(value) for value in node.get("transform", "").split()]
                if len(transform) != 6 or width < 0:
                    raise PreparationError("MuPDF stroke measurement lacks a valid transform/width")
                a, b, c, d, _, _ = transform
                scale_x, scale_y = math.hypot(a, b), math.hypot(c, d)
                uniform = math.isclose(
                    scale_x, scale_y, rel_tol=1e-6, abs_tol=1e-8
                ) and math.isclose(a * c + b * d, 0, abs_tol=max(1, scale_x * scale_y) * 1e-6)
                # Hairlines depend on output device; anisotropic stroke width
                # depends on path direction. Do not treat either as measured zero.
                if not uniform or width == 0 or scale_x == 0:
                    stroke_uncertain = True
                elif width * scale_x < options.min_stroke_width_pt:
                    strokes.append(
                        {
                            "page": number,
                            "effective_width_pt": width * scale_x,
                            "source_width": width,
                            "transform": transform,
                        }
                    )
            if options.allowed_color_spaces and node.tag in color_ops:
                space = node.get("colorspace")
                if space is None:
                    color_uncertain = True
                    continue
                color_count += 1
                if space not in options.allowed_color_spaces:
                    colors.append({"page": number, "operation": node.tag, "color_space": space})
    result = []
    if options.min_stroke_width_pt is not None:
        result.append(
            _result(
                "pdf.stroke_width",
                "Compared uniformly transformed stroke widths with the supplied minimum.",
                strokes,
                uncertain=stroke_uncertain or stroke_count == 0,
                advisory=True,
                stroke_count=stroke_count,
                minimum_pt=options.min_stroke_width_pt,
                scope="Stroke operators including placement scale; raster lines "
                "and filled outlines "
                "are excluded. Hairlines and nonuniform transforms are unmeasured.",
            )
        )
    if options.allowed_color_spaces:
        result.append(
            _result(
                "pdf.color_space_policy",
                "Compared rendered operation color-space names with the supplied list.",
                colors,
                uncertain=color_uncertain or color_count == 0,
                measured_operations=color_count,
                allowed=list(options.allowed_color_spaces),
                scope="MuPDF rendering color-space names; source encoding, contrast, color-only "
                "meaning and perceptual accessibility are not established.",
            )
        )
    result.extend(_contrast_findings(root, options))
    return result


async def inspect_pdf_details(
    pdf: Path,
    work: Path,
    runner: ToolRunner,
    *,
    options: PdfCheckOptions | None = None,
    budget: ResourceBudget | None = None,
) -> list[Finding]:
    """Run only configured checks; unsupported or partial evidence is inconclusive."""
    options = options or PdfCheckOptions()
    configured = _configured(options)
    if not configured:
        return []
    try:
        session = await _session(pdf, work, runner, budget)
        fields = await session.measure(
            "properties",
            ["pdfinfo", "-enc", "UTF-8", "-isodates", "@PDF@"],
            lambda execution, _: _properties(execution.stdout),
        )
        pages = _page_count(fields)
    except (PreparationError, OSError) as error:
        return [_unavailable(rule, error) for rule in configured]
    result = [*_metadata_findings(fields, options), *_section_findings(pages, options)]
    layout_rules = [
        rule
        for rule in configured
        if rule
        in {
            "pdf.rendered_text_size",
            "pdf.printable_text_bounds",
            "pdf.text_overlap",
        }
    ]
    if layout_rules:
        try:
            unsupported = await session.measure(
                "page-boxes",
                ["pdfinfo", "-box", "-f", "1", "-l", str(pages), "@PDF@"],
                lambda execution, _: _geometry_support(execution.stdout, pages),
            )
            # -i excludes image extraction, -nomerge preserves smaller text boxes.
            layout = await session.measure(
                "text-geometry",
                [
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
                    "@PDF@",
                ],
                lambda execution, _: _layout_findings(
                    _text_pages(execution.stdout, pages), unsupported, options
                ),
            )
            result.extend(layout)
        except (PreparationError, OSError) as error:
            result.extend(_unavailable(rule, error) for rule in layout_rules)
    if options.min_page_ink_ratio is not None:
        result.extend(await _sparse_findings(session, pages, options))
    object_rules = [
        rule
        for rule in configured
        if rule
        in {
            "pdf.document_language",
            "pdf.annotation_policy",
            "pdf.action_policy",
        }
    ]
    if object_rules:
        try:
            result.extend(
                await session.measure(
                    "objects",
                    [
                        "qpdf",
                        "--json=2",
                        "--json-key=qpdf",
                        "--json-key=pages",
                        "--json-stream-data=none",
                        "@PDF@",
                    ],
                    lambda execution, _: _object_findings(execution.stdout, pages, options),
                )
            )
        except (PreparationError, OSError) as error:
            result.extend(_unavailable(rule, error) for rule in object_rules)
    trace_rules = [
        rule
        for rule in configured
        if rule
        in {
            "pdf.stroke_width",
            "pdf.color_space_policy",
            "pdf.assumed_text_contrast",
            "pdf.grayscale_separation",
        }
    ]
    if trace_rules:
        try:
            result.extend(
                await session.measure(
                    "trace",
                    ["mutool", "trace", "@PDF@", f"1-{pages}"],
                    lambda execution, _: _trace_findings(execution.stdout, pages, options),
                )
            )
        except (PreparationError, OSError) as error:
            result.extend(_unavailable(rule, error) for rule in trace_rules)
    return result


async def inspect_included_pdf_figures(
    figures: tuple[Path, ...],
    work: Path,
    runner: ToolRunner,
    *,
    options: PdfCheckOptions | None = None,
    budget: ResourceBudget | None = None,
) -> list[Finding]:
    """Inspect explicit source figure files without claiming final-PDF object attribution."""
    figure_options = options or PdfCheckOptions()
    if not (
        figure_options.require_embedded_figure_fonts or figure_options.forbid_type3_figure_fonts
    ):
        return []
    if (
        not isinstance(figures, tuple)
        or len(figures) > 300
        or any(not isinstance(figure, Path) for figure in figures)
    ):
        raise PreparationError("Included PDF figure inspection requires at most 300 explicit paths")
    if work.exists():
        raise PreparationError("Included figure inspection requires a new workspace")
    result = []
    for index, figure in enumerate(dict.fromkeys(figures), 1):
        try:
            session = await _session(figure, work / str(index), runner, budget)

            def fonts(execution: CommandResult, _directory: Path) -> Finding:
                inventory = _font_inventory(execution.stdout)
                violations: list[object] = [
                    font
                    for font in inventory
                    if (figure_options.require_embedded_figure_fonts and not font["embedded"])
                    or (figure_options.forbid_type3_figure_fonts and _is_type3(font["type"]))
                ]
                # A figure without font resources (outlined or raster-only text) cannot
                # violate an embedding or Type 3 policy.
                return _result(
                    "pdf.included_figure_fonts",
                    "Inspected font resources in an explicit input PDF figure."
                    if inventory
                    else "No font resources in this figure; nothing to embed.",
                    violations,
                    font_count=len(inventory),
                    require_embedded=figure_options.require_embedded_figure_fonts,
                    forbid_type3=figure_options.forbid_type3_figure_fonts,
                    scope="Input-figure font resources; final-PDF attribution is not established. "
                    "Type 3 does not imply bitmap glyphs.",
                )

            finding = await session.measure("fonts", ["pdffonts", "@PDF@"], fonts)
        except (PreparationError, OSError) as error:
            finding = _unavailable("pdf.included_figure_fonts", error)
        result.append(replace(finding, path=str(figure)))
    return result
