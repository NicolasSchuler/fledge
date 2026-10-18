"""Run the configured PDF check groups together, for a source project or a lone PDF.

The groups (the opt-in measurements, privacy, included figures, artwork, structure, and
for a lone PDF the inspection and comparison) run concurrently within the CPU slots of the
shared budget. The groups that measure the checked PDF share one ``SharedPdfSession``: it
freezes the PDF once, on first use, and remembers each read-only measurement, so a tool
runs once per PDF however many groups need its output; each group still has its own
working directory and tool record. The remembered output is released when the run ends.
The included figures are other PDFs, and the comparison reads two PDFs under side names,
so each of them freezes its own. This module bridges the source project to the PDF checks
(expected metadata and included figures) and decides which settings a lone PDF cannot
honor. Composite groups hold no reservation of their own; only the tool commands and file
reads inside them do.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field, replace
from functools import partial
from pathlib import Path
from typing import NamedTuple, TypeVar

from .analysed_source import AnalysedSource, reuse_or_analyse
from .cancellation import bounded_map, run_in_thread
from .command_runner import CommandRunner
from .errors import PreparationError, ToolUnavailableError
from .findings import Finding, has_blockers
from .manuscript_metadata_checks import read_literal_metadata
from .pdf_artwork import inspect_artwork_inputs, inspect_pdf_artwork
from .pdf_artwork_options import PdfArtworkOptions, artwork_checks_enabled
from .pdf_check_options import MAX_FIGURE_INPUTS, PdfCheckOptions, figure_font_checks_enabled
from .pdf_checks import extract_pdf_evidence, inspect_included_pdf_figures, inspect_pdf_details
from .pdf_comparison import compare_pdfs
from .pdf_inspection import PdfInspectionOptions, inspect_pdf
from .pdf_previews import PdfPreviewCollector
from .pdf_rule_names import PdfRule
from .pdf_structure_checks import StructureOptions, inspect_pdf_structure
from .pdf_tools import SharedPdfSession
from .resource_budget import ResourceBudget
from .submission_checks import check_pdf_identity
from .submission_options import SubmissionOptions


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
    """One PDF, its private workspace, the settings and the frozen copy its groups share."""

    pdf: Path
    work: Path
    runner: CommandRunner
    settings: PdfCheckSettings
    budget: ResourceBudget
    session: SharedPdfSession

    @classmethod
    def of(
        cls,
        pdf: Path,
        work: Path,
        runner: CommandRunner,
        settings: PdfCheckSettings,
        budget: ResourceBudget,
    ) -> _PdfCheckContext:
        """The context of ``pdf``, whose shared session freezes it below ``work``."""
        session = SharedPdfSession(pdf, work / _SHARED_DOCUMENT, runner, budget)
        return cls(pdf, work, runner, settings, budget, session)


_T = TypeVar("_T")
# The shared frozen copy of the checked PDF lives in this directory of the workspace; each
# group writes to a directory of its own name.
_SHARED_DOCUMENT = "document"


class _FigureInputs(NamedTuple):
    """Literal PDF figures of the selected source graph, and whether the set is complete."""

    paths: tuple[Path, ...]
    complete: bool


async def _read(
    budget: ResourceBudget, name: str, function: Callable[..., _T], *args: object
) -> _T:
    """Run a blocking source read in a worker thread under a lease of the shared budget."""
    async with budget.lease(name):
        return await run_in_thread(function, *args)


def _figure_inputs(source: AnalysedSource) -> _FigureInputs:
    inspection = source.graph
    figures = tuple(
        source.root / name
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
            context.pdf,
            context.work / "privacy",
            context.runner,
            budget=budget,
            session=context.session,
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
        session=context.session,
    )


async def _inspect_pdf_structure(context: _PdfCheckContext) -> list[Finding]:
    return await inspect_pdf_structure(
        context.pdf,
        context.work / "structure",
        context.runner,
        context.settings.structure,
        context.budget,
        session=context.session,
    )


async def _inspect_pdf_basics(context: _PdfCheckContext) -> list[Finding]:
    settings = context.settings
    return await inspect_pdf(
        context.pdf,
        context.work / "basic",
        context.runner,
        settings.max_pages,
        options=settings.inspection,
        budget=context.budget,
        session=context.session,
    )


async def _inspect_pdf_details(context: _PdfCheckContext) -> list[Finding]:
    return await inspect_pdf_details(
        context.pdf,
        context.work / "details",
        context.runner,
        options=context.settings.details,
        budget=context.budget,
        session=context.session,
    )


async def _compare_with_reference(
    context: _PdfCheckContext, reference: Path | None, previews: PdfPreviewCollector | None
) -> list[Finding]:
    if reference is None:
        return []
    return await compare_pdfs(
        reference,
        context.pdf,
        context.work / "comparison",
        context.runner,
        budget=context.budget,
        previews=previews,
    )


async def _run_groups(
    groups: Sequence[Callable[[], Awaitable[list[Finding]]]], budget: ResourceBudget
) -> list[Finding]:
    results = await bounded_map(groups, lambda function: function(), limit=budget.cpu_slots)
    return [finding for group in results for finding in group]


def _source_metadata_finding(name: str, main: str, reasons: object) -> Finding:
    return Finding(
        PdfRule.METADATA_AGREEMENT,
        f"Source {name} cannot be resolved as one literal metadata value.",
        severity="error",
        status="inconclusive",
        path=main,
        details={"field": name, "interpretation_gaps": reasons},
        suggestion="Provide one unambiguous literal declaration, or configure "
        "an explicit expected PDF value instead of source matching.",
    )


async def _inspect_pdf_details_with_source_metadata(
    context: _PdfCheckContext, source: AnalysedSource, main: str
) -> list[Finding]:
    """Configured measurements, with expected metadata resolved from literal sources."""
    settings, budget = context.settings, context.budget
    options = settings.details
    findings = []
    if settings.match_source_metadata:
        metadata, reasons = await _read(
            budget,
            "source metadata",
            partial(
                read_literal_metadata,
                source.root,
                main,
                settings.match_source_metadata,
                analysed=source,
            ),
        )
        expected = dict(options.expected_metadata)
        for name in settings.match_source_metadata:
            if reasons or name not in metadata:
                findings.append(_source_metadata_finding(name, main, reasons))
            else:
                expected[name.title()] = metadata[name]
        options = replace(options, expected_metadata=tuple(sorted(expected.items())))
    findings.extend(
        await inspect_pdf_details(
            context.pdf,
            context.work / "details",
            context.runner,
            options=options,
            budget=budget,
            session=context.session,
        )
    )
    return findings


def _font_scope_finding(main: str, paths: tuple[Path, ...], *, exhaustive: bool) -> Finding | None:
    """What the source graph says about the figure fonts, when it cannot be taken as measured."""
    if not exhaustive:
        return Finding(
            PdfRule.INCLUDED_FIGURE_FONTS,
            "The complete set of included PDF figures could not be established "
            f"within the supported source and {MAX_FIGURE_INPUTS}-figure limits.",
            severity="error",
            status="inconclusive",
            path=main,
        )
    if not paths:
        return Finding(
            PdfRule.INCLUDED_FIGURE_FONTS,
            "No literal PDF figure inputs in the selected source graph.",
            severity="info",
            status="passed",
            path=main,
        )
    return None


def _artwork_scope_finding(main: str) -> Finding:
    return Finding(
        PdfRule.ARTWORK_INPUT_SCOPE,
        "The full set of PDF artwork inputs could not be established; "
        "measured inputs are not an exhaustive inventory.",
        severity="error",
        status="inconclusive",
        path=main,
    )


def _relative_to_source(findings: list[Finding], root: Path) -> list[Finding]:
    """Keep locations actionable after temporary workspaces have been removed."""
    return [
        replace(finding, path=Path(finding.path).relative_to(root).as_posix())
        if finding.path and Path(finding.path).is_relative_to(root)
        else finding
        for finding in findings
    ]


async def _inspect_included_figures(
    context: _PdfCheckContext, source: AnalysedSource, main: str
) -> list[Finding]:
    """Font and artwork policies applied to each literal PDF figure of the source graph."""
    settings, budget, runner, work = context.settings, context.budget, context.runner, context.work
    figure_fonts = figure_font_checks_enabled(settings.details)
    figure_artwork = artwork_checks_enabled(settings.figure_artwork)
    if not (figure_fonts or figure_artwork):
        return []
    paths, complete = await _read(budget, "included PDF graph", _figure_inputs, source)
    exhaustive = complete and len(paths) <= MAX_FIGURE_INPUTS
    inspected = paths[:MAX_FIGURE_INPUTS]
    findings = []
    if (
        figure_fonts
        and (scope := _font_scope_finding(main, paths, exhaustive=exhaustive)) is not None
    ):
        findings.append(scope)
    measured = await inspect_included_pdf_figures(
        inspected, work / "figures", runner, options=settings.details, budget=budget
    )
    if figure_artwork:
        if not exhaustive:
            findings.append(_artwork_scope_finding(main))
        measured.extend(
            await inspect_artwork_inputs(
                inspected,
                work / "artwork-inputs",
                runner,
                options=settings.figure_artwork,
                budget=budget,
            )
        )
    return [*findings, *_relative_to_source(measured, source.root)]


async def inspect_configured_pdf_checks(
    pdf: Path,
    root: Path,
    main: str,
    work: Path,
    runner: CommandRunner,
    settings: PdfCheckSettings,
    budget: ResourceBudget,
    *,
    analysed: AnalysedSource | None = None,
) -> list[Finding]:
    """Inspect frozen PDF inputs; read source metadata only under shared admission.

    ``analysed`` shares the source analysis of the same root and main file with other
    checks; the metadata and figure groups share one either way.
    """
    context = _PdfCheckContext.of(pdf, work, runner, settings, budget)
    source = reuse_or_analyse(root, main, analysed)
    with context.session:
        return await _run_groups(
            [
                partial(_inspect_pdf_details_with_source_metadata, context, source, main),
                partial(_inspect_pdf_privacy, context),
                partial(_inspect_included_figures, context, source, main),
                partial(_inspect_pdf_artwork, context),
                partial(_inspect_pdf_structure, context),
            ],
            budget,
        )


def _source_only_findings(settings: PdfCheckSettings) -> list[Finding]:
    """Settings that need a source project: not failures, and no evidence for a lone PDF."""
    source_only = []
    if settings.match_source_metadata:
        source_only.append((PdfRule.METADATA_AGREEMENT, "Source-to-PDF metadata comparison"))
    if figure_font_checks_enabled(settings.details):
        source_only.append((PdfRule.INCLUDED_FIGURE_FONTS, "Input-figure font inspection"))
    if artwork_checks_enabled(settings.figure_artwork):
        source_only.append((PdfRule.ARTWORK_INPUT_SCOPE, "Input-figure artwork inspection"))
    return [
        Finding(
            rule,
            f"{subject} requires a source project; not run for a standalone PDF.",
            severity="info",
            status="skipped",
        )
        for rule, subject in source_only
    ]


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
    context = _PdfCheckContext.of(pdf, work, runner, settings, budget)
    with context.session:
        findings = await _run_groups(
            [
                partial(_inspect_pdf_basics, context),
                partial(_inspect_pdf_details, context),
                partial(_inspect_pdf_privacy, context),
                partial(_compare_with_reference, context, reference, previews),
                partial(_inspect_pdf_artwork, context),
                partial(_inspect_pdf_structure, context),
            ],
            budget,
        )
    return [*findings, *_source_only_findings(settings)]
