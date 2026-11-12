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
from typing import NamedTuple

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
from .build_failure_help import explain_build_message
from .build_fonts import (
    HOST_FONTS_RULE,
    host_font_files,
    host_fonts_finding,
    luaotfload_font_files,
    tex_distribution_trees,
    xdv_font_files,
)
from .build_log import bibliography_log_findings, log_findings, log_tail, read_bounded
from .build_recorder import RecorderEvidence, read_recorder
from .build_result import BibliographyRecord, BuildResult, ExecutionPolicy
from .build_workspace import BuildLayout, prepare_layout
from .cancellation import run_in_thread
from .command_runner import CommandFailure, CommandResult, RuntimeLimits, ToolVersion
from .engine_selection import (
    DEFAULT_ENGINE,
    ENGINE_LABELS,
    EngineChoice,
    choose_engine,
    engine_finding,
    find_latexmk_configurations,
    latexmk_configuration_finding,
    read_engine_comment,
)
from .errors import PreparationError, ToolUnavailableError
from .findings import Finding
from .latexmk_command import BiberPlan, controlled_rules, latexmk_command
from .loaded_options import (
    BuildEvidence,
    LoadedOptionsEvidence,
    LoadedOptionsProbe,
    collect_loaded_options,
    loaded_options_unavailable,
    stage_loaded_options,
)
from .option_config import require_unique_strings, validate_scalars
from .path_safety import is_relative_posix_path
from .runtime import ToolRunner
from .setting_choices import BibliographyBackend, Engine, EngineSetting
from .tool_launch import tool_state_directory

# Only these policy-level log errors may be deselected. Tool failures, incomplete
# output/recorder evidence, TeX errors and unresolved reruns remain build failures.
SELECTABLE_BUILD_ERRORS = frozenset({"BLD001", "BLD002"})
_MAIN_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+")
_PDF_HEADER = b"%PDF-"
# The part of latexmk's standard error a build finding keeps in its details.
_STDERR_TAIL_CHARS = 4000
# The closing lines of latexmk's output that are searched for a restriction that explains a
# failed build, and the prefix of the lines that the PDF driver of XeLaTeX prints.
_MAX_EXPLAINED_OUTPUT_LINES = 400
_DRIVER_PREFIX = "xdvipdfmx:"
_FAILURE_RULES = {
    CommandFailure.TIMEOUT: "build.timeout",
    CommandFailure.OUTPUT_LIMIT: "build.output_limit",
    CommandFailure.RESOURCE_LIMIT: "build.resource_limit",
    CommandFailure.EXIT_STATUS: "build.failed",
}


@dataclass(frozen=True, slots=True)
class BuildRequest:
    """One isolated build of ``main`` from ``source`` in the new directory ``work``.

    Construction raises ``SettingsError`` when a field is invalid or
    ``ignored_log_checks`` repeats a code or names one outside ``SELECTABLE_BUILD_ERRORS``,
    and ``PreparationError`` when ``main`` is not a relative ``.tex`` path of letters,
    digits, underscores, dots or hyphens.

    Attributes:
        source: The project directory to copy.
        main: The main file, relative to ``source``.
        work: The workspace directory to create.
        engine: The TeX engine.
        bibliography_backend: The bibliography strategy.
        inventory_loaded_options: Whether to instrument the build for the loaded class
            and package option inventory.
        ignored_log_checks: The deselected policy-level log errors, from
            ``SELECTABLE_BUILD_ERRORS``, that do not fail the build.
    """

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
        """The main file's name without folder or suffix, which names the log and PDF."""
        return PurePosixPath(self.main).stem


@dataclass(frozen=True, slots=True)
class _BibliographyOutcome:
    """The bibliography record of a build, its findings and the backend versions verified.

    Attributes:
        record: The strategy, the control-file evidence and the Biber preparation note.
        findings: The bibliography policy finding and the backend verification findings.
        versions: The verified backend versions by tool.
    """

    record: BibliographyRecord
    findings: list[Finding]
    versions: dict[str, ToolVersion]


