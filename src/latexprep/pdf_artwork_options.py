"""Artwork policy options and the rules they switch on (no measurement code).

Owns the user-facing settings of the ``rendered_pdf_artwork`` and
``included_figure_artwork`` tables, the names of the thirteen artwork rules, the
mapping from options to configured rules and external tools, and the work bounds of
the rendered measurements. Numerical thresholds come from the user: tuning values
such as ``raster_dpi`` alone never select a measurement.

Does not own: the measurements (``pdf_artwork_drawings``, ``pdf_type3_glyphs``,
``pdf_artwork_rendered``), the orchestration (``pdf_artwork``) or the rule catalogue
text (``pdf_artwork_rules``).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from typing import ClassVar, Final, Literal

from .errors import SettingsError
from .option_config import (
    EIGHT_BIT_VALUE,
    FRACTION,
    NONNEGATIVE_NUMBER,
    NONZERO_FRACTION,
    POSITIVE_NUMBER,
    Bounds,
    require_integers,
    require_number,
    require_pairs,
    require_records,
    require_text,
    validate_scalars,
)
from .pdf_check_options import (
    CONTRAST_RATIO_BOUNDS,
    DEFAULT_GEOMETRY_TOLERANCE_PT,
    MAX_REGIONS_PER_POLICY,
    RASTER_SAMPLING_BOUNDS,
    PdfRegion,
)

ColorSpaceFamily = Literal["rgb", "cmyk"]

# Pixels examined across all figure regions of one rendered page, and across all contrast
# patches of one page. The bound keeps hundreds of large regions from monopolising a worker.
DEFAULT_MAX_PIXEL_VISITS_PER_PAGE: Final = 8_000_000


class ArtworkRule(StrEnum):
    """The names of the artwork rules, as reports and the rule catalogue spell them."""

    PRINTER_MARKS = "pdf.printer_marks"
    FIGURE_WHITESPACE = "pdf.figure_whitespace"
    FIGURE_EDGE_INK = "pdf.figure_edge_ink"
    DRAWING_BOUNDS = "pdf.drawing_bounds"
    CLIPPING_CANDIDATES = "pdf.clipping_candidates"
    ARTWORK_COMPOSITION = "pdf.artwork_composition"
    CATEGORY_RESOLUTION = "pdf.artwork_category_resolution"
    TYPE3_GLYPH_PROGRAMS = "pdf.type3_glyph_programs"
    TRANSFORMED_STROKE_WIDTH = "pdf.transformed_stroke_width"
    RASTER_REGION_CANDIDATES = "pdf.raster_region_candidates"
    GRAYSCALE_PREVIEW = "pdf.grayscale_preview"
    RENDERED_SAMPLE_CONTRAST = "pdf.rendered_sample_contrast"
    COLOR_SPACE = "figure.color_space"


# Rules measured on rendered figure regions.
REGION_RULES: Final = frozenset({ArtworkRule.FIGURE_WHITESPACE, ArtworkRule.FIGURE_EDGE_INK})
# Rules measured from the drawing operations of a MuPDF trace.
DRAWING_TRACE_RULES: Final = frozenset(
    {
        ArtworkRule.PRINTER_MARKS,
        ArtworkRule.DRAWING_BOUNDS,
        ArtworkRule.CLIPPING_CANDIDATES,
        ArtworkRule.ARTWORK_COMPOSITION,
        ArtworkRule.CATEGORY_RESOLUTION,
        ArtworkRule.TRANSFORMED_STROKE_WIDTH,
        ArtworkRule.RASTER_REGION_CANDIDATES,
    }
)


@dataclass(frozen=True, slots=True)
class ArtworkLimits:
    """Work bounds of the rendered measurements; the defaults suit ordinary manuscripts."""

    max_pixel_visits_per_page: int = DEFAULT_MAX_PIXEL_VISITS_PER_PAGE


@dataclass(frozen=True)
class PdfContrastSample:
    """User-selected foreground/background patches, with no inferred text semantics."""

    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        "minimum_ratio": CONTRAST_RATIO_BOUNDS,
        "max_channel_spread": EIGHT_BIT_VALUE,
    }

    name: str
    foreground: PdfRegion
    background: PdfRegion
    minimum_ratio: float
    max_channel_spread: int = 0

    def __post_init__(self) -> None:
        require_text("name", self.name)
        validate_scalars(self)
        if not isinstance(self.foreground, PdfRegion) or not isinstance(self.background, PdfRegion):
            raise SettingsError("Contrast samples require foreground and background PDF regions")
        if self.foreground.page != self.background.page:
            raise SettingsError("Foreground and background samples must be on the same page")


@dataclass(frozen=True)
class PdfArtworkOptions:
    """Which artwork measurements run on a PDF, with the thresholds that judge them.

    Lengths ending in ``_pt`` are PDF points (1/72 inch) in the top-left frame of an
    unrotated page. ``raster_dpi``, ``background_gray`` and ``ink_difference`` tune how
    rendered pages are sampled (resolution, paper gray level 0 to 255, and the gray
    difference that counts as ink); they select no measurement by themselves.
    """

    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        **RASTER_SAMPLING_BOUNDS,
        "printer_mark_max_length_pt": POSITIVE_NUMBER,
        "figure_edge_band_pt": POSITIVE_NUMBER,
        "min_stroke_width_pt": POSITIVE_NUMBER,
        "max_figure_whitespace_ratio": FRACTION,
        "min_raster_region_coverage": NONZERO_FRACTION,
        "geometry_tolerance_pt": NONNEGATIVE_NUMBER,
    }

    detect_printer_marks: bool = False
    trim_regions: tuple[PdfRegion, ...] = ()
    printer_mark_max_length_pt: float | None = None
    printer_mark_exemptions: tuple[int, ...] = ()
    figure_regions: tuple[PdfRegion, ...] = ()
    max_figure_whitespace_ratio: float | None = None
    figure_edge_band_pt: float | None = None
    drawing_regions: tuple[PdfRegion, ...] = ()
    check_clipping: bool = False
    classify_artwork: bool = False
    artwork_category: str | None = None
    category_min_dpi: tuple[tuple[str, float], ...] = ()
    classify_type3_glyphs: bool = False
    forbid_bitmap_type3_glyphs: bool = False
    min_stroke_width_pt: float | None = None
    raster_regions: tuple[PdfRegion, ...] = ()
    min_raster_region_coverage: float | None = None
    grayscale_preview: bool = False
    contrast_samples: tuple[PdfContrastSample, ...] = ()
    required_color_space: ColorSpaceFamily | None = None
    raster_dpi: int = 72
    background_gray: int = 255
    ink_difference: int = 5
    geometry_tolerance_pt: float = DEFAULT_GEOMETRY_TOLERANCE_PT

    def __post_init__(self) -> None:
        validate_scalars(self)
        self._validate_regions()
        require_integers("printer_mark_exemptions", self.printer_mark_exemptions, minimum=1)
        self._validate_companion_settings()
        self._validate_categories()

    def _validate_regions(self) -> None:
        require_records(
            "trim_regions",
            self.trim_regions,
            PdfRegion,
            max_items=MAX_REGIONS_PER_POLICY,
            distinct_by=lambda region: region.page,
            distinct_description="page",
        )
        for name in ("figure_regions", "drawing_regions", "raster_regions"):
            require_records(name, getattr(self, name), PdfRegion, max_items=MAX_REGIONS_PER_POLICY)
        require_records(
            "contrast_samples",
            self.contrast_samples,
            PdfContrastSample,
            max_items=MAX_REGIONS_PER_POLICY,
        )

    def _validate_companion_settings(self) -> None:
        if self.detect_printer_marks and (
            not self.trim_regions or self.printer_mark_max_length_pt is None
        ):
            raise SettingsError(
                "Printer marks need trim_regions and printer_mark_max_length_pt",
                ("detect_printer_marks", "trim_regions", "printer_mark_max_length_pt"),
            )
        if bool(self.raster_regions) != (self.min_raster_region_coverage is not None):
            raise SettingsError(
                "Raster candidates need raster_regions and min_raster_region_coverage together",
                ("raster_regions", "min_raster_region_coverage"),
            )

    def _validate_categories(self) -> None:
        require_text("artwork_category", self.artwork_category, allow_none=True)
        require_pairs(
            "category_min_dpi",
            self.category_min_dpi,
            first=require_text,
            second=partial(require_number, minimum=0, exclusive_minimum=True),
        )
        if self.category_min_dpi and self.artwork_category not in dict(self.category_min_dpi):
            raise SettingsError(
                "artwork_category must name a category listed in category_min_dpi",
                ("artwork_category", "category_min_dpi"),
            )


def configured_artwork_rules(options: PdfArtworkOptions) -> list[ArtworkRule]:
    """The rules the options switch on, in the order their findings are reported."""
    return [
        rule
        for rule, enabled in (
            (ArtworkRule.PRINTER_MARKS, options.detect_printer_marks),
            (ArtworkRule.FIGURE_WHITESPACE, options.max_figure_whitespace_ratio is not None),
            (ArtworkRule.FIGURE_EDGE_INK, options.figure_edge_band_pt is not None),
            (ArtworkRule.DRAWING_BOUNDS, bool(options.drawing_regions)),
            (ArtworkRule.CLIPPING_CANDIDATES, options.check_clipping),
            (ArtworkRule.ARTWORK_COMPOSITION, options.classify_artwork),
            (ArtworkRule.CATEGORY_RESOLUTION, bool(options.category_min_dpi)),
            (
                ArtworkRule.TYPE3_GLYPH_PROGRAMS,
                options.classify_type3_glyphs or options.forbid_bitmap_type3_glyphs,
            ),
            (ArtworkRule.TRANSFORMED_STROKE_WIDTH, options.min_stroke_width_pt is not None),
            (ArtworkRule.RASTER_REGION_CANDIDATES, bool(options.raster_regions)),
            (ArtworkRule.GRAYSCALE_PREVIEW, options.grayscale_preview),
            (ArtworkRule.RENDERED_SAMPLE_CONTRAST, bool(options.contrast_samples)),
            (ArtworkRule.COLOR_SPACE, options.required_color_space is not None),
        )
        if enabled
    ]


def required_artwork_tools(options: PdfArtworkOptions) -> set[str]:
    """The external tools the configured measurements run, for setup diagnostics."""
    rules = set(configured_artwork_rules(options))
    tools = {"pdfinfo"} if rules else set()
    if rules & DRAWING_TRACE_RULES or ArtworkRule.COLOR_SPACE in rules:
        # The colour-space comparison reads the same MuPDF trace as the drawing measurements.
        tools.add("mutool")
    if ArtworkRule.TYPE3_GLYPH_PROGRAMS in rules:
        tools.add("qpdf")
    if rules & {
        *REGION_RULES,
        ArtworkRule.GRAYSCALE_PREVIEW,
        ArtworkRule.RENDERED_SAMPLE_CONTRAST,
    }:
        tools.add("pdftoppm")
    if options.classify_artwork or options.category_min_dpi:
        tools.add("pdffonts")
    return tools


def artwork_checks_enabled(options: PdfArtworkOptions) -> bool:
    """Tuning values alone do not select a measurement or require a source graph."""
    return bool(configured_artwork_rules(options))
