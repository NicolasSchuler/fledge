"""Application workflow entry point. Interfaces submit jobs; the job owns mutations and release.

``run_job`` validates a request, runs its command inside one workspace, resource budget
and deadline, reviews the findings and publishes what verified as one transaction. Which
command runs, and how, is decided only here; the stages themselves live in the modules
this one calls. Messages here name settings and outputs in neutral words, because the
interfaces that submit jobs name their options differently.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from pathlib import Path
from typing import NamedTuple

from .check_policy import configured_opt_in_codes
from .config import Settings, effective_settings
from .coverage_hints import not_checked_areas
from .document_run import run_project
from .documents import run_documents
from .errors import PreparationError, RequestError
from .execution_records import (
    CheckSelectionRecord,
    record_check_selection,
    record_not_checked,
    record_pdf_previews,
)
from .findings import Finding
from .job_context import (
    DOCUMENT_COMMANDS,
    PREVIEW_COMMANDS,
    DocumentBackends,
    JobCommand,
    JobContext,
    JobRequest,
)
from .job_execution import JobDeadlineExceeded, JobExecution
from .job_report import is_blocked, select_and_review
from .publication import (
    DocumentPackages,
    JobOutput,
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
from .report import Outcome, Report
from .rules import MANDATORY_CODES
from .standalone_checks import check_bibliography, check_formatting, check_standalone_pdf
from .task_findings import has_task_defect


class _Destinations(NamedTuple):
    """Validated new directories for the bundle and for explicit previews.

    Attributes:
        output: The directory of the bundle, or ``None``.
        previews: The directory of explicit previews, or ``None``.
    """

    output: Path | None
    previews: Path | None


def _record_settings(request: JobRequest, report: Report) -> Settings:
    """Record the requested and effective settings and the check selection in the report.

    The effective settings are recorded only when they differ from the requested ones,
    and a document command also records the opt-in areas left unchecked.

    Args:
        request: The request, which holds the requested settings.
        report: The report to record in.

    Returns:
        The effective settings, which the job runs with.
    """
    settings = request.settings
    report.settings = settings.to_dict()
    effective = effective_settings(settings)
    if effective is not settings:
        report.execution["effective_settings"] = effective.to_dict()
    record_check_selection(
        report.execution,
        CheckSelectionRecord(
            effective.checks.select,
            effective.checks.ignore,
            tuple(sorted(MANDATORY_CODES)),
        ),
    )
    if request.command in DOCUMENT_COMMANDS:
        record_not_checked(
            report.execution,
            not_checked_areas(configured_opt_in_codes(effective.top_level_values())),
        )
    return effective


def _runs_documents(request: JobRequest, settings: Settings) -> bool:
    """Return whether the configured ``workflow.documents`` apply to this command.

    The single-purpose commands (``bib``, ``fmt``, ``pdf``) work on the input as
    a whole and ignore the document list.
    """
    return bool(settings.workflow.documents) and request.command in DOCUMENT_COMMANDS


def _grayscale_previews(settings: Settings) -> bool:
    """Return whether either artwork setting asks for grayscale previews."""
    return (
        settings.rendered_pdf_artwork.grayscale_preview
        or settings.included_figure_artwork.grayscale_preview
    )


def _validate_destinations(request: JobRequest) -> _Destinations:
    """Validate the new output directories of the request, which must not overlap each other.

    Returns:
        The validated directories; each is ``None`` when the request names none.

    Raises:
        RequestError: The preview output is the output, or one lies inside the other.
    """
    output = (
        validate_destination(request.source, request.output, "output")
        if request.output is not None
        else None
    )
    preview_output = (
        validate_destination(request.source, request.preview_output, "preview output")
        if request.preview_output is not None
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
        raise RequestError(
            "The preview output must be separate from the output",
            "Choose a preview output directory that is neither inside nor the parent of the "
            "output directory.",
        )
    return _Destinations(output, preview_output)


def _validate_request(request: JobRequest, settings: Settings) -> _Destinations:
    """Reject a request that cannot succeed, before any workspace or tool exists.

    Args:
        request: The request to validate.
        settings: The effective settings.

    Returns:
        The validated output directories.

    Raises:
        RequestError: The output directories overlap; grayscale previews have no
            directory to be saved in; ``prepare`` has no output directory and is not a dry
            run; a reference PDF is given while ``workflow.documents`` is configured; or
            the reference PDF does not exist.
    """
    destinations = _validate_destinations(request)
    if (
        _grayscale_previews(settings)
        and request.command in PREVIEW_COMMANDS
        and destinations.previews is None
        and (destinations.output is None or request.dry_run)
    ):
        raise RequestError(
            "Grayscale previews need a directory to be saved in",
            "Give a preview output directory, or run prepare with an output directory so the "
            "previews are saved with the verified bundle.",
        )
    if request.command is JobCommand.PREPARE and not request.dry_run and request.output is None:
        raise RequestError(
            "prepare needs an output directory, or a dry run",
            "Give an output directory that does not exist yet and lies outside the input, or "
            "request a dry run to preview the planned changes without writing a bundle.",
        )
    if _runs_documents(request, settings) and request.reference_pdf is not None:
        raise RequestError(
            "A reference PDF cannot be used while workflow.documents is configured",
            "Set reference_pdf on each document under workflow.documents instead, or remove "
            "the workflow.documents setting to compare one document with a reference PDF.",
        )
    if request.reference_pdf and not request.reference_pdf.is_file():
        raise RequestError(
            f"Reference PDF does not exist: {request.reference_pdf}",
            "Check the path of the reference PDF, or leave it out to skip the PDF comparison.",
        )
    return destinations


def _record_execution(report: Report, execution: JobExecution, request: JobRequest) -> None:
    """Record the job's statistics, tool versions and preview summary, and sort its stages."""
    report.execution = {**report.execution, **execution.statistics()}
    report.tools.update(
        {name: version.to_report() for name, version in execution.runner.tool_versions.items()}
    )
    report.stages.sort(key=lambda item: str(item["name"]))
    if request.pdf_previews is not None:
        record_pdf_previews(report.execution, request.pdf_previews.summary())


