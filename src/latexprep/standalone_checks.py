"""Checks of one aspect without the build workflow: a PDF, the bibliography or formatting.

Each function runs one single-purpose command to its outcome. The PDF check reads only
the PDF; the others import a separate snapshot of the input first. None of them builds
the document, selects preparation inputs or publishes anything.
"""

from __future__ import annotations

from .bibliography_checks import check_bibliography_coverage_and_fields
from .bibliography_integrity import check_bibliography_integrity
from .formatting import format_project
from .job_context import JobContext, import_input, pdf_check_settings
from .job_report import job_outcome
from .main_selection import resolve_main
from .online_checks import check_online_references
from .pdf_check_pipeline import inspect_standalone_pdf
from .report import Report


async def check_standalone_pdf(context: JobContext, report: Report) -> None:
    """Check PDF constraints of the input PDF itself; no source project is read."""
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
    """Bibliography syntax, citation use and configured fields, optionally online evidence."""
    settings = context.settings
    report.scope = "selected bibliography checks; no build or bundle verification"
    context.emit("Importing a separate project snapshot")
    snapshot = (await import_input(context, report)).root
    # Without a main file the checks still cover every bibliography file of the project.
    main = report.main = (await resolve_main(context, snapshot)).main
    # The three checks read the same sources, so they share one source analysis.
    analysed = context.sources.get(snapshot, main)
    report.findings.extend(
        await context.thread_operation(
            "bibliography checks", check_bibliography_integrity, snapshot, main, analysed=analysed
        )
    )
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
    report.outcome = job_outcome(report, settings)


async def check_formatting(context: JobContext, report: Report) -> None:
    """Report tex-fmt differences of the input without changing any source."""
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
