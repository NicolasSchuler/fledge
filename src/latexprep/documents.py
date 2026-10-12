"""Several explicitly selected documents of one project, each checked and packaged independently."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from .config import Settings
from .document_run import run_project
from .job_context import (
    DOCUMENT_COMMANDS,
    JobCommand,
    JobContext,
    is_blocked,
    job_outcome,
    select_and_review,
)
from .models import Finding, Outcome, PreparationError, Report
from .project import ImportLimits, import_project
from .publication import DocumentResult
from .review_policy import ReportingOptions, ReviewRule
from .scheduler import bounded_map
from .source_staging import stage_document
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
        )
        package = await run_project(child_context, child)
    except (PreparationError, OSError, ValueError) as error:
        child.findings.append(
            Finding("package.document", str(error), severity="error", status="inconclusive")
        )
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
        raise PreparationError("workflow.documents is supported by inspect, check and prepare")
    if request.reference_pdf is not None:
        raise PreparationError("Configure reference_pdf separately for each document")
    imported = await context.thread_operation(
        "import shared document snapshot",
        import_project,
        request.source,
        context.work / "shared-import",
        ImportLimits(settings.max_files, settings.max_bytes, settings.max_depth),
    )
    report.findings.extend(imported.findings)
    report.changes.extend(imported.changes)
    if is_blocked(report, settings):
        report.outcome = Outcome.BLOCKED
        return []
    documents = await bounded_map(
        settings.workflow.documents,
        lambda document: _run_document(context, imported.root, document),
        limit=1 if request.pdf_previews is not None else settings.jobs,
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
