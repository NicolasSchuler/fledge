"""Optional, bounded PDF measurements with explicit evidence boundaries.

Coordinates are PDF points from the top-left of an unrotated, uncropped page.
Poppler text boxes do not describe images, outlines, clipping paths or semantics.
qpdf JSON supplies object structure; MuPDF trace supplies drawing operations.
Neither adapter asserts reading order, description quality or full accessibility.
"""

from __future__ import annotations

import math
import re
import xml.etree.ElementTree as ET
from collections.abc import Awaitable, Callable, Collection, Iterator
from dataclasses import dataclass, replace
from functools import partial
from pathlib import Path
from typing import ClassVar

from .errors import PreparationError, SettingsError
from .findings import Finding, Severity
from .option_config import (
    NONNEGATIVE_NUMBER,
    NONZERO_FRACTION,
    POSITIVE_INTEGER,
    POSITIVE_NUMBER,
    Bounds,
    TextFormat,
    require_choice,
    require_integers,
    require_numbers,
    require_pairs,
    require_records,
    require_text,
    require_unique_strings,
    validate_scalars,
)
from .pdf_support import (
    COLOR_OPERATIONS,
    CONTRAST_RATIO_BOUNDS,
    MAX_FIGURE_INPUTS,
    MAX_MEASUREMENT_DIMENSION,
    MAX_MEASUREMENT_PIXELS,
    MAX_PAIR_CHECKS,
    MAX_PARSED_NODES,
    MAX_REGIONS_PER_POLICY,
    MAX_SAMPLES,
    METADATA_FIELDS,
    RASTER_SAMPLING_BOUNDS,
    PageFrames,
    PdfPreflight,
    PdfRegion,
    PdfToolSession,
    QpdfObjectGraph,
    contains,
    evaluate_font_policy,
    exceeds_measurement_raster,
    finite_number,
    font_inventory,
    font_inventory_argv,
    heuristic_finding,
    intersection_area,
    layout_text_argv,
    measurement_finding,
    measurement_raster_size,
    mupdf_trace_argv,
    open_pdf_session,
    page_boxes_argv,
    page_frames,
    parse_mupdf_trace,
    parse_page_sizes,
    parse_tool_xml,
    preflight_pdf,
    qpdf_objects_argv,
    read_measurement_raster,
    relative_luminance,
    require_rendered_pages,
    split_text_pages,
    text_geometry_argv,
    unavailable_finding,
)
from .raster_pixels import InkMask
from .resource_budget import ResourceBudget
from .runtime import CommandResult, CommandRunner

# Rules measured from one evidence source; required_pdf_check_tools maps them to tools.
TEXT_GEOMETRY_RULES = frozenset(
    {"pdf.rendered_text_size", "pdf.printable_text_bounds", "pdf.text_overlap"}
)
OBJECT_STRUCTURE_RULES = frozenset(
    {"pdf.document_language", "pdf.annotation_policy", "pdf.action_policy"}
)
PAINT_TRACE_RULES = frozenset(
    {
        "pdf.stroke_width",
        "pdf.color_space_policy",
        "pdf.assumed_text_contrast",
        "pdf.grayscale_separation",
    }
)
# A PDF name as /Name is written, without the slash: annotation subtypes and action types.
_PDF_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]*")
_LAYOUT_SCOPE = (
    "Extractable Poppler text boxes only. Outlined/raster text, figures, tables, "
    "equations and clipping paths are not identified semantically."
)


@dataclass(frozen=True)
class PdfSectionBudget:
    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        "start_page": POSITIVE_INTEGER,
        "end_page": POSITIVE_INTEGER,
        "max_pages": POSITIVE_INTEGER,
    }

    name: str
    start_page: int
    end_page: int
    max_pages: int

    def __post_init__(self) -> None:
        require_text("name", self.name)
        validate_scalars(self)
        if self.start_page > self.end_page:
            raise SettingsError("end_page must not be before start_page")