def _block_on_resource_failure(report: Report, execution: JobExecution) -> bool:
    """Block the job when a runtime resource limit or its accounting failed.

    Args:
        report: The report, which receives an ``execution.resources`` error and a blocked
            outcome.
        execution: The job's execution, whose runner records resource failures.

    Returns:
        Whether the job was blocked.
    """
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


async def _run_command(context: JobContext, report: Report) -> JobOutput:
    """Run the requested command.

    The only dispatch on the command. The single-purpose commands fill the report and
    verify nothing; the document commands also return what is ready to publish.

    Args:
        context: The job.
        report: The report that the command fills in.

    Returns:
        The package or packages the command verified, if any.
    """
    request = context.request
    match request.command:
        case JobCommand.PDF:
            await check_standalone_pdf(context, report)
        case JobCommand.BIBLIOGRAPHY:
            await check_bibliography(context, report)
        case JobCommand.FORMAT:
            await check_formatting(context, report)
        case JobCommand.INSPECT | JobCommand.CHECK | JobCommand.PREPARE:
            if _runs_documents(request, context.settings):
                return DocumentPackages(tuple(await run_documents(context, report)))
            return await run_project(context, report)
    return None


def _is_publishable(outputs: JobOutput, report: Report, settings: Settings) -> bool:
    """Return whether the job produced something verified that publication may write."""
    if isinstance(outputs, VerifiedPackage):
        return True
    return (
        isinstance(outputs, DocumentPackages)
        and bool(outputs.results)
        and not is_blocked(report, settings)
    )


def _export_previews(
    report: Report,
    outputs: JobOutput,
    work: Path,
    destinations: _Destinations,
    *,
    publishable: bool,
) -> PreviewPlan:
    """Plan the exported preview paths and point findings and artifacts at them.

    Previews go to the explicit preview directory, otherwise into ``previews`` in the
    output directory when the job produced something publishable.

    Args:
        report: The report whose findings and artifacts are pointed at the previews.
        outputs: What the job verified; the reports of document packages are pointed at
            the previews too.
        work: The job's workspace.
        destinations: The validated output directories.
        publishable: Whether publication may write what the job produced.

    Returns:
        The planned previews; none without a directory to export them to.
    """
    previews: PreviewPlan = ()
    if destinations.previews is not None:
        previews = plan_previews(report, work, destinations.previews)
    elif destinations.output is not None and publishable:
        previews = plan_previews(report, work, destinations.output / "previews")
    locate_previews(report, previews)
    if isinstance(outputs, DocumentPackages):
        for document in outputs.results:
            locate_previews(document.report, previews)
    return previews


