"""Optional, bounded PDF measurements with explicit evidence boundaries.

``inspect_pdf_details`` runs only the measurements ``PdfCheckOptions`` enables: document
properties and section budgets here, and the measurement families that read Poppler text
boxes, rendered pages, qpdf objects and MuPDF's trace in their own ``pdf_*_checks``
modules. Unsupported or partial evidence is inconclusive, never a pass. This module
also extracts the text and properties the privacy and agreement checks of other modules
read, and inspects the PDF figures a source includes.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Collection
from dataclasses import replace
from functools import partial
from pathlib import Path

from .command_runner import CommandResult, CommandRunner
from .errors import PreparationError
from .finding_text import plural
from .findings import Finding
from .measurement_findings import measurement_finding, unavailable_finding
from .pdf_check_options import (
    MAX_FIGURE_INPUTS,
    OBJECT_STRUCTURE_RULES,
    PAINT_TRACE_RULES,
    TEXT_GEOMETRY_RULES,
    PdfCheckOptions,
    configured_pdf_check_rules,
    figure_font_checks_enabled,
)
from .pdf_object_checks import measure_object_policies
from .pdf_paint_checks import measure_paint_trace
from .pdf_rule_names import PdfRule
from .pdf_sparse_page_checks import measure_sparse_pages
from .pdf_text_geometry_checks import measure_text_geometry
from .pdf_tool_output import evaluate_font_policy, font_inventory, split_text_pages
from .pdf_tools import (
    PdfPreflight,
    PdfToolSession,
    SharedPdfSession,
    font_inventory_argv,
    layout_text_argv,
    open_pdf_session,
    preflight_pdf,
)
from .resource_budget import ResourceBudget

# One measurement family: the rules it serves, and how it measures the ones configured.
# Each reports its own findings, and each of its rules inconclusive when its tool fails.
_MeasureFamily = Callable[
    [PdfToolSession, int, PdfCheckOptions, tuple[str, ...]], Awaitable[list[Finding]]
]
_MEASUREMENT_FAMILIES: tuple[tuple[Collection[str], _MeasureFamily], ...] = (
    (TEXT_GEOMETRY_RULES, measure_text_geometry),
    ((PdfRule.SPARSE_PAGES,), measure_sparse_pages),
    (OBJECT_STRUCTURE_RULES, measure_object_policies),
    (PAINT_TRACE_RULES, measure_paint_trace),
)


async def extract_pdf_evidence(
    pdf: Path,
    work: Path,
    runner: CommandRunner,
    *,
    budget: ResourceBudget | None = None,
    session: SharedPdfSession | None = None,
) -> tuple[str, dict[str, str]]:
    """Return bounded text/properties for caller-owned privacy or agreement checks.

    Empty pages remain empty evidence, not proof of anonymity or readability.
    No PDF text or metadata is copied into diagnostic messages by this helper.

    Args:
        pdf: The PDF to read.
        work: The workspace in which the PDF is frozen when there is no shared session.
        runner: Runs the PDF tools.
        budget: The resource budget, or ``None``.
        session: A shared session for ``pdf``, which supplies the frozen PDF and remembered
            measurements, or ``None``.

    Returns:
        The extracted text and the document properties.

    Raises:
        PreparationError: The PDF cannot be opened or its text cannot be extracted.
        OSError: A tool could not be run.
    """
    preflight = await PdfPreflight.open(pdf, work, runner, budget, session=session)

    def extract(result: CommandResult) -> str:
        """Return the extracted text once its page boundaries are validated."""
        # Only the page boundaries are validated; the privacy scan reads the text as extracted.
        split_text_pages(result.stdout, preflight.pages)
        return result.stdout

    text = await preflight.session.measure("text", layout_text_argv(), extract)
    return text, preflight.fields


def _required_metadata_finding(fields: dict[str, str], options: PdfCheckOptions) -> Finding:
    """Report each required PDF property that is missing or blank in ``fields``."""
    missing: list[object] = [
        key for key in options.required_metadata if not fields.get(key, "").strip()
    ]
    return measurement_finding(
        PdfRule.REQUIRED_METADATA,
        "Compared nonempty PDF properties with configured required fields.",
        missing,
        details={
            "required": list(options.required_metadata),
            "scope": "pdfinfo document properties",
        },
    )


def _metadata_agreement_finding(fields: dict[str, str], options: PdfCheckOptions) -> Finding:
    """Compare PDF properties with the expected text; only whitespace is normalised."""

    def normalize(value: str) -> str:
        """Collapse each run of whitespace into one space and trim both ends."""
        return " ".join(value.split())

    different: list[object] = [
        key
        for key, expected in options.expected_metadata
        if normalize(fields.get(key, "")) != normalize(expected)
    ]
    return measurement_finding(
        PdfRule.METADATA_AGREEMENT,
        "Compared PDF properties with supplied expected text.",
        different,
        details={
            "compared_fields": [key for key, _ in options.expected_metadata],
            "normalization": "Whitespace only; case, punctuation and author order are retained.",
            "scope": "Explicit expected text; source macro expansion and identity are not "
            "established.",
        },
    )


def _tagging_finding(fields: dict[str, str]) -> Finding:
    """Check that the PDF is reported as tagged; incomplete unless it says yes or no."""
    value = fields.get("Tagged", "").lower()
    return measurement_finding(
        PdfRule.TAGGING_PRESENCE,
        "Checked the reported presence of PDF tagging.",
        ["Tagged: no"] if value == "no" else [],
        incomplete=value not in {"yes", "no"},
        details={
            "reported": value or None,
            "scope": "Tagging presence only; reading order, links, tables and PDF/UA are not "
            "validated.",
        },
    )


def _metadata_findings(fields: dict[str, str], options: PdfCheckOptions) -> list[Finding]:
    """Return the configured property and tagging findings for the document properties."""
    result: list[Finding] = []
    if options.required_metadata:
        result.append(_required_metadata_finding(fields, options))
    if options.expected_metadata:
        result.append(_metadata_agreement_finding(fields, options))
    if options.require_tagged:
        result.append(_tagging_finding(fields))
    return result


def _section_findings(pages: int, options: PdfCheckOptions) -> list[Finding]:
    """Compare each section budget with the pages its range spans.

    Args:
        pages: The number of pages of the PDF.
        options: The PDF check options, which list the section budgets.

    Returns:
        One finding per section; a range that ends beyond the PDF is inconclusive.
    """
    result = []
    for section in options.section_budgets:
        actual = section.end_page - section.start_page + 1
        outside = section.end_page > pages
        over = not outside and actual > section.max_pages
        where = (
            f"page {section.start_page}"
            if actual == 1
            else f"pages {section.start_page}-{section.end_page}"
        )
        span = f"{section.name} spans {plural(actual, 'page')} ({where})"
        if outside:
            message = (
                f"{section.name} is configured to end at page {section.end_page}, but the PDF "
                f"has only {plural(pages, 'page')}; correct the section's page range to check "
                "its budget."
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
                PdfRule.SECTION_PAGE_BUDGET,
                message,
                violations,
                incomplete=outside,
                details={
                    "section": section.name,
                    "start_page": section.start_page,
                    "end_page": section.end_page,
                    "observed_total_pages": pages,
                    "section_pages": None if outside else actual,
                    "maximum": section.max_pages,
                    "scope": "Inclusive user-supplied boundaries; semantic section locations "
                    "are not inferred.",
                },
            )
        )
    return result


async def inspect_pdf_details(
    pdf: Path,
    work: Path,
    runner: CommandRunner,
    *,
    options: PdfCheckOptions | None = None,
    budget: ResourceBudget | None = None,
    session: SharedPdfSession | None = None,
) -> list[Finding]:
    """Run only configured checks; unsupported or partial evidence is inconclusive.

    Args:
        pdf: The PDF to measure.
        work: The workspace, in which the PDF is frozen when there is no shared session.
        runner: Runs the PDF tools.
        options: The PDF check options; ``None`` uses the defaults, which select nothing.
        budget: The resource budget, or ``None``.
        session: A shared session for ``pdf``, which supplies the frozen PDF and remembered
            measurements, or ``None``.

    Returns:
        The findings; empty when no check is configured.
    """
    options = options or PdfCheckOptions()
    configured = configured_pdf_check_rules(options)
    if not configured:
        return []
    preflight = await preflight_pdf(pdf, work, runner, budget, configured, session=session)
    if isinstance(preflight, list):
        return preflight
    tools, pages = preflight.session, preflight.pages
    result = [*_metadata_findings(preflight.fields, options), *_section_findings(pages, options)]
    for family_rules, measure in _MEASUREMENT_FAMILIES:
        if selected := tuple(rule for rule in configured if rule in family_rules):
            result.extend(await measure(tools, pages, options, selected))
    return result


def _figure_font_finding(execution: CommandResult, *, options: PdfCheckOptions) -> Finding:
    """Judge the fonts of one input figure against the figure font policies.

    Args:
        execution: The font inventory command of the figure.
        options: The PDF check options, which hold the figure font policies.

    Returns:
        The finding, with a violation for every font that breaks a policy.
    """
    inventory = font_inventory(execution.stdout)
    violations: list[object] = [
        font.to_report()
        for font in evaluate_font_policy(
            inventory,
            require_embedded=options.require_embedded_figure_fonts,
            forbid_type3=options.forbid_type3_figure_fonts,
        ).violations
    ]
    # A figure without font resources (outlined or raster-only text) cannot
    # violate an embedding or Type 3 policy.
    return measurement_finding(
        PdfRule.INCLUDED_FIGURE_FONTS,
        "Inspected font resources in an explicit input PDF figure."
        if inventory
        else "No font resources in this figure; nothing to embed.",
        violations,
        details={
            "font_count": len(inventory),
            "require_embedded": options.require_embedded_figure_fonts,
            "forbid_type3": options.forbid_type3_figure_fonts,
            "scope": "Input-figure font resources; final-PDF attribution is not established. "
            "Type 3 does not imply bitmap glyphs.",
        },
    )


async def inspect_included_pdf_figures(
    figures: tuple[Path, ...],
    work: Path,
    runner: CommandRunner,
    *,
    options: PdfCheckOptions | None = None,
    budget: ResourceBudget | None = None,
) -> list[Finding]:
    """Inspect explicit source figure files without claiming final-PDF object attribution.

    Args:
        figures: The input PDF figures; a repeated figure is inspected once.
        work: A new workspace, which must not exist yet.
        runner: Runs the PDF tools.
        options: The PDF check options; ``None`` uses the defaults, which select nothing.
        budget: The resource budget, or ``None``.

    Returns:
        One finding per figure, with the figure's path; empty when no figure font policy
        is configured.

    Raises:
        PreparationError: ``figures`` is not a tuple of at most ``MAX_FIGURE_INPUTS`` paths,
            or ``work`` already exists.
    """
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
            finding = unavailable_finding(PdfRule.INCLUDED_FIGURE_FONTS, error)
        result.append(replace(finding, path=str(figure)))
    return result
