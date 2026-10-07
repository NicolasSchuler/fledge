"""Compose optional checks without giving composite tasks resource reservations."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import Settings
from .manuscript_checks import read_literal_metadata
from .models import Finding, PreparationError, has_blockers
from .pdf import compare_pdfs, inspect_pdf
from .pdf_artwork import artwork_checks_enabled, inspect_artwork_inputs, inspect_pdf_artwork
from .pdf_checks import (
    extract_pdf_evidence,
    inspect_included_pdf_figures,
    inspect_pdf_details,
)
from .runtime import ToolRunner
from .scheduler import ResourceBudget, bounded_map, run_in_thread
from .source import _Inspection
from .structure_checks import inspect_pdf_structure
from .submission_checks import check_pdf_identity


async def _read(budget: ResourceBudget, name: str, function: Callable[..., Any], *args: Any) -> Any:
    async with budget.lease(name):
        return await run_in_thread(function, *args)


def _figure_inputs(root: Path, main: str) -> tuple[tuple[Path, ...], bool]:
    inspection = _Inspection(root, main)
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
    return figures, complete


async def inspect_additional_pdf_checks(
    pdf: Path,
    root: Path,
    main: str,
    work: Path,
    runner: ToolRunner,
    settings: Settings,
    budget: ResourceBudget,
) -> list[Finding]:
    """Inspect frozen PDF inputs; read source metadata only under shared admission."""

    async def details() -> list[Finding]:
        options = settings.pdf_checks
        findings = []
        if settings.match_source_pdf_metadata:
            metadata, reasons = await _read(
                budget,
                "source metadata",
                read_literal_metadata,
                root,
                main,
                settings.match_source_pdf_metadata,
            )
            expected = dict(options.expected_metadata)
            for name in settings.match_source_pdf_metadata:
                if reasons or name not in metadata:
                    findings.append(
                        Finding(
                            "pdf.metadata_agreement",
                            f"Source {name} cannot be resolved as one literal metadata value.",
                            "error",
                            "inconclusive",
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
            await inspect_pdf_details(pdf, work / "details", runner, options=options, budget=budget)
        )
        return findings

    async def privacy() -> list[Finding]:
        privacy_options = settings.submission_checks
        if not settings.checks.enabled("PRV002"):
            privacy_options = replace(privacy_options, identity_terms=())
        if not (privacy_options.identity_terms or privacy_options.scan_secrets):
            return []
        try:
            text, metadata = await extract_pdf_evidence(
                pdf, work / "privacy", runner, budget=budget
            )
        except (PreparationError, OSError):
            # Tool errors can contain document fragments. The checker explains missing evidence
            # without copying raw tool output into an identity/credential report.
            text, metadata = "", {}
        return await _read(
            budget, "PDF privacy", check_pdf_identity, text, metadata, privacy_options
        )

    async def figures() -> list[Finding]:
        if not (
            settings.pdf_checks.require_embedded_figure_fonts
            or settings.pdf_checks.forbid_type3_figure_fonts
            or artwork_checks_enabled(settings.figure_artwork)
        ):
            return []
        paths, complete = await _read(budget, "included PDF graph", _figure_inputs, root, main)
        findings = []
        if (not complete or len(paths) > 300) and (
            settings.pdf_checks.require_embedded_figure_fonts
            or settings.pdf_checks.forbid_type3_figure_fonts
        ):
            findings.append(
                Finding(
                    "pdf.included_figure_fonts",
                    "The complete set of included PDF figures could not be established "
                    "within the supported source and 300-figure limits.",
                    "error",
                    "inconclusive",
                    path=main,
                )
            )
        elif not paths and (
            settings.pdf_checks.require_embedded_figure_fonts
            or settings.pdf_checks.forbid_type3_figure_fonts
        ):
            findings.append(
                Finding(
                    "pdf.included_figure_fonts",
                    "No literal PDF figure inputs in the selected source graph.",
                    "info",
                    "passed",
                    path=main,
                )
            )
        measured = await inspect_included_pdf_figures(
            paths[:300], work / "figures", runner, options=settings.pdf_checks, budget=budget
        )
        if artwork_checks_enabled(settings.figure_artwork):
            if not complete or len(paths) > 300:
                findings.append(
                    Finding(
                        "pdf.artwork_input_scope",
                        "The full set of PDF artwork inputs could not be established; "
                        "measured inputs are not an exhaustive inventory.",
                        "error",
                        "inconclusive",
                        path=main,
                    )
                )
            measured.extend(
                await inspect_artwork_inputs(
                    paths[:300],
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

    async def execute(function: Callable) -> list[Finding]:
        return await function()

    async def artwork() -> list[Finding]:
        return await inspect_pdf_artwork(
            pdf, work / "artwork", runner, options=settings.pdf_artwork, budget=budget
        )

    async def structure() -> list[Finding]:
        return await inspect_pdf_structure(
            pdf, work / "structure", runner, settings.structure_checks, budget
        )

    groups = await bounded_map(
        [details, privacy, figures, artwork, structure], execute, limit=budget.jobs
    )
    return [finding for group in groups for finding in group]


async def inspect_standalone_pdf(
    pdf: Path,
    work: Path,
    runner: ToolRunner,
    settings: Settings,
    budget: ResourceBudget,
    reference: Path | None = None,
) -> list[Finding]:
    """Run PDF-only constraints without implying that a source bundle was checked."""

    async def basic() -> list[Finding]:
        return await inspect_pdf(
            pdf,
            work / "basic",
            runner,
            settings.max_pages,
            options=settings.pdf_options(),
            budget=budget,
        )

    async def detailed() -> list[Finding]:
        return await inspect_pdf_details(
            pdf, work / "details", runner, options=settings.pdf_checks, budget=budget
        )

    async def privacy() -> list[Finding]:
        privacy_options = settings.submission_checks
        if not settings.checks.enabled("PRV002"):
            privacy_options = replace(privacy_options, identity_terms=())
        if not (privacy_options.identity_terms or privacy_options.scan_secrets):
            return []
        try:
            text, metadata = await extract_pdf_evidence(
                pdf, work / "privacy", runner, budget=budget
            )
        except (PreparationError, OSError):
            text, metadata = "", {}
        return await _read(
            budget, "PDF privacy", check_pdf_identity, text, metadata, privacy_options
        )

    async def comparison() -> list[Finding]:
        return (
            await compare_pdfs(reference, pdf, work / "comparison", runner, budget=budget)
            if reference
            else []
        )

    async def execute(function: Callable) -> list[Finding]:
        return await function()

    async def artwork() -> list[Finding]:
        return await inspect_pdf_artwork(
            pdf, work / "artwork", runner, options=settings.pdf_artwork, budget=budget
        )

    async def structure() -> list[Finding]:
        return await inspect_pdf_structure(
            pdf, work / "structure", runner, settings.structure_checks, budget
        )

    groups = await bounded_map(
        [basic, detailed, privacy, comparison, artwork, structure], execute, limit=budget.jobs
    )
    findings = [finding for group in groups for finding in group]
    if settings.match_source_pdf_metadata:
        findings.append(
            Finding(
                "pdf.metadata_agreement",
                "Source-to-PDF metadata comparison requires a source project.",
                "error",
                "skipped",
            )
        )
    if (
        settings.pdf_checks.require_embedded_figure_fonts
        or settings.pdf_checks.forbid_type3_figure_fonts
    ):
        findings.append(
            Finding(
                "pdf.included_figure_fonts",
                "Separate input-figure fonts require a source project; "
                "the final PDF font resources were inspected separately.",
                "error",
                "skipped",
            )
        )
    if artwork_checks_enabled(settings.figure_artwork):
        findings.append(
            Finding(
                "pdf.artwork_input_scope",
                "Input-figure artwork checks require a source project; "
                "only the supplied PDF was inspected.",
                "error",
                "skipped",
            )
        )
    return findings
