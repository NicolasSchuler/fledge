"""Bounded single-PDF inspection and explicitly configured acceptance policies.

Every inspection reads the same Poppler inventories (geometry, fonts, text, attachments
and images) and reports what they show; it then evaluates only the acceptance policies
the user configured. A failed measurement makes the observation and each of its
configured policies inconclusive, never a pass. The opt-in measurements that run only
when configured belong to ``pdf_checks``; this module is the always-on inventory.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar, NamedTuple

from .command_runner import CommandRunner
from .errors import PreparationError, SettingsError
from .findings import EvidenceKind, Finding
from .measurement_findings import TOOL_FAILURE_SUGGESTION, unavailable_finding
from .option_config import NONNEGATIVE_NUMBER, POSITIVE_NUMBER, Bounds, validate_scalars
from .pdf_rule_names import PdfRule
from .pdf_tool_output import (
    PdfFont,
    evaluate_font_policy,
    font_inventory,
    inventory_rows,
    normalized_text_pages,
    parse_page_sizes,
)
from .pdf_tools import (
    SharedPdfSession,
    ToolMeasurement,
    attachment_list_argv,
    font_inventory_argv,
    image_list_argv,
    layout_text_argv,
    page_boxes_argv,
    preflight_pdf,
    run_tool_measurements,
)
from .resource_budget import ResourceBudget

_POINT_UNIT = "PDF points (1/72 inch)"
# Placement kinds in the pdfimages -list type column: stencil is a one-bit /ImageMask image.
_IMAGE_TYPES = frozenset({"image", "stencil", "mask", "smask"})
# Columns of a pdfimages -list row: page num type width height color comp bpc enc interp
# object ID x-ppi y-ppi size ratio.
_IMAGE_INVENTORY_COLUMNS = 16
# Placements below the minimum DPI that the message names; the rest are counted.
_MAX_LOW_DPI_LISTED = 8
_POLICY_SUGGESTIONS = {
    PdfRule.PAGE_LIMIT: "Review the total page budget, including references and appendices.",
    PdfRule.PAGE_DIMENSIONS: "Check the configured paper size and PDF page boxes.",
    PdfRule.PAGE_SIZE_CONSISTENCY: "Use a consistent page size, including inserted PDF pages.",
    PdfRule.IMAGE_RESOLUTION: "Use higher-resolution inputs or reduce the rendered raster size.",
    PdfRule.FONT_EMBEDDING: "Embed the listed font resources when generating the PDF or figures.",
    PdfRule.TYPE3_FONTS: "Replace the listed Type 3 font resources with an allowed font type.",
    PdfRule.ENCRYPTION_POLICY: "Export an unencrypted PDF when this policy is required.",
    PdfRule.FORMS_POLICY: "Remove form fields from a separate submission PDF copy.",
    PdfRule.JAVASCRIPT_POLICY: "Remove JavaScript from a separate submission PDF copy.",
    PdfRule.ATTACHMENTS_POLICY: "Remove embedded files from a separate submission PDF copy.",
}


@dataclass(frozen=True)
class PdfInspectionOptions:
    FIELD_BOUNDS: ClassVar[dict[str, Bounds]] = {
        "expected_page_width_pt": POSITIVE_NUMBER,
        "expected_page_height_pt": POSITIVE_NUMBER,
        "page_size_tolerance_pt": NONNEGATIVE_NUMBER,
        "min_image_dpi": POSITIVE_NUMBER,
    }

    expected_page_width_pt: float | None = None
    expected_page_height_pt: float | None = None
    page_size_tolerance_pt: float = 1.0
    require_consistent_page_size: bool = False
    min_image_dpi: float | None = None
    require_embedded_fonts: bool = False
    forbid_type3_fonts: bool = False
    forbid_encryption: bool = False
    forbid_forms: bool = False
    forbid_javascript: bool = False
    forbid_attachments: bool = False

    def __post_init__(self) -> None:
        validate_scalars(self)
        if (self.expected_page_width_pt is None) != (self.expected_page_height_pt is None):
            raise SettingsError(
                "expected_page_width_pt and expected_page_height_pt must be set together",
                ("expected_page_width_pt", "expected_page_height_pt"),
            )


def configured_policy_rules(options: PdfInspectionOptions, max_pages: int | None) -> list[str]:
    values = {
        PdfRule.PAGE_LIMIT: max_pages is not None,
        PdfRule.PAGE_DIMENSIONS: options.expected_page_width_pt is not None,
        PdfRule.PAGE_SIZE_CONSISTENCY: options.require_consistent_page_size,
        PdfRule.IMAGE_RESOLUTION: options.min_image_dpi is not None,
        PdfRule.FONT_EMBEDDING: options.require_embedded_fonts,
        PdfRule.TYPE3_FONTS: options.forbid_type3_fonts,
        PdfRule.ENCRYPTION_POLICY: options.forbid_encryption,
        PdfRule.FORMS_POLICY: options.forbid_forms,
        PdfRule.JAVASCRIPT_POLICY: options.forbid_javascript,
        PdfRule.ATTACHMENTS_POLICY: options.forbid_attachments,
    }
    return [rule for rule, enabled in values.items() if enabled]


def _policy_finding(
    rule: str,
    message: str,
    *,
    violated: bool,
    details: dict[str, object],
    incomplete: bool = False,
) -> Finding:
    return Finding(
        rule,
        message,
        severity="error" if violated else "info",
        status="failed" if violated else "passed",
        evidence_kind=EvidenceKind.DERIVED,
        incomplete=incomplete,
        details=details,
        suggestion=_POLICY_SUGGESTIONS.get(rule) if violated else None,
    )


def _unavailable(rule: str, error: Exception) -> Finding:
    return unavailable_finding(rule, error, suggestion=TOOL_FAILURE_SUGGESTION)


@dataclass(frozen=True, slots=True)
class _InspectionContext:
    pages: int
    options: PdfInspectionOptions
    configured_rules: frozenset[str]


def _document_findings(pages: int, fields: dict[str, str], max_pages: int | None) -> list[Finding]:
    findings = [
        Finding(
            PdfRule.PAGES,
            f"PDF contains {pages} pages.",
            severity="info",
            status="passed",
            details={"pages": pages, "scope": "Total pages, including references and appendices."},
        )
    ]
    if max_pages is not None:
        findings.append(
            _policy_finding(
                PdfRule.PAGE_LIMIT,
                f"PDF has {pages} total pages; configured maximum: {max_pages}.",
                violated=pages > max_pages,
                details={
                    "pages": pages,
                    "maximum": max_pages,
                    "scope": "All pages, including references and appendices.",
                },
            )
        )
    findings.append(
        Finding(
            PdfRule.METADATA,
            "PDF document properties were read.",
            severity="info",
            status="passed",
            details={"properties": fields},
        )
    )
    return findings


def _geometry_findings(output: str, context: _InspectionContext) -> list[Finding]:
    options = context.options
    sizes = parse_page_sizes(output, context.pages)
    findings = [
        Finding(
            PdfRule.PAGE_GEOMETRY,
            "PDF page dimensions and boxes were read for every page.",
            severity="info",
            status="passed",
            details={"dimensions_pt": sizes, "boxes": output, "unit": _POINT_UNIT},
        )
    ]
    if options.expected_page_width_pt is not None and options.expected_page_height_pt is not None:
        expected = (options.expected_page_width_pt, options.expected_page_height_pt)
        differing = [
            page
            for page, size in enumerate(sizes, 1)
            if any(
                abs(value - target) > options.page_size_tolerance_pt
                for value, target in zip(size, expected, strict=True)
            )
        ]
        findings.append(
            _policy_finding(
                PdfRule.PAGE_DIMENSIONS,
                f"Expected {expected[0]:g} × {expected[1]:g} PDF points "
                f"within {options.page_size_tolerance_pt:g} points; "
                f"differing pages: {differing or 'none'}.",
                violated=bool(differing),
                details={
                    "expected_pt": expected,
                    "tolerance_pt": options.page_size_tolerance_pt,
                    "dimensions_pt": sizes,
                    "differing_pages": differing,
                    "unit": _POINT_UNIT,
                    "orientation": (
                        "Width and height are ordered as reported by pdfinfo; "
                        "not swapped automatically."
                    ),
                },
            )
        )
    if options.require_consistent_page_size:
        # Max-minus-min enforces the tolerance for every pair of pages, not
        # just against a reference page that could hide opposite deviations.
        spread = tuple(
            max(size[axis] for size in sizes) - min(size[axis] for size in sizes) for axis in (0, 1)
        )
        findings.append(
            _policy_finding(
                PdfRule.PAGE_SIZE_CONSISTENCY,
                f"Page width/height ranges span {spread[0]:g}/{spread[1]:g} PDF points; "
                f"maximum permitted spread: {options.page_size_tolerance_pt:g} points.",
                violated=any(value > options.page_size_tolerance_pt for value in spread),
                details={
                    "dimensions_pt": sizes,
                    "range_pt": spread,
                    "tolerance_pt": options.page_size_tolerance_pt,
                    "unit": _POINT_UNIT,
                },
            )
        )
    return findings


# pdfinfo prints the Form feature as "none", "AcroForm" or "XFA".
_FORM_PRESENCE = {"none": False, "acroform": True, "xfa": True}


def _feature_presence(name: str, value: str | None) -> bool | None:
    """Whether pdfinfo reports a document feature present, or None when it is not recognized.

    The other features print "no", or "yes" optionally followed by detail such as the
    encryption permissions.
    """
    if value is None:
        return None
    value = value.lower()
    if name == "Form":
        return _FORM_PRESENCE.get(value)
    if value == "no":
        return False
    return True if re.match(r"yes(?:\s|$)", value) else None


class _Feature(NamedTuple):
    """A document feature pdfinfo reports, the rule that observes it and the policy on it."""

    name: str
    observation: PdfRule
    policy: PdfRule
    forbidden: Callable[[PdfInspectionOptions], bool]


_FEATURES = (
    _Feature(
        "Encrypted", PdfRule.ENCRYPTED, PdfRule.ENCRYPTION_POLICY, lambda o: o.forbid_encryption
    ),
    _Feature("Form", PdfRule.FORM, PdfRule.FORMS_POLICY, lambda o: o.forbid_forms),
    _Feature(
        "JavaScript", PdfRule.JAVASCRIPT, PdfRule.JAVASCRIPT_POLICY, lambda o: o.forbid_javascript
    ),
)


def _feature_findings(fields: dict[str, str], options: PdfInspectionOptions) -> list[Finding]:
    findings: list[Finding] = []
    for feature in _FEATURES:
        name = feature.name
        value = fields.get(name)
        present = _feature_presence(name, value)
        findings.append(
            Finding(
                feature.observation,
                f"{name}: {value}."
                if value is not None
                else f"{name} was not reported by pdfinfo.",
                severity="info" if present is not None else "warning",
                status="passed" if present is not None else "inconclusive",
                details={"value": value, "scope": "pdfinfo document-level feature reporting"},
            )
        )
        if not feature.forbidden(options):
            continue
        if present is None:
            findings.append(
                _unavailable(
                    feature.policy,
                    PreparationError(f"pdfinfo did not report a recognized {name} value"),
                )
            )
        else:
            findings.append(
                _policy_finding(
                    feature.policy,
                    f"{name}: {value}; the configured policy requires absence.",
                    violated=present,
                    details={
                        "value": value,
                        "scope": (
                            "pdfinfo document-level feature reporting; "
                            "arbitrary media/annotations are not covered."
                        ),
                    },
                )
            )
    return findings


def _font_reports(fonts: list[PdfFont]) -> list[dict[str, object]]:
    return [font.to_report() for font in fonts]


def _font_findings(output: str, context: _InspectionContext) -> list[Finding]:
    options = context.options
    inventory = font_inventory(output)
    fonts = evaluate_font_policy(
        inventory,
        require_embedded=options.require_embedded_fonts,
        forbid_type3=options.forbid_type3_fonts,
    )
    missing = [font.name for font in fonts.nonembedded]
    findings = [
        Finding(
            PdfRule.FONTS,
            f"Inspected {len(inventory)} font resources; {len(missing)} are not embedded.",
            severity="warning" if missing else "info",
            status="failed" if missing else "passed",
            details={
                "fonts": _font_reports(inventory),
                "nonembedded": missing,
                "scope": (
                    "Font resources reported by pdffonts; "
                    "source-figure attribution is not established."
                ),
            },
        )
    ]
    if options.require_embedded_fonts:
        if not inventory:
            findings.append(
                _unavailable(
                    PdfRule.FONT_EMBEDDING,
                    PreparationError(
                        "No font resources were reported; an embedding requirement cannot "
                        "establish text/font coverage for outlined or image-only content"
                    ),
                )
            )
        else:
            findings.append(
                _policy_finding(
                    PdfRule.FONT_EMBEDDING,
                    f"Unembedded fonts: {', '.join(missing) or 'none'}; "
                    "all reported font resources must be embedded.",
                    violated=bool(missing),
                    details={"nonembedded": missing, "fonts": _font_reports(inventory)},
                )
            )
    if options.forbid_type3_fonts:
        type3 = fonts.type3
        findings.append(
            _policy_finding(
                PdfRule.TYPE3_FONTS,
                "Type 3 fonts: "
                f"{', '.join(font.name for font in type3) or 'none'}; "
                "the configured policy forbids Type 3.",
                violated=bool(type3),
                details={"type3_fonts": _font_reports(type3), "inspected_fonts": len(inventory)},
            )
        )
    return findings


def _text_findings(output: str, context: _InspectionContext) -> list[Finding]:
    text_pages = normalized_text_pages(output, context.pages)
    empty = [page for page, value in enumerate(text_pages, 1) if not value.strip()]
    return [
        Finding(
            PdfRule.TEXT,
            "Extractable text is present on every page."
            if not empty
            else (
                "Some pages have no extractable text; image-only, blank, or outlined "
                "content may be intentional."
            ),
            severity="warning" if empty else "info",
            status="inconclusive" if empty else "passed",
            details={
                "extracted_characters": len(output),
                "pages_without_text": empty,
                "scope": "Text extraction, not OCR or semantic readability.",
            },
        )
    ]


def _attachment_findings(output: str, context: _InspectionContext) -> list[Finding]:
    match = re.search(r"^(\d+) embedded files\s*$", output, re.MULTILINE)
    if match is None:
        raise PreparationError("pdfdetach did not report its attachment count")
    count = int(match[1])
    entries = re.findall(r"^(\d+):\s*\S.*$", output, re.MULTILINE)
    if [int(index) for index in entries] != list(range(1, count + 1)):
        raise PreparationError("pdfdetach did not return a complete attachment inventory")
    findings = [
        Finding(
            PdfRule.ATTACHMENTS,
            f"PDF contains {count} embedded files.",
            severity="info",
            status="passed",
            details={"count": count, "inventory": output},
        )
    ]
    if context.options.forbid_attachments:
        findings.append(
            _policy_finding(
                PdfRule.ATTACHMENTS_POLICY,
                f"PDF contains {count} embedded files; configured maximum: 0.",
                violated=count > 0,
                details={"count": count, "inventory": output},
            )
        )
    return findings


@dataclass(frozen=True, slots=True)
class _RasterPlacement:
    """One row of the pdfimages inventory: a raster placed on a page, with its effective DPI."""

    page: int
    number: int
    image_type: str
    width_px: int
    height_px: int
    x_dpi: float
    y_dpi: float

    @property
    def dpi_unknown(self) -> bool:
        """pdfimages reports zero when it cannot compute a resolution."""
        return self.x_dpi == 0 or self.y_dpi == 0

    @property
    def lowest_dpi(self) -> float:
        return min(self.x_dpi, self.y_dpi)

    def to_report(self) -> dict[str, object]:
        return {
            "page": self.page,
            "number": self.number,
            "type": self.image_type,
            "width_px": self.width_px,
            "height_px": self.height_px,
            "x_dpi": self.x_dpi,
            "y_dpi": self.y_dpi,
        }


def _placement_reports(placements: list[_RasterPlacement]) -> list[dict[str, object]]:
    return [placement.to_report() for placement in placements]


def _image_inventory(output: str, pages: int) -> list[_RasterPlacement]:
    inventory: list[_RasterPlacement] = []
    header = (
        "page num type width height color comp bpc enc interp object ID x-ppi y-ppi size ratio"
    ).split()
    for line in inventory_rows(output, header, "pdfimages"):
        values = line.split()
        if len(values) != _IMAGE_INVENTORY_COLUMNS:
            raise PreparationError("pdfimages returned an unrecognized image inventory row")
        try:
            placement = _RasterPlacement(
                page=int(values[0]),
                number=int(values[1]),
                image_type=values[2],
                width_px=int(values[3]),
                height_px=int(values[4]),
                x_dpi=float(values[12]),
                y_dpi=float(values[13]),
            )
        except ValueError as error:
            raise PreparationError("pdfimages returned invalid image measurements") from error
        if (
            placement.page not in range(1, pages + 1)
            or placement.number < 0
            or min(placement.width_px, placement.height_px) < 1
            or placement.image_type not in _IMAGE_TYPES
            or not all(
                math.isfinite(dpi) and dpi >= 0 for dpi in (placement.x_dpi, placement.y_dpi)
            )
        ):
            raise PreparationError("pdfimages returned invalid image measurements")
        inventory.append(placement)
    return inventory


def _image_resolution_finding(inventory: list[_RasterPlacement], minimum_dpi: float) -> Finding:
    unknown = [item for item in inventory if item.dpi_unknown]
    low = [item for item in inventory if 0 < item.lowest_dpi < minimum_dpi]
    if unknown and not low:
        return Finding(
            PdfRule.IMAGE_RESOLUTION,
            "No usable DPI for raster placements on pages "
            f"{', '.join(sorted({str(item.page) for item in unknown}))}; "
            f"configured minimum: {minimum_dpi:g} DPI.",
            severity="error",
            status="inconclusive",
            evidence_kind=EvidenceKind.DERIVED,
            suggestion="Inspect the listed raster placements for usable DPI.",
            incomplete=True,
            details={"minimum_dpi": minimum_dpi, "unmeasured": _placement_reports(unknown)},
        )
    shown = "; ".join(
        f"page {item.page} image {item.number}: {item.x_dpi} × {item.y_dpi} DPI"
        for item in low[:_MAX_LOW_DPI_LISTED]
    )
    hidden = len(low) - _MAX_LOW_DPI_LISTED
    return _policy_finding(
        PdfRule.IMAGE_RESOLUTION,
        f"Minimum {minimum_dpi:g} DPI on both axes; "
        f"{len(inventory)} raster placements inspected. Below minimum: "
        + (shown or "none")
        + (f"; plus {hidden} more" if hidden > 0 else ""),
        violated=bool(low),
        details={
            "minimum_dpi": minimum_dpi,
            "below_minimum": _placement_reports(low),
            "unmeasured": _placement_reports(unknown),
            "inspected_placements": len(inventory),
            "includes_masks": True,
        },
        # Placements without a usable DPI could also be below the minimum.
        incomplete=bool(unknown),
    )


def _image_findings(output: str, context: _InspectionContext) -> list[Finding]:
    inventory = _image_inventory(output, context.pages)
    findings = [
        Finding(
            PdfRule.IMAGES,
            f"Measured {len(inventory)} reported raster placements, including masks.",
            severity="info",
            status="passed",
            evidence_kind=EvidenceKind.DERIVED,
            details={
                "images": _placement_reports(inventory),
                "scope": (
                    "Effective x/y PPI from pdfimages for each reported placement, "
                    "including masks; vector artwork has no raster-DPI test."
                ),
            },
        )
    ]
    if context.options.min_image_dpi is not None:
        findings.append(_image_resolution_finding(inventory, context.options.min_image_dpi))
    return findings


def _inspection_measurements(context: _InspectionContext) -> tuple[ToolMeasurement, ...]:
    """The inventory commands, in report order; every one runs whatever is configured.

    Each also gates the policies that read it: when its command fails, its own
    ``*_unavailable`` observation and each configured policy of its are inconclusive.
    """

    def measurement(
        name: str,
        argv: list[str],
        unavailable: PdfRule,
        policies: tuple[PdfRule, ...],
        evaluate: Callable[[str, _InspectionContext], list[Finding]],
    ) -> ToolMeasurement:
        configured = (policy for policy in policies if policy in context.configured_rules)
        return ToolMeasurement(
            name,
            argv,
            (unavailable, *configured),
            lambda result: evaluate(result.stdout, context),
            suggestion=TOOL_FAILURE_SUGGESTION,
        )

    return (
        measurement(
            "geometry",
            page_boxes_argv(context.pages),
            PdfRule.GEOMETRY_UNAVAILABLE,
            (PdfRule.PAGE_DIMENSIONS, PdfRule.PAGE_SIZE_CONSISTENCY),
            _geometry_findings,
        ),
        measurement(
            "fonts",
            font_inventory_argv(),
            PdfRule.FONTS_UNAVAILABLE,
            (PdfRule.FONT_EMBEDDING, PdfRule.TYPE3_FONTS),
            _font_findings,
        ),
        measurement("text", layout_text_argv(), PdfRule.TEXT_UNAVAILABLE, (), _text_findings),
        measurement(
            "attachments",
            attachment_list_argv(),
            PdfRule.ATTACHMENTS_UNAVAILABLE,
            (PdfRule.ATTACHMENTS_POLICY,),
            _attachment_findings,
        ),
        measurement(
            "images",
            image_list_argv(),
            PdfRule.IMAGES_UNAVAILABLE,
            (PdfRule.IMAGE_RESOLUTION,),
            _image_findings,
        ),
    )


def required_inspection_tools() -> set[str]:
    """The external tools of the inspection, which runs every measurement above.

    Each tool is the command of a measurement's argument list, so the set cannot drift
    from what the inspection runs; pdfinfo also reads the document properties first.
    """
    context = _InspectionContext(1, PdfInspectionOptions(), frozenset())
    return {"pdfinfo", *(item.argv[0] for item in _inspection_measurements(context))}


def _annotation_media_scope_finding() -> Finding:
    return Finding(
        PdfRule.ANNOTATION_MEDIA_SCOPE,
        (
            "Arbitrary annotations and active media are not inspected by this "
            "Poppler adapter; their absence is unverified."
        ),
        # A scope disclaimer, not an observation: it must not make an otherwise
        # clean inspection an advisory outcome.
        severity="info",
        status="skipped",
        suggestion="Use a structural PDF checker before relying on these objects being absent.",
    )


async def inspect_pdf(
    pdf: Path,
    work: Path,
    runner: CommandRunner,
    max_pages: int | None = None,
    *,
    options: PdfInspectionOptions | None = None,
    budget: ResourceBudget | None = None,
    session: SharedPdfSession | None = None,
) -> list[Finding]:
    """Measure a PDF and evaluate only explicitly configured acceptance constraints.

    A shared ``session`` for ``pdf`` supplies the frozen PDF and remembered measurements;
    the ``pdf.tools`` finding still lists exactly the tools this inspection relied on.
    """
    options = options or PdfInspectionOptions()
    configured = configured_policy_rules(options, max_pages)
    # The inspection runs under exactly the supplied budget; its failures name the tool step.
    preflight = await preflight_pdf(
        pdf,
        work,
        runner,
        budget,
        [PdfRule.INSPECTION_UNAVAILABLE, *configured],
        default_to_runner_budget=False,
        suggestion=TOOL_FAILURE_SUGGESTION,
        session=session,
    )
    if isinstance(preflight, list):
        return preflight
    tools, fields, pages = preflight.session, preflight.fields, preflight.pages
    context = _InspectionContext(pages, options, frozenset(configured))
    geometry, *inventories = await run_tool_measurements(tools, _inspection_measurements(context))
    return [
        *_document_findings(pages, fields, max_pages),
        *geometry,
        *_feature_findings(fields, options),
        *(finding for findings in inventories for finding in findings),
        _annotation_media_scope_finding(),
        Finding(
            PdfRule.TOOLS,
            "PDF tool versions were recorded.",
            severity="info",
            status="passed",
            details={"tools": tools.tool_versions()},
        ),
    ]