@dataclass(frozen=True)
class PdfCheckOptions:
    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        **RASTER_SAMPLING_BOUNDS,
        "min_text_size_pt": POSITIVE_NUMBER,
        "geometry_tolerance_pt": NONNEGATIVE_NUMBER,
        "overlap_min_area_ratio": NONZERO_FRACTION,
        "min_page_ink_ratio": NONZERO_FRACTION,
        "min_grayscale_luminance_difference": NONZERO_FRACTION,
        "min_stroke_width_pt": POSITIVE_NUMBER,
        "min_text_contrast": CONTRAST_RATIO_BOUNDS,
    }

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
        validate_scalars(self)
        self._validate_regions()
        self._validate_geometry()
        self._validate_contrast()
        self._validate_metadata()
        self._validate_names()

    def _validate_regions(self) -> None:
        require_records(
            "section_budgets",
            self.section_budgets,
            PdfSectionBudget,
            max_items=MAX_REGIONS_PER_POLICY,
        )
        for name in ("text_size_regions", "printable_regions"):
            require_records(name, getattr(self, name), PdfRegion, max_items=MAX_REGIONS_PER_POLICY)
        if any(region.min_text_size_pt is None for region in self.text_size_regions):
            raise SettingsError(
                "Every text_size_regions entry needs min_text_size_pt", ("text_size_regions",)
            )

    def _validate_geometry(self) -> None:
        require_numbers(
            "printable_margins_pt",
            self.printable_margins_pt,
            count=4,
            minimum=0,
            description="(left, top, right, bottom)",
            allow_none=True,
        )
        require_integers("sparse_page_exemptions", self.sparse_page_exemptions, minimum=1)

    def _validate_contrast(self) -> None:
        require_numbers(
            "contrast_background_rgb",
            self.contrast_background_rgb,
            count=3,
            minimum=0,
            maximum=1,
            description="(red, green, blue)",
            allow_none=True,
        )
        if self.min_text_contrast is not None and self.contrast_background_rgb is None:
            raise SettingsError(
                "min_text_contrast requires contrast_background_rgb",
                ("min_text_contrast", "contrast_background_rgb"),
            )

    def _validate_metadata(self) -> None:
        require_unique_strings("required_metadata", self.required_metadata, choices=METADATA_FIELDS)
        require_pairs(
            "expected_metadata",
            self.expected_metadata,
            first=partial(require_choice, choices=sorted(METADATA_FIELDS)),
            second=require_text,
        )
        require_text("expected_language", self.expected_language, allow_none=True)

    def _validate_names(self) -> None:
        for name in ("forbidden_annotation_types", "forbidden_action_types"):
            require_unique_strings(
                name,
                getattr(self, name),
                item_format=TextFormat(_PDF_NAME, "a PDF name without a leading slash"),
            )
        require_unique_strings(
            "allowed_color_spaces", self.allowed_color_spaces, allow_surrounding_whitespace=True
        )


def configured_pdf_check_rules(options: PdfCheckOptions) -> list[str]:
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


def figure_font_checks_enabled(options: PdfCheckOptions) -> bool:
    """Input-figure font policies need the source graph's included PDF figures."""
    return options.require_embedded_figure_fonts or options.forbid_type3_figure_fonts


def required_pdf_check_tools(options: PdfCheckOptions) -> set[str]:
    """The external tools the configured measurements run, for setup diagnostics."""
    rules = set(configured_pdf_check_rules(options))
    tools = {"pdfinfo"} if rules else set()
    if rules & TEXT_GEOMETRY_RULES:
        tools.add("pdftohtml")
    if rules & OBJECT_STRUCTURE_RULES:
        tools.add("qpdf")
    if rules & PAINT_TRACE_RULES:
        tools.add("mutool")
    if options.min_page_ink_ratio is not None:
        tools.add("pdftoppm")
    if figure_font_checks_enabled(options):
        tools.add("pdffonts")
    return tools


async def extract_pdf_evidence(
    pdf: Path,
    work: Path,
    runner: CommandRunner,
    *,
    budget: ResourceBudget | None = None,
) -> tuple[str, dict[str, str]]:
    """Return bounded text/properties for caller-owned privacy or agreement checks.

    Empty pages remain empty evidence, not proof of anonymity or readability.
    No PDF text or metadata is copied into diagnostic messages by this helper.
    """
    preflight = await PdfPreflight.open(pdf, work, runner, budget)

    def extract(result: CommandResult) -> str:
        # Only the page boundaries are validated; the privacy scan reads the text as extracted.
        split_text_pages(result.stdout, preflight.pages)
        return result.stdout

    text = await preflight.session.measure("text", layout_text_argv(), extract)
    return text, preflight.fields


