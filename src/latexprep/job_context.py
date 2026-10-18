"""The request, settings and resources every stage of one job shares, and its report bookkeeping."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from .cancellation import run_in_thread
from .check_policy import selected_findings
from .config import Settings
from .errors import PreparationError, RequestError
from .findings import Finding
from .path_safety import is_relative_posix_path
from .pdf_check_pipeline import PdfCheckSettings
from .pdf_previews import PdfPreviewCollector, PdfPreviewStore
from .report import Outcome, Report, outcome_from_findings
from .resource_budget import ResourceBudget
from .review_policy import apply_reviews, has_unaccepted_blockers
from .runtime import ToolRunner

R = TypeVar("R")


class JobCommand(StrEnum):
    """The job an interface requests; each value is the command name in reports."""

    INSPECT = "inspect"
    CHECK = "check"
    PREPARE = "prepare"
    BIBLIOGRAPHY = "bib"
    FORMAT = "fmt"
    PDF = "pdf"


# Commands that run the source workflow on one or more selected documents.
DOCUMENT_COMMANDS = frozenset({JobCommand.INSPECT, JobCommand.CHECK, JobCommand.PREPARE})


@dataclass(frozen=True)
class JobRequest:
    command: JobCommand
    source: Path
    settings: Settings
    output: Path | None = None
    dry_run: bool = False
    reference_pdf: Path | None = None
    preview_output: Path | None = None
    pdf_previews: PdfPreviewStore | None = None
    document_name: str | None = None

    def __post_init__(self) -> None:
        # Interfaces may pass the command's string value; an unknown one fails up front.
        try:
            command = JobCommand(self.command)
        except ValueError as error:
            raise PreparationError(f"Unknown job command: {self.command}") from error
        object.__setattr__(self, "command", command)


@dataclass(frozen=True, slots=True)
class JobContext:
    """What every stage of one job reads; ``settings`` are the effective settings."""

    request: JobRequest
    settings: Settings
    work: Path
    runner: ToolRunner
    budget: ResourceBudget
    emit: Callable[[str], None]

    async def thread_operation(
        self, name: str, function: Callable[..., R], *args: Any, **kwargs: Any
    ) -> R:
        """Run a blocking reader or writer in a worker thread under a default lease."""
        async with self.budget.lease(name):
            return await run_in_thread(function, *args, **kwargs)

    def previews(self, stage: str) -> PdfPreviewCollector | None:
        """A preview collector for one PDF comparison when previews were requested."""
        store = self.request.pdf_previews
        return None if store is None else store.for_comparison(stage, self.request.document_name)


def pdf_check_settings(settings: Settings) -> PdfCheckSettings:
    """The options of the configured PDF checks."""
    privacy = settings.submission_checks
    if not settings.checks.enabled("PRV002"):
        # Source and rendered-PDF identity policies share terms but have separate codes.
        privacy = replace(privacy, identity_terms=())
    return PdfCheckSettings(
        max_pages=settings.max_pages,
        inspection=settings.pdf,
        details=settings.pdf_checks,
        rendered_artwork=settings.rendered_pdf_artwork,
        figure_artwork=settings.included_figure_artwork,
        structure=settings.structure_checks,
        privacy=privacy,
        match_source_metadata=settings.match_source_pdf_metadata,
    )


def record_findings(report: Report, findings: Iterable[Finding], stage: str) -> None:
    """Add findings tagged with the stage that produced them."""
    report.findings.extend(
        replace(finding, details={**finding.details, "stage": stage}) for finding in findings
    )


def is_blocked(report: Report, settings: Settings) -> bool:
    """Whether a selected, unaccepted blocking finding stops the job; changes nothing."""
    selected = selected_findings(report.findings, settings.checks)
    return has_unaccepted_blockers(selected, settings.reporting)


def job_outcome(report: Report, settings: Settings) -> Outcome:
    """Classify a completed run from its selected findings."""
    selected = selected_findings(report.findings, settings.checks)
    return outcome_from_findings(
        selected, blocked=has_unaccepted_blockers(selected, settings.reporting)
    )


def select_and_review(report: Report, settings: Settings) -> None:
    """Apply check selection to the report once, then the configured review decisions."""
    report.findings = selected_findings(report.findings, settings.checks)
    reviewed = apply_reviews(report, settings.reporting)
    report.findings, report.outcome = reviewed.findings, reviewed.outcome


def names_project_file(snapshot: Path, main: str) -> bool:
    """Whether ``main`` is a relative path to a file inside the imported project."""
    return is_relative_posix_path(main, normalized=False) and (snapshot / main).is_file()


def main_selection_error(main: str, roots: list[str]) -> RequestError:
    found = (
        f"documents found in the input: {', '.join(roots)}"
        if roots
        else "no file with \\documentclass was found in the input"
    )
    return RequestError(
        f"main must name a source file inside the project: {main}",
        "Set --main (or main in the configuration) to a .tex path relative to the input "
        f"folder or ZIP root; {found}.",
    )