@dataclass(frozen=True, slots=True)
class _LogEvidence:
    """The bounded final-pass log and the findings read from it.

    Attributes:
        text: The log text, at most the output byte limit.
        findings: The findings read from the log, and one when it is incomplete.
    """

    text: str
    findings: list[Finding]


async def build_project(
    source: Path,
    main: str,
    work: Path,
    engine: EngineSetting,
    runner: ToolRunner,
    *,
    bibliography_backend: BibliographyBackend = "auto",
    inventory_loaded_options: bool = False,
    ignored_log_checks: tuple[str, ...] = (),
) -> BuildResult:
    """Build ``main`` in isolation, preparing Biber for an automatic strategy only when needed.

    The engine is the setting, else the main file's ``% !TEX program`` comment, else
    pdfLaTeX; the result says which, and notes a project latexmkrc that latexmk ignores.

    On macOS, preparing Biber copies and unpacks a large universal binary, which
    costs seconds per job. Unless the sources mention biblatex, an automatic build
    first runs with Biber blocked. Only if that build writes a fresh .bcf is it
    discarded and rebuilt in the same, emptied workspace with Biber prepared.

    Args:
        source: The project directory to copy.
        main: The main file, relative to ``source``.
        work: The workspace directory to create.
        engine: The engine setting; ``auto`` follows the main file's magic comment.
        runner: The restricted tool runner.
        bibliography_backend: The bibliography strategy.
        inventory_loaded_options: Whether to collect the loaded class and package option
            inventory.
        ignored_log_checks: The deselected policy-level log errors.

    Returns:
        The build result; tool and evidence failures are findings in it.

    Raises:
        PreparationError: The request is invalid, or the build copy cannot be prepared.
    """
    request = BuildRequest(
        source,
        main,
        work,
        DEFAULT_ENGINE,
        bibliography_backend=bibliography_backend,
        inventory_loaded_options=inventory_loaded_options,
        ignored_log_checks=ignored_log_checks,
    )
    # The request has validated the main file's name before it is read.
    choice = choose_engine(engine, await run_in_thread(read_engine_comment, source / main))
    request = replace(request, engine=choice.engine)
    result = await _build_with_biber_plan(request, runner)
    result.findings[0:0] = await run_in_thread(_engine_findings, choice, source, main)
    return result


async def _build_with_biber_plan(request: BuildRequest, runner: ToolRunner) -> BuildResult:
    """Run the build, first without Biber when the plan defers it.

    Args:
        request: The build to run.
        runner: The restricted tool runner.

    Returns:
        The build result.
    """
    plan = await plan_biber(runner, request.bibliography_backend, request.source)
    if not plan.deferred:
        return await _build_once(request, runner, plan)
    try:
        return await _build_once(request, runner, plan)
    except BiberRequired:
        await run_in_thread(shutil.rmtree, request.work)
    return await _build_once(
        request, runner, BiberPlan(deferred=False, note=BIBER_PREPARED_AFTER_DEFERRAL)
    )


def _engine_findings(choice: EngineChoice, source: Path, main: str) -> list[Finding]:
    """Say which engine built the document, and what a project latexmkrc would have changed.

    Args:
        choice: The engine and what chose it.
        source: The project folder, searched for latexmk configuration files.
        main: The main file.

    Returns:
        The engine finding, then one finding per latexmk configuration file.
    """
    return [
        engine_finding(choice, main),
        *(
            latexmk_configuration_finding(configuration, choice, main)
            for configuration in find_latexmk_configurations(source, main)
        ),
    ]


