"""Compose optional PDF checks without giving composite tasks resource reservations."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import Any, NamedTuple

from .cancellation import bounded_map, run_in_thread
from .errors import PreparationError, ToolUnavailableError
from .findings import Finding, has_blockers
from .manuscript_checks import read_literal_metadata
from .pdf_artwork import (
    PdfArtworkOptions,
    artwork_checks_enabled,
    inspect_artwork_inputs,
    inspect_pdf_artwork,
)
from .pdf_checks import (
    PdfCheckOptions,
    extract_pdf_evidence,
    figure_font_checks_enabled,
    inspect_included_pdf_figures,
    inspect_pdf_details,
)
from .pdf_comparison import compare_pdfs
from .pdf_inspection import PdfInspectionOptions, inspect_pdf
from .pdf_previews import PdfPreviewCollector
from .pdf_structure_checks import StructureOptions, inspect_pdf_structure
from .pdf_support import MAX_FIGURE_INPUTS
from .resource_budget import ResourceBudget
from .runtime import CommandRunner
from .source_graph import SourceGraph
from .submission_checks import SubmissionOptions, check_pdf_identity


@dataclass(frozen=True, slots=True, kw_only=True)
class PdfCheckSettings:
    """The options of every configured PDF check group."""

    max_pages: int | None = None
    inspection: PdfInspectionOptions = field(default_factory=PdfInspectionOptions)
    details: PdfCheckOptions = field(default_factory=PdfCheckOptions)
    rendered_artwork: PdfArtworkOptions = field(default_factory=PdfArtworkOptions)
    figure_artwork: PdfArtworkOptions = field(default_factory=PdfArtworkOptions)
    structure: StructureOptions = field(default_factory=StructureOptions)
    # Rendered-PDF privacy; identity terms are empty when PDF identity (PRV002) is unselected.
    privacy: SubmissionOptions = field(default_factory=SubmissionOptions)
    match_source_metadata: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class _PdfCheckContext:
    """One frozen PDF, its private workspace and the settings every check group shares."""

    pdf: Path
    work: Path
    runner: CommandRunner
    settings: PdfCheckSettings
    budget: ResourceBudget


class _FigureInputs(NamedTuple):
    """Literal PDF figures of the selected source graph, and whether the set is complete."""

    paths: tuple[Path, ...]
    complete: bool


async def _read(budget: ResourceBudget, name: str, function: Callable[..., Any], *args: Any) -> Any:
    async with budget.lease(name):
        return await run_in_thread(function, *args)


def _figure_inputs(root: Path, main: str) -> _FigureInputs:
    inspection = SourceGraph.build(root, main)
    figures = tuple(
        root / name
        for name in sorted(
            {
                target
                for target, command in inspection.references.values()
                if command == "includegraphics" and Path(target).suffix.lower() == ".pdf"
            }
        )
    )
    complete = bool(inspection.main) and not inspection.uncertainties
    complete = complete and not has_blockers(inspection.findings)
    return _FigureInputs(figures, complete)


def _extraction_failure_reason(error: Exception) -> str:
    """A cause that is safe to report: tool errors can quote the document, so only a missing
    tool is named and every other failure keeps its raw message out of the report."""
    if isinstance(error, ToolUnavailableError):
        return f"{error.tool} is not installed"
    return (
        "a PDF tool did not complete normally; the raw tool message is withheld "
        "because it can quote the document"
    )


def _with_extraction_failure(finding: Finding, reason: str) -> Finding:
    """Tell an incomplete scan why its evidence is missing, unlike a text-free PDF."""
    if not finding.incomplete:
        return finding
    return replace(
        finding,
        message=f"{finding.message} Extraction failed: {reason}.",
        suggestion=finding.suggestion
        or "Make sure Poppler's pdftotext and pdfinfo are installed and can read this PDF, "
        "then repeat the check.",
        details={**finding.details, "extraction_failure": reason},
    )


async def _inspect_pdf_privacy(context: _PdfCheckContext) -> list[Finding]:
    budget = context.budget
    privacy_options = context.settings.privacy
    if not (privacy_options.identity_terms or privacy_options.scan_secrets):
        return []
    failure = None
    try:
        text, metadata = await extract_pdf_evidence(
            context.pdf, context.work / "privacy", context.runner, budget=budget
        )
    except (PreparationError, OSError) as error:
        # Tool errors can contain document fragments. The checker explains missing evidence
        # without copying raw tool output into an identity/credential report.
        text, metadata = "", {}
        failure = _extraction_failure_reason(error)
    findings = await _read(
        budget, "PDF privacy", check_pdf_identity, text, metadata, privacy_options
    )
    if failure is None:
        return findings
    return [_with_extraction_failure(finding, failure) for finding in findings]


async def _inspect_pdf_artwork(context: _PdfCheckContext) -> list[Finding]:
    return await inspect_pdf_artwork(
        context.pdf,
        context.work / "artwork",
        context.runner,
        options=context.settings.rendered_artwork,
        budget=context.budget,
    )


async def _inspect_pdf_structure(context: _PdfCheckContext) -> list[Finding]:
    return await inspect_pdf_structure(
        context.pdf,
        context.work / "structure",
        context.runner,
        context.settings.structure,
        context.budget,
    )


async def _run_groups(
    groups: Sequence[Callable[[], Awaitable[list[Finding]]]], budget: ResourceBudget
) -> list[Finding]:
    results = await bounded_map(groups, lambda function: function(), limit=budget.cpu_slots)
    return [finding for group in results for finding in group]


async def _inspect_pdf_details_with_source_metadata(
    context: _PdfCheckContext, root: Path, main: str
) -> list[Finding]:
    """Configured measurements, with expected metadata resolved from literal sources."""
    settings, budget = context.settings, context.budget
    options = settings.details
    findings = []
    if settings.match_source_metadata:
        metadata, reasons = await _read(
            budget,
            "source metadata",
            read_literal_metadata,
            root,
            main,
            settings.match_source_metadata,
        )
        expected = dict(options.expected_metadata)
        for name in settings.match_source_metadata:
            if reasons or name not in metadata:
                findings.append(
                    Finding(
                        "pdf.metadata_agreement",
                        f"Source {name} cannot be resolved as one literal metadata value.",
                        severity="error",
                        status="inconclusive",
                        path=main,
                        details={"field": name, "interpretation_gaps": reasons},
                        suggestion="Provide one unambiguous literal declaration, or configure "
                        "an explicit expected PDF value instead of source matching.",
                    )
                )
            else:
                expected[name.title()] = metadata[name]
        options = replace(options, expected_metadata=tuple(sorted(expected.items())))
    findings.extend(
        await inspect_pdf_details(
            context.pdf, context.work / "details", context.runner, options=options, budget=budget
        )
    )
    return findings


async def _inspect_included_figures(
    context: _PdfCheckContext, root: Path, main: str
) -> list[Finding]:
    """Font and artwork policies applied to each literal PDF figure of the source graph."""
    settings, budget, runner, work = context.settings, context.budget, context.runner, context.work
    figure_fonts = figure_font_checks_enabled(settings.details)
    figure_artwork = artwork_checks_enabled(settings.figure_artwork)
    if not (figure_fonts or figure_artwork):
        return []
    inputs = await _read(budget, "included PDF graph", _figure_inputs, root, main)
    paths, complete = inputs
    exhaustive = complete and len(paths) <= MAX_FIGURE_INPUTS
    findings = []
    if not exhaustive and figure_fonts:
        findings.append(
            Finding(
                "pdf.included_figure_fonts",
                "The complete set of included PDF figures could not be established "
                f"within the supported source and {MAX_FIGURE_INPUTS}-figure limits.",
                severity="error",
                status="inconclusive",
                path=main,
            )
        )
    elif not paths and figure_fonts:
        findings.append(
            Finding(
                "pdf.included_figure_fonts",
                "No literal PDF figure inputs in the selected source graph.",
                severity="info",
                status="passed",
                path=main,
            )
        )
    measured = await inspect_included_pdf_figures(
        paths[:MAX_FIGURE_INPUTS],
        work / "figures",
        runner,
        options=settings.details,
        budget=budget,
    )
    if figure_artwork:
        if not exhaustive:
            findings.append(
                Finding(
                    "pdf.artwork_input_scope",
                    "The full set of PDF artwork inputs could not be established; "
                    "measured inputs are not an exhaustive inventory.",
                    severity="error",
                    status="inconclusive",
                    path=main,
                )
            )
        measured.extend(
            await inspect_artwork_inputs(
                paths[:MAX_FIGURE_INPUTS],
                work / "artwork-inputs",
                runner,
                options=settings.figure_artwork,
                budget=budget,
            )
        )
    # Keep locations actionable after temporary workspaces have been removed.
    findings.extend(
        replace(finding, path=Path(finding.path).relative_to(root).as_posix())
        if finding.path and Path(finding.path).is_relative_to(root)
        else finding
        for finding in measured
    )
    return findings


async def inspect_configured_pdf_checks(
    pdf: Path,
    root: Path,
    main: str,
    work: Path,
    runner: CommandRunner,
    settings: PdfCheckSettings,
    budget: ResourceBudget,
) -> list[Finding]:
    """Inspect frozen PDF inputs; read source metadata only under shared admission."""
    context = _PdfCheckContext(pdf, work, runner, settings, budget)
    return await _run_groups(
        [
            partial(_inspect_pdf_details_with_source_metadata, context, root, main),
            partial(_inspect_pdf_privacy, context),
            partial(_inspect_included_figures, context, root, main),
            partial(_inspect_pdf_artwork, context),
            partial(_inspect_pdf_structure, context),
        ],
        budget,
    )


async def inspect_standalone_pdf(
    pdf: Path,
    work: Path,
    runner: CommandRunner,
    settings: PdfCheckSettings,
    budget: ResourceBudget,
    reference: Path | None = None,
    *,
    previews: PdfPreviewCollector | None = None,
) -> list[Finding]:
    """Run PDF-only constraints without implying that a source bundle was checked."""
    context = _PdfCheckContext(pdf, work, runner, settings, budget)

    async def basic() -> list[Finding]:
        return await inspect_pdf(
            pdf,
            work / "basic",
            runner,
            settings.max_pages,
            options=settings.inspection,
            budget=budget,
        )

    async def detailed() -> list[Finding]:
        return await inspect_pdf_details(
            pdf, work / "details", runner, options=settings.details, budget=budget
        )

    async def comparison() -> list[Finding]:
        if reference is None:
            return []
        return await compare_pdfs(
            reference, pdf, work / "comparison", runner, budget=budget, previews=previews
        )

    findings = await _run_groups(
        [
            basic,
            detailed,
            partial(_inspect_pdf_privacy, context),
            comparison,
            partial(_inspect_pdf_artwork, context),
            partial(_inspect_pdf_structure, context),
        ],
        budget,
    )
    # Source-only settings are neither failures nor evidence for a standalone PDF.
    source_only = []
    if settings.match_source_metadata:
        source_only.append(("pdf.metadata_agreement", "Source-to-PDF metadata comparison"))
    if figure_font_checks_enabled(settings.details):
        source_only.append(("pdf.included_figure_fonts", "Input-figure font inspection"))
    if artwork_checks_enabled(settings.figure_artwork):
        source_only.append(("pdf.artwork_input_scope", "Input-figure artwork inspection"))
    findings.extend(
        Finding(
            rule,
            f"{subject} requires a source project; not run for a standalone PDF.",
            severity="info",
            status="skipped",
        )
        for rule, subject in source_only
    )
    return findings