def _metadata_findings(fields: dict[str, str], options: PdfCheckOptions) -> list[Finding]:
    result: list[Finding] = []
    if options.required_metadata:
        missing: list[object] = [
            key for key in options.required_metadata if not fields.get(key, "").strip()
        ]
        result.append(
            measurement_finding(
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
            measurement_finding(
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
            measurement_finding(
                "pdf.tagging_presence",
                "Checked the reported presence of PDF tagging.",
                ["Tagged: no"] if value == "no" else [],
                incomplete=value not in {"yes", "no"},
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
        outside = section.end_page > pages
        over = not outside and actual > section.max_pages
        span = (
            f"{section.name} spans {actual} pages (pages {section.start_page}-{section.end_page})"
        )
        if outside:
            message = (
                f"{section.name} is configured to end at page {section.end_page}, but the PDF "
                f"has only {pages} pages; correct the section's page range to check its budget."
            )
        elif over:
            message = f"{span}, over its budget of {section.max_pages}."
        else:
            message = f"{span}; the budget is {section.max_pages}."
        reason = f"{section.name}: {actual} pages against a budget of {section.max_pages}"
        violations: list[object] = (
            [
                {
                    "section": section.name,
                    "pages": actual,
                    "maximum": section.max_pages,
                    "reason": reason,
                }
            ]
            if over
            else []
        )
        result.append(
            measurement_finding(
                "pdf.section_page_budget",
                message,
                violations,
                incomplete=outside,
                section=section.name,
                start_page=section.start_page,
                end_page=section.end_page,
                observed_total_pages=pages,
                section_pages=None if outside else actual,
                maximum=section.max_pages,
                scope="Inclusive user-supplied boundaries; semantic section locations "
                "are not inferred.",
            )
        )
    return result


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
    root = parse_tool_xml(output, "pdf2xml")
    fonts: dict[str, float] = {}
    result = []
    for page in root.findall("page"):
        try:
            number = int(page.attrib["number"])
        except (KeyError, ValueError) as error:
            raise PreparationError("Text geometry lacks a valid page number") from error
        width, height = finite_number(page.get("width")), finite_number(page.get("height"))
        if min(width, height) <= 0:
            raise PreparationError("Text geometry contains an invalid page size")
        for font in page.findall("fontspec"):
            key = font.get("id")
            size = finite_number(font.get("size"))
            if key is None or size <= 0 or (key in fonts and fonts[key] != size):
                raise PreparationError("Text geometry contains ambiguous font-size identifiers")
            fonts[key] = size
        boxes = []
        for index, node in enumerate(page.findall("text"), 1):
            if not "".join(node.itertext()).strip():
                continue
            left, top = finite_number(node.get("left")), finite_number(node.get("top"))
            box_width, box_height = (
                finite_number(node.get("width")),
                finite_number(node.get("height")),
            )
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


def _text_box_sample(box: _TextBox) -> dict[str, object]:
    return {
        "page": box.page,
        "text_box": box.index,
        "bounds_pt": box.box,
        "reported_size_pt": box.size,
    }


@dataclass(frozen=True, slots=True)
class _TextLayout:
    """Extracted text pages and the pages whose coordinate frame cannot be aligned."""

    pages: tuple[_TextPage, ...]
    unsupported: frozenset[int]
    options: PdfCheckOptions

    def measurable_pages(self) -> Iterator[_TextPage]:
        return (page for page in self.pages if page.page not in self.unsupported)


def _text_size_finding(layout: _TextLayout) -> Finding:
    options = layout.options
    violations: list[object] = []
    uncertain = bool(layout.unsupported)
    measured = 0
    regions_seen: set[int] = set()
    for page in layout.measurable_pages():
        if not page.boxes and options.min_text_size_pt is not None:
            uncertain = True
        for box in page.boxes:
            minimum = options.min_text_size_pt
            selected = []
            for index, region in enumerate(options.text_size_regions):
                if region.page == page.page and intersection_area(region.box, box.box):
                    regions_seen.add(index)
                    if contains(region.box, box.box, options.geometry_tolerance_pt):
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
                violations.append({**_text_box_sample(box), "minimum_pt": minimum})
            elif box.size - 0.5 < minimum:
                uncertain = True
    uncertain |= len(regions_seen) != len(options.text_size_regions) or measured == 0
    return measurement_finding(
        "pdf.rendered_text_size",
        "Compared reported text sizes with configured minima.",
        violations,
        incomplete=uncertain,
        measured_text_boxes=measured,
        unsupported_pages=sorted(layout.unsupported),
        precision_pt=0.5,
        scope=_LAYOUT_SCOPE,
    )


def _printable_regions(page: _TextPage, options: PdfCheckOptions) -> list[tuple[float, ...]]:
    regions: list[tuple[float, ...]] = [
        region.box for region in options.printable_regions if region.page == page.page
    ]
    if options.printable_margins_pt is not None and not regions:
        left, top, right, bottom = options.printable_margins_pt
        regions = [(left, top, page.width - right, page.height - bottom)]
    return regions


def _printable_bounds_finding(layout: _TextLayout) -> Finding:
    options = layout.options
    violations: list[object] = []
    page_numbers = {page.page for page in layout.pages}
    uncertain = bool(layout.unsupported) or any(
        region.page not in page_numbers for region in options.printable_regions
    )
    measured = 0
    for page in layout.measurable_pages():
        regions = _printable_regions(page, options)
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
                contains(region, box.box, options.geometry_tolerance_pt) for region in regions
            ):
                violations.append(_text_box_sample(box))
    return heuristic_finding(
        "pdf.printable_text_bounds",
        "Checked text boxes against supplied printable regions.",
        violations,
        incomplete=uncertain or measured == 0,
        measured_text_boxes=measured,
        unsupported_pages=sorted(layout.unsupported),
        tolerance_pt=options.geometry_tolerance_pt,
        scope=_LAYOUT_SCOPE,
    )


def _vertically_overlapping_pairs(
    layout: _TextLayout,
) -> Iterator[tuple[int, _TextBox, _TextBox]]:
    """Sweep each page top to bottom, pairing boxes whose vertical extents overlap."""
    for page in layout.measurable_pages():
        active: list[_TextBox] = []
        for box in sorted(page.boxes, key=lambda item: item.box[1]):
            active = [other for other in active if other.box[3] > box.box[1]]
            for other in active:
                yield page.page, other, box
            active.append(box)


def _overlap_finding(layout: _TextLayout, minimum_ratio: float) -> Finding:
    overlaps: list[object] = []
    comparisons = overlap_count = 0
    uncertain = bool(layout.unsupported)
    measured = sum(len(page.boxes) for page in layout.measurable_pages())
    for comparisons, (page, other, box) in enumerate(_vertically_overlapping_pairs(layout), 1):
        if comparisons > MAX_PAIR_CHECKS:
            uncertain = True
            break
        area = intersection_area(box.box, other.box)
        smaller = min(
            (item.box[2] - item.box[0]) * (item.box[3] - item.box[1]) for item in (box, other)
        )
        if area / smaller >= minimum_ratio:
            overlap_count += 1
            if len(overlaps) < MAX_SAMPLES:
                overlaps.append(
                    {
                        "page": page,
                        "text_boxes": [other.index, box.index],
                        "bounds_pt": [other.box, box.box],
                        "area_ratio": area / smaller,
                    }
                )
    return heuristic_finding(
        "pdf.text_overlap",
        "Measured intersections between extractable text boxes.",
        overlaps,
        incomplete=uncertain or measured == 0,
        total_violations=overlap_count,
        minimum_area_ratio=minimum_ratio,
        comparisons=min(comparisons, MAX_PAIR_CHECKS),
        scope=_LAYOUT_SCOPE
        + " Overlaps are candidates, including legitimate mathematical overlays.",
    )


def _layout_findings(
    pages: list[_TextPage], frames: PageFrames, options: PdfCheckOptions
) -> list[Finding]:
    unsupported = set(frames.unsupported_pages)
    for page, size in zip(pages, frames.sizes, strict=True):
        if abs(page.width - size[0]) > 1 or abs(page.height - size[1]) > 1:
            unsupported.add(page.page)
    layout = _TextLayout(tuple(pages), frozenset(unsupported), options)
    result: list[Finding] = []
    if options.min_text_size_pt is not None or options.text_size_regions:
        result.append(_text_size_finding(layout))
    if options.printable_margins_pt is not None or options.printable_regions:
        result.append(_printable_bounds_finding(layout))
    if options.overlap_min_area_ratio is not None:
        result.append(_overlap_finding(layout, options.overlap_min_area_ratio))
    return result


async def _layout_measurement(
    session: PdfToolSession, pages: int, options: PdfCheckOptions
) -> list[Finding]:
    frames = await session.measure(
        "page-boxes",
        page_boxes_argv(pages),
        lambda execution: page_frames(execution.stdout, pages),
    )
    return await session.measure(
        "text-geometry",
        text_geometry_argv(),
        lambda execution: _layout_findings(_text_pages(execution.stdout, pages), frames, options),
    )


def _ink_coverage(
    path: Path,
    *,
    expected: tuple[int, int],
    options: PdfCheckOptions,
    max_file_bytes: int,
) -> tuple[int, int, float]:
    image = read_measurement_raster(path, channels=1, max_file_bytes=max_file_bytes)
    width, height = image.width, image.height
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
    ink = InkMask.from_gray(image, options.background_gray, options.ink_difference).count()
    return width, height, ink / len(image.data)


def _percent(ratio: float) -> str:
    return f"{ratio * 100:.3g}%"


def _page_ranges(pages: list[int]) -> str:
    """Ascending page numbers as ``1-3, 5``."""
    ranges: list[list[int]] = []
    for page in sorted(pages):
        if ranges and page == ranges[-1][1] + 1:
            ranges[-1][1] = page
        else:
            ranges.append([page, page])
    return ", ".join(str(first) if first == last else f"{first}-{last}" for first, last in ranges)


def _merge_page_unavailability(findings: list[Finding]) -> list[Finding]:
    """One finding for each distinct reason pages could not be measured, naming those pages."""

    def unavailable_page(finding: Finding) -> int | None:
        page = finding.details.get("page")
        return page if finding.status == "inconclusive" and type(page) is int else None

    pages_by_message: dict[str, list[int]] = {}
    for finding in findings:
        if (page := unavailable_page(finding)) is not None:
            pages_by_message.setdefault(finding.message, []).append(page)
    result = []
    for finding in findings:
        if unavailable_page(finding) is None:
            result.append(finding)
        elif (pages := pages_by_message.pop(finding.message, None)) is not None:
            if len(pages) == 1:
                label, details = f"page {pages[0]}", finding.details
            else:
                label = f"pages {_page_ranges(pages)}"
                details = {k: v for k, v in finding.details.items() if k != "page"}
                details["pages"] = pages
            result.append(replace(finding, message=f"{finding.message} ({label})", details=details))
    return result


async def _sparse_page_finding(
    session: PdfToolSession,
    page: int,
    pages: int,
    size: tuple[float, float],
    options: PdfCheckOptions,
) -> Finding:
    expected = measurement_raster_size(size, options.raster_dpi)
    if exceeds_measurement_raster(expected):
        return unavailable_finding(
            "pdf.sparse_pages",
            PreparationError(
                f"A {size[0]:g} x {size[1]:g} point page would need a "
                f"{expected[0]:,} x {expected[1]:,} pixel render at {options.raster_dpi} DPI, "
                f"above the limit of {MAX_MEASUREMENT_PIXELS:,} pixels "
                f"({MAX_MEASUREMENT_DIMENSION:,} per side); lower raster_dpi to measure it"
            ),
            severity="warning",
            details={"page": page},
        )
    try:
        width, height, ratio = await session.render_page(
            f"raster-{page}",
            page,
            options.raster_dpi,
            partial(
                _ink_coverage,
                expected=expected,
                options=options,
                max_file_bytes=session.runner.limits.max_file_bytes,
            ),
            raster="gray",
        )
    except (PreparationError, OSError) as error:
        return unavailable_finding(
            "pdf.sparse_pages", error, severity="warning", details={"page": page}
        )
    minimum = options.min_page_ink_ratio
    violations: list[object] = []
    ink = f"Page {page} has {_percent(ratio)} inked pixels"
    if minimum is None:
        message = f"{ink}."
    elif ratio < minimum:
        message = f"{ink}, below the configured minimum of {_percent(minimum)}."
        violations.append(
            {
                "page": page,
                "ink_ratio": ratio,
                "minimum": minimum,
                "reason": f"{_percent(ratio)} inked pixels against a minimum of "
                f"{_percent(minimum)}",
            }
        )
    else:
        message = f"{ink}; the minimum is {_percent(minimum)}."
    return heuristic_finding(
        "pdf.sparse_pages",
        message,
        violations,
        page=page,
        last_page=page == pages,
        ink_ratio=ratio,
        minimum=minimum,
        raster_pixels=[width, height],
        background_gray=options.background_gray,
        ink_difference=options.ink_difference,
        raster_dpi=options.raster_dpi,
        scope="Rendered pixel coverage; intentional blank/image-only pages "
        "are not inferred. "
        f"Pages above {MAX_MEASUREMENT_PIXELS:,} pixels ({MAX_MEASUREMENT_DIMENSION:,} per side) "
        "are inconclusive before rendering.",
    )


async def _sparse_findings(
    session: PdfToolSession, pages: int, options: PdfCheckOptions
) -> list[Finding]:
    try:
        require_rendered_pages(pages, "Rendered sparse-page measurement")
        sizes = await session.measure(
            "raster-sizes",
            page_boxes_argv(pages),
            lambda execution: parse_page_sizes(execution.stdout, pages),
        )
    except (PreparationError, OSError) as error:
        return [unavailable_finding("pdf.sparse_pages", error, severity="warning")]
    result = []
    if any(page > pages for page in options.sparse_page_exemptions):
        result.append(
            unavailable_finding(
                "pdf.sparse_pages",
                PreparationError("A configured sparse-page exemption is outside the document"),
                severity="warning",
            )
        )
    for page in range(1, pages + 1):
        if page not in options.sparse_page_exemptions:
            result.append(
                await _sparse_page_finding(session, page, pages, sizes[page - 1], options)
            )
    return _merge_page_unavailability(result)


def _language_finding(objects: QpdfObjectGraph, options: PdfCheckOptions) -> Finding:
    raw = objects.resolve(objects.root.get("/Lang"))
    language = raw[2:].strip() if isinstance(raw, str) and raw.startswith("u:") else None
    expected = options.expected_language
    violations: list[object] = []
    if raw is None or language == "":
        violations.append(
            "The PDF catalog declares no language"
            + (f"; the configured language is {expected!r}." if expected else ".")
        )
    elif (
        language is not None and expected is not None and language.casefold() != expected.casefold()
    ):
        violations.append(
            f"Catalog language {language!r} differs from the configured language {expected!r}."
        )
    return measurement_finding(
        "pdf.document_language",
        "Checked the catalog language declaration.",
        violations,
        incomplete=raw is not None and language is None,
        declared_language=language,
        expected=expected,
        scope="Declaration presence/exact language-tag agreement, not the language of the prose.",
    )


def _annotation_finding(objects: QpdfObjectGraph, options: PdfCheckOptions) -> Finding:
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
            if count > MAX_PARSED_NODES:
                raise PreparationError("Page annotations exceed the bounded traversal limit")
            if subtype[1:] in options.forbidden_annotation_types:
                annotations.append({"page": page_number, "type": subtype[1:]})
    return measurement_finding(
        "pdf.annotation_policy",
        "Inspected page annotation object subtypes.",
        annotations,
        annotation_count=count,
        forbidden_types=list(options.forbidden_annotation_types),
        scope="Page Annots arrays; content-stream drawings and inaccessible "
        "orphan objects excluded.",
    )


def _action_finding(objects: QpdfObjectGraph, options: PdfCheckOptions) -> Finding:
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
    return heuristic_finding(
        "pdf.action_policy",
        "Scanned reachable object dictionaries for configured action names.",
        actions,
        forbidden_types=list(options.forbidden_action_types),
        scope="Reachable /S names; a match is an action candidate requiring "
        "context review. "
        "Opaque streams and unreferenced objects are excluded; no malware-free claim.",
    )


# Each object check, and the severity of its finding when its evidence is unavailable.
_OBJECT_CHECKS: tuple[
    tuple[str, Callable[[QpdfObjectGraph, PdfCheckOptions], Finding], Severity], ...
] = (
    ("pdf.document_language", _language_finding, "error"),
    ("pdf.annotation_policy", _annotation_finding, "error"),
    ("pdf.action_policy", _action_finding, "warning"),
)


def _object_findings(
    output: str, pages: int, options: PdfCheckOptions, rules: Collection[str]
) -> list[Finding]:
    objects = QpdfObjectGraph(output, pages)
    # Preserve completed observations if another part of the object graph is incomplete.
    result = []
    for rule, check, unavailable_severity in _OBJECT_CHECKS:
        if rule not in rules:
            continue
        try:
            result.append(check(objects, options))
        except PreparationError as error:
            result.append(unavailable_finding(rule, error, severity=unavailable_severity))
    return result


def _simple_rgb(node: ET.Element) -> tuple[float, float, float] | None:
    """Interpret only opaque DeviceRGB/Gray values under an explicit sRGB assumption."""
    if finite_number(node.get("alpha", "1")) != 1:
        return None
    values = tuple(finite_number(value) for value in node.get("color", "").split())
    if node.get("colorspace") == "DeviceGray" and len(values) == 1:
        values = values * 3
    elif node.get("colorspace") != "DeviceRGB":
        return None
    if len(values) != 3 or any(value < 0 or value > 1 for value in values):
        return None
    return values[0], values[1], values[2]


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
                    fg = relative_luminance(foreground)
                    bg = relative_luminance(options.contrast_background_rgb)
                    ratio = (max(fg, bg) + 0.05) / (min(fg, bg) + 0.05)
                    if ratio < options.min_text_contrast:
                        contrast.append(
                            {
                                "page": page_number,
                                "foreground_rgb": foreground,
                                "contrast_ratio": ratio,
                            }
                        )
            if (
                options.min_grayscale_luminance_difference is not None
                and node.tag in COLOR_OPERATIONS
            ):
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
                if pairs > MAX_PAIR_CHECKS:
                    separation_uncertain = True
                    break
                difference = abs(relative_luminance(first) - relative_luminance(second))
                if (
                    options.min_grayscale_luminance_difference is not None
                    and difference < options.min_grayscale_luminance_difference
                ):
                    separation_count += 1
                    if len(separation) < MAX_SAMPLES:
                        separation.append(
                            {
                                "page": page_number,
                                "colors_rgb": [first, second],
                                "luminance_difference": difference,
                            }
                        )
            if pairs > MAX_PAIR_CHECKS:
                break
    result = []
    if options.min_text_contrast is not None:
        result.append(
            heuristic_finding(
                "pdf.assumed_text_contrast",
                "Compared opaque text colors with the supplied background.",
                contrast,
                incomplete=contrast_uncertain or contrast_count == 0,
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
            heuristic_finding(
                "pdf.grayscale_separation",
                "Compared distinct observed page colors by nominal luminance.",
                separation,
                incomplete=separation_uncertain or palette_count == 0,
                total_violations=separation_count,
                page_palette_entries=palette_count,
                compared_pairs=min(pairs, MAX_PAIR_CHECKS),
                minimum_difference=options.min_grayscale_luminance_difference,
                scope="Opaque DeviceRGB/Gray colors treated as sRGB. A close pair is advisory; "
                "semantic color-only encoding, visual proximity and perceptual discrimination "
                "are not inferred. Raster colors, ICC conversion and compositing are unmeasured.",
            )
        )
    return result


def _trace_findings(output: str, pages: int, options: PdfCheckOptions) -> list[Finding]:
    trace = parse_mupdf_trace(output, pages)
    strokes: list[object] = []
    colors: list[object] = []
    stroke_uncertain = color_uncertain = False
    stroke_count = color_count = 0
    for number, page in trace.pages:
        for node in page.iter():
            if options.min_stroke_width_pt is not None and node.tag in {
                "stroke_path",
                "stroke_text",
            }:
                stroke_count += 1
                width = finite_number(node.get("linewidth"))
                transform = [finite_number(value) for value in node.get("transform", "").split()]
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
            if options.allowed_color_spaces and node.tag in COLOR_OPERATIONS:
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
            heuristic_finding(
                "pdf.stroke_width",
                "Compared uniformly transformed stroke widths with the supplied minimum.",
                strokes,
                incomplete=stroke_uncertain or stroke_count == 0,
                stroke_count=stroke_count,
                minimum_pt=options.min_stroke_width_pt,
                scope="Stroke operators including placement scale; raster lines "
                "and filled outlines "
                "are excluded. Hairlines and nonuniform transforms are unmeasured.",
            )
        )
    if options.allowed_color_spaces:
        result.append(
            measurement_finding(
                "pdf.color_space_policy",
                "Compared rendered operation color-space names with the supplied list.",
                colors,
                incomplete=color_uncertain or color_count == 0,
                measured_operations=color_count,
                allowed=list(options.allowed_color_spaces),
                scope="MuPDF rendering color-space names; source encoding, contrast, color-only "
                "meaning and perceptual accessibility are not established.",
            )
        )
    result.extend(_contrast_findings(trace.root, options))
    return result


async def _findings_or_unavailable(
    rules: list[str], measurement: Awaitable[list[Finding]]
) -> list[Finding]:
    """One measurement's findings, or each of its configured rules as unavailable."""
    try:
        return await measurement
    except (PreparationError, OSError) as error:
        return [unavailable_finding(rule, error) for rule in rules]


async def inspect_pdf_details(
    pdf: Path,
    work: Path,
    runner: CommandRunner,
    *,
    options: PdfCheckOptions | None = None,
    budget: ResourceBudget | None = None,
) -> list[Finding]:
    """Run only configured checks; unsupported or partial evidence is inconclusive."""
    options = options or PdfCheckOptions()
    configured = configured_pdf_check_rules(options)
    if not configured:
        return []
    preflight = await preflight_pdf(pdf, work, runner, budget, configured)
    if isinstance(preflight, list):
        return preflight
    session, pages = preflight.session, preflight.pages
    result = [*_metadata_findings(preflight.fields, options), *_section_findings(pages, options)]
    if layout_rules := [rule for rule in configured if rule in TEXT_GEOMETRY_RULES]:
        result.extend(
            await _findings_or_unavailable(
                layout_rules, _layout_measurement(session, pages, options)
            )
        )
    if options.min_page_ink_ratio is not None:
        result.extend(await _sparse_findings(session, pages, options))
    if object_rules := [rule for rule in configured if rule in OBJECT_STRUCTURE_RULES]:
        result.extend(
            await _findings_or_unavailable(
                object_rules,
                session.measure(
                    "objects",
                    qpdf_objects_argv(),
                    lambda execution: _object_findings(
                        execution.stdout, pages, options, object_rules
                    ),
                ),
            )
        )
    if trace_rules := [rule for rule in configured if rule in PAINT_TRACE_RULES]:
        result.extend(
            await _findings_or_unavailable(
                trace_rules,
                session.measure(
                    "trace",
                    mupdf_trace_argv(pages),
                    lambda execution: _trace_findings(execution.stdout, pages, options),
                ),
            )
        )
    return result


def _figure_font_finding(execution: CommandResult, *, options: PdfCheckOptions) -> Finding:
    inventory = font_inventory(execution.stdout)
    violations: list[object] = list(
        evaluate_font_policy(
            inventory,
            require_embedded=options.require_embedded_figure_fonts,
            forbid_type3=options.forbid_type3_figure_fonts,
        ).violations
    )
    # A figure without font resources (outlined or raster-only text) cannot
    # violate an embedding or Type 3 policy.
    return measurement_finding(
        "pdf.included_figure_fonts",
        "Inspected font resources in an explicit input PDF figure."
        if inventory
        else "No font resources in this figure; nothing to embed.",
        violations,
        font_count=len(inventory),
        require_embedded=options.require_embedded_figure_fonts,
        forbid_type3=options.forbid_type3_figure_fonts,
        scope="Input-figure font resources; final-PDF attribution is not established. "
        "Type 3 does not imply bitmap glyphs.",
    )


async def inspect_included_pdf_figures(
    figures: tuple[Path, ...],
    work: Path,
    runner: CommandRunner,
    *,
    options: PdfCheckOptions | None = None,
    budget: ResourceBudget | None = None,
) -> list[Finding]:
    """Inspect explicit source figure files without claiming final-PDF object attribution."""
    figure_options = options or PdfCheckOptions()
    if not figure_font_checks_enabled(figure_options):
        return []
    if (
        not isinstance(figures, tuple)
        or len(figures) > MAX_FIGURE_INPUTS
        or any(not isinstance(figure, Path) for figure in figures)
    ):
        raise PreparationError(
            f"Included PDF figure inspection requires at most {MAX_FIGURE_INPUTS} explicit paths"
        )
    if work.exists():
        raise PreparationError("Included figure inspection requires a new workspace")
    result = []
    for index, figure in enumerate(dict.fromkeys(figures), 1):
        try:
            session = await open_pdf_session(figure, work / str(index), runner, budget)
            finding = await session.measure(
                "fonts",
                font_inventory_argv(),
                partial(_figure_font_finding, options=figure_options),
            )
        except (PreparationError, OSError) as error:
            finding = unavailable_finding("pdf.included_figure_fonts", error)
        result.append(replace(finding, path=str(figure)))
    return result