async def _build_once(request: BuildRequest, runner: ToolRunner, biber: BiberPlan) -> BuildResult:
    """Run one isolated build.

    Args:
        request: The build to run.
        runner: The restricted tool runner.
        biber: Whether Biber is deferred for this build.

    Returns:
        The build result, successful only with a complete PDF, no unignored error
        finding and, when requested, a complete option inventory.

    Raises:
        BiberRequired: A build that deferred Biber needs it.
        PreparationError: The build copy cannot be prepared.
    """
    layout = await run_in_thread(
        prepare_layout, request.source, request.main, request.work, runner.limits.max_copy_bytes
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
        result.tools.versions.update(await _probe_build_tools(request, runner, layout))
        await _prepare_engine(request, runner, layout)
        execution = await runner.run(result.command, cwd=layout.cwd, workspace=layout.work)
    except PreparationError as error:
        result.findings.extend(_unavailable_findings(error, request))
        return result
    result.command = execution.resolved_command
    result.tools.execution = replace(policy, duration_seconds=execution.duration_seconds)
    declared_roots = _declared_roots(execution, runner)
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
            tool_state=tool_state_directory(layout.work),
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
    result.findings.extend(
        await run_in_thread(
            _output_findings,
            request,
            layout,
            _OutputEvidence(recorder.inputs, declared_roots, result.log, complete_pdf),
        )
    )
    built = complete_pdf and not any(
        item.severity == "error" and item.code not in request.ignored_log_checks
        for item in result.findings
    )
    options = (
        _loaded_options(request, staged, recorder, built=built, max_bytes=max_output_bytes)
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


class _OutputEvidence(NamedTuple):
    """What a finished build left that its output findings read.

    Attributes:
        recorder_inputs: The files the recorder lists.
        declared_roots: The declared resource roots.
        log: The final-pass log.
        complete_pdf: Whether the build produced a complete PDF.
    """

    recorder_inputs: frozenset[Path]
    declared_roots: tuple[Path, ...]
    log: str
    complete_pdf: bool


def _output_findings(
    request: BuildRequest, layout: BuildLayout, evidence: _OutputEvidence
) -> list[Finding]:
    """Report a missing PDF, or the fonts a XeLaTeX or LuaLaTeX PDF took from this computer.

    Args:
        request: The build.
        layout: The build's workspace.
        evidence: The recorder inputs, declared roots, log and PDF state of the build.

    Returns:
        The missing-PDF error, the host-font finding, or nothing for a pdfLaTeX PDF.
    """
    if not evidence.complete_pdf:
        return [
            Finding(
                "build.missing_pdf",
                "No complete new PDF was produced.",
                severity="error",
                path=request.main,
                details={"log_tail": log_tail(evidence.log)},
            )
        ]
    if request.engine == "pdflatex":
        return []
    return [_host_fonts(request, layout, evidence.recorder_inputs, evidence.declared_roots)]


async def _probe_build_tools(
    request: BuildRequest, runner: ToolRunner, layout: BuildLayout
) -> dict[str, ToolVersion]:
    """Verify the versions of latexmk and the engine; the job remembers them.

    The probes run one after the other in the task that holds the build's lease: a command
    may not run in a task of its own under that lease.

    Args:
        request: The build, which names the engine.
        runner: The restricted tool runner.
        layout: The build's workspace.

    Returns:
        The version of each tool, by name.

    Raises:
        PreparationError: A tool is missing or gave no usable version.
    """
    return {
        "latexmk": await runner.tool_version(["latexmk", "-norc", "-v"], workspace=layout.work),
        request.engine: await runner.tool_version(
            [request.engine, "--version"], workspace=layout.work
        ),
    }


async def _prepare_engine(request: BuildRequest, runner: ToolRunner, layout: BuildLayout) -> None:
    """Keep a LuaLaTeX build's font caches and give it the job's sealed font database.

    The kept caches name the font files luaotfload loaded, which the host-font check reads
    after the build; the sealed database spares each build the font scan.

    Args:
        request: The build, which names the engine.
        runner: The restricted tool runner.
        layout: The build's workspace.
    """
    if request.engine == "lualatex":
        runner.keep_tex_cache(layout.work)
        await runner.prepare_font_cache(layout.work.parent)


def _declared_roots(execution: CommandResult, runner: ToolRunner) -> tuple[Path, ...]:
    """Return the directories the build could read from apart from the project.

    Args:
        execution: The result of the latexmk command, which records its declared roots.
        runner: The runner, whose roots stand in when the result records none, and whose
            sealed font cache LuaLaTeX reads.

    Returns:
        The declared resource roots, with the font cache when there is one.
    """
    roots = (
        tuple(Path(root) for root in execution.resource_roots)
        if execution.resource_roots
        else tuple(runner.resource_roots)
    )
    return roots if runner.font_cache is None else (*roots, runner.font_cache)


def _host_fonts(
    request: BuildRequest,
    layout: BuildLayout,
    recorder_inputs: frozenset[Path],
    declared_roots: tuple[Path, ...],
) -> Finding:
    """Report the fonts a XeLaTeX or LuaLaTeX build loaded from outside its trees.

    Args:
        request: The build, which names the engine.
        layout: The build's workspace, whose output holds the XDV file.
        recorder_inputs: The files the recorder lists, which name LuaLaTeX's font caches.
        declared_roots: The declared resource roots, among which the TeX distribution is.

    Returns:
        The host-font finding; an informational, inconclusive one when the XDV file could
        not be read.
    """
    label = ENGINE_LABELS[request.engine]
    try:
        files = (
            xdv_font_files(layout.output / f"{request.stem}.xdv")
            if request.engine == "xelatex"
            else luaotfload_font_files(recorder_inputs)
        )
    except PreparationError as error:
        return Finding(
            HOST_FONTS_RULE,
            f"The fonts {label} loaded could not be read: {error}.",
            severity="info",
            status="inconclusive",
            path=request.main,
        )
    distribution = tex_distribution_trees(declared_roots)
    hosted = host_font_files(files, distribution=distribution, project=layout.project)
    return host_fonts_finding(label, hosted, request.main)


async def _judge_bibliography(
    request: BuildRequest,
    runner: ToolRunner,
    layout: BuildLayout,
    biber: BiberPlan,
    biber_setup: BiberSetup,
) -> _BibliographyOutcome:
    """Compare the fresh control files with the strategy and verify the backends they need.

    Args:
        request: The build.
        runner: The restricted tool runner.
        layout: The build's workspace and output folders.
        biber: Whether Biber was deferred, and the preparation note.
        biber_setup: The prepared Biber, or why preparing it failed.

    Returns:
        The bibliography record, findings and verified backend versions.

    Raises:
        BiberRequired: A build that deferred Biber wrote a Biber control file.
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
    """Instrument the build copy, or say why the option inventory is unavailable.

    Args:
        request: The build, which says whether the inventory is wanted.
        layout: The build's workspace.

    Returns:
        The staged probe, evidence that records why instrumentation was unavailable, or
        ``None`` when no inventory is wanted.
    """
    if not request.inventory_loaded_options:
        return None
    try:
        return stage_loaded_options(layout.project, request.main, layout.work)
    except (OSError, PreparationError) as error:
        return LoadedOptionsEvidence(uncertainty=str(error) or "Instrumentation was unavailable")


def _unavailable_findings(error: PreparationError, request: BuildRequest) -> list[Finding]:
    """Report a build whose tools could not be verified or started.

    Args:
        error: Why the tools could not be verified or started.
        request: The build, which says whether an option inventory was wanted.

    Returns:
        An inconclusive ``build.execution_unavailable`` finding, and an unavailable
        option inventory finding when one was wanted.
    """
    details: dict[str, object] = {"engine": request.engine}
    if isinstance(error, ToolUnavailableError):
        details["tool"] = error.tool
    findings = [
        Finding(
            "build.execution_unavailable",
            str(error),
            severity="error",
            status="inconclusive",
            details=details,
        )
    ]
    if request.inventory_loaded_options:
        findings.append(loaded_options_unavailable("Build execution was unavailable", request.main))
    return findings


def _read_log(request: BuildRequest, layout: BuildLayout, max_bytes: int) -> _LogEvidence:
    """Read the bounded final-pass log and the findings it holds.

    Args:
        request: The build, whose main file names the log.
        layout: The build's folders.
        max_bytes: The most bytes of log read.

    Returns:
        The log text and its findings, with one more when the log is incomplete.
    """
    text, complete = read_bounded(layout.output / f"{request.stem}.log", max_bytes)
    findings = log_findings(text, request.main, layout.project, layout.cwd)
    # BibTeX and Biber write their own warnings to <main>.blg; a build without one ran neither.
    bibliography_log, _ = read_bounded(layout.output / f"{request.stem}.blg", max_bytes)
    findings.extend(bibliography_log_findings(bibliography_log, request.main))
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
    """Report when the original inputs a submission needs could not be established.

    Args:
        submission: The submission dependency evidence.
        main: The main file the finding names.

    Returns:
        One inconclusive warning, or an empty list when the inputs are established.
    """
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
    *,
    built: bool,
    max_bytes: int,
) -> LoadedOptionsEvidence:
    """Collect the loaded option inventory of a finished build.

    Args:
        request: The build.
        staged: The staged probe, or evidence of why instrumentation was unavailable,
            which is returned as it is.
        recorder: The build's recorder evidence.
        built: Whether the build completed without an unignored error.
        max_bytes: The most bytes of evidence read.

    Returns:
        The option inventory evidence.
    """
    if isinstance(staged, LoadedOptionsEvidence):
        return staged
    return collect_loaded_options(
        staged,
        BuildEvidence(
            engine=request.engine,
            packages=tuple(recorder.loaded_packages),
            recorder_inputs=frozenset(recorder.inputs),
            recorder_complete=recorder.complete,
            build_complete=built,
            max_bytes=max_bytes,
        ),
    )


def _failure_suggestion(failure: CommandFailure, limits: RuntimeLimits) -> str | None:
    """Suggest what a researcher can do about a build that a limit stopped.

    Args:
        failure: How the build failed.
        limits: The runtime limits, whose timeout the suggestion names.

    Returns:
        The suggestion, or ``None`` for a plain failure.
    """
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
                "build_memory_mb (memory), max_generated_file_bytes (size of one generated "
                "file) or max_temporary_bytes (temporary disk) in your configuration."
            )
    return None


def _execution_findings(
    execution: CommandResult, main: str, log: str, limits: RuntimeLimits
) -> list[Finding]:
    """Explain in one finding why latexmk did not exit normally, if it did not.

    A limit that stopped the build also leaves the killed process with a signal
    status, so the limit is reported alone: that status adds nothing the researcher
    can act on.

    Args:
        execution: The result of the latexmk command.
        main: The main file the finding names.
        log: The final-pass log, whose tail explains a failed exit status.
        limits: The runtime limits.

    Returns:
        One error finding, or an empty list when latexmk exited normally.
    """
    failure = execution.failure
    if failure is None:
        return []
    details: dict[str, object] = {"stderr": execution.stderr[-_STDERR_TAIL_CHARS:]}
    suggestion = _failure_suggestion(failure, limits)
    if failure is CommandFailure.EXIT_STATUS:
        message = f"Build tool exited with status {execution.returncode}."
        # TeX reports the actual cause only in the log: latexmk's own
        # stderr names the failed rule, not the offending input. A converter that the
        # sandbox blocks, such as XeLaTeX's Ghostscript for EPS figures, shows only there.
        details["log_tail"] = log_tail(log)
        suggestion = _converter_suggestion(execution.stdout + execution.stderr)
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
            suggestion=suggestion,
            details=details,
        )
    ]


def _converter_suggestion(output: str) -> str | None:
    """Explain a failed exit status from the PDF driver's output, when a restriction caused it.

    Only the driver's own lines count, such as xdvipdfmx failing to run Ghostscript on an EPS
    figure for XeLaTeX: TeX's errors are findings of the log, which carry their own next step.

    Args:
        output: What latexmk and the tools it ran printed.

    Returns:
        The next step for the first explained driver line, or ``None``.
    """
    for line in output.splitlines()[-_MAX_EXPLAINED_OUTPUT_LINES:]:
        if line.lstrip().startswith(_DRIVER_PREFIX) and (suggestion := explain_build_message(line)):
            return suggestion
    return None


def _is_complete_pdf(pdf: Path, max_bytes: int) -> bool:
    """Return whether ``pdf`` is a file of at most ``max_bytes`` that starts with a PDF header."""
    if not pdf.is_file() or not len(_PDF_HEADER) <= pdf.stat().st_size <= max_bytes:
        return False
    with pdf.open("rb") as stream:
        return stream.read(len(_PDF_HEADER)) == _PDF_HEADER
