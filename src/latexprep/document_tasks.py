"""Scheduled tasks of one document and the build and comparison adapters they share.

Builders return tasks for the source analysis, the baseline build and its PDF
checks, and the final verification of a fresh archive extraction. Paths that
are only known once a prerequisite finished are read when the task starts.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import replace
from pathlib import Path

from .author_records import check_author_records
from .bibliography_checks import (
    check_bibliography_coverage_and_fields,
    check_bibliography_integrity,
)
from .build_checks import check_local_package_shadows
from .config import Settings
from .job_context import JobCommand, JobContext, pdf_check_settings
from .latexmk_build import SELECTABLE_BUILD_ERRORS, BuildResult, build_project
from .manuscript import check_manuscript
from .manuscript_checks import check_manuscript_policies
from .metadata_privacy import check_image_metadata
from .models import Finding, TaskName
from .online_checks import check_online_references
from .pdf_check_pipeline import inspect_configured_pdf_checks
from .pdf_comparison import compare_pdfs
from .pdf_inspection import inspect_pdf
from .scheduler import Task, run_in_thread
from .source import analyze_sources
from .submission_checks import SubmissionOptions, check_submission
from .submission_readiness_checks import check_submission_readiness
from .workflow_options import ComparisonOptions


async def build_document(context: JobContext, tree: Path, main: str, directory: str) -> BuildResult:
    """Build ``main`` from ``tree`` in the new workspace directory ``directory``."""
    settings = context.settings
    return await build_project(
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
    """Compare two PDFs in ``directory``, collecting previews for ``stage`` when requested."""
    return await compare_pdfs(
        left,
        right,
        context.work / directory,
        context.runner,
        budget=context.budget,
        options=options,
        previews=context.previews(stage),
    )


def source_submission_options(settings: Settings) -> SubmissionOptions:
    options = settings.submission_checks
    # Source and rendered-PDF identity policies share terms but have separate codes.
    return options if settings.checks.enabled("PRV001") else replace(options, identity_terms=())


def prepared_submission_options(
    settings: Settings, path_mapping: dict[str, str]
) -> SubmissionOptions:
    """Submission options for the shipped sources, with paths in their flattened names."""
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


def _submission_readiness(root: Path, main: str, settings: Settings) -> list[Finding]:
    return check_submission_readiness(
        root,
        main,
        settings.submission_checks,
        required_color_space=settings.included_figure_artwork.required_color_space,
    )


def _source_policies(root: Path, main: str, settings: Settings) -> list[Finding]:
    """Re-evaluate the actual shipped sources after all transformations."""
    return [
        *analyze_sources(root, main).findings,
        *check_bibliography_integrity(root, main),
        *check_bibliography_coverage_and_fields(root, main, settings.bibliography_checks),
        *check_manuscript(root, main, settings.manuscript),
        *check_manuscript_policies(root, main, settings.manuscript_checks),
        *check_image_metadata(root, settings.metadata_privacy),
        *check_author_records(root, main, settings.structure_checks),
        *_submission_readiness(root, main, settings),
    ]


def source_analysis_tasks(
    context: JobContext, snapshot: Callable[[], Path], main: str | None
) -> list[Task]:
    """Source, bibliography, manuscript and privacy checks of ``snapshot()``."""
    request, settings = context.request, context.settings
    tasks = [
        Task(TaskName.SOURCE_CHECKS, lambda: run_in_thread(analyze_sources, snapshot(), main)),
        Task(
            TaskName.BIBLIOGRAPHY_CHECKS,
            lambda: run_in_thread(check_bibliography_integrity, snapshot(), main),
        ),
        Task(
            TaskName.BIBLIOGRAPHY_COVERAGE,
            lambda: run_in_thread(
                check_bibliography_coverage_and_fields,
                snapshot(),
                main,
                settings.bibliography_checks,
            ),
        ),
        Task(
            TaskName.MANUSCRIPT_DETAILS,
            lambda: run_in_thread(
                check_manuscript_policies, snapshot(), main, settings.manuscript_checks
            ),
        ),
        Task(
            TaskName.IMAGE_METADATA_PRIVACY,
            lambda: run_in_thread(check_image_metadata, snapshot(), settings.metadata_privacy),
        ),
        Task(
            TaskName.AUTHOR_FIELDS,
            lambda: run_in_thread(
                check_author_records, snapshot(), main, settings.structure_checks
            ),
        ),
    ]
    submission = source_submission_options(settings)
    if request.command is JobCommand.PREPARE:
        # These constraints target the transformed output, which may rename or remove files.
        submission = replace(
            submission,
            filename_max_length=None,
            filename_allowed_characters=None,
            allowed_extensions=(),
            required_deliverables=(),
        )
    tasks.append(
        Task(
            TaskName.SOURCE_SUBMISSION_CHECKS,
            lambda: run_in_thread(check_submission, snapshot(), main, submission),
        )
    )
    # A released package needs evidence about its transformed references. Run
    # online checks once per document, on the final extraction for preparation,
    # without spending a second request allowance on the original snapshot.
    if request.command is not JobCommand.PREPARE or request.dry_run:
        tasks.append(
            Task(
                TaskName.ONLINE_REFERENCES,
                lambda: check_online_references(
                    snapshot(), main, options=settings.online_checks, budget=context.budget
                ),
                cpu=0,
                memory_mb=0,
            )
        )
    if main is None:
        return tasks
    tasks.append(
        Task(
            TaskName.MANUSCRIPT_CHECKS,
            lambda: run_in_thread(check_manuscript, snapshot(), main, settings.manuscript),
        )
    )
    tasks.append(
        Task(
            TaskName.SUBMISSION_READINESS,
            lambda: run_in_thread(_submission_readiness, snapshot(), main, settings),
        )
    )
    return tasks


def baseline_build_task(context: JobContext, source: Path, main: str) -> Task:
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
    """Checks of the successful baseline build and its PDF."""
    settings, work, runner, budget = context.settings, context.work, context.runner, context.budget

    def after_baseline(name: TaskName, run: Callable[[], Awaitable[object]]) -> Task:
        return Task(name, run, needs_success_of=(TaskName.BASELINE_BUILD,), cpu=0, memory_mb=0)

    tasks = [
        after_baseline(
            TaskName.BASELINE_CONFIGURED_PDF_CHECKS,
            lambda: inspect_configured_pdf_checks(
                baseline_pdf(),
                snapshot(),
                main,
                work / "additional-baseline",
                runner,
                pdf_check_settings(settings),
                budget,
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
            lambda: inspect_pdf(
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


def final_verification_tasks(
    context: JobContext,
    root: Path,
    main: str,
    *,
    staged_pdf: Path,
    final_pdf: Path,
    submission: SubmissionOptions,
    reference: Path | None,
) -> list[Task]:
    """Checks of the fresh archive extraction ``root`` and its rebuilt ``final_pdf``."""
    settings, work, runner, budget = context.settings, context.work, context.runner, context.budget
    tasks = [
        Task(
            TaskName.FINAL_SOURCE_CHECKS,
            lambda: run_in_thread(_source_policies, root, main, settings),
        ),
        Task(
            TaskName.FINAL_ONLINE_REFERENCES,
            lambda: check_online_references(
                root, main, options=settings.online_checks, budget=budget
            ),
            cpu=0,
            memory_mb=0,
        ),
        Task(
            TaskName.PREPARED_VERSUS_ARCHIVE,
            lambda: compare_documents(
                context,
                staged_pdf,
                final_pdf,
                "compare-archive",
                TaskName.PREPARED_VERSUS_ARCHIVE.value,
            ),
            cpu=0,
            memory_mb=0,
        ),
        Task(
            TaskName.FINAL_PDF_INSPECTION,
            lambda: inspect_pdf(
                final_pdf,
                work / "inspect-final",
                runner,
                settings.max_pages,
                options=settings.pdf,
                budget=budget,
            ),
            cpu=0,
            memory_mb=0,
        ),
        Task(
            TaskName.FINAL_CONFIGURED_PDF_CHECKS,
            lambda: inspect_configured_pdf_checks(
                final_pdf,
                root,
                main,
                work / "additional-final",
                runner,
                pdf_check_settings(settings),
                budget,
            ),
            cpu=0,
            memory_mb=0,
        ),
        Task(
            TaskName.FINAL_SUBMISSION_CHECKS,
            lambda: run_in_thread(check_submission, root, main, submission),
        ),
    ]
    if reference is not None:
        tasks.append(
            Task(
                TaskName.REFERENCE_VERSUS_ARCHIVE,
                lambda: compare_documents(
                    context,
                    reference,
                    final_pdf,
                    "reference-final",
                    TaskName.REFERENCE_VERSUS_ARCHIVE.value,
                ),
                # Comparisons that collect previews share one store; run them one at a time.
                waits_for=(TaskName.PREPARED_VERSUS_ARCHIVE,)
                if context.request.pdf_previews is not None
                else (),
                cpu=0,
                memory_mb=0,
            )
        )
    return tasks
