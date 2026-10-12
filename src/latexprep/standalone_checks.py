"""Checks of one aspect without the build workflow: a PDF, the bibliography or formatting."""

from __future__ import annotations

from pathlib import Path

from .bibliography_checks import (
    check_bibliography_coverage_and_fields,
    check_bibliography_integrity,
)
from .formatting import format_project
from .job_context import (
    JobContext,
    job_outcome,
    main_selection_error,
    names_project_file,
    pdf_check_settings,
)
from .models import Report
from .online_checks import check_online_references
from .pdf_check_pipeline import inspect_standalone_pdf
from .source import discover_roots


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


async def check_bibliography(context: JobContext, report: Report, snapshot: Path) -> None:
    """Bibliography syntax, citation use and configured fields, optionally online evidence."""
    settings = context.settings
    report.scope = "selected bibliography checks; no build or bundle verification"
    roots = await context.thread_operation("discover roots", discover_roots, snapshot)
    selected = settings.main or (roots[0] if len(roots) == 1 else None)
    if selected and not names_project_file(snapshot, selected):
        raise main_selection_error(selected, roots)
    main = Path(selected).as_posix() if selected else None
    report.main = main
    report.findings.extend(
        await context.thread_operation(
            "bibliography checks", check_bibliography_integrity, snapshot, main
        )
    )
    report.findings.extend(
        await context.thread_operation(
            "bibliography coverage and fields",
            check_bibliography_coverage_and_fields,
            snapshot,
            main,
            settings.bibliography_checks,
        )
    )
    report.findings.extend(
        await check_online_references(
            snapshot, main, options=settings.online_checks, budget=context.budget
        )
    )
    report.outcome = job_outcome(report, settings)


async def check_formatting(context: JobContext, report: Report, snapshot: Path) -> None:
    """Report tex-fmt differences of the snapshot without changing any source."""
    report.scope = "formatting check; no build or bundle verification"
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
