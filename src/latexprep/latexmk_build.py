"""Isolated latexmk builds of one main file, with fresh and bounded build evidence.

Every build copies the project into a new workspace, runs latexmk with a static
trusted configuration under the runner's sandbox, and judges the result only
from evidence that build produced: its final log, recorder trace, rule database,
bibliography control files and PDF.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
from dataclasses import KW_ONLY, dataclass, field
from pathlib import Path, PurePosixPath
from typing import NamedTuple

from .build_dependencies import (
    BibliographyEvidence,
    LoadedPackage,
    RecorderTrace,
    collect_submission_dependencies,
    read_bibliography_evidence,
    read_loaded_package,
    read_recorder_trace,
)
from .build_log import log_findings, log_tail, read_bounded
from .file_safety import MIB
from .loaded_options import (
    LoadedOptionsEvidence,
    LoadedOptionsProbe,
    collect_loaded_options,
    loaded_options_unavailable,
    stage_loaded_options,
)
from .models import (
    BIBLIOGRAPHY_BACKENDS,
    ENGINES,
    BibliographyBackend,
    Engine,
    Finding,
    PreparationError,
)
from .runtime import CommandResult, RuntimeLimits, ToolRunner, ToolVersion

# Only these policy-level log errors may be deselected. Tool failures, incomplete
# output/recorder evidence, TeX errors and unresolved reruns remain build failures.
SELECTABLE_BUILD_ERRORS = frozenset({"BLD001", "BLD002"})
_ENGINE_MODES: dict[Engine, str] = {
    "pdflatex": "-pdf",
    "xelatex": "-xelatex",
    "lualatex": "-lualatex",
}
_MAIN_COMPONENT = re.compile(r"[A-Za-z0-9_.-]+")
_MAX_IMPORTED_ENTRIES = 20_000
_MAX_LOADED_PACKAGES = 2048
_PDF_HEADER = b"%PDF-"
_BIBER_HINT_SUFFIXES = frozenset(
    {".tex", ".ltx", ".latex", ".sty", ".cls", ".cfg", ".bbx", ".cbx", ".dbx"}
)
_MAX_BIBER_HINT_BYTES = 64 * MIB
_BIBER_NOT_PREPARED = "not prepared: no source mentions biblatex and the build wrote no .bcf"
_BIBER_PREPARED_AFTER_DEFERRAL = (
    "prepared after a build without Biber wrote a .bcf; that build was discarded"
)


@dataclass(frozen=True, slots=True)
class ExecutionPolicy:
    """The isolation and limits a build ran under."""

    backend: str
    limits: RuntimeLimits
    source_date_epoch: str

    def to_report(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "limits": self.limits.to_report(),
            "network": "denied",
            "shell_escape": "disabled",
            "imported_configuration": "disabled",
            "source_date_epoch": self.source_date_epoch,
        }


@dataclass(frozen=True, slots=True)
class BibliographyRecord:
    """The requested backend, the fresh control-file evidence and how Biber was prepared."""

    requested_backend: BibliographyBackend
    evidence: BibliographyEvidence
    biber_preparation: str | None = None

    def to_report(self) -> dict[str, object]:
        report: dict[str, object] = {
            "requested_backend": self.requested_backend,
            **self.evidence.to_details(),
        }
        if self.biber_preparation is not None:
            report["biber_preparation"] = self.biber_preparation
        return report


@dataclass(slots=True)
class BuildTools:
    """What one build records about its toolchain; ``to_report`` gives the report entries."""

    execution: ExecutionPolicy | None = None
    versions: dict[str, ToolVersion] = field(default_factory=dict)
    resource_roots: tuple[str, ...] | None = None
    submission_dependencies: dict[str, object] | None = None
    bibliography: BibliographyRecord | None = None
    loaded_options: LoadedOptionsEvidence | None = None

    def to_report(self) -> dict[str, object]:
        report: dict[str, object] = {}
        if self.execution is not None:
            report["execution"] = self.execution.to_report()
        report.update({name: version.to_report() for name, version in self.versions.items()})
        if self.resource_roots is not None:
            report["resource_roots"] = list(self.resource_roots)
        if self.submission_dependencies is not None:
            report["submission_dependencies"] = self.submission_dependencies
        if self.bibliography is not None:
            report["bibliography"] = self.bibliography.to_report()
        if self.loaded_options is not None:
            report["loaded_options"] = self.loaded_options.details()
        return report


@dataclass(slots=True)
class BuildResult:
    success: bool
    _: KW_ONLY
    pdf: Path | None = None
    findings: list[Finding] = field(default_factory=list)
    # Project files the TeX recorder shows the build read.
    recorder_project_inputs: set[str] = field(default_factory=set)
    tools: BuildTools = field(default_factory=BuildTools)
    command: list[str] = field(default_factory=list)
    log: str = ""
    loaded_packages: list[LoadedPackage] = field(default_factory=list)
    recorder_complete: bool = False
    submission_inputs: set[str] | None = None
    # Generated files read by the build (output- or project-relative), e.g. main.bbl.
    generated_reads: frozenset[str] = frozenset()


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
        if self.engine not in ENGINES:
            raise PreparationError(f"Unsupported TeX engine: {self.engine}")
        if self.bibliography_backend not in BIBLIOGRAPHY_BACKENDS:
            raise PreparationError("Bibliography backend must be auto, bibtex or biber")
        if type(self.inventory_loaded_options) is not bool:
            raise PreparationError("inventory_loaded_options must be a boolean")
        if not isinstance(self.ignored_log_checks, tuple) or any(
            not isinstance(code, str) or code not in SELECTABLE_BUILD_ERRORS
            for code in self.ignored_log_checks
        ):
            raise PreparationError("Only undefined-citation/reference log checks may be ignored")
        relative = PurePosixPath(self.main)
        if (
            relative.is_absolute()
            or relative.suffix != ".tex"
            or ".." in relative.parts
            or not relative.parts
            or any(not _MAIN_COMPONENT.fullmatch(part) for part in relative.parts)
        ):
            raise PreparationError(
                "Build main must be a relative .tex path with letters, digits, underscores, "
                "dots or hyphens; other filename forms are not yet supported."
            )

    @property
    def stem(self) -> str:
        return PurePosixPath(self.main).stem


@dataclass(frozen=True, slots=True)
class _BuildTree:
    """The resolved source and the new workspace of one build."""

    source: Path
    work: Path
    project: Path
    output: Path
    cwd: Path


@dataclass(frozen=True, slots=True)
class _BiberPlan:
    """Whether this build defers Biber, and the preparation note it reports."""

    deferred: bool
    note: str | None


class _BiberSetup(NamedTuple):
    executable: str
    error: str | None


@dataclass(frozen=True, slots=True)
class _RecorderEvidence:
    """What the recorder shows this build read, interpreted for build checks."""

    trace: RecorderTrace | None
    inputs: frozenset[Path]
    project_inputs: frozenset[str]
    loaded_packages: tuple[LoadedPackage, ...]
    complete: bool
    findings: tuple[Finding, ...]


class _BiberRequired(Exception):
    """A build that deferred Biber wrote a fresh .bcf, so Biber is needed after all."""


def _sources_may_use_biber(source: Path, max_bytes: int = _MAX_BIBER_HINT_BYTES) -> bool:
    """Whether any TeX source mentions biblatex; unreadable or oversized input answers yes.

    A literal hint, so mentions in comments count. A miss is safe because a
    deferred build that writes a fresh .bcf is rebuilt with Biber prepared.
    """
    total = 0
    try:
        for path in source.rglob("*"):
            if (
                path.suffix.lower() not in _BIBER_HINT_SUFFIXES
                or path.is_symlink()
                or not path.is_file()
            ):
                continue
            total += path.stat().st_size
            if total > max_bytes or b"biblatex" in path.read_bytes():
                return True
    except OSError:
        return True
    return False


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

    On macOS, preparing Biber copies and unpacks an 84 MB binary, which costs
    seconds per job. Unless the sources mention biblatex, an automatic build
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
    defer_biber = (
        bibliography_backend == "auto"
        and runner.biber_needs_preparation
        and not _sources_may_use_biber(source)
    )
    if not defer_biber:
        return await _build_once(request, runner, _BiberPlan(deferred=False, note=None))
    try:
        return await _build_once(
            request, runner, _BiberPlan(deferred=True, note=_BIBER_NOT_PREPARED)
        )
    except _BiberRequired:
        shutil.rmtree(work)
    return await _build_once(
        request, runner, _BiberPlan(deferred=False, note=_BIBER_PREPARED_AFTER_DEFERRAL)
    )


async def _build_once(request: BuildRequest, runner: ToolRunner, biber: _BiberPlan) -> BuildResult:
    """Run one isolated build; raise ``_BiberRequired`` if a deferred build needs Biber."""
    tree = _prepare_tree(request, runner.limits.max_file_bytes)
    staged = _stage_loaded_options(request, tree)
    biber_setup = await _prepare_biber(request, runner, biber, tree.work.parent)
    result = BuildResult(
        success=False, command=_latexmk_command(request, tree, biber, biber_setup.executable)
    )
    result.tools.execution = ExecutionPolicy(
        runner.sandbox.description, runner.limits, runner.source_date_epoch
    )
    try:
        execution = await _run_latexmk(request, runner, tree, result)
    except PreparationError as error:
        result.findings.append(
            Finding(
                "build.execution_unavailable", str(error), severity="error", status="inconclusive"
            )
        )
        if request.inventory_loaded_options:
            result.findings.append(
                loaded_options_unavailable("Build execution was unavailable", request.main)
            )
        return result
    declared_roots = (
        tuple(Path(root) for root in execution.resource_roots)
        if execution.resource_roots
        else tuple(runner.resource_roots)
    )
    result.tools.resource_roots = tuple(sorted(str(root) for root in declared_roots))
    max_output_bytes = runner.limits.max_output_bytes
    result.findings.extend(_log_evidence(request, tree, max_output_bytes, result))
    result.findings.extend(_execution_findings(execution, request.main, result.log))
    recorder = _read_recorder(request, tree, declared_roots, max_output_bytes)
    result.findings.extend(recorder.findings)
    result.recorder_complete = recorder.complete
    result.recorder_project_inputs = set(recorder.project_inputs)
    result.loaded_packages = list(recorder.loaded_packages)
    owned_inputs = (staged.hook,) if isinstance(staged, LoadedOptionsProbe) else ()
    result.findings.extend(
        _submission_evidence(
            request, tree, recorder, declared_roots, owned_inputs, max_output_bytes, result
        )
    )
    evidence = read_bibliography_evidence(tree.output, max_output_bytes)
    if biber.deferred and "biber" in evidence.observed_backends:
        raise _BiberRequired
    result.tools.bibliography = BibliographyRecord(
        request.bibliography_backend, evidence, biber.note
    )
    result.findings.append(_bibliography_policy(evidence, request))
    result.findings.extend(
        await _bibliography_tools(
            request, runner, tree.work, evidence, biber_setup.error, result.tools.versions
        )
    )
    pdf = tree.output / f"{request.stem}.pdf"
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


def _stage_loaded_options(
    request: BuildRequest, tree: _BuildTree
) -> LoadedOptionsProbe | LoadedOptionsEvidence | None:
    """Instrument the build copy, or say why the option inventory is unavailable."""
    if not request.inventory_loaded_options:
        return None
    try:
        return stage_loaded_options(tree.project, request.main, tree.work)
    except (OSError, PreparationError) as error:
        return LoadedOptionsEvidence(uncertainty=str(error) or "Instrumentation was unavailable")


async def _run_latexmk(
    request: BuildRequest, runner: ToolRunner, tree: _BuildTree, result: BuildResult
) -> CommandResult:
    """Verify latexmk and the engine under isolation, then run the build itself."""
    result.tools.versions["latexmk"] = await runner.tool_version(
        ["latexmk", "-norc", "-v"], workspace=tree.work
    )
    result.tools.versions[request.engine] = await runner.tool_version(
        [request.engine, "--version"], workspace=tree.work
    )
    execution = await runner.run(result.command, cwd=tree.cwd, workspace=tree.work)
    result.command = execution.resolved_command
    return execution


def _log_evidence(
    request: BuildRequest, tree: _BuildTree, max_bytes: int, result: BuildResult
) -> list[Finding]:
    """Read the bounded final-pass log into ``result`` and return its findings."""
    log_path = tree.output / f"{request.stem}.log"
    result.log = read_bounded(log_path, max_bytes)
    findings = log_findings(result.log, request.main, tree.project, tree.cwd)
    if not log_path.is_file() or log_path.stat().st_size > max_bytes:
        findings.append(
            Finding(
                "build.log_unavailable",
                "A complete bounded final-pass log was not produced.",
                severity="error",
                status="inconclusive",
                path=request.main,
            )
        )
    return findings


def _submission_evidence(
    request: BuildRequest,
    tree: _BuildTree,
    recorder: _RecorderEvidence,
    declared_roots: tuple[Path, ...],
    owned_inputs: tuple[Path, ...],
    max_bytes: int,
    result: BuildResult,
) -> list[Finding]:
    """Record the original inputs a submission needs, or why they are uncertain."""
    submission = collect_submission_dependencies(
        source=tree.source,
        project=tree.project,
        cwd=tree.cwd,
        output=tree.output,
        main=request.main,
        engine=request.engine,
        recorder=recorder.trace,
        recorder_complete=recorder.complete,
        resource_roots=declared_roots,
        owned_inputs=owned_inputs,
        max_bytes=max_bytes,
    )
    result.submission_inputs = submission.inputs
    result.generated_reads = submission.generated_reads
    result.tools.submission_dependencies = submission.details
    if submission.inputs is not None:
        return []
    return [
        Finding(
            "build.submission_dependencies",
            "Build dependency evidence is incomplete for submission packaging.",
            severity="warning",
            status="inconclusive",
            path=request.main,
            details=submission.details,
        )
    ]


def _loaded_options(
    request: BuildRequest,
    staged: LoadedOptionsProbe | LoadedOptionsEvidence,
    recorder: _RecorderEvidence,
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


def _prepare_tree(request: BuildRequest, max_total_bytes: int) -> _BuildTree:
    source = request.source.resolve(strict=True)
    if not (source / request.main).is_file():
        raise PreparationError(f"Build main file does not exist: {request.main}")
    work = request.work.absolute()
    if work.exists() or work.resolve().is_relative_to(source):
        raise PreparationError("Each build requires a new workspace outside its source tree")
    work.mkdir(parents=True)
    work = work.resolve()
    project, output = work / "project", work / "output"
    _copy_source(source, project, max_total_bytes=max_total_bytes)
    output.mkdir()
    return _BuildTree(source, work, project, output, project / PurePosixPath(request.main).parent)


def _copy_source(source: Path, destination: Path, max_total_bytes: int) -> None:
    """Import a bounded project snapshot into a disposable writable build copy.

    ``max_total_bytes`` is the per-job import budget: it caps the combined size
    of every imported file rather than the size of any single one. Callers pass
    ``RuntimeLimits.max_file_bytes``, which therefore acts as the whole-tree
    limit here, not as a per-file limit.
    """
    total = count = 0
    for item in source.rglob("*"):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode) or not (item.is_file() or item.is_dir()):
            raise PreparationError(f"Build inputs must be regular files/directories: {item.name}")
        count += 1
        total += info.st_size if item.is_file() else 0
        if count > _MAX_IMPORTED_ENTRIES or total > max_total_bytes:
            raise PreparationError("Build input exceeds file-count or total-size limits")
    shutil.copytree(source, destination)
    # Imported snapshots may be read-only; writable disposable build copies are distinct.
    destination.chmod(0o700)
    for item in destination.rglob("*"):
        item.chmod(0o700 if item.is_dir() else 0o600)


async def _prepare_biber(
    request: BuildRequest, runner: ToolRunner, biber: _BiberPlan, root: Path
) -> _BiberSetup:
    if request.bibliography_backend == "bibtex" or biber.deferred:
        return _BiberSetup("biber", None)
    # Prepared once per runner and reused by every later build of the job.
    # A failure is reported through the bibliography-tool check below rather
    # than raised, because an auto build may never need Biber at all.
    try:
        prepared = await runner.prepare_biber(root)
    except PreparationError as error:
        return _BiberSetup("biber", str(error))
    return _BiberSetup("biber" if prepared is None else str(prepared), None)


def _latexmk_command(
    request: BuildRequest, tree: _BuildTree, biber: _BiberPlan, biber_executable: str
) -> list[str]:
    return [
        "latexmk",
        "-norc",
        "-e",
        _controlled_rules(request.bibliography_backend, biber, biber_executable),
        "-use-make-",
        "-view=none",
        "-interaction=nonstopmode",
        "-halt-on-error",
        "-file-line-error",
        "-no-shell-escape",
        "-recorder",
        _ENGINE_MODES[request.engine],
        f"-outdir={os.path.relpath(tree.output, tree.cwd)}",
        "./" + PurePosixPath(request.main).name,
    ]


def _controlled_rules(
    backend: BibliographyBackend, biber: _BiberPlan, biber_executable: str
) -> str:
    # Static trusted configuration overrides no imported rc, rules, or environment.
    rules = "$max_repeat=5; $bibtex_use=1; $use_make_for_missing_files=0; @cus_dep_list=();"
    if backend != "auto":
        # Latexmk's documented internal-command adapter returns failure without
        # launching the unselected backend. All code is application-owned; no
        # filename or user-provided string is interpolated into Perl or a shell.
        disabled = "$biber" if backend == "bibtex" else "$bibtex"
        rules += (
            " sub latexprep_backend_blocked { "
            "warn 'Bibliography backend is disabled by the selected strategy'; return 1; } "
            f"{disabled}='internal latexprep_backend_blocked';"
        )
    if biber.deferred:
        # Fails like a blocked backend; the fresh .bcf left behind makes
        # build_project discard this build and rebuild with Biber prepared.
        rules += (
            " sub latexprep_biber_deferred { "
            "warn 'Biber is deferred until a build shows that it is needed'; return 1; } "
            "$biber='internal latexprep_biber_deferred';"
        )
    elif backend != "bibtex":
        # Only an application-generated path reaches this Perl literal, and
        # prepare_biber rejects any path that is not inert in Perl and the shell.
        rules += f" $biber='{biber_executable} --noconf %O %B';"
    return rules


def _execution_findings(execution: CommandResult, main: str, log: str) -> list[Finding]:
    findings = []
    for failed, rule, message in (
        (execution.timed_out, "build.timeout", "The build exceeded its time limit."),
        (
            execution.output_limited,
            "build.output_limit",
            "The build exceeded its diagnostic output limit.",
        ),
        (
            bool(execution.resource_exceeded),
            "build.resource_limit",
            execution.resource_exceeded or "",
        ),
        (
            bool(execution.returncode),
            "build.failed",
            f"Build tool exited with status {execution.returncode}.",
        ),
    ):
        if failed:
            details: dict[str, object] = {"stderr": execution.stderr[-4000:]}
            if rule == "build.failed":
                # TeX reports the actual cause only in the log: latexmk's own
                # stderr names the failed rule, not the offending input.
                details["log_tail"] = log_tail(log)
            findings.append(Finding(rule, message, severity="error", path=main, details=details))
    return findings


def _read_recorder(
    request: BuildRequest, tree: _BuildTree, declared_roots: tuple[Path, ...], max_bytes: int
) -> _RecorderEvidence:
    try:
        trace = read_recorder_trace(
            tree.output / f"{request.stem}.fls", tree.output, tree.cwd, max_bytes
        )
    except (OSError, PreparationError):
        missing = Finding(
            "build.dependency_trace",
            "A complete, bounded recorder trace was not produced.",
            severity="error",
            status="inconclusive",
            path=request.main,
        )
        return _RecorderEvidence(None, frozenset(), frozenset(), (), False, (missing,))
    inputs: set[Path] = set()
    project_inputs: set[str] = set()
    packages: dict[Path, LoadedPackage] = {}
    findings: list[Finding] = []
    too_many_packages = False
    for recorded in trace.inputs:
        path = recorded.absolute.resolve()
        inputs.add(path)
        in_project = path.is_relative_to(tree.project)
        declared = (
            in_project
            or path.is_relative_to(tree.work)
            or any(path.is_relative_to(root) for root in declared_roots)
        )
        if declared and path.suffix.lower() in {".cls", ".sty"} and path not in packages:
            if len(packages) >= _MAX_LOADED_PACKAGES:
                too_many_packages = True
            else:
                packages[path] = read_loaded_package(path, tree.project)
        if in_project:
            project_inputs.add(path.relative_to(tree.project).as_posix())
        elif not declared:
            findings.append(
                Finding(
                    "build.external_dependency",
                    f"Recorder contains an undeclared external input: {Path(recorded.recorded)}",
                    severity="error",
                    path=request.main,
                )
            )
    read_main = (tree.project / request.main).resolve() in inputs
    if not read_main:
        findings.append(
            Finding(
                "build.dependency_trace",
                "Recorder trace does not establish that the selected main file was read.",
                severity="error",
                status="inconclusive",
                path=request.main,
            )
        )
    return _RecorderEvidence(
        trace,
        frozenset(inputs),
        frozenset(project_inputs),
        tuple(packages.values()),
        read_main and not too_many_packages,
        tuple(findings),
    )


def _bibliography_policy(evidence: BibliographyEvidence, request: BuildRequest) -> Finding:
    requested = request.bibliography_backend
    mismatch = requested != "auto" and any(
        backend != requested for backend in evidence.observed_backends
    )
    uncertain = bool(evidence.uncertainty)
    if mismatch:
        status = "failed"
    elif uncertain:
        status = "inconclusive"
    else:
        status = "passed" if evidence.observed_backends else "not_applicable"
    return Finding(
        "build.bibliography_backend",
        (
            "Fresh bibliography controls require a backend outside the selected strategy."
            if mismatch
            else "Checked the bibliography backend against fresh build control files."
        ),
        severity="error" if mismatch or uncertain else "info",
        status=status,
        path=request.main,
        details={"requested_backend": requested, **evidence.to_details()},
    )


async def _bibliography_tools(
    request: BuildRequest,
    runner: ToolRunner,
    work: Path,
    evidence: BibliographyEvidence,
    biber_error: str | None,
    versions: dict[str, ToolVersion],
) -> list[Finding]:
    """Verify each required backend under isolation and record its version."""
    findings = []
    for name in evidence.observed_backends:
        if request.bibliography_backend not in {"auto", name}:
            continue
        version_command = (
            ["biber", "--noconf", "--version"] if name == "biber" else ["bibtex", "--version"]
        )
        try:
            if name == "biber" and biber_error is not None:
                raise PreparationError(
                    f"Cannot run biber under restricted execution: {biber_error}"
                )
            version = await runner.tool_version(version_command, workspace=work)
        except PreparationError as error:
            findings.append(
                Finding(
                    "build.bibliography_tool",
                    str(error),
                    severity="error",
                    status="inconclusive",
                    path=request.main,
                    details={"backend": name},
                )
            )
            continue
        versions[name] = version
        findings.append(
            Finding(
                "build.bibliography_tool",
                f"Verified the locally available {name} version in isolation.",
                severity="info",
                status="passed",
                path=request.main,
                details={"backend": name, "version": version.to_report()},
            )
        )
    return findings


def _is_complete_pdf(pdf: Path, max_bytes: int) -> bool:
    if not pdf.is_file() or not len(_PDF_HEADER) <= pdf.stat().st_size <= max_bytes:
        return False
    with pdf.open("rb") as stream:
        return stream.read(len(_PDF_HEADER)) == _PDF_HEADER