def _bundle_directory(destinations: _Destinations) -> Path:
    """Return the output directory a verified package is published into.

    Returns:
        The output directory.

    Raises:
        PreparationError: No output directory was given.
    """
    if destinations.output is None:
        raise PreparationError("A verified package was produced but no output directory was given")
    return destinations.output


def _publish(
    report: Report,
    context: JobContext,
    destinations: _Destinations,
    execution: JobExecution,
    outputs: JobOutput,
    previews: PreviewPlan,
) -> None:
    """Publish everything verified as one transaction; a failure leaves no new output.

    Previews go into the bundle only without an explicit preview directory, and are not
    published to that directory when publication refused the package. A failure also
    clears the report's artifacts.

    Args:
        report: The report, which records the published artifacts.
        context: The job.
        destinations: The validated output directories.
        execution: The job's execution, whose deadline is checked while publishing.
        outputs: What the job verified.
        previews: The planned previews.
    """
    request, settings, emit = context.request, context.settings, context.emit
    preview_output = destinations.previews
    # Previews go into the bundle only without an explicit preview directory.
    bundled_previews = previews if preview_output is None else ()
    with OutputTransaction(emit, report) as transaction:
        try:
            refused = False
            if isinstance(outputs, VerifiedPackage):
                emit("Publishing the verified local output")
                refused = not publish_package(
                    transaction,
                    _bundle_directory(destinations),
                    outputs,
                    report,
                    execution.check_deadline,
                    previews=bundled_previews,
                )
            elif (
                isinstance(outputs, DocumentPackages)
                and outputs.results
                and request.command is JobCommand.PREPARE
                and not request.dry_run
                and not is_blocked(report, settings)
            ):
                emit("Publishing the verified document packages")
                refused = not publish_documents(
                    transaction,
                    _bundle_directory(destinations),
                    outputs.results,
                    report,
                    execution.check_deadline,
                    previews=bundled_previews,
                )
            elif destinations.output is not None:
                emit(
                    "Dry run: no output was published"
                    if request.dry_run
                    else "No output was published: the job did not produce a verified package"
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
    *,
    backends: DocumentBackends | None = None,
) -> Report:
    """Run one job and return its sanitized report.

    Every outcome other than a request that cannot succeed, including a missed deadline,
    is in the report.

    Args:
        request: The job to run.
        progress: Receives short status messages.
        report: A report to fill in; pass one to keep a handle on the report while the
            job runs.
        backends: Replaces how documents are built, inspected and compared (the default
            drives the real tools).

    Returns:
        The sanitized report.

    Raises:
        PreparationError: The request cannot succeed; a ``RequestError`` when it names a
            next step.
        asyncio.CancelledError: The job was cancelled; the report is marked cancelled
            first.
    """
    emit = progress or (lambda _message: None)
    report = report if report is not None else Report(request.command.value)
    settings = _record_settings(request, report)
    report.scope = "source analysis"
    destinations = _validate_request(request, settings)
    execution = JobExecution(settings)
    reviewed = False
    try:
        with execution.workspace("fledge-") as work:
            context = JobContext(
                request,
                settings,
                work,
                execution.runner,
                execution.budget,
                emit,
                backends=backends or DocumentBackends(),
            )
            outputs: JobOutput = None
            try:
                async with execution.bounded(work):
                    outputs = await _run_command(context, report)
            finally:
                _record_execution(report, execution, request)
            if _block_on_resource_failure(report, execution):
                outputs = None
            if has_task_defect(report.findings):
                # A defect in a check is not a problem with the input: it ends in the error
                # outcome, whatever the other tasks found, and no package is published.
                # Previews still go to an explicit preview directory, as for a blocked run.
                report.outcome = Outcome.ERROR
                outputs = None
            execution.check_deadline()
            previews: PreviewPlan = ()
            if _grayscale_previews(settings):
                previews = _export_previews(
                    report,
                    outputs,
                    work,
                    destinations,
                    publishable=_is_publishable(outputs, report, settings),
                )
            select_and_review(report, settings)
            reviewed = True
            # All asynchronous readers, tool children and the monitor are drained.
            # Publication is the final synchronous critical section, never an early branch.
            # It renders the report before it writes anything and refuses a report that
            # cannot be sanitized, so the report is sanitized only once, on return.
            _publish(report, context, destinations, execution, outputs, previews)
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
                suggestion="Increase job_timeout_seconds or reduce the input workload and retry.",
            )
        )
        report.outcome = Outcome.BLOCKED
    except asyncio.CancelledError:
        report.outcome = Outcome.CANCELLED
        raise
    return report.redacted_copy()
