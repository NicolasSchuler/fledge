"""Checks of one aspect without the build workflow: a PDF, the bibliography or formatting.

Each function runs one single-purpose command to its outcome. The PDF check reads only
the PDF; the others import a separate snapshot of the input first. No check builds the
document, selects preparation inputs or publishes anything.
"""

from __future__ import annotations

from pathlib import Path

from .analysed_source import AnalysedSource
from .bibliography_checks import check_bibliography_coverage_and_fields
from .bibliography_integrity import check_bibliography_integrity
from .bibliography_results import SOURCE_ERROR_NOTE_RULE
from .citation_graph import citation_source_errors
from .execution_records import record_silenced_suggestions
from .findings import Finding
from .formatting import format_project
from .inline_suppressions import split_silenced_suggestions
from .job_context import JobContext, import_input, pdf_check_settings
from .job_report import job_outcome
from .main_selection import resolve_main
from .online_checks import check_online_references
from .pdf_check_pipeline import inspect_standalone_pdf
from .report import Report


async def check_standalone_pdf(context: JobContext, report: Report) -> None:
    """Check PDF constraints of the input PDF itself; no source project is read.

    Args:
        context: The job context, whose request names the PDF and an optional reference
            PDF to compare it with.
        report: The report that receives the scope, findings and outcome.
    """
    request, settings = context.request, context.settings
    report.scope = "standalone PDF inspection; source and submission bundle were not verified"
    report.findings.extend(
        await inspect_standalone_pdf(
            request.source,
            context.work / "standalone",
            context.runner,
            pdf_check_settings(settings),
            context.budget,
            request.reference_pdf,
            previews=context.previews("supplied PDF versus inspected PDF"),
        )
    )
    report.outcome = job_outcome(report, settings)


async def check_bibliography(context: JobContext, report: Report) -> None:
    """Check bibliography syntax, citation use and configured fields, optionally online evidence.

    The checks read a separate snapshot of the input and share one source analysis.
    Without a main file they still cover every bibliography file of the project.

    Args:
        context: The job context with the settings, budget and source analyses.
        report: The report that receives the scope, main file, findings and outcome.
    """
    settings = context.settings
    report.scope = "selected bibliography checks; no build or bundle verification"
    context.emit("Importing a separate project snapshot")
    snapshot = (await import_input(context, report)).root
    # Without a main file the checks still cover every bibliography file of the project.
    main = report.main = (await resolve_main(context, snapshot)).main
    # The three checks read the same sources, so they share one source analysis.
    analysed = context.sources.get(snapshot, main)
    integrity = await context.thread_operation(
        "bibliography checks", check_bibliography_integrity, snapshot, main, analysed=analysed
    )
    report.findings.extend(_unsilenced(report, integrity, snapshot))
    report.findings.extend(
        await context.thread_operation(
            "bibliography coverage and fields",
            check_bibliography_coverage_and_fields,
            snapshot,
            main,
            settings.bibliography_checks,
            analysed=analysed,
        )
    )
    report.findings.extend(
        await check_online_references(
            snapshot, main, options=settings.online_checks, budget=context.budget, analysed=analysed
        )
    )
    if any(item.rule == SOURCE_ERROR_NOTE_RULE for item in report.findings):
        # No source check runs here, so the source errors the note restates are added: the
        # report then shows the cause and stays blocked by it.
        report.findings.extend(
            await context.thread_operation("source errors", _restated_source_errors, analysed)
        )
    report.outcome = job_outcome(report, settings)


def _unsilenced(report: Report, findings: list[Finding], root: Path) -> list[Finding]:
    """Drop the suggestions that a ``% fledge: ignore`` comment silences, recording them.

    Args:
        report: The report whose execution record lists the silenced suggestions.
        findings: The findings of a check of the files under ``root``.
        root: The snapshot the findings' paths are relative to.

    Returns:
        The findings without the silenced suggestions.
    """
    kept, silenced = split_silenced_suggestions(findings, root)
    if silenced:
        record_silenced_suggestions(report.execution, [item.to_report() for item in silenced])
    return kept


def _restated_source_errors(analysed: AnalysedSource) -> list[Finding]:
    """Return the blocking source findings that left bibliography checks incomplete."""
    return citation_source_errors(analysed.graph)


async def check_formatting(context: JobContext, report: Report) -> None:
    """Report tex-fmt differences of the input without changing any source.

    The formatter runs on a separate snapshot of the input.

    Args:
        context: The job context with the runner, settings and budget.
        report: The report that receives the scope, findings, tools, changes and outcome.
    """
    report.scope = "formatting check; no build or bundle verification"
    context.emit("Importing a separate project snapshot")
    snapshot = (await import_input(context, report)).root
    formatted = await format_project(
        snapshot,
        context.work / "format-check",
        context.runner,
        context.settings.formatting_options,
        budget=context.budget,
    )
    report.findings.extend(formatted.findings)
    report.tools.update(formatted.tools)
    report.changes.extend(formatted.changes)
    report.outcome = job_outcome(report, context.settings)
