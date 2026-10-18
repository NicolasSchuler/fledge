"""Application workflow entry point. Interfaces submit jobs; the job owns mutations and release."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from .check_policy import MANDATORY_CODES, configured_opt_in_codes
from .config import Settings, effective_settings
from .coverage_hints import not_checked_areas
from .document_run import run_project
from .documents import run_documents
from .job_context import (
    DOCUMENT_COMMANDS,
    JobCommand,
    JobContext,
    JobRequest,
    is_blocked,
    select_and_review,
)
from .job_execution import JobDeadlineExceeded, JobExecution
from .models import Finding, Outcome, PreparationError, Report, RequestError
from .publication import (
    DocumentResult,
    OutputTransaction,
    PreviewPlan,
    VerifiedPackage,
    locate_previews,
    plan_previews,
    publish_documents,
    publish_package,
    publish_previews,
    validate_destination,
)


class _Destinations(NamedTuple):
    """Validated new directories for the bundle and for explicit previews."""

    output: Path | None
    previews: Path | None


def _effective_settings(request: JobRequest, report: Report) -> Settings:
    """Record the requested and effective settings and the check selection in the report."""
    settings = request.settings
    report.settings = settings.to_dict()
    effective = effective_settings(settings)
    if effective is not settings:
        report.execution["effective_settings"] = effective.to_dict()
    report.execution["check_selection"] = {
        "select": list(effective.checks.select),
        "ignore": list(effective.checks.ignore),
        "always_enforced_when_applicable": sorted(MANDATORY_CODES),
    }
    if request.command in DOCUMENT_COMMANDS:
        report.execution["not_checked"] = not_checked_areas(
            configured_opt_in_codes(effective.top_level_values())
        )
    return effective


def _runs_documents(request: JobRequest, settings: Settings) -> bool:
    """Whether the configured ``workflow.documents`` apply to this command.

    The single-purpose commands (``bib``, ``fmt``, ``pdf``) work on the input as
    a whole and ignore the document list.
    """
    return bool(settings.workflow.documents) and request.command in DOCUMENT_COMMANDS


def _grayscale_previews(settings: Settings) -> bool:
    return (
        settings.rendered_pdf_artwork.grayscale_preview
        or settings.included_figure_artwork.grayscale_preview
    )


def _validate_request(request: JobRequest, settings: Settings) -> _Destinations:
    output = (
        validate_destination(request.source, request.output, "--output")
        if request.output is not None
        else None
    )
    preview_output = (
        validate_destination(request.source, request.preview_output, "--preview-output")
        if request.preview_output
        else None
    )
    if (
        preview_output is not None
        and output is not None
        and (
            preview_output == output
            or preview_output.is_relative_to(output)
            or output.is_relative_to(preview_output)
        )
    ):
        raise PreparationError("Explicit preview output must be separate from the bundle output")
    if (
        _grayscale_previews(settings)
        and request.command in {JobCommand.CHECK, JobCommand.PDF, JobCommand.PREPARE}
        and preview_output is None
        and (output is None or request.dry_run)
    ):
        raise PreparationError(
            "Grayscale previews require --preview-output, or a verified prepare --output"
        )
    if request.command is JobCommand.PREPARE and not request.dry_run and output is None:
        raise RequestError(
            "prepare requires --output (or --dry-run)",
            "Add --output with a new directory outside the input, or --dry-run to preview "
            "the planned changes without writing a bundle.",
        )
    if _runs_documents(request, settings) and request.reference_pdf is not None:
        raise RequestError(
            "--reference-pdf cannot be used while workflow.documents is configured",
            "Set reference_pdf on each document under workflow.documents instead, or remove "
            "the workflow.documents setting to compare one document with a reference PDF.",
        )
    if request.reference_pdf and not request.reference_pdf.is_file():
        raise RequestError(
            f"Reference PDF does not exist: {request.reference_pdf}",
            "Check the --reference-pdf path, or omit it to skip the PDF comparison.",
        )
    return _Destinations(output, preview_output)


def _record_execution(report: Report, execution: JobExecution, request: JobRequest) -> None:
    report.execution = {**report.execution, **execution.statistics()}
    report.tools.update(
        {name: version.to_report() for name, version in execution.runner.tool_versions.items()}
    )
    report.stages.sort(key=lambda item: str(item["name"]))
    if request.pdf_previews is not None:
        report.execution["pdf_previews"] = request.pdf_previews.summary()


def _record_resource_failure(report: Report, execution: JobExecution) -> bool:
    """Block the job when a runtime resource limit or its accounting failed."""
    resource_failure = execution.runner.statistics().resource_exceeded
    if not resource_failure:
        return False
    report.findings.append(
        Finding(
            "execution.resources",
            str(resource_failure),
            severity="error",
            status="inconclusive",
            suggestion="Increase the relevant limit or reduce parallelism and retry.",
        )
    )
    report.outcome = Outcome.BLOCKED
    return True


def _export_previews(
    report: Report,
    documents: list[DocumentResult] | None,
    work: Path,
    destinations: _Destinations,
    *,
    publishable: bool,
) -> PreviewPlan:
    """Plan the exported preview paths and point findings and artifacts at them."""
    previews: PreviewPlan = ()
    if destinations.previews is not None:
        previews = plan_previews(report, work, destinations.previews)
    elif destinations.output is not None and publishable:
        previews = plan_previews(report, work, destinations.output / "previews")
    locate_previews(report, previews)
    for document in documents or []:
        locate_previews(document.report, previews)
    return previews


def _publish(
    report: Report,
    request: JobRequest,
    settings: Settings,
    destinations: _Destinations,
    execution: JobExecution,
    emit: Callable[[str], None],
    *,
    package: VerifiedPackage | None,
    documents: list[DocumentResult] | None,
    previews: PreviewPlan,
) -> None:
    """Publish everything verified as one transaction; a failure leaves no new output."""
    output, preview_output = destinations
    # Previews go into the bundle only without an explicit preview directory.
    bundled_previews = previews if preview_output is None else ()
    with OutputTransaction() as transaction:
        try:
            refused = False
            if package is not None:
                assert output is not None
                emit("Publishing the verified local output")
                refused = not publish_package(
                    transaction,
                    output,
                    package,
                    report,
                    execution.check_deadline,
                    previews=bundled_previews,
                )
            elif (
                documents
                and request.command is JobCommand.PREPARE
                and not request.dry_run
                and not is_blocked(report, settings)
            ):
                assert output is not None
                publish_documents(
                    transaction,
                    output,
                    documents,
                    report,
                    execution.check_deadline,
                    previews=bundled_previews,
                )
            if preview_output is not None and previews and not refused:
                publish_previews(
                    transaction, preview_output, previews, report, execution.check_deadline
                )
            execution.check_deadline()
        except BaseException:
            report.artifacts.clear()
            raise


async def run_job(
    request: JobRequest,
    progress: Callable[[str], None] | None = None,
    report: Report | None = None,
) -> Report:
    emit = progress or (lambda _message: None)
    report = report if report is not None else Report(request.command.value)
    settings = _effective_settings(request, report)
    report.scope = "source analysis"
    destinations = _validate_request(request, settings)
    execution = JobExecution(settings)
    reviewed = False
    try:
        with execution.workspace("fledge-") as work:
            context = JobContext(request, settings, work, execution.runner, execution.budget, emit)
            package: VerifiedPackage | None = None
            documents: list[DocumentResult] | None = None
            try:
                async with execution.bounded(work):
                    if _runs_documents(request, settings):
                        documents = await run_documents(context, report)
                    else:
                        package = await run_project(context, report)
            finally:
                _record_execution(report, execution, request)
            if _record_resource_failure(report, execution):
                package, documents = None, None
            execution.check_deadline()
            previews: PreviewPlan = ()
            if _grayscale_previews(settings):
                previews = _export_previews(
                    report,
                    documents,
                    work,
                    destinations,
                    publishable=package is not None
                    or bool(documents and not is_blocked(report, settings)),
                )
            # The same redacted report is used by API callers and saved output. A
            # sanitization limit must be resolved before publishing a verified bundle.
            select_and_review(report, settings)
            reviewed = True
            public = report.redacted_copy()
            if public.outcome == Outcome.ERROR:
                report = public
                package, documents, previews = None, None, ()
            # All asynchronous readers, tool children and the monitor are drained.
            # Publication is the final synchronous critical section, never an early branch.
            _publish(
                report,
                request,
                settings,
                destinations,
                execution,
                emit,
                package=package,
                documents=documents,
                previews=previews,
            )
    except JobDeadlineExceeded:
        if not reviewed:
            # Findings collected before the deadline still follow the check selection and
            # the configured review decisions.
            select_and_review(report, settings)
        report.findings.append(
            Finding(
                "execution.deadline",
                f"Job exceeded its {settings.job_timeout_seconds}-second deadline; "
                "no bundle was published",
                severity="error",
                status="inconclusive",
                suggestion="Increase --job-timeout-seconds or reduce the input workload and retry.",
            )
        )
        report.outcome = Outcome.BLOCKED
    except asyncio.CancelledError:
        report.outcome = Outcome.CANCELLED
        raise
    return report.redacted_copy()
