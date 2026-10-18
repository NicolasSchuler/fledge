"""The request, settings and resources every stage of one job shares.

Report bookkeeping lives in ``job_report`` and choosing the main file in
``main_selection``; this module holds only what every stage reads or calls.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import ParamSpec, TypeVar

from .cancellation import run_in_thread
from .config import Settings
from .errors import PreparationError
from .import_budget import ImportLimits
from .pdf_check_pipeline import PdfCheckSettings
from .pdf_previews import PdfPreviewCollector, PdfPreviewStore
from .project import ImportedProject, import_project
from .report import Report
from .resource_budget import ResourceBudget
from .runtime import ToolRunner

P = ParamSpec("P")
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
# Commands that can export page previews, and so need a directory to save them in.
PREVIEW_COMMANDS = frozenset({JobCommand.CHECK, JobCommand.PDF, JobCommand.PREPARE})


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
        self, name: str, function: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        """Run a blocking reader or writer in a worker thread under a default lease."""
        async with self.budget.lease(name):
            return await run_in_thread(function, *args, **kwargs)

    @property
    def preview_serial(self) -> bool:
        """Whether comparisons must run one at a time.

        Comparisons that collect previews share one store, so they cannot overlap.
        """
        return self.request.pdf_previews is not None

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


async def import_input(
    context: JobContext,
    report: Report,
    *,
    operation: str = "import project",
    directory: str = "import",
) -> ImportedProject:
    """Import a separate snapshot of the input; its findings and changes enter the report."""
    imported = await context.thread_operation(
        operation,
        import_project,
        context.request.source,
        context.work / directory,
        ImportLimits.from_settings(context.settings),
    )
    report.findings.extend(imported.findings)
    report.changes.extend(imported.changes)
    return imported
