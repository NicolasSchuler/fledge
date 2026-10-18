"""Isolated latexmk builds of one main file, with fresh and bounded build evidence.

Every build copies the project into a new workspace, runs latexmk with a static
trusted configuration under the runner's sandbox, and judges the result only
from evidence that build produced: its final log, recorder trace, rule database,
bibliography control files and PDF. This module orchestrates one build and turns its
evidence into findings; the workspace, the command line, the recorder, the
bibliography backends and the result types are the sibling modules ``build_workspace``,
``latexmk_command``, ``build_recorder``, ``build_bibliography`` and ``build_result``.
"""

from __future__ import annotations

import re
import shutil
from dataclasses import KW_ONLY, dataclass, replace
from pathlib import Path, PurePosixPath

from .bibliography_controls import read_bibliography_evidence
from .build_bibliography import (
    BIBER_PREPARED_AFTER_DEFERRAL,
    BiberRequired,
    BiberSetup,
    backends_to_verify,
    bibliography_policy,
    plan_biber,
    prepare_biber,
    verify_backend_tools,
)
from .build_dependencies import (
    DependencyRequest,
    SubmissionDependencies,
    collect_submission_dependencies,
)
from .build_log import log_findings, log_tail, read_bounded
from .build_recorder import RecorderEvidence, read_recorder
from .build_result import BibliographyRecord, BuildResult, ExecutionPolicy
from .build_workspace import BuildLayout, prepare_layout
from .cancellation import run_in_thread
from .command_runner import CommandFailure, CommandResult, RuntimeLimits, ToolVersion
from .errors import PreparationError, ToolUnavailableError
from .findings import Finding
from .latexmk_command import BiberPlan, controlled_rules, latexmk_command
from .loaded_options import (
    LoadedOptionsEvidence,
    LoadedOptionsProbe,
    collect_loaded_options,
    loaded_options_unavailable,
    stage_loaded_options,
)
from .option_config import require_unique_strings, validate_scalars
from .path_safety import is_relative_posix_path
from .runtime import ToolRunner
from .setting_choices import BibliographyBackend, Engine

# Only these policy-level log errors may be deselected. Tool failures, incomplete
# output/recorder evidence, TeX errors and unresolved reruns remain build failures.
SELECTABLE_BUILD_ERRORS = frozenset({"BLD001", "BLD002"})
_MAIN_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+")
_PDF_HEADER = b"%PDF-"
# The part of latexmk's standard error a build finding keeps in its details.
_STDERR_TAIL_CHARS = 4000
_FAILURE_RULES = {
    CommandFailure.TIMEOUT: "build.timeout",
    CommandFailure.OUTPUT_LIMIT: "build.output_limit",
    CommandFailure.RESOURCE_LIMIT: "build.resource_limit",
    CommandFailure.EXIT_STATUS: "build.failed",
}


@dataclass(frozen=True, slots=True)
class BuildRequest:
    """One isolated build of ``main`` from ``source`` in the new directory ``work``."""

    source: Path
    main: str
    work: Path
    engine: Engine
    _: KW_ONLY
    bibliography_backend: BibliographyBackend = "auto"
    inventory_loaded_options: bool = False
    ignored_log_checks: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_scalars(self)
        require_unique_strings(
            "ignored_log_checks", self.ignored_log_checks, choices=SELECTABLE_BUILD_ERRORS
        )
        if PurePosixPath(self.main).suffix != ".tex" or not is_relative_posix_path(
            self.main, normalized=False, component_pattern=_MAIN_COMPONENT
        ):
            raise PreparationError(
                "Build main must be a relative .tex path with letters, digits, underscores, "
                "dots or hyphens; other filename forms are not yet supported."
            )

    @property
    def stem(self) -> str:
        return PurePosixPath(self.main).stem


