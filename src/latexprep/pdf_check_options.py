"""The option schema of the configured PDF measurements, and which checks it enables.

``PdfCheckOptions`` holds every opt-in measurement of the manuscript PDF; a measurement
that is not configured does not run and reports nothing. The validation here is what a
settings file is checked against at load time. This module owns the schema only: the
measurements live in the ``pdf_*_checks`` modules. ``PdfRegion`` and the shared bounds
below are also the schema of the artwork options.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from functools import partial
from typing import ClassVar

from .errors import SettingsError
from .option_config import (
    EIGHT_BIT_MAXIMUM,
    EIGHT_BIT_VALUE,
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
from .pdf_geometry import MAX_MEASUREMENT_DPI, MIN_MEASUREMENT_DPI
from .pdf_rule_names import PdfRule
from .pdf_tool_output import METADATA_FIELDS

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
# Literal PDF figures of the source graph that are inspected for fonts or artwork.
MAX_FIGURE_INPUTS = 300
# Slack, in points, allowed when a text box is compared with the edge of a region or margin.
DEFAULT_GEOMETRY_TOLERANCE_PT = 0.5

# Rules measured from one evidence source; required_pdf_check_tools maps them to tools.
TEXT_GEOMETRY_RULES = frozenset(
    {PdfRule.RENDERED_TEXT_SIZE, PdfRule.PRINTABLE_TEXT_BOUNDS, PdfRule.TEXT_OVERLAP}
)
OBJECT_STRUCTURE_RULES = frozenset(
    {PdfRule.DOCUMENT_LANGUAGE, PdfRule.ANNOTATION_POLICY, PdfRule.ACTION_POLICY}
)
PAINT_TRACE_RULES = frozenset(
    {
        PdfRule.STROKE_WIDTH,
        PdfRule.COLOR_SPACE_POLICY,
        PdfRule.ASSUMED_TEXT_CONTRAST,
        PdfRule.GRAYSCALE_SEPARATION,
    }
)
# A PDF name as /Name is written, without the slash: annotation subtypes and action types.
_PDF_NAME = re.compile(r"[A-Za-z][A-Za-z0-9]*")


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


@dataclass(frozen=True)
class PdfSectionBudget:
    """A page budget for one named, inclusive page range of the PDF."""

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
    """Every opt-in measurement of the manuscript PDF; an unset option enables nothing."""

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
    geometry_tolerance_pt: float = DEFAULT_GEOMETRY_TOLERANCE_PT
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
    """The rules ``options`` enables, in the order their findings are reported."""
    settings = {
        PdfRule.SECTION_PAGE_BUDGET: bool(options.section_budgets),
        PdfRule.RENDERED_TEXT_SIZE: options.min_text_size_pt is not None
        or bool(options.text_size_regions),
        PdfRule.PRINTABLE_TEXT_BOUNDS: options.printable_margins_pt is not None
        or bool(options.printable_regions),
        PdfRule.TEXT_OVERLAP: options.overlap_min_area_ratio is not None,
        PdfRule.SPARSE_PAGES: options.min_page_ink_ratio is not None,
        PdfRule.REQUIRED_METADATA: bool(options.required_metadata),
        PdfRule.METADATA_AGREEMENT: bool(options.expected_metadata),
        PdfRule.TAGGING_PRESENCE: options.require_tagged,
        PdfRule.DOCUMENT_LANGUAGE: options.require_language
        or options.expected_language is not None,
        PdfRule.ANNOTATION_POLICY: bool(options.forbidden_annotation_types),
        PdfRule.ACTION_POLICY: bool(options.forbidden_action_types),
        PdfRule.STROKE_WIDTH: options.min_stroke_width_pt is not None,
        PdfRule.COLOR_SPACE_POLICY: bool(options.allowed_color_spaces),
        PdfRule.ASSUMED_TEXT_CONTRAST: options.min_text_contrast is not None,
        PdfRule.GRAYSCALE_SEPARATION: options.min_grayscale_luminance_difference is not None,
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
