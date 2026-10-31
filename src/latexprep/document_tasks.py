"""Scheduled tasks of one document and the build and comparison adapters they share.

Builders return tasks for the source analysis, the baseline build and its PDF
checks, and the final verification of a fresh archive extraction. Paths that
are only known once a prerequisite finished are read when the task starts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import TypeVar

from .analysed_source import AnalysedSource
from .author_records import check_author_records
from .bibliography_checks import check_bibliography_coverage_and_fields
from .bibliography_integrity import check_bibliography_integrity
from .build_checks import check_local_package_shadows
from .build_result import BuildResult
from .cancellation import run_in_thread
from .config import Settings
from .errors import PreparationError
from .findings import Finding
from .image_metadata import check_image_metadata
from .job_context import JobCommand, JobContext, pdf_check_settings
from .latexmk_build import SELECTABLE_BUILD_ERRORS
from .manuscript import check_manuscript
from .manuscript_checks import check_manuscript_policies
from .online_checks import check_online_references
from .pdf_check_pipeline import inspect_configured_pdf_checks
from .scheduler import Task
from .source_analysis import analyze_sources
from .submission_checks import check_submission
from .submission_options import SubmissionOptions
from .submission_readiness_checks import check_submission_readiness
from .workflow_options import ComparisonOptions

_T = TypeVar("_T")


class TaskName(StrEnum):
    """Scheduled job tasks; each value is also the stage name in reports.

    Attributes:
        SOURCE_CHECKS: The source analysis.
        BIBLIOGRAPHY_CHECKS: The bibliography integrity checks.
        BIBLIOGRAPHY_COVERAGE: The bibliography coverage and field checks.
        MANUSCRIPT_DETAILS: The manuscript policy checks.
        IMAGE_METADATA_PRIVACY: The image metadata privacy check.
        AUTHOR_FIELDS: The per-author field checks.
        SOURCE_SUBMISSION_CHECKS: The privacy and bundle checks of the original sources.
        ONLINE_REFERENCES: The online reference checks of the original sources.
        MANUSCRIPT_CHECKS: The manuscript checks, which need a selected main file.
        SUBMISSION_READINESS: The submission-readiness advisories.
        BASELINE_BUILD: The build of the unchanged sources.
        BASELINE_CONFIGURED_PDF_CHECKS: The configured PDF checks of the baseline PDF.
        BASELINE_PACKAGE_SHADOWS: The local package shadow check of the baseline build.
        BASELINE_PDF_INSPECTION: The inspection of the baseline PDF.
        REFERENCE_VERSUS_BASELINE: The comparison of the supplied PDF with the baseline.
        SELECT_PREPARATION_INPUTS: The selection of the inputs that preparation copies.
        FINAL_SOURCE_CHECKS: The source checks of the fresh archive extraction.
        FINAL_ONLINE_REFERENCES: The online reference checks of the fresh extraction.
        PREPARED_VERSUS_ARCHIVE: The comparison of the prepared PDF with the PDF rebuilt
            from the archive.
        FINAL_PDF_INSPECTION: The inspection of the rebuilt PDF.
        FINAL_CONFIGURED_PDF_CHECKS: The configured PDF checks of the rebuilt PDF.
        FINAL_SUBMISSION_CHECKS: The submission checks of the fresh extraction.
        REFERENCE_VERSUS_ARCHIVE: The comparison of the supplied PDF with the rebuilt PDF.
    """

    SOURCE_CHECKS = "source checks"
    BIBLIOGRAPHY_CHECKS = "bibliography checks"
    BIBLIOGRAPHY_COVERAGE = "bibliography coverage and fields"
    MANUSCRIPT_DETAILS = "manuscript details"
    IMAGE_METADATA_PRIVACY = "image metadata privacy"
    AUTHOR_FIELDS = "per-author fields"
    SOURCE_SUBMISSION_CHECKS = "source privacy and bundle checks"
    ONLINE_REFERENCES = "online reference checks"
    MANUSCRIPT_CHECKS = "manuscript checks"
    SUBMISSION_READINESS = "submission readiness"
    BASELINE_BUILD = "baseline build"
    BASELINE_CONFIGURED_PDF_CHECKS = "baseline configured PDF checks"
    BASELINE_PACKAGE_SHADOWS = "baseline local package shadows"
    BASELINE_PDF_INSPECTION = "baseline PDF inspection"
    REFERENCE_VERSUS_BASELINE = "supplied PDF versus baseline"
    SELECT_PREPARATION_INPUTS = "select preparation inputs"
    FINAL_SOURCE_CHECKS = "final source checks"
    FINAL_ONLINE_REFERENCES = "final online reference checks"
    PREPARED_VERSUS_ARCHIVE = "prepared versus archive rebuild"
    FINAL_PDF_INSPECTION = "final PDF inspection"
    FINAL_CONFIGURED_PDF_CHECKS = "final configured PDF checks"
    FINAL_SUBMISSION_CHECKS = "final submission checks"
    REFERENCE_VERSUS_ARCHIVE = "supplied PDF versus archive rebuild"


# The stage name of the baseline build in reports, whether the build finished or crashed,
# so that reports and review decisions see one name for it.
BASELINE_STAGE = "baseline"


async def build_document(context: JobContext, tree: Path, main: str, directory: str) -> BuildResult:
    """Build ``main`` from ``tree`` in the new workspace directory ``directory``.

    The build uses the job's backend, settings and runner, and ignores the build log
    checks that the settings disable.

    Args:
        context: The job.
        tree: The source tree to build.
        main: The main file to build.
        directory: The name of the workspace directory, under the job's work directory.

    Returns:
        The build result.
    """
    settings = context.settings
    return await context.backends.build(
        tree,
        main,
        context.work / directory,
        settings.engine,
        context.runner,
        bibliography_backend=settings.bibliography_backend,
        inventory_loaded_options=settings.build_checks.inventory_loaded_options,
        ignored_log_checks=tuple(
            code for code in sorted(SELECTABLE_BUILD_ERRORS) if not settings.checks.enabled(code)
        ),
    )


async def compare_documents(
    context: JobContext,
    left: Path,
    right: Path,
    directory: str,
    stage: str,
    *,
    options: ComparisonOptions | None = None,
) -> list[Finding]:
    """Compare two PDFs in ``directory``, collecting previews for ``stage`` when requested.

    Args:
        context: The job, whose backend compares the PDFs.
        left: The first PDF.
        right: The second PDF.
        directory: The name of the workspace directory, under the job's work directory.
        stage: The stage whose previews are collected.
        options: The comparison options passed to the backend.

    Returns:
        The findings of the comparison.
    """
    return await context.backends.compare_pdfs(
        left,
        right,
        context.work / directory,
        context.runner,
        budget=context.budget,
        options=options,
        previews=context.previews(stage),
    )


def source_submission_options(settings: Settings) -> SubmissionOptions:
    """Return the submission options for source checks; identity terms only while PRV001 runs."""
    options = settings.submission_checks
    # Source and rendered-PDF identity policies share terms but have separate codes.
    return options if settings.checks.enabled("PRV001") else replace(options, identity_terms=())


def prepared_submission_options(
    settings: Settings, path_mapping: Mapping[str, str]
) -> SubmissionOptions:
    """Return the submission options for the shipped sources, with paths in their flattened names.

    Args:
        settings: The job settings.
        path_mapping: The flattened name of each original path; the required deliverables
            and template references are renamed with it, and names it lacks are kept.

    Returns:
        The options of :func:`source_submission_options` with the renamed paths.
    """
    return replace(
        source_submission_options(settings),
        required_deliverables=tuple(
            (path_mapping.get(name, name), kind)
            for name, kind in settings.submission_checks.required_deliverables
        ),
        template_references=tuple(
            (path_mapping.get(name, name), reference)
            for name, reference in settings.submission_checks.template_references
        ),
    )


def source_readiness_findings(
    root: Path, main: str | None, settings: Settings, analysed: AnalysedSource | None = None
) -> list[Finding]:
    """Check the sources under ``root`` for the submission-readiness advisories.

    Args:
        root: The source tree.
        main: The main file, or ``None``.
        settings: The job settings.
        analysed: The shared source analysis, if there is one.

    Returns:
        The findings of the readiness checks.
    """
    return check_submission_readiness(
        root,
        main,
        settings.bbl_checks,
        required_color_space=settings.included_figure_artwork.required_color_space,
        analysed=analysed,
    )


def _selected_main(main: str | None) -> str:
    """Return the main file for a check that is scheduled only once a main file was selected.

    Returns:
        ``main``.

    Raises:
        PreparationError: No main file was selected.
    """
    if main is None:
        raise PreparationError("This check needs a selected main file")
    return main


@dataclass(frozen=True, slots=True)
class SourceCheck:
    """One check of a document's sources, run on the original and again on the final copy.

    Attributes:
        task: The task, which is also the stage name in reports.
        run: The check, called with the root, the main file, the settings and the source
            analysis shared by every check of the same root and main file.
        needs_main: Whether the check needs a selected main file; on the original sources
            it is scheduled only after a main file was selected.
    """

    task: TaskName
    run: Callable[[Path, str | None, Settings, AnalysedSource], list[Finding]]
    needs_main: bool = False


# The checks of the document's sources apart from the source analysis itself and the
# submission policy, which take other inputs on the original and the final copy. The
# initial tasks and the final verification both run exactly these checks, so a new
# source check is added here and to ``TaskName`` only.
_SOURCE_CHECKS = (
    SourceCheck(
        TaskName.BIBLIOGRAPHY_CHECKS,
        lambda root, main, _, analysed: check_bibliography_integrity(root, main, analysed=analysed),
    ),
    SourceCheck(
        TaskName.BIBLIOGRAPHY_COVERAGE,
        lambda root, main, settings, analysed: check_bibliography_coverage_and_fields(
            root, main, settings.bibliography_checks, analysed=analysed
        ),
    ),
    SourceCheck(
        TaskName.MANUSCRIPT_DETAILS,
        lambda root, main, settings, analysed: check_manuscript_policies(
            root, main, settings.manuscript_checks, analysed=analysed
        ),
    ),
    SourceCheck(
        TaskName.IMAGE_METADATA_PRIVACY,
        lambda root, _main, settings, _analysed: check_image_metadata(
            root, settings.metadata_privacy
        ),
    ),
    SourceCheck(
        TaskName.AUTHOR_FIELDS,
        lambda root, main, settings, analysed: check_author_records(
            root, main, settings.author_records, analysed=analysed
        ),
    ),
    SourceCheck(
        TaskName.MANUSCRIPT_CHECKS,
        lambda root, main, settings, analysed: check_manuscript(
            root, _selected_main(main), settings.manuscript, analysed=analysed
        ),
        needs_main=True,
    ),
    SourceCheck(TaskName.SUBMISSION_READINESS, source_readiness_findings, needs_main=True),
)


def _final_source_findings(
    root: Path, main: str, settings: Settings, analysed: AnalysedSource
) -> list[Finding]:
    """Re-evaluate the actual shipped sources after all transformations.

    Args:
        root: The extracted sources.
        main: The main file.
        settings: The job settings.
        analysed: The shared source analysis.

    Returns:
        The findings of the source analysis, followed by those of every source check.
    """
    findings = list(analyze_sources(root, main, analysed=analysed).findings)
    for check in _SOURCE_CHECKS:
        findings.extend(check.run(root, main, settings, analysed))
    return findings


def _original_submission_options(settings: Settings, command: JobCommand) -> SubmissionOptions:
    """Return the submission policy for the original sources of ``command``.

    For ``prepare``, the filename, extension and deliverable constraints are dropped,
    because they target the transformed output.
    """
    submission = source_submission_options(settings)
    if command is JobCommand.PREPARE:
        # These constraints target the transformed output, which may rename or remove files.
        submission = replace(
            submission,
            filename_max_length=None,
            filename_allowed_characters=None,
            allowed_extensions=(),
            required_deliverables=(),
        )
    return submission


def _in_thread_with_analysis(
    context: JobContext,
    function: Callable[..., _T],
    root: Path,
    main: str | None,
    *arguments: object,
) -> Awaitable[_T]:
    """Run ``function(root, main, *arguments, analysed=...)`` in a worker thread.

    The analysis is the one the job shares for this root and main file, so concurrent
    checks of the same sources build their graph once.

    Args:
        context: The job, which shares the analysis.
        function: The check to run.
        root: The source tree.
        main: The main file, or ``None``.
        *arguments: Further arguments for ``function``.

    Returns:
        An awaitable of the result of ``function``.
    """
    return run_in_thread(function, root, main, *arguments, analysed=context.sources.get(root, main))


def _run_source_check(
    context: JobContext, check: SourceCheck, root: Path, main: str | None
) -> Awaitable[list[Finding]]:
    """Run one source check in a worker thread on the job's shared analysis of ``root``.

    Args:
        context: The job, which shares the analysis and supplies the settings.
        check: The source check.
        root: The source tree.
        main: The main file, or ``None``.

    Returns:
        An awaitable of the check's findings.
    """
    return run_in_thread(check.run, root, main, context.settings, context.sources.get(root, main))


def _online_references(
    context: JobContext, root: Path, main: str | None
) -> Awaitable[list[Finding]]:
    """Check the online references of the sources under ``root``, sharing their analysis.

    Args:
        context: The job, which shares the analysis and supplies the online options and
            the budget.
        root: The source tree.
        main: The main file, or ``None``.

    Returns:
        An awaitable of the findings.
    """
    return check_online_references(
        root,
        main,
        options=context.settings.online_checks,
        budget=context.budget,
        analysed=context.sources.get(root, main),
    )


def _configured_pdf_checks(
    context: JobContext, pdf: Path, root: Path, main: str, directory: str
) -> Awaitable[list[Finding]]:
    """Run the configured PDF checks of ``pdf``, with expected values read from ``root``.

    Args:
        context: The job.
        pdf: The PDF to check.
        root: The source tree that the expected values are read from.
        main: The main file.
        directory: The name of the workspace directory, under the job's work directory.

    Returns:
        An awaitable of the findings.
    """
    return inspect_configured_pdf_checks(
        pdf,
        root,
        main,
        context.work / directory,
        context.runner,
        pdf_check_settings(context.settings),
        context.budget,
        analysed=context.sources.get(root, main),
    )


def source_analysis_tasks(
    context: JobContext, snapshot: Callable[[], Path], main: str | None
) -> list[Task]:
    """Build the source, bibliography, manuscript and privacy check tasks of ``snapshot()``.

    Checks that need a main file are left out without one. The online reference checks
    are included unless the job prepares a package for real, which checks them once on
    the final extraction instead.

    Args:
        context: The job.
        snapshot: Returns the source tree to check; it is called when a task starts.
        main: The main file, or ``None``.

    Returns:
        The tasks.
    """
    request, settings = context.request, context.settings
    tasks = [
        Task(
            TaskName.SOURCE_CHECKS,
            lambda: _in_thread_with_analysis(context, analyze_sources, snapshot(), main),
        ),
        *(
            Task(
                check.task,
                lambda check=check: _run_source_check(context, check, snapshot(), main),
            )
            for check in _SOURCE_CHECKS
            if main is not None or not check.needs_main
        ),
    ]
    submission = _original_submission_options(settings, request.command)
    tasks.append(
        Task(
            TaskName.SOURCE_SUBMISSION_CHECKS,
            lambda: _in_thread_with_analysis(
                context, check_submission, snapshot(), main, submission
            ),
        )
    )
    # A released package needs evidence about its transformed references. Run
    # online checks once per document, on the final extraction for preparation,
    # without spending a second request allowance on the original snapshot.
    if request.command is not JobCommand.PREPARE or request.dry_run:
        tasks.append(
            Task.composite(
                TaskName.ONLINE_REFERENCES,
                lambda: _online_references(context, snapshot(), main),
            )
        )
    return tasks


def baseline_build_task(context: JobContext, source: Path, main: str) -> Task:
    """Build the task that builds ``main`` from ``source`` in the ``baseline`` directory.

    The task holds a build lease and succeeds only when the build succeeded with a PDF.
    """
    return Task(
        TaskName.BASELINE_BUILD,
        lambda: build_document(context, source, main, "baseline"),
        memory_mb=context.settings.build_memory_mb,
        kind="build",
        is_success=lambda value: value.success and value.pdf is not None,
    )


def baseline_pdf_tasks(
    context: JobContext,
    baseline: Callable[[], BuildResult],
    baseline_pdf: Callable[[], Path],
    snapshot: Callable[[], Path],
    main: str,
    reference: Path | None,
) -> list[Task]:
    """Build the tasks that check the successful baseline build and its PDF.

    Each task starts only after the baseline build succeeded. The package shadow check
    and the comparison with a reference PDF are included only when they apply.

    Args:
        context: The job.
        baseline: Returns the baseline build result.
        baseline_pdf: Returns the baseline PDF.
        snapshot: Returns the source tree of the baseline.
        main: The main file.
        reference: The PDF the researcher supplied for comparison, or ``None``.

    Returns:
        The tasks.
    """
    settings, work, runner, budget = context.settings, context.work, context.runner, context.budget

    def after_baseline(name: TaskName, run: Callable[[], Awaitable[object]]) -> Task:
        """Make a composite task that starts only after the baseline build succeeded."""
        return Task.composite(name, run, needs_success_of=(TaskName.BASELINE_BUILD,))

    tasks = [
        after_baseline(
            TaskName.BASELINE_CONFIGURED_PDF_CHECKS,
            lambda: _configured_pdf_checks(
                context, baseline_pdf(), snapshot(), main, "additional-baseline"
            ),
        )
    ]
    if settings.build_checks.check_local_package_shadows:
        tasks.append(
            after_baseline(
                TaskName.BASELINE_PACKAGE_SHADOWS,
                lambda: check_local_package_shadows(
                    baseline(), work / "shadows-baseline", runner, budget=budget
                ),
            )
        )
    tasks.append(
        after_baseline(
            TaskName.BASELINE_PDF_INSPECTION,
            lambda: context.backends.inspect_pdf(
                baseline_pdf(),
                work / "inspect-baseline",
                runner,
                settings.max_pages,
                options=settings.pdf,
                budget=budget,
            ),
        )
    )
    if reference is not None:
        tasks.append(
            after_baseline(
                TaskName.REFERENCE_VERSUS_BASELINE,
                lambda: compare_documents(
                    context,
                    reference,
                    baseline_pdf(),
                    "reference-baseline",
                    TaskName.REFERENCE_VERSUS_BASELINE.value,
                ),
            )
        )
    return tasks


@dataclass(frozen=True, slots=True)
class FinalExtraction:
    """The fresh extraction of the archive, and the PDFs its verification compares.

    Attributes:
        root: The directory of the extraction.
        main: The main file.
        staged_pdf: The PDF of the prepared sources.
        final_pdf: The PDF rebuilt from the extraction.
        submission: The submission policy for the extracted sources.
        reference: The PDF the researcher supplied for comparison, or ``None``.
    """

    root: Path
    main: str
    staged_pdf: Path
    final_pdf: Path
    submission: SubmissionOptions
    reference: Path | None


def final_verification_tasks(context: JobContext, extraction: FinalExtraction) -> list[Task]:
    """Build the tasks that check the fresh archive extraction and its rebuilt PDF."""
    return [*_final_source_tasks(context, extraction), *_final_pdf_tasks(context, extraction)]


def _final_source_tasks(context: JobContext, extraction: FinalExtraction) -> list[Task]:
    """Build the tasks that check the extracted sources."""
    root, main, settings = extraction.root, extraction.main, context.settings
    return [
        Task(
            TaskName.FINAL_SOURCE_CHECKS,
            lambda: _in_thread_with_analysis(context, _final_source_findings, root, main, settings),
        ),
        Task.composite(
            TaskName.FINAL_ONLINE_REFERENCES,
            lambda: _online_references(context, root, main),
        ),
        Task(
            TaskName.FINAL_SUBMISSION_CHECKS,
            lambda: _in_thread_with_analysis(
                context, check_submission, root, main, extraction.submission
            ),
        ),
    ]


def _final_pdf_tasks(context: JobContext, extraction: FinalExtraction) -> list[Task]:
    """Build the tasks that check the rebuilt PDF.

    The PDF is compared with the prepared PDF and, when one was supplied, with the
    reference PDF; when previews are collected one at a time, the reference comparison
    waits for the other.
    """
    settings, work, runner, budget = context.settings, context.work, context.runner, context.budget
    root, main, final_pdf = extraction.root, extraction.main, extraction.final_pdf
    tasks = [
        Task.composite(
            TaskName.PREPARED_VERSUS_ARCHIVE,
            lambda: compare_documents(
                context,
                extraction.staged_pdf,
                final_pdf,
                "compare-archive",
                TaskName.PREPARED_VERSUS_ARCHIVE.value,
            ),
        ),
        Task.composite(
            TaskName.FINAL_PDF_INSPECTION,
            lambda: context.backends.inspect_pdf(
                final_pdf,
                work / "inspect-final",
                runner,
                settings.max_pages,
                options=settings.pdf,
                budget=budget,
            ),
        ),
        Task.composite(
            TaskName.FINAL_CONFIGURED_PDF_CHECKS,
            lambda: _configured_pdf_checks(context, final_pdf, root, main, "additional-final"),
        ),
    ]
    if extraction.reference is not None:
        reference = extraction.reference
        tasks.append(
            Task.composite(
                TaskName.REFERENCE_VERSUS_ARCHIVE,
                lambda: compare_documents(
                    context,
                    reference,
                    final_pdf,
                    "reference-final",
                    TaskName.REFERENCE_VERSUS_ARCHIVE.value,
                ),
                # Comparisons that collect previews share one store; run them one at a time.
                waits_for=(TaskName.PREPARED_VERSUS_ARCHIVE,) if context.preview_serial else (),
            )
        )
    return tasks
