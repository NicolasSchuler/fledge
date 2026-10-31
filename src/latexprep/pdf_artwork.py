"""Opt-in advisory artwork measurements of one PDF, composed from the artwork modules.

MuPDF trace supplies painted operations, not semantic figure/table labels. qpdf supplies
declared Type 3 glyph programs, not proof that every glyph is used. All visual
candidates remain advisory, and numerical policies are supplied by the user.

This module owns the order in which the measurements run and what each failure of a
shared input means for the findings that depend on it. The measurements live in
``pdf_artwork_drawings`` (trace-based), ``pdf_printer_marks``, ``pdf_type3_glyphs`` and
``pdf_artwork_rendered`` (rendered pixels); the settings in ``pdf_artwork_options``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, NamedTuple

from .cancellation import run_in_thread
from .command_runner import CommandRunner
from .errors import PreparationError
from .findings import Finding
from .measurement_findings import unavailable_finding
from .pdf_artwork_drawings import (
    color_space_finding,
    inspect_drawings,
    trace_unavailable_findings,
)
from .pdf_artwork_options import (
    DRAWING_TRACE_RULES,
    ArtworkLimits,
    ArtworkRule,
    ColorSpaceFamily,
    PdfArtworkOptions,
    configured_artwork_rules,
)
from .pdf_artwork_rendered import (
    PageGeometry,
    figure_regions,
    inspect_rendered_figures,
    inspect_sample_contrast,
)
from .pdf_artwork_trace import misaligned_frame_pages, parse_drawing_trace
from .pdf_check_options import MAX_FIGURE_INPUTS
from .pdf_tool_output import PageFrames, TracedDocument, page_frames, parse_mupdf_trace
from .pdf_tools import (
    PdfToolSession,
    SharedPdfSession,
    mupdf_trace_argv,
    page_boxes_argv,
    preflight_pdf,
)
from .pdf_type3_glyphs import inspect_type3_glyphs
from .resource_budget import ResourceBudget

# Rules measured on rendered figure regions or as grayscale previews.
_RENDERED_FIGURE_RULES: Final = frozenset(
    {ArtworkRule.FIGURE_WHITESPACE, ArtworkRule.FIGURE_EDGE_INK, ArtworkRule.GRAYSCALE_PREVIEW}
)


async def _read_trace(session: PdfToolSession, pages: int) -> TracedDocument:
    """Read one MuPDF trace, parsed once and shared by the drawing and colour-space measurements.

    Args:
        session: The PDF tool session that runs the trace.
        pages: The number of pages to trace.

    Returns:
        The parsed trace.

    Raises:
        PreparationError: The trace could not be run or parsed.
        OSError: The tool could not be run.
    """
    return await session.measure(
        "trace",
        mupdf_trace_argv(pages),
        lambda execution: parse_mupdf_trace(execution.stdout, pages),
    )


async def _drawing_findings(
    session: PdfToolSession,
    trace: TracedDocument,
    frames: PageFrames,
    options: PdfArtworkOptions,
    rules: list[ArtworkRule],
) -> tuple[list[Finding], frozenset[int]]:
    """Return the trace-based findings, and every page whose coordinate frame cannot be aligned.

    The pages are those Poppler reports as rotated or cropped plus those whose trace frame
    disagrees with the reported size; the rendered measurements skip them as well. When the
    trace cannot be parsed, every rule is unavailable evidence and only Poppler's pages are
    returned.

    Args:
        session: The PDF tool session.
        trace: The MuPDF trace.
        frames: The page sizes and the pages Poppler reports as rotated or cropped.
        options: The artwork options.
        rules: The configured trace rules.

    Returns:
        The findings and the misaligned pages.
    """
    try:
        async with session.lease("drawing trace"):
            drawing_trace = await run_in_thread(parse_drawing_trace, trace)
    except (PreparationError, OSError) as error:
        return trace_unavailable_findings(rules, error), frozenset(frames.unsupported_pages)
    misaligned = frozenset(frames.unsupported_pages) | misaligned_frame_pages(
        drawing_trace.page_boxes, frames.sizes, options.geometry_tolerance_pt
    )
    return await inspect_drawings(session, drawing_trace, misaligned, options, rules), misaligned


async def _color_space_finding(
    session: PdfToolSession, trace: TracedDocument, required: ColorSpaceFamily
) -> Finding:
    """Compare the colour spaces of the trace with ``required``; unavailable on failure.

    Args:
        session: The PDF tool session.
        trace: The MuPDF trace.
        required: The required colour family.

    Returns:
        The finding.
    """
    try:
        async with session.lease("color trace"):
            return await run_in_thread(color_space_finding, trace, required)
    except (PreparationError, OSError) as error:
        return unavailable_finding(ArtworkRule.COLOR_SPACE, error)


@dataclass(frozen=True, slots=True)
class _ArtworkPolicy:
    """What an inspection measures and how: the options, work bounds and figure scope.

    Attributes:
        options: The artwork options.
        limits: The work bounds of the rendered measurements.
        whole_page_figures: Whether every page of an input figure is measured whole when the
            options name no figure regions.
    """

    options: PdfArtworkOptions
    limits: ArtworkLimits
    whole_page_figures: bool = False


class _Trace(NamedTuple):
    """The MuPDF trace of a document, or why it could not be read.

    Attributes:
        document: The parsed trace, or ``None``.
        failure: Why the trace could not be read, or ``None``.
    """

    document: TracedDocument | None
    failure: PreparationError | OSError | None


async def _read_optional_trace(session: PdfToolSession, pages: int, *, wanted: bool) -> _Trace:
    """Read the MuPDF trace if it is ``wanted``, keeping a failure instead of raising it.

    Args:
        session: The PDF tool session that runs the trace.
        pages: The number of pages to trace.
        wanted: Whether any configured measurement needs the trace.

    Returns:
        The trace or its failure; neither when the trace is not wanted.
    """
    if not wanted:
        return _Trace(None, None)
    try:
        return _Trace(await _read_trace(session, pages), None)
    except (PreparationError, OSError) as error:
        return _Trace(None, error)


async def _type3_finding(
    session: PdfToolSession, pages: int, options: PdfArtworkOptions
) -> Finding:
    """Classify the Type 3 glyph programs; unavailable evidence when the inspection fails.

    Args:
        session: The PDF tool session.
        pages: The number of pages of the PDF.
        options: The artwork options, which say whether bitmap glyphs are forbidden.

    Returns:
        The finding.
    """
    try:
        return await inspect_type3_glyphs(
            session, pages, forbid_bitmap=options.forbid_bitmap_type3_glyphs
        )
    except (PreparationError, OSError) as error:
        return unavailable_finding(ArtworkRule.TYPE3_GLYPH_PROGRAMS, error)


async def _rendered_findings(
    session: PdfToolSession,
    geometry: PageGeometry,
    policy: _ArtworkPolicy,
    configured: Sequence[ArtworkRule],
) -> list[Finding]:
    """Return the findings of the configured figure region, preview and contrast measurements.

    Args:
        session: The PDF tool session that renders the pages.
        geometry: The page sizes and the pages whose frame must be skipped.
        policy: What the inspection measures and how.
        configured: The configured rules.

    Returns:
        The figure region and preview findings, then the contrast findings.
    """
    options = policy.options
    result: list[Finding] = []
    if _RENDERED_FIGURE_RULES & set(configured):
        regions = figure_regions(
            options, geometry.sizes, whole_pages_by_default=policy.whole_page_figures
        )
        result.extend(
            await inspect_rendered_figures(session, geometry, options, regions, policy.limits)
        )
    if ArtworkRule.RENDERED_SAMPLE_CONTRAST in configured:
        result.extend(await inspect_sample_contrast(session, geometry, options, policy.limits))
    return result


async def _inspect_artwork(
    pdf: Path,
    work: Path,
    runner: CommandRunner,
    budget: ResourceBudget | None,
    policy: _ArtworkPolicy,
    shared: SharedPdfSession | None = None,
) -> list[Finding]:
    """Run the configured artwork measurements on one PDF, in report order.

    Args:
        pdf: The PDF to inspect.
        work: The inspection workspace.
        runner: Runs the PDF tools.
        budget: The resource budget, or ``None``.
        policy: What the inspection measures and how.
        shared: A shared session for ``pdf``, or ``None``.

    Returns:
        The findings; empty when no rule is configured.
    """
    options = policy.options
    configured = configured_artwork_rules(options)
    if not configured:
        return []
    preflight = await preflight_pdf(pdf, work, runner, budget, configured, session=shared)
    if isinstance(preflight, list):
        return preflight
    session, pages = preflight.session, preflight.pages
    try:
        frames = await session.measure(
            "geometry", page_boxes_argv(pages), lambda result: page_frames(result.stdout, pages)
        )
    except (PreparationError, OSError) as error:
        return [unavailable_finding(rule, error) for rule in configured]
    trace_rules = [rule for rule in configured if rule in DRAWING_TRACE_RULES]
    color_space = options.required_color_space
    trace = await _read_optional_trace(
        session, pages, wanted=bool(trace_rules) or color_space is not None
    )
    misaligned = frozenset(frames.unsupported_pages)
    result: list[Finding] = []
    if trace_rules and trace.document is not None:
        drawing_findings, misaligned = await _drawing_findings(
            session, trace.document, frames, options, trace_rules
        )
        result.extend(drawing_findings)
    elif trace_rules and trace.failure is not None:
        result.extend(trace_unavailable_findings(trace_rules, trace.failure))
    if ArtworkRule.TYPE3_GLYPH_PROGRAMS in configured:
        result.append(await _type3_finding(session, pages, options))
    geometry = PageGeometry(frames.sizes, misaligned)
    result.extend(await _rendered_findings(session, geometry, policy, configured))
    if color_space is not None and trace.document is not None:
        result.append(await _color_space_finding(session, trace.document, color_space))
    elif color_space is not None and trace.failure is not None:
        result.append(unavailable_finding(ArtworkRule.COLOR_SPACE, trace.failure))
    return result


async def inspect_pdf_artwork(
    pdf: Path,
    work: Path,
    runner: CommandRunner,
    *,
    options: PdfArtworkOptions | None = None,
    budget: ResourceBudget | None = None,
    limits: ArtworkLimits | None = None,
    session: SharedPdfSession | None = None,
) -> list[Finding]:
    """Inspect one manuscript PDF; figure measurements need explicit regions.

    Args:
        pdf: The manuscript PDF.
        work: The inspection workspace.
        runner: Runs the PDF tools.
        options: The artwork options; ``None`` uses the defaults, which select nothing.
        budget: The resource budget, or ``None``.
        limits: The work bounds of the rendered measurements; ``None`` uses the defaults.
        session: A shared session for ``pdf``, which supplies the frozen PDF and remembered
            measurements, or ``None``.

    Returns:
        The findings, in report order.
    """
    policy = _ArtworkPolicy(options or PdfArtworkOptions(), limits or ArtworkLimits())
    return await _inspect_artwork(pdf, work, runner, budget, policy, session)


async def inspect_artwork_inputs(
    figures: tuple[Path, ...],
    work: Path,
    runner: CommandRunner,
    *,
    options: PdfArtworkOptions | None = None,
    budget: ResourceBudget | None = None,
    limits: ArtworkLimits | None = None,
) -> list[Finding]:
    """Apply the selected artwork policy separately to explicit input PDF figures.

    Unless the policy names figure regions, every page of an input figure is measured whole.

    Args:
        figures: The input PDF figures; a repeated figure is measured once.
        work: A new inspection workspace, which must not exist yet.
        runner: Runs the PDF tools.
        options: The artwork options; ``None`` uses the defaults, which select nothing.
        budget: The resource budget, or ``None``.
        limits: The work bounds of the rendered measurements; ``None`` uses the defaults.

    Returns:
        The findings of every figure, each with the figure's path.

    Raises:
        PreparationError: ``figures`` is not a tuple of at most ``MAX_FIGURE_INPUTS`` paths,
            or ``work`` already exists.
    """
    if (
        not isinstance(figures, tuple)
        or len(figures) > MAX_FIGURE_INPUTS
        or any(not isinstance(figure, Path) for figure in figures)
    ):
        raise PreparationError(
            f"Artwork input inspection requires at most {MAX_FIGURE_INPUTS} immutable PDF paths"
        )
    if work.exists():
        raise PreparationError("Artwork inputs require a new inspection workspace")
    policy = _ArtworkPolicy(
        options or PdfArtworkOptions(), limits or ArtworkLimits(), whole_page_figures=True
    )
    result = []
    for index, figure in enumerate(dict.fromkeys(figures), 1):
        findings = await _inspect_artwork(figure, work / str(index), runner, budget, policy)
        result.extend(replace(finding, path=str(figure)) for finding in findings)
    return result