@dataclass(frozen=True, slots=True)
class _BibliographyOutcome:
    """The bibliography record of a build, its findings and the backend versions verified."""

    record: BibliographyRecord
    findings: list[Finding]
    versions: dict[str, ToolVersion]


@dataclass(frozen=True, slots=True)
class _LogEvidence:
    """The bounded final-pass log and the findings read from it."""

    text: str
    findings: list[Finding]


async def build_project(
    source: Path,
    main: str,
    work: Path,
    engine: Engine,
    runner: ToolRunner,
    *,
    bibliography_backend: BibliographyBackend = "auto",
    inventory_loaded_options: bool = False,
    ignored_log_checks: tuple[str, ...] = (),
) -> BuildResult:
    """Build ``main`` in isolation, preparing Biber for an automatic strategy only when needed.

    On macOS, preparing Biber copies and unpacks a large universal binary, which
    costs seconds per job. Unless the sources mention biblatex, an automatic build
    first runs with Biber blocked. Only if that build writes a fresh .bcf is it
    discarded and rebuilt in the same, emptied workspace with Biber prepared.
    """
    request = BuildRequest(
        source,
        main,
        work,
        engine,
        bibliography_backend=bibliography_backend,
        inventory_loaded_options=inventory_loaded_options,
        ignored_log_checks=ignored_log_checks,
    )
    plan = await plan_biber(runner, bibliography_backend, source)
    if not plan.deferred:
        return await _build_once(request, runner, plan)
    try:
        return await _build_once(request, runner, plan)
    except BiberRequired:
        await run_in_thread(shutil.rmtree, work)
    return await _build_once(
        request, runner, BiberPlan(deferred=False, note=BIBER_PREPARED_AFTER_DEFERRAL)
    )


async def _build_once(request: BuildRequest, runner: ToolRunner, biber: BiberPlan) -> BuildResult:
    """Run one isolated build; raise ``BiberRequired`` if a deferred build needs Biber."""
    layout = await run_in_thread(
        prepare_layout, request.source, request.main, request.work, runner.limits.max_file_bytes
    )
    staged = _stage_loaded_options(request, layout)
    biber_setup = await prepare_biber(
        request.bibliography_backend, runner, biber, layout.work.parent
    )
    rules = controlled_rules(request.bibliography_backend, biber, biber_setup.executable)
    result = BuildResult(
        success=False, command=latexmk_command(request.engine, request.main, layout, rules)
    )
    policy = ExecutionPolicy(runner.sandbox.description, runner.limits, runner.source_date_epoch)
    result.tools.execution = policy
    try:
        result.tools.versions["latexmk"] = await runner.tool_version(
            ["latexmk", "-norc", "-v"], workspace=layout.work
        )
        result.tools.versions[request.engine] = await runner.tool_version(
            [request.engine, "--version"], workspace=layout.work
        )
        execution = await runner.run(result.command, cwd=layout.cwd, workspace=layout.work)
    except PreparationError as error:
        result.findings.extend(_unavailable_findings(error, request))
        return result
    result.command = execution.resolved_command
    result.tools.execution = replace(policy, duration_seconds=execution.duration_seconds)
    declared_roots = (
        tuple(Path(root) for root in execution.resource_roots)
        if execution.resource_roots
        else tuple(runner.resource_roots)
    )
    result.tools.resource_roots = tuple(sorted(str(root) for root in declared_roots))
    max_output_bytes = runner.limits.max_output_bytes
    log = _read_log(request, layout, max_output_bytes)
    result.log = log.text
    result.findings.extend(log.findings)
    result.findings.extend(_execution_findings(execution, request.main, log.text, runner.limits))
    recorder = read_recorder(request.main, layout, declared_roots, max_output_bytes)
    result.findings.extend(recorder.findings)
    result.recorder_complete = recorder.complete
    result.recorder_project_inputs = set(recorder.project_inputs)
    result.loaded_packages = list(recorder.loaded_packages)
    submission = collect_submission_dependencies(
        DependencyRequest(
            layout=layout,
            main=request.main,
            engine=request.engine,
            recorder=recorder.trace,
            recorder_complete=recorder.complete,
            resource_roots=declared_roots,
            max_bytes=max_output_bytes,
            owned_inputs=(staged.hook,) if isinstance(staged, LoadedOptionsProbe) else (),
        )
    )
    result.submission_inputs = submission.inputs
    result.generated_reads = submission.generated_reads
    result.tools.submission_dependencies = submission
    result.findings.extend(_submission_findings(submission, request.main))
    bibliography = await _judge_bibliography(request, runner, layout, biber, biber_setup)
    result.tools.bibliography = bibliography.record
    result.findings.extend(bibliography.findings)
    result.tools.versions.update(bibliography.versions)
    pdf = layout.output / f"{request.stem}.pdf"
    complete_pdf = _is_complete_pdf(pdf, runner.limits.max_file_bytes)
    if not complete_pdf:
        result.findings.append(
            Finding(
                "build.missing_pdf",
                "No complete new PDF was produced.",
                severity="error",
                path=request.main,
                details={"log_tail": log_tail(result.log)},
            )
        )
    built = complete_pdf and not any(
        item.severity == "error" and item.code not in request.ignored_log_checks
        for item in result.findings
    )
    options = (
        _loaded_options(request, staged, recorder, built, max_output_bytes)
        if staged is not None
        else None
    )
    if options is not None:
        result.tools.loaded_options = options
        result.findings.append(options.finding(request.main))
    result.success = built and (options is None or options.complete)
    if result.success:
        result.pdf = pdf
        result.findings.append(
            Finding(
                "build.completed",
                "Fresh isolated build completed.",
                severity="info",
                status="passed",
                path=request.main,
            )
        )
    return result


