"""Several explicitly selected documents of one project, each checked and packaged independently."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .cancellation import bounded_map
from .config import Settings
from .defects import REPORT_DEFECT_SUGGESTION, is_expected_failure, summarize_traceback
from .document_run import run_project
from .errors import PreparationError
from .findings import Finding
from .job_context import DOCUMENT_COMMANDS, JobCommand, JobContext, import_input
from .job_report import is_blocked, job_outcome, select_and_review
from .publication import DocumentResult
from .report import Outcome, Report
from .review_policy import ReportingOptions, ReviewRule
from .staging import stage_document
from .workflow_options import DocumentOptions


def _document_settings(settings: Settings, document: DocumentOptions) -> Settings:
    return replace(
        settings,
        main=document.main,
        engine=document.engine or settings.engine,
        workflow=replace(settings.workflow, documents=()),
    )


def _document_rules(rules: tuple[ReviewRule, ...], name: str) -> tuple[ReviewRule, ...]:
    """The review decisions for every document or this one, no longer document-specific."""
    return tuple(replace(rule, document=None) for rule in rules if rule.document in {None, name})


def _document_reporting(reporting: ReportingOptions, name: str) -> ReportingOptions:
    return replace(
        reporting,
        accepted_exceptions=_document_rules(reporting.accepted_exceptions, name),
        suppressions=_document_rules(reporting.suppressions, name),
    )


def _failed_document_finding(error: Exception, document: DocumentOptions) -> Finding:
    """Say why one selected document could not be checked or packaged.

    Expected failures point at the document's own settings (the command-line
    advice of the underlying error does not apply to ``workflow.documents``). A
    defect is reported as one, with its traceback frames, instead of passing for
    an input problem.
    """
    if not is_expected_failure(error):
        return Finding(
            "package.document",
            f"Document {document.name} stopped unexpectedly: {type(error).__name__}: {error}",
            severity="error",
            status="inconclusive",
            suggestion=REPORT_DEFECT_SUGGESTION,
            details={"traceback": list(summarize_traceback(error))},
        )
    if isinstance(error, PreparationError):
        suggestion = (
            f"Check the settings of document '{document.name}' under workflow.documents "
            "(main, source_directory, include, reference_pdf) and the files they select, "
            "then run the command again."
        )
    else:
        suggestion = (
            "Check that the input files are readable and the temporary directory has free "
            "space, then run the command again."
        )
    return Finding(
        "package.document",
        str(error),
        severity="error",
        status="inconclusive",
        suggestion=suggestion,
    )


async def _run_document(
    context: JobContext, shared_root: Path, document: DocumentOptions
) -> DocumentResult:
    request = context.request
    child = Report(request.command.value)
    settings = _document_settings(context.settings, document)
    child.settings = settings.to_dict()
    settings = replace(settings, reporting=_document_reporting(settings.reporting, document.name))
    try:
        source = await context.thread_operation(
            f"select {document.name} files",
            stage_document,
            shared_root,
            context.work / "selected" / document.name,
            document,
        )
        child_request = replace(
            request,
            source=source,
            settings=_document_settings(request.settings, document),
            reference_pdf=Path(document.reference_pdf) if document.reference_pdf else None,
            document_name=document.name,
        )
        child_work = context.work / "documents" / document.name
        child_work.mkdir(parents=True)
        child_context = JobContext(
            child_request,
            settings,
            child_work,
            context.runner,
            context.budget,
            lambda message: context.emit(f"{document.name}: {message}"),
            backends=context.backends,
        )
        package = await run_project(child_context, child)
    except Exception as error:  # noqa: BLE001 - one failed document becomes a finding; the rest run
        child.findings.append(_failed_document_finding(error, document))
        child.outcome = Outcome.BLOCKED
        package = None
    select_and_review(child, settings)
    return DocumentResult(document.name, child, package)


def _merge_document(report: Report, document: DocumentResult) -> None:
    """Add one document's results to the combined report, labelled with its name."""
    child = document.report
    report.findings.extend(
        replace(item, details={**item.details, "document": document.name})
        for item in child.findings
    )
    report.changes.extend(
        replace(
            change,
            path=f"{document.name}/{change.path}",
            destination=f"{document.name}/{change.destination}" if change.destination else None,
        )
        for change in child.changes
    )
    report.stages.extend(
        {**stage, "name": f"{document.name}: {stage['name']}", "document": document.name}
        for stage in child.stages
    )
    report.tools.update(child.tools)
    report.package_contents.extend(child.package_contents)


async def run_documents(context: JobContext, report: Report) -> list[DocumentResult]:
    """Check, and for preparation verify, every configured document from one shared import."""
    request, settings = context.request, context.settings
    if request.command not in DOCUMENT_COMMANDS:
        # ``run_job`` routes only these commands here; the others ignore workflow.documents.
        raise PreparationError("workflow.documents is supported by inspect, check and prepare")
    imported = await import_input(
        context, report, operation="import shared document snapshot", directory="shared-import"
    )
    if is_blocked(report, settings):
        report.outcome = Outcome.BLOCKED
        return []
    documents = await bounded_map(
        settings.workflow.documents,
        lambda document: _run_document(context, imported.root, document),
        limit=1 if context.preview_serial else settings.jobs,
    )
    for document in documents:
        _merge_document(report, document)
    verified = all(document.package is not None for document in documents)
    if request.command is JobCommand.PREPARE and not request.dry_run:
        report.findings.append(
            Finding(
                "package.independent_documents",
                f"{len(documents)} selected document packages independently verified."
                if verified
                else "At least one selected document package failed verification; "
                "no package will be released.",
                severity="info" if verified else "error",
                status="passed" if verified else "failed",
                details={
                    "documents": [
                        {
                            "name": item.name,
                            "outcome": item.report.outcome,
                            "verified": item.package is not None,
                        }
                        for item in documents
                    ]
                },
            )
        )
    report.scope = (
        "all explicitly selected independent document packages; "
        "each has its own checks and archive rebuild"
    )
    report.outcome = (
        Outcome.PLANNED
        if request.dry_run and not is_blocked(report, settings)
        else job_outcome(report, settings)
    )
    return documents
