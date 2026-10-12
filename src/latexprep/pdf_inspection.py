"""Bounded single-PDF inspection and explicitly configured acceptance policies.

Each Poppler measurement reports what it observed, then evaluates only the policies
the user configured. A failed measurement makes the observation and each of its
configured policies inconclusive, never a pass.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import NamedTuple

from .models import EvidenceKind, Finding, PreparationError
from .pdf_support import (
    DOCUMENT,
    TOOL_FAILURE_SUGGESTION,
    font_inventory,
    gather_measurements,
    inventory_rows,
    is_type3,
    layout_text_argv,
    normalized_text_pages,
    open_inspection_session,
    page_boxes_argv,
    page_count,
    page_sizes,
    pdfinfo_fields,
    unavailable_finding,
)
from .runtime import CommandResult, CommandRunner
from .scheduler import ResourceBudget

_POINT_UNIT = "PDF points (1/72 inch)"
_POLICY_SUGGESTIONS = {
    "pdf.page_limit": "Review the total page budget, including references and appendices.",
    "pdf.page_dimensions": "Check the configured paper size and PDF page boxes.",
    "pdf.page_size_consistency": "Use a consistent page size, including inserted PDF pages.",
    "pdf.image_resolution": "Use higher-resolution inputs or reduce the rendered raster size.",
    "pdf.font_embedding": "Embed the listed font resources when generating the PDF or figures.",
    "pdf.type3_fonts": "Replace the listed Type 3 font resources with an allowed font type.",
    "pdf.encryption_policy": "Export an unencrypted PDF when this policy is required.",
    "pdf.forms_policy": "Remove form fields from a separate submission PDF copy.",
    "pdf.javascript_policy": "Remove JavaScript from a separate submission PDF copy.",
    "pdf.attachments_policy": "Remove embedded files from a separate submission PDF copy.",
}


@dataclass(frozen=True)
class PdfInspectionOptions:
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
        for value in (
            self.require_consistent_page_size,
            self.require_embedded_fonts,
            self.forbid_type3_fonts,
            self.forbid_encryption,
            self.forbid_forms,
            self.forbid_javascript,
            self.forbid_attachments,
        ):
            if type(value) is not bool:
                raise PreparationError("PDF policy switches must be booleans")
        if (self.expected_page_width_pt is None) != (self.expected_page_height_pt is None):
            raise PreparationError("Expected PDF width and height must be supplied together")
        for value in (
            self.expected_page_width_pt,
            self.expected_page_height_pt,
            self.min_image_dpi,
        ):
            if value is not None and (
                isinstance(value, bool)
                or not isinstance(value, (int, float))
                or not math.isfinite(value)
                or value <= 0
            ):
                raise PreparationError(
                    "PDF dimensions and image resolution must be positive finite numbers"
                )
        if (
            isinstance(self.page_size_tolerance_pt, bool)
            or not isinstance(self.page_size_tolerance_pt, (int, float))
            or not math.isfinite(self.page_size_tolerance_pt)
            or self.page_size_tolerance_pt < 0
        ):
            raise PreparationError("PDF page-size tolerance must be a nonnegative finite number")


def configured_policy_rules(options: PdfInspectionOptions, max_pages: int | None) -> list[str]:
    values = {
        "pdf.page_limit": max_pages is not None,
        "pdf.page_dimensions": options.expected_page_width_pt is not None,
        "pdf.page_size_consistency": options.require_consistent_page_size,
        "pdf.image_resolution": options.min_image_dpi is not None,
        "pdf.font_embedding": options.require_embedded_fonts,
        "pdf.type3_fonts": options.forbid_type3_fonts,
        "pdf.encryption_policy": options.forbid_encryption,
        "pdf.forms_policy": options.forbid_forms,
        "pdf.javascript_policy": options.forbid_javascript,
        "pdf.attachments_policy": options.forbid_attachments,
    }
    return [rule for rule, enabled in values.items() if enabled]


def _policy_finding(
    rule: str,
    message: str,
    violated: bool,
    details: dict[str, object],
    *,
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
            "pdf.pages",
            f"PDF contains {pages} pages.",
            severity="info",
            status="passed",
            details={"pages": pages, "scope": "Total pages, including references and appendices."},
        )
    ]
    if max_pages is not None:
        findings.append(
            _policy_finding(
                "pdf.page_limit",
                f"PDF has {pages} total pages; configured maximum: {max_pages}.",
                pages > max_pages,
                {
                    "pages": pages,
                    "maximum": max_pages,
                    "scope": "All pages, including references and appendices.",
                },
            )
        )
    findings.append(
        Finding(
            "pdf.metadata",
            "PDF document properties were read.",
            severity="info",
            status="passed",
            details={"properties": fields},
        )
    )
    return findings


def _geometry_findings(output: str, context: _InspectionContext) -> list[Finding]:
    options = context.options
    sizes = page_sizes(output, context.pages)
    findings = [
        Finding(
            "pdf.page_geometry",
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
                "pdf.page_dimensions",
                f"Expected {expected[0]:g} × {expected[1]:g} PDF points "
                f"within {options.page_size_tolerance_pt:g} points; "
                f"differing pages: {differing or 'none'}.",
                bool(differing),
                {
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
                "pdf.page_size_consistency",
                f"Page width/height ranges span {spread[0]:g}/{spread[1]:g} PDF points; "
                f"maximum permitted spread: {options.page_size_tolerance_pt:g} points.",
                any(value > options.page_size_tolerance_pt for value in spread),
                {
                    "dimensions_pt": sizes,
                    "range_pt": spread,
                    "tolerance_pt": options.page_size_tolerance_pt,
                    "unit": _POINT_UNIT,
                },
            )
        )
    return findings


def _feature_presence(name: str, value: str | None) -> bool | None:
    if value is None:
        return None
    value = value.lower()
    if name == "Form":
        return False if value == "none" else True if value in {"acroform", "xfa"} else None
    return False if value == "no" else True if re.match(r"yes(?:\s|$)", value) else None


def _feature_findings(fields: dict[str, str], options: PdfInspectionOptions) -> list[Finding]:
    findings: list[Finding] = []
    for name, required, rule in (
        ("Encrypted", options.forbid_encryption, "pdf.encryption_policy"),
        ("Form", options.forbid_forms, "pdf.forms_policy"),
        ("JavaScript", options.forbid_javascript, "pdf.javascript_policy"),
    ):
        value = fields.get(name)
        present = _feature_presence(name, value)
        findings.append(
            Finding(
                f"pdf.{name.lower()}",
                f"{name}: {value}."
                if value is not None
                else f"{name} was not reported by pdfinfo.",
                severity="info" if present is not None else "warning",
                status="passed" if present is not None else "inconclusive",
                details={"value": value, "scope": "pdfinfo document-level feature reporting"},
            )
        )
        if not required:
            continue
        if present is None:
            findings.append(
                _unavailable(
                    rule, PreparationError(f"pdfinfo did not report a recognized {name} value")
                )
            )
        else:
            findings.append(
                _policy_finding(
                    rule,
                    f"{name}: {value}; the configured policy requires absence.",
                    present,
                    {
                        "value": value,
                        "scope": (
                            "pdfinfo document-level feature reporting; "
                            "arbitrary media/annotations are not covered."
                        ),
                    },
                )
            )
    return findings


def _font_findings(output: str, context: _InspectionContext) -> list[Finding]:
    options = context.options
    inventory = font_inventory(output)
    missing = [font["name"] for font in inventory if not font["embedded"]]
    findings = [
        Finding(
            "pdf.fonts",
            f"Inspected {len(inventory)} font resources; {len(missing)} are not embedded.",
            severity="warning" if missing else "info",
            status="failed" if missing else "passed",
            details={
                "fonts": inventory,
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
                    "pdf.font_embedding",
                    PreparationError(
                        "No font resources were reported; an embedding requirement cannot "
                        "establish text/font coverage for outlined or image-only content"
                    ),
                )
            )
        else:
            findings.append(
                _policy_finding(
                    "pdf.font_embedding",
                    f"Unembedded fonts: {', '.join(str(name) for name in missing) or 'none'}; "
                    "all reported font resources must be embedded.",
                    bool(missing),
                    {"nonembedded": missing, "fonts": inventory},
                )
            )
    if options.forbid_type3_fonts:
        type3 = [font for font in inventory if is_type3(font["type"])]
        findings.append(
            _policy_finding(
                "pdf.type3_fonts",
                "Type 3 fonts: "
                f"{', '.join(str(font['name']) for font in type3) or 'none'}; "
                "the configured policy forbids Type 3.",
                bool(type3),
                {"type3_fonts": type3, "inspected_fonts": len(inventory)},
            )
        )
    return findings


def _text_findings(output: str, context: _InspectionContext) -> list[Finding]:
    text_pages = normalized_text_pages(output, context.pages)
    empty = [page for page, value in enumerate(text_pages, 1) if not value.strip()]
    return [
        Finding(
            "pdf.text",
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
            "pdf.attachments",
            f"PDF contains {count} embedded files.",
            severity="info",
            status="passed",
            details={"count": count, "inventory": output},
        )
    ]
    if context.options.forbid_attachments:
        findings.append(
            _policy_finding(
                "pdf.attachments_policy",
                f"PDF contains {count} embedded files; configured maximum: 0.",
                count > 0,
                {"count": count, "inventory": output},
            )
        )
    return findings


def _image_inventory(output: str, pages: int) -> list[dict[str, object]]:
    inventory: list[dict[str, object]] = []
    header = (
        "page num type width height color comp bpc enc interp object ID x-ppi y-ppi size ratio"
    ).split()
    for line in inventory_rows(output, header, "pdfimages"):
        values = line.split()
        if len(values) != 16:
            raise PreparationError("pdfimages returned an unrecognized image inventory row")
        try:
            page, number, width, height = (
                int(values[0]),
                int(values[1]),
                int(values[3]),
                int(values[4]),
            )
            x_dpi, y_dpi = float(values[12]), float(values[13])
        except ValueError as error:
            raise PreparationError("pdfimages returned invalid image measurements") from error
        if (
            page not in range(1, pages + 1)
            or number < 0
            or min(width, height) < 1
            or values[2] not in {"image", "mask", "smask"}
            or not all(math.isfinite(dpi) and dpi >= 0 for dpi in (x_dpi, y_dpi))
        ):
            raise PreparationError("pdfimages returned invalid image measurements")
        inventory.append(
            {
                "page": page,
                "number": number,
                "type": values[2],
                "width_px": width,
                "height_px": height,
                "x_dpi": x_dpi,
                "y_dpi": y_dpi,
            }
        )
    return inventory


def _image_resolution_finding(inventory: list[dict[str, object]], minimum_dpi: float) -> Finding:
    unknown = [item for item in inventory if item["x_dpi"] == 0 or item["y_dpi"] == 0]
    low = [
        item
        for item in inventory
        if isinstance(item["x_dpi"], float)
        and isinstance(item["y_dpi"], float)
        and min(item["x_dpi"], item["y_dpi"]) > 0
        and min(item["x_dpi"], item["y_dpi"]) < minimum_dpi
    ]
    if unknown and not low:
        return Finding(
            "pdf.image_resolution",
            "No usable DPI for raster placements on pages "
            f"{', '.join(sorted({str(item['page']) for item in unknown}))}; "
            f"configured minimum: {minimum_dpi:g} DPI.",
            severity="error",
            status="inconclusive",
            evidence_kind=EvidenceKind.DERIVED,
            suggestion="Inspect the listed raster placements for usable DPI.",
            incomplete=True,
            details={"minimum_dpi": minimum_dpi, "unmeasured": unknown},
        )
    return _policy_finding(
        "pdf.image_resolution",
        f"Minimum {minimum_dpi:g} DPI on both axes; "
        f"{len(inventory)} raster placements inspected. Below minimum: "
        + (
            "; ".join(
                f"page {item['page']} image {item['number']}: {item['x_dpi']} × {item['y_dpi']} DPI"
                for item in low[:8]
            )
            or "none"
        )
        + (f"; plus {len(low) - 8} more" if len(low) > 8 else ""),
        bool(low),
        {
            "minimum_dpi": minimum_dpi,
            "below_minimum": low,
            "unmeasured": unknown,
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
            "pdf.images",
            f"Measured {len(inventory)} reported raster placements, including masks.",
            severity="info",
            status="passed",
            evidence_kind=EvidenceKind.DERIVED,
            details={
                "images": inventory,
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


class _Measurement(NamedTuple):
    """One concurrent Poppler command, its observation and the policies it gates."""

    name: str
    argv: Callable[[int], list[str]]
    unavailable_rule: str
    policy_rules: tuple[str, ...]
    evaluate: Callable[[str, _InspectionContext], list[Finding]]


_GEOMETRY = _Measurement(
    "geometry",
    page_boxes_argv,
    "pdf.geometry_unavailable",
    ("pdf.page_dimensions", "pdf.page_size_consistency"),
    _geometry_findings,
)
# Reported after geometry and the document features read during preflight.
_INVENTORIES = (
    _Measurement(
        "fonts",
        lambda _pages: ["pdffonts", DOCUMENT],
        "pdf.fonts_unavailable",
        ("pdf.font_embedding", "pdf.type3_fonts"),
        _font_findings,
    ),
    _Measurement(
        "text", lambda _pages: layout_text_argv(), "pdf.text_unavailable", (), _text_findings
    ),
    _Measurement(
        "attachments",
        lambda _pages: ["pdfdetach", "-list", DOCUMENT],
        "pdf.attachments_unavailable",
        ("pdf.attachments_policy",),
        _attachment_findings,
    ),
    _Measurement(
        "images",
        lambda _pages: ["pdfimages", "-list", DOCUMENT],
        "pdf.images_unavailable",
        ("pdf.image_resolution",),
        _image_findings,
    ),
)


def _evaluate(
    measurement: _Measurement,
    outcome: CommandResult | PreparationError,
    context: _InspectionContext,
) -> list[Finding]:
    if isinstance(outcome, PreparationError):
        return _measurement_unavailable(measurement, outcome, context)
    try:
        return measurement.evaluate(outcome.stdout, context)
    except (PreparationError, OSError) as error:
        return _measurement_unavailable(measurement, error, context)


def _measurement_unavailable(
    measurement: _Measurement, error: Exception, context: _InspectionContext
) -> list[Finding]:
    return [
        _unavailable(measurement.unavailable_rule, error),
        *(
            _unavailable(rule, error)
            for rule in measurement.policy_rules
            if rule in context.configured_rules
        ),
    ]


def _annotation_media_scope_finding() -> Finding:
    return Finding(
        "pdf.annotation_media_scope",
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
) -> list[Finding]:
    """Measure a PDF and evaluate only explicitly configured acceptance constraints."""
    options = options or PdfInspectionOptions()
    configured = configured_policy_rules(options, max_pages)
    try:
        session = await open_inspection_session(pdf, work, runner, budget)
        info = await session.run("properties", ["pdfinfo", "-isodates", DOCUMENT])
        fields = pdfinfo_fields(info.stdout)
        pages = page_count(fields)
    except (PreparationError, OSError) as error:
        return [_unavailable(rule, error) for rule in ["pdf.inspection_unavailable", *configured]]
    context = _InspectionContext(pages, options, frozenset(configured))
    measurements = (_GEOMETRY, *_INVENTORIES)
    outcomes = await gather_measurements(
        [partial(session.run, item.name, item.argv(pages)) for item in measurements], budget
    )
    geometry, *inventories = (
        _evaluate(measurement, outcome, context)
        for measurement, outcome in zip(measurements, outcomes, strict=True)
    )
    return [
        *_document_findings(pages, fields, max_pages),
        *geometry,
        *_feature_findings(fields, options),
        *(finding for findings in inventories for finding in findings),
        _annotation_media_scope_finding(),
        Finding(
            "pdf.tools",
            "PDF tool versions were recorded.",
            severity="info",
            status="passed",
            details={"tools": session.tool_versions()},
        ),
    ]