async def _judge_bibliography(
    request: BuildRequest,
    runner: ToolRunner,
    layout: BuildLayout,
    biber: BiberPlan,
    biber_setup: BiberSetup,
) -> _BibliographyOutcome:
    """Compare the fresh control files with the strategy and verify the backends they need.

    Raises ``BiberRequired`` when a build that deferred Biber wrote a Biber control file.
    """
    evidence = read_bibliography_evidence(layout.output, runner.limits.max_output_bytes)
    if biber.deferred and "biber" in evidence.observed_backends:
        raise BiberRequired
    verification = await verify_backend_tools(
        runner,
        layout.work,
        request.main,
        backends_to_verify(request.bibliography_backend, evidence),
        biber_setup.error,
    )
    return _BibliographyOutcome(
        BibliographyRecord(request.bibliography_backend, evidence, biber.note),
        [
            bibliography_policy(evidence, request.bibliography_backend, request.main),
            *verification.findings,
        ],
        verification.versions,
    )


def _stage_loaded_options(
    request: BuildRequest, layout: BuildLayout
) -> LoadedOptionsProbe | LoadedOptionsEvidence | None:
    """Instrument the build copy, or say why the option inventory is unavailable."""
    if not request.inventory_loaded_options:
        return None
    try:
        return stage_loaded_options(layout.project, request.main, layout.work)
    except (OSError, PreparationError) as error:
        return LoadedOptionsEvidence(uncertainty=str(error) or "Instrumentation was unavailable")


def _unavailable_findings(error: PreparationError, request: BuildRequest) -> list[Finding]:
    """The findings for a build whose tools could not be verified or started."""
    findings = [
        Finding(
            "build.execution_unavailable",
            str(error),
            severity="error",
            status="inconclusive",
            details={"tool": error.tool} if isinstance(error, ToolUnavailableError) else {},
        )
    ]
    if request.inventory_loaded_options:
        findings.append(loaded_options_unavailable("Build execution was unavailable", request.main))
    return findings


