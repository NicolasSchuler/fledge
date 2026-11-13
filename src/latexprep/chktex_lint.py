r"""The optional chktex lint: chktex's warnings about a document's sources, as suggestions.

chktex, the LaTeX lint tool of TeX Live, runs in the sandbox of the TeX tools, never
directly. It may read the frozen copies of the TeX files that the selected main file uses
(the files of the literal source graph with a manuscript suffix), its own configuration
in the TeX distribution, and a project ``.chktexrc`` beside the main file once
``chktex_configuration`` has accepted it. It may write only into its own working folder,
which holds nothing but that configuration and its tool state. The tool environment has
no ``HOME``, so chktex never reads a user's configuration.

Each file is checked by a run of its own with ``-I0``. chktex then follows no ``\input``
itself, and the bracket counts it reports at the end of a file belong to that file; a run
over several files counts brackets across them and names the wrong file. A run that a
time, output or resource limit stops, or that the sandbox refuses, ends the lint. Every
warning becomes a ``suggestion`` finding of CHK001 at its file and line, up to
``MAX_LISTED_WARNINGS``, and one more finding counts the rest. A missing chktex, a refused
configuration and a run that did not complete are reported under the same rule, so the
lint never changes the outcome or the exit code.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath

from .analysed_source import AnalysedSource, reuse_or_analyse
from .cancellation import run_in_thread
from .chktex_configuration import (
    CONFIGURATION_NAME,
    ProjectConfiguration,
    read_project_configuration,
)
from .chktex_output import ChktexWarning, new_run_token, output_format, read_output
from .chktex_rules import CHKTEX_WARNING
from .command_runner import CommandResult, CommandRunner
from .errors import PreparationError, ToolUnavailableError
from .findings import Finding, Status
from .tex_commands import MANUSCRIPT_SUFFIXES
from .text_safety import escape_single_line_text
from .toolchain import install_hint

logger = logging.getLogger(__name__)

CHKTEX = "chktex"
# The memory one chktex run may use; chktex reads a file line by line.
CHKTEX_MEMORY_MB = 128
# The most files one lint checks, each in a run of its own; a thesis has a few dozen.
MAX_CHECKED_FILES = 256
# The most warnings a report lists; the rest are counted.
MAX_LISTED_WARNINGS = 200
# chktex exits with 0 (no warning, or only messages), 2 (warnings) or 3 (errors) after a check.
_CHECKED_STATUSES = frozenset({0, 2, 3})
# The files and the characters of a reason that a finding about incomplete runs names.
_MAX_NAMED_FILES = 10
_MAX_REASON_CHARACTERS = 300


@dataclass(slots=True)
class _LintProgress:
    """What the runs of one lint found so far.

    Attributes:
        token: The token that marks the warning lines of this lint's runs.
        files: The files the lint sets out to check, in the order it checks them.
        warnings: The kept warnings, each with its project-relative file.
        total: How many warnings chktex printed, including those not kept.
        unreadable_lines: How many lines of output were not warnings.
        problems: Each file whose run did not complete, with the reason.
        unchecked: The files the lint did not check at all.
    """

    token: str
    files: list[str]
    warnings: list[tuple[str, ChktexWarning]] = field(default_factory=list)
    total: int = 0
    unreadable_lines: int = 0
    problems: list[tuple[str, str]] = field(default_factory=list)
    unchecked: list[str] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class _LintInputs:
    """What one lint reads from the project before chktex runs.

    Attributes:
        files: The TeX files to check, the main file first, then by name.
        configuration: The ``.chktexrc`` beside the main file, as Fledge judged it.
    """

    files: list[str]
    configuration: ProjectConfiguration


def _chktex_finding(
    message: str,
    status: Status,
    *,
    path: str | None = None,
    line: int | None = None,
    suggestion: str | None = None,
    details: dict[str, object] | None = None,
    bounded: bool = False,
) -> Finding:
    """Build a finding of the chktex rule, which is always a suggestion.

    A lint that did not run or did not complete, and a count of warnings not listed, mark
    their finding incomplete.

    Args:
        message: What the finding says.
        status: ``failed`` for a warning, ``passed`` for a clean lint, ``skipped`` for a
            lint that did not run, ``inconclusive`` for one that did not complete.
        path: The project-relative file the finding concerns, if any.
        line: The line of ``path``, if any.
        suggestion: What to do next.
        details: Further evidence.
        bounded: Whether the finding stands for warnings the report does not list.

    Returns:
        The finding.
    """
    return Finding(
        CHKTEX_WARNING.name,
        message,
        severity="suggestion",
        status=status,
        path=path,
        line=line,
        incomplete=bounded or status in {"skipped", "inconclusive"},
        suggestion=suggestion,
        details=details or {},
    )


def _plural(count: int, noun: str) -> str:
    """Write ``count`` with ``noun``, adding an ``s`` unless the count is one."""
    return f"{count} {noun}{'' if count == 1 else 's'}"


def _source_files(analysis: AnalysedSource) -> list[str]:
    """Return the TeX files of the main file's graph that chktex checks, the main file first.

    Returns:
        The visited files with a manuscript suffix: the main file, then the others by
        name; nothing when no main file could be selected.
    """
    graph = analysis.graph
    if graph.main is None:
        return []
    names = sorted(
        name for name in graph.visited if PurePosixPath(name).suffix.lower() in MANUSCRIPT_SUFFIXES
    )
    main = [graph.main] if graph.main in names else []
    return main + [name for name in names if name != graph.main]


def _lint_inputs(analysis: AnalysedSource, root: Path, main: str) -> _LintInputs:
    """Read the files to check and the project's ``.chktexrc``; runs in a worker thread.

    Returns:
        The files and the judged configuration.
    """
    return _LintInputs(_source_files(analysis), read_project_configuration(root, main))


def _refused_configuration(configuration: ProjectConfiguration) -> Finding:
    """Report a ``.chktexrc`` that chktex may not read, at the line that holds the problem."""
    refusal = configuration.refusal
    assert refusal is not None
    return _chktex_finding(
        f"chktex was not run: {configuration.path} {refusal.reason}.",
        "skipped",
        path=configuration.path,
        line=refusal.line,
        suggestion=refusal.remedy,
    )


def _not_installed(error: ToolUnavailableError) -> Finding:
    """Report that chktex is not installed, with how to install it."""
    hint = install_hint(error.tool)
    install = f"Install chktex ({hint})" if hint else "Install chktex"
    return _chktex_finding(
        "chktex is not installed, so its warnings were not checked.",
        "skipped",
        suggestion=f"{install}, or leave {CHKTEX_WARNING.code} out of the selected checks.",
        details={"tool": error.tool},
    )


def _not_run(reason: str, suggestion: str) -> Finding:
    """Report a lint that could not start, with why and what to do."""
    return _chktex_finding(f"chktex was not run: {reason}.", "inconclusive", suggestion=suggestion)


def _warning_finding(path: str, warning: ChktexWarning) -> Finding:
    """Turn one chktex warning into a suggestion at its file and line."""
    number = warning.number
    return _chktex_finding(
        f"{warning.message} (chktex {warning.kind.lower()} {number})",
        "failed",
        path=path,
        line=warning.line,
        suggestion=(
            f"Follow chktex's advice. To keep the construct, end line {warning.line} with "
            f"% chktex {number}, or put % chktex-file {number} in the file."
        ),
        details={
            "chktex_number": number,
            "chktex_kind": warning.kind,
            "chktex_message": warning.message,
            "column": warning.column,
        },
    )


def _failure_reason(result: CommandResult, copy: Path, name: str, runner: CommandRunner) -> str:
    """Explain why a run did not complete, naming the project file instead of its copy.

    Args:
        result: How the run ended.
        copy: The frozen copy chktex read.
        name: The project-relative file the copy stands for.
        runner: The runner, whose limits a stopped run exceeded.

    Returns:
        The reason: the limit that stopped chktex, or the first line it printed on its
        standard error, or its exit status.
    """
    if not result.within_limits:
        return result.failure_reason(CHKTEX, runner.limits) or "chktex was stopped"
    first = next((line.strip() for line in result.stderr.splitlines() if line.strip()), "")
    if not first:
        return f"chktex exited with status {result.returncode}"
    shown = first.replace(str(copy), name)
    if len(shown) > _MAX_REASON_CHARACTERS:
        shown = shown[:_MAX_REASON_CHARACTERS] + "…"
    return escape_single_line_text(shown)


def _record_run(
    progress: _LintProgress, name: str, copy: Path, result: CommandResult, runner: CommandRunner
) -> bool:
    """Keep the warnings of one run and note whether it completed.

    A run counts as complete when it ended within its limits, with a status chktex gives
    after a check, and printed nothing on its standard error.

    Args:
        progress: What the lint found so far.
        name: The project-relative file the run checked.
        copy: The frozen copy chktex read.
        result: How the run ended.
        runner: The runner, whose limits apply.

    Returns:
        Whether a limit stopped the run, which ends the lint.
    """
    output = read_output(
        result.stdout, progress.token, keep=MAX_LISTED_WARNINGS - len(progress.warnings)
    )
    kept = sorted(output.warnings, key=lambda item: (item.line, item.column, item.number))
    progress.warnings.extend((name, warning) for warning in kept)
    progress.total += output.total
    progress.unreadable_lines += output.unreadable_lines
    completed = result.within_limits and result.returncode in _CHECKED_STATUSES
    if not completed or result.stderr.strip():
        progress.problems.append((name, _failure_reason(result, copy, name, runner)))
    return not result.within_limits


def _freeze_sources(
    runner: CommandRunner, root: Path, names: list[str], storage: Path
) -> list[tuple[str, Path | str]]:
    """Freeze a read-only copy of each file for chktex; runs in a worker thread.

    Args:
        runner: The runner that keeps the copies and grants only them.
        root: The project root.
        names: The project-relative files.
        storage: The new folder for the copies.

    Returns:
        Each file with its copy, or with why it could not be copied.
    """
    frozen: list[tuple[str, Path | str]] = []
    for name in names:
        try:
            frozen.append((name, runner.freeze_input(root / name, storage)))
        except PreparationError as error:
            frozen.append((name, f"the file could not be copied for chktex ({error})"))
        except OSError as error:
            cause = error.strerror or type(error).__name__
            frozen.append((name, f"the file could not be copied for chktex ({cause})"))
    return frozen


def _prepare_folders(work: Path, configuration: ProjectConfiguration) -> tuple[Path, Path]:
    """Create the version probe's folder and chktex's working folder; runs in a worker thread.

    The probe's folder stays empty, so the probe reads no project configuration. The
    working folder receives the accepted ``.chktexrc``: chktex reads a ``.chktexrc`` in its
    working folder before its command line, so the options Fledge passes override the
    file's ``CmdLine``.

    Args:
        work: The new folder of the lint.
        configuration: The project's ``.chktexrc``, as Fledge judged it.

    Returns:
        The probe's folder and the working folder.

    Raises:
        OSError: A folder exists already or cannot be created.
    """
    probe, workspace = work / "version", work / "run"
    work.mkdir(parents=True)
    probe.mkdir()
    workspace.mkdir()
    if configuration.content is not None:
        (workspace / CONFIGURATION_NAME).write_bytes(configuration.content)
    return probe, workspace


def _command(token: str, copy: Path) -> list[str]:
    r"""Return the chktex command that checks one frozen copy.

    ``-q`` drops the banner, ``-v0`` the twirling baton, and ``-I0`` keeps chktex from
    following ``\input``; ``-f``, last among the output options, sets the format Fledge
    reads.
    """
    return [CHKTEX, "-q", "-v0", "-I0", "-f", output_format(token), "--", str(copy)]


async def _run_files(
    progress: _LintProgress,
    frozen: list[tuple[str, Path | str]],
    workspace: Path,
    runner: CommandRunner,
) -> None:
    """Run chktex on each frozen copy in turn until a limit or the sandbox stops it.

    Args:
        progress: What the lint found so far; every run adds to it.
        frozen: Each file with its copy, or with why it could not be copied.
        workspace: chktex's working folder.
        runner: The runner of the job.
    """
    for index, (name, copy) in enumerate(frozen):
        if isinstance(copy, str):
            progress.problems.append((name, copy))
            continue
        try:
            result = await runner.run(
                _command(progress.token, copy),
                cwd=workspace,
                workspace=workspace,
                readonly_inputs=(copy,),
            )
        except PreparationError as error:
            progress.problems.append((name, f"chktex could not run ({error})"))
            stopped = True
        else:
            stopped = _record_run(progress, name, copy, result, runner)
        if stopped:
            progress.unchecked.extend(item for item, _ in frozen[index + 1 :])
            return


def _incomplete_finding(progress: _LintProgress) -> Finding | None:
    """Report the files whose runs did not complete or did not happen, and unreadable output.

    Returns:
        One inconclusive finding, or ``None`` when every run completed and was read.
    """
    sentences = []
    if progress.problems:
        name, reason = progress.problems[0]
        others = len(progress.problems) - 1
        more = f" ({_plural(others, 'more file')})" if others else ""
        sentences.append(f"chktex did not complete for {name}{more}: {reason}.")
    if progress.unchecked:
        count = len(progress.unchecked)
        sentences.append(f"{_plural(count, 'file')} {'was' if count == 1 else 'were'} not checked.")
    if progress.unreadable_lines:
        lines = _plural(progress.unreadable_lines, "line")
        sentences.append(f"{lines} of chktex's output could not be read.")
    if not sentences:
        return None
    return _chktex_finding(
        " ".join(sentences),
        "inconclusive",
        suggestion=(
            "Run chktex on the named files to see what stops it, or raise timeout_seconds "
            "when a time limit stopped it."
        ),
        details={
            "files": [name for name, _ in progress.problems[:_MAX_NAMED_FILES]],
            "unchecked_files": progress.unchecked[:_MAX_NAMED_FILES],
            "unchecked_file_count": len(progress.unchecked),
            "unreadable_lines": progress.unreadable_lines,
        },
    )


def _lint_findings(progress: _LintProgress) -> list[Finding]:
    """Turn what the runs found into findings: the warnings, the rest counted, the gaps.

    Returns:
        A finding for each kept warning, then one counting the warnings not listed, then
        one about incomplete runs; or a passed finding when chktex warned about nothing and
        every run completed.
    """
    findings = [_warning_finding(name, warning) for name, warning in progress.warnings]
    unlisted = progress.total - len(progress.warnings)
    if unlisted:
        findings.append(
            _chktex_finding(
                f"chktex reported {_plural(unlisted, 'more warning')} that the report does "
                "not list.",
                "failed",
                suggestion=(
                    "Run chktex on the sources to see every warning, or switch off the "
                    "warnings you do not want in a .chktexrc beside the main file."
                ),
                details={"listed_warnings": len(progress.warnings), "unlisted_warnings": unlisted},
                bounded=True,
            )
        )
    incomplete = _incomplete_finding(progress)
    if incomplete is not None:
        findings.append(incomplete)
    if not findings:
        checked = len(progress.files)
        findings.append(
            _chktex_finding(
                f"chktex reported no warnings in {_plural(checked, 'file')}.",
                "passed",
                details={"checked_files": checked},
            )
        )
    return findings


async def _lint_files(
    inputs: _LintInputs, root: Path, work: Path, runner: CommandRunner
) -> list[Finding]:
    """Probe chktex, freeze the files, run chktex on each and report what it found.

    Args:
        inputs: The files to check and the accepted configuration.
        root: The project root.
        work: The new folder for the lint's working folders and copies.
        runner: The runner of the job.

    Returns:
        The findings of the lint.
    """
    try:
        probe, workspace = await run_in_thread(_prepare_folders, work, inputs.configuration)
        await runner.tool_version([CHKTEX, "--version"], probe)
    except ToolUnavailableError as error:
        return [_not_installed(error)]
    except (OSError, PreparationError) as error:
        return [_not_run(f"chktex could not be started ({error})", "Run fledge doctor.")]
    progress = _LintProgress(new_run_token(), inputs.files[:MAX_CHECKED_FILES])
    progress.unchecked.extend(inputs.files[MAX_CHECKED_FILES:])
    frozen = await run_in_thread(_freeze_sources, runner, root, progress.files, work / "inputs")
    await _run_files(progress, frozen, workspace, runner)
    logger.debug(
        "chktex lint: %d files, %d warnings, %d incomplete, %d unchecked",
        len(progress.files),
        progress.total,
        len(progress.problems),
        len(progress.unchecked),
    )
    return _lint_findings(progress)


async def lint_with_chktex(
    root: Path,
    main: str,
    work: Path,
    runner: CommandRunner,
    *,
    analysed: AnalysedSource | None = None,
) -> list[Finding]:
    """Run chktex in the sandbox on the TeX files ``main`` uses and report its warnings.

    Every finding is a suggestion of CHK001, so the lint never changes the outcome. A
    ``.chktexrc`` beside ``main`` that chktex may not read stops the lint before chktex
    runs; so do a missing chktex and sources that cannot be read.

    Args:
        root: The project root.
        main: The main file, relative to ``root``.
        work: A folder that must not exist yet; it receives the copies chktex reads and
            chktex's working folders.
        runner: The runner of the job, which runs chktex in the sandbox.
        analysed: The job's shared analysis of ``root`` and ``main``, if there is one.

    Returns:
        The findings: chktex's warnings, a count of those not listed and any gaps; or one
        finding that says why chktex was not run.
    """
    analysis = reuse_or_analyse(root, main, analysed)
    try:
        inputs = await run_in_thread(_lint_inputs, analysis, root, main)
    except (OSError, PreparationError) as error:
        return [_not_run(f"the sources could not be read ({error})", "Run fledge inspect.")]
    configuration = inputs.configuration
    if configuration.refusal is not None:
        logger.debug("chktex lint: project configuration refused")
        return [_refused_configuration(configuration)]
    if not inputs.files:
        return [
            _chktex_finding(
                "chktex was not run: the main file and the files it inputs include no TeX "
                "file that Fledge could read.",
                "skipped",
            )
        ]
    return await _lint_files(inputs, root, work, runner)
