"""The request, settings and resources every stage of one job shares.

Report bookkeeping lives in ``job_report`` and choosing the main file in
``main_selection``; this module holds only what every stage reads or calls.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace
from enum import StrEnum
from pathlib import Path
from typing import ParamSpec, TypeVar

from .analysed_source import AnalysedSourceCache
from .build_result import BuildResult
from .cancellation import run_in_thread
from .config import Settings
from .errors import PreparationError
from .findings import Finding
from .import_budget import ImportLimits
from .latexmk_build import build_project
from .pdf_check_pipeline import PdfCheckSettings
from .pdf_comparison import compare_pdfs
from .pdf_inspection import inspect_pdf
from .pdf_previews import PdfPreviewCollector, PdfPreviewStore
from .project import ImportedProject, import_project
from .report import Report
from .resource_budget import ResourceBudget
from .runtime import ToolRunner

P = ParamSpec("P")
R = TypeVar("R")


class JobCommand(StrEnum):
    """The job an interface requests; each value is the command name in reports.

    Attributes:
        INSPECT: Check sources and bibliography without running TeX.
        CHECK: Build the PDF and check the sources and the PDF.
        PREPARE: Create a verified submission copy of the paper.
        BIBLIOGRAPHY: Bibliography checks, named ``bib``.
        FORMAT: Check tex-fmt formatting without changing any file, named ``fmt``.
        PDF: Check a PDF without its source project.
    """

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
    """What an interface asks of one job: the command, its input, settings and destinations.

    Construction raises ``PreparationError`` when the command is unknown; interfaces may
    pass the command's string value, which construction converts.

    Attributes:
        command: The job to run.
        source: The input the job reads: a project, or the PDF of a standalone PDF check.
        settings: The settings of the job.
        output: The directory a ``prepare`` job publishes to, or ``None``.
        dry_run: Whether a ``prepare`` job only plans its changes and publishes nothing.
        reference_pdf: A supplied PDF to compare the built or inspected PDF with, or
            ``None``.
        preview_output: The directory that receives page previews, or ``None``.
        pdf_previews: The store that collects page previews, or ``None`` when no previews
            were requested.
        document_name: The configured name of the document when the request is one
            document of a multi-document job, else ``None``.
    """

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
class DocumentBackends:
    """The tool-driven operations a document's tasks call; the defaults run the real tools.

    This is the injection point for the three slow operations of a document job: building
    a document, inspecting a PDF and comparing two PDFs. Each takes and returns what the
    function it defaults to does, so a controlled replacement needs no patching of module
    globals. ``run_job`` accepts one and hands it to every stage and document of the job.

    Attributes:
        build: Builds a document; defaults to :func:`build_project`.
        inspect_pdf: Inspects a built PDF; defaults to :func:`inspect_pdf`.
        compare_pdfs: Compares two PDFs; defaults to :func:`compare_pdfs`.
    """

    build: Callable[..., Awaitable[BuildResult]] = build_project
    inspect_pdf: Callable[..., Awaitable[list[Finding]]] = inspect_pdf
    compare_pdfs: Callable[..., Awaitable[list[Finding]]] = compare_pdfs


@dataclass(frozen=True, slots=True)
class JobContext:
    """What every stage of one job reads.

    A document of a multi-document job has its own context and so its own analyses.

    Attributes:
        request: The request the job runs.
        settings: The effective settings.
        work: The job's private working directory.
        runner: The restricted tool runner of the job.
        budget: The resource budget that leases are taken from.
        emit: Reports a progress message.
        sources: The source analysis of each project root the job's checks read, so that
            checks of the same sources share one graph.
        backends: The operations that drive real tools.
    """

    request: JobRequest
    settings: Settings
    work: Path
    runner: ToolRunner
    budget: ResourceBudget
    emit: Callable[[str], None]
    sources: AnalysedSourceCache = field(default_factory=AnalysedSourceCache)
    backends: DocumentBackends = field(default_factory=DocumentBackends)

    async def thread_operation(
        self, name: str, function: Callable[P, R], /, *args: P.args, **kwargs: P.kwargs
    ) -> R:
        """Run a blocking reader or writer in a worker thread under a default lease.

        Args:
            name: The name of the lease, which also names the operation.
            function: The blocking function to run.
            *args: The positional arguments of ``function``.
            **kwargs: The keyword arguments of ``function``.

        Returns:
            What ``function`` returns.
        """
        async with self.budget.lease(name):
            return await run_in_thread(function, *args, **kwargs)

    @property
    def preview_serial(self) -> bool:
        """Whether comparisons must run one at a time.

        Comparisons that collect previews share one store, so they cannot overlap.
        """
        return self.request.pdf_previews is not None

    def previews(self, stage: str) -> PdfPreviewCollector | None:
        """Open a preview collector for one PDF comparison when previews were requested.

        Args:
            stage: The comparison the previews belong to.

        Returns:
            The collector, labelled with the document name, or ``None`` when no previews
            were requested.
        """
        store = self.request.pdf_previews
        return None if store is None else store.for_comparison(stage, self.request.document_name)


def pdf_check_settings(settings: Settings) -> PdfCheckSettings:
    """Gather the options of the configured PDF checks from the settings.

    Identity terms reach the PDF privacy check only while ``PRV002`` is enabled.

    Args:
        settings: The effective settings.

    Returns:
        The options of the PDF check pipeline.
    """
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
    """Import a separate snapshot of the input; its findings and changes enter the report.

    Args:
        context: The job context, whose request names the input and whose settings bound
            the import.
        report: The report that receives the import's findings and changes.
        operation: The name of the lease the import runs under.
        directory: The folder under the job's working directory that receives the snapshot.

    Returns:
        The imported project.
    """
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
