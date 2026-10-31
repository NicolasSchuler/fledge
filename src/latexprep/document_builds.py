"""The builds of one document beyond the scheduled baseline: staged rebuilds and repeats.

Each build runs under a build lease and is recorded in the report like the baseline
build, with its own time and its wait for the lease. This module does not decide which
builds a preparation needs; ``DocumentRun`` and ``PackageVerification`` call it.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from .build_checks import check_local_package_shadows
from .build_result import BuildResult
from .cancellation import bounded_map
from .document_tasks import build_document, compare_documents
from .findings import Finding, has_blockers
from .job_context import JobContext
from .job_report import record_findings
from .pdf_geometry import COMPARISON_DPI
from .task_reporting import TaskReporter


@dataclass(frozen=True, slots=True)
class BuildStage:
    """A rebuild of the document: its name in reports, and its workspace directory.

    Attributes:
        label: The stage name in reports.
        directory: The name of the workspace directory of the build.
    """

    label: str
    directory: str


TRANSFORMED = BuildStage("transformed", "transformed-build")
PREPARED = BuildStage("prepared", "staged-build")
ARCHIVE = BuildStage("archive", "archive-build")


class _TimedBuild(NamedTuple):
    """A build result with the time the build took and the time it waited for its lease.

    Attributes:
        result: The build result.
        elapsed_seconds: The build's own time, in seconds.
        queue_seconds: The wait for the build lease, in seconds.
    """

    result: BuildResult
    elapsed_seconds: float
    queue_seconds: float


class _RepeatedBaseline(NamedTuple):
    """One repeat of the baseline build and its comparison with the first build.

    Attributes:
        stage: The stage of the repeat.
        timed: The repeated build and its times.
        comparison: The findings of comparing the repeat's PDF with the first PDF; empty
            when the repeat failed or produced no PDF.
    """

    stage: BuildStage
    timed: _TimedBuild
    comparison: list[Finding]


async def _leased_build(
    context: JobContext, lease_name: str, tree: Path, main: str, directory: str
) -> _TimedBuild:
    """Build under a build lease; report the build's own time and the wait for the lease.

    Args:
        context: The job, whose budget grants the lease.
        lease_name: The name of the lease.
        tree: The source tree to build.
        main: The main file to build.
        directory: The name of the new workspace directory of the build.

    Returns:
        The build result with both times.
    """
    async with context.budget.lease(
        lease_name, memory_mb=context.settings.build_memory_mb, kind="build"
    ) as lease:
        started = time.monotonic()
        result = await build_document(context, tree, main, directory)
        elapsed = time.monotonic() - started
    return _TimedBuild(result, elapsed, lease.queue_seconds)


async def build_stage(
    context: JobContext, reporter: TaskReporter, tree: Path, main: str, stage: BuildStage
) -> BuildResult:
    """Build one staged tree under a build lease and record it like the baseline.

    A successful build without submission-input evidence adds an inconclusive
    ``package.dependencies`` error, and the local package shadow check runs when the
    settings enable it.

    Args:
        context: The job.
        reporter: The reporter that records the build and its findings.
        tree: The staged source tree.
        main: The main file to build.
        stage: The stage that names the build and its workspace directory.

    Returns:
        The build result.
    """
    settings, report = context.settings, reporter.report
    context.emit(f"Rebuilding {stage.label} sources")
    timed = await _leased_build(context, f"{stage.label} build", tree, main, stage.directory)
    result = timed.result
    # Every build records its time, like the scheduled baseline build.
    built = reporter.record_build(
        result,
        stage.label,
        elapsed_seconds=timed.elapsed_seconds,
        queue_seconds=timed.queue_seconds,
    )
    context.emit(
        f"{stage.label} build: {'succeeded' if built else 'failed'} ({timed.elapsed_seconds:.1f}s)"
    )
    if result.success and result.submission_inputs is None:
        record_findings(
            report,
            [
                Finding(
                    "package.dependencies",
                    "Complete build dependency evidence is unavailable.",
                    severity="error",
                    status="inconclusive",
                    suggestion="Resolve the submission dependency diagnostic and retry.",
                )
            ],
            stage.label,
        )
    if settings.build_checks.check_local_package_shadows:
        record_findings(
            report,
            await check_local_package_shadows(
                result,
                context.work / f"shadows-{stage.label}",
                context.runner,
                budget=context.budget,
            ),
            stage.label,
        )
    return result


async def repeat_baselines(
    context: JobContext,
    reporter: TaskReporter,
    source: Path,
    main: str,
    first_pdf: Path,
) -> None:
    """Rebuild the unchanged baseline and require identical evidence (CMP101).

    The ``workflow.baseline_runs`` setting counts the first build, so one fewer repeat is
    built. Every repeat and its comparison with the first PDF are recorded, followed by a
    ``compare.baseline_stability`` finding that fails unless every repeat built a PDF
    without a blocking difference.

    Args:
        context: The job.
        reporter: The reporter that records the builds and findings.
        source: The unchanged source tree.
        main: The main file to build.
        first_pdf: The PDF of the first baseline build.
    """
    settings = context.settings

    async def attempt(number: int) -> _RepeatedBaseline:
        """Build repeat ``number`` and compare its PDF with the first one."""
        stage = BuildStage(f"baseline repeat {number}", f"baseline-{number}")
        timed = await _leased_build(
            context, f"repeat baseline {number}", source, main, stage.directory
        )
        repeated = timed.result
        if not repeated.success or repeated.pdf is None:
            return _RepeatedBaseline(stage, timed, [])
        compared = await compare_documents(
            context, first_pdf, repeated.pdf, f"compare-baseline-{number}", stage.label
        )
        return _RepeatedBaseline(stage, timed, compared)

    # Independent repeats overlap up to the configured build concurrency.
    attempts = await bounded_map(
        list(range(2, settings.workflow.baseline_runs + 1)),
        attempt,
        limit=1 if context.preview_serial else settings.build_jobs,
    )
    stable = True
    for attempted in attempts:
        repeated = attempted.timed.result
        reporter.record_build(
            repeated,
            attempted.stage.label,
            elapsed_seconds=attempted.timed.elapsed_seconds,
            queue_seconds=attempted.timed.queue_seconds,
        )
        record_findings(reporter.report, attempted.comparison, attempted.stage.label)
        if not repeated.success or repeated.pdf is None or has_blockers(attempted.comparison):
            stable = False
    reporter.report.findings.append(
        Finding(
            "compare.baseline_stability",
            f"{settings.workflow.baseline_runs} independent baseline builds match."
            if stable
            else "Baseline builds are inconsistent or could not be fully compared.",
            severity="info" if stable else "error",
            status="passed" if stable else "failed",
            details={
                "requested_builds": settings.workflow.baseline_runs,
                "comparison": "exact page count, normalized extracted text and "
                f"{COMPARISON_DPI}-DPI pixels",
            },
        )
    )