def _read_log(request: BuildRequest, layout: BuildLayout, max_bytes: int) -> _LogEvidence:
    """Read the bounded final-pass log and the findings it holds."""
    text, complete = read_bounded(layout.output / f"{request.stem}.log", max_bytes)
    findings = log_findings(text, request.main, layout.project, layout.cwd)
    if not complete:
        findings.append(
            Finding(
                "build.log_unavailable",
                "A complete bounded final-pass log was not produced.",
                severity="error",
                status="inconclusive",
                path=request.main,
            )
        )
    return _LogEvidence(text, findings)


def _submission_findings(submission: SubmissionDependencies, main: str) -> list[Finding]:
    """A finding when the original inputs a submission needs could not be established."""
    if submission.inputs is not None:
        return []
    return [
        Finding(
            "build.submission_dependencies",
            "Build dependency evidence is incomplete for submission packaging.",
            severity="warning",
            status="inconclusive",
            path=main,
            details=submission.to_details(),
        )
    ]


def _loaded_options(
    request: BuildRequest,
    staged: LoadedOptionsProbe | LoadedOptionsEvidence,
    recorder: RecorderEvidence,
    built: bool,
    max_bytes: int,
) -> LoadedOptionsEvidence:
    if isinstance(staged, LoadedOptionsEvidence):
        return staged
    return collect_loaded_options(
        staged,
        list(recorder.loaded_packages),
        set(recorder.inputs),
        engine=request.engine,
        recorder_complete=recorder.complete,
        build_complete=built,
        max_bytes=max_bytes,
    )


def _failure_suggestion(failure: CommandFailure, limits: RuntimeLimits) -> str | None:
    """What a researcher can do about a build that a limit stopped; none for a plain failure."""
    match failure:
        case CommandFailure.TIMEOUT:
            return (
                "Check the end of the log for a loop or a very slow step. If the document "
                f"needs longer, raise timeout_seconds (now {limits.timeout_seconds}) in your "
                "configuration or pass --timeout-seconds."
            )
        case CommandFailure.OUTPUT_LIMIT:
            return (
                "Look for a repeating error or warning at the end of the log and fix its "
                "source. This output limit is not configurable."
            )
        case CommandFailure.RESOURCE_LIMIT:
            return (
                "Check for a runaway document. If it legitimately needs more, raise "
                "build_memory_mb (memory), max_bytes (size of generated files) or "
                "max_temporary_bytes (temporary disk) in your configuration."
            )
    return None


def _execution_findings(
    execution: CommandResult, main: str, log: str, limits: RuntimeLimits
) -> list[Finding]:
    """The one finding that explains why latexmk did not exit normally, if it did not.

    A limit that stopped the build also leaves the killed process with a signal
    status, so the limit is reported alone: that status adds nothing the researcher
    can act on.
    """
    failure = execution.failure
    if failure is None:
        return []
    details: dict[str, object] = {"stderr": execution.stderr[-_STDERR_TAIL_CHARS:]}
    if failure is CommandFailure.EXIT_STATUS:
        message = f"Build tool exited with status {execution.returncode}."
        # TeX reports the actual cause only in the log: latexmk's own
        # stderr names the failed rule, not the offending input.
        details["log_tail"] = log_tail(log)
    else:
        reason = execution.failure_reason("The build", limits) or ""
        message = (
            f"The build was stopped: {reason.rstrip('.')}."
            if failure is CommandFailure.RESOURCE_LIMIT
            else reason + "."
        )
    return [
        Finding(
            _FAILURE_RULES[failure],
            message,
            severity="error",
            path=main,
            suggestion=_failure_suggestion(failure, limits),
            details=details,
        )
    ]


def _is_complete_pdf(pdf: Path, max_bytes: int) -> bool:
    if not pdf.is_file() or not len(_PDF_HEADER) <= pdf.stat().st_size <= max_bytes:
        return False
    with pdf.open("rb") as stream:
        return stream.read(len(_PDF_HEADER)) == _PDF_HEADER
